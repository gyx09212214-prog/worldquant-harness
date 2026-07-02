"""Screen deterministic WQ repair candidates for known repair-specific risks."""

from __future__ import annotations

from typing import Any

from .wq_expression_utils import expression_components
from .wq_field_groups import (
    GROUP_DISTRIBUTION_OPERATORS,
    field_used_as_denominator,
    is_broad_dispersion_field,
    is_price_volume_dispersion_field,
    is_sparse_concentration_field,
)


def repair_candidate_concentration_risk(expression: str) -> dict[str, Any] | None:
    """Return sparse-leg concentration risk details for a repair expression."""
    parts = expression_components(expression)
    fields = {str(item) for item in parts.get("fields", set())}
    sparse_fields = sorted(field for field in fields if is_sparse_concentration_field(field))
    if not sparse_fields:
        return None

    operators = {str(item) for item in parts.get("operators", set())}
    group_operator_count = group_distribution_operator_count(expression, operators)
    pcr_fields = sorted(field for field in sparse_fields if field.startswith("pcr_"))
    denominator_sparse_fields = sorted(
        field for field in sparse_fields if field_used_as_denominator(expression, field)
    )
    broad_dispersion_fields = sorted(field for field in fields if is_broad_dispersion_field(field))
    price_volume_dispersion_fields = sorted(
        field for field in fields if is_price_volume_dispersion_field(field)
    )

    reasons: list[str] = []
    if len(sparse_fields) > 1 and group_operator_count > 0:
        reasons.append("multiple_sparse_legs_with_group_ops")
    if denominator_sparse_fields and group_operator_count > 0:
        reasons.append("sparse_denominator_group_stack")
    if denominator_sparse_fields and len(sparse_fields) > 1 and group_operator_count > 0:
        reasons.append("sparse_denominator_plus_other_sparse_group_leg")
    if len(sparse_fields) == 1 and group_operator_count > 0 and not broad_dispersion_fields:
        reasons.append("single_sparse_group_without_broad_dispersion_leg")
    if pcr_fields and not price_volume_dispersion_fields:
        reasons.append("pcr_sparse_leg_without_price_volume_dispersion")
    if not reasons:
        return None

    return {
        "reasons": sorted(set(reasons)),
        "sparse_fields": sparse_fields,
        "sparse_field_count": len(sparse_fields),
        "pcr_fields": pcr_fields,
        "denominator_sparse_fields": denominator_sparse_fields,
        "broad_dispersion_fields": broad_dispersion_fields,
        "price_volume_dispersion_fields": price_volume_dispersion_fields,
        "group_operator_count": group_operator_count,
        "lesson": (
            "Do not repair concentrated-weight failures with lower truncation, decay, or stacked group ranks only. "
            "Keep at most one sparse main leg; PCR legs need explicit price-volume dispersion."
        ),
    }


def group_distribution_operator_count(expression: str, operators: set[str]) -> int:
    text = str(expression or "")
    return max(
        sum(1 for operator in operators if operator in GROUP_DISTRIBUTION_OPERATORS),
        sum(text.count(f"{operator}(") for operator in GROUP_DISTRIBUTION_OPERATORS),
    )
