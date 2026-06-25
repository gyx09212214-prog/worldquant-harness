"""Read-only helpers for the bundled WQ alpha research reference catalog."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REFERENCE_DIR = REPO_ROOT / "references" / "wq_alpha_research"
REFERENCE_SOURCE_URL = "https://github.com/QuantML-Research/wq-alpha-research"
REFERENCE_LICENSE_NOTE = (
    "Copied from QuantML-Research/wq-alpha-research references/ for local research use; "
    "no LICENSE file was present in the inspected upstream repository."
)

FIELD_JSON = "wq_usa_top3000_delay1_data_fields.json"
FIELD_CSV = "wq_usa_top3000_delay1_data_fields.csv"
FIELD_SUMMARY = "wq_usa_top3000_delay1_data_fields_summary.json"
REQUIRED_FILES = (FIELD_JSON, FIELD_CSV, FIELD_SUMMARY)


class ReferenceCatalogError(RuntimeError):
    """Raised when the local reference catalog is missing or malformed."""


def reference_catalog_status(reference_dir: Path | str | None = None) -> dict[str, Any]:
    """Return file presence and summary metadata for the local WQ reference catalog."""

    root = Path(reference_dir) if reference_dir is not None else DEFAULT_REFERENCE_DIR
    files = {
        name: {
            "path": str(root / name),
            "exists": (root / name).is_file(),
            "size_bytes": (root / name).stat().st_size if (root / name).is_file() else 0,
        }
        for name in REQUIRED_FILES
    }
    missing = [name for name, meta in files.items() if not meta["exists"]]
    summary: dict[str, Any] = {}
    if not missing and (root / FIELD_SUMMARY).is_file():
        summary = load_field_summary(root)
    return {
        "ok": not missing,
        "reference_dir": str(root),
        "source_url": REFERENCE_SOURCE_URL,
        "license_note": REFERENCE_LICENSE_NOTE,
        "required_files": list(REQUIRED_FILES),
        "missing_files": missing,
        "files": files,
        "summary": summary,
    }


def load_field_summary(reference_dir: Path | str | None = None) -> dict[str, Any]:
    """Load the compact field-summary artifact."""

    root = Path(reference_dir) if reference_dir is not None else DEFAULT_REFERENCE_DIR
    path = root / FIELD_SUMMARY
    if not path.is_file():
        raise ReferenceCatalogError(f"missing WQ reference summary: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ReferenceCatalogError(f"invalid WQ reference summary: {path}")
    return payload


def load_fields(reference_dir: Path | str | None = None) -> list[dict[str, Any]]:
    """Load all field records from the bundled JSON catalog."""

    root = Path(reference_dir) if reference_dir is not None else DEFAULT_REFERENCE_DIR
    path = root / FIELD_JSON
    if not path.is_file():
        raise ReferenceCatalogError(f"missing WQ reference fields: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ReferenceCatalogError(f"invalid WQ reference fields: {path}")
    return [row for row in payload if isinstance(row, dict)]


def search_fields(
    query: str,
    *,
    category: str | None = None,
    field_type: str | None = None,
    limit: int = 20,
    reference_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Search fields by id, description, category, subcategory, or dataset text."""

    needle = query.strip().lower()
    category_filter = category.strip().lower() if category else ""
    type_filter = field_type.strip().upper() if field_type else ""
    if limit <= 0:
        return []

    matches: list[dict[str, Any]] = []
    for row in load_fields(reference_dir):
        if type_filter and str(row.get("type") or "").upper() != type_filter:
            continue
        if category_filter and category_filter not in _field_category_text(row).lower():
            continue
        haystack = _field_search_text(row)
        if needle and needle not in haystack:
            continue
        matches.append(_compact_field(row))
        if len(matches) >= limit:
            break
    return matches


def _field_search_text(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("id") or ""),
        str(row.get("name") or ""),
        str(row.get("description") or ""),
        str(row.get("type") or ""),
        _field_category_text(row),
        _dict_text(row.get("subcategory")),
        _dict_text(row.get("dataset")),
    ]
    return " ".join(parts).lower()


def _field_category_text(row: dict[str, Any]) -> str:
    return _dict_text(row.get("category"))


def _dict_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(value.get(key) or "") for key in ("id", "name"))
    return str(value or "")


def _compact_field(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "description": row.get("description") or "",
        "type": row.get("type"),
        "category": _dict_id(row.get("category")),
        "subcategory": _dict_id(row.get("subcategory")),
        "dataset": _dict_id(row.get("dataset")),
        "coverage": row.get("coverage"),
        "userCount": row.get("userCount"),
        "alphaCount": row.get("alphaCount"),
    }


def _dict_id(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("id") or value.get("name")
    return value
