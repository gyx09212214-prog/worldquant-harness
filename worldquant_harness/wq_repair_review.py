"""Review-row helpers shared by WQ repair planning templates."""

from __future__ import annotations

from .record_utils import safe_float
from .wq_expression_utils import expression_components


def repair_row_fields(row: dict) -> set[str]:
    values = row.get("source_fields")
    if isinstance(values, list):
        return {str(value) for value in values if value}
    expression = str(row.get("expression") or row.get("source_expression") or "")
    return set(expression_components(expression).get("fields", [])) if expression else set()


def is_submit_metric_pass(row: dict) -> bool:
    sharpe = safe_float(row.get("sharpe")) or 0.0
    fitness = safe_float(row.get("fitness")) or 0.0
    turnover = safe_float(row.get("turnover"))
    turnover_ok = turnover is None or 0.01 <= turnover <= 0.7
    return sharpe >= 1.25 and fitness >= 1.0 and turnover_ok
