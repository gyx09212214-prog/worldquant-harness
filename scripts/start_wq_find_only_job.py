"""Start a WQ find-only background job and return immediately."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)


def main() -> int:
    parser = argparse.ArgumentParser(description="Start WQ find-only background job")
    parser.add_argument("--candidates", default="")
    parser.add_argument("--exclude-expressions", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--region", default="USA")
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--decay", type=int, default=6)
    parser.add_argument("--neutralization", default="SUBINDUSTRY")
    parser.add_argument("--truncation", type=float, default=0.08)
    parser.add_argument("--account", default="primary")
    parser.add_argument("--max-runs", type=int, default=200)
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--target-eligible", type=int, default=10)
    parser.add_argument("--similarity-threshold", type=float, default=0.82)
    parser.add_argument("--hit-similarity-threshold", type=float, default=0.82)
    parser.add_argument("--api-check-after-run", action="store_true")
    parser.add_argument("--api-check-delay-seconds", type=int, default=0)
    parser.add_argument("--api-check-all", action="store_true")
    args = parser.parse_args()

    candidates = Path(args.candidates)
    if args.candidates:
        if not candidates.is_absolute():
            candidates = ROOT / candidates
        if not candidates.is_file():
            print(f"candidate file not found: {candidates}", file=sys.stderr)
            return 2

    exclude_expressions = Path(args.exclude_expressions)
    if args.exclude_expressions:
        if not exclude_expressions.is_absolute():
            exclude_expressions = ROOT / exclude_expressions
        if not exclude_expressions.is_file():
            print(f"exclude expressions file not found: {exclude_expressions}", file=sys.stderr)
            return 2

    logs_dir = ROOT / "logs"
    reports_dir = ROOT / "reports"
    logs_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir) if args.output_dir else reports_dir / f"wq_find_only_{timestamp}"
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    status_path = output_dir / "status.json"
    hits_path = output_dir / "hits.jsonl"
    stop_path = output_dir / "STOP"
    stdout_path = logs_dir / f"wq_find_only_{timestamp}.out.log"
    stderr_path = logs_dir / f"wq_find_only_{timestamp}.err.log"
    latest_path = logs_dir / "wq_find_only_latest.json"

    cmd = [
        str(PYTHON),
        "scripts/wq_find_only.py",
        "--output-dir",
        str(output_dir),
        "--hits-file",
        str(hits_path),
        "--stop-file",
        str(stop_path),
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
    if args.candidates:
        cmd.extend(["--candidates", str(candidates)])
    if args.exclude_expressions:
        cmd.extend(["--exclude-expressions", str(exclude_expressions)])
    if args.api_check_after_run:
        cmd.append("--api-check-after-run")
    if args.api_check_delay_seconds:
        cmd.extend(["--api-check-delay-seconds", str(args.api_check_delay_seconds)])
    if args.api_check_all:
        cmd.append("--api-check-all")

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

    latest = {
        "schema_version": 1,
        "kind": "wq_find_only",
        "status": "RUNNING",
        "pid": process.pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "canonical_entrypoint": "scripts/wq_daily_mining.py",
        "legacy_entrypoint": "scripts/start_wq_find_only_job.py",
        "status_reader": "scripts/wq_status.py --kind find-only",
        "submit_guard": "find-only/check-only; no submit endpoint is called",
        "authoritative_status_file": str(status_path),
        "candidates": str(candidates) if args.candidates else "",
        "exclude_expressions": str(exclude_expressions) if args.exclude_expressions else "",
        "output_dir": str(output_dir),
        "status_file": str(status_path),
        "results_file": str(output_dir / "results.jsonl"),
        "hits_file": str(hits_path),
        "stop_file": str(stop_path),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "max_runs": args.max_runs,
        "start_index": args.start_index,
        "target_eligible": args.target_eligible,
        "decay": args.decay,
        "truncation": args.truncation,
        "similarity_threshold": args.similarity_threshold,
        "hit_similarity_threshold": args.hit_similarity_threshold,
        "api_check_after_run": args.api_check_after_run,
        "api_check_delay_seconds": args.api_check_delay_seconds,
        "api_check_all": args.api_check_all,
        "auto_submit": False,
    }
    latest_path.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    status_path.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"STARTED pid={process.pid} output_dir={output_dir} "
        f"target_eligible={args.target_eligible} max_runs={args.max_runs}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
