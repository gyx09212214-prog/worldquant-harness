"""Unified status reader for WQ mining jobs.

The ``logs/*_latest.json`` files are only pointers. The authoritative state is
the status file inside each run directory.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

KIND_CONFIG = {
    "find-only": {
        "latest": "wq_find_only_latest.json",
        "missing": "NO_WQ_FIND_ONLY_RUN",
        "terminal_ok": {"FOUND", "PARTIAL_FOUND", "NOT_FOUND", "STOPPED"},
        "terminal_fail": {"FAILED", "FAILED_SUBMISSION_GUARD"},
    },
    "loop": {
        "latest": "wq_loop_latest.json",
        "missing": "NO_WQ_LOOP_RUN",
        "terminal_ok": {"SUCCESS", "STOPPED"},
        "terminal_fail": {"FAILED"},
    },
}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    snapshot = build_status_snapshot(kind=args.kind, root=_resolve_root(args.root))
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_status(snapshot))
        if snapshot.get("stderr_tail"):
            print("stderr_tail:")
            print(snapshot["stderr_tail"])
    return int(snapshot.get("exit_code", 2))


def build_status_snapshot(*, kind: str, root: Path = ROOT) -> dict:
    if kind not in KIND_CONFIG:
        raise ValueError(f"unsupported kind: {kind}")
    config = KIND_CONFIG[kind]
    latest_file = root / "logs" / config["latest"]
    if not latest_file.is_file():
        return {
            "kind": kind,
            "status": config["missing"],
            "latest_file": str(latest_file),
            "status_file": str(latest_file),
            "authoritative_status_file": "",
            "running": False,
            "terminal": False,
            "stale": False,
            "exit_code": 1,
        }

    latest = _read_json(latest_file)
    status_file = _path_value(latest.get("status_file"), root=root)
    authoritative = _read_json(status_file) if status_file and status_file.is_file() else {}
    state = str(authoritative.get("status") or latest.get("status") or "UNKNOWN").upper()
    pid = _safe_int(latest.get("pid"))
    pid_running = _pid_running(pid)
    terminal_ok = state in config["terminal_ok"]
    terminal_fail = state in config["terminal_fail"]
    terminal = terminal_ok or terminal_fail
    running = bool(pid_running and not terminal)
    stale = bool(not pid_running and not terminal)
    status = "RUNNING" if running else state
    if stale:
        status = "STALE"

    data = authoritative or latest
    snapshot = {
        "kind": kind,
        "status": status,
        "state": state,
        "running": running,
        "terminal": terminal,
        "stale": stale,
        "pid": pid,
        "pid_running": pid_running,
        "latest_file": str(latest_file),
        "status_file": str(status_file) if status_file else "",
        "authoritative_status_file": str(status_file) if authoritative else "",
        "output_dir": str(data.get("output_dir") or latest.get("output_dir") or ""),
        "reason": data.get("reason") or "",
        "message": _ascii_message(data.get("message") or data.get("reason") or ""),
        "current_expression": data.get("current_expression") or "",
        "counters": _counters(data),
        "best": _best_record(data),
        "raw_status": data,
        "raw_latest": latest,
    }
    snapshot["exit_code"] = _exit_code(snapshot, terminal_ok=terminal_ok, terminal_fail=terminal_fail)
    if snapshot["exit_code"] == 2:
        stderr_path = _path_value(latest.get("stderr"), root=root)
        snapshot["stderr_tail"] = _tail(stderr_path, lines=80) if stderr_path else ""
    return snapshot


def format_status(snapshot: dict) -> str:
    status = snapshot["status"]
    if status in {"NO_WQ_FIND_ONLY_RUN", "NO_WQ_LOOP_RUN"}:
        return f"{status} status_file={snapshot['status_file']}"
    if snapshot["kind"] == "find-only":
        return _format_find_only(snapshot)
    return _format_loop(snapshot)


def _format_find_only(snapshot: dict) -> str:
    counters = snapshot["counters"]
    best = snapshot["best"]
    target = _first(snapshot["raw_status"].get("target_eligible"), snapshot["raw_latest"].get("target_eligible"), 0)
    prefix = "RUNNING" if snapshot["running"] else snapshot["status"]
    current = f" current={snapshot['current_expression']}" if snapshot.get("current_expression") else ""
    return (
        f"{prefix} pid={snapshot.get('pid') or ''} state={snapshot.get('state')} "
        f"reason={snapshot.get('reason')} eligible={counters.get('eligible', 0)}/{target} "
        f"processed={counters.get('processed', 0)} completed={counters.get('completed', 0)} "
        f"failed={counters.get('failed', 0)} skipped={counters.get('skipped', 0)} "
        f"best_alpha={best.get('alpha_id', '')} best_fitness={best.get('fitness', '')} "
        f"best_sharpe={best.get('sharpe', '')} best_sc={best.get('sc_result', '')} "
        f"best_similarity={best.get('similarity', '')} message={snapshot.get('message', '')}"
        f"{current} output_dir={snapshot.get('output_dir', '')}"
    )


def _format_loop(snapshot: dict) -> str:
    counters = snapshot["counters"]
    best = snapshot["best"]
    total = _first(snapshot["raw_status"].get("total_candidates"), "na")
    max_runs = _first(snapshot["raw_status"].get("max_runs"), snapshot["raw_latest"].get("max_runs"), 0)
    target = _first(snapshot["raw_status"].get("target_submissions"), snapshot["raw_latest"].get("target_submissions"), 0)
    prefix = "RUNNING" if snapshot["running"] else snapshot["status"]
    current = f" current={snapshot['current_expression']}" if snapshot.get("current_expression") else ""
    return (
        f"{prefix} pid={snapshot.get('pid') or ''} state={snapshot.get('state')} "
        f"reason={snapshot.get('reason')} processed={counters.get('processed', 0)}/{total} "
        f"runs={counters.get('runs_started', 0)}/{max_runs} "
        f"submitted={counters.get('submitted', 0)}/{target} completed={counters.get('completed', 0)} "
        f"failed={counters.get('failed', 0)} skipped={counters.get('skipped', 0)} "
        f"best_alpha={best.get('alpha_id', '')} best_fitness={best.get('fitness', '')} "
        f"best_sharpe={best.get('sharpe', '')} message={snapshot.get('message', '')}"
        f"{current} output_dir={snapshot.get('output_dir', '')}"
    )


def _counters(data: dict) -> dict:
    raw = data.get("counters") if isinstance(data.get("counters"), dict) else data
    completed = _safe_int(raw.get("completed")) or 0
    failed = _safe_int(raw.get("failed")) or 0
    skipped = _safe_int(raw.get("skipped")) or 0
    processed = _safe_int(raw.get("processed"))
    if processed is None:
        processed = completed + failed + skipped
    return {
        "processed": processed,
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
        "eligible": _safe_int(raw.get("eligible")) or 0,
        "runs_started": _safe_int(raw.get("runs_started")) or 0,
        "submitted": _safe_int(raw.get("submitted")) or 0,
    }


def _best_record(data: dict) -> dict:
    row = data.get("hit") if isinstance(data.get("hit"), dict) else None
    if row is None:
        row = data.get("best") if isinstance(data.get("best"), dict) else {}
    sc = row.get("sc_result") or (row.get("self_correlation") or {}).get("result") or ""
    similarity = (
        row.get("similarity")
        or (row.get("similarity_to_blocked") or {}).get("overall_similarity")
        or ""
    )
    return {
        "alpha_id": row.get("alpha_id") or "",
        "fitness": row.get("fitness") or "",
        "sharpe": row.get("sharpe") or "",
        "sc_result": sc,
        "similarity": similarity,
    }


def _exit_code(snapshot: dict, *, terminal_ok: bool, terminal_fail: bool) -> int:
    if snapshot["running"] or terminal_ok:
        return 0
    if terminal_fail or snapshot["stale"]:
        return 2
    return 2


def _pid_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            return False
        return result.returncode == 0 and str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _tail(path: Path | None, *, lines: int) -> str:
    if not path or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[-lines:])


def _read_json(path: Path | None) -> dict:
    if not path or not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _path_value(value: Any, *, root: Path) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else root / path


def _resolve_root(value: str) -> Path:
    if not value:
        return ROOT
    return Path(value).resolve()


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ascii_message(value: Any) -> str:
    text = str(value or "")
    if any(ord(char) > 127 for char in text):
        return "WQ platform wait/retry; see status_file for raw message"
    return text


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return ""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read the authoritative WQ job status")
    parser.add_argument("--kind", choices=sorted(KIND_CONFIG), required=True)
    parser.add_argument("--root", default="")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
