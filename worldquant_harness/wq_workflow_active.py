"""Expression, family, and virtual-active helpers for WQ workflow stages."""

from __future__ import annotations

from collections import Counter

from .wq_expression_utils import expression_fields, expression_operators
from .wq_expression_utils import field_signature as expression_field_signature
from .wq_field_groups import (
    OPTION_FIELDS,
    PLATFORM_ANALYST_REVISION_FIELDS,
    PLATFORM_CASHFLOW_FIELDS,
    PLATFORM_DERIVATIVE_FIELDS,
    PLATFORM_FORWARD_VALUE_FIELDS,
)


def _virtual_active_row(row: dict) -> dict:
    expression = str(row.get("expression") or "")
    return {
        "alpha_id": row.get("alpha_id"),
        "expression": expression,
        "status": "VIRTUAL_ACTIVE",
        "active_source": "virtual_presubmit",
        "virtual_active": True,
        "cycle_index": row.get("cycle_index"),
        "ready_index": row.get("ready_index"),
        "tag": row.get("tag"),
        "source_family": _row_family(row),
        "fields": _fields(expression),
        "operators": _operators(expression),
        "sharpe": row.get("sharpe"),
        "fitness": row.get("fitness"),
        "returns": row.get("returns"),
        "turnover": row.get("turnover"),
        "sc_value": row.get("sc_value"),
        "prod_corr_value": row.get("prod_corr_value"),
    }


def _active_family_counts(rows: list[dict]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        family = _row_family(row)
        if family:
            counts[family] += 1
    return counts


def _active_field_signature_counts(rows: list[dict]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        signature = _field_signature(str(row.get("expression") or ""))
        if signature:
            counts[signature] += 1
    return counts


def _row_family(row: dict) -> str:
    return str(
        row.get("source_family")
        or row.get("mutation_strategy")
        or (row.get("candidate_meta") or {}).get("source_family")
        or ""
    )


def _field_signature(expression: str) -> str:
    return expression_field_signature(expression)


def _is_option_only_expression(expression: str) -> bool:
    fields = set(_fields(expression))
    return bool(fields) and fields <= OPTION_FIELDS


def _platform_candidate_family(expression: str) -> str:
    fields = set(_fields(expression))
    prefix = "platform_recent_unsubmitted"
    if _is_option_only_expression(expression):
        return f"{prefix}_options_only"
    if fields & PLATFORM_DERIVATIVE_FIELDS:
        return f"{prefix}_model_derivative"
    if fields & PLATFORM_FORWARD_VALUE_FIELDS:
        return f"{prefix}_forward_value"
    if fields & PLATFORM_ANALYST_REVISION_FIELDS:
        return f"{prefix}_analyst_revision"
    if fields & PLATFORM_CASHFLOW_FIELDS:
        return f"{prefix}_cashflow_value"
    if {"high", "low", "close"} <= fields:
        return f"{prefix}_intraday_reversal"
    return f"{prefix}_memory"


def _has_unsupported_statement_separator(expression: str) -> bool:
    return ";" in (expression or "")


def _fields(expression: str) -> list[str]:
    return expression_fields(expression)


def _operators(expression: str) -> list[str]:
    return expression_operators(expression)


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    return round(len(left & right) / len(left | right), 4)
