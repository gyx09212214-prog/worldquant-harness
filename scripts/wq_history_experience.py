"""CLI for collecting WQ history into canonical experience artifacts."""

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
from worldquant_harness.wq_history_experience import WQHistoryExperienceConfig, collect_history_experience


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect historical WQ submit/check/failure records into experience memory")
    sub = parser.add_subparsers(dest="mode", required=True)

    collect = sub.add_parser("collect", help="Collect local reports and optional WQ platform history")
    collect.add_argument("--reports", default="reports")
    collect.add_argument("--output-dir", default="")
    collect.add_argument("--account", default="primary")
    collect.add_argument("--check-policy", choices=["all", "pending", "none"], default="all")
    collect.add_argument("--write-ledger", action="store_true")
    collect.add_argument("--dry-run", action="store_true", help="Alias for not writing ledger")
    collect.add_argument("--no-platform", action="store_true", help="Skip WQ platform GET/check-only sync")
    collect.add_argument("--no-resume", action="store_true")
    collect.add_argument("--chunk-size", type=int, default=25)
    collect.add_argument("--delay-seconds", type=float, default=1.0)
    collect.add_argument("--platform-limit", type=int, default=0)
    collect.add_argument("--max-checks", type=int, default=0)
    collect.add_argument("--check-polls", type=int, default=2)
    collect.add_argument("--check-interval", type=int, default=5)
    collect.add_argument("--probe-pnl-limit", type=int, default=0)
    collect.add_argument("--local-file-limit", type=int, default=0)
    collect.add_argument("--event-limit", type=int, default=0)
    collect.add_argument("--pnl-min-overlap", type=int, default=20)
    collect.add_argument("--pnl-island-abs-corr", type=float, default=0.70)
    collect.add_argument("--pnl-warn-abs-corr", type=float, default=0.50)

    args = parser.parse_args(argv)
    if args.mode != "collect":
        parser.error(f"unsupported mode: {args.mode}")

    load_dotenv(ROOT)
    output_dir = _resolve(args.output_dir) if args.output_dir else ROOT / "reports" / f"wq_history_experience_{datetime.now():%Y%m%d_%H%M%S}"
    config = WQHistoryExperienceConfig(
        reports_dir=_resolve(args.reports),
        output_dir=output_dir,
        account=args.account,
        check_policy=args.check_policy,
        write_ledger=bool(args.write_ledger and not args.dry_run),
        platform_enabled=not args.no_platform,
        resume=not args.no_resume,
        chunk_size=max(1, args.chunk_size),
        delay_seconds=max(0.0, args.delay_seconds),
        platform_limit=max(0, args.platform_limit),
        max_checks=max(0, args.max_checks),
        check_polls=max(1, args.check_polls),
        check_interval=max(0, args.check_interval),
        probe_pnl_limit=max(0, args.probe_pnl_limit),
        local_file_limit=max(0, args.local_file_limit),
        event_limit=max(0, args.event_limit),
        pnl_min_overlap=max(2, args.pnl_min_overlap),
        pnl_island_abs_corr=args.pnl_island_abs_corr,
        pnl_warn_abs_corr=args.pnl_warn_abs_corr,
    )
    summary = collect_history_experience(config)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("ok") else 1


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
