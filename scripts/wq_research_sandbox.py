"""CLI for the local WQ research sandbox."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.wq_research_harness import (  # noqa: E402
    WQHarnessEvalConfig,
    WQHarnessEvolutionConfig,
    evolve_wq_research_experiment,
    render_wq_harness_report,
    run_wq_harness_evaluation,
)
from quantgpt.wq_research_sandbox import (  # noqa: E402
    DEFAULT_EXPERIMENT_ROOT,
    ResearchSandboxMineConfig,
    gate_research_experiment,
    init_research_sandbox,
    mine_research_experiment,
    new_research_experiment,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage local WQ research sandbox experiments")
    sub = parser.add_subparsers(dest="mode", required=True)

    init = sub.add_parser("init", help="Create the research experiment root")
    init.add_argument("--root", default="")

    new = sub.add_parser("new", help="Create a new research experiment")
    new.add_argument("--topic", required=True)
    new.add_argument("--hypothesis", default="")
    new.add_argument("--citation", action="append", default=[])
    new.add_argument("--root", default="")
    _add_settings_args(new)

    mine = sub.add_parser("mine", help="Generate candidates and run presubmit-sequential")
    mine.add_argument("--experiment", required=True)
    mine.add_argument("--run-dirs", nargs="*", default=[])
    mine.add_argument("--ready-files", nargs="*", default=[])
    mine.add_argument("--rejected-files", nargs="*", default=[])
    mine.add_argument("--active-inventory-files", nargs="*", default=[])
    mine.add_argument("--platform-files", nargs="*", default=[])
    mine.add_argument("--weak-memory-files", nargs="*", default=[])
    mine.add_argument("--submission-policy-file", default="")
    mine.add_argument("--max-candidates", type=int, default=200)
    mine.add_argument("--similarity-cutoff", type=float, default=0.72)
    mine.add_argument("--max-family-count", type=int, default=8)
    mine.add_argument("--max-field-signature-count", type=int, default=4)
    mine.add_argument("--target-ready", type=int, default=3)
    mine.add_argument("--max-total-simulations", type=int, default=120)
    mine.add_argument("--cycle-candidate-count", type=int, default=20)
    mine.add_argument("--max-cycles", type=int, default=10)
    mine.add_argument("--max-consecutive-empty-cycles", type=int, default=3)
    mine.add_argument("--allow-model", action="store_true")
    mine.add_argument("--no-ledger", action="store_true")
    mine.add_argument("--dry-run", action="store_true")
    _add_settings_args(mine)

    gate = sub.add_parser("gate", help="Run the fixed critic/gate for an experiment")
    gate.add_argument("--experiment", required=True)

    eval_parser = sub.add_parser("eval", help="Evaluate sandbox artifacts with harness metrics")
    eval_parser.add_argument("--experiment", required=True)
    eval_parser.add_argument("--submit-run-dirs", nargs="*", default=[])
    eval_parser.add_argument("--eval-id", default="")
    eval_parser.add_argument("--output-dir", default="")

    evolve = sub.add_parser("evolve", help="Create the next rule-based mining generation")
    evolve.add_argument("--experiment", required=True)
    evolve.add_argument("--eval-dir", default="")
    evolve.add_argument("--output-root", default="")
    evolve.add_argument("--min-improvement", type=float, default=0.02)
    evolve.add_argument("--no-child", action="store_true")

    report = sub.add_parser("report", help="Render/read a harness evaluation report")
    report.add_argument("--eval-dir", required=True)

    args = parser.parse_args(argv)
    if args.mode == "init":
        result = init_research_sandbox(_resolve(args.root) if args.root else DEFAULT_EXPERIMENT_ROOT)
    elif args.mode == "new":
        result = new_research_experiment(
            args.topic,
            root=_resolve(args.root) if args.root else DEFAULT_EXPERIMENT_ROOT,
            hypothesis=args.hypothesis,
            citations=args.citation,
            settings=_settings_from_args(args),
        )
    elif args.mode == "mine":
        result = mine_research_experiment(
            ResearchSandboxMineConfig(
                experiment=_resolve(args.experiment),
                run_dirs=tuple(_resolve_many(args.run_dirs)),
                ready_files=tuple(_resolve_many(args.ready_files)),
                rejected_files=tuple(_resolve_many(args.rejected_files)),
                active_inventory_files=tuple(_resolve_many(args.active_inventory_files)),
                platform_files=tuple(_resolve_many(args.platform_files)),
                weak_memory_files=tuple(_resolve_many(args.weak_memory_files)),
                submission_policy_file=_resolve(args.submission_policy_file) if args.submission_policy_file else None,
                max_candidates=args.max_candidates,
                similarity_cutoff=args.similarity_cutoff,
                max_family_count=args.max_family_count,
                max_field_signature_count=args.max_field_signature_count,
                target_ready=args.target_ready,
                max_total_simulations=args.max_total_simulations,
                cycle_candidate_count=args.cycle_candidate_count,
                max_cycles=args.max_cycles,
                max_consecutive_empty_cycles=args.max_consecutive_empty_cycles,
                allow_model=args.allow_model,
                use_ledger=not args.no_ledger,
                dry_run=args.dry_run,
                **_settings_from_args(args),
            )
        )
    elif args.mode == "gate":
        result = gate_research_experiment(_resolve(args.experiment))
    elif args.mode == "eval":
        result = run_wq_harness_evaluation(
            WQHarnessEvalConfig(
                experiment=_resolve(args.experiment),
                submit_run_dirs=tuple(_resolve_many(args.submit_run_dirs)),
                eval_id=args.eval_id or None,
                output_dir=_resolve(args.output_dir) if args.output_dir else None,
            )
        )
    elif args.mode == "evolve":
        result = evolve_wq_research_experiment(
            WQHarnessEvolutionConfig(
                experiment=_resolve(args.experiment),
                eval_dir=_resolve(args.eval_dir) if args.eval_dir else None,
                output_root=_resolve(args.output_root) if args.output_root else None,
                min_improvement=args.min_improvement,
                create_child_experiment=not args.no_child,
            )
        )
    elif args.mode == "report":
        result = render_wq_harness_report(_resolve(args.eval_dir))
    else:
        parser.error(f"unsupported mode: {args.mode}")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


def _add_settings_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", default="primary")
    parser.add_argument("--region", default="USA")
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--decay", type=int, default=8)
    parser.add_argument("--neutralization", default="SUBINDUSTRY")
    parser.add_argument("--truncation", type=float, default=0.08)


def _settings_from_args(args: argparse.Namespace) -> dict:
    return {
        "account": args.account,
        "region": args.region,
        "universe": args.universe,
        "delay": args.delay,
        "decay": args.decay,
        "neutralization": args.neutralization,
        "truncation": args.truncation,
    }


def _resolve_many(values: list[str]) -> list[Path]:
    return [_resolve(value) for value in values if value]


def _resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
