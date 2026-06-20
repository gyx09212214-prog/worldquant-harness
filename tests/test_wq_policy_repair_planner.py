import json
from pathlib import Path

from quantgpt.wq_policy_repair_planner import (
    PolicyRepairPlannerConfig,
    build_policy_repair_candidates,
    build_policy_repair_plan,
    build_policy_repair_records,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def test_policy_repair_records_generate_self_corr_and_concentration_candidates():
    rows = [
        {
            "alpha_id": "sc1",
            "tag": "iv-eps-near",
            "expression": "rank(0.82 * rank(ts_rank(actual_eps_value_quarterly / close, 90)) + 0.18 * rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5)))",
            "triage_bucket": "near_miss_repair",
            "triage_reason": "self-correlation failed (0.8318)",
            "sc_value": 0.8318,
            "sharpe": 2.4,
            "fitness": 1.5,
            "turnover": 0.25,
            "source_fields": [
                "actual_eps_value_quarterly",
                "implied_volatility_call_120",
                "implied_volatility_put_120",
            ],
        },
        {
            "alpha_id": "cw1",
            "tag": "concentrated",
            "expression": "rank(group_neutralize(actual_eps_value_quarterly / vwap, industry))",
            "triage_bucket": "near_miss_repair",
            "triage_reason": "platform check failed",
            "failed_platform_checks": [{"name": "CONCENTRATED_WEIGHT", "result": "FAIL"}],
            "sharpe": 2.2,
            "fitness": 1.4,
            "turnover": 0.2,
        },
    ]

    records = build_policy_repair_records(rows, max_repairs_per_row=5)
    candidates = build_policy_repair_candidates(records)

    assert {row["failure_kind"] for row in records} == {"self_correlation_fail", "concentrated_weight"}
    assert all(row["candidate_expressions"] for row in records)
    assert any("pcr_oi_60" in row["expression"] for row in candidates)
    assert any("ts_decay_linear" in row["expression"] for row in candidates)
    assert any((row.get("simulation_settings") or {}).get("truncation") == 0.05 for row in candidates)
    assert any((row.get("simulation_settings") or {}).get("maxPosition") == "ON" for row in candidates)
    assert all(row["source"] == "wq_policy_repair_planner" for row in candidates)


def test_policy_repair_plan_writes_artifacts(tmp_path):
    review = tmp_path / "review.jsonl"
    output = tmp_path / "repair"
    _write_jsonl(review, [{
        "alpha_id": "sc2",
        "tag": "sales-eps",
        "expression": "rank(0.45 * ts_rank(actual_sales_value_quarterly / enterprise_value, 80) + 0.25 * ts_rank(actual_eps_value_quarterly / vwap, 80) + 0.15 * ts_rank(change_in_eps_surprise, 60) + 0.15 * rank(ts_corr(close, volume, 20)) - ts_rank(returns, 40))",
        "triage_bucket": "near_miss_repair",
        "triage_reason": "self-correlation failed (0.8251)",
        "sc_value": 0.8251,
        "sharpe": 1.8,
        "fitness": 1.1,
        "turnover": 0.32,
        "source_fields": [
            "actual_sales_value_quarterly",
            "actual_eps_value_quarterly",
            "change_in_eps_surprise",
        ],
    }])

    plan = build_policy_repair_plan(PolicyRepairPlannerConfig(
        review_paths=(review,),
        output_dir=output,
        max_candidates=5,
    ))

    assert plan["summary"]["review_rows"] == 1
    assert plan["summary"]["repair_records"] == 1
    assert plan["summary"]["candidates"] >= 1
    assert (output / "repair_candidates.jsonl").is_file()
    assert (output / "repair_plan.md").read_text(encoding="utf-8").startswith("---")
