"""Run a local candidate JSONL through WQ simulation and real submit.

This script is intentionally model-free: it reads prebuilt expressions from a
JSONL file, simulates them on WorldQuant BRAIN, and submits only candidates
that pass the platform's basic in-sample submit gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


BLOCKING_PLATFORM_CHECKS = {
    "LOW_SHARPE",
    "LOW_FITNESS",
    "LOW_TURNOVER",
    "HIGH_TURNOVER",
    "CONCENTRATED_WEIGHT",
    "LOW_SUB_UNIVERSE_SHARPE",
    "LOW_SUB_UNIVERSE_FITNESS",
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(ROOT / ".env")

    if args.poll_max_attempts > 0:
        os.environ["WQ_POLL_MAX_ATTEMPTS"] = str(args.poll_max_attempts)
    if args.poll_interval > 0:
        os.environ["WQ_POLL_INTERVAL"] = str(args.poll_interval)

    # Import after dotenv and poll env are set. wq_brain_client reads some
    # timing constants at import time.
    from worldquant_harness.wq_brain_client import get_client, is_configured
    from worldquant_harness.wq_brain_service import run_single_simulation, safe_float

    candidate_file = resolve_path(args.candidate_file)
    output_dir = resolve_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    simulation_path = output_dir / "simulation_results.jsonl"
    submit_path = output_dir / "submit_results.jsonl"
    progress_path = output_dir / "progress.json"
    summary_path = output_dir / "summary.json"

    candidates = read_jsonl(candidate_file)
    if not candidates:
        raise SystemExit(f"no candidates found: {candidate_file}")

    existing_sim_keys = set()
    existing_alpha_ids = set()
    existing_submitted_alpha_ids = set()
    existing_submit_rows = []
    if args.resume:
        for row in read_jsonl(simulation_path):
            key = str(row.get("candidate_key") or "")
            if key:
                existing_sim_keys.add(key)
            alpha_id = str(row.get("alpha_id") or "")
            if alpha_id:
                existing_alpha_ids.add(alpha_id)
        existing_submit_rows = read_jsonl(submit_path)
        for row in existing_submit_rows:
            alpha_id = str(row.get("alpha_id") or "")
            if alpha_id:
                existing_submitted_alpha_ids.add(alpha_id)

    active_count = count_active(existing_submit_rows)
    active_alpha_ids = active_ids(existing_submit_rows)

    summary = write_summary(
        summary_path,
        ok=active_count >= args.target,
        target=args.target,
        active=active_count,
        active_alpha_ids=active_alpha_ids,
        submit_attempts=len(existing_submit_rows),
        simulated=len(existing_sim_keys),
        last_event="start",
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)

    if not is_configured(args.account):
        print(json.dumps({"ok": False, "error": f"WQ credentials not configured for {args.account}"}), file=sys.stderr)
        return 2

    client = get_client(args.account)
    processed = 0
    submit_attempts = len(existing_submit_rows)
    try:
        print(json.dumps({"event": "auth_start", "account": args.account}, ensure_ascii=False), flush=True)
        if not client.authenticate(_max_retries=args.auth_retries):
            raise RuntimeError("WQ BRAIN authentication failed")
        print(json.dumps({"event": "auth_ok", "account": args.account}, ensure_ascii=False), flush=True)

        for source_index, candidate in enumerate(candidates, start=1):
            if source_index <= args.offset:
                continue
            if args.max_candidates > 0 and processed >= args.max_candidates:
                break
            if active_count >= args.target:
                break

            candidate = dict(candidate)
            key = candidate_key(candidate)
            if args.resume and key in existing_sim_keys:
                continue
            processed += 1

            expression = str(candidate.get("expression") or "").strip()
            tag = str(candidate.get("tag") or candidate.get("name") or f"candidate-{source_index}")
            settings = candidate_settings(candidate, args)
            created_at = datetime.now().isoformat(timespec="seconds")

            if not expression:
                sim_row = {
                    **candidate,
                    "created_at": created_at,
                    "index": source_index,
                    "candidate_key": key,
                    "sim_ok": False,
                    "result_error": "missing expression",
                }
                append_jsonl(simulation_path, sim_row)
                existing_sim_keys.add(key)
                write_progress(progress_path, summary_path, args, active_count, active_alpha_ids, submit_attempts, source_index, tag, "missing_expression")
                continue

            print(json.dumps({"event": "simulate_start", "index": source_index, "tag": tag}, ensure_ascii=False), flush=True)
            result = run_single_simulation(
                client,
                expression,
                region=args.region,
                universe=args.universe,
                delay=args.delay,
                decay=settings["decay"],
                neutralization=settings["neutralization"],
                truncation=settings["truncation"],
                max_trade=args.max_trade,
                max_position=args.max_position,
                auto_submit=False,
                tag=tag,
            )

            metrics = extract_metrics(result, safe_float)
            failed_checks = extract_failed_checks(result)
            blocking_failed_checks = [name for name in failed_checks if name in BLOCKING_PLATFORM_CHECKS]
            alpha_id = str(result.get("alpha_id") or "")
            submit_eligible = bool(result.get("submit_eligible"))

            sim_row = {
                **candidate,
                "created_at": created_at,
                "index": source_index,
                "candidate_key": key,
                "alpha_id": alpha_id or None,
                "sim_ok": bool(result.get("ok")),
                "metrics": metrics,
                "submit_eligible": submit_eligible,
                "failed_checks": failed_checks,
                "blocking_failed_checks": blocking_failed_checks,
                "result_error": result.get("error"),
                "effective_settings": settings,
            }
            append_jsonl(simulation_path, sim_row)
            existing_sim_keys.add(key)
            if alpha_id:
                existing_alpha_ids.add(alpha_id)
            write_progress(progress_path, summary_path, args, active_count, active_alpha_ids, submit_attempts, source_index, tag, "simulate_done")
            print(json.dumps({
                "event": "simulate_done",
                "index": source_index,
                "tag": tag,
                "alpha_id": alpha_id,
                "sim_ok": sim_row["sim_ok"],
                "submit_eligible": submit_eligible,
                "metrics": metrics,
                "blocking_failed_checks": blocking_failed_checks,
            }, ensure_ascii=False), flush=True)

            if not alpha_id or not result.get("ok") or not submit_eligible or blocking_failed_checks:
                continue
            if args.no_submit:
                continue
            if args.resume and alpha_id in existing_submitted_alpha_ids:
                continue

            print(json.dumps({"event": "submit_start", "index": source_index, "tag": tag, "alpha_id": alpha_id}, ensure_ascii=False), flush=True)
            submit_result = client.submit_alpha(alpha_id)
            submit_attempts += 1
            final_status = final_status_from_submit(submit_result)
            submit_row = {
                **candidate,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "index": source_index,
                "candidate_key": key,
                "alpha_id": alpha_id,
                "ok": bool(submit_result.get("ok")),
                "final_status": final_status,
                "platform_status": submit_result.get("platform_status"),
                "failure_kind": submit_result.get("failure_kind"),
                "detail": submit_result.get("detail"),
                "sc_value": submit_result.get("sc_value"),
                "sc_limit": submit_result.get("sc_limit"),
                "prod_value": submit_result.get("prod_value"),
                "prod_limit": submit_result.get("prod_limit"),
                "submit_result": submit_result,
                "metrics": metrics,
                "effective_settings": settings,
            }
            append_jsonl(submit_path, submit_row)
            existing_submit_rows.append(submit_row)
            existing_submitted_alpha_ids.add(alpha_id)
            if submit_row["ok"] and final_status == "ACTIVE":
                active_count += 1
                active_alpha_ids.append(alpha_id)
            write_progress(progress_path, summary_path, args, active_count, active_alpha_ids, submit_attempts, source_index, tag, "submit_done")
            print(json.dumps({
                "event": "submit_done",
                "index": source_index,
                "tag": tag,
                "alpha_id": alpha_id,
                "status": final_status,
                "ok": submit_row["ok"],
                "active_count": active_count,
                "target": args.target,
                "sc_value": submit_row["sc_value"],
                "prod_value": submit_row["prod_value"],
            }, ensure_ascii=False), flush=True)

            if args.delay_seconds > 0:
                time.sleep(args.delay_seconds)
    finally:
        client.close()

    final_submit_rows = read_jsonl(submit_path)
    final_sim_rows = read_jsonl(simulation_path)
    final_active_ids = active_ids(final_submit_rows)
    final_summary = write_summary(
        summary_path,
        ok=len(final_active_ids) >= args.target,
        target=args.target,
        active=len(final_active_ids),
        active_alpha_ids=final_active_ids,
        submit_attempts=len(final_submit_rows),
        simulated=len(final_sim_rows),
        last_event="finished",
    )
    print(json.dumps(final_summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if args.no_submit or len(final_active_ids) >= args.target else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-file", required=True, help="JSONL file with expression candidates")
    parser.add_argument("--output-dir", required=True, help="Directory for progress and result files")
    parser.add_argument("--target", type=int, default=10, help="Target ACTIVE submissions")
    parser.add_argument("--max-candidates", type=int, default=0, help="Maximum new candidates to simulate; 0 means all")
    parser.add_argument("--offset", type=int, default=0, help="Skip candidates up to this 1-based source index")
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True, help="Resume from existing output files")
    parser.add_argument("--no-submit", action="store_true", help="Simulate only; do not call /submit")
    parser.add_argument("--account", default="primary")
    parser.add_argument("--auth-retries", type=int, default=3)
    parser.add_argument("--region", default="USA")
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--neutralization", default="SUBINDUSTRY")
    parser.add_argument("--decay", type=int, default=0)
    parser.add_argument("--truncation", type=float, default=0.08)
    parser.add_argument("--max-trade", default="OFF")
    parser.add_argument("--max-position", default="OFF")
    parser.add_argument("--poll-max-attempts", type=int, default=45)
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--delay-seconds", type=float, default=5.0)
    return parser.parse_args(argv)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value


def resolve_path(path_text: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        path = ROOT / path
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_progress(
    progress_path: Path,
    summary_path: Path,
    args: argparse.Namespace,
    active_count: int,
    active_alpha_ids: list[str],
    submit_attempts: int,
    source_index: int,
    tag: str,
    event: str,
) -> None:
    payload = {
        "ok": active_count >= args.target,
        "target": args.target,
        "active": active_count,
        "active_alpha_ids": active_alpha_ids,
        "submit_attempts": submit_attempts,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "last_event": event,
        "last_index": source_index,
        "last_tag": tag,
    }
    write_json(progress_path, payload)
    write_json(summary_path, payload)


def write_summary(path: Path, **payload: Any) -> dict[str, Any]:
    payload.setdefault("updated_at", datetime.now().isoformat(timespec="seconds"))
    write_json(path, payload)
    return payload


def candidate_key(candidate: dict[str, Any]) -> str:
    settings = candidate.get("simulation_settings") or {}
    payload = {
        "tag": candidate.get("tag"),
        "expression": candidate.get("expression"),
        "settings": settings,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def candidate_settings(candidate: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    settings = candidate.get("simulation_settings") or {}
    if not isinstance(settings, dict):
        settings = {}
    return {
        "neutralization": str(settings.get("neutralization") or args.neutralization),
        "decay": int(settings.get("decay") if settings.get("decay") is not None else args.decay),
        "truncation": float(settings.get("truncation") if settings.get("truncation") is not None else args.truncation),
    }


def extract_metrics(result: dict[str, Any], safe_float) -> dict[str, float | None]:
    is_data = result.get("is_metrics") or {}
    return {
        "sharpe": safe_float(is_data.get("sharpe")),
        "fitness": safe_float(is_data.get("fitness")),
        "returns": safe_float(is_data.get("returns")),
        "turnover": safe_float(is_data.get("turnover")),
    }


def extract_failed_checks(result: dict[str, Any]) -> list[str]:
    is_data = result.get("is_metrics") or {}
    checks = is_data.get("checks") or []
    if isinstance(checks, dict):
        checks = list(checks.values())
    failed: list[str] = []
    if isinstance(checks, list):
        for check in checks:
            if not isinstance(check, dict):
                continue
            name = str(check.get("name") or check.get("check") or "").upper()
            status = str(check.get("result") or check.get("status") or "").upper()
            if name and status == "FAIL":
                failed.append(name)
    return failed


def final_status_from_submit(result: dict[str, Any]) -> str:
    if result.get("ok") and str(result.get("platform_status") or "").upper() == "ACTIVE":
        return "ACTIVE"
    failure_kind = str(result.get("failure_kind") or "").lower()
    if failure_kind == "self_correlation":
        return "SC_FAIL"
    if failure_kind == "prod_correlation":
        return "PROD_FAIL"
    if failure_kind:
        return failure_kind.upper()
    platform_status = str(result.get("platform_status") or "").upper()
    return platform_status or ("OK" if result.get("ok") else "FAIL")


def count_active(rows: list[dict[str, Any]]) -> int:
    return len(active_ids(rows))


def active_ids(rows: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for row in rows:
        alpha_id = str(row.get("alpha_id") or "")
        status = str(row.get("final_status") or "").upper()
        if alpha_id and row.get("ok") and status == "ACTIVE":
            ids.append(alpha_id)
    return ids


if __name__ == "__main__":
    raise SystemExit(main())
