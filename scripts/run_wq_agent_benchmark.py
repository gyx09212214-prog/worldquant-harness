"""Run the deterministic, offline WQ agent benchmark suite."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_agent_benchmark import BenchmarkConfig, run_wq_agent_benchmark  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Fake-only deterministic WorldQuant agent benchmark (never submits)."
    )
    parser.add_argument(
        "--suite-path",
        default=str(ROOT / "tests" / "fixtures" / "wq_agent_benchmark"),
        help="Directory containing suite.json and the nine versioned case fixtures.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "reports" / "wq_agent_benchmark"),
        help="Directory for manifest, traces, scorecard, cost report, and Markdown report.",
    )
    parser.add_argument(
        "--seeds",
        default="",
        help="Optional comma-separated integer seeds. Defaults to the suite's five fixed seeds.",
    )
    parser.add_argument(
        "--simulation-budget",
        type=int,
        default=None,
        help="Optional per-trial simulation cap override, applied equally to every variant.",
    )
    parser.add_argument("--code-revision", default=None, help="Optional explicit revision recorded in the manifest.")
    parser.add_argument("--model-id", default="scripted-wq-agent-v1", help="Fake model identity for manifest hashing.")
    parser.add_argument(
        "--prompt-hash",
        default="sha256:benchmark-prompt-schema-v1",
        help="Versioned prompt identity included in the manifest hash.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    seeds = None
    if args.seeds.strip():
        try:
            seeds = tuple(int(value.strip()) for value in args.seeds.split(",") if value.strip())
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": f"invalid --seeds: {exc}"}, ensure_ascii=False), file=sys.stderr)
            return 2
    try:
        result = run_wq_agent_benchmark(
            BenchmarkConfig(
                suite_path=Path(args.suite_path),
                output_dir=Path(args.output_dir),
                seeds=seeds,
                simulation_budget=args.simulation_budget,
                code_revision=args.code_revision,
                model_id=args.model_id,
                prompt_hash=args.prompt_hash,
            )
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
