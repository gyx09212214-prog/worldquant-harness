"""Check generated WQ alpha IDs without submitting them."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.wq_auto_mining import load_dotenv, write_json
from quantgpt.wq_brain_client import get_client, is_configured
from quantgpt.wq_brain_service import run_check_alphas


DEFAULT_CHECK_STATUSES = {"eligible", "pending_correlation_check"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only API check for generated WQ alphas")
    parser.add_argument("--input", nargs="+", required=True, help="JSONL result/shortlist files containing alpha_id")
    parser.add_argument("--output", default="", help="Output JSONL path")
    parser.add_argument("--summary-output", default="", help="Optional summary JSON path")
    parser.add_argument("--account", default="primary")
    parser.add_argument("--all", action="store_true", help="Check every row with alpha_id, not only candidates")
    parser.add_argument("--delay-seconds", type=int, default=0, help="Delay before API check")
    parser.add_argument("--chunk-size", type=int, default=50)
    parser.add_argument("--self-correlation-cutoff", type=float, default=0.70)
    parser.add_argument("--record-ledger", action="store_true", default=os.environ.get("WQ_LEDGER_ENABLED") == "1")
    args = parser.parse_args(argv)

    input_paths = [_resolve_path(value) for value in args.input]
    output_path = _resolve_path(args.output) if args.output else ROOT / "reports" / f"wq_api_check_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    summary_path = _resolve_path(args.summary_output) if args.summary_output else None

    try:
        summary = check_generated_alphas(
            input_paths=input_paths,
            output_path=output_path,
            summary_output_path=summary_path,
            account=args.account,
            include_all=args.all,
            delay_seconds=args.delay_seconds,
            chunk_size=args.chunk_size,
            self_correlation_cutoff=args.self_correlation_cutoff,
            record_ledger=args.record_ledger,
        )
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


def check_generated_alphas(
    *,
    input_paths: list[Path],
    output_path: Path,
    summary_output_path: Path | None = None,
    account: str = "primary",
    include_all: bool = False,
    delay_seconds: int = 0,
    chunk_size: int = 50,
    self_correlation_cutoff: float = 0.70,
    record_ledger: bool = False,
) -> dict:
    """Check alpha IDs through GET /alphas/{id}; never submits."""
    load_dotenv(ROOT)
    source_rows = load_alpha_rows(input_paths, include_all=include_all)
    unique_rows = _dedupe_by_alpha_id(source_rows)
    alpha_ids = [row["alpha_id"] for row in unique_rows]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if summary_output_path:
        summary_output_path.parent.mkdir(parents=True, exist_ok=True)

    if not alpha_ids:
        summary = _summary([], {}, output_path, summary_output_path)
        _write_jsonl(output_path, [])
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
        for chunk in _chunks(alpha_ids, max(1, chunk_size)):
            result = run_check_alphas(client, chunk)
            checked.update(result.get("alphas", {}))
    finally:
        client.close()

    records = [
        build_api_check_record(
            source_row=row,
            alpha_check=checked.get(row["alpha_id"], {"ok": False, "error": "missing check result"}),
            self_correlation_cutoff=self_correlation_cutoff,
        )
        for row in unique_rows
    ]
    _write_jsonl(output_path, records)
    ledger_result = None
    if record_ledger:
        ledger_result = _record_ledger(records, account=account, source_run_id=output_path.parent.name)

    summary = _summary(records, checked, output_path, summary_output_path)
    if ledger_result is not None:
        summary["ledger"] = ledger_result
    if summary_output_path:
        write_json(summary_output_path, summary)
    return summary


def load_alpha_rows(input_paths: list[Path], *, include_all: bool = False) -> list[dict]:
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
            if not include_all and not _should_check_row(row):
                continue
            rows.append({
                "alpha_id": alpha_id,
                "expression": row.get("expression") or (row.get("result") or {}).get("expression"),
                "tag": row.get("tag"),
                "source_status": row.get("status"),
                "source_submit_eligible": row.get("submit_eligible"),
                "source_submitted": row.get("submitted"),
                "source_file": str(path),
            })
    return rows


def build_api_check_record(
    *,
    source_row: dict,
    alpha_check: dict,
    self_correlation_cutoff: float = 0.70,
) -> dict:
    api_check_status = classify_api_check(alpha_check, self_correlation_cutoff=self_correlation_cutoff)
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "alpha_id": source_row.get("alpha_id"),
        "api_check_status": api_check_status,
        "platform_status": alpha_check.get("status"),
        "grade": alpha_check.get("grade"),
        "dateCreated": alpha_check.get("dateCreated"),
        "sharpe": alpha_check.get("sharpe"),
        "fitness": alpha_check.get("fitness"),
        "returns": alpha_check.get("returns"),
        "turnover": alpha_check.get("turnover"),
        "sc_result": alpha_check.get("sc_result"),
        "sc_value": alpha_check.get("sc_value"),
        "sc_limit": alpha_check.get("sc_limit"),
        "prod_corr_result": alpha_check.get("prod_corr_result"),
        "prod_corr_value": alpha_check.get("prod_corr_value"),
        "prod_corr_limit": alpha_check.get("prod_corr_limit"),
        "review_failure_kind": alpha_check.get("review_failure_kind"),
        "error": alpha_check.get("error"),
        "source_status": source_row.get("source_status"),
        "source_submit_eligible": source_row.get("source_submit_eligible"),
        "source_submitted": source_row.get("source_submitted"),
        "tag": source_row.get("tag"),
        "expression": source_row.get("expression"),
        "source_file": source_row.get("source_file"),
    }


def classify_api_check(alpha_check: dict, *, self_correlation_cutoff: float = 0.70) -> str:
    failure_kind = alpha_check.get("review_failure_kind")
    if failure_kind == "prod_correlation" or alpha_check.get("prod_corr_result") == "FAIL":
        return "prod_correlation_fail"
    if failure_kind == "self_correlation" or alpha_check.get("sc_result") == "FAIL":
        return "self_correlation_fail"
    if failure_kind == "correlation_pending":
        return "api_check_pending"

    if not alpha_check.get("ok"):
        return "api_check_failed"

    if alpha_check.get("sc_result") == "PENDING" or alpha_check.get("prod_corr_result") == "PENDING":
        return "api_check_pending"

    has_review_signal = (
        alpha_check.get("sc_result") in {"PASS", "FAIL", "WARNING"}
        or alpha_check.get("prod_corr_result") in {"PASS", "FAIL", "WARNING"}
        or alpha_check.get("sc_value") is not None
        or alpha_check.get("prod_corr_value") is not None
    )
    if not has_review_signal and str(alpha_check.get("status") or "").upper() != "ACTIVE":
        return "api_check_pending"

    platform_status = str(alpha_check.get("status") or "").upper()
    sc_value = _safe_float(alpha_check.get("sc_value"))
    if platform_status == "ACTIVE" and sc_value is not None:
        if sc_value <= self_correlation_cutoff:
            return "platform_active_sc_below_cutoff"
        return "platform_active_sc_above_cutoff"

    if platform_status == "ACTIVE":
        return "platform_active_check_readable"
    return "api_check_readable"


def _should_check_row(row: dict) -> bool:
    status = str(row.get("status") or "").lower()
    return status in DEFAULT_CHECK_STATUSES or row.get("submit_eligible") is True


def _dedupe_by_alpha_id(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for row in rows:
        alpha_id = str(row.get("alpha_id") or "")
        if not alpha_id or alpha_id in seen:
            continue
        seen.add(alpha_id)
        unique.append(row)
    return unique


def _summary(records: list[dict], checked: dict[str, dict], output_path: Path, summary_path: Path | None) -> dict:
    counts = Counter(record.get("api_check_status", "unknown") for record in records)
    return {
        "ok": True,
        "total": len(records),
        "checked": len(checked),
        "counts": dict(sorted(counts.items())),
        "active": sum(1 for record in records if record.get("platform_status") == "ACTIVE"),
        "unsubmitted": sum(1 for record in records if record.get("platform_status") == "UNSUBMITTED"),
        "corr_pending": counts.get("api_check_pending", 0),
        "self_correlation_fail": counts.get("self_correlation_fail", 0),
        "prod_correlation_fail": counts.get("prod_correlation_fail", 0),
        "output": str(output_path),
        "summary_output": str(summary_path) if summary_path else "",
    }


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    text = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _record_ledger(records: list[dict], *, account: str, source_run_id: str) -> dict:
    try:
        from quantgpt.wq_alpha_ledger import record_api_check_records_sync

        return record_api_check_records_sync(
            records,
            settings={"account": account},
            source_run_id=source_run_id,
        )
    except Exception as exc:
        return {"ok": False, "recorded": 0, "error": str(exc)}


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_path(path_value: str) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
