"""CLI for WQ alpha period quality review and next-direction suggestions."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_alpha_quality_review import WQAlphaQualityReviewConfig, build_alpha_quality_review
from worldquant_harness.wq_auto_mining import load_dotenv


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_dotenv(ROOT)
    output_dir = _resolve(args.output_dir) if args.output_dir else (
        ROOT / "reports" / f"wq_alpha_quality_review_{datetime.now():%Y%m%d_%H%M%S}"
    )
    obsidian_output = None
    if not args.no_obsidian:
        obsidian_output = _resolve(args.obsidian_output) if args.obsidian_output else _default_obsidian_output()

    config = WQAlphaQualityReviewConfig(
        reports_dir=_resolve(args.reports),
        output_dir=output_dir,
        account=args.account,
        since=args.since or None,
        until=args.until or None,
        window_days=max(1, args.window_days),
        platform_enabled=not args.no_platform,
        check_policy=args.check_policy,
        max_checks=max(0, args.max_checks),
        check_polls=max(1, args.check_polls),
        check_interval=max(0, args.check_interval),
        platform_limit=max(0, args.platform_limit),
        local_file_limit=max(0, args.local_file_limit),
        obsidian_output=obsidian_output,
        profile_dir=_resolve(args.profile_dir) if args.profile_dir else None,
        write_profile_candidate=not args.no_profile_candidate,
    )
    report = build_alpha_quality_review(config)
    print(json.dumps({
        "ok": report.get("ok"),
        "period": report.get("period"),
        "counts": report.get("counts"),
        "metrics": report.get("metrics"),
        "files": report.get("files"),
        "profile_candidate": report.get("profile_candidate"),
    }, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("ok") else 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review recent WQ alpha submit/generated quality")
    parser.add_argument("--reports", default="reports")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--account", default="primary")
    parser.add_argument("--since", default="", help="Inclusive period start, e.g. 2026-06-01")
    parser.add_argument("--until", default="", help="Exclusive period end unless date-only, e.g. 2026-06-24")
    parser.add_argument("--window-days", type=int, default=14)
    parser.add_argument("--no-platform", action="store_true", help="Do not call WQ read-only/check-only API")
    parser.add_argument(
        "--check-policy",
        choices=["none", "window_unsubmitted", "all_unsubmitted"],
        default="window_unsubmitted",
    )
    parser.add_argument("--max-checks", type=int, default=50)
    parser.add_argument("--check-polls", type=int, default=2)
    parser.add_argument("--check-interval", type=int, default=5)
    parser.add_argument("--platform-limit", type=int, default=0)
    parser.add_argument("--local-file-limit", type=int, default=0)
    parser.add_argument("--obsidian-output", default="")
    parser.add_argument("--no-obsidian", action="store_true")
    parser.add_argument("--profile-dir", default="")
    parser.add_argument("--no-profile-candidate", action="store_true")
    return parser.parse_args(argv)


def _default_obsidian_output() -> Path:
    return Path(r"F:\Obsidian Vault") / "WorldQuant" / f"Alpha提交质量复盘 {datetime.now():%Y%m%d}.md"


def _resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
