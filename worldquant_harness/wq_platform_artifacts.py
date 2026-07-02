"""Shared read-only platform and local artifact helpers for WQ records."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


def local_file_inventory(
    files: Iterable[Path],
    *,
    source_type_for_path: Callable[[Path | str], str],
) -> list[dict[str, Any]]:
    return [
        {
            "path": str(path),
            "name": path.name,
            "source_type": source_type_for_path(path),
            "size_bytes": path.stat().st_size,
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
        }
        for path in files
    ]


def fetch_platform_alphas(client: Any, *, limit: int = 0) -> list[dict[str, Any]]:
    alphas: list[dict[str, Any]] = []
    offset = 0
    page_size = 100
    while True:
        payload = client.get_json(
            "/users/self/alphas",
            params={"limit": page_size, "offset": offset, "order": "-dateCreated"},
        )
        results = payload.get("results") if isinstance(payload, dict) else []
        if not isinstance(results, list) or not results:
            break
        alphas.extend(row for row in results if isinstance(row, dict))
        if limit and len(alphas) >= limit:
            return alphas[:limit]
        offset += len(results)
        total = payload.get("count") if isinstance(payload, dict) else None
        if isinstance(total, int) and offset >= total:
            break
    return alphas
