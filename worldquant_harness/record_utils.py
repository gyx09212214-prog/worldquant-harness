"""Small coercion helpers for record-shaped WQ artifacts."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

RowT = TypeVar("RowT")


def nested(payload: Any, *keys: Any) -> Any:
    if len(keys) == 1 and isinstance(keys[0], (list, tuple)):
        keys = tuple(keys[0])
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def first_float(*values: Any) -> float | None:
    for value in values:
        parsed = safe_float(value)
        if parsed is not None:
            return parsed
    return None


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def first_text(*values: Any) -> str | None:
    for value in values:
        if value is not None and str(value).strip() != "":
            return str(value)
    return None


def first_stripped_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def dedupe_rows_by_key(
    rows: Iterable[RowT],
    key_fn: Callable[[RowT], Any],
    *,
    skip_empty: bool = False,
) -> list[RowT]:
    out: list[RowT] = []
    seen: set[Any] = set()
    for row in rows:
        key = key_fn(row)
        if skip_empty and not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out
