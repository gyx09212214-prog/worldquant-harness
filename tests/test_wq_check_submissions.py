import json
import shutil
import uuid
from pathlib import Path

import pytest

from scripts import check_wq_submissions


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FakeClient:
    def __init__(self):
        self.closed = False
        self.submitted = []

    def authenticate(self):
        return True

    def check_alpha_submission(self, alpha_id):
        if alpha_id == "pass":
            return {
                "ok": True,
                "status": "UNSUBMITTED",
                "is": {"sharpe": 2.0, "fitness": 1.35, "turnover": 0.3},
                "review_checks": {
                    "self_correlation": {"name": "SELF_CORRELATION", "result": "PASS", "value": 0.7961, "limit": 0.7},
                    "prod_correlation": {"name": "PROD_CORRELATION", "result": "MISSING", "value": 0.0, "limit": None},
                    "failed": [],
                    "pending": [],
                },
            }
        return {
            "ok": False,
            "status": "UNSUBMITTED",
            "failure_kind": "self_correlation",
            "is": {"sharpe": 1.5, "fitness": 1.1, "turnover": 0.2},
            "review_checks": {
                "self_correlation": {"name": "SELF_CORRELATION", "result": "FAIL", "value": 0.8457, "limit": 0.7},
                "prod_correlation": {"name": "PROD_CORRELATION", "result": "MISSING", "value": None, "limit": None},
                "failed": ["self_correlation"],
                "pending": [],
            },
        }

    def submit_alpha(self, alpha_id):
        self.submitted.append(alpha_id)
        raise AssertionError("check submission flow must not submit")

    def close(self):
        self.closed = True


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_check_wq_submissions_writes_check_only_results(workdir, monkeypatch):
    output_path = workdir / "submission_check.jsonl"
    summary_path = workdir / "submission_check_summary.json"
    fake_client = FakeClient()

    monkeypatch.setattr(check_wq_submissions, "load_dotenv", lambda root: None)
    monkeypatch.setattr(check_wq_submissions, "is_configured", lambda account: True)
    monkeypatch.setattr(check_wq_submissions, "get_client", lambda account: fake_client)

    summary = check_wq_submissions.check_wq_submissions(
        input_paths=[],
        alpha_ids=["pass", "fail", "pass"],
        output_path=output_path,
        summary_output_path=summary_path,
        account="primary",
    )

    records = read_jsonl(output_path)
    assert summary["total"] == 2
    assert summary["counts"] == {
        "api_check_readable": 1,
        "self_correlation_fail": 1,
    }
    assert [record["api_check_status"] for record in records] == [
        "api_check_readable",
        "self_correlation_fail",
    ]
    assert records[0]["sc_value"] == 0.7961
    assert records[1]["sc_limit"] == 0.7
    assert fake_client.closed is True
    assert fake_client.submitted == []
    assert json.loads(summary_path.read_text(encoding="utf-8"))["total"] == 2


def test_check_wq_submissions_resume_only_pending_rechecks_pending_records(workdir, monkeypatch):
    output_path = workdir / "submission_check.jsonl"
    summary_path = workdir / "submission_check_summary.json"
    prior_records = [
        {
            "alpha_id": "pass",
            "expression": "rank(close)",
            "api_check_status": "api_check_readable",
            "platform_status": "UNSUBMITTED",
            "sharpe": 2.0,
            "fitness": 1.3,
            "turnover": 0.2,
            "sc_result": "PASS",
        },
        {
            "alpha_id": "pending",
            "expression": "rank(open)",
            "api_check_status": "api_check_pending",
            "platform_status": "UNSUBMITTED",
            "source_status": "eligible",
            "sharpe": 1.5,
            "fitness": 1.1,
            "turnover": 0.3,
            "sc_result": "PENDING",
        },
    ]
    output_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in prior_records) + "\n",
        encoding="utf-8",
    )
    fake_client = FakeClient()
    checked: list[str] = []

    def fake_check(alpha_id):
        checked.append(alpha_id)
        return {
            "ok": True,
            "status": "UNSUBMITTED",
            "is": {"sharpe": 1.7, "fitness": 1.2, "turnover": 0.25},
            "review_checks": {
                "self_correlation": {"name": "SELF_CORRELATION", "result": "PASS", "value": 0.6, "limit": 0.7},
                "prod_correlation": {"name": "PROD_CORRELATION", "result": "MISSING", "value": None, "limit": None},
                "failed": [],
                "pending": [],
            },
        }

    fake_client.check_alpha_submission = fake_check
    monkeypatch.setattr(check_wq_submissions, "load_dotenv", lambda root: None)
    monkeypatch.setattr(check_wq_submissions, "is_configured", lambda account: True)
    monkeypatch.setattr(check_wq_submissions, "get_client", lambda account: fake_client)

    summary = check_wq_submissions.check_wq_submissions(
        input_paths=[output_path],
        alpha_ids=[],
        output_path=output_path,
        summary_output_path=summary_path,
        account="primary",
        include_all=True,
        resume=True,
        only_pending=True,
    )

    records = read_jsonl(output_path)
    assert checked == ["pending"]
    assert summary["total"] == 2
    assert summary["newly_checked"] == 1
    assert summary["counts"] == {"api_check_readable": 2}
    assert [record["alpha_id"] for record in records] == ["pass", "pending"]
    assert records[1]["sc_result"] == "PASS"
