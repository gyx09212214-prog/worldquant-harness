"""Metric-threshold repair template bank for WQ policy repair planning."""

from __future__ import annotations

from typing import Any

from .wq_expression_utils import strip_outer_rank as _strip_outer_rank
from .wq_repair_records import make_repair_candidate as _candidate


def metric_threshold_repairs(
    fields: set[str],
    tag: str,
    parent: list[Any],
    *,
    source_expression: str,
    source_row: dict | None = None,
) -> list[dict]:
    base = _strip_outer_rank(source_expression)
    source_row = source_row or {}
    rows: list[dict] = []
    if {"anl4_adjusted_netincome_ft", "cap"} <= fields:
        rows.extend([
            _candidate(
                "rank(0.42 * ts_rank(anl4_adjusted_netincome_ft / cap, 90) + "
                "0.24 * ts_rank(forward_cash_flow_to_price, 140) + "
                "0.16 * rank(ts_corr(vwap, volume, 100)) - "
                "0.18 * ts_rank(returns, 90))",
                tag=f"repair-{tag}-netincome-forwardcf-flow-rebuild",
                family="repair_metric_netincome_value_rebuild",
                strategy="metric_near_miss_rebuild_with_forward_cashflow_flow",
                parent_alpha_ids=parent,
                rationale="Lift Fitness by rebuilding the net-income/cap signal with forward cashflow and price-volume dispersion.",
            ),
            _candidate(
                "rank(0.40 * ts_rank(anl4_adjusted_netincome_ft / cap, 100) + "
                "0.22 * ts_rank(forward_book_value_to_price, 140) + "
                "0.16 * rank(-1 * relative_valuation_rank_derivative) + "
                "0.12 * rank(ts_corr(close, volume, 120)) - "
                "0.16 * ts_rank(returns, 100))",
                tag=f"repair-{tag}-netincome-forwardbook-value-rebuild",
                family="repair_metric_netincome_value_rebuild",
                strategy="metric_near_miss_rebuild_with_forward_book_value",
                parent_alpha_ids=parent,
                rationale="Replace weak short-window overlays with forward book value, valuation derivative, and price-volume dispersion.",
            ),
            _candidate(
                "rank(group_neutralize(0.38 * ts_rank(anl4_adjusted_netincome_ft / cap, 100) + "
                "0.22 * ts_rank(forward_sales_to_price, 150) + "
                "0.16 * ts_rank(coefficient_variation_fy1_eps, 120) + "
                "0.10 * rank(ts_corr(vwap, volume, 120)) - "
                "0.16 * ts_rank(returns, 120), industry))",
                tag=f"repair-{tag}-netincome-forward-sales-industry-rebuild",
                family="repair_metric_netincome_value_rebuild",
                strategy="metric_near_miss_industry_forward_value_rebuild",
                parent_alpha_ids=parent,
                rationale="Use slower forward-value and dispersion legs instead of same-expression settings retests.",
                simulation_settings={"truncation": 0.05},
            ),
        ])
    if {
        "actual_sales_value_quarterly",
        "cap",
        "forward_sales_to_price",
        "earnings_certainty_rank_derivative",
        "earnings_revision_magnitude",
        "vwap",
        "volume",
    } <= fields:
        rows.extend([
            _candidate(
                "rank(0.30 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / cap, 150) + "
                "0.24 * ts_rank(forward_sales_to_price, 150) + "
                "0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.14 * ts_rank(earnings_revision_magnitude, 130) + "
                "0.10 * rank(ts_corr(vwap, volume, 90)) - "
                "0.10 * ts_rank(returns, 130))",
                tag=f"repair-{tag}-sales-cap-revision-core-lift",
                family="repair_metric_sales_cap_revision_tune",
                strategy="metric_near_miss_sales_cap_revision_core_lift",
                parent_alpha_ids=parent,
                rationale="Lift the sales/cap revision near-miss by strengthening the high-coverage sales and forward-value legs without EV/PCR.",
            ),
            _candidate(
                "rank(group_neutralize(0.30 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / cap, 150) + "
                "0.24 * ts_rank(forward_sales_to_price, 150) + "
                "0.16 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.14 * ts_rank(earnings_revision_magnitude, 130) + "
                "0.12 * rank(ts_corr(vwap, volume, 80)) - "
                "0.08 * ts_rank(returns, 120), industry))",
                tag=f"repair-{tag}-sales-cap-revision-industry-lift",
                family="repair_metric_sales_cap_revision_tune",
                strategy="metric_near_miss_sales_cap_revision_industry_lift",
                parent_alpha_ids=parent,
                rationale="Keep industry neutralization but reduce the returns drag and strengthen price-volume breadth.",
            ),
            _candidate(
                "rank(0.28 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / cap, 150) + "
                "0.22 * ts_rank(earnings_momentum_composite_score, 80) + "
                "0.20 * ts_rank(forward_sales_to_price, 150) + "
                "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.10 * rank(ts_corr(vwap, volume, 90)) - "
                "0.10 * ts_rank(returns, 130))",
                tag=f"repair-{tag}-sales-cap-earnmom-lite",
                family="repair_metric_sales_cap_revision_tune",
                strategy="metric_near_miss_sales_cap_revision_light_earnmom",
                parent_alpha_ids=parent,
                rationale="Recover some original sales/earnings-momentum strength while keeping cap normalization and avoiding EV/PCR.",
            ),
            _candidate(
                "rank(0.26 * ts_rank(ts_backfill(actual_sales_value_quarterly, 100) / cap, 130) + "
                "0.22 * ts_rank(forward_sales_to_price, 130) + "
                "0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.12 * ts_rank(earnings_revision_magnitude, 110) + "
                "0.12 * rank(volume / adv20) - "
                "0.10 * ts_rank(returns, 110))",
                tag=f"repair-{tag}-sales-cap-revision-liquidity-lift",
                family="repair_metric_sales_cap_revision_tune",
                strategy="metric_near_miss_sales_cap_revision_liquidity_lift",
                parent_alpha_ids=parent,
                rationale="Use volume breadth instead of extra group operations to lift fitness while preserving high coverage.",
            ),
        ])
    if source_expression:
        rows.extend([
            _candidate(
                source_expression.strip(),
                tag=f"repair-{tag}-metric-retest-decay12-trunc005",
                family="repair_metric_threshold_settings",
                strategy="metric_near_miss_decay_truncation_retest",
                parent_alpha_ids=parent,
                rationale="Retest the near-threshold expression with slower decay and lower truncation.",
                simulation_settings={"decay": 12, "truncation": 0.05},
            ),
            _candidate(
                source_expression.strip(),
                tag=f"repair-{tag}-metric-retest-maxpos-trunc005",
                family="repair_metric_threshold_settings",
                strategy="metric_near_miss_max_position_retest",
                parent_alpha_ids=parent,
                rationale="Retest the near-threshold expression with maxPosition enabled to reduce peak risk.",
                simulation_settings={"maxPosition": "ON", "truncation": 0.05},
            ),
        ])
    if base:
        rows.append(_candidate(
            f"rank(ts_decay_linear(group_neutralize({base}, industry), 5))",
            tag=f"repair-{tag}-metric-smooth-industry",
            family="repair_metric_threshold_smoothing",
            strategy="metric_near_miss_smooth_group_neutralize",
            parent_alpha_ids=parent,
            rationale="Smooth and industry-neutralize the near-threshold expression to improve fitness stability.",
            simulation_settings={"truncation": 0.05},
        ))
    if {
        "actual_sales_value_quarterly",
        "enterprise_value",
        "earnings_momentum_composite_score",
        "vwap",
        "volume",
    } <= fields:
        rows.append(_candidate(
            "rank(0.46 * ts_rank(actual_sales_value_quarterly / enterprise_value, 80) + "
            "0.30 * ts_rank(earnings_momentum_composite_score, 70) + "
            "0.14 * rank(ts_corr(vwap, volume, 60)) - "
            "0.10 * ts_rank(returns, 60))",
            tag=f"repair-{tag}-sales-earnmom-slower-ret60",
            family="repair_metric_sales_momentum_tune",
            strategy="metric_near_miss_slow_turnover_tune",
            parent_alpha_ids=parent,
            rationale="Use slower windows and a smaller returns drag to lift fitness without changing the core payload.",
        ))
    return rows
