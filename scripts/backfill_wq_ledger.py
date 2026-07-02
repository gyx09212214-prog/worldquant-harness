"""Backfill WQ alpha ledger/failure memory from existing report JSONL files."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.source_utils import source_run_id_from_report_path as _source_run_id
from worldquant_harness.wq_alpha_ledger import record_api_check_record_sync, record_find_only_entry_sync
from worldquant_harness.wq_auto_mining import load_dotenv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill WQ alpha ledger from JSONL reports")
    parser.add_argument("--reports", default="reports", help="Reports directory")
    parser.add_argument("--write", action="store_true", help="Persist rows to the database")
    parser.add_argument("--limit", type=int, default=0, help="Optional max rows to process")
    parser.add_argument("--account", default="primary")
    args = parser.parse_args(argv)

    load_dotenv(ROOT)
    reports_dir = _resolve_path(args.reports)
    files = discover_files(reports_dir)
    summary = backfill(files, write=args.write, limit=args.limit, account=args.account)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


def discover_files(reports_dir: Path) -> list[Path]:
    if reports_dir.is_file():
        return [reports_dir]
    patterns = [
        "wq_find_only_*/results.jsonl",
        "wq_find_only_*/hits.jsonl",
        "wq_find_only_*/api_check*.jsonl",
        "wq_*candidates*.jsonl",
        "wq_*submit_candidates*.jsonl",
        "wq_find_only_sc_exclusions.jsonl",
        "wq_submitted_platform_alphas.jsonl",
    ]
    files: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for path in sorted(reports_dir.glob(pattern)):
            if path.is_file() and path not in seen:
                seen.add(path)
                files.append(path)
    return files


def backfill(files: list[Path], *, write: bool, limit: int = 0, account: str = "primary") -> dict:
    counts: Counter[str] = Counter()
    examples: dict[str, str] = {}
    processed = 0

    for path in files:
        for row in _iter_jsonl(path):
            if limit and processed >= limit:
                break
            kind = _row_kind(row, path)
            counts[kind] += 1
            examples.setdefault(kind, str(path))
            processed += 1
            if write:
                _write_row(row, path=path, kind=kind, account=account)
        if limit and processed >= limit:
            break

    return {
        "ok": True,
        "mode": "write" if write else "dry_run",
        "files": len(files),
        "processed": processed,
        "counts": dict(sorted(counts.items())),
        "examples": examples,
    }


def _write_row(row: dict, *, path: Path, kind: str, account: str) -> None:
    source_run_id = _source_run_id(path)
    source_file = str(path)
    settings = {"account": account}
    if kind == "api_check":
        record_api_check_record_sync(row, settings=settings, source_run_id=source_run_id)
        return

    if kind == "platform_alpha":
        row = {
            **row,
            "status": row.get("status") or "active",
            "api_check_status": "platform_active_check_readable",
            "platform_status": row.get("platform_status") or row.get("status") or "ACTIVE",
        }
    elif kind == "exclusion":
        row = {
            **row,
            "status": row.get("status") or "skipped_similar",
            "source_family": row.get("reason") or row.get("tag") or "exclusion",
        }
    else:
        row = {**row, "status": row.get("status") or "candidate"}

    record_find_only_entry_sync(
        row,
        settings=settings,
        source_run_id=source_run_id,
        source_file=source_file,
        source_type=kind,
    )


def _row_kind(row: dict, path: Path) -> str:
    name = path.name.lower()
    if row.get("api_check_status") or name.startswith("api_check"):
        return "api_check"
    if "submitted_platform" in path.name.lower() or "wq_submitted_platform_alphas" in str(path).lower():
        return "platform_alpha"
    if "sc_exclusions" in path.name.lower():
        return "exclusion"
    if row.get("status") or row.get("alpha_id"):
        return "find_only"
    return "candidate"


def _iter_jsonl(path: Path):
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("{"):
            yield {"expression": line}
            continue
        try:
            value: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("expression"):
            yield value


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
