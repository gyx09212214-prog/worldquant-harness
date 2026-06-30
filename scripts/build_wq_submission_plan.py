"""Build a forum-informed WQ submission optimization plan."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_forum_submission_optimizer import (
    ForumSubmissionOptimizerConfig,
    build_forum_submission_plan,
)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    factor_map_dir = _resolve(args.factor_map_dir)
    forum_dirs = tuple(_resolve_many(args.forum_memory_dirs or _default_forum_dirs()))
    output_dir = _resolve(args.output_dir) if args.output_dir else (
        ROOT / "reports" / f"wq_submission_plan_{datetime.now():%Y%m%d_%H%M%S}"
    )
    obsidian_output = None
    if not args.no_obsidian:
        obsidian_output = _resolve(args.obsidian_output) if args.obsidian_output else _default_obsidian_output()

    plan = build_forum_submission_plan(
        ForumSubmissionOptimizerConfig(
            factor_map_dir=factor_map_dir,
            forum_memory_dirs=forum_dirs,
            output_dir=output_dir,
            obsidian_output=obsidian_output,
            submitted_alpha_map_dir=_resolve(args.submitted_alpha_map_dir) if args.submitted_alpha_map_dir else None,
            community_skill_memory_file=_resolve(args.community_skill_memory_file) if args.community_skill_memory_file else None,
            region=args.region,
            universe=args.universe,
            account=args.account,
            max_directions=args.max_directions,
            strict_similarity_cutoff=args.strict_similarity_cutoff,
            default_similarity_cutoff=args.default_similarity_cutoff,
        )
    )
    print(json.dumps({
        "ok": plan["ok"],
        "summary": plan["summary"],
        "files": plan.get("files", {}),
    }, ensure_ascii=False, indent=2, default=str))
    return 0 if plan.get("ok") else 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build forum-informed WQ submission plan")
    parser.add_argument("--factor-map-dir", default="reports/wq_factor_map_smoke_20260609")
    parser.add_argument("--forum-memory-dirs", nargs="*", default=[])
    parser.add_argument("--submitted-alpha-map-dir", default="")
    parser.add_argument("--community-skill-memory-file", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--obsidian-output", default="")
    parser.add_argument("--no-obsidian", action="store_true")
    parser.add_argument("--account", default="primary")
    parser.add_argument("--region", default="USA")
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--max-directions", type=int, default=30)
    parser.add_argument("--strict-similarity-cutoff", type=float, default=0.62)
    parser.add_argument("--default-similarity-cutoff", type=float, default=0.70)
    return parser.parse_args(argv)


def _default_forum_dirs() -> list[str]:
    candidates = [
        "reports/wq_forum_research_20260521/idea_memory",
        "reports/wq_forum_research_20260521/idea_memory_long_1000",
    ]
    return [value for value in candidates if (ROOT / value).is_dir()]


def _resolve_many(values: list[str]) -> list[Path]:
    return [_resolve(value) for value in values if value]


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _default_obsidian_output() -> Path:
    code_root = ROOT.parents[1] if len(ROOT.parents) > 1 else ROOT
    return (
        code_root
        / "doc"
        / "obsidian"
        / "exports"
        / "Quant"
        / "Stock"
        / "Factors"
        / f"worldquant-harness 论坛提交优化 {datetime.now():%Y%m%d}.md"
    )


if __name__ == "__main__":
    raise SystemExit(main())
