"""Build deterministic WQ repair candidates from presubmit review rows."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.wq_policy_repair_planner import PolicyRepairPlannerConfig, build_policy_repair_plan


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output_dir = _resolve(args.output_dir) if args.output_dir else (
        ROOT / "reports" / f"wq_policy_repair_{datetime.now():%Y%m%d_%H%M%S}"
    )
    plan = build_policy_repair_plan(PolicyRepairPlannerConfig(
        review_paths=tuple(_resolve_many(args.review_paths)),
        output_dir=output_dir,
        submission_policy_file=_resolve(args.submission_policy_file) if args.submission_policy_file else None,
        obsidian_output=_resolve(args.obsidian_output) if args.obsidian_output else None,
        max_candidates=args.max_candidates,
        max_repairs_per_row=args.max_repairs_per_row,
    ))
    print(json.dumps({
        "ok": plan["ok"],
        "summary": plan["summary"],
        "files": plan.get("files", {}),
    }, ensure_ascii=False, indent=2, default=str))
    return 0 if plan.get("ok") else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build deterministic repair candidates from WQ presubmit review rows")
    parser.add_argument("--review-paths", nargs="+", required=True)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--submission-policy-file", default="")
    parser.add_argument("--obsidian-output", default="")
    parser.add_argument("--max-candidates", type=int, default=40)
    parser.add_argument("--max-repairs-per-row", type=int, default=4)
    return parser.parse_args(argv)


def _resolve_many(values: list[str]) -> list[Path]:
    return [_resolve(value) for value in values if value]


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
