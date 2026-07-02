"""Record helpers for deterministic WQ repair candidates."""

from __future__ import annotations

from typing import Any

from .record_utils import dedupe_rows_by_key
from .wq_agent_records import candidate_dedupe_key, clean_simulation_settings
from .wq_auto_mining import validate_wq_expression
from .wq_expression_utils import expression_components
from .wq_repair_scoring import expression_priority


def make_repair_candidate(
    expression: str,
    *,
    tag: str,
    family: str,
    strategy: str,
    parent_alpha_ids: list[Any],
    rationale: str,
    simulation_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fields = sorted(expression_components(expression).get("fields", []))
    row = {
        "expression": expression,
        "tag": tag,
        "source_family": family,
        "mutation_strategy": strategy,
        "rationale": rationale,
        "expected_low_corr_reason": "Deterministic repair changed field/operator family after presubmit failure.",
        "parent_alpha_ids": [value for value in parent_alpha_ids if value],
        "source_fields": fields,
        "risk_flags": ["repair_candidate", strategy],
        "repair_priority_score": expression_priority(expression),
    }
    settings = clean_simulation_settings(simulation_settings)
    if settings:
        row["simulation_settings"] = settings
        row["settings_hint"] = ", ".join(f"{key}={value}" for key, value in sorted(settings.items()))
        row["risk_flags"].append("settings_variant")
        row["repair_priority_score"] = round(row["repair_priority_score"] + 2.0, 4)
    return row


def dedupe_repair_candidates(rows: list[dict]) -> list[dict]:
    return dedupe_rows_by_key(rows, candidate_dedupe_key, skip_empty=True)


def repair_candidate_dedupe_key(row: dict) -> str:
    return candidate_dedupe_key(row)


def is_locally_valid_expression(expression: str) -> bool:
    try:
        validate_wq_expression(expression)
        return True
    except Exception:
        return False
