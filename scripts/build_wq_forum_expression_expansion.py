"""CLI for expanding forum-derived WQ ideas into screened candidates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.wq_forum_expression_expander import (  # noqa: E402
    WQForumExpressionExpanderConfig,
    build_forum_expression_expansion,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Expand forum-derived WQ ideas into screened candidates")
    parser.add_argument("--forum-memory-dirs", nargs="*", default=[])
    parser.add_argument("--direction-score-files", nargs="*", default=[])
    parser.add_argument("--active-inventory-files", nargs="*", default=[])
    parser.add_argument("--platform-files", nargs="*", default=[])
    parser.add_argument("--rejected-files", nargs="*", default=[])
    parser.add_argument("--submission-policy-file", default="")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--obsidian-output", default="")
    parser.add_argument("--max-candidates", type=int, default=40)
    parser.add_argument("--similarity-cutoff", type=float, default=0.62)
    parser.add_argument("--max-family-count", type=int, default=4)
    parser.add_argument("--max-field-signature-count", type=int, default=2)
    args = parser.parse_args(argv)

    config = WQForumExpressionExpanderConfig(
        forum_memory_dirs=tuple(_resolve_many(args.forum_memory_dirs)),
        direction_score_files=tuple(_resolve_many(args.direction_score_files)),
        active_inventory_files=tuple(_resolve_many(args.active_inventory_files)),
        platform_files=tuple(_resolve_many(args.platform_files)),
        rejected_files=tuple(_resolve_many(args.rejected_files)),
        submission_policy_file=_resolve(args.submission_policy_file) if args.submission_policy_file else None,
        output_dir=_resolve(args.output_dir),
        obsidian_output=_resolve(args.obsidian_output) if args.obsidian_output else None,
        max_candidates=args.max_candidates,
        similarity_cutoff=args.similarity_cutoff,
        max_family_count=args.max_family_count,
        max_field_signature_count=args.max_field_signature_count,
    )
    plan = build_forum_expression_expansion(config)
    print(json.dumps({
        "ok": plan.get("ok"),
        "summary": plan.get("summary"),
        "files": plan.get("files"),
    }, ensure_ascii=False, indent=2, default=str))
    return 0 if plan.get("ok") else 1


def _resolve_many(values: list[str]) -> list[Path]:
    return [_resolve(value) for value in values if value]


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
