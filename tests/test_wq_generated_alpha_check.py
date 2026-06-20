import json
import shutil
import uuid
from pathlib import Path

import pytest

from scripts import check_wq_generated_alphas as api_check


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

    def submit_alpha(self, alpha_id):
        self.submitted.append(alpha_id)
        raise AssertionError("check-only flow must not submit")

    def close(self):
        self.closed = True


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_classify_api_check_distinguishes_active_pending_and_failures():
    assert api_check.classify_api_check({"ok": False}) == "api_check_failed"
    assert api_check.classify_api_check({"ok": True, "sc_result": "FAIL"}) == "self_correlation_fail"
    assert api_check.classify_api_check({"ok": True, "prod_corr_result": "FAIL"}) == "prod_correlation_fail"
    assert api_check.classify_api_check({"ok": True, "sc_result": "PENDING"}) == "api_check_pending"
    assert api_check.classify_api_check({"ok": True, "sc_result": "MISSING", "prod_corr_result": "MISSING"}) == "api_check_pending"
    assert api_check.classify_api_check({"ok": False, "review_failure_kind": "correlation_pending"}) == "api_check_pending"
    assert api_check.classify_api_check({"ok": True, "status": "ACTIVE", "sc_value": 0.64}) == "platform_active_sc_below_cutoff"
    assert api_check.classify_api_check({"ok": True, "status": "ACTIVE", "sc_value": 0.74}) == "platform_active_sc_above_cutoff"


def test_load_alpha_rows_defaults_to_candidate_statuses(workdir):
    path = workdir / "results.jsonl"
    rows = [
        {"alpha_id": "pending", "status": "pending_correlation_check", "expression": "rank(close)"},
        {"alpha_id": "weak", "status": "simulated", "submit_eligible": False, "expression": "rank(open)"},
        {"alpha_id": "eligible", "status": "simulated", "submit_eligible": True, "expression": "rank(volume)"},
        {"alpha_id": "pending", "status": "pending_correlation_check", "expression": "rank(close)"},
        {"status": "pending_correlation_check", "expression": "rank(high)"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    loaded = api_check.load_alpha_rows([path])
    assert [row["alpha_id"] for row in loaded] == ["pending", "eligible", "pending"]
    assert [row["alpha_id"] for row in api_check._dedupe_by_alpha_id(loaded)] == ["pending", "eligible"]

    loaded_all = api_check.load_alpha_rows([path], include_all=True)
    assert [row["alpha_id"] for row in loaded_all] == ["pending", "weak", "eligible", "pending"]


def test_check_generated_alphas_writes_read_only_api_results(workdir, monkeypatch):
    input_path = workdir / "results.jsonl"
    input_path.write_text(
        "\n".join([
            json.dumps({"alpha_id": "active", "status": "pending_correlation_check", "expression": "rank(close)"}),
            json.dumps({"alpha_id": "pending", "status": "eligible", "expression": "rank(open)"}),
        ]),
        encoding="utf-8",
    )
    output_path = workdir / "api_check.jsonl"
    summary_path = workdir / "api_check_summary.json"
    fake_client = FakeClient()

    def fake_run_check_alphas(_client, alpha_ids):
        assert alpha_ids == ["active", "pending"]
        return {
            "alphas": {
                "active": {
                    "ok": True,
                    "status": "ACTIVE",
                    "grade": "AVERAGE",
                    "sharpe": 1.7,
                    "fitness": 1.2,
                    "turnover": 0.3,
                    "sc_result": "MISSING",
                    "sc_value": 0.64,
                },
                "pending": {
                    "ok": True,
                    "status": "UNSUBMITTED",
                    "grade": "AVERAGE",
                    "sharpe": 1.5,
                    "fitness": 1.0,
                    "turnover": 0.33,
                    "sc_result": "PENDING",
                },
            }
        }

    monkeypatch.setattr(api_check, "load_dotenv", lambda root: None)
    monkeypatch.setattr(api_check, "is_configured", lambda account: True)
    monkeypatch.setattr(api_check, "get_client", lambda account: fake_client)
    monkeypatch.setattr(api_check, "run_check_alphas", fake_run_check_alphas)

    summary = api_check.check_generated_alphas(
        input_paths=[input_path],
        output_path=output_path,
        summary_output_path=summary_path,
        account="primary",
    )

    records = read_jsonl(output_path)
    assert summary["total"] == 2
    assert summary["counts"] == {
        "api_check_pending": 1,
        "platform_active_sc_below_cutoff": 1,
    }
    assert [record["api_check_status"] for record in records] == [
        "platform_active_sc_below_cutoff",
        "api_check_pending",
    ]
    assert fake_client.closed is True
    assert fake_client.submitted == []
    assert json.loads(summary_path.read_text(encoding="utf-8"))["total"] == 2
