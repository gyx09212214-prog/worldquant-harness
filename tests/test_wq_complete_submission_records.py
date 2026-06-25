import json
import shutil
import uuid
from pathlib import Path

import pytest

from worldquant_harness.wq_complete_submission_records import (
    WQCompleteSubmissionRecordsConfig,
    collect_complete_submission_records,
)


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / f"wq_complete_records_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_complete_records_preserves_best_metrics_when_later_snapshot_is_light(workdir):
    reports = workdir / "reports"
    _write_jsonl(
        reports / "old_snapshot" / "platform_alphas.jsonl",
        [
            {
                "id": "alpha_active",
                "status": "ACTIVE",
                "dateSubmitted": "2026-05-29T04:00:18-04:00",
                "regular": {"code": "rank(ts_rank(cashflow_op / cap, 80))"},
                "is": {"sharpe": 1.8, "fitness": 1.15, "returns": 0.09, "turnover": 0.218},
            }
        ],
    )
    _write_jsonl(
        reports / "light_snapshot" / "platform_alphas.jsonl",
        [
            {
                "id": "alpha_active",
                "status": "ACTIVE",
                "regular": {"code": "rank(ts_rank(cashflow_op / cap, 80))"},
            }
        ],
    )

    summary = collect_complete_submission_records(WQCompleteSubmissionRecordsConfig(
        reports_dir=reports,
        output_dir=workdir / "out",
        platform_enabled=False,
    ))

    records = _read_jsonl(Path(summary["files"]["alpha_records"]))
    assert len(records) == 1
    assert records[0]["canonical_status"] == "ACTIVE"
    assert records[0]["sharpe"] == 1.8
    assert records[0]["fitness"] == 1.15
    assert summary["coverage"]["active_metric_complete_rate"] == 1.0


def test_complete_records_final_sc_fail_overrides_check_only_pass(workdir):
    reports = workdir / "reports"
    _write_jsonl(
        reports / "check_run" / "submit_existing_results.jsonl",
        [
            {
                "alpha_id": "alpha_sc",
                "expression": "rank(open)",
                "final_status": "PRECHECK_PASS",
                "detail": "precheck passed; check-only mode skipped submit",
                "candidate_metrics": {"sharpe": 1.7, "fitness": 1.2, "returns": 0.08, "turnover": 0.15},
            }
        ],
    )
    _write_jsonl(
        reports / "submit_run" / "submit_existing_results.jsonl",
        [
            {
                "alpha_id": "alpha_sc",
                "expression": "rank(open)",
                "final_status": "SC_FAIL",
                "failure_kind": "self_correlation",
                "detail": "SELF_CORRELATION FAIL: value=0.91 > limit=0.7",
                "candidate_metrics": {"sharpe": 1.7, "fitness": 1.2, "returns": 0.08, "turnover": 0.15},
            }
        ],
    )

    summary = collect_complete_submission_records(WQCompleteSubmissionRecordsConfig(
        reports_dir=reports,
        output_dir=workdir / "out",
        platform_enabled=False,
    ))

    records = _read_jsonl(Path(summary["files"]["alpha_records"]))
    assert records[0]["canonical_status"] == "SC_FAIL"
    assert records[0]["experience_label"] == "sc_hard_negative"
    assert records[0]["sc_value"] == 0.91
    assert summary["rates"]["actual_submit"]["active"] == 0


def test_complete_records_fetches_read_only_detail_for_missing_local_submit_id(workdir):
    reports = workdir / "reports"
    _write_jsonl(
        reports / "submit_run" / "submit_existing_results.jsonl",
        [
            {
                "alpha_id": "alpha_new",
                "expression": "rank(close)",
                "final_status": "ACTIVE",
                "detail": "submitted and ACTIVE",
                "candidate_metrics": {"sharpe": 1.29, "fitness": 1.09, "returns": 0.07, "turnover": 0.1},
            }
        ],
    )

    class FakeClient:
        def authenticate(self):
            return True

        def close(self):
            return None

        def get_json(self, path, params=None):
            assert path == "/users/self/alphas"
            return {"ok": True, "count": 0, "results": []}

        def get_alpha_raw(self, alpha_id):
            assert alpha_id == "alpha_new"
            return {
                "ok": True,
                "data": {
                    "id": "alpha_new",
                    "status": "ACTIVE",
                    "dateSubmitted": "2026-06-25T10:41:08-04:00",
                    "regular": {"code": "rank(close)"},
                    "is": {"sharpe": 1.31, "fitness": 1.11, "returns": 0.071, "turnover": 0.097},
                },
            }

    summary = collect_complete_submission_records(
        WQCompleteSubmissionRecordsConfig(
            reports_dir=reports,
            output_dir=workdir / "out",
            platform_enabled=True,
            detail_enabled=True,
        ),
        client_factory=lambda account: FakeClient(),
    )

    records = _read_jsonl(Path(summary["files"]["alpha_records"]))
    assert records[0]["canonical_status"] == "ACTIVE"
    assert records[0]["sharpe"] == 1.31
    assert records[0]["metrics_source"] == "alpha_detail"
    assert summary["platform"]["detail_count"] == 1
    details = _read_jsonl(Path(summary["files"]["alpha_details"]))
    assert details[0]["ok"] is True
