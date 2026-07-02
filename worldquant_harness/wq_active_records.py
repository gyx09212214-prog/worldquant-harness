"""Helpers for reading local WQ active-alpha artifact rows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifact_io import read_jsonl


def load_active_node_rows(path: Path | str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in read_jsonl(path):
        expression = str(row.get("expression") or "").strip()
        if not expression:
            continue
        rows.append(
            {
                "alpha_id": (row.get("alpha_ids") or [None])[0],
                "status": "ACTIVE",
                "expression": expression,
                "metrics": row.get("metrics") or {},
            }
        )
    return rows
