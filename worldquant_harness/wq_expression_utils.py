"""Shared helpers for WQ expression component extraction."""

from __future__ import annotations

from typing import Any, Iterable

from .expression_parser import extract_components


def expression_components(expression: str | None) -> dict[str, set[str]]:
    raw = _raw_components(expression, strict=False)
    return {
        "fields": _clean_values(raw.get("fields") if isinstance(raw, dict) else []),
        "operators": _clean_values(raw.get("operators") if isinstance(raw, dict) else []),
    }


def expression_component_lists(expression: str | None, *, strict: bool = False) -> dict[str, list[str]]:
    raw = _raw_components(expression, strict=strict)
    if not isinstance(raw, dict):
        raw = {}
    return {
        "fields": sorted(str(value) for value in raw.get("fields", [])),
        "operators": sorted(str(value) for value in raw.get("operators", [])),
    }


def expression_fields(expression: str | None) -> list[str]:
    return sorted(expression_components(expression)["fields"])


def expression_operators(expression: str | None) -> list[str]:
    return sorted(expression_components(expression)["operators"])


def field_signature(expression: str | None) -> str:
    return "|".join(expression_fields(expression))


def strip_outer_rank(expression: str) -> str:
    text = expression.strip()
    lower = text.lower()
    if not lower.startswith("rank(") or not text.endswith(")"):
        return text
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and index != len(text) - 1:
                return text
    return text[text.find("(") + 1:-1].strip()


def jaccard(left: Iterable[Any], right: Iterable[Any], *, precision: int = 4) -> float:
    left_set = {str(value) for value in left if value}
    right_set = {str(value) for value in right if value}
    if not left_set and not right_set:
        return 0.0
    return round(len(left_set & right_set) / len(left_set | right_set), precision)


def _raw_components(expression: str | None, *, strict: bool) -> dict[str, Any]:
    if strict:
        return extract_components(expression or "")
    try:
        return extract_components(expression or "")
    except Exception:
        return {}


def _clean_values(values: Any) -> set[str]:
    if values is None:
        return set()
    return {str(value) for value in values if value}
