"""CLI for local WQ post-submit review."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_post_submit_review import WQPostSubmitReviewConfig, build_post_submit_review


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output_dir = _resolve(args.output_dir) if args.output_dir else (
        ROOT / "reports" / f"wq_post_submit_review_{datetime.now():%Y%m%d_%H%M%S}"
    )
    report = build_post_submit_review(WQPostSubmitReviewConfig(
        run_dirs=tuple(_resolve_many(args.run_dirs)),
        output_dir=output_dir,
        baseline_dirs=tuple(_resolve_many(args.baseline_run_dirs)),
        baseline_roots=tuple(_resolve_many(args.baseline_roots)),
        profile_dir=_resolve(args.profile_dir) if args.profile_dir else None,
        write_profile_candidate=not args.no_profile_candidate,
        window_days=max(1, args.window_days),
    ))
    print(json.dumps({
        "ok": report.get("ok"),
        "counts": report.get("counts"),
        "current": report.get("current"),
        "delta": report.get("delta"),
        "files": report.get("files"),
    }, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("ok") else 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build WQ post-submit review artifacts")
    parser.add_argument("--run-dirs", nargs="+", required=True)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--baseline-run-dirs", nargs="*", default=[])
    parser.add_argument("--baseline-roots", nargs="*", default=[])
    parser.add_argument("--profile-dir", default="")
    parser.add_argument("--window-days", type=int, default=14)
    parser.add_argument("--no-profile-candidate", action="store_true")
    return parser.parse_args(argv)


def _resolve_many(values: list[str]) -> list[Path]:
    return [_resolve(value) for value in values]


def _resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
