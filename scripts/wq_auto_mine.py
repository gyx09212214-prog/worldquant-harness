"""Run a WorldQuant BRAIN autonomous alpha mining loop."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.wq_auto_mining import WQAutoMiner, WQAutoMiningConfig


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a WQ BRAIN auto-mining loop")
    parser.add_argument("--candidates", default=str(ROOT / "scripts" / "wq_loop_candidates.example.jsonl"))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--results-file", default="")
    parser.add_argument("--checkpoint-file", default="")
    parser.add_argument("--status-file", default="")
    parser.add_argument("--submitted-file", default="")
    parser.add_argument("--summary-file", default="")
    parser.add_argument("--stop-file", default="")
    parser.add_argument("--fields-file", default="")
    parser.add_argument("--community-context-dir", default="")
    parser.add_argument("--community-context-mode", choices=["auto", "off"], default="auto")
    parser.add_argument("--community-seed-limit", type=int, default=12)
    parser.add_argument("--region", default="USA")
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--decay", type=int, default=0)
    parser.add_argument("--neutralization", default="SUBINDUSTRY")
    parser.add_argument("--truncation", type=float, default=0.08)
    parser.add_argument("--account", default="primary")
    parser.add_argument("--tag", default="wq-auto-mine")
    parser.add_argument("--max-runs", type=int, default=200)
    parser.add_argument("--max-rounds", type=int, default=30)
    parser.add_argument("--parents-per-round", type=int, default=3)
    parser.add_argument("--children-per-parent", type=int, default=4)
    parser.add_argument("--max-generation", type=int, default=4)
    parser.add_argument("--max-consecutive-failures", type=int, default=8)
    parser.add_argument("--target-submissions", type=int, default=3)
    parser.add_argument("--direction", default="")
    args = parser.parse_args()
    print(
        "WARNING: scripts/wq_auto_mine.py is a legacy autonomous mining path. "
        "Canonical presubmit workflow: scripts/wq_agent_workflow.py presubmit-sequential.",
        file=sys.stderr,
    )

    candidates = _resolve_path(args.candidates, ROOT)
    if not candidates.is_file():
        print(f"candidate file not found: {candidates}", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir) if args.output_dir else ROOT / "reports" / f"wq_auto_mine_{datetime.now():%Y%m%d_%H%M%S}"
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir

    fields_file = _resolve_optional_path(args.fields_file, ROOT)
    community_context_dir = _resolve_optional_path(args.community_context_dir, ROOT)
    config = WQAutoMiningConfig(
        candidates_file=candidates,
        output_dir=output_dir,
        results_file=_resolve_path(args.results_file, output_dir / "candidates.jsonl"),
        checkpoint_file=_resolve_path(args.checkpoint_file, output_dir / "checkpoint.json"),
        status_file=_resolve_path(args.status_file, output_dir / "status.json"),
        submitted_file=_resolve_path(args.submitted_file, output_dir / "submitted.jsonl"),
        summary_file=_resolve_path(args.summary_file, output_dir / "summary.md"),
        stop_file=_resolve_path(args.stop_file, output_dir / "STOP"),
        fields_file=fields_file,
        community_context_dir=community_context_dir,
        community_context_mode=args.community_context_mode,
        community_seed_limit=max(0, args.community_seed_limit),
        region=args.region,
        universe=args.universe,
        delay=args.delay,
        decay=args.decay,
        neutralization=args.neutralization,
        truncation=args.truncation,
        account=args.account,
        tag=args.tag,
        max_runs=args.max_runs,
        max_rounds=args.max_rounds,
        parents_per_round=args.parents_per_round,
        children_per_parent=args.children_per_parent,
        max_generation=args.max_generation,
        max_consecutive_failures=args.max_consecutive_failures,
        target_submissions=args.target_submissions,
        direction=args.direction or None,
    )

    rc = WQAutoMiner(config).run()
    print(config.output_dir)
    return rc


def _resolve_path(value: str, default: Path) -> Path:
    if not value:
        return default
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _resolve_optional_path(value: str, root: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else root / path


if __name__ == "__main__":
    raise SystemExit(main())
