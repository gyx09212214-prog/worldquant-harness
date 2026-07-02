"""Run WQ BRAIN check-only submission review without submitting alphas."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_wq_generated_alphas import (
    build_api_check_record,
)
from worldquant_harness.artifact_io import read_jsonl as _read_jsonl
from worldquant_harness.artifact_io import write_jsonl as _write_jsonl
from worldquant_harness.record_utils import dedupe_rows_by_key as _dedupe_rows_by_key
from worldquant_harness.wq_auto_mining import load_dotenv, write_json
from worldquant_harness.wq_brain_client import get_client, is_configured
from worldquant_harness.wq_brain_service import run_check_submissions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run WQ check-only submission review")
    parser.add_argument("--input", nargs="*", default=[], help="JSONL result/shortlist files containing alpha_id")
    parser.add_argument("--ids", nargs="*", default=[], help="Explicit alpha IDs")
    parser.add_argument("--output", default="", help="Output JSONL path")
    parser.add_argument("--summary-output", default="", help="Optional summary JSON path")
    parser.add_argument("--account", default="primary")
    parser.add_argument("--all", action="store_true", help="Check every row with alpha_id, not only candidates")
    parser.add_argument("--delay-seconds", type=int, default=0)
    parser.add_argument("--chunk-size", type=int, default=25)
    parser.add_argument("--max-checks", type=int, default=0, help="Maximum new alpha IDs to check this run")
    parser.add_argument("--resume", action="store_true", help="Preserve existing output records and skip completed IDs")
    parser.add_argument(
        "--only-pending",
        action="store_true",
        help="Only check rows whose previous check status is pending or missing review signals",
    )
    parser.add_argument("--record-ledger", action="store_true", default=os.environ.get("WQ_LEDGER_ENABLED") == "1")
    args = parser.parse_args(argv)

    output_path = _resolve_path(args.output) if args.output else ROOT / "reports" / f"wq_submission_check_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    summary_path = _resolve_path(args.summary_output) if args.summary_output else None
    input_paths = [_resolve_path(value) for value in args.input]

    try:
        summary = check_wq_submissions(
            input_paths=input_paths,
            alpha_ids=args.ids,
            output_path=output_path,
            summary_output_path=summary_path,
            account=args.account,
            include_all=args.all,
            delay_seconds=args.delay_seconds,
            chunk_size=args.chunk_size,
            max_checks=args.max_checks,
            resume=args.resume,
            only_pending=args.only_pending,
            record_ledger=args.record_ledger,
        )
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


def check_wq_submissions(
    *,
    input_paths: list[Path],
    alpha_ids: list[str],
    output_path: Path,
    summary_output_path: Path | None = None,
    account: str = "primary",
    include_all: bool = False,
    delay_seconds: int = 0,
    chunk_size: int = 25,
    max_checks: int = 0,
    resume: bool = False,
    only_pending: bool = False,
    record_ledger: bool = False,
) -> dict:
    """Run /alphas/{id}/check for IDs; never calls /submit."""
    load_dotenv(ROOT)
    rows = load_submission_rows(input_paths, include_all=include_all, only_pending=only_pending) if input_paths else []
    rows.extend({"alpha_id": alpha_id, "source_file": "cli"} for alpha_id in alpha_ids if alpha_id)
    unique_rows = _dedupe_rows(rows)

    existing_records = _read_jsonl(output_path) if resume and output_path.is_file() else []
    existing_by_id = _records_by_alpha_id(existing_records)
    if resume:
        if only_pending:
            carried_records = [
                record for record in existing_records
                if not _needs_pending_check(record)
            ]
            skip_ids = {str(record.get("alpha_id") or "") for record in carried_records if record.get("alpha_id")}
        else:
            carried_records = existing_records
            skip_ids = set(existing_by_id)
        unique_rows = [row for row in unique_rows if str(row.get("alpha_id") or "") not in skip_ids]
    else:
        carried_records = []

    if max_checks and max_checks > 0:
        unique_rows = unique_rows[:max_checks]
    ids = [row["alpha_id"] for row in unique_rows]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if summary_output_path:
        summary_output_path.parent.mkdir(parents=True, exist_ok=True)

    if not ids:
        final_records = carried_records if resume else []
        summary = _summary(final_records, {}, output_path, summary_output_path)
        summary["newly_checked"] = 0
        summary["resume"] = resume
        _write_jsonl(output_path, final_records)
        if summary_output_path:
            write_json(summary_output_path, summary)
        return summary

    if not is_configured(account):
        raise RuntimeError(f"WQ BRAIN credentials are not configured (account={account})")

    if delay_seconds > 0:
        time.sleep(delay_seconds)

    client = get_client(account)
    try:
        if not client.authenticate():
            raise RuntimeError(f"WQ BRAIN authentication failed (account={account})")

        checked: dict[str, dict] = {}
        for chunk in _chunks(ids, max(1, chunk_size)):
            result = run_check_submissions(client, chunk)
            checked.update(result.get("alphas", {}))
    finally:
        client.close()

    records = [
        build_api_check_record(
            source_row=row,
            alpha_check=checked.get(row["alpha_id"], {"ok": False, "error": "missing check result"}),
        )
        for row in unique_rows
    ]
    final_records = _merge_records(carried_records, records) if resume else records
    _write_jsonl(output_path, final_records)

    ledger_result = None
    if record_ledger:
        ledger_result = _record_ledger(records, account=account, source_run_id=output_path.parent.name)

    summary = _summary(final_records, checked, output_path, summary_output_path)
    summary["newly_checked"] = len(records)
    summary["resume"] = resume
    if ledger_result is not None:
        summary["ledger"] = ledger_result
    if summary_output_path:
        write_json(summary_output_path, summary)
    return summary


def load_submission_rows(input_paths: list[Path], *, include_all: bool = False, only_pending: bool = False) -> list[dict]:
    rows: list[dict] = []
    for path in input_paths:
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or not line.startswith("{"):
                continue
            row = json.loads(line)
            alpha_id = row.get("alpha_id") or (row.get("result") or {}).get("alpha_id")
            if not alpha_id:
                continue
            if only_pending and not _needs_pending_check(row):
                continue
            if not only_pending and not include_all and not _should_check_source_row(row):
                continue
            rows.append({
                "alpha_id": alpha_id,
                "expression": row.get("expression") or (row.get("result") or {}).get("expression"),
                "tag": row.get("tag"),
                "source_status": row.get("status") or row.get("source_status"),
                "source_submit_eligible": _first_present(row.get("submit_eligible"), row.get("source_submit_eligible")),
                "source_submitted": _first_present(row.get("submitted"), row.get("source_submitted")),
                "source_api_check_status": row.get("api_check_status"),
                "source_file": str(path),
            })
    return rows


def _dedupe_rows(rows: list[dict]) -> list[dict]:
    return _dedupe_rows_by_key(rows, lambda row: str(row.get("alpha_id") or ""), skip_empty=True)


def _should_check_source_row(row: dict) -> bool:
    status = str(row.get("status") or row.get("source_status") or "").lower()
    if status in {"eligible", "pending_correlation_check", "pre_submit_pass", "correlation_pending"}:
        return True
    api_status = str(row.get("api_check_status") or "").lower()
    if api_status in {"api_check_pending", "api_check_failed"}:
        return True
    return row.get("submit_eligible") is True or row.get("source_submit_eligible") is True


def _needs_pending_check(row: dict) -> bool:
    api_status = str(row.get("api_check_status") or row.get("source_api_check_status") or "").lower()
    if api_status in {"api_check_pending", "api_check_failed"}:
        return True
    sc_result = str(row.get("sc_result") or "").upper()
    prod_result = str(row.get("prod_corr_result") or "").upper()
    if sc_result == "PENDING" or prod_result == "PENDING":
        return True
    if row.get("alpha_id") and not _has_review_signal(row):
        status = str(row.get("platform_status") or row.get("status") or "").upper()
        source_status = str(row.get("source_status") or "").lower()
        return status != "ACTIVE" and source_status in {"eligible", "pending_correlation_check", "correlation_pending", ""}
    return False


def _has_review_signal(row: dict) -> bool:
    return (
        row.get("sc_result") in {"PASS", "FAIL", "WARNING"}
        or row.get("prod_corr_result") in {"PASS", "FAIL", "WARNING"}
        or row.get("sc_value") is not None
        or row.get("prod_corr_value") is not None
    )


def _first_present(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _records_by_alpha_id(records: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for record in records:
        alpha_id = str(record.get("alpha_id") or "")
        if alpha_id:
            out[alpha_id] = record
    return out


def _merge_records(existing_records: list[dict], new_records: list[dict]) -> list[dict]:
    new_by_id = _records_by_alpha_id(new_records)
    merged: list[dict] = []
    emitted: set[str] = set()
    for record in existing_records:
        alpha_id = str(record.get("alpha_id") or "")
        if alpha_id and alpha_id in new_by_id:
            merged.append(new_by_id[alpha_id])
            emitted.add(alpha_id)
        else:
            merged.append(record)
            if alpha_id:
                emitted.add(alpha_id)
    for record in new_records:
        alpha_id = str(record.get("alpha_id") or "")
        if alpha_id and alpha_id in emitted:
            continue
        merged.append(record)
    return merged


def _summary(records: list[dict], checked: dict[str, dict], output_path: Path, summary_path: Path | None) -> dict:
    counts = Counter(record.get("api_check_status", "unknown") for record in records)
    return {
        "ok": True,
        "total": len(records),
        "checked": len(checked),
        "counts": dict(sorted(counts.items())),
        "corr_pending": counts.get("api_check_pending", 0),
        "self_correlation_fail": counts.get("self_correlation_fail", 0),
        "prod_correlation_fail": counts.get("prod_correlation_fail", 0),
        "readable": counts.get("api_check_readable", 0) + counts.get("platform_active_check_readable", 0),
        "output": str(output_path),
        "summary_output": str(summary_path) if summary_path else "",
    }


def _record_ledger(records: list[dict], *, account: str, source_run_id: str) -> dict:
    try:
        from worldquant_harness.wq_alpha_ledger import record_api_check_records_safe

        return record_api_check_records_safe(records, account=account, source_run_id=source_run_id)
    except Exception as exc:
        return {"ok": False, "recorded": 0, "error": str(exc)}


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _resolve_path(path_value: str) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
