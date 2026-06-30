"""CLI for the minimal Alpha-GPT dry-run workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_alpha_gpt_workflow import AlphaGPTWorkflowConfig, run_alpha_gpt_dry_run  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a no-submit Alpha-GPT-style dry-run workflow")
    sub = parser.add_subparsers(dest="mode", required=True)
    demo = sub.add_parser("demo", help="Run the synthetic no-submit Alpha-GPT workflow")
    demo.add_argument("--topic", default="analyst revision momentum")
    demo.add_argument("--run-id", default="alpha-gpt-demo")
    demo.add_argument("--output-dir", default="reports/examples/alpha_gpt_demo")
    demo.add_argument("--profile-name", default="default")
    demo.add_argument("--account", default="primary")
    demo.add_argument("--region", default="USA")
    demo.add_argument("--universe", default="TOP3000")
    demo.add_argument("--delay", type=int, default=1)
    demo.add_argument("--legal-inputs", default="")
    demo.add_argument("--no-strict-legal-inputs", action="store_true")
    demo.add_argument("--no-negative-fixture", action="store_true")

    args = parser.parse_args(argv)
    output_dir = _resolve_path(args.output_dir)
    legal_inputs = _resolve_path(args.legal_inputs) if args.legal_inputs else None
    try:
        summary = run_alpha_gpt_dry_run(
            AlphaGPTWorkflowConfig(
                output_dir=output_dir,
                topic=args.topic,
                run_id=args.run_id,
                profile_name=args.profile_name,
                account=args.account,
                region=args.region,
                universe=args.universe,
                delay=args.delay,
                legal_inputs_file=legal_inputs,
                strict_legal_inputs=not args.no_strict_legal_inputs,
                include_negative_fixture=not args.no_negative_fixture,
            )
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("ok") else 1


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
