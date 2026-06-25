"""CLI for generating WQ submission experience from local artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_submission_experience import WQSubmissionExperienceConfig, build_submission_experience


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate local WQ submission experience rules and memory")
    parser.add_argument("--reports", default="reports")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--local-file-limit", type=int, default=0)
    parser.add_argument("--record-limit", type=int, default=0)
    parser.add_argument("--min-field-evidence", type=int, default=3)
    parser.add_argument("--near-pass-sharpe", type=float, default=0.60)
    parser.add_argument("--near-pass-fitness", type=float, default=0.80)
    args = parser.parse_args(argv)

    output_dir = _resolve(args.output_dir) if args.output_dir else ROOT / "reports" / f"wq_submission_experience_{datetime.now():%Y%m%d_%H%M%S}"
    summary = build_submission_experience(WQSubmissionExperienceConfig(
        reports_dir=_resolve(args.reports),
        output_dir=output_dir,
        local_file_limit=max(0, args.local_file_limit),
        record_limit=max(0, args.record_limit),
        min_field_evidence=max(1, args.min_field_evidence),
        near_pass_sharpe=args.near_pass_sharpe,
        near_pass_fitness=args.near_pass_fitness,
    ))
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("ok") else 1


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
