"""Triage exported WorldQuant BRAIN Community content for WQ factor mining."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.community_triage import config_from_paths, triage_community


def main() -> int:
    parser = argparse.ArgumentParser(description="Triage WQ Community posts/comments into candidate seeds")
    parser.add_argument("--posts", required=True, help="JSONL file exported as posts.jsonl")
    parser.add_argument("--comments", default="", help="Optional JSONL file exported as comments.jsonl")
    parser.add_argument(
        "--output-dir",
        default=r"D:\tmp\worldquant_community_20260513\triage",
        help="Directory for triage_records.jsonl, candidate JSONL, report, and manifest",
    )
    parser.add_argument("--max-candidates-per-record", type=int, default=5)
    parser.add_argument("--min-score", type=int, default=15)
    args = parser.parse_args()

    config = config_from_paths(
        posts_file=args.posts,
        comments_file=args.comments or None,
        output_dir=args.output_dir,
        max_candidates_per_record=args.max_candidates_per_record,
        min_score=args.min_score,
    )
    manifest = triage_community(config)
    print(manifest["output_dir"])
    print(f"records={manifest['triage_records']} candidates={manifest['candidate_rows']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
