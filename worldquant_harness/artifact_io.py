"""Small JSON artifact helpers for local research workflows."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


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


def rows_from_payload(payload: Any, *, collection_keys: tuple[str, ...] = ("ready", "active", "rows", "records", "review", "results", "alphas")) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in collection_keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def read_jsonish_rows(
    path: Path | str,
    *,
    collection_keys: tuple[str, ...] = ("ready", "active", "rows", "records", "review", "results", "alphas"),
    skip_comments: bool = True,
    errors: str = "replace",
) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.is_file():
        return []
    text = file_path.read_text(encoding="utf-8-sig", errors=errors).strip()
    if not text:
        return []
    if file_path.suffix.lower() == ".json":
        try:
            return rows_from_payload(json.loads(text), collection_keys=collection_keys)
        except json.JSONDecodeError:
            return []
    rows: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or (skip_comments and line.startswith("#")):
            continue
        try:
            rows.extend(rows_from_payload(json.loads(line), collection_keys=collection_keys))
        except json.JSONDecodeError:
            continue
    return rows


def read_jsonish_rows_many(
    paths: tuple[Path | str, ...] | list[Path | str],
    *,
    collection_keys: tuple[str, ...] = ("ready", "active", "rows", "records", "review", "results", "alphas"),
    skip_comments: bool = True,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        if path:
            rows.extend(read_jsonish_rows(path, collection_keys=collection_keys, skip_comments=skip_comments))
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


def append_jsonl(path: Path | str, row: dict[str, Any]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def write_text(path: Path | str, text: str) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(text, encoding="utf-8")


def write_csv(
    path: Path | str,
    rows: list[dict[str, Any]],
    fieldnames: list[str] | None = None,
    *,
    sort_fieldnames: bool = False,
    encoding: str = "utf-8-sig",
    value_transform: Callable[[Any], Any] | None = None,
    empty_fieldnames: tuple[str, ...] | None = ("empty",),
) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        fieldnames = sorted(keys) if sort_fieldnames else keys
    output_fieldnames = fieldnames if fieldnames else ([] if empty_fieldnames is None else list(empty_fieldnames))
    with file_path.open("w", newline="", encoding=encoding) as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            if value_transform is None:
                writer.writerow(row)
            else:
                writer.writerow({key: value_transform(row.get(key)) for key in output_fieldnames})


def csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value
