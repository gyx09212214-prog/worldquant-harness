"""Run a sequential, resumable WQ BRAIN factor loop."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.wq_loop_runner import LoopConfig, run_loop


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a sequential WQ BRAIN loop")
    parser.add_argument("--candidates", default=str(ROOT / "scripts" / "wq_loop_candidates.example.jsonl"))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--results-file", default="")
    parser.add_argument("--checkpoint-file", default="")
    parser.add_argument("--status-file", default="")
    parser.add_argument("--stop-file", default="")
    parser.add_argument("--region", default="USA")
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--decay", type=int, default=0)
    parser.add_argument("--neutralization", default="SUBINDUSTRY")
    parser.add_argument("--truncation", type=float, default=0.08)
    parser.add_argument("--auto-submit", action="store_true")
    parser.add_argument("--tag", default="wq-loop")
    parser.add_argument("--max-runs", type=int, default=50)
    parser.add_argument("--max-consecutive-failures", type=int, default=5)
    parser.add_argument("--target-submissions", type=int, default=0)
    args = parser.parse_args()
    print(
        "WARNING: scripts/run_wq_loop.py is a legacy sequential loop. "
        "Canonical presubmit workflow: scripts/wq_agent_workflow.py presubmit-sequential.",
        file=sys.stderr,
    )

    candidates = Path(args.candidates)
    if not candidates.is_absolute():
        candidates = ROOT / candidates

    output_dir = Path(args.output_dir) if args.output_dir else ROOT / "reports" / f"wq_loop_{datetime.now():%Y%m%d_%H%M%S}"
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir

    config = LoopConfig(
        candidates_file=candidates,
        output_dir=output_dir,
        results_file=_resolve_path(args.results_file, output_dir / "results.jsonl"),
        checkpoint_file=_resolve_path(args.checkpoint_file, output_dir / "checkpoint.json"),
        status_file=_resolve_path(args.status_file, output_dir / "status.json"),
        stop_file=_resolve_path(args.stop_file, output_dir / "STOP"),
        region=args.region,
        universe=args.universe,
        delay=args.delay,
        decay=args.decay,
        neutralization=args.neutralization,
        truncation=args.truncation,
        auto_submit=args.auto_submit,
        tag=args.tag,
        max_runs=args.max_runs,
        max_consecutive_failures=args.max_consecutive_failures,
        target_submissions=args.target_submissions,
    )

    if not config.candidates_file.is_file():
        print(f"candidate file not found: {config.candidates_file}", file=sys.stderr)
        return 2

    rc = run_loop(config)
    print(config.output_dir)
    return rc


def _resolve_path(value: str, default: Path) -> Path:
    if not value:
        return default
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
