"""Small JSON artifact helpers for local research workflows."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path | str) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    text = file_path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw in file_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        text = raw.strip()
        if not text or not text.startswith("{"):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def write_json(path: Path | str, payload: Any) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def write_jsonl(path: Path | str, rows: list[dict[str, Any]]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows)
    file_path.write_text(text, encoding="utf-8")
