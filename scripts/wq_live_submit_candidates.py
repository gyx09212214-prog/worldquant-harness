"""Sequentially simulate, check, and submit WQ candidates.

This is a small operational runner for real submissions. It avoids waiting for
a full workflow cycle: each candidate is simulated, checked, and submitted as
soon as self-correlation is confirmed below the strict cutoff.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_auto_mining import load_dotenv, validate_wq_expression
from worldquant_harness.wq_brain_client import get_client, is_configured
from worldquant_harness.wq_brain_service import run_single_simulation

IGNORED_PLATFORM_CHECKS = {"SELF_CORRELATION", "PROD_CORRELATION", "MATCHES_COMPETITION"}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_dotenv(ROOT)

    if not is_configured(args.account):
        print(json.dumps({"ok": False, "error": f"WQ credentials not configured for {args.account}"}), file=sys.stderr)
        return 2

    output_dir = _resolve(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "progress": output_dir / "progress.json",
        "simulation_results": output_dir / "simulation_results.jsonl",
        "check_results": output_dir / "check_results.jsonl",
        "submit_results": output_dir / "submit_results.jsonl",
        "summary": output_dir / "summary.json",
    }

    candidates = _load_candidates(_resolve(args.candidate_file))
    candidates = [
        row for row in candidates
        if int(row.get("source_index") or 0) >= args.start_index
    ]
    if args.max_candidates > 0:
        candidates = candidates[: args.max_candidates]

    existing_submit_rows = _read_jsonl(paths["submit_results"]) if args.resume else []
    submitted_successes = [row for row in existing_submit_rows if row.get("ok") and str(row.get("final_status") or "").upper() == "ACTIVE"]
    tried_keys = {str(row.get("candidate_key") or "") for row in _read_jsonl(paths["simulation_results"])} if args.resume else set()
    tried_alpha_ids = {str(row.get("alpha_id") or "") for row in existing_submit_rows if row.get("alpha_id")} if args.resume else set()

    client = get_client(args.account)
    try:
        print(json.dumps({"event": "auth_start", "account": args.account}, ensure_ascii=False), flush=True)
        if not client.authenticate(_max_retries=args.auth_retries):
            raise RuntimeError("WQ BRAIN authentication failed")
        print(json.dumps({"event": "auth_ok", "account": args.account}, ensure_ascii=False), flush=True)

        for index, candidate in enumerate(candidates, start=1):
            if len(submitted_successes) >= args.target_successes:
                break
            key = _candidate_key(candidate)
            if key in tried_keys:
                continue

            _write_progress(paths["progress"], candidate, index, len(candidates), "simulate_started")
            try:
                validate_wq_expression(candidate["expression"])
            except Exception as exc:
                row = _base_row(candidate, key)
                row.update({"ok": False, "stage": "validation", "status": "validation_failed", "error": str(exc)})
                _append_jsonl(paths["simulation_results"], row)
                tried_keys.add(key)
                continue

            settings = _settings_for_candidate(candidate, args)
            print(
                json.dumps(
                    {"event": "simulate_start", "index": index, "tag": candidate.get("tag")},
                    ensure_ascii=False,
                ),
                flush=True,
            )

            def on_progress(percent: int, message: str, *, candidate: dict[str, Any] = candidate) -> None:
                _write_progress(paths["progress"], candidate, index, len(candidates), "simulate_running", percent=percent, message=message)

            sim = run_single_simulation(
                client,
                candidate["expression"],
                region=settings["region"],
                universe=settings["universe"],
                delay=settings["delay"],
                decay=settings["decay"],
                neutralization=settings["neutralization"],
                truncation=settings["truncation"],
                max_trade=settings["maxTrade"],
                max_position=settings["maxPosition"],
                auto_submit=False,
                tag=candidate.get("tag"),
                progress_callback=on_progress,
            )
            sim_row = _simulation_row(candidate, key, settings, sim)
            _append_jsonl(paths["simulation_results"], sim_row)
            tried_keys.add(key)
            _write_progress(paths["progress"], candidate, index, len(candidates), sim_row.get("status") or "simulated", alpha_id=sim_row.get("alpha_id"))

            alpha_id = str(sim_row.get("alpha_id") or "")
            if not _should_check(sim_row) or not alpha_id or alpha_id in tried_alpha_ids:
                print(
                    json.dumps(
                        {
                            "event": "simulate_done",
                            "index": index,
                            "status": sim_row.get("status"),
                            "alpha_id": alpha_id,
                            "sharpe": sim_row.get("sharpe"),
                            "fitness": sim_row.get("fitness"),
                            "turnover": sim_row.get("turnover"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                _write_summary(paths["summary"], paths, args, submitted_successes)
                if args.delay_seconds > 0:
                    time.sleep(args.delay_seconds)
                continue

            _write_progress(paths["progress"], candidate, index, len(candidates), "check_started", alpha_id=alpha_id)
            print(json.dumps({"event": "check_start", "index": index, "alpha_id": alpha_id}, ensure_ascii=False), flush=True)
            check = client.check_alpha_submission(alpha_id, max_polls=args.check_polls, interval=args.check_interval)
            check_row = _check_row(sim_row, check)
            _append_jsonl(paths["check_results"], check_row)
            _write_progress(paths["progress"], candidate, index, len(candidates), check_row.get("api_check_status") or "checked", alpha_id=alpha_id)

            if not _is_ready_to_submit(check_row, args.self_corr_cutoff, submit_pending=args.submit_pending):
                print(
                    json.dumps(
                        {
                            "event": "check_done",
                            "index": index,
                            "alpha_id": alpha_id,
                            "api_check_status": check_row.get("api_check_status"),
                            "sc_value": check_row.get("sc_value"),
                            "prod_value": check_row.get("prod_value"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                _write_summary(paths["summary"], paths, args, submitted_successes)
                if args.delay_seconds > 0:
                    time.sleep(args.delay_seconds)
                continue

            _write_progress(paths["progress"], candidate, index, len(candidates), "submit_started", alpha_id=alpha_id)
            print(json.dumps({"event": "submit_start", "index": index, "alpha_id": alpha_id}, ensure_ascii=False), flush=True)
            submit = client.submit_alpha(alpha_id)
            submit_row = _submit_row(sim_row, check_row, submit)
            _append_jsonl(paths["submit_results"], submit_row)
            tried_alpha_ids.add(alpha_id)
            if submit_row.get("ok") and submit_row.get("final_status") == "ACTIVE":
                submitted_successes.append(submit_row)
            print(
                json.dumps(
                    {
                        "event": "submit_done",
                        "index": index,
                        "alpha_id": alpha_id,
                        "final_status": submit_row.get("final_status"),
                        "ok": submit_row.get("ok"),
                        "active_count": len(submitted_successes),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            _write_progress(paths["progress"], candidate, index, len(candidates), submit_row.get("final_status") or "submitted", alpha_id=alpha_id)
            _write_summary(paths["summary"], paths, args, submitted_successes)

            if args.delay_seconds > 0:
                time.sleep(args.delay_seconds)
    finally:
        client.close()

    summary = _write_summary(paths["summary"], paths, args, submitted_successes)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if len(submitted_successes) >= args.target_successes else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live simulate/check/submit WQ candidates")
    parser.add_argument("--candidate-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--account", default="primary")
    parser.add_argument("--target-successes", type=int, default=10)
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--max-candidates", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--auth-retries", type=int, default=2)
    parser.add_argument("--region", default="USA")
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--decay", type=int, default=8)
    parser.add_argument("--neutralization", default="SUBINDUSTRY")
    parser.add_argument("--truncation", type=float, default=0.08)
    parser.add_argument("--check-polls", type=int, default=4)
    parser.add_argument("--check-interval", type=int, default=10)
    parser.add_argument("--self-corr-cutoff", type=float, default=0.7)
    parser.add_argument("--submit-pending", action="store_true", help="Submit when platform checks pass but correlation review is still pending/missing")
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    return parser.parse_args(argv)


def _load_candidates(path: Path) -> list[dict[str, Any]]:
    rows = []
    for index, raw in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("{"):
            row = json.loads(line)
        else:
            row = {"expression": line}
        if not row.get("expression"):
            continue
        row["source_index"] = index
        row.setdefault("tag", f"live-submit-{index:03d}")
        rows.append(row)
    return rows


def _settings_for_candidate(candidate: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    settings = {
        "region": args.region,
        "universe": args.universe,
        "delay": args.delay,
        "decay": args.decay,
        "neutralization": args.neutralization,
        "truncation": args.truncation,
        "maxTrade": "OFF",
        "maxPosition": "OFF",
    }
    raw = candidate.get("simulation_settings") if isinstance(candidate.get("simulation_settings"), dict) else {}
    for key in ("region", "universe", "neutralization"):
        if raw.get(key) not in (None, ""):
            settings[key] = str(raw[key])
    for key in ("delay", "decay"):
        if raw.get(key) not in (None, ""):
            settings[key] = int(raw[key])
    if raw.get("truncation") not in (None, ""):
        settings["truncation"] = float(raw["truncation"])
    for key in ("maxTrade", "maxPosition"):
        value = str(raw.get(key) or "").upper()
        if value in {"ON", "OFF"}:
            settings[key] = value
    return settings


def _simulation_row(candidate: dict[str, Any], key: str, settings: dict[str, Any], sim: dict[str, Any]) -> dict[str, Any]:
    row = _base_row(candidate, key)
    row["stage"] = "simulation"
    row["simulation_settings_effective"] = settings
    row["result"] = sim
    row["ok"] = bool(sim.get("ok"))
    if not sim.get("ok"):
        row["status"] = "simulation_failed"
        row["error"] = sim.get("error")
        return row
    metrics = sim.get("wq_brain") if isinstance(sim.get("wq_brain"), dict) else {}
    is_metrics = sim.get("is_metrics") if isinstance(sim.get("is_metrics"), dict) else {}
    checks = is_metrics.get("checks") if isinstance(is_metrics.get("checks"), list) else []
    failed_platform = _failed_platform_checks(checks)
    row.update(
        {
            "status": "pending_correlation_check" if sim.get("submit_eligible") and not failed_platform else "simulated",
            "alpha_id": sim.get("alpha_id"),
            "sharpe": _first_float(metrics.get("wq_sharpe"), is_metrics.get("sharpe")),
            "fitness": _first_float(metrics.get("wq_fitness"), is_metrics.get("fitness")),
            "returns": _first_float(metrics.get("wq_returns"), is_metrics.get("returns")),
            "turnover": _first_float(metrics.get("wq_turnover"), is_metrics.get("turnover")),
            "submit_eligible": bool(sim.get("submit_eligible")),
            "submit_checks": sim.get("submit_checks"),
            "is_checks": checks,
            "failed_platform_checks": failed_platform,
            "simulation_id": sim.get("simulation_id"),
        }
    )
    return row


def _check_row(sim_row: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    review = check.get("review_checks") if isinstance(check.get("review_checks"), dict) else {}
    sc = review.get("self_correlation") if isinstance(review.get("self_correlation"), dict) else {}
    prod = review.get("prod_correlation") if isinstance(review.get("prod_correlation"), dict) else {}
    failure = str(check.get("failure_kind") or "")
    failed_platform = _failed_platform_checks(_extract_check_items(check))
    if failed_platform:
        api_status = "platform_check_fail"
    elif failure == "self_correlation":
        api_status = "self_correlation_fail"
    elif failure == "prod_correlation":
        api_status = "prod_correlation_fail"
    elif failure == "correlation_pending":
        api_status = "api_check_pending"
    else:
        api_status = "api_check_readable" if check.get("ok") else "api_check_error"
    return {
        "created_at": _now(),
        "candidate_key": sim_row.get("candidate_key"),
        "source_index": sim_row.get("source_index"),
        "tag": sim_row.get("tag"),
        "expression": sim_row.get("expression"),
        "alpha_id": sim_row.get("alpha_id"),
        "api_check_status": api_status,
        "ok": bool(check.get("ok")) and not failure,
        "failure_kind": failure or None,
        "detail": check.get("detail"),
        "sc_result": sc.get("result"),
        "sc_value": sc.get("value", check.get("sc_value")),
        "sc_limit": sc.get("limit", check.get("sc_limit")),
        "prod_corr_result": prod.get("result"),
        "prod_corr_value": prod.get("value", check.get("prod_value")),
        "prod_corr_limit": prod.get("limit", check.get("prod_limit")),
        "failed_platform_checks": failed_platform,
        "raw_check": check,
    }


def _submit_row(sim_row: dict[str, Any], check_row: dict[str, Any], submit: dict[str, Any]) -> dict[str, Any]:
    final_status = _final_status_from_submit(submit)
    return {
        "created_at": _now(),
        "candidate_key": sim_row.get("candidate_key"),
        "source_index": sim_row.get("source_index"),
        "tag": sim_row.get("tag"),
        "expression": sim_row.get("expression"),
        "alpha_id": sim_row.get("alpha_id"),
        "sharpe": sim_row.get("sharpe"),
        "fitness": sim_row.get("fitness"),
        "turnover": sim_row.get("turnover"),
        "sc_value": check_row.get("sc_value"),
        "prod_corr_value": check_row.get("prod_corr_value"),
        "ok": bool(submit.get("ok")),
        "final_status": final_status,
        "status_code": submit.get("status_code"),
        "platform_status": submit.get("platform_status"),
        "failure_kind": submit.get("failure_kind"),
        "detail": submit.get("detail"),
        "submit_result": submit,
    }


def _should_check(row: dict[str, Any]) -> bool:
    return bool(row.get("alpha_id")) and bool(row.get("submit_eligible")) and not row.get("failed_platform_checks")


def _is_ready_to_submit(check_row: dict[str, Any], cutoff: float, *, submit_pending: bool = False) -> bool:
    if check_row.get("failed_platform_checks"):
        return False
    status = check_row.get("api_check_status")
    if status == "api_check_pending" and submit_pending:
        return True
    if status != "api_check_readable":
        return False
    if str(check_row.get("sc_result") or "").upper() != "PASS":
        return False
    sc_value = _first_float(check_row.get("sc_value"))
    if sc_value is not None and sc_value > cutoff:
        return False
    if str(check_row.get("prod_corr_result") or "").upper() == "FAIL":
        return False
    return True


def _failed_platform_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failed = []
    for check in checks:
        name = str(check.get("name") or "").upper()
        if name in IGNORED_PLATFORM_CHECKS:
            continue
        if str(check.get("result") or "").upper() == "FAIL":
            failed.append(check)
    return failed


def _extract_check_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for container in (
        payload,
        payload.get("raw_check") or {},
        (payload.get("raw_check") or {}).get("is") or {},
        payload.get("is") or {},
    ):
        value = container.get("checks") if isinstance(container, dict) else None
        if isinstance(value, list):
            checks.extend(item for item in value if isinstance(item, dict))
    return checks


def _final_status_from_submit(result: dict[str, Any]) -> str:
    if result.get("ok"):
        return "ACTIVE"
    detail = str(result.get("detail") or "")
    if any(token in detail for token in ("CONCENTRATED_WEIGHT", "LOW_SUB_UNIVERSE_SHARPE", "LOW_SUB_UNIVERSE_FITNESS")):
        return "PLATFORM_CHECK_FAIL"
    failure = str(result.get("failure_kind") or "").lower()
    if failure == "self_correlation":
        return "SC_FAIL"
    if failure == "prod_correlation":
        return "PROD_CORR_FAIL"
    if failure == "correlation_pending":
        return "CORR_PENDING"
    return "OTHER_FAIL"


def _base_row(candidate: dict[str, Any], key: str) -> dict[str, Any]:
    return {
        "created_at": _now(),
        "candidate_key": key,
        "source_index": candidate.get("source_index"),
        "tag": candidate.get("tag"),
        "source_family": candidate.get("source_family"),
        "source": candidate.get("source"),
        "expression": candidate.get("expression"),
        "simulation_settings": candidate.get("simulation_settings") or {},
    }


def _candidate_key(candidate: dict[str, Any]) -> str:
    settings = candidate.get("simulation_settings") if isinstance(candidate.get("simulation_settings"), dict) else {}
    return f"{candidate.get('source_index')}|{candidate.get('expression')}|{json.dumps(settings, sort_keys=True, separators=(',', ':'))}"


def _write_progress(
    path: Path,
    candidate: dict[str, Any],
    index: int,
    total: int,
    status: str,
    *,
    percent: int | None = None,
    message: str | None = None,
    alpha_id: str | None = None,
) -> None:
    payload = {
        "updated_at": _now(),
        "status": status,
        "current_index": index,
        "total": total,
        "source_index": candidate.get("source_index"),
        "tag": candidate.get("tag"),
        "alpha_id": alpha_id,
        "percent": percent,
        "message": message,
        "expression": candidate.get("expression"),
    }
    _write_json(path, payload)


def _write_summary(paths_summary: Path, paths: dict[str, Path], args: argparse.Namespace, submitted_successes: list[dict[str, Any]]) -> dict[str, Any]:
    sim_rows = _read_jsonl(paths["simulation_results"])
    check_rows = _read_jsonl(paths["check_results"])
    submit_rows = _read_jsonl(paths["submit_results"])
    summary = {
        "ok": len(submitted_successes) >= args.target_successes,
        "updated_at": _now(),
        "target_successes": args.target_successes,
        "submitted_successes": len(submitted_successes),
        "active_alpha_ids": [row.get("alpha_id") for row in submitted_successes],
        "simulated": len(sim_rows),
        "checked": len(check_rows),
        "submit_attempts": len(submit_rows),
        "simulation_counts": _counts(row.get("status") for row in sim_rows),
        "check_counts": _counts(row.get("api_check_status") for row in check_rows),
        "submit_counts": _counts(row.get("final_status") for row in submit_rows),
        "files": {name: str(path) for name, path in paths.items()},
    }
    _write_json(paths_summary, summary)
    return summary


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _counts(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        key = str(value or "")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _first_float(*values: Any) -> float | None:
    for value in values:
        parsed = _float_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
