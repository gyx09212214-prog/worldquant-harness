r"""Export WorldQuant BRAIN Community posts/comments to JSONL.

Usage:
    python scripts/export_wq_community.py --output-dir D:\tmp\worldquant_community_20260513

If the default Community paths do not match the current site, copy the request
URL from browser DevTools Network and pass it with --posts-path. For comments,
use --comments-path-template with {post_id}.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.community_triage import config_from_paths, triage_community
from quantgpt.wq_brain_client import WQBrainClient
from quantgpt.wq_community_client import (
    DEFAULT_COMMENT_PATH_TEMPLATES,
    DEFAULT_POST_PATHS,
    WQCommunityExportConfig,
    export_community,
)


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


def _parse_key_values(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise ValueError(f"Expected KEY=VALUE, got: {raw}")
        key, _, value = raw.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Missing key in: {raw}")
        out[key] = value
    return out


def _default_output_dir() -> Path:
    return Path(r"D:\tmp") / f"worldquant_community_{datetime.now():%Y%m%d}"


def _cookie_from_file(path: str) -> str:
    value = Path(path).read_text(encoding="utf-8-sig").lstrip("\ufeff").strip()
    if value.lower().startswith("cookie:"):
        return value.split(":", 1)[1].strip()
    return value


def _apply_manual_auth(session, args: argparse.Namespace) -> bool:
    used_manual_auth = False
    cookie = args.cookie or os.environ.get("WQ_COMMUNITY_COOKIE", "")
    if args.cookie_file:
        cookie = _cookie_from_file(args.cookie_file)
    if cookie:
        session.headers["Cookie"] = cookie
        used_manual_auth = True

    authorization = args.authorization or os.environ.get("WQ_COMMUNITY_AUTHORIZATION", "")
    if authorization:
        session.headers["Authorization"] = authorization
        used_manual_auth = True

    for key, value in _parse_key_values(args.header or []).items():
        session.headers[key] = value
        used_manual_auth = True

    session.headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QuantGPT WQ Community Exporter",
    )
    session.headers["Accept-Encoding"] = "identity"
    return used_manual_auth


def main() -> int:
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Export WQ Community posts/comments to JSONL")
    parser.add_argument("--output-dir", default="", help="Directory for posts.jsonl, comments.jsonl, manifest.json")
    parser.add_argument("--posts-path", action="append", default=[], help="Post list API path or full URL")
    parser.add_argument(
        "--comments-path-template",
        action="append",
        default=[],
        help="Comment API path or URL template. Use {post_id}; e.g. /posts/{post_id}/comments",
    )
    parser.add_argument("--posts-param", action="append", default=[], help="Extra post query param as KEY=VALUE")
    parser.add_argument("--comments-param", action="append", default=[], help="Extra comment query param as KEY=VALUE")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--max-posts", type=int, default=500)
    parser.add_argument("--comments-max-pages", type=int, default=5)
    parser.add_argument("--max-comments-per-post", type=int, default=500)
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    parser.add_argument("--skip-comments", action="store_true")
    parser.add_argument("--include-raw", action="store_true", help="Write raw API rows inside each JSONL object")
    parser.add_argument("--cookie", default="", help="Browser Cookie header value")
    parser.add_argument("--cookie-file", default="", help="Text file containing a Cookie header value")
    parser.add_argument("--authorization", default="", help="Authorization header value, if the Community API uses one")
    parser.add_argument("--header", action="append", default=[], help="Extra request header as KEY=VALUE")
    parser.add_argument("--no-auth", action="store_true", help="Skip WQ email/password authentication")
    parser.add_argument("--triage", action="store_true", help="Run triage_wq_community after export")
    parser.add_argument("--min-score", type=int, default=15, help="Triage min score when --triage is set")
    args = parser.parse_args()

    client = WQBrainClient()
    session = client._get_session()
    used_manual_auth = _apply_manual_auth(session, args)

    if not args.no_auth and not used_manual_auth:
        if not client.authenticate():
            print(
                "WQ authentication failed. Set WQ_BRAIN_EMAIL/WQ_BRAIN_PASSWORD, "
                "or pass --cookie/--authorization from a logged-in browser session.",
                file=sys.stderr,
            )
            return 2

    output_dir = Path(args.output_dir) if args.output_dir else _default_output_dir()
    config = WQCommunityExportConfig(
        output_dir=output_dir,
        post_paths=args.posts_path or list(DEFAULT_POST_PATHS),
        comment_path_templates=args.comments_path_template or list(DEFAULT_COMMENT_PATH_TEMPLATES),
        include_comments=not args.skip_comments,
        limit=max(1, args.limit),
        max_pages=max(1, args.max_pages),
        max_posts=max(1, args.max_posts),
        comments_max_pages=max(1, args.comments_max_pages),
        max_comments_per_post=max(1, args.max_comments_per_post),
        sleep_seconds=max(0.0, args.sleep_seconds),
        posts_params=_parse_key_values(args.posts_param or []),
        comments_params=_parse_key_values(args.comments_param or []),
        include_raw=args.include_raw,
    )

    try:
        manifest = export_community(session, config)
    finally:
        client.close()

    print(manifest["posts_file"])
    print(manifest["comments_file"])
    print(f"posts={manifest['posts']} comments={manifest['comments']}")

    if args.triage:
        triage_dir = output_dir / "triage"
        triage_manifest = triage_community(
            config_from_paths(
                posts_file=manifest["posts_file"],
                comments_file=manifest["comments_file"],
                output_dir=triage_dir,
                min_score=args.min_score,
            )
        )
        print(triage_manifest["output_dir"])
        print(f"records={triage_manifest['triage_records']} candidates={triage_manifest['candidate_rows']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
