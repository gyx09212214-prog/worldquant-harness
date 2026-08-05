"""Sequentially simulate, check, and submit WQ candidates.

This is a small operational runner for real submissions. It avoids waiting for
a full workflow cycle: each candidate is simulated, checked, and submitted as
soon as self-correlation is confirmed below the strict cutoff.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.artifact_io import append_jsonl, read_jsonl, write_json, write_jsonl
from worldquant_harness.wq_agent_core import (
    EffectiveSettings,
    ExecutionEngine,
    ExecutionPolicy,
    JsonlArtifactObserver,
    RunScope,
    SqlAgentRepository,
    WQBrainPlatformGateway,
)
from worldquant_harness.wq_agent_core.backfill import apply_backfill, build_backfill
from worldquant_harness.wq_agent_core.domain import stable_hash, utc_now
from worldquant_harness.wq_agent_core.engine import correlation_gate
from worldquant_harness.wq_auto_mining import load_dotenv
from worldquant_harness.wq_brain_client import get_client, is_configured
from worldquant_harness.wq_diversity import (
    DiversityPolicy,
    build_diversity_record,
    evaluate_diversity,
    final_diversity_summary,
    load_diversity_ledger,
    merge_diversity_records,
    save_diversity_ledger,
)


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
        "agent_events": output_dir / "agent_events.jsonl",
        "simulation_results": output_dir / "simulation_results.jsonl",
        "check_results": output_dir / "check_results.jsonl",
        "submit_results": output_dir / "submit_results.jsonl",
        "diversity_results": output_dir / "diversity_results.jsonl",
        "summary": output_dir / "summary.json",
    }

    candidates = _load_candidates(_resolve(args.candidate_file))
    candidates = [
        row for row in candidates
        if int(row.get("source_index") or 0) >= args.start_index
    ]
    if args.max_candidates > 0:
        candidates = candidates[: args.max_candidates]

    existing_submit_rows = read_jsonl(paths["submit_results"]) if args.resume else []
    submitted_successes = _active_rows(existing_submit_rows)
    diversity_policy = _diversity_policy(args)
    diversity_accepted, diversity_legacy = _load_diversity_inventories(
        args,
        paths["submit_results"],
        diversity_policy,
    )
    legacy_resume = args.resume and not paths["agent_events"].is_file()
    tried_keys = {
        str(row.get("candidate_key") or "")
        for row in read_jsonl(paths["simulation_results"])
    } if legacy_resume else set()
    run_id = args.run_id or f"live-submit:{output_dir.name}:{stable_hash(str(output_dir.resolve()), length=12)}"
    repository = SqlAgentRepository()
    observer = JsonlArtifactObserver(paths)
    runtime_error: str | None = None

    if legacy_resume:
        legacy_items, legacy_report = build_backfill(
            [output_dir],
            account=args.account,
            user_id=args.user_id,
            include_platform_snapshots=False,
            max_conflict_details=50,
            run_id_override=run_id,
        )
        legacy_report["applied_events"] = apply_backfill(repository, legacy_items)
        legacy_report["dry_run"] = False
        write_json(output_dir / "legacy_resume_import.json", legacy_report)
        write_jsonl(paths["agent_events"], [item.event.to_dict() for item in legacy_items])
        tried_keys.clear()
        print(
            json.dumps(
                {
                    "event": "legacy_resume_imported",
                    "run_id": run_id,
                    "unique_events": legacy_report["unique_events"],
                    "applied_events": legacy_report["applied_events"],
                    "skipped_rows": legacy_report["skipped_rows"],
                    "conflict_count": legacy_report["conflict_count"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    if args.submit_pending:
        print(
            json.dumps(
                {
                    "event": "deprecated_option_ignored",
                    "option": "--submit-pending",
                    "reason": "canonical gate requires explicit SELF PASS and blocks correlation PENDING",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    client = get_client(args.account)
    try:
        print(json.dumps({"event": "auth_start", "account": args.account}, ensure_ascii=False), flush=True)
        if not client.authenticate(_max_retries=args.auth_retries):
            raise RuntimeError("WQ BRAIN authentication failed")
        print(json.dumps({"event": "auth_ok", "account": args.account}, ensure_ascii=False), flush=True)
        engine = ExecutionEngine(WQBrainPlatformGateway(client), repository)

        for index, candidate in enumerate(candidates, start=1):
            if len(submitted_successes) >= args.target_successes:
                break
            key = _candidate_key(candidate)
            if key in tried_keys:
                continue
            candidate = {**candidate, "candidate_key": key}

            if args.diversity_ledger:
                diversity_gate = evaluate_diversity(
                    candidate,
                    diversity_accepted,
                    diversity_legacy,
                    policy=diversity_policy,
                )
                append_jsonl(
                    paths["diversity_results"],
                    {
                        "created_at": _now(),
                        "run_id": run_id,
                        "source_index": candidate.get("source_index"),
                        "tag": candidate.get("tag"),
                        "candidate_key": key,
                        **diversity_gate,
                    },
                )
                if not diversity_gate["ok"]:
                    _write_progress(
                        paths["progress"],
                        candidate,
                        index,
                        len(candidates),
                        "diversity_blocked",
                        message=",".join(diversity_gate["reasons"]),
                    )
                    print(
                        json.dumps(
                            {
                                "event": "diversity_blocked",
                                "run_id": run_id,
                                "index": index,
                                "tag": candidate.get("tag"),
                                "reasons": diversity_gate["reasons"],
                                "max_new_weighted_jaccard": diversity_gate["max_new_weighted_jaccard"],
                                "max_legacy_weighted_jaccard": diversity_gate["max_legacy_weighted_jaccard"],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    _write_summary(paths["summary"], paths, args, submitted_successes, run_id=run_id, runtime_error=runtime_error)
                    continue

            _write_progress(paths["progress"], candidate, index, len(candidates), "simulate_started")
            settings = EffectiveSettings.from_mapping(_settings_for_candidate(candidate, args))
            scope = RunScope.from_settings(settings.to_platform_dict(), user_id=args.user_id)
            policy = ExecutionPolicy(
                self_correlation_cutoff=args.self_corr_cutoff,
                submit_enabled=True,
                check_polls=args.check_polls,
                check_interval=args.check_interval,
                max_submit_retries=args.max_submit_retries,
            )
            print(
                json.dumps(
                    {
                        "event": "candidate_start",
                        "run_id": run_id,
                        "index": index,
                        "source_index": candidate.get("source_index"),
                        "tag": candidate.get("tag"),
                        "source_family": candidate.get("source_family"),
                        "settings": settings.to_platform_dict(),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

            def on_progress(percent: int, message: str, *, candidate: dict[str, Any] = candidate) -> None:
                _write_progress(paths["progress"], candidate, index, len(candidates), "simulate_running", percent=percent, message=message)

            try:
                result = engine.execute(
                    candidate,
                    run_id=run_id,
                    settings=settings,
                    scope=scope,
                    policy=policy,
                    observer=observer,
                    resume=True,
                    progress_callback=on_progress,
                )
            except Exception as exc:
                runtime_error = f"{type(exc).__name__}: {exc}"
                _write_progress(
                    paths["progress"],
                    candidate,
                    index,
                    len(candidates),
                    "execution_interrupted",
                    message=runtime_error,
                )
                print(
                    json.dumps(
                        {
                            "event": "execution_interrupted",
                            "run_id": run_id,
                            "index": index,
                            "tag": candidate.get("tag"),
                            "error": runtime_error,
                            "action": "stopped; rerun with --resume to reconcile before any new submit",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                break

            if result.active and result.alpha_id:
                submitted_successes = _active_rows([
                    *submitted_successes,
                    result.submit_row or {
                        "alpha_id": result.alpha_id,
                        "ok": True,
                        "final_status": "ACTIVE",
                    },
                ])
                if args.diversity_ledger:
                    diversity_record = build_diversity_record(
                        {
                            **candidate,
                            **(result.simulation_row or {}),
                            **(result.check_row or {}),
                            **(result.submit_row or {}),
                        },
                        alpha_id=result.alpha_id,
                        candidate_uid=result.candidate.candidate_uid,
                    )
                    diversity_accepted = merge_diversity_records(diversity_accepted, [diversity_record])
                    save_diversity_ledger(
                        str(_resolve(args.diversity_ledger)),
                        diversity_accepted,
                        policy=diversity_policy,
                    )
            metrics = result.simulation_row or {}
            correlations = result.check_row or {}
            print(
                json.dumps(
                    {
                        "event": "candidate_done",
                        "run_id": run_id,
                        "index": index,
                        "tag": candidate.get("tag"),
                        "candidate_uid": result.candidate.candidate_uid,
                        "attempt_uid": result.attempt_uid,
                        "status": result.status,
                        "resumed": result.resumed,
                        "alpha_id": result.alpha_id,
                        "sharpe": metrics.get("sharpe"),
                        "fitness": metrics.get("fitness"),
                        "returns": metrics.get("returns"),
                        "turnover": metrics.get("turnover"),
                        "sc_result": correlations.get("sc_result"),
                        "sc_value": correlations.get("sc_value"),
                        "prod_corr_result": correlations.get("prod_corr_result"),
                        "prod_corr_value": correlations.get("prod_corr_value"),
                        "failure_reason": result.reason,
                        "active_count": len(submitted_successes),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            _write_progress(
                paths["progress"],
                candidate,
                index,
                len(candidates),
                result.status,
                alpha_id=result.alpha_id,
                message=result.reason,
            )
            _write_summary(paths["summary"], paths, args, submitted_successes, run_id=run_id, runtime_error=runtime_error)

            if args.delay_seconds > 0:
                time.sleep(args.delay_seconds)
    finally:
        client.close()

    summary = _write_summary(
        paths["summary"],
        paths,
        args,
        submitted_successes,
        run_id=run_id,
        runtime_error=runtime_error,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if not runtime_error and len(submitted_successes) >= args.target_successes else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live simulate/check/submit WQ candidates")
    parser.add_argument("--candidate-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--account", default="primary")
    parser.add_argument("--user-id", default=None, help="Optional tenant UUID used to isolate event and memory scope")
    parser.add_argument("--run-id", default=None, help="Stable run identity; defaults to a hash of the output directory")
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
    parser.add_argument("--self-corr-cutoff", type=float, default=0.5, help="Exclusive upper bound: SELF must be strictly below this value")
    parser.add_argument("--submit-pending", action="store_true", help="Submit when platform checks pass but correlation review is still pending/missing")
    parser.add_argument("--max-submit-retries", type=int, default=0, help="Retries after the first submit, only when platform explicitly reports UNSUBMITTED")
    parser.add_argument("--diversity-ledger", default=None, help="Enable hard diversity gates and persist accepted ACTIVE records here")
    parser.add_argument("--diversity-accepted-glob", action="append", default=[], help="Receipt glob(s) used to reconcile this goal's accepted inventory")
    parser.add_argument("--diversity-legacy-glob", action="append", default=[], help="Receipt glob(s) used as the legacy overlap inventory")
    parser.add_argument("--max-new-field-jaccard", type=float, default=0.60)
    parser.add_argument("--max-legacy-field-jaccard", type=float, default=0.75)
    parser.add_argument("--max-core-signature-count", type=int, default=2)
    parser.add_argument("--max-research-field-count", type=int, default=3)
    parser.add_argument("--max-market-helper-field-count", type=int, default=10)
    parser.add_argument("--max-diversity-family-count", type=int, default=4)
    parser.add_argument("--min-final-diversity-families", type=int, default=8)
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
        "account": args.account,
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


def _is_ready_to_submit(check_row: dict[str, Any], cutoff: float, *, submit_pending: bool = False) -> bool:
    if check_row.get("failed_platform_checks"):
        return False
    if check_row.get("api_check_status") == "api_check_pending" and submit_pending:
        return True
    return correlation_gate(check_row, cutoff=cutoff)[0]


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
    write_json(path, payload)


def _write_summary(
    paths_summary: Path,
    paths: dict[str, Path],
    args: argparse.Namespace,
    submitted_successes: list[dict[str, Any]],
    *,
    run_id: str,
    runtime_error: str | None,
) -> dict[str, Any]:
    sim_rows = read_jsonl(paths["simulation_results"])
    check_rows = read_jsonl(paths["check_results"])
    submit_rows = read_jsonl(paths["submit_results"])
    event_rows = read_jsonl(paths["agent_events"])
    diversity_rows = read_jsonl(paths["diversity_results"])
    diversity_accepted = []
    if args.diversity_ledger:
        diversity_accepted = load_diversity_ledger(str(_resolve(args.diversity_ledger)))["accepted"]
    summary = {
        "ok": runtime_error is None and len(submitted_successes) >= args.target_successes,
        "updated_at": _now(),
        "run_id": run_id,
        "runtime_error": runtime_error,
        "target_successes": args.target_successes,
        "submitted_successes": len(submitted_successes),
        "active_alpha_ids": [row.get("alpha_id") for row in submitted_successes],
        "simulated": len(sim_rows),
        "checked": len(check_rows),
        "submit_attempts": len(submit_rows),
        "simulation_counts": _counts(row.get("status") for row in sim_rows),
        "check_counts": _counts(row.get("api_check_status") for row in check_rows),
        "submit_counts": _counts(row.get("final_status") for row in submit_rows),
        "event_counts": _counts(row.get("event_type") for row in event_rows),
        "diversity": {
            "enabled": bool(args.diversity_ledger),
            "policy": _diversity_policy(args).to_dict(),
            "gate_counts": _counts("allowed" if row.get("ok") else "blocked" for row in diversity_rows),
            "block_reason_counts": _counts(
                reason
                for row in diversity_rows
                for reason in (row.get("reasons") or [])
            ),
            "accepted_inventory": final_diversity_summary(diversity_accepted, policy=_diversity_policy(args)),
        },
        "files": {name: str(path) for name, path in paths.items()},
    }
    write_json(paths_summary, summary)
    return summary


def _active_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active_by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        alpha_id = str(row.get("alpha_id") or "").strip()
        if not alpha_id or str(row.get("final_status") or "").upper() != "ACTIVE":
            continue
        active_by_id[alpha_id] = row
    return list(active_by_id.values())


def _diversity_policy(args: argparse.Namespace) -> DiversityPolicy:
    return DiversityPolicy(
        max_new_weighted_jaccard=args.max_new_field_jaccard,
        max_legacy_weighted_jaccard=args.max_legacy_field_jaccard,
        max_core_signature_count=args.max_core_signature_count,
        max_research_field_count=args.max_research_field_count,
        max_market_helper_field_count=args.max_market_helper_field_count,
        max_family_count=args.max_diversity_family_count,
        min_final_family_count=args.min_final_diversity_families,
    )


def _load_diversity_inventories(
    args: argparse.Namespace,
    current_submit_path: Path,
    policy: DiversityPolicy,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not args.diversity_ledger:
        return [], []
    ledger_path = _resolve(args.diversity_ledger)
    accepted = load_diversity_ledger(str(ledger_path))["accepted"]
    accepted_rows = [
        *read_jsonl(current_submit_path),
        *_rows_from_globs(args.diversity_accepted_glob),
    ]
    reconciled = [
        build_diversity_record(row)
        for row in _active_rows(accepted_rows)
        if _strict_self_pass(row.get("sc_value"), args.self_corr_cutoff)
    ]
    accepted = merge_diversity_records(accepted, reconciled)
    save_diversity_ledger(str(ledger_path), accepted, policy=policy)

    accepted_ids = {str(row.get("alpha_id") or "") for row in accepted}
    legacy = [
        build_diversity_record(row)
        for row in _active_rows(_rows_from_globs(args.diversity_legacy_glob))
        if str(row.get("alpha_id") or "") not in accepted_ids
    ]
    return accepted, legacy


def _rows_from_globs(patterns: list[str]) -> list[dict[str, Any]]:
    paths: list[Path] = []
    seen: set[str] = set()
    for pattern in patterns:
        absolute_pattern = str(Path(pattern) if Path(pattern).is_absolute() else ROOT / pattern)
        for value in sorted(glob.glob(absolute_pattern, recursive=True)):
            key = str(Path(value).resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            paths.append(Path(value))
    return [row for path in paths for row in read_jsonl(path)]


def _strict_self_pass(value: Any, cutoff: float) -> bool:
    try:
        return float(value) < float(cutoff)
    except (TypeError, ValueError):
        return False


def _counts(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        key = str(value or "")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _now() -> str:
    return utc_now()


if __name__ == "__main__":
    raise SystemExit(main())
