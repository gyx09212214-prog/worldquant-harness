"""CLI for building complete read-only WQ submission records."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_auto_mining import load_dotenv
from worldquant_harness.wq_complete_submission_records import (
    WQCompleteSubmissionRecordsConfig,
    collect_complete_submission_records,
)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_dotenv(ROOT)
    output_dir = _resolve(args.output_dir) if args.output_dir else (
        ROOT / "reports" / f"wq_complete_submission_records_{datetime.now():%Y%m%d_%H%M%S}"
    )
    config = WQCompleteSubmissionRecordsConfig(
        reports_dir=_resolve(args.reports),
        output_dir=output_dir,
        account=args.account,
        platform_enabled=not args.no_platform,
        detail_enabled=not args.no_details,
        platform_limit=max(0, args.platform_limit),
        max_details=max(0, args.max_details),
        local_file_limit=max(0, args.local_file_limit),
        record_limit=max(0, args.record_limit),
        delay_seconds=max(0.0, args.delay_seconds),
        chunk_size=max(1, args.chunk_size),
    )
    summary = collect_complete_submission_records(config)
    print(json.dumps({
        "ok": summary.get("ok"),
        "read_only": summary.get("read_only"),
        "output_dir": summary.get("output_dir"),
        "alpha_count": summary.get("alpha_count"),
        "active_count": summary.get("active_count"),
        "active_metric_complete_rate": (summary.get("coverage") or {}).get("active_metric_complete_rate"),
        "platform": summary.get("platform"),
        "files": summary.get("files"),
    }, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("ok") else 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build complete read-only WQ submission records")
    parser.add_argument("--reports", default="reports")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--account", default="primary")
    parser.add_argument("--no-platform", action="store_true", help="Skip platform GET sync and use local reports only")
    parser.add_argument("--no-details", action="store_true", help="Skip GET /alphas/{id} detail backfill")
    parser.add_argument("--platform-limit", type=int, default=0, help="Limit platform alpha pagination for smoke tests")
    parser.add_argument("--max-details", type=int, default=0, help="Limit alpha detail GETs; 0 means unlimited")
    parser.add_argument("--local-file-limit", type=int, default=0)
    parser.add_argument("--record-limit", type=int, default=0)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument("--chunk-size", type=int, default=25)
    return parser.parse_args(argv)


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
