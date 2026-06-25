"""CLI for building reviewable WQ knowledge updates from harness evaluations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_knowledge_evolution import build_wq_knowledge_snippet, load_eval_summary
from worldquant_harness.wq_reference_catalog import reference_catalog_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a reviewable WQ knowledge update")
    parser.add_argument("--eval-summary", required=True)
    parser.add_argument("--reference-dir", default="")
    parser.add_argument("--output", default="docs/WQ_ALPHA_RESEARCH_SKILL.md")
    parser.add_argument("--apply", action="store_true", help="Write the generated snippet to --output")
    args = parser.parse_args(argv)

    summary = load_eval_summary(args.eval_summary)
    catalog = reference_catalog_status(Path(args.reference_dir) if args.reference_dir else None)
    snippet = build_wq_knowledge_snippet(summary, catalog_status=catalog)
    result = {
        "ok": True,
        "eval_summary": args.eval_summary,
        "output": args.output,
        "applied": bool(args.apply),
        "snippet": snippet,
    }
    if args.apply:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(snippet, encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
