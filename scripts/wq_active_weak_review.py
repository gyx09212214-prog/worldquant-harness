"""CLI for weak ACTIVE/SUBMITTED WorldQuant alpha review."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.wq_active_weak_review import WQActiveWeakReviewConfig, run_active_weak_review


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review weak ACTIVE/SUBMITTED WQ alphas without model calls or submit")
    sub = parser.add_subparsers(dest="mode", required=True)
    review = sub.add_parser("review", help="Fetch or load platform alphas and distill weak-active memory")
    review.add_argument("--output-dir", default="")
    review.add_argument("--run-id", default="")
    review.add_argument("--platform-file", default="", help="Optional local JSON/JSONL platform alpha file")
    review.add_argument("--account", default="primary")
    review.add_argument("--platform-sync-limit", type=int, default=2000)
    review.add_argument("--max-checks", type=int, default=30)
    review.add_argument("--check-chunk-size", type=int, default=25)
    review.add_argument("--weak-score-cutoff", type=float, default=4.0)
    review.add_argument("--bottom-quantile", type=float, default=0.30)

    args = parser.parse_args(argv)
    if args.mode != "review":
        parser.error(f"unsupported mode: {args.mode}")

    config = WQActiveWeakReviewConfig(
        output_dir=_resolve_output_dir(args.output_dir, args.run_id),
        platform_file=_resolve(args.platform_file) if args.platform_file else None,
        account=args.account,
        platform_sync_limit=args.platform_sync_limit,
        max_checks=args.max_checks,
        check_chunk_size=args.check_chunk_size,
        weak_score_cutoff=args.weak_score_cutoff,
        bottom_quantile=args.bottom_quantile,
    )
    try:
        summary = run_active_weak_review(config)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("ok") else 1


def _resolve_output_dir(value: str, run_id: str) -> Path:
    if value:
        return _resolve(value)
    if run_id:
        return ROOT / "reports" / "wq_active_weak_reviews" / run_id
    return ROOT / "reports" / "wq_active_weak_reviews" / f"{datetime.now():%Y%m%d_%H%M%S}"


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
