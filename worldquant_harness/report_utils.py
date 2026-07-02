"""Small formatting helpers shared by local report builders."""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from typing import Any, Callable

from .expression_parser import normalize_expression
from .record_utils import safe_float


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def markdown_cell(value: Any, *, pipe_replacement: str = "\\|", newline_replacement: str = " ") -> str:
    text = str(value if value is not None else "")
    return text.replace("|", pipe_replacement).replace("\n", newline_replacement)


def ratio(numerator: float | int, denominator: float | int) -> float | None:
    try:
        denominator_float = float(denominator)
        if denominator_float == 0.0:
            return None
        return round(float(numerator) / denominator_float, 6)
    except (TypeError, ValueError):
        return None


def matching_reason_count(counts: dict[str, int], targets: set[str]) -> int:
    total = 0
    for reason, count in counts.items():
        lowered = str(reason).lower()
        if lowered in targets or any(target in lowered for target in targets):
            total += count
    return total


def mean(values: Iterable[Any], *, coerce: Callable[[Any], float | None] = safe_float) -> float | None:
    nums = [coerce(value) for value in values]
    nums = [value for value in nums if value is not None]
    return round(statistics.mean(nums), 6) if nums else None


def format_number(value: Any, *, coerce: Callable[[Any], float | None] = safe_float, large_commas: bool = False) -> str:
    number = coerce(value)
    if number is None:
        return ""
    if large_commas and abs(number) >= 1000:
        return f"{number:,.0f}"
    return f"{number:.4f}".rstrip("0").rstrip(".")


def safe_normalize_expression(expression: str | None, *, fallback_compact: bool = True) -> str:
    try:
        return normalize_expression(expression or "")
    except Exception:
        text = str(expression or "")
        return "".join(text.split()) if fallback_compact else " ".join(text.split())


def truncate_text(value: Any, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
