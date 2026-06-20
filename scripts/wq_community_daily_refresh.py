"""Daily WQ Community refresh wrapper.

This script stores no credentials. It reads optional local secrets from:
- WQ_COMMUNITY_STORAGE_STATE / .secrets/wq_community_state.json
- WQ_COMMUNITY_COOKIE / WQ_COMMUNITY_COOKIE_FILE
- WQ_COMMUNITY_AUTHORIZATION / WQ_COMMUNITY_AUTHORIZATION_FILE

If the live Community export fails, it can fall back to the most recent local
posts.jsonl/comments.jsonl cache and rebuild triage output for the current day.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = Path(r"D:\tmp")
DEFAULT_STORAGE_STATE = ROOT / ".secrets" / "wq_community_state.json"
DEFAULT_COOKIE_FILE = ROOT / ".secrets" / "wq_community_cookie.txt"
DEFAULT_AUTHORIZATION_FILE = ROOT / ".secrets" / "wq_community_authorization.txt"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _load_dotenv()

    run_date = args.run_date or datetime.now().strftime("%Y%m%d")
    output_root = _resolve_path(args.output_root)
    output_dir = _resolve_path(args.output_dir) if args.output_dir else output_root / f"{args.run_prefix}_{run_date}"
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = output_dir / "daily_refresh_manifest.json"
    env = os.environ.copy()
    auth_sources = _prepare_auth(args, env)

    export_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "export_wq_community.py"),
        "--output-dir",
        str(output_dir),
        "--limit",
        str(args.limit),
        "--max-pages",
        str(args.max_pages),
        "--max-posts",
        str(args.max_posts),
        "--comments-max-pages",
        str(args.comments_max_pages),
        "--max-comments-per-post",
        str(args.max_comments_per_post),
        "--sleep-seconds",
        str(args.sleep_seconds),
    ]
    if args.triage:
        export_cmd.extend(["--triage", "--min-score", str(args.min_score)])
    if auth_sources.get("cookie_file"):
        export_cmd.extend(["--cookie-file", auth_sources["cookie_file"]])
    if args.no_auth:
        export_cmd.append("--no-auth")

    export_result = _run(export_cmd, env=env)
    fallback_result: dict[str, Any] | None = None
    final_status = "LIVE_REFRESHED" if export_result["returncode"] == 0 else "LIVE_REFRESH_FAILED"

    if export_result["returncode"] != 0 and not args.no_fallback:
        fallback_dir = _resolve_fallback_dir(args.fallback_dir, output_dir)
        if fallback_dir:
            fallback_result = _run_fallback_triage(fallback_dir, output_dir, args)
            if fallback_result.get("returncode") == 0:
                final_status = "FALLBACK_TRIAGED"
            else:
                final_status = "FAILED"
        else:
            fallback_result = {"ok": False, "reason": "no fallback cache found"}
            final_status = "FAILED"

    manifest = {
        "schema_version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": final_status,
        "no_external_llm": True,
        "auth_sources": auth_sources,
        "output_dir": str(output_dir),
        "export": export_result,
        "fallback": fallback_result,
        "files": {
            "posts": str(output_dir / "posts.jsonl"),
            "comments": str(output_dir / "comments.jsonl"),
            "triage": str(output_dir / "triage"),
            "manifest": str(manifest_path),
        },
    }
    _write_json(manifest_path, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if final_status in {"LIVE_REFRESHED", "FALLBACK_TRIAGED"} else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh WQ Community export daily, with local-cache fallback")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--run-prefix", default="worldquant_community_daily")
    parser.add_argument("--run-date", default="")
    parser.add_argument("--storage-state", default="")
    parser.add_argument("--no-playwright-state", action="store_true")
    parser.add_argument(
        "--state-cookie-domain",
        action="append",
        default=None,
        help="Domain suffix to extract cookies for from Playwright storage state. Defaults to worldquantbrain.com",
    )
    parser.add_argument("--cookie-file", default="")
    parser.add_argument("--authorization-file", default="")
    parser.add_argument("--fallback-dir", default="")
    parser.add_argument("--no-fallback", action="store_true")
    parser.add_argument("--no-auth", action="store_true")
    parser.add_argument("--triage", action="store_true", default=True)
    parser.add_argument("--no-triage", dest="triage", action="store_false")
    parser.add_argument("--min-score", type=int, default=15)
    parser.add_argument("--max-candidates-per-record", type=int, default=5)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--max-posts", type=int, default=500)
    parser.add_argument("--comments-max-pages", type=int, default=5)
    parser.add_argument("--max-comments-per-post", type=int, default=500)
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    return parser.parse_args(argv)


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def _prepare_auth(args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    storage_state = ""
    state_cookie_count = 0
    state_authorization_present = False
    if not args.no_playwright_state:
        storage_state = _first_existing_path(
            args.storage_state,
            env.get("WQ_COMMUNITY_STORAGE_STATE", ""),
            str(DEFAULT_STORAGE_STATE),
        )

    cookie_file = _first_existing_path(
        args.cookie_file,
        env.get("WQ_COMMUNITY_COOKIE_FILE", ""),
        str(DEFAULT_COOKIE_FILE),
    )
    authorization_file = _first_existing_path(
        args.authorization_file,
        env.get("WQ_COMMUNITY_AUTHORIZATION_FILE", ""),
        str(DEFAULT_AUTHORIZATION_FILE),
    )
    if authorization_file and not env.get("WQ_COMMUNITY_AUTHORIZATION"):
        env["WQ_COMMUNITY_AUTHORIZATION"] = _read_secret(Path(authorization_file))

    if storage_state and not cookie_file and not env.get("WQ_COMMUNITY_COOKIE"):
        state_auth = _auth_from_storage_state(
            Path(storage_state),
            domain_suffixes=args.state_cookie_domain or ["worldquantbrain.com"],
        )
        state_cookie_count = state_auth["cookie_count"]
        if state_auth.get("cookie_header"):
            env["WQ_COMMUNITY_COOKIE"] = state_auth["cookie_header"]
        if state_auth.get("authorization") and not env.get("WQ_COMMUNITY_AUTHORIZATION"):
            env["WQ_COMMUNITY_AUTHORIZATION"] = state_auth["authorization"]
            state_authorization_present = True

    return {
        "playwright_state": storage_state,
        "playwright_state_cookie_count": state_cookie_count,
        "playwright_state_authorization_present": state_authorization_present,
        "cookie_env_present": bool(env.get("WQ_COMMUNITY_COOKIE")),
        "cookie_file": cookie_file,
        "authorization_env_present": bool(env.get("WQ_COMMUNITY_AUTHORIZATION")),
        "authorization_file_present": bool(authorization_file),
        "brain_email_present": bool(env.get("WQ_BRAIN_EMAIL")),
        "brain_password_present": bool(env.get("WQ_BRAIN_PASSWORD")),
    }


def _run_fallback_triage(fallback_dir: Path, output_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    posts = fallback_dir / "posts.jsonl"
    comments = fallback_dir / "comments.jsonl"
    triage_dir = output_dir / "triage"
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "triage_wq_community.py"),
        "--posts",
        str(posts),
        "--output-dir",
        str(triage_dir),
        "--min-score",
        str(args.min_score),
        "--max-candidates-per-record",
        str(args.max_candidates_per_record),
    ]
    if comments.is_file():
        cmd.extend(["--comments", str(comments)])
    result = _run(cmd, env=os.environ.copy())
    result["fallback_dir"] = str(fallback_dir)
    result["posts"] = str(posts)
    result["comments"] = str(comments) if comments.is_file() else ""
    return result


def _resolve_fallback_dir(path_value: str, current_output_dir: Path) -> Path | None:
    if path_value:
        path = _resolve_path(path_value)
        return path if (path / "posts.jsonl").is_file() else None
    candidates = [
        path
        for path in DEFAULT_OUTPUT_ROOT.glob("worldquant_community*")
        if path.is_dir() and path.resolve() != current_output_dir.resolve() and (path / "posts.jsonl").is_file()
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda path: (path.stat().st_mtime, str(path)), reverse=True)
    return candidates[0]


def _run(cmd: list[str], *, env: dict[str, str]) -> dict[str, Any]:
    started = datetime.now().isoformat(timespec="seconds")
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "started_at": started,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "cmd": _redacted_cmd(cmd),
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
    }


def _redacted_cmd(cmd: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for item in cmd:
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        redacted.append(item)
        if item in {"--cookie", "--authorization", "--header"}:
            skip_next = True
    return redacted


def _tail(text: str, *, max_chars: int = 4000) -> str:
    text = text.strip()
    return text[-max_chars:] if len(text) > max_chars else text


def _first_existing_path(*values: str) -> str:
    for value in values:
        if not value:
            continue
        path = _resolve_path(value)
        if path.is_file():
            return str(path)
    return ""


def _auth_from_storage_state(path: Path, *, domain_suffixes: list[str]) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    suffixes = [item.lstrip(".").lower() for item in domain_suffixes if item]
    cookies: list[tuple[str, str]] = []
    seen: set[str] = set()
    for cookie in data.get("cookies", []):
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if not name or not value:
            continue
        if suffixes and not any(domain == suffix or domain.endswith("." + suffix) for suffix in suffixes):
            continue
        key = f"{domain}|{name}"
        if key in seen:
            continue
        seen.add(key)
        cookies.append((name, value))

    return {
        "cookie_header": "; ".join(f"{name}={value}" for name, value in cookies),
        "cookie_count": len(cookies),
        "authorization": _authorization_from_storage_state(data, suffixes),
    }


def _authorization_from_storage_state(data: dict[str, Any], suffixes: list[str]) -> str:
    for origin in data.get("origins", []):
        origin_url = str(origin.get("origin") or "").lower()
        if suffixes and not any(suffix in origin_url for suffix in suffixes):
            continue
        for item in origin.get("localStorage", []):
            key = str(item.get("name") or "").lower()
            value = str(item.get("value") or "").strip()
            token = _token_from_storage_value(key, value)
            if token:
                return token
    return ""


def _token_from_storage_value(key: str, value: str) -> str:
    if not value:
        return ""
    if value.lower().startswith("bearer "):
        return value
    if key in {"authorization", "access_token", "id_token"} and len(value) > 20:
        return f"Bearer {value}"
    if "token" in key and len(value) > 20 and value.count(".") >= 1:
        return f"Bearer {value}"
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    for nested_key in ("authorization", "access_token", "id_token", "token"):
        nested_value = parsed.get(nested_key)
        if isinstance(nested_value, str) and len(nested_value) > 20:
            return nested_value if nested_value.lower().startswith("bearer ") else f"Bearer {nested_value}"
    return ""


def _read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8-sig").lstrip("\ufeff").strip()
    if ":" in value and value.split(":", 1)[0].lower() in {"authorization", "bearer"}:
        return value.split(":", 1)[1].strip() if value.lower().startswith("authorization:") else value
    return value


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
