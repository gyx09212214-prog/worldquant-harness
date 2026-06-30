"""CLI for generating Alpha-GPT style WQ search memory from local artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_alpha_search_memory import WQAlphaSearchMemoryConfig, build_alpha_search_memory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate WQ Alpha-GPT trajectory and skill memory")
    parser.add_argument("--reports", default="reports")
    parser.add_argument("--run-dirs", nargs="*", default=[])
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--local-file-limit", type=int, default=0)
    parser.add_argument("--record-limit", type=int, default=0)
    parser.add_argument("--target-submit-count", type=int, default=5)
    parser.add_argument("--min-high-score", type=float, default=1.0)
    parser.add_argument("--min-parent-score", type=float, default=1.0)
    parser.add_argument("--preferred-corr-max", type=float, default=0.70)
    parser.add_argument("--min-turnover", type=float, default=0.01)
    parser.add_argument("--max-turnover", type=float, default=0.70)
    parser.add_argument("--sc-min", type=float, default=0.70)
    parser.add_argument("--sc-max", type=float, default=0.82)
    parser.add_argument("--max-parents", type=int, default=20)
    parser.add_argument("--max-candidates-per-parent", type=int, default=12)
    parser.add_argument("--decays", default="2,4,6,8")
    parser.add_argument("--truncations", default="0.02,0.03,0.05")
    parser.add_argument("--neutralizations", default="SUBINDUSTRY,INDUSTRY,SECTOR")
    args = parser.parse_args(argv)

    output_dir = (
        _resolve(args.output_dir)
        if args.output_dir
        else ROOT / "reports" / f"wq_alpha_search_memory_{datetime.now():%Y%m%d_%H%M%S}"
    )
    result = build_alpha_search_memory(WQAlphaSearchMemoryConfig(
        reports_dir=_resolve(args.reports),
        output_dir=output_dir,
        run_dirs=tuple(_resolve(path) for path in args.run_dirs),
        local_file_limit=max(0, args.local_file_limit),
        record_limit=max(0, args.record_limit),
        target_submit_count=max(0, args.target_submit_count),
        min_high_score=args.min_high_score,
        min_parent_score=args.min_parent_score,
        preferred_corr_max=args.preferred_corr_max,
        min_turnover=args.min_turnover,
        max_turnover=args.max_turnover,
        sc_min=args.sc_min,
        sc_max=args.sc_max,
        max_parents=max(0, args.max_parents),
        max_candidates_per_parent=max(0, args.max_candidates_per_parent),
        decays=tuple(_split_ints(args.decays)),
        truncations=tuple(_split_floats(args.truncations)),
        neutralizations=tuple(_split_text(args.neutralizations)),
    ))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _split_text(value: str) -> list[str]:
    return [item.strip().upper() for item in str(value).split(",") if item.strip()]


def _split_ints(value: str) -> list[int]:
    return [int(item) for item in _split_text(value)]


def _split_floats(value: str) -> list[float]:
    return [float(item) for item in _split_text(value)]


if __name__ == "__main__":
    raise SystemExit(main())
