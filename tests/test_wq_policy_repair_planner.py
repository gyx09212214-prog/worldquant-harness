import json
from pathlib import Path

from worldquant_harness.wq_policy_repair_planner import (
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
            "effective_simulation_settings": {"truncation": 0.08, "maxPosition": "OFF"},
        },
    ]

    records = build_policy_repair_records(rows, max_repairs_per_row=5)
    candidates = build_policy_repair_candidates(records)

    assert {row["failure_kind"] for row in records} == {"self_correlation_fail", "concentrated_weight"}
    assert all(row["candidate_expressions"] for row in records)
    assert any("coefficient_variation_fy1_eps" in row["expression"] for row in candidates)
    assert all(
        "pcr_" not in row["expression"]
        for row in candidates
        if row.get("source_family") == "repair_self_corr_minimal_orthogonal"
    )
    assert any("ts_decay_linear" in row["expression"] for row in candidates)
    assert any((row.get("simulation_settings") or {}).get("truncation") == 0.05 for row in candidates)
    assert any((row.get("simulation_settings") or {}).get("maxPosition") == "ON" for row in candidates)
    assert any(
        row.get("source_family") == "repair_concentration_max_position_retest"
        and (row.get("simulation_settings") or {}).get("truncation") == 0.05
        for row in candidates
    )
    assert any((row.get("simulation_settings") or {}).get("truncation") == 0.03 for row in candidates)
    assert all(row["source"] == "wq_policy_repair_planner" for row in candidates)


def test_policy_repair_records_generate_metric_threshold_repairs():
    rows = [
        {
            "alpha_id": "metric1",
            "tag": "sales-earnmom-near",
            "expression": "rank(0.50 * ts_rank(actual_sales_value_quarterly / enterprise_value, 60) + 0.30 * ts_rank(earnings_momentum_composite_score, 50) + 0.20 * rank(ts_corr(vwap, volume, 40)) - ts_rank(returns, 30))",
            "triage_bucket": "near_miss_repair",
            "triage_reason": "platform check failed",
            "failed_platform_checks": [{"name": "LOW_FITNESS", "result": "FAIL", "value": 0.99, "limit": 1.0}],
            "sharpe": 1.66,
            "fitness": 0.99,
            "turnover": 0.344,
            "source_fields": [
                "actual_sales_value_quarterly",
                "earnings_momentum_composite_score",
                "enterprise_value",
                "returns",
                "volume",
                "vwap",
            ],
        }
    ]

    records = build_policy_repair_records(rows, max_repairs_per_row=5)
    candidates = build_policy_repair_candidates(records)

    assert [row["failure_kind"] for row in records] == ["metric_threshold_near_miss"]
    assert any("actual_sales_value_quarterly / enterprise_value, 80" in row["expression"] for row in candidates)
    assert all((row.get("simulation_settings") or {}).get("decay") != 12 for row in candidates)
    assert all("ts_decay_linear" not in row["expression"] for row in candidates)


def test_policy_repair_records_generate_sales_cap_revision_metric_repairs_without_ev_or_pcr():
    rows = [
        {
            "alpha_id": "j2Z9pzmk",
            "tag": "sales-cap-revision-near",
            "expression": "rank(group_neutralize(0.24 * ts_rank(ts_backfill(actual_sales_value_quarterly, 140) / cap, 170) + 0.22 * ts_rank(forward_sales_to_price, 170) + 0.18 * rank(-1 * earnings_certainty_rank_derivative) + 0.14 * ts_rank(earnings_revision_magnitude, 150) + 0.10 * rank(ts_corr(vwap, volume, 120)) - 0.14 * ts_rank(returns, 170), industry))",
            "triage_bucket": "near_miss_repair",
            "triage_reason": "platform check failed",
            "failed_platform_checks": [{"name": "LOW_FITNESS", "result": "FAIL", "value": 0.94, "limit": 1.0}],
            "sharpe": 1.26,
            "fitness": 0.94,
            "turnover": 0.1155,
            "source_fields": [
                "actual_sales_value_quarterly",
                "cap",
                "earnings_certainty_rank_derivative",
                "earnings_revision_magnitude",
                "forward_sales_to_price",
                "industry",
                "returns",
                "volume",
                "vwap",
            ],
        }
    ]

    records = build_policy_repair_records(rows, max_repairs_per_row=8)
    candidates = build_policy_repair_candidates(records)
    sales_cap_repairs = [
        row for row in candidates
        if row.get("source_family") == "repair_metric_sales_cap_revision_tune"
    ]

    assert [row["failure_kind"] for row in records] == ["metric_threshold_near_miss"]
    assert sales_cap_repairs
    assert any("earnings_momentum_composite_score" in row["expression"] for row in sales_cap_repairs)
    assert any("volume / adv20" in row["expression"] for row in sales_cap_repairs)
    assert all("enterprise_value" not in row["expression"] for row in sales_cap_repairs)
    assert all("pcr_" not in row["expression"] for row in sales_cap_repairs)


def test_policy_repair_prefers_actual_settings_when_platform_overrides_request():
    rows = [
        {
            "alpha_id": "actual-off",
            "tag": "requested-maxpos-but-actual-off",
            "expression": "rank(group_neutralize(actual_eps_value_quarterly / vwap, industry))",
            "triage_bucket": "near_miss_repair",
            "triage_reason": "platform check failed",
            "failed_platform_checks": [{"name": "CONCENTRATED_WEIGHT", "result": "FAIL"}],
            "sharpe": 2.0,
            "fitness": 1.2,
            "turnover": 0.2,
            "simulation_settings": {"truncation": 0.05, "maxPosition": "ON"},
            "actual_simulation_settings": {"truncation": 0.05, "maxPosition": "OFF"},
        }
    ]

    records = build_policy_repair_records(rows, max_repairs_per_row=6)
    candidates = build_policy_repair_candidates(records)

    assert any(row.get("source_family") == "repair_concentration_max_position_retest" for row in candidates)


def test_policy_repair_records_generate_high_self_corr_sales_family_rebuilds():
    rows = [
        {
            "alpha_id": "pw61ebEo",
            "tag": "sales-earnmom-vwap-high-sc",
            "expression": "rank(0.50 * ts_rank(actual_sales_value_quarterly / enterprise_value, 60) + 0.30 * ts_rank(earnings_momentum_composite_score, 50) + 0.20 * rank(ts_corr(vwap, volume, 40)) - ts_rank(returns, 30))",
            "triage_bucket": "hard_fail",
            "triage_reason": "self-correlation failed (0.8948)",
            "sc_value": 0.8948,
            "sharpe": 1.55,
            "fitness": 1.0,
            "turnover": 0.2681,
            "source_fields": [
                "actual_sales_value_quarterly",
                "earnings_momentum_composite_score",
                "enterprise_value",
                "returns",
                "volume",
                "vwap",
            ],
        }
    ]

    records = build_policy_repair_records(rows, max_repairs_per_row=5)
    candidates = build_policy_repair_candidates(records)

    assert [row["failure_kind"] for row in records] == ["self_correlation_fail"]
    assert any("snt1_d1_netearningsrevision" in row["expression"] for row in candidates)
    assert any("actual_cashflow_per_share_value_quarterly" in row["expression"] for row in candidates)
    assert all("earnings_momentum_composite_score" not in row["expression"] for row in candidates)


def test_policy_repair_records_generate_high_self_corr_equity_sales_eps_rebuilds():
    rows = [
        {
            "alpha_id": "KPbXZ2k1",
            "tag": "equity-sales-eps-high-sc",
            "expression": "rank(0.45 * ts_rank(equity / cap, 60) + 0.25 * ts_rank(forward_sales_to_price, 60) + 0.15 * ts_rank(change_in_eps_surprise, 60) + 0.15 * ts_rank(snt1_d1_netearningsrevision, 60) - ts_mean(ts_rank(returns, 20), 2))",
            "triage_bucket": "hard_fail",
            "triage_reason": "self-correlation failed (0.8641)",
            "sc_value": 0.8641,
            "sharpe": 1.58,
            "fitness": 1.03,
            "turnover": 0.2189,
            "source_fields": [
                "cap",
                "change_in_eps_surprise",
                "equity",
                "forward_sales_to_price",
                "returns",
                "snt1_d1_netearningsrevision",
            ],
        }
    ]

    records = build_policy_repair_records(rows, max_repairs_per_row=6)
    candidates = build_policy_repair_candidates(records)

    assert [row["failure_kind"] for row in records] == ["self_correlation_fail"]
    equity_rebuilds = [
        row for row in candidates
        if row.get("source_family") == "repair_self_corr_equity_sales_eps_rebuild"
    ]
    assert equity_rebuilds
    assert any("forward_book_value_to_price" in row["expression"] for row in equity_rebuilds)
    assert any("earnings_certainty_rank_derivative" in row["expression"] for row in equity_rebuilds)
    assert any("volume / adv20" in row["expression"] for row in equity_rebuilds)
    assert any("snt1_d1_netearningsrevision" not in row["expression"] for row in equity_rebuilds)
    assert all("pcr_" not in row["expression"] for row in equity_rebuilds)


def test_policy_repair_records_generate_cashflow_iv_near_threshold_repairs():
    rows = [
        {
            "alpha_id": "58wa826M",
            "tag": "cashflow-iv-near-sc",
            "expression": "rank(0.82 * rank(0.48 * ts_rank(cashflow_op / enterprise_value, 100) + 0.22 * rank(-1 * cashflow_efficiency_rank_derivative) + 0.15 * rank(ts_corr(close, volume, 20)) + 0.15 * rank(ts_mean((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), 5)) - ts_rank(returns, 40)) + 0.18 * rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5)))",
            "triage_bucket": "hard_fail",
            "triage_reason": "self-correlation failed (0.8561)",
            "sc_value": 0.8561,
            "sharpe": 2.4,
            "fitness": 1.63,
            "turnover": 0.3119,
            "source_fields": [
                "cashflow_op",
                "cashflow_efficiency_rank_derivative",
                "close",
                "enterprise_value",
                "implied_volatility_call_120",
                "implied_volatility_call_90",
                "implied_volatility_put_120",
                "implied_volatility_put_90",
                "returns",
                "volume",
            ],
        }
    ]

    records = build_policy_repair_records(rows, max_repairs_per_row=6)
    candidates = build_policy_repair_candidates(records)

    assert [row["failure_kind"] for row in records] == ["self_correlation_fail"]
    assert any("forward_cash_flow_to_price" in row["expression"] for row in candidates)
    assert any("forward_book_value_to_price" in row["expression"] for row in candidates)
    assert any("group_neutralize" in row["expression"] for row in candidates)


def test_policy_repair_records_remove_iv90_from_active_family_self_corr():
    rows = [
        {
            "alpha_id": "kq31QJRz",
            "tag": "repair-active-cf-forward-credit-v1-iv90-8218",
            "expression": "rank(0.82 * rank(group_neutralize(0.20 * group_rank(ts_rank(forward_cash_flow_to_price, 120), industry) + 0.16 * group_rank(ts_rank(cashflow_op / enterprise_value, 100), subindustry) + 0.12 * rank(-1 * relative_valuation_rank_derivative) + 0.12 * rank(-1 * credit_risk_premium_indicator) + 0.12 * rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5)) + 0.10 * rank(ts_corr(vwap, volume, 80)) - 0.12 * ts_rank(returns, 100), sector)) + 0.18 * rank(-1 * ts_rank(pcr_oi_60, 60)))",
            "triage_bucket": "near_miss_repair",
            "triage_reason": "self-correlation failed (0.8184)",
            "sc_value": 0.8184,
            "sharpe": 1.64,
            "fitness": 1.18,
            "turnover": 0.1573,
            "source_fields": [
                "cashflow_op",
                "credit_risk_premium_indicator",
                "enterprise_value",
                "forward_cash_flow_to_price",
                "implied_volatility_call_90",
                "implied_volatility_put_90",
                "pcr_oi_60",
                "relative_valuation_rank_derivative",
                "returns",
                "volume",
                "vwap",
            ],
        }
    ]

    records = build_policy_repair_records(rows, max_repairs_per_row=8)
    candidates = build_policy_repair_candidates(records)

    assert [row["failure_kind"] for row in records] == ["self_correlation_fail"]
    noiv_candidates = [
        row for row in candidates
        if row.get("source_family") == "repair_self_corr_active_iv90_noiv_cashflow_credit"
    ]
    assert noiv_candidates
    assert all("implied_volatility_call_90" not in row["expression"] for row in noiv_candidates)
    assert all("implied_volatility_put_90" not in row["expression"] for row in noiv_candidates)
    assert any("pcr_vol_10" in row["expression"] for row in noiv_candidates)
    assert any("pcr_oi_60" in row["expression"] for row in noiv_candidates)


def test_policy_repair_records_generate_composite_dividend_concentration_repairs():
    rows = [
        {
            "alpha_id": "YPp2ZRKl",
            "tag": "composite-dividend-micro-sector-v2",
            "expression": "rank(group_neutralize(0.18 * rank(-1 * composite_factor_score_derivative) + 0.18 * rank(power((high - close) / (high - low), 2)) + 0.14 * ts_rank(actual_dividend_value_quarterly / open, 120) + 0.12 * rank(ts_delta(fifty_to_two_hundred_day_price_ratio, 30)) + 0.10 * rank(-1 * correlation_last_30_days_spy) - 0.12 * ts_rank(returns, 140), industry))",
            "triage_bucket": "near_miss_repair",
            "triage_reason": "platform check failed",
            "failed_platform_checks": [{"name": "CONCENTRATED_WEIGHT", "result": "FAIL"}],
            "sharpe": 2.13,
            "fitness": 1.27,
            "turnover": 0.3006,
            "source_fields": [
                "actual_dividend_value_quarterly",
                "close",
                "composite_factor_score_derivative",
                "correlation_last_30_days_spy",
                "fifty_to_two_hundred_day_price_ratio",
                "high",
                "industry",
                "low",
                "open",
                "returns",
            ],
        }
    ]

    records = build_policy_repair_records(rows, max_repairs_per_row=4)
    candidates = build_policy_repair_candidates(records)

    assert [row["failure_kind"] for row in records] == ["concentrated_weight"]
    assert any(row.get("source_family") == "repair_concentration_composite_dividend_dispersed" for row in candidates)
    assert any("dividends_to_gross_profit" in row["expression"] for row in candidates)
    assert any((row.get("simulation_settings") or {}).get("maxPosition") == "ON" for row in candidates)
    assert any((row.get("simulation_settings") or {}).get("truncation") == 0.05 for row in candidates)


def test_policy_repair_records_generate_noiv_active_concentration_repairs():
    rows = [
        {
            "alpha_id": "Vkp2p7qY",
            "tag": "sales-revision-pcr-noiv",
            "expression": "rank(group_rank(0.20 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / enterprise_value, 150) + 0.18 * ts_rank(forward_sales_to_price, 150) + 0.14 * rank(-1 * earnings_certainty_rank_derivative) + 0.12 * ts_rank(earnings_revision_magnitude, 120) + 0.12 * rank(-1 * ts_rank(pcr_oi_60, 90)) - 0.18 * ts_rank(returns, 130), industry))",
            "triage_bucket": "near_miss_repair",
            "triage_reason": "platform check failed",
            "failed_platform_checks": [{"name": "CONCENTRATED_WEIGHT", "result": "FAIL"}],
            "sharpe": 1.65,
            "fitness": 1.18,
            "turnover": 0.1811,
            "source_fields": [
                "actual_sales_value_quarterly",
                "earnings_certainty_rank_derivative",
                "earnings_revision_magnitude",
                "enterprise_value",
                "forward_sales_to_price",
                "industry",
                "pcr_oi_60",
                "returns",
            ],
        },
        {
            "alpha_id": "e7Od0e3d",
            "tag": "cashflow-credit-pcr-noiv",
            "expression": "rank(group_neutralize(0.20 * group_rank(ts_rank(forward_cash_flow_to_price, 150), industry) + 0.16 * group_rank(ts_rank(cashflow_op / enterprise_value, 120), subindustry) + 0.12 * rank(-1 * relative_valuation_rank_derivative) + 0.12 * rank(-1 * credit_risk_premium_indicator) + 0.12 * rank(-1 * ts_rank(pcr_oi_60, 90)) + 0.10 * rank(ts_corr(vwap, volume, 100)) - 0.14 * ts_rank(returns, 130), sector))",
            "triage_bucket": "hard_fail",
            "triage_reason": "platform check failed",
            "failed_platform_checks": [{"name": "CONCENTRATED_WEIGHT", "result": "FAIL"}],
            "sharpe": 1.49,
            "fitness": 0.99,
            "turnover": 0.1659,
            "source_fields": [
                "cashflow_op",
                "credit_risk_premium_indicator",
                "enterprise_value",
                "forward_cash_flow_to_price",
                "industry",
                "pcr_oi_60",
                "relative_valuation_rank_derivative",
                "returns",
                "sector",
                "subindustry",
                "volume",
                "vwap",
            ],
        },
        {
            "alpha_id": "ZYp2n7NQ",
            "tag": "netincome-forwardcf-pcr-noiv",
            "expression": "rank(group_neutralize(0.20 * ts_rank(anl4_adjusted_netincome_ft / cap, 110) + 0.18 * ts_rank(forward_cash_flow_to_price, 150) + 0.14 * rank(-1 * credit_risk_premium_indicator) + 0.12 * rank(-1 * relative_valuation_rank_derivative) + 0.12 * rank(-1 * ts_rank(pcr_oi_60, 90)) + 0.08 * rank(-1 * ts_rank(close / vwap, 80)) - 0.16 * ts_rank(returns, 130), sector))",
            "triage_bucket": "hard_fail",
            "triage_reason": "platform check failed",
            "failed_platform_checks": [{"name": "CONCENTRATED_WEIGHT", "result": "FAIL"}],
            "sharpe": 1.38,
            "fitness": 0.97,
            "turnover": 0.1997,
            "source_fields": [
                "anl4_adjusted_netincome_ft",
                "cap",
                "close",
                "credit_risk_premium_indicator",
                "forward_cash_flow_to_price",
                "pcr_oi_60",
                "relative_valuation_rank_derivative",
                "returns",
                "sector",
                "vwap",
            ],
        },
    ]

    records = build_policy_repair_records(rows, max_repairs_per_row=8)
    candidates = build_policy_repair_candidates(records)
    families = {row.get("source_family") for row in candidates}

    assert {row["failure_kind"] for row in records} == {"concentrated_weight"}
    assert "repair_concentration_active_noiv_sales_revision" in families
    assert "repair_concentration_active_noiv_cashflow_credit" in families
    assert "repair_concentration_active_noiv_netincome_forwardcf" in families
    noiv_active = [row for row in candidates if str(row.get("source_family") or "").startswith("repair_concentration_active_noiv")]
    guard_rejected = [
        row
        for record in records
        for row in record.get("guard_rejected_candidates", [])
    ]
    assert noiv_active
    assert guard_rejected
    assert any(
        "multiple_sparse_legs_with_group_ops" in (row.get("concentration_risk") or {}).get("reasons", [])
        for row in guard_rejected
    )
    assert all("implied_volatility_call_90" not in row["expression"] for row in noiv_active)
    assert all("implied_volatility_put_90" not in row["expression"] for row in noiv_active)
    assert all(
        not (
            "group_" in row["expression"]
            and "cashflow_op / enterprise_value" in row["expression"]
            and "pcr_" in row["expression"]
        )
        for row in noiv_active
    )
    assert any((row.get("simulation_settings") or {}).get("truncation") == 0.03 for row in noiv_active)


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
