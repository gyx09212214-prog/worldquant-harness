"""CLI for WQ research profiles and the bundled reference catalog."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_reference_catalog import reference_catalog_status, search_fields
from worldquant_harness.wq_research_profile import (
    apply_candidate,
    candidate_diff,
    init_profile,
    load_profile,
    profile_status,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage local WQ research profiles")
    sub = parser.add_subparsers(dest="mode", required=True)

    init_cmd = sub.add_parser("init", help="Initialize a profile directory")
    init_cmd.add_argument("--name", default="default")
    init_cmd.add_argument("--profile-dir", default="")
    init_cmd.add_argument("--force", action="store_true")

    status_cmd = sub.add_parser("status", help="Show active profile status")
    status_cmd.add_argument("--profile-dir", default="")

    show_cmd = sub.add_parser("show", help="Print a profile")
    show_cmd.add_argument("--name", default="")
    show_cmd.add_argument("--profile-dir", default="")

    diff_cmd = sub.add_parser("diff", help="Diff a candidate profile against active")
    diff_cmd.add_argument("candidate")
    diff_cmd.add_argument("--profile-dir", default="")

    apply_cmd = sub.add_parser("apply", help="Apply a candidate profile")
    apply_cmd.add_argument("candidate")
    apply_cmd.add_argument("--profile-dir", default="")

    catalog_cmd = sub.add_parser("catalog-status", help="Show bundled reference catalog status")
    catalog_cmd.add_argument("--reference-dir", default="")

    search_cmd = sub.add_parser("search-fields", help="Search bundled WQ data fields")
    search_cmd.add_argument("query")
    search_cmd.add_argument("--category", default="")
    search_cmd.add_argument("--field-type", default="")
    search_cmd.add_argument("--limit", type=int, default=20)
    search_cmd.add_argument("--reference-dir", default="")

    args = parser.parse_args(argv)
    if args.mode == "init":
        result = init_profile(name=args.name, profile_dir=_path(args.profile_dir), force=args.force)
    elif args.mode == "status":
        result = profile_status(profile_dir=_path(args.profile_dir))
    elif args.mode == "show":
        result = {"ok": True, "profile": load_profile(args.name or None, profile_dir=_path(args.profile_dir))}
    elif args.mode == "diff":
        result = candidate_diff(args.candidate, profile_dir=_path(args.profile_dir))
    elif args.mode == "apply":
        result = apply_candidate(args.candidate, profile_dir=_path(args.profile_dir))
    elif args.mode == "catalog-status":
        result = reference_catalog_status(_path(args.reference_dir))
    elif args.mode == "search-fields":
        result = {
            "ok": True,
            "fields": search_fields(
                args.query,
                category=args.category or None,
                field_type=args.field_type or None,
                limit=args.limit,
                reference_dir=_path(args.reference_dir),
            ),
        }
    else:
        parser.error(f"unsupported mode: {args.mode}")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok", True) else 1


def _path(value: str) -> Path | None:
    return Path(value) if value else None


if __name__ == "__main__":
    raise SystemExit(main())
