"""Expression similarity helpers shared by WQ workflow modules."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from .expression_parser import extract_components, normalize_expression


def compute_similarity(expr_a: str, expr_b: str) -> dict[str, float]:
    norm_a = normalize_expression(expr_a)
    norm_b = normalize_expression(expr_b)

    text_sim = SequenceMatcher(None, norm_a, norm_b).ratio()
    comp_a = extract_components(expr_a)
    comp_b = extract_components(expr_b)

    ops_a = set(comp_a.get("operators", []))
    ops_b = set(comp_b.get("operators", []))
    fields_a = set(comp_a.get("fields", []))
    fields_b = set(comp_b.get("fields", []))

    ops_jaccard = len(ops_a & ops_b) / len(ops_a | ops_b) if (ops_a | ops_b) else 1.0
    fields_jaccard = len(fields_a & fields_b) / len(fields_a | fields_b) if (fields_a | fields_b) else 1.0
    overall = 0.5 * text_sim + 0.3 * ops_jaccard + 0.2 * fields_jaccard

    return {
        "text_similarity": round(text_sim, 4),
        "operator_overlap": round(ops_jaccard, 4),
        "field_overlap": round(fields_jaccard, 4),
        "overall_similarity": round(overall, 4),
    }


def nearest_similarity(expression: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    nearest: dict[str, Any] | None = None
    normalized = normalize_expression(expression)
    for row in rows:
        other = str(row.get("expression") or "")
        if not other:
            continue
        similarity = compute_similarity(expression, other)
        item = {
            "alpha_id": row.get("alpha_id"),
            "expression": other,
            "status": row.get("status"),
            "similarity": similarity,
            "exact": normalized == normalize_expression(other),
        }
        if nearest is None or similarity.get("overall_similarity", 0.0) > nearest["similarity"].get("overall_similarity", 0.0):
            nearest = item
    return nearest
