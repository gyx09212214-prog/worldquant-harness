"""Analyze WQ alpha detail probe outputs into yearly PnL metrics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.wq_pnl_analysis import analyze_probe_directory, write_pnl_analysis_artifacts


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    probe_dir = _resolve_path(args.probe_dir)
    output_dir = _resolve_path(args.output_dir) if args.output_dir else probe_dir
    if not probe_dir.is_dir():
        print(json.dumps({"ok": False, "error": f"probe_dir not found: {probe_dir}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    submitted_rows = _read_jsonl_paths([_resolve_path(path) for path in args.submitted_input])
    report = analyze_probe_directory(
        probe_dir,
        submitted_rows=submitted_rows,
        default_book_size=args.default_book_size,
    )
    files = write_pnl_analysis_artifacts(report, output_dir)
    print(json.dumps({
        "ok": True,
        "probe_dir": str(probe_dir),
        "output_dir": str(output_dir),
        "alpha_count": report["alpha_count"],
        "pnl_found_count": report["pnl_found_count"],
        "files": files,
    }, ensure_ascii=False, indent=2))
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze read-only WQ PnL probe outputs")
    parser.add_argument("--probe-dir", required=True, help="Directory produced by scripts/probe_wq_alpha_detail.py")
    parser.add_argument("--output-dir", default="", help="Output directory; defaults to probe dir")
    parser.add_argument("--submitted-input", nargs="*", default=[], help="JSONL files with alpha_id/tag metadata")
    parser.add_argument("--default-book-size", type=float, default=20_000_000.0)
    return parser.parse_args(argv)


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _read_jsonl_paths(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = raw.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows.append(row)
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
