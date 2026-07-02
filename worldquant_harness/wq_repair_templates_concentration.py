"""Concentrated-weight repair template bank for WQ policy repair planning."""

from __future__ import annotations

from typing import Any

from .record_utils import safe_float as _safe_float
from .wq_agent_records import source_simulation_settings as _source_simulation_settings
from .wq_expression_utils import strip_outer_rank as _strip_outer_rank
from .wq_repair_records import make_repair_candidate as _candidate
from .wq_repair_review import is_submit_metric_pass as _is_submit_metric_pass
from .wq_repair_review import repair_row_fields as _fields


def concentration_repairs(
    tag: str,
    parent: list[Any],
    *,
    source_expression: str = "",
    source_row: dict | None = None,
) -> list[dict]:
    iv_ratio = "((implied_volatility_call_90 - implied_volatility_put_90) / (implied_volatility_call_90 + implied_volatility_put_90))"
    source_row = source_row or {}
    source_family = str(
        source_row.get("source_family")
        or source_row.get("mutation_strategy")
        or (source_row.get("candidate_meta") or {}).get("source_family")
        or ""
    )
    source_settings = _source_simulation_settings(source_row)
    is_second_stage = source_family.startswith("repair_concentration")
    rows: list[dict] = []
    source_fields = _fields(source_row)
    if {"actual_dividend_value_quarterly", "composite_factor_score_derivative"} <= source_fields:
        rows.extend([
            _candidate(
                "rank(group_neutralize("
                "0.18 * ts_rank(actual_dividend_value_quarterly / cap, 170) + "
                "0.16 * rank(-1 * composite_factor_score_derivative) + "
                "0.14 * ts_rank(fifty_to_two_hundred_day_price_ratio, 140) + "
                "0.12 * rank(ts_corr(close, volume, 100)) + "
                "0.10 * rank(-1 * ts_rank(volume / adv20, 90)) + "
                "0.10 * rank(-1 * correlation_last_30_days_spy) - "
                "0.14 * ts_rank(returns, 170), industry))",
                tag=f"repair-{tag}-composite-dividend-broad-maxpos",
                family="repair_concentration_composite_dividend_dispersed",
                strategy="single_dividend_leg_broad_dispersion",
                parent_alpha_ids=parent,
                rationale="Keep one dividend leg and disperse weights with cap, price-volume, and broad relative-risk legs.",
                simulation_settings={"truncation": 0.05, "maxPosition": "ON"},
            ),
            _candidate(
                "rank(group_neutralize("
                "0.18 * ts_rank(dividends_to_gross_profit, 150) + "
                "0.16 * rank(-1 * composite_factor_score_derivative) + "
                "0.14 * ts_rank(fifty_to_two_hundred_day_price_ratio, 140) + "
                "0.12 * rank(ts_corr(close, volume, 100)) + "
                "0.10 * rank(-1 * ts_rank(volume / adv20, 90)) + "
                "0.10 * rank(-1 * correlation_last_30_days_spy) - "
                "0.14 * ts_rank(returns, 170), sector))",
                tag=f"repair-{tag}-dividend-grossprofit-broad-maxpos",
                family="repair_concentration_composite_dividend_relative",
                strategy="single_dividends_grossprofit_leg_broad_dispersion",
                parent_alpha_ids=parent,
                rationale="Use dividends-to-gross-profit as the only dividend leg, with broad price-volume dispersion.",
                simulation_settings={"truncation": 0.05, "maxPosition": "ON"},
            ),
            _candidate(
                "rank(group_rank(ts_decay_linear(group_neutralize("
                "0.16 * group_rank(rank(-1 * composite_factor_score_derivative), subindustry) + "
                "0.14 * group_rank(ts_rank(actual_dividend_value_quarterly / open, 140), industry) + "
                "0.14 * group_rank(ts_rank(dividends_to_gross_profit, 120), sector) + "
                "0.12 * group_rank(ts_rank(fifty_to_two_hundred_day_price_ratio, 120), industry) + "
                "0.10 * group_rank(rank(-1 * correlation_last_30_days_spy), subindustry) + "
                "0.10 * rank(ts_corr(close, volume, 80)) - "
                "0.14 * ts_rank(returns, 160), sector), 8), subindustry))",
                tag=f"repair-{tag}-composite-dividend-dispersed-maxpos",
                family="repair_concentration_composite_dividend_dispersed",
                strategy="composite_dividend_group_rank_maxpos",
                parent_alpha_ids=parent,
                rationale="Disperse the high-metric composite/dividend signal with grouped component ranks, smoothing, and max-position controls.",
                simulation_settings={"truncation": 0.05, "maxPosition": "ON"},
            ),
            _candidate(
                "rank(group_neutralize("
                "0.18 * group_rank(ts_rank(actual_dividend_value_quarterly / open, 160), subindustry) + "
                "0.16 * group_rank(ts_rank(dividends_to_gross_profit, 120), industry) + "
                "0.14 * rank(-1 * ts_rank(composite_factor_score_derivative, 120)) + "
                "0.12 * rank(-1 * correlation_last_30_days_spy) + "
                "0.10 * ts_rank(rel_ret_cust, 120) + "
                "0.10 * rank(-1 * ts_rank(volume / adv20, 60)) - "
                "0.14 * ts_rank(returns, 160), sector))",
                tag=f"repair-{tag}-composite-dividend-relative-dispersed",
                family="repair_concentration_composite_dividend_relative",
                strategy="replace_wick_with_relative_volume_dividend",
                parent_alpha_ids=parent,
                rationale="Replace the concentrated wick leg with relative-return and volume dispersion while preserving the dividend/composite edge.",
                simulation_settings={"truncation": 0.05, "maxPosition": "ON"},
            ),
        ])
    if {"actual_sales_value_quarterly", "forward_sales_to_price", "pcr_oi_60"} <= source_fields:
        rows.extend([
            _candidate(
                "rank(group_neutralize("
                "0.22 * ts_rank(ts_backfill(actual_sales_value_quarterly, 150) / cap, 180) + "
                "0.20 * ts_rank(forward_sales_to_price, 180) + "
                "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.12 * ts_rank(earnings_revision_magnitude, 160) + "
                "0.10 * rank(ts_corr(vwap, volume, 120)) + "
                "0.08 * rank(-1 * ts_rank(volume / adv20, 100)) - "
                "0.14 * ts_rank(returns, 180), industry))",
                tag=f"repair-{tag}-sales-revision-broad-trunc003",
                family="repair_concentration_active_noiv_sales_revision",
                strategy="single_sales_cap_leg_broad_dispersion",
                parent_alpha_ids=parent,
                rationale="Replace the EV/PCR sparse stack with cap-normalized sales and broad price-volume dispersion.",
                simulation_settings={"truncation": 0.03},
            ),
            _candidate(
                "rank(group_rank(ts_decay_linear(group_neutralize("
                "0.18 * group_rank(ts_rank(ts_backfill(actual_sales_value_quarterly, 140) / enterprise_value, 180), subindustry) + "
                "0.17 * group_rank(ts_rank(forward_sales_to_price, 170), industry) + "
                "0.13 * group_rank(rank(-1 * earnings_certainty_rank_derivative), sector) + "
                "0.11 * group_rank(ts_rank(earnings_revision_magnitude, 150), industry) + "
                "0.11 * rank(-1 * ts_rank(pcr_oi_60, 120)) + "
                "0.08 * rank(ts_corr(close, volume, 100)) - "
                "0.13 * ts_rank(returns, 170), sector), 10), subindustry))",
                tag=f"repair-{tag}-sales-revision-dispersed-trunc003",
                family="repair_concentration_active_noiv_sales_revision",
                strategy="sales_revision_component_group_rank_low_truncation",
                parent_alpha_ids=parent,
                rationale="Disperse the no-IV sales/revision repair with slower component ranks, smoothing, and stricter truncation.",
                simulation_settings={"truncation": 0.03},
            ),
            _candidate(
                "rank(group_neutralize("
                "0.18 * group_rank(ts_rank(forward_sales_to_price, 180), subindustry) + "
                "0.16 * group_rank(ts_rank(ts_backfill(actual_sales_value_quarterly, 160) / enterprise_value, 160), industry) + "
                "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.12 * ts_rank(earnings_revision_magnitude, 160) + "
                "0.10 * rank(-1 * ts_rank(pcr_oi_60, 130)) + "
                "0.08 * rank(-1 * ts_rank(volume / adv20, 90)) - "
                "0.12 * ts_rank(returns, 180), sector))",
                tag=f"repair-{tag}-sales-revision-liquidity-dispersed",
                family="repair_concentration_active_noiv_sales_revision",
                strategy="sales_revision_liquidity_dispersed",
                parent_alpha_ids=parent,
                rationale="Add a liquidity-dispersion leg and slower sector neutralization to reduce peak weights.",
                simulation_settings={"truncation": 0.05},
            ),
        ])
    if {"cashflow_op", "forward_cash_flow_to_price", "credit_risk_premium_indicator"} <= source_fields:
        rows.extend([
            _candidate(
                "rank(group_neutralize("
                "0.22 * ts_rank(forward_cash_flow_to_price, 180) + "
                "0.15 * ts_rank(cashflow_op, 180) + "
                "0.14 * rank(-1 * credit_risk_premium_indicator) + "
                "0.12 * rank(-1 * relative_valuation_rank_derivative) + "
                "0.11 * rank(ts_corr(vwap, volume, 120)) + "
                "0.10 * rank(-1 * ts_rank(volume / adv20, 100)) - "
                "0.14 * ts_rank(returns, 180), industry))",
                tag=f"repair-{tag}-cashflow-credit-broad-trunc003",
                family="repair_concentration_active_noiv_cashflow_credit",
                strategy="single_cashflow_leg_broad_dispersion",
                parent_alpha_ids=parent,
                rationale="Keep one cashflow leg and replace the EV/PCR sparse stack with broad liquidity dispersion.",
                simulation_settings={"truncation": 0.03},
            ),
            _candidate(
                "rank(group_rank(ts_decay_linear(group_neutralize("
                "0.20 * group_rank(ts_rank(forward_cash_flow_to_price, 170), industry) + "
                "0.17 * group_rank(ts_rank(cashflow_op / enterprise_value, 150), subindustry) + "
                "0.12 * group_rank(rank(-1 * credit_risk_premium_indicator), sector) + "
                "0.10 * group_rank(rank(-1 * relative_valuation_rank_derivative), industry) + "
                "0.10 * rank(-1 * ts_rank(pcr_oi_60, 120)) + "
                "0.09 * rank(ts_corr(vwap, volume, 120)) - "
                "0.12 * ts_rank(returns, 170), sector), 10), subindustry))",
                tag=f"repair-{tag}-cashflow-credit-dispersed-trunc003",
                family="repair_concentration_active_noiv_cashflow_credit",
                strategy="cashflow_credit_component_group_rank_low_truncation",
                parent_alpha_ids=parent,
                rationale="Keep the no-IV cashflow/credit core but reduce concentration through component group ranks and strict truncation.",
                simulation_settings={"truncation": 0.03},
            ),
            _candidate(
                "rank(0.72 * rank(ts_decay_linear(group_neutralize("
                "0.20 * ts_rank(forward_cash_flow_to_price, 180) + "
                "0.16 * ts_rank(cashflow_op / enterprise_value, 160) + "
                "0.12 * rank(-1 * credit_risk_premium_indicator) + "
                "0.10 * rank(-1 * relative_valuation_rank_derivative) - "
                "0.12 * ts_rank(returns, 180), industry), 8)) + "
                "0.16 * rank(-1 * ts_rank(pcr_oi_60, 130)) + "
                "0.12 * rank(-1 * ts_rank(volume / adv20, 100)))",
                tag=f"repair-{tag}-cashflow-credit-liquidity-dispersed",
                family="repair_concentration_active_noiv_cashflow_credit",
                strategy="cashflow_credit_liquidity_dispersed",
                parent_alpha_ids=parent,
                rationale="Blend the cashflow/credit core with slower PCR and liquidity dispersion to lower peak constituent weights.",
                simulation_settings={"truncation": 0.05},
            ),
        ])
    if {"anl4_adjusted_netincome_ft", "forward_cash_flow_to_price", "credit_risk_premium_indicator"} <= source_fields:
        rows.extend([
            _candidate(
                "rank(group_neutralize("
                "0.22 * ts_rank(anl4_adjusted_netincome_ft / cap, 150) + "
                "0.20 * ts_rank(forward_cash_flow_to_price, 180) + "
                "0.14 * rank(-1 * credit_risk_premium_indicator) + "
                "0.12 * rank(-1 * relative_valuation_rank_derivative) + "
                "0.10 * rank(ts_corr(vwap, volume, 120)) + "
                "0.08 * rank(-1 * ts_rank(close / vwap, 100)) - "
                "0.14 * ts_rank(returns, 180), sector))",
                tag=f"repair-{tag}-netincome-forwardcf-broad-trunc003",
                family="repair_concentration_active_noiv_netincome_forwardcf",
                strategy="netincome_forwardcf_broad_dispersion_no_pcr",
                parent_alpha_ids=parent,
                rationale="Remove PCR from the net-income/forward-cash-flow concentration repair and disperse with price-volume legs.",
                simulation_settings={"truncation": 0.03},
            ),
            _candidate(
                "rank(group_rank(ts_decay_linear(group_neutralize("
                "0.20 * group_rank(ts_rank(anl4_adjusted_netincome_ft / cap, 130), industry) + "
                "0.18 * group_rank(ts_rank(forward_cash_flow_to_price, 170), subindustry) + "
                "0.12 * group_rank(rank(-1 * credit_risk_premium_indicator), sector) + "
                "0.10 * rank(-1 * relative_valuation_rank_derivative) + "
                "0.10 * rank(-1 * ts_rank(pcr_oi_60, 120)) + "
                "0.08 * rank(-1 * ts_rank(close / vwap, 100)) - "
                "0.12 * ts_rank(returns, 170), sector), 10), subindustry))",
                tag=f"repair-{tag}-netincome-forwardcf-dispersed-trunc003",
                family="repair_concentration_active_noiv_netincome_forwardcf",
                strategy="netincome_forwardcf_component_group_rank_low_truncation",
                parent_alpha_ids=parent,
                rationale="Disperse the no-IV net-income/forward-cash-flow repair with grouped component ranks and lower truncation.",
                simulation_settings={"truncation": 0.03},
            ),
            _candidate(
                "rank(group_neutralize("
                "0.20 * group_rank(ts_rank(forward_cash_flow_to_price, 180), industry) + "
                "0.18 * group_rank(ts_rank(anl4_adjusted_netincome_ft / cap, 140), subindustry) + "
                "0.12 * rank(-1 * credit_risk_premium_indicator) + "
                "0.10 * rank(-1 * relative_valuation_rank_derivative) + "
                "0.10 * rank(-1 * ts_rank(pcr_oi_60, 130)) + "
                "0.08 * rank(ts_corr(vwap, volume, 120)) - "
                "0.12 * ts_rank(returns, 180), sector))",
                tag=f"repair-{tag}-netincome-forwardcf-flow-dispersed",
                family="repair_concentration_active_noiv_netincome_forwardcf",
                strategy="netincome_forwardcf_flow_dispersed",
                parent_alpha_ids=parent,
                rationale="Use flow dispersion and slower windows to reduce concentrated weights while preserving the net-income signal.",
                simulation_settings={"truncation": 0.05},
            ),
        ])
    if not is_second_stage:
        rows.extend([
            _candidate(
                f"rank(ts_decay_linear(group_neutralize(0.18 * ts_rank(actual_eps_value_quarterly / vwap, 100) + "
                f"0.16 * ts_rank(earnings_momentum_composite_score, 80) + "
                f"0.16 * rank(ts_mean({iv_ratio}, 10)) + "
                f"0.14 * rank(volume / adv20) + "
                f"0.14 * rank(-1 * ts_rank(pcr_oi_10, 80)) + "
                f"0.22 * ts_rank(forward_sales_to_price, 100), industry), 5))",
                tag=f"repair-{tag}-smooth-diversified-concentration",
                family="repair_concentration_smooth_diversified",
                strategy="smooth_and_diversify_weight",
                parent_alpha_ids=parent,
                rationale="Smooth and diversify the concentrated analyst/options blend while keeping the high Sharpe core.",
            ),
            _candidate(
                f"rank(0.30 * group_rank(ts_rank(actual_eps_value_quarterly / vwap, 100), industry) + "
                f"0.22 * group_rank(ts_rank(earnings_momentum_composite_score, 80), industry) + "
                f"0.18 * rank(ts_mean({iv_ratio}, 10)) + "
                f"0.15 * ts_rank(forward_sales_to_price, 100) + "
                f"0.15 * rank(-1 * ts_rank(pcr_oi_10, 80)))",
                tag=f"repair-{tag}-group-rank-lower-peak-weight",
                family="repair_concentration_group_rank",
                strategy="replace_neutralized_sum_with_group_rank_legs",
                parent_alpha_ids=parent,
                rationale="Use group-ranked component legs to reduce peak stock weights.",
            ),
        ])
    base = _strip_outer_rank(source_expression)
    if base and _is_submit_metric_pass(source_row or {}):
        source_has_truncation = "truncation" in source_settings
        source_has_max_position = source_settings.get("maxPosition") == "ON"
        source_truncation = _safe_float(source_settings.get("truncation")) or 0.08
        low_truncation = min(source_truncation, 0.05)
        max_position_settings = {
            "truncation": low_truncation,
            "maxPosition": "ON",
        }
        if not source_has_max_position:
            rows.append(_candidate(
                source_expression.strip(),
                tag=f"repair-{tag}-retest-maxpos",
                family="repair_concentration_max_position_retest",
                strategy="max_position_low_truncation_retest",
                parent_alpha_ids=parent,
                rationale="Retest the high-metric expression with maxPosition enabled and lower truncation.",
                simulation_settings=max_position_settings,
            ))
            rows.append(_candidate(
                f"rank(group_rank(ts_decay_linear(group_neutralize({base}, subindustry), 10), subindustry))",
                tag=f"repair-{tag}-subindustry-dispersed-maxpos",
                family="repair_concentration_subindustry_dispersed",
                strategy="subindustry_dispersed_max_position",
                parent_alpha_ids=parent,
                rationale="Combine stronger subindustry dispersion with maxPosition enabled.",
                simulation_settings=max_position_settings,
            ))
        if not source_has_truncation and not source_has_max_position:
            rows.extend([
                _candidate(
                    source_expression.strip(),
                    tag=f"repair-{tag}-retest-trunc005",
                    family="repair_concentration_low_truncation_retest",
                    strategy="low_truncation_retest",
                    parent_alpha_ids=parent,
                    rationale="Retest the high-metric expression with lower truncation to reduce peak stock weights.",
                    simulation_settings={"truncation": 0.05},
                ),
                _candidate(
                    source_expression.strip(),
                    tag=f"repair-{tag}-retest-trunc003",
                    family="repair_concentration_low_truncation_retest",
                    strategy="low_truncation_retest_strict",
                    parent_alpha_ids=parent,
                    rationale="Strict lower-truncation retest for concentrated-weight near miss.",
                    simulation_settings={"truncation": 0.03},
                ),
                _candidate(
                    f"rank(group_rank(ts_decay_linear(group_neutralize({base}, subindustry), 10), subindustry))",
                    tag=f"repair-{tag}-subindustry-dispersed-trunc005",
                    family="repair_concentration_subindustry_dispersed",
                    strategy="subindustry_dispersed_low_truncation",
                    parent_alpha_ids=parent,
                    rationale="Use subindustry neutralization, group ranking, smoothing, and lower truncation to reduce peak weights.",
                    simulation_settings={"truncation": 0.05},
                ),
            ])
        elif source_has_truncation and source_truncation > 0.03:
            rows.append(_candidate(
                source_expression.strip(),
                tag=f"repair-{tag}-retest-trunc003",
                family="repair_concentration_low_truncation_retest",
                strategy="low_truncation_retest_strict",
                parent_alpha_ids=parent,
                rationale="Strict lower-truncation retest for a concentrated-weight miss whose previous truncation did not clear peak weights.",
                simulation_settings={"truncation": 0.03},
            ))
        rows.extend([
            _candidate(
                "rank(group_neutralize(0.20 * group_rank(ts_rank(actual_sales_value_quarterly / enterprise_value, 120), sector) + "
                "0.16 * group_rank(ts_rank(forward_sales_to_price, 100), industry) + "
                "0.14 * group_rank(ts_backfill(implied_volatility_call_120 - implied_volatility_put_120, 70), subindustry) + "
                "0.12 * rank(-1 * ts_rank(pcr_oi_60, 100)) + "
                "0.10 * group_rank(ts_rank(rel_ret_cust, 120), industry) + "
                "0.10 * ts_rank(-ts_delta(vwap, 12) / vwap, 50) + "
                "0.08 * rank(-1 * beta_last_30_days_spy) - "
                "0.12 * ts_rank(returns, 110), sector))",
                tag=f"repair-{tag}-orthogonal-sector-dispersed",
                family="repair_concentration_orthogonal_sector",
                strategy="orthogonal_sector_dispersed_rebuild",
                parent_alpha_ids=parent,
                rationale="Rebuild the concentrated value/options blend with sector neutralization and broader orthogonal legs.",
                simulation_settings={"truncation": 0.05, "maxPosition": "ON"},
            ),
            _candidate(
                "rank(group_neutralize(0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.14 * ts_rank(dividends_to_gross_profit, 90) + "
                "0.14 * group_rank(ts_rank(rel_ret_cust, 120), sector) + "
                "0.12 * group_rank(ts_rank(forward_sales_to_price, 100), industry) + "
                "0.10 * rank(ts_corr(close, volume, 60)) + "
                "0.10 * ts_rank(-ts_delta(vwap, 15) / vwap, 60) + "
                "0.08 * rank(-1 * correlation_last_30_days_spy) - "
                "0.12 * ts_rank(returns, 120), industry))",
                tag=f"repair-{tag}-quality-relative-dispersed",
                family="repair_concentration_quality_relative",
                strategy="quality_relative_dispersed_rebuild",
                parent_alpha_ids=parent,
                rationale="Use quality, relative-return, forward-value, and beta-correlation legs to reduce peak constituent weights.",
                simulation_settings={"truncation": 0.05, "maxPosition": "ON"},
            ),
        ])
    return rows
