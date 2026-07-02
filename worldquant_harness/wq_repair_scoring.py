"""Scoring helpers for deterministic WQ repair candidates."""

from __future__ import annotations

from .record_utils import safe_float
from .wq_expression_utils import expression_components


def repair_candidate_sort_key(row: dict) -> tuple:
    score = safe_float(row.get("repair_priority_score")) or 0.0
    family = str(row.get("source_family") or "")
    fields = set(row.get("source_fields") or [])
    if family.startswith("repair_concentration_active_noiv"):
        score += 8.0
    if family in {
        "repair_self_corr_equity_sales_eps_rebuild",
        "repair_self_corr_sales_earnmom_rebuild",
    }:
        score += 1.0
    if family in {
        "repair_self_corr_cashflow_book_revision",
        "repair_self_corr_minimal_orthogonal",
    }:
        score -= 2.0
    if family == "repair_metric_sales_cap_revision_tune":
        score -= 5.0
    if family == "repair_metric_netincome_value_rebuild":
        score += 6.0
    if family in {"repair_metric_threshold_settings", "repair_metric_threshold_smoothing"}:
        score -= 8.0
    if family == "repair_metric_sales_momentum_tune":
        score -= 6.0
    if str(row.get("repair_failure_kind") or "") == "concentrated_weight" and (
        fields & {
            "implied_volatility_call_90",
            "implied_volatility_put_90",
            "implied_volatility_call_120",
            "implied_volatility_put_120",
        }
    ):
        score -= 10.0
    if family in {"repair_concentration_max_position_retest", "repair_concentration_low_truncation_retest"}:
        score -= 3.0
    return (
        -score,
        str(row.get("repair_failure_kind") or ""),
        str(row.get("tag") or ""),
    )


def expression_priority(expression: str) -> float:
    fields = expression_components(expression).get("fields", [])
    crowded_penalty = sum(1 for field in fields if field in {"returns", "close", "volume", "vwap"})
    rare_bonus = sum(1 for field in fields if field in {
        "forward_sales_to_price",
        "forward_book_value_to_price",
        "coefficient_variation_fy1_eps",
        "pcr_oi_60",
        "pcr_vol_10",
        "snt1_d1_netearningsrevision",
        "actual_cashflow_per_share_value_quarterly",
    })
    return round(50.0 + rare_bonus * 4.0 - crowded_penalty * 2.5, 4)
