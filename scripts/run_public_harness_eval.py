"""Run the public no-submit harness eval contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.harness_runner import HarnessRunnerConfig, run_public_harness_eval  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the deterministic public worldquant-harness eval suite")
    parser.add_argument(
        "--output-root",
        default="reports/public_harness_eval",
        help="Directory where eval contract artifacts are written.",
    )
    parser.add_argument("--run-id", default="public-harness-eval", help="Stable run/eval id.")
    parser.add_argument("--topic", default="public harness eval", help="Human-readable harness topic.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output_root = Path(args.output_root)
    if not output_root.is_absolute():
        output_root = ROOT / output_root
    try:
        result = run_public_harness_eval(
            HarnessRunnerConfig(
                output_root=output_root,
                run_id=args.run_id,
                topic=args.topic,
                no_submit=True,
            )
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
