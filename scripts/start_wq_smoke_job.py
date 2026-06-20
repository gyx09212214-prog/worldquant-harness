"""Start a WQ smoke simulation in the background and return immediately."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(r"C:\Users\guoyx\AppData\Local\Programs\Python\Python313\python.exe")


def main() -> int:
    parser = argparse.ArgumentParser(description="Start WQ smoke simulation background job")
    parser.add_argument("--expression", default="rank(ts_delta(close,5))")
    parser.add_argument("--region", default="USA")
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--decay", type=int, default=0)
    parser.add_argument("--neutralization", default="SUBINDUSTRY")
    parser.add_argument("--truncation", type=float, default=0.08)
    parser.add_argument("--tag", default="quantgpt-smoke")
    args = parser.parse_args()

    logs_dir = ROOT / "logs"
    reports_dir = ROOT / "reports"
    logs_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = reports_dir / f"wq_smoke_{timestamp}.json"
    stdout_path = logs_dir / f"wq_smoke_{timestamp}.out.log"
    stderr_path = logs_dir / f"wq_smoke_{timestamp}.err.log"
    status_path = logs_dir / "wq_smoke_latest.json"

    cmd = [
        str(PYTHON),
        "scripts/run_wq_smoke_simulation.py",
        "--expression", args.expression,
        "--region", args.region,
        "--universe", args.universe,
        "--delay", str(args.delay),
        "--decay", str(args.decay),
        "--neutralization", args.neutralization,
        "--truncation", str(args.truncation),
        "--tag", args.tag,
        "--output", str(output),
        "--status-file", str(status_path),
    ]

    stdout_fh = stdout_path.open("w", encoding="utf-8")
    stderr_fh = stderr_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=stdout_fh,
        stderr=stderr_fh,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    stdout_fh.close()
    stderr_fh.close()

    status = {
        "kind": "wq_smoke_simulation",
        "status": "RUNNING",
        "pid": process.pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "expression": args.expression,
        "region": args.region,
        "universe": args.universe,
        "delay": args.delay,
        "output": str(output),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"STARTED pid={process.pid} expression={args.expression} "
        f"region={args.region} universe={args.universe} delay={args.delay} output={output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
