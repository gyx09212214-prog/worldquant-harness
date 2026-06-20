"""Create or refresh a Playwright storage state for WQ Community.

Run this manually when Community export starts returning 401:

    python scripts/wq_community_login_state.py

The script opens a visible browser. Log in normally, then press Enter in the
terminal. The resulting .secrets/wq_community_state.json is ignored by git.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STORAGE_STATE = ROOT / ".secrets" / "wq_community_state.json"
DEFAULT_LOGIN_URL = "https://support.worldquantbrain.com/hc/en-us/community/topics"
DEFAULT_CHECK_URL = "https://support.worldquantbrain.com/api/v2/community/posts.json?per_page=1"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright is not installed. Run:\n"
            "  python -m pip install -e \".[community]\"\n"
            "  python -m playwright install chromium",
            file=sys.stderr,
        )
        return 2

    state_path = _resolve(args.storage_state)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser_type = getattr(p, args.browser)
        launch_kwargs: dict[str, Any] = {"headless": args.headless}
        if args.channel:
            launch_kwargs["channel"] = args.channel
        browser = browser_type.launch(**launch_kwargs)
        try:
            context_kwargs: dict[str, Any] = {}
            if args.reuse_state and state_path.is_file():
                context_kwargs["storage_state"] = str(state_path)
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            page.goto(args.login_url, wait_until="domcontentloaded", timeout=args.goto_timeout_ms)

            print(f"Opened: {args.login_url}")
            print("Log in to WQ Community in the browser window, then press Enter here.")
            input()

            context.storage_state(path=str(state_path))
            validation = _validate_context(context, args.check_url, args.check_timeout_ms, PlaywrightError)
        finally:
            browser.close()

    summary = {
        "ok": validation["ok"],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "storage_state": str(state_path),
        "login_url": args.login_url,
        "check_url": args.check_url,
        "check_status": validation.get("status"),
        "cookie_count": _cookie_count(state_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not validation["ok"]:
        print(
            "The storage state was saved, but the Community API validation did not pass. "
            "Re-run this script and complete any login, SSO, or captcha step before pressing Enter.",
            file=sys.stderr,
        )
    return 0 if validation["ok"] else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Save Playwright login state for WQ Community export")
    parser.add_argument("--storage-state", default=str(DEFAULT_STORAGE_STATE))
    parser.add_argument("--login-url", default=DEFAULT_LOGIN_URL)
    parser.add_argument("--check-url", default=DEFAULT_CHECK_URL)
    parser.add_argument("--browser", choices=["chromium", "firefox", "webkit"], default="chromium")
    parser.add_argument("--channel", default="", help="Optional browser channel, e.g. chrome or msedge")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--reuse-state", dest="reuse_state", action="store_true", default=True)
    parser.add_argument("--no-reuse-state", dest="reuse_state", action="store_false")
    parser.add_argument("--goto-timeout-ms", type=int, default=60000)
    parser.add_argument("--check-timeout-ms", type=int, default=30000)
    return parser.parse_args(argv)


def _validate_context(context: Any, check_url: str, timeout_ms: int, playwright_error: type[Exception]) -> dict:
    try:
        response = context.request.get(check_url, timeout=timeout_ms)
        return {"ok": 200 <= response.status < 400, "status": response.status}
    except playwright_error as exc:
        return {"ok": False, "status": None, "error": str(exc)}


def _cookie_count(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return 0
    return sum(1 for cookie in data.get("cookies", []) if "worldquantbrain.com" in str(cookie.get("domain", "")))


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
