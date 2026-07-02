"""Generate mixed repair candidates for the submit-5-more run."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_candidate_generation import run_static_candidate_generator

DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "mixed_repair_candidates.jsonl"


def main(argv: list[str] | None = None) -> int:
    return run_static_candidate_generator(
        argv,
        records_func=_records,
        default_output=DEFAULT_OUTPUT,
        default_limit=None,
        description='Generate mixed WQ repair candidates',
        limit_valid_count=False,
    )


def _add(
    rows: list[dict[str, Any]],
    tag: str,
    family: str,
    expr: str,
    settings: dict[str, Any],
    rationale: str,
) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": family,
            "source": "generate_wq_submit5_more_mixed_repair_candidates",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "mixed_active_map_decorrelation_repair",
            "rationale": rationale,
            "risk_flags": ["real_submit_candidate", "requires_online_simulation"],
        }
    )


def _records() -> list[dict[str, Any]]:
    d8_t05 = {"neutralization": "SUBINDUSTRY", "decay": 8, "truncation": 0.05}
    d10_t01 = {"neutralization": "SUBINDUSTRY", "decay": 10, "truncation": 0.01}
    d12_t01 = {"neutralization": "SUBINDUSTRY", "decay": 12, "truncation": 0.01}
    d12_t02 = {"neutralization": "SUBINDUSTRY", "decay": 12, "truncation": 0.02}
    d16_t02 = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.02}
    d16_t03 = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.03}
    ind_d10 = {"neutralization": "INDUSTRY", "decay": 10, "truncation": 0.03}

    rows: list[dict[str, Any]] = []

    _add(
        rows,
        "mx-a130-snt-closevol-r70",
        "a130_second_order_forum_micro",
        "rank(group_rank(0.13 * ts_rank(ts_backfill(earnings_revision_magnitude, 120), 80) + "
        "0.13 * ts_rank(ts_backfill(earnings_momentum_analyst_score, 120), 80) + "
        "0.16 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / enterprise_value, 100) + "
        "0.12 * ts_rank(ts_backfill(forward_sales_to_price, 120), 100) + "
        "0.10 * rank(-1 * ts_backfill(earnings_certainty_rank_derivative, 120)) + "
        "0.08 * ts_rank(snt1_cored1_score, 60) + "
        "0.07 * rank(-1 * ts_rank(close / vwap, 30)) + "
        "0.07 * rank(ts_corr(close, volume, 50)) - "
        "0.20 * ts_rank(returns, 70), subindustry))",
        d10_t01,
        "Reduce the crowded a130 analyst core and use small forum plus close/volume trajectory overlays.",
    )
    _add(
        rows,
        "mx-a130-pcr-spy-r80",
        "a130_second_order_options_market",
        "rank(group_rank(0.13 * ts_rank(ts_backfill(earnings_revision_magnitude, 120), 80) + "
        "0.13 * ts_rank(ts_backfill(earnings_momentum_analyst_score, 120), 100) + "
        "0.16 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / enterprise_value, 100) + "
        "0.12 * ts_rank(ts_backfill(forward_sales_to_price, 120), 120) + "
        "0.10 * rank(-1 * ts_backfill(earnings_certainty_rank_derivative, 120)) + "
        "0.07 * rank(-1 * ts_rank(pcr_oi_60, 90)) + "
        "0.06 * rank(-1 * correlation_last_30_days_spy) + "
        "0.06 * rank(ts_corr(vwap, volume, 50)) - "
        "0.20 * ts_rank(returns, 80), subindustry))",
        d12_t01,
        "Use small options and market-correlation overlays to move away from the existing active a130 shell.",
    )
    _add(
        rows,
        "mx-a130-iv-div-r70",
        "a130_second_order_options_dividend",
        "rank(group_rank(0.13 * ts_rank(ts_backfill(earnings_revision_magnitude, 120), 80) + "
        "0.12 * ts_rank(ts_backfill(earnings_momentum_analyst_score, 120), 100) + "
        "0.15 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / enterprise_value, 100) + "
        "0.11 * ts_rank(ts_backfill(forward_sales_to_price, 120), 120) + "
        "0.09 * rank(-1 * ts_backfill(earnings_certainty_rank_derivative, 120)) + "
        "0.09 * ts_rank(dividends_to_gross_profit, 80) + "
        "0.06 * group_rank(ts_backfill(implied_volatility_call_90 - implied_volatility_put_90, 60), subindustry) + "
        "0.06 * rank(ts_corr(close, volume, 45)) - "
        "0.19 * ts_rank(returns, 70), subindustry))",
        d12_t02,
        "Add dividend and IV texture while lowering revision/momentum weights.",
    )
    _add(
        rows,
        "mx-a130-missingness-closevwap",
        "a130_second_order_missingness",
        "rank(group_rank(0.12 * ts_rank(ts_backfill(earnings_revision_magnitude, 120), 60) + "
        "0.12 * ts_rank(ts_backfill(earnings_momentum_analyst_score, 120), 100) + "
        "0.16 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / enterprise_value, 80) + "
        "0.12 * ts_rank(ts_backfill(forward_sales_to_price, 120), 120) + "
        "0.10 * rank(-1 * ts_backfill(earnings_certainty_rank_derivative, 120)) + "
        "0.08 * group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 45), industry) + "
        "0.08 * rank(-1 * ts_rank(close / vwap, 35)) + "
        "0.06 * rank(volume / adv20) - "
        "0.20 * ts_rank(returns, 80), subindustry))",
        d16_t02,
        "Blend the lower-coverage missingness signal into a130 with slower decay.",
    )
    _add(
        rows,
        "mx-a130-cfps-snt-pcr",
        "a130_second_order_cfps_forum",
        "rank(group_rank(0.15 * ts_rank(ts_backfill(anl4_af_cfps_value, 120) / vwap, 100) + "
        "0.12 * ts_rank(ts_backfill(earnings_revision_magnitude, 120), 80) + "
        "0.15 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / enterprise_value, 100) + "
        "0.12 * ts_rank(ts_backfill(forward_sales_to_price, 120), 100) + "
        "0.08 * ts_rank(snt1_cored1_score, 80) + "
        "0.07 * rank(-1 * ts_rank(pcr_oi_60, 80)) + "
        "0.07 * rank(ts_corr(close, volume, 45)) - "
        "0.19 * ts_rank(returns, 70), subindustry))",
        d10_t01,
        "Move from EPS-style analyst core to cashflow estimate plus small forum/options overlay.",
    )
    _add(
        rows,
        "mx-a130-sntrevision-liq",
        "a130_second_order_forum_revision_delta",
        "rank(group_rank(0.12 * ts_rank(ts_backfill(earnings_revision_magnitude, 120), 80) + "
        "0.12 * ts_rank(ts_backfill(earnings_momentum_analyst_score, 120), 100) + "
        "0.16 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / enterprise_value, 100) + "
        "0.12 * ts_rank(ts_backfill(forward_sales_to_price, 120), 120) + "
        "0.09 * rank(-1 * ts_backfill(earnings_certainty_rank_derivative, 120)) + "
        "0.06 * zscore(ts_delta(snt1_d1_netearningsrevision, 5)) + "
        "0.08 * rank(volume / adv20) + "
        "0.07 * rank(-1 * ts_rank(close / vwap, 30)) - "
        "0.19 * ts_rank(returns, 80), subindustry))",
        d12_t01,
        "Use forum revision delta and liquidity instead of the old vwap-volume leg.",
    )
    _add(
        rows,
        "mx-a130-industry-snt-pcr",
        "a130_second_order_industry_neutral",
        "rank(group_rank(0.13 * ts_rank(ts_backfill(earnings_revision_magnitude, 120), 80) + "
        "0.12 * ts_rank(ts_backfill(earnings_momentum_analyst_score, 120), 100) + "
        "0.15 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / enterprise_value, 100) + "
        "0.12 * ts_rank(ts_backfill(forward_sales_to_price, 120), 120) + "
        "0.10 * rank(-1 * ts_backfill(earnings_certainty_rank_derivative, 120)) + "
        "0.08 * ts_rank(snt1_cored1_score, 80) + "
        "0.07 * rank(-1 * ts_rank(pcr_oi_60, 80)) + "
        "0.06 * rank(ts_corr(close, volume, 50)) - "
        "0.19 * ts_rank(returns, 80), industry))",
        ind_d10,
        "Change the grouping level and add small forum/options overlays.",
    )

    _add(
        rows,
        "mx-fo-sales-pcr-closevwap",
        "forum_group_rank_sales_options",
        "rank(group_neutralize(0.15 * group_rank(ts_rank(cashflow_op / cap, 100), industry) + "
        "0.13 * group_rank(ts_rank(actual_sales_value_quarterly / enterprise_value, 120), subindustry) + "
        "0.10 * rank(-1 * credit_risk_premium_indicator) + "
        "0.10 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * ts_rank(snt1_cored1_score, 80) + "
        "0.07 * rank(-1 * ts_rank(pcr_oi_60, 90)) + "
        "0.08 * rank(-1 * ts_rank(close / vwap, 40)) + "
        "0.07 * rank(ts_corr(close, volume, 50)) - "
        "0.16 * ts_rank(returns, 100), industry))",
        d8_t05,
        "Shift the successful forum group-rank skeleton from forward cashflow to sales plus PCR.",
    )
    _add(
        rows,
        "mx-fo-cfop-ev-spy",
        "forum_group_rank_cashflow_ev_market",
        "rank(group_neutralize(0.16 * group_rank(ts_rank(cashflow_op / enterprise_value, 100), industry) + "
        "0.14 * group_rank(ts_rank(forward_cash_flow_to_price, 120), subindustry) + "
        "0.10 * rank(-1 * credit_risk_premium_indicator) + "
        "0.10 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.11 * ts_rank(snt1_cored1_score, 70) + "
        "0.07 * rank(-1 * correlation_last_30_days_spy) + "
        "0.08 * rank(-1 * ts_rank(close / vwap, 35)) + "
        "0.07 * rank(ts_corr(vwap, volume, 60)) - "
        "0.16 * ts_rank(returns, 100), industry))",
        d8_t05,
        "Use enterprise-value cashflow and SPY-correlation overlay to reduce overlap with 3qAGdJV0.",
    )
    _add(
        rows,
        "mx-fo-div-missingness",
        "forum_group_rank_dividend_missingness",
        "rank(group_neutralize(0.14 * group_rank(ts_rank(cashflow_op / cap, 100), industry) + "
        "0.12 * group_rank(ts_rank(forward_cash_flow_to_price, 120), subindustry) + "
        "0.10 * rank(-1 * credit_risk_premium_indicator) + "
        "0.10 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * ts_rank(snt1_cored1_score, 80) + "
        "0.08 * ts_rank(dividends_to_gross_profit, 90) + "
        "0.06 * group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 45), industry) + "
        "0.08 * rank(-1 * ts_rank(close / vwap, 35)) - "
        "0.16 * ts_rank(returns, 100), industry))",
        d10_t01 | {"truncation": 0.05},
        "Cross the forum group-rank shell with the lower-covered dividend/missingness active area.",
    )
    _add(
        rows,
        "mx-fo-iv-pcr-r120",
        "forum_group_rank_options",
        "rank(group_neutralize(0.15 * group_rank(ts_rank(cashflow_op / cap, 100), industry) + "
        "0.13 * group_rank(ts_rank(forward_cash_flow_to_price, 140), subindustry) + "
        "0.10 * rank(-1 * credit_risk_premium_indicator) + "
        "0.09 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * ts_rank(snt1_cored1_score, 80) + "
        "0.07 * group_rank(ts_backfill(implied_volatility_call_90 - implied_volatility_put_90, 60), subindustry) + "
        "0.06 * rank(-1 * ts_rank(pcr_oi_60, 90)) + "
        "0.08 * rank(ts_corr(close, volume, 60)) - "
        "0.16 * ts_rank(returns, 120), industry))",
        d8_t05,
        "Introduce options positioning into the successful forum skeleton with longer return penalty.",
    )
    _add(
        rows,
        "mx-fo-sntrevision-closevol",
        "forum_group_rank_revision_delta",
        "rank(group_neutralize(0.15 * group_rank(ts_rank(cashflow_op / cap, 100), industry) + "
        "0.13 * group_rank(ts_rank(forward_cash_flow_to_price, 120), subindustry) + "
        "0.10 * rank(-1 * credit_risk_premium_indicator) + "
        "0.09 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.08 * ts_rank(snt1_cored1_score, 80) + "
        "0.06 * zscore(ts_delta(snt1_d1_netearningsrevision, 5)) + "
        "0.08 * rank(-1 * ts_rank(close / vwap, 40)) + "
        "0.07 * rank(ts_corr(close, volume, 60)) - "
        "0.16 * ts_rank(returns, 110), industry))",
        d8_t05,
        "Use forum revision change as a small path perturbation on the group-rank shell.",
    )
    _add(
        rows,
        "mx-fo-cashflow-split-v2",
        "forum_group_rank_cashflow_split",
        "rank(group_neutralize(0.13 * group_rank(ts_rank(cashflow_op / cap, 100), industry) + "
        "0.10 * group_rank(ts_rank(cashflow / cap, 100), subindustry) - "
        "0.07 * group_rank(ts_rank(cashflow_fin / cap, 100), industry) + "
        "0.11 * rank(-1 * credit_risk_premium_indicator) + "
        "0.10 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * ts_rank(snt1_cored1_score, 80) + "
        "0.08 * rank(-1 * ts_rank(close / vwap, 40)) + "
        "0.08 * rank(ts_corr(vwap, volume, 60)) - "
        "0.16 * ts_rank(returns, 110), industry))",
        d8_t05,
        "Split cashflow components while keeping the forum/micro path from the active forum alpha.",
    )

    _add(
        rows,
        "mx-mdm-snt-pcr-r50",
        "missingness_dividend_forum_options",
        "rank(0.36 * group_rank(ts_backfill(0.16 * ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 45) + "
        "0.14 * ts_rank(dividends_to_gross_profit, 90) + "
        "0.14 * rank(volume / adv20) + "
        "0.13 * rank(ts_corr(close, volume, 50)) + "
        "0.07 * ts_rank(snt1_cored1_score, 70) - "
        "0.18 * ts_rank(returns, 50), 80), industry) + "
        "0.24 * ts_rank(-ts_delta(vwap, 12) / vwap, 50) + "
        "0.16 * rank((high - close) / (high - low) * rank(volume / ts_mean(volume, 20))) + "
        "0.12 * rank(-ts_decay_linear(close / vwap, 15)) + "
        "0.12 * rank(-1 * ts_rank(pcr_oi_60, 80)))",
        d16_t03,
        "Rebuild the missingness/dividend active with smaller core weight and forum/options perturbations.",
    )
    _add(
        rows,
        "mx-mdm-spy-div-r60",
        "missingness_dividend_market",
        "rank(0.36 * group_rank(ts_backfill(0.15 * ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 50) + "
        "0.15 * ts_rank(dividends_to_gross_profit, 100) + "
        "0.13 * rank(volume / adv20) + "
        "0.12 * rank(ts_corr(vwap, volume, 60)) + "
        "0.08 * rank(-1 * correlation_last_30_days_spy) - "
        "0.18 * ts_rank(returns, 60), 90), industry) + "
        "0.24 * ts_rank(-ts_delta(vwap, 15) / vwap, 60) + "
        "0.16 * rank((open - close) / (high - low) * rank(volume / ts_mean(volume, 20))) + "
        "0.12 * rank(-ts_decay_linear(close / vwap, 15)) + "
        "0.12 * ts_rank(snt1_cored1_score, 80))",
        d16_t03,
        "Use SPY-correlation and open-close pressure to avoid cloning pw7xWejv.",
    )
    _add(
        rows,
        "mx-mdm-iv-closevol-r70",
        "missingness_dividend_options",
        "rank(0.34 * group_rank(ts_backfill(0.15 * ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 50) + "
        "0.14 * ts_rank(dividends_to_gross_profit, 100) + "
        "0.13 * rank(volume / adv20) + "
        "0.12 * rank(ts_corr(close, volume, 60)) + "
        "0.08 * group_rank(ts_backfill(implied_volatility_call_90 - implied_volatility_put_90, 60), subindustry) - "
        "0.18 * ts_rank(returns, 70), 90), industry) + "
        "0.24 * ts_rank(-ts_delta(vwap, 15) / vwap, 60) + "
        "0.16 * rank((high - close) / (high - low) * rank(volume / ts_mean(volume, 20))) + "
        "0.14 * rank(-ts_decay_linear(close / vwap, 15)) + "
        "0.12 * rank(-1 * ts_rank(pcr_oi_60, 90)))",
        d12_t02,
        "Blend IV and PCR into the missingness/dividend structure with changed windows.",
    )
    _add(
        rows,
        "mx-mdm-industry-snt",
        "missingness_dividend_industry_neutral",
        "rank(0.34 * group_rank(ts_backfill(0.15 * ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 50) + "
        "0.14 * ts_rank(dividends_to_gross_profit, 100) + "
        "0.13 * rank(volume / adv20) + "
        "0.12 * rank(ts_corr(close, volume, 60)) + "
        "0.08 * ts_rank(snt1_cored1_score, 80) - "
        "0.18 * ts_rank(returns, 70), 90), subindustry) + "
        "0.24 * ts_rank(-ts_delta(vwap, 15) / vwap, 60) + "
        "0.16 * rank((high - close) / (high - low) * rank(volume / ts_mean(volume, 20))) + "
        "0.14 * rank(-ts_decay_linear(close / vwap, 15)) + "
        "0.12 * rank(-1 * correlation_last_30_days_spy))",
        ind_d10,
        "Change the inner group and final neutralization on the missingness/dividend path.",
    )

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
