"""Seed ready/rejected record loaders for WQ workflow loops."""

from __future__ import annotations

from pathlib import Path

from .artifact_io import read_jsonl as _read_jsonl
from .wq_agent_records import candidate_dedupe_key as _candidate_dedupe_key


def _load_seed_ready_records(paths: list[Path]) -> list[dict]:
    records: list[dict] = []
    seen: set[str] = set()
    for path in paths:
        if not path or not path.exists():
            continue
        for row in _read_jsonl(path):
            expression = str(row.get("expression") or "").strip()
            if not expression:
                continue
            key = _candidate_dedupe_key(row)
            if key in seen:
                continue
            seen.add(key)
            ready = dict(row)
            ready.setdefault("virtual_active_status", "VIRTUAL_ACTIVE")
            ready.setdefault("presubmit_accepted", True)
            ready["seed_ready"] = True
            records.append(ready)
    return records


def _load_rejected_expression_keys(paths: list[Path]) -> set[str]:
    keys: set[str] = set()
    for path in paths:
        if not path or not path.exists():
            continue
        for row in _read_jsonl(path):
            expression = str(row.get("expression") or "").strip()
            if expression:
                keys.add(_candidate_dedupe_key(row))
    return keys
