"""CLI for WQ research-memory maintenance reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_memory_maintenance import (
    load_memory_rows,
    memory_maintenance_report,
    render_memory_maintenance_markdown,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report WQ research-memory compression and policy-absorption candidates")
    parser.add_argument("memory_files", nargs="+")
    parser.add_argument("--compress-threshold", type=int, default=50)
    parser.add_argument("--absorb-threshold", type=int, default=3)
    parser.add_argument("--output", default="")
    parser.add_argument("--markdown-output", default="")
    args = parser.parse_args(argv)

    rows = load_memory_rows([Path(value) for value in args.memory_files])
    report = memory_maintenance_report(
        rows,
        compress_threshold=args.compress_threshold,
        absorb_threshold=args.absorb_threshold,
    )
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    if args.markdown_output:
        path = Path(args.markdown_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_memory_maintenance_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
