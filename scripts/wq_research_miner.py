"""CLI for local WQ research-memory candidate planning."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_research_miner import WQResearchMinerConfig, run_research_miner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate WQ candidate JSONL from local research memory")
    sub = parser.add_subparsers(dest="mode", required=True)
    generate = sub.add_parser("generate", help="Generate local candidates without model calls or WQ simulation")
    generate.add_argument("--output", required=True)
    generate.add_argument("--memory-output", default="")
    generate.add_argument("--summary-output", default="")
    generate.add_argument("--run-dirs", nargs="*", default=[], help="Prior WQ run directories to mine for ready/rejected/inventory artifacts")
    generate.add_argument("--ready-files", nargs="*", default=[])
    generate.add_argument("--rejected-files", nargs="*", default=[])
    generate.add_argument("--active-inventory-files", nargs="*", default=[])
    generate.add_argument("--platform-files", nargs="*", default=[])
    generate.add_argument("--weak-memory-files", nargs="*", default=[])
    generate.add_argument("--submission-policy-file", default="")
    generate.add_argument("--legal-inputs", default="", help="Compiled WQ legal input registry JSON")
    generate.add_argument("--no-strict-legal-inputs", action="store_true", help="Warn instead of rejecting unknown registry fields")
    generate.add_argument("--account", default="primary")
    generate.add_argument("--region", default="USA")
    generate.add_argument("--universe", default="TOP3000")
    generate.add_argument("--delay", type=int, default=1)
    generate.add_argument("--max-candidates", type=int, default=40)
    generate.add_argument("--similarity-cutoff", type=float, default=0.65)
    generate.add_argument("--max-family-count", type=int, default=3)
    generate.add_argument("--max-field-signature-count", type=int, default=2)
    generate.add_argument("--max-expression-length", type=int, default=500)
    generate.add_argument("--max-nesting", type=int, default=10)
    generate.add_argument("--platform-blocker-min-correlation", type=float, default=0.70)
    generate.add_argument("--platform-blocker-field-jaccard-cutoff", type=float, default=0.62)
    generate.add_argument("--llm-provider", choices=["none"], default="none")

    args = parser.parse_args(argv)
    if args.mode != "generate":
        parser.error(f"unsupported mode: {args.mode}")

    config = WQResearchMinerConfig(
        output=_resolve(args.output),
        memory_output=_resolve(args.memory_output) if args.memory_output else None,
        summary_output=_resolve(args.summary_output) if args.summary_output else None,
        run_dirs=tuple(_resolve_many(args.run_dirs)),
        ready_files=tuple(_resolve_many(args.ready_files)),
        rejected_files=tuple(_resolve_many(args.rejected_files)),
        active_inventory_files=tuple(_resolve_many(args.active_inventory_files)),
        platform_files=tuple(_resolve_many(args.platform_files)),
        weak_memory_files=tuple(_resolve_many(args.weak_memory_files)),
        submission_policy_file=_resolve(args.submission_policy_file) if args.submission_policy_file else None,
        legal_inputs_file=_resolve(args.legal_inputs) if args.legal_inputs else None,
        strict_legal_inputs=not args.no_strict_legal_inputs,
        account=args.account,
        region=args.region,
        universe=args.universe,
        delay=args.delay,
        max_candidates=args.max_candidates,
        similarity_cutoff=args.similarity_cutoff,
        max_family_count=args.max_family_count,
        max_field_signature_count=args.max_field_signature_count,
        max_expression_length=args.max_expression_length,
        max_nesting=args.max_nesting,
        platform_blocker_min_correlation=args.platform_blocker_min_correlation,
        platform_blocker_field_jaccard_cutoff=args.platform_blocker_field_jaccard_cutoff,
        llm_provider=args.llm_provider,
    )
    summary = run_research_miner(config)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("ok") else 1


def _resolve_many(values: list[str]) -> list[Path]:
    return [_resolve(value) for value in values if value]


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
