"""Build WQ Community-derived skill memory from triage output."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.community_skill_memory import CommunitySkillMemoryConfig, build_community_skill_memory


def main() -> int:
    parser = argparse.ArgumentParser(description="Build reusable WQ Community skill memory")
    parser.add_argument("--triage-dir", required=True, help="Directory containing triage_records.jsonl")
    parser.add_argument("--output-dir", default="", help="Output directory for community_skill_memory.jsonl")
    parser.add_argument("--forum-memory-dir", action="append", default=[], help="Optional forum idea memory directory")
    parser.add_argument("--source-label", default="", help="Optional label written to the manifest")
    parser.add_argument("--top-sources", type=int, default=8)
    parser.add_argument("--min-recipe-evidence", type=int, default=1)
    args = parser.parse_args()

    manifest = build_community_skill_memory(
        CommunitySkillMemoryConfig(
            triage_dir=Path(args.triage_dir),
            output_dir=Path(args.output_dir) if args.output_dir else None,
            forum_memory_dirs=tuple(Path(path) for path in args.forum_memory_dir),
            source_label=args.source_label,
            top_sources=max(1, args.top_sources),
            min_recipe_evidence=max(0, args.min_recipe_evidence),
        )
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
