"""Build a static WQ factor map from QuantGPT ledger and artifact files."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.wq_auto_mining import load_dotenv
from quantgpt.wq_factor_map import FactorMapConfig, build_factor_map


DEFAULT_INPUT_GLOBS = (
    "reports/wq_forum_research_*/**/*.jsonl",
    "reports/wq_forum_find_only_*/**/*.jsonl",
    "reports/*community*.jsonl",
    "reports/*forum*.jsonl",
)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_dotenv(ROOT)
    os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{(ROOT / 'quantgpt.db').as_posix()}")

    from quantgpt.db import _get_session_factory

    output_dir = _resolve_path(args.output_dir) if args.output_dir else (
        ROOT / "reports" / f"wq_factor_map_{datetime.now():%Y%m%d_%H%M%S}"
    )
    obsidian_output = None
    if not args.no_obsidian:
        obsidian_output = _resolve_path(args.obsidian_output) if args.obsidian_output else _default_obsidian_output()

    input_paths = _resolve_input_paths(args.input, include_defaults=not args.no_default_inputs)
    config = FactorMapConfig(
        input_paths=tuple(input_paths),
        output_dir=output_dir,
        obsidian_output=obsidian_output,
        account=args.account or None,
        region=args.region or None,
        universe=args.universe or None,
        similarity_threshold=args.similarity_threshold,
        max_edge_nodes=args.max_edge_nodes,
        max_edges=args.max_edges,
        db_limit=args.db_limit or None,
    )

    async def _run() -> dict:
        factory = _get_session_factory()
        async with factory() as session:
            return await build_factor_map(session, config)

    report = asyncio.run(_run())
    payload = {
        "ok": report["ok"],
        "summary": report["summary"],
        "files": report.get("files", {}),
        "output_dir": str(output_dir),
        "obsidian_output": str(obsidian_output) if obsidian_output else "",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build QuantGPT WQ factor map artifacts")
    parser.add_argument("--input", nargs="*", default=[], help="JSON/JSONL artifact files or glob patterns")
    parser.add_argument("--no-default-inputs", action="store_true", help="Do not scan known forum/community artifacts")
    parser.add_argument("--output-dir", default="", help="Artifact output directory")
    parser.add_argument("--obsidian-output", default="", help="Obsidian Markdown output path")
    parser.add_argument("--no-obsidian", action="store_true", help="Skip Obsidian Markdown export")
    parser.add_argument("--account", default="primary", help="Ledger account filter; empty disables")
    parser.add_argument("--region", default="USA", help="Region filter; empty disables")
    parser.add_argument("--universe", default="TOP3000", help="Universe filter; empty disables")
    parser.add_argument("--similarity-threshold", type=float, default=0.70)
    parser.add_argument("--max-edge-nodes", type=int, default=800)
    parser.add_argument("--max-edges", type=int, default=5000)
    parser.add_argument("--db-limit", type=int, default=0, help="Optional per-table DB row limit")
    return parser.parse_args(argv)


def _resolve_input_paths(values: list[str], *, include_defaults: bool) -> list[Path]:
    patterns = list(values)
    if include_defaults:
        patterns.extend(DEFAULT_INPUT_GLOBS)
    paths: list[Path] = []
    seen: set[Path] = set()
    for value in patterns:
        if not value:
            continue
        raw = Path(value)
        matches = list(ROOT.glob(value)) if _is_relative_glob(value) else list(raw.parent.glob(raw.name)) if _has_glob(value) else [raw]
        for match in matches:
            path = match if match.is_absolute() else ROOT / match
            path = path.resolve()
            if path.is_file() and path.suffix.lower() in {".json", ".jsonl"} and path not in seen:
                seen.add(path)
                paths.append(path)
    paths.sort()
    return paths


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _default_obsidian_output() -> Path:
    code_root = ROOT.parents[1] if len(ROOT.parents) > 1 else ROOT
    return (
        code_root
        / "doc"
        / "obsidian"
        / "exports"
        / "Quant"
        / "Stock"
        / "Factors"
        / f"QuantGPT 因子地图 {datetime.now():%Y%m%d}.md"
    )


def _has_glob(value: str) -> bool:
    return any(char in value for char in "*?[]")


def _is_relative_glob(value: str) -> bool:
    return _has_glob(value) and not Path(value).is_absolute()


if __name__ == "__main__":
    raise SystemExit(main())
