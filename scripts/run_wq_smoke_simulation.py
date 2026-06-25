"""Run one WQ BRAIN simulation and write a compact JSON result.

This script is intended for a smoke test. It never auto-submits unless
--auto-submit is explicitly passed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.expression_parser import parse_expression
from worldquant_harness.wq_brain_client import get_client, is_configured
from worldquant_harness.wq_brain_service import run_single_simulation


def _load_dotenv():
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if value.strip():
            os.environ.setdefault(key.strip(), value.strip())


def _update_status(status_file: str | None, **updates) -> None:
    if not status_file:
        return
    path = Path(status_file)
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except Exception:
        current = {}
    current.update(updates)
    current["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def _progress_message(progress: int, message: str) -> str:
    if "并发限制" in message:
        return "Concurrent simulation limit; waiting before retry"
    if "速率限制" in message:
        return "Rate limited; waiting before retry"
    if "连接异常" in message:
        return "Connection error; waiting before retry"
    if "模拟完成" in message:
        return "Simulation completed"
    if "模拟进行中" in message:
        return f"Simulation running ({progress}%)"
    return message


def main() -> int:
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Run one WQ BRAIN simulation")
    parser.add_argument("--expression", default="rank(ts_delta(close, 5))")
    parser.add_argument("--region", default="USA")
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--decay", type=int, default=0)
    parser.add_argument("--neutralization", default="SUBINDUSTRY")
    parser.add_argument("--truncation", type=float, default=0.08)
    parser.add_argument("--auto-submit", action="store_true")
    parser.add_argument("--tag", default="worldquant_harness-smoke")
    parser.add_argument("--output", required=True)
    parser.add_argument("--status-file")
    args = parser.parse_args()

    parse_expression(args.expression, mode="wq")

    if not is_configured("primary"):
        _update_status(args.status_file, status="FAILED", message="WQ credentials are not configured")
        print("WQ credentials are not configured", file=sys.stderr)
        return 2

    client = get_client("primary")
    try:
        _update_status(args.status_file, status="AUTHENTICATING", progress=0, message="Authenticating to WQ BRAIN")
        if not client.authenticate():
            _update_status(args.status_file, status="FAILED", message="WQ authentication failed")
            print("WQ authentication failed", file=sys.stderr)
            return 3

        _update_status(args.status_file, status="SIMULATING", progress=0, message="Submitting simulation")

        def on_progress(progress: int, message: str) -> None:
            _update_status(
                args.status_file,
                status="SIMULATING",
                progress=progress,
                message=_progress_message(progress, message),
            )

        result = run_single_simulation(
            client,
            args.expression,
            region=args.region,
            universe=args.universe,
            delay=args.delay,
            decay=args.decay,
            neutralization=args.neutralization,
            truncation=args.truncation,
            auto_submit=args.auto_submit,
            tag=args.tag,
            progress_callback=on_progress,
        )
    finally:
        client.close()

    compact = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "params": {
            "expression": args.expression,
            "region": args.region,
            "universe": args.universe,
            "delay": args.delay,
            "decay": args.decay,
            "neutralization": args.neutralization,
            "truncation": args.truncation,
            "auto_submit": args.auto_submit,
            "tag": args.tag,
        },
        "result": result,
    }
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(compact, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if not result.get("ok"):
        _update_status(args.status_file, status="FAILED", message=result.get("error", "simulation failed"))
        print(result.get("error", "simulation failed"), file=sys.stderr)
        return 4

    _update_status(args.status_file, status="SUCCESS", progress=100, message="Simulation completed")
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
