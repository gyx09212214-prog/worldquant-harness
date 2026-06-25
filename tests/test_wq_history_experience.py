import json
import shutil
import uuid
from pathlib import Path

import pytest

from worldquant_harness.wq_history_experience import (
    WQHistoryExperienceConfig,
    build_pnl_corr_islands,
    collect_history_experience,
    collect_pnl_curve_index,
    normalize_event,
)


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / f"wq_history_experience_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_normalize_event_maps_self_correlation_submit_failure():
    event = normalize_event(
        {
            "alpha_id": "alpha_sc",
            "expression": "rank(open)",
            "failure_kind": "self_correlation",
            "detail": "SELF_CORRELATION FAIL: value=0.9025 > limit=0.7",
            "final_status": "UNSUBMITTED",
        },
        source_type="submit_existing_result",
        source_file="reports/run/submit_existing_results.jsonl",
    )

    assert event["api_check_status"] == "self_correlation_fail"
    assert event["failure_kind"] == "self_correlation_fail"
    assert event["severity"] == "block"
    assert event["lifecycle_status"] == "self_corr_fail"
    assert event["sc_value"] == 0.9025


def test_collect_history_experience_writes_local_canonical_outputs(workdir):
    reports = workdir / "reports"
    run = reports / "wq_agent_runs" / "local_run"
    _write_jsonl(
        run / "presubmit_ready_sequential.jsonl",
        [
            {
                "alpha_id": "ready1",
                "expression": "rank(open)",
                "status": "eligible",
                "sharpe": 1.8,
                "fitness": 1.2,
            }
        ],
    )
    _write_jsonl(
        run / "presubmit_rejected.jsonl",
        [
            {
                "alpha_id": "reject1",
                "expression": "rank(close)",
                "presubmit_reject_reason": "too_similar_to_real_or_virtual_active",
                "nearest_similarity": 0.82,
            }
        ],
    )

    summary = collect_history_experience(
        WQHistoryExperienceConfig(
            reports_dir=reports,
            output_dir=workdir / "out",
            platform_enabled=False,
        )
    )

    assert summary["ok"] is True
    assert summary["event_count"] == 2
    assert summary["memory_count"] == 1
    assert summary["elite_count"] == 1
    events = _read_jsonl(Path(summary["files"]["history_alpha_events"]))
    assert {row["alpha_id"] for row in events} == {"ready1", "reject1"}
    memory = _read_jsonl(Path(summary["files"]["history_experience_memory"]))
    assert memory[0]["failure_kind"] == "high_similarity"
    profile = _read_json(Path(summary["files"]["history_research_profile_candidate"]))
    assert profile["history_experience"]["memory_count"] == 1


def test_collect_history_experience_uses_fake_platform_check(workdir):
    class FakeClient:
        def authenticate(self):
            return True

        def close(self):
            return None

        def get_json(self, path, params=None):
            assert path == "/users/self/alphas"
            return {
                "ok": True,
                "count": 1,
                "results": [
                    {
                        "id": "platform1",
                        "status": "UNSUBMITTED",
                        "regular": {"code": "rank(volume)"},
                        "is": {"sharpe": 1.7, "fitness": 1.1, "turnover": 0.2},
                        "settings": {"region": "USA", "universe": "TOP3000", "delay": 1},
                    }
                ],
            }

        def check_alpha_submission(self, alpha_id):
            assert alpha_id == "platform1"
            return {
                "ok": True,
                "status": "UNSUBMITTED",
                "review_checks": {
                    "self_correlation": {"name": "SELF_CORRELATION", "result": "FAIL", "value": 0.81, "limit": 0.7},
                    "prod_correlation": {"name": "PROD_CORRELATION", "result": "PASS", "value": 0.1, "limit": 0.7},
                },
                "failure_kind": "self_correlation",
            }

    summary = collect_history_experience(
        WQHistoryExperienceConfig(
            reports_dir=workdir / "reports",
            output_dir=workdir / "out",
            platform_enabled=True,
            check_policy="all",
        ),
        client_factory=lambda account: FakeClient(),
    )

    assert summary["platform"]["alpha_count"] == 1
    assert summary["platform"]["check_count"] == 1
    assert summary["counts"]["failure_kind"]["self_correlation_fail"] == 1
    checks = _read_jsonl(Path(summary["files"]["platform_check_results"]))
    assert checks[0]["api_check_status"] == "self_correlation_fail"


def test_pnl_corr_islands_group_highly_correlated_curves(workdir):
    reports = workdir / "reports"
    curve_a = [{"date": f"2024-01-{day:02d}", "pnl": float(day)} for day in range(1, 22)]
    curve_b = [{"date": f"2024-01-{day:02d}", "pnl": float(day * 2)} for day in range(1, 22)]
    _write_jsonl(reports / "alpha_a_pnl_curve.jsonl", curve_a)
    _write_jsonl(reports / "alpha_b_pnl_curve.jsonl", curve_b)

    index = collect_pnl_curve_index(reports, events=[])
    islands = build_pnl_corr_islands(index, min_overlap=20, island_abs_corr=0.7, warn_abs_corr=0.5)

    assert islands["alpha_count"] == 2
    assert len(islands["islands"]) == 1
    assert islands["islands"][0]["members"] == ["alpha_a", "alpha_b"]
