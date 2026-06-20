"""Start WQ find-only mining from exported Community posts/comments.

This is a bridge between the Community exporter/triage pipeline and the
find-only WQ simulation runner. It never submits alphas; it only starts
scripts/wq_find_only.py through scripts/start_wq_find_only_job.py.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.community_triage import config_from_paths, triage_community


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Community triage candidates through WQ find-only")
    parser.add_argument("--community-dir", default="", help="Directory containing posts.jsonl/comments.jsonl and/or triage")
    parser.add_argument("--posts", default="", help="Exported Community posts.jsonl")
    parser.add_argument("--comments", default="", help="Exported Community comments.jsonl")
    parser.add_argument("--triage-dir", default="", help="Existing or output triage directory")
    parser.add_argument("--refresh-triage", action="store_true", help="Rebuild triage even if candidates already exist")
    parser.add_argument("--max-candidates-per-record", type=int, default=5)
    parser.add_argument("--min-score", type=int, default=15)
    parser.add_argument("--exclude-expressions", default=str(ROOT / "reports" / "wq_find_only_sc_exclusions.jsonl"))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--region", default="USA")
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--decay", type=int, default=8)
    parser.add_argument("--neutralization", default="SUBINDUSTRY")
    parser.add_argument("--truncation", type=float, default=0.08)
    parser.add_argument("--account", default="primary")
    parser.add_argument("--max-runs", type=int, default=50)
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--target-eligible", type=int, default=1)
    parser.add_argument("--similarity-threshold", type=float, default=0.75)
    parser.add_argument("--hit-similarity-threshold", type=float, default=0.75)
    parser.add_argument("--dry-run", action="store_true", help="Build/locate candidates and print the find-only command")
    args = parser.parse_args()

    posts, comments, triage_dir = _resolve_community_inputs(args)
    candidates_file = triage_dir / "community_wq_candidates.jsonl"

    if args.refresh_triage or not candidates_file.is_file():
        if not posts.is_file():
            print(f"posts file not found: {posts}", file=sys.stderr)
            return 2
        if comments and not comments.is_file():
            print(f"comments file not found: {comments}", file=sys.stderr)
            return 2
        manifest = triage_community(
            config_from_paths(
                posts_file=posts,
                comments_file=comments if comments and comments.is_file() else None,
                output_dir=triage_dir,
                max_candidates_per_record=args.max_candidates_per_record,
                min_score=args.min_score,
            )
        )
        print(f"TRIAGE records={manifest['triage_records']} candidates={manifest['candidate_rows']} dir={triage_dir}")

    if not candidates_file.is_file():
        print(f"candidate file not found after triage: {candidates_file}", file=sys.stderr)
        return 3
    candidate_count = _count_jsonl_rows(candidates_file)
    if candidate_count == 0:
        print(f"candidate file is empty: {candidates_file}", file=sys.stderr)
        return 4

    output_dir = _resolve_output_dir(args.output_dir)
    manifest_file = output_dir / "community_find_only_manifest.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_file.write_text(
        json.dumps(
            {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "posts": str(posts) if posts else "",
                "comments": str(comments) if comments else "",
                "triage_dir": str(triage_dir),
                "candidates": str(candidates_file),
                "candidate_count": candidate_count,
                "exclude_expressions": str(_resolve_path(args.exclude_expressions)),
                "output_dir": str(output_dir),
                "auto_submit": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "start_wq_find_only_job.py"),
        "--candidates",
        str(candidates_file),
        "--exclude-expressions",
        str(_resolve_path(args.exclude_expressions)),
        "--output-dir",
        str(output_dir),
        "--region",
        args.region,
        "--universe",
        args.universe,
        "--delay",
        str(args.delay),
        "--decay",
        str(args.decay),
        "--neutralization",
        args.neutralization,
        "--truncation",
        str(args.truncation),
        "--account",
        args.account,
        "--max-runs",
        str(args.max_runs),
        "--start-index",
        str(args.start_index),
        "--target-eligible",
        str(args.target_eligible),
        "--similarity-threshold",
        str(args.similarity_threshold),
        "--hit-similarity-threshold",
        str(args.hit_similarity_threshold),
    ]
    if args.dry_run:
        print(" ".join(_quote(part) for part in cmd))
        print(f"candidates={candidate_count} manifest={manifest_file}")
        return 0

    result = subprocess.run(cmd, cwd=ROOT)
    return result.returncode


def _resolve_community_inputs(args: argparse.Namespace) -> tuple[Path, Path | None, Path]:
    community_dir = _resolve_optional_path(args.community_dir)
    posts = _resolve_optional_path(args.posts)
    comments = _resolve_optional_path(args.comments)
    triage_dir = _resolve_optional_path(args.triage_dir)

    if community_dir:
        posts = posts or community_dir / "posts.jsonl"
        default_comments = community_dir / "comments.jsonl"
        comments = comments or (default_comments if default_comments.is_file() else None)
        triage_dir = triage_dir or community_dir / "triage"
    else:
        if not posts and triage_dir:
            default_posts = triage_dir.parent / "posts.jsonl"
            default_comments = triage_dir.parent / "comments.jsonl"
            posts = default_posts if default_posts.is_file() else Path("")
            comments = comments or (default_comments if default_comments.is_file() else None)
        triage_dir = triage_dir or ROOT / "reports" / f"wq_community_triage_{datetime.now():%Y%m%d_%H%M%S}"

    return posts or Path(""), comments, triage_dir


def _resolve_output_dir(value: str) -> Path:
    if value:
        path = Path(value)
        return path if path.is_absolute() else ROOT / path
    return ROOT / "reports" / f"wq_find_only_community_{datetime.now():%Y%m%d_%H%M%S}"


def _resolve_optional_path(value: str) -> Path | None:
    if not value:
        return None
    return _resolve_path(value)


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _count_jsonl_rows(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip())


def _quote(value: str) -> str:
    if not value or any(char.isspace() for char in value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


if __name__ == "__main__":
    raise SystemExit(main())
