"""Maintenance helpers for WQ research memory JSONL files."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MEMORY_MAINTENANCE_SCHEMA_VERSION = 1


def load_memory_rows(paths: list[Path | str]) -> list[dict[str, Any]]:
    """Load memory rows from one or more JSONL files."""

    rows: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            text = line.strip()
            if not text or not text.startswith("{"):
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append({**payload, "_memory_source_file": str(path)})
    return rows


def memory_maintenance_report(
    rows: list[dict[str, Any]],
    *,
    compress_threshold: int = 50,
    absorb_threshold: int = 3,
) -> dict[str, Any]:
    """Build a no-mutation maintenance report for memory rows."""

    active_rows = [row for row in rows if not _is_deprecated(row)]
    by_failure = Counter(_failure_kind(row) for row in active_rows)
    by_status = Counter(str(row.get("status") or row.get("platform_status") or row.get("triage_bucket") or "unknown") for row in active_rows)
    groups = _group_rows(active_rows)
    compression = [
        _compression_candidate(key, grouped)
        for key, grouped in sorted(groups.items())
        if len(grouped) >= compress_threshold
    ]
    absorption = [
        _absorption_candidate(key, grouped)
        for key, grouped in sorted(groups.items())
        if len(grouped) >= absorb_threshold and _failure_key(key)
    ]
    return {
        "schema_version": MEMORY_MAINTENANCE_SCHEMA_VERSION,
        "created_at": _now(),
        "ok": True,
        "row_count": len(rows),
        "active_row_count": len(active_rows),
        "deprecated_row_count": len(rows) - len(active_rows),
        "failure_kind_counts": dict(sorted(by_failure.items())),
        "status_counts": dict(sorted(by_status.items())),
        "compress_threshold": compress_threshold,
        "absorb_threshold": absorb_threshold,
        "compression_candidates": compression,
        "absorption_candidates": absorption,
    }


def render_memory_maintenance_markdown(report: dict[str, Any]) -> str:
    """Render a compact markdown report."""

    lines = [
        "# WQ Memory Maintenance",
        "",
        f"- Rows: {report.get('row_count')}",
        f"- Active rows: {report.get('active_row_count')}",
        f"- Deprecated rows: {report.get('deprecated_row_count')}",
        "",
        "## Failure Kinds",
        "",
    ]
    counts = report.get("failure_kind_counts") or {}
    if counts:
        lines.extend(f"- {key}: {value}" for key, value in counts.items())
    else:
        lines.append("- none")
    lines.extend(["", "## Compression Candidates", ""])
    compression = report.get("compression_candidates") or []
    if compression:
        lines.extend(f"- {row['group_key']}: {row['count']} rows" for row in compression)
    else:
        lines.append("- none")
    lines.extend(["", "## Absorption Candidates", ""])
    absorption = report.get("absorption_candidates") or []
    if absorption:
        lines.extend(f"- {row['group_key']}: {row['proposed_policy']}" for row in absorption)
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def _group_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[_memory_group_key(row)].append(row)
    return groups


def _memory_group_key(row: dict[str, Any]) -> str:
    parts = [
        _failure_kind(row),
        str(row.get("field_signature") or row.get("source_family") or ""),
        str(row.get("mutation_strategy") or row.get("tag") or ""),
    ]
    return "|".join(parts)


def _failure_kind(row: dict[str, Any]) -> str:
    return str(row.get("failure_kind") or row.get("presubmit_reject_reason") or row.get("api_check_status") or "unknown")


def _failure_key(group_key: str) -> str:
    return group_key.split("|", 1)[0]


def _compression_candidate(group_key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    examples = [_example_text(row) for row in rows[:5]]
    return {
        "group_key": group_key,
        "count": len(rows),
        "failure_kind": _failure_key(group_key),
        "source_files": sorted({str(row.get("_memory_source_file") or "") for row in rows if row.get("_memory_source_file")}),
        "examples": [example for example in examples if example],
        "action": "compress_to_retrieval_summary",
    }


def _absorption_candidate(group_key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    failure = _failure_key(group_key)
    proposed = {
        "self_correlation_high": "Prefer a new field family or operator shell after repeated self-correlation failures.",
        "self_correlation_fail": "Prefer a new field family or operator shell after repeated self-correlation failures.",
        "too_similar_to_real_or_virtual_active": "Tighten similarity cutoff and avoid nearby active expressions.",
        "illegal_field": "Refresh legal-input registry before generating more candidates from this field family.",
        "known_invalid_wq_field": "Exclude invalid fields from future candidate prompts.",
    }.get(failure, f"Down-weight future candidates matching memory group {group_key}.")
    return {
        "group_key": group_key,
        "count": len(rows),
        "failure_kind": failure,
        "proposed_policy": proposed,
        "action": "absorb_to_policy_candidate",
    }


def _example_text(row: dict[str, Any]) -> str:
    return str(row.get("retrieval_text") or row.get("expression") or row.get("triage_reason") or "")[:240]


def _is_deprecated(row: dict[str, Any]) -> bool:
    return bool(row.get("deprecated") or row.get("absorbed_to_policy") or row.get("superseded_by"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
