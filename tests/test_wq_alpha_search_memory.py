import json
import shutil
import uuid
from pathlib import Path

from worldquant_harness.wq_alpha_search_memory import WQAlphaSearchMemoryConfig, build_alpha_search_memory


def _workdir() -> Path:
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / f"wq_alpha_search_memory_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_alpha_search_memory_builds_repair_queue_and_skills():
    workdir = _workdir()
    try:
        reports = workdir / "reports"
        output = workdir / "alpha_search"
        expression = "rank(ts_rank(vwap / close, 20) - ts_rank(returns, 20))"
        _write_jsonl(reports / "run_a" / "simulation_results.jsonl", [
            {
                "alpha_id": "alpha_near",
                "expression": expression,
                "tag": "kq3n-retest-ind-d8-t003",
                "source_family": "kq3n_near_sc_cutoff_repair",
                "metrics": {
                    "sharpe": 2.58,
                    "fitness": 1.42,
                    "returns": 0.151,
                    "turnover": 0.37,
                },
                "submit_eligible": True,
                "simulation_settings": {
                    "neutralization": "INDUSTRY",
                    "decay": 8,
                    "truncation": 0.03,
                },
            },
            {
                "alpha_id": "alpha_sub",
                "expression": "rank(ts_rank(close, 5))",
                "tag": "bad-sub",
                "source_family": "sub_universe_family",
                "sharpe": 2.9,
                "fitness": 1.2,
                "turnover": 0.2,
                "failed_platform_checks": [
                    {"name": "LOW_SUB_UNIVERSE_SHARPE", "result": "FAIL"},
                    {"name": "SELF_CORRELATION", "result": "FAIL", "value": 0.74},
                ],
            },
            {
                "alpha_id": "alpha_ready",
                "expression": "rank(ts_rank(actual_sales_value_quarterly / assets, 80))",
                "tag": "ready-score",
                "source_family": "quality_value",
                "sharpe": 1.35,
                "fitness": 1.25,
                "returns": 0.09,
                "turnover": 0.18,
            },
        ])
        _write_jsonl(reports / "run_a" / "check_results.jsonl", [
            {
                "alpha_id": "alpha_near",
                "api_check_status": "self_correlation_fail",
                "sc_result": "FAIL",
                "sc_value": 0.7332,
                "review_failure_kind": "self_correlation",
            },
            {
                "alpha_id": "alpha_ready",
                "api_check_status": "api_check_readable",
                "sc_result": "PASS",
                "sc_value": 0.21,
                "prod_corr_result": "PASS",
                "prod_corr_value": 0.08,
            },
        ])
        _write_jsonl(reports / "run_b" / "submit_existing_results.jsonl", [
            {
                "alpha_id": "alpha_active",
                "expression": expression,
                "tag": "kq3n-retest-subind-d4-t003",
                "domain": "kq3n_near_sc_cutoff_repair",
                "candidate_metrics": {
                    "sharpe": 2.63,
                    "fitness": 1.37,
                    "returns": 0.1509,
                    "turnover": 0.5574,
                },
                "ok": True,
                "final_status": "ACTIVE",
                "platform_status": "ACTIVE",
                "review_checks": {
                    "self_correlation": {
                        "name": "SELF_CORRELATION",
                        "result": "MISSING",
                        "value": 0.7302,
                    }
                },
            },
        ])

        result = build_alpha_search_memory(WQAlphaSearchMemoryConfig(
            reports_dir=reports,
            output_dir=output,
            decays=(4, 8),
            truncations=(0.02, 0.03),
            neutralizations=("SUBINDUSTRY", "INDUSTRY"),
            max_candidates_per_parent=4,
        ))

        ledger = _read_jsonl(output / "trajectory_ledger.jsonl")
        candidates = _read_jsonl(output / "near_pass_repair_candidates.jsonl")
        submit_targets = _read_jsonl(output / "top_submit_targets.jsonl")
        check_targets = _read_jsonl(output / "top_check_targets.jsonl")
        skills = _read_jsonl(output / "skill_memory.jsonl")

        near = next(row for row in ledger if row["alpha_id"] == "alpha_near")
        assert result["ok"] is True
        assert near["lifecycle"] == "self_corr_fail"
        assert near["expression"] == expression
        assert near["wq_score"] == 1.42
        assert near["sc_value"] == 0.7332
        assert result["summary"]["funnel"]["active_count"] == 1
        assert result["summary"]["funnel"]["high_score_count"] == 4
        assert result["summary"]["funnel"]["platform_eligible_count"] == 4
        assert result["summary"]["funnel"]["near_sc_repair_parent_count"] == 1
        assert candidates
        assert {row["parent_alpha_id"] for row in candidates} == {"alpha_near"}
        assert all(row["parent_alpha_id"] != "alpha_sub" for row in candidates)
        assert all(row["source_family"] == "near_sc_cutoff_settings_repair" for row in candidates)
        assert any(row["simulation_settings"]["neutralization"] == "SUBINDUSTRY" for row in candidates)
        assert submit_targets[0]["alpha_id"] == "alpha_ready"
        assert submit_targets[0]["wq_score"] == 1.25
        assert submit_targets[0]["correlation_risk"] < 0.5
        assert check_targets == []
        repair_skill = next(row for row in skills if row["skill_id"] == "near_sc_cutoff_settings_repair")
        assert repair_skill["evidence"]["near_parent_count"] == 1
        assert repair_skill["evidence"]["active_high_score_examples"][0]["alpha_id"] == "alpha_active"
        submit_skill = next(row for row in skills if row["skill_id"] == "top5_high_score_low_corr_submit")
        assert submit_skill["evidence"]["current_submit_target_count"] == 1
        assert (output / "alpha_search_report.md").is_file()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_alpha_search_memory_uses_configured_high_score_and_sc_thresholds():
    workdir = _workdir()
    try:
        reports = workdir / "reports"
        output = workdir / "alpha_search"
        _write_jsonl(reports / "run_a" / "simulation_results.jsonl", [
            {
                "alpha_id": "alpha_near_default_only",
                "expression": "rank(ts_rank(vwap / close, 20))",
                "tag": "near-default",
                "source_family": "test_family",
                "metrics": {
                    "sharpe": 2.4,
                    "fitness": 1.42,
                    "returns": 0.12,
                    "turnover": 0.25,
                },
                "simulation_settings": {
                    "neutralization": "INDUSTRY",
                    "decay": 4,
                    "truncation": 0.03,
                },
            }
        ])
        _write_jsonl(reports / "run_a" / "check_results.jsonl", [
            {
                "alpha_id": "alpha_near_default_only",
                "api_check_status": "self_correlation_fail",
                "sc_result": "FAIL",
                "sc_value": 0.7332,
                "review_failure_kind": "self_correlation",
            }
        ])

        result = build_alpha_search_memory(WQAlphaSearchMemoryConfig(
            reports_dir=reports,
            output_dir=output,
            min_high_score=1.5,
            sc_min=0.75,
            sc_max=0.82,
        ))

        ledger = _read_jsonl(output / "trajectory_ledger.jsonl")
        row = next(item for item in ledger if item["alpha_id"] == "alpha_near_default_only")

        assert row["is_high_score"] is False
        assert row["is_near_sc_repair_parent"] is False
        assert result["summary"]["funnel"]["high_score_count"] == 0
        assert result["summary"]["funnel"]["near_sc_repair_parent_count"] == 0
        assert _read_jsonl(output / "near_pass_repair_candidates.jsonl") == []
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
