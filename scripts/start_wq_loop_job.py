"""Start a WQ loop background job and return immediately."""

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
    parser = argparse.ArgumentParser(description="Start WQ loop background job")
    parser.add_argument("--candidates", default=str(ROOT / "scripts" / "wq_loop_candidates.example.jsonl"))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--region", default="USA")
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--decay", type=int, default=0)
    parser.add_argument("--neutralization", default="SUBINDUSTRY")
    parser.add_argument("--truncation", type=float, default=0.08)
    parser.add_argument("--auto-submit", action="store_true")
    parser.add_argument("--tag", default="wq-loop")
    parser.add_argument("--max-runs", type=int, default=50)
    parser.add_argument("--max-consecutive-failures", type=int, default=5)
    parser.add_argument("--target-submissions", type=int, default=0)
    args = parser.parse_args()

    candidates = Path(args.candidates)
    if not candidates.is_absolute():
        candidates = ROOT / candidates
    if not candidates.is_file():
        print(f"candidate file not found: {candidates}", file=sys.stderr)
        return 2

    logs_dir = ROOT / "logs"
    reports_dir = ROOT / "reports"
    logs_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir) if args.output_dir else reports_dir / f"wq_loop_{timestamp}"
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    status_path = output_dir / "status.json"
    checkpoint_path = output_dir / "checkpoint.json"
    results_path = output_dir / "results.jsonl"
    stop_path = output_dir / "STOP"
    stdout_path = logs_dir / f"wq_loop_{timestamp}.out.log"
    stderr_path = logs_dir / f"wq_loop_{timestamp}.err.log"
    latest_path = logs_dir / "wq_loop_latest.json"

    cmd = [
        str(PYTHON),
        "scripts/run_wq_loop.py",
        "--candidates", str(candidates),
        "--output-dir", str(output_dir),
        "--results-file", str(results_path),
        "--checkpoint-file", str(checkpoint_path),
        "--status-file", str(status_path),
        "--stop-file", str(stop_path),
        "--region", args.region,
        "--universe", args.universe,
        "--delay", str(args.delay),
        "--decay", str(args.decay),
        "--neutralization", args.neutralization,
        "--truncation", str(args.truncation),
        "--tag", args.tag,
        "--max-runs", str(args.max_runs),
        "--max-consecutive-failures", str(args.max_consecutive_failures),
        "--target-submissions", str(args.target_submissions),
    ]
    if args.auto_submit:
        cmd.append("--auto-submit")

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
        "kind": "wq_loop",
        "status": "RUNNING",
        "pid": process.pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "canonical_entrypoint": "scripts/wq_agent_workflow.py presubmit-sequential",
        "legacy_entrypoint": "scripts/start_wq_loop_job.py -> scripts/run_wq_loop.py",
        "status_reader": "scripts/wq_status.py --kind loop",
        "submit_guard": "legacy loop only submits when --auto-submit is set",
        "authoritative_status_file": str(status_path),
        "candidates": str(candidates),
        "output_dir": str(output_dir),
        "status_file": str(status_path),
        "checkpoint_file": str(checkpoint_path),
        "results_file": str(results_path),
        "stop_file": str(stop_path),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "max_runs": args.max_runs,
        "target_submissions": args.target_submissions,
        "auto_submit": args.auto_submit,
    }
    latest_path.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    status_path.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"STARTED pid={process.pid} output_dir={output_dir} max_runs={args.max_runs} "
        f"target_submissions={args.target_submissions} candidates={candidates}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
