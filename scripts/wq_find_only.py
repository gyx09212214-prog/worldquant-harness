"""Find WQ BRAIN submit-eligible alphas without submitting them."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.alpha_tracker import compute_similarity
from quantgpt.wq_auto_mining import append_jsonl, load_dotenv, validate_wq_expression, write_json as _write_json
from quantgpt.wq_brain_client import get_client, is_configured
from quantgpt.wq_brain_service import run_single_simulation
from scripts.check_wq_generated_alphas import check_generated_alphas


EXCLUDED_EXPRESSIONS = [
    "rank((high - close) / (high - low) * volume / ts_mean(volume, 20))",
    "rank((high - close) / (high - low) * ts_mean(volume, 5) / ts_mean(volume, 20))",
    "rank((high - close) / (high - low) * rank(volume / ts_mean(volume, 20)))",
]


DIVERSE_CANDIDATES = [
    ("rank(ts_corr(vwap, volume, 20))", "ts-corr-vwap-volume-20"),
    ("rank(ts_corr(vwap, volume, 40))", "ts-corr-vwap-volume-40"),
    ("rank(ts_corr(returns, volume, 20))", "ts-corr-returns-volume-20"),
    ("rank(ts_corr(returns, ts_mean(volume, 5) / ts_mean(volume, 20), 20))", "ts-corr-returns-volume-ratio"),
    ("rank(ts_corr(open / close, volume, 20))", "ts-corr-open-close-volume"),
    ("rank(ts_corr(high / low, volume, 20))", "ts-corr-high-low-volume"),
    ("rank(ts_rank(returns, 40))", "ts-rank-returns-40"),
    ("rank(ts_rank(vwap / close, 30))", "ts-rank-vwap-close-30"),
    ("rank(ts_rank(volume / ts_mean(volume, 20), 30)) * rank(-1 * ts_std_dev(returns, 20))", "ts-rank-volume-vol-interaction"),
    ("rank(ts_sum(log(close / vwap), 10))", "ts-sum-log-close-vwap-10"),
    ("rank(ts_sum(log(volume / ts_mean(volume, 20)), 10))", "ts-sum-log-volume-ratio-10"),
    ("rank(ts_sum(log(vwap / ts_shift(vwap, 1)), 20))", "ts-sum-log-vwap-shift-20"),
    ("group_rank(rank(ts_corr(vwap, volume, 20)), IndClass.industry)", "group-rank-ts-corr-vwap-volume"),
    ("group_zscore(rank(ts_rank(returns, 40)), IndClass.industry)", "group-zscore-ts-rank-returns"),
    ("group_neutralize(rank(ts_corr(returns, volume, 20)), IndClass.industry)", "group-neutralize-ts-corr-returns-volume"),
    ("rank(ts_corr(vwap, volume, 20)) * rank(-1 * ts_std_dev(returns, 20))", "ts-corr-volatility-interaction"),
    ("rank(ts_rank(returns, 40)) * rank(volume / ts_mean(volume, 20))", "ts-rank-returns-volume-interaction"),
    ("rank(ts_sum(log(close / vwap), 10)) * rank(ts_corr(returns, volume, 20))", "log-vwap-ts-corr-interaction"),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find submit-eligible WQ alphas without submitting")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--candidates", default="")
    parser.add_argument("--exclude-expressions", default="")
    parser.add_argument("--hits-file", default="")
    parser.add_argument("--stop-file", default="")
    parser.add_argument("--region", default="USA")
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--decay", type=int, default=6)
    parser.add_argument("--neutralization", default="SUBINDUSTRY")
    parser.add_argument("--truncation", type=float, default=0.08)
    parser.add_argument("--account", default="primary")
    parser.add_argument("--max-runs", type=int, default=18)
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--target-eligible", type=int, default=1)
    parser.add_argument("--similarity-threshold", type=float, default=0.82)
    parser.add_argument("--hit-similarity-threshold", type=float, default=0.82)
    parser.add_argument("--api-check-after-run", action="store_true")
    parser.add_argument("--api-check-delay-seconds", type=int, default=0)
    parser.add_argument("--api-check-all", action="store_true")
    parser.add_argument("--use-ledger", action="store_true", default=os.environ.get("WQ_LEDGER_ENABLED") == "1")
    parser.add_argument("--ledger-similarity-threshold", type=float, default=0.70)
    args = parser.parse_args(argv)
    print(
        "INFO: scripts/wq_find_only.py is the low-level find-only/check-only worker; "
        "it never submits alphas.",
        file=sys.stderr,
    )

    load_dotenv(ROOT)
    output_dir = Path(args.output_dir) if args.output_dir else ROOT / "reports" / f"wq_find_only_{datetime.now():%Y%m%d_%H%M%S}"
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    results_file = output_dir / "results.jsonl"
    status_file = output_dir / "status.json"
    hit_file = output_dir / "hit.json"
    hits_file = _resolve_path(args.hits_file, output_dir / "hits.jsonl")
    stop_file = _resolve_path(args.stop_file, output_dir / "STOP")
    target_eligible = max(1, args.target_eligible)

    candidates = _load_candidates(args.candidates)
    if not is_configured(args.account):
        write_json(status_file, {"status": "FAILED", "reason": "WQ credentials are not configured"})
        return 2

    client = get_client(args.account)
    counters = {"processed": 0, "completed": 0, "failed": 0, "eligible": 0, "skipped": 0}
    best: dict[str, Any] | None = None
    best_hit: dict[str, Any] | None = None
    excluded_expressions = [*EXCLUDED_EXPRESSIONS, *_load_expressions(args.exclude_expressions)]
    hit_expressions: list[str] = []
    settings = {
        "account": args.account,
        "region": args.region,
        "universe": args.universe,
        "delay": args.delay,
        "decay": args.decay,
        "neutralization": args.neutralization,
        "truncation": args.truncation,
    }
    if args.use_ledger:
        try:
            from quantgpt.wq_alpha_ledger import build_exclusion_expressions_sync

            excluded_expressions.extend(build_exclusion_expressions_sync())
        except Exception:
            pass

    try:
        write_json(status_file, {
            "status": "AUTHENTICATING",
            "output_dir": str(output_dir),
            "results_file": str(results_file),
            "hits_file": str(hits_file),
            "hit_file": str(hit_file),
            "stop_file": str(stop_file),
            "target_eligible": target_eligible,
            "similarity_threshold": args.similarity_threshold,
            "hit_similarity_threshold": args.hit_similarity_threshold,
            "use_ledger": args.use_ledger,
            "ledger_similarity_threshold": args.ledger_similarity_threshold,
            "excluded_count": len(excluded_expressions),
            "counters": counters,
        })
        if not client.authenticate():
            write_json(status_file, {"status": "FAILED", "reason": "WQ authentication failed", "counters": counters})
            return 3

        start_index = max(1, args.start_index)
        selected_candidates = candidates[start_index - 1 : start_index - 1 + args.max_runs]
        for index, candidate in enumerate(selected_candidates, start=start_index):
            if stop_file.exists():
                payload = {
                    "status": "STOPPED",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "reason": "stop_file_detected",
                    "counters": counters,
                    "best": best,
                    "hit": best_hit,
                    "output_dir": str(output_dir),
                    "target_eligible": target_eligible,
                }
                _write_terminal_status(args, status_file, payload, results_file, output_dir)
                return 0

            expression = candidate["expression"]
            ledger_block = _ledger_block(expression, settings, args) if args.use_ledger else {"blocked": False}
            if ledger_block.get("blocked"):
                counters["skipped"] += 1
                entry = {
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "status": "skipped_similar",
                    "expression": expression,
                    "tag": candidate.get("tag"),
                    "candidate_meta": _candidate_meta(candidate),
                    "ledger_block": ledger_block,
                    "similarity": (ledger_block.get("nearest") or {}).get("similarity"),
                }
                append_jsonl(results_file, entry)
                _record_ledger_entry(entry, settings, output_dir, results_file, args)
                _write_skip_status(
                    status_file,
                    output_dir,
                    results_file,
                    hits_file,
                    hit_file,
                    stop_file,
                    target_eligible,
                    index,
                    entry,
                    counters,
                    best,
                    best_hit,
                )
                continue

            similarity = _max_similarity(expression, excluded_expressions)
            hit_similarity = _max_similarity(expression, hit_expressions)
            if similarity["overall_similarity"] >= args.similarity_threshold:
                counters["skipped"] += 1
                entry = {
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "status": "skipped_similar",
                    "expression": expression,
                    "tag": candidate.get("tag"),
                    "candidate_meta": _candidate_meta(candidate),
                    "similarity": similarity,
                }
                append_jsonl(results_file, entry)
                _record_ledger_entry(entry, settings, output_dir, results_file, args)
                _write_skip_status(
                    status_file,
                    output_dir,
                    results_file,
                    hits_file,
                    hit_file,
                    stop_file,
                    target_eligible,
                    index,
                    entry,
                    counters,
                    best,
                    best_hit,
                )
                continue
            if hit_similarity["overall_similarity"] >= args.hit_similarity_threshold:
                counters["skipped"] += 1
                entry = {
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "status": "skipped_similar_to_hit",
                    "expression": expression,
                    "tag": candidate.get("tag"),
                    "candidate_meta": _candidate_meta(candidate),
                    "similarity_to_hit": hit_similarity,
                }
                append_jsonl(results_file, entry)
                _record_ledger_entry(entry, settings, output_dir, results_file, args)
                _write_skip_status(
                    status_file,
                    output_dir,
                    results_file,
                    hits_file,
                    hit_file,
                    stop_file,
                    target_eligible,
                    index,
                    entry,
                    counters,
                    best,
                    best_hit,
                )
                continue

            try:
                validate_wq_expression(expression)
            except Exception as exc:
                counters["failed"] += 1
                entry = {
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "status": "failed_validation",
                    "expression": expression,
                    "tag": candidate.get("tag"),
                    "candidate_meta": _candidate_meta(candidate),
                    "error": str(exc),
                }
                append_jsonl(results_file, entry)
                _record_ledger_entry(entry, settings, output_dir, results_file, args)
                continue

            def on_progress(progress: int, message: str) -> None:
                write_json(status_file, {
                    "status": "RUNNING",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "current_index": index,
                    "current_expression": expression,
                    "current_progress": progress,
                    "message": message,
                    "counters": counters,
                    "best": best,
                    "hit": best_hit,
                    "output_dir": str(output_dir),
                    "results_file": str(results_file),
                    "hits_file": str(hits_file),
                    "hit_file": str(hit_file),
                    "stop_file": str(stop_file),
                    "target_eligible": target_eligible,
                })

            counters["processed"] += 1
            on_progress(0, "starting simulation")
            result = run_single_simulation(
                client,
                expression,
                region=args.region,
                universe=args.universe,
                delay=args.delay,
                decay=args.decay,
                neutralization=args.neutralization,
                truncation=args.truncation,
                auto_submit=False,
                tag=candidate.get("tag"),
                progress_callback=on_progress,
            )

            entry = _entry(candidate, result, similarity, hit_similarity)
            if result.get("submitted"):
                counters["failed"] += 1
                entry["status"] = "failed_submission_guard"
                append_jsonl(results_file, entry)
                _record_ledger_entry(entry, settings, output_dir, results_file, args)
                write_json(status_file, {
                    "status": "FAILED_SUBMISSION_GUARD",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "reason": "simulation result unexpectedly reported submitted=true",
                    "counters": counters,
                    "failed_entry": entry,
                    "best": best,
                    "hit": best_hit,
                    "output_dir": str(output_dir),
                    "target_eligible": target_eligible,
                })
                return 4

            if result.get("ok"):
                counters["completed"] += 1
                best = _best(best, entry)
                if result.get("submit_eligible") and _failed_platform_checks(entry):
                    counters["failed"] += 1
                    entry["status"] = "failed_platform_check"
                    entry["failed_platform_checks"] = _failed_platform_checks(entry)
                elif result.get("submit_eligible") and _has_failed_correlation(entry):
                    counters["failed"] += 1
                    entry["status"] = "failed_correlation_check"
                elif result.get("submit_eligible") and _has_pending_correlation(entry):
                    entry["status"] = "pending_correlation_check"
                elif result.get("submit_eligible"):
                    counters["eligible"] += 1
                    entry["status"] = "eligible"
                    append_jsonl(results_file, entry)
                    append_jsonl(hits_file, entry)
                    _record_ledger_entry(entry, settings, output_dir, results_file, args)
                    best_hit = _best(best_hit, entry)
                    hit_expressions.append(expression)
                    excluded_expressions.append(expression)
                    write_json(hit_file, best_hit)
                    status = "FOUND" if counters["eligible"] >= target_eligible else "RUNNING"
                    reason = "target_eligible_reached" if counters["eligible"] >= target_eligible else "eligible_collected_without_submit"
                    payload = {
                        "status": status,
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                        "reason": reason,
                        "counters": counters,
                        "hit": best_hit,
                        "latest_hit": entry,
                        "best": best,
                        "output_dir": str(output_dir),
                        "results_file": str(results_file),
                        "hits_file": str(hits_file),
                        "hit_file": str(hit_file),
                        "stop_file": str(stop_file),
                        "target_eligible": target_eligible,
                    }
                    if status == "FOUND":
                        _write_terminal_status(args, status_file, payload, results_file, output_dir)
                    else:
                        write_json(status_file, payload)
                    print(json.dumps(entry, ensure_ascii=False, default=str), flush=True)
                    if counters["eligible"] >= target_eligible:
                        return 0
                    continue
            else:
                counters["failed"] += 1
            append_jsonl(results_file, entry)
            _record_ledger_entry(entry, settings, output_dir, results_file, args)

        final_status = "PARTIAL_FOUND" if counters["eligible"] > 0 else "NOT_FOUND"
        payload = {
            "status": final_status,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "reason": "candidate_pool_exhausted",
            "counters": counters,
            "hit": best_hit,
            "best": best,
            "output_dir": str(output_dir),
            "results_file": str(results_file),
            "hits_file": str(hits_file),
            "hit_file": str(hit_file),
            "stop_file": str(stop_file),
            "target_eligible": target_eligible,
        }
        _write_terminal_status(args, status_file, payload, results_file, output_dir)
        return 1
    finally:
        client.close()


def write_json(path: Path, payload: dict) -> None:
    if path.name == "status.json" and isinstance(payload, dict):
        payload = {
            "schema_version": 1,
            "canonical_entrypoint": "scripts/wq_daily_mining.py",
            "legacy_entrypoint": "scripts/wq_find_only.py",
            "status_reader": "scripts/wq_status.py --kind find-only",
            "submit_guard": "find-only/check-only; no submit endpoint is called",
            "authoritative_status_file": str(path),
            **payload,
        }
    _write_json(path, payload)


def _load_candidates(path_value: str) -> list[dict]:
    if not path_value:
        return [{"expression": expression, "tag": tag} for expression, tag in DIVERSE_CANDIDATES]
    return _load_jsonl_values(path_value)


def _candidate_meta(candidate: dict) -> dict:
    return {
        key: value
        for key, value in candidate.items()
        if key not in {"expression", "tag"} and value is not None
    }


def _load_expressions(path_value: str) -> list[str]:
    if not path_value:
        return []
    return [row["expression"] for row in _load_jsonl_values(path_value)]


def _load_jsonl_values(path_value: str) -> list[dict]:
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT / path
    rows: list[dict] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("{") or line.startswith('"'):
            value = json.loads(line)
        else:
            value = {"expression": line}
        if isinstance(value, str):
            value = {"expression": value}
        rows.append(value)
    return rows


def _resolve_path(path_value: str, default: Path) -> Path:
    if not path_value:
        return default
    path = Path(path_value)
    return path if path.is_absolute() else ROOT / path


def _max_similarity(expression: str, existing_expressions: list[str]) -> dict:
    if not existing_expressions:
        return {"text_similarity": 0.0, "operator_overlap": 0.0, "field_overlap": 0.0, "overall_similarity": 0.0}
    scores = [compute_similarity(expression, existing) for existing in existing_expressions]
    return max(scores, key=lambda item: item["overall_similarity"])


def _entry(candidate: dict, result: dict, similarity: dict, hit_similarity: dict) -> dict:
    metrics = result.get("wq_brain", {}) if result.get("ok") else {}
    is_checks = (result.get("is_metrics") or {}).get("checks") or []
    candidate_meta = {
        key: value
        for key, value in candidate.items()
        if key not in {"expression", "tag"} and value is not None
    }
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "simulated" if result.get("ok") else "failed",
        "expression": candidate["expression"],
        "tag": candidate.get("tag"),
        "candidate_meta": candidate_meta,
        "alpha_id": result.get("alpha_id"),
        "sharpe": metrics.get("wq_sharpe"),
        "fitness": metrics.get("wq_fitness"),
        "returns": metrics.get("wq_returns"),
        "turnover": metrics.get("wq_turnover"),
        "submit_eligible": result.get("submit_eligible"),
        "submitted": result.get("submitted"),
        "submit_checks": result.get("submit_checks"),
        "self_correlation": _find_check(is_checks, "SELF_CORRELATION"),
        "prod_correlation": _find_check(is_checks, "PROD_CORRELATION"),
        "is_checks": is_checks,
        "similarity_to_blocked": similarity,
        "similarity_to_hits": hit_similarity,
        "result": result,
    }


def _find_check(checks: list[dict], name: str) -> dict | None:
    for check in checks:
        if check.get("name") == name:
            return check
    return None


def _has_failed_correlation(entry: dict) -> bool:
    return _check_result(entry.get("self_correlation")) == "FAIL" or _check_result(entry.get("prod_correlation")) == "FAIL"


def _has_pending_correlation(entry: dict) -> bool:
    return _check_result(entry.get("self_correlation")) == "PENDING" or _check_result(entry.get("prod_correlation")) == "PENDING"


def _failed_platform_checks(entry: dict) -> list[dict]:
    correlation_checks = {"SELF_CORRELATION", "PROD_CORRELATION"}
    return [
        check for check in entry.get("is_checks", [])
        if str(check.get("name") or "").upper() not in correlation_checks
        and _check_result(check) == "FAIL"
    ]


def _check_result(check: dict | None) -> str:
    return str((check or {}).get("result") or "").upper()


def _write_skip_status(
    status_file: Path,
    output_dir: Path,
    results_file: Path,
    hits_file: Path,
    hit_file: Path,
    stop_file: Path,
    target_eligible: int,
    index: int,
    entry: dict,
    counters: dict,
    best: dict | None,
    best_hit: dict | None,
) -> None:
    write_json(status_file, {
        "status": "RUNNING",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "current_index": index,
        "current_expression": entry["expression"],
        "message": entry["status"],
        "counters": counters,
        "best": best,
        "hit": best_hit,
        "latest_skip": entry,
        "output_dir": str(output_dir),
        "results_file": str(results_file),
        "hits_file": str(hits_file),
        "hit_file": str(hit_file),
        "stop_file": str(stop_file),
        "target_eligible": target_eligible,
    })


def _write_terminal_status(args: argparse.Namespace, status_file: Path, payload: dict, results_file: Path, output_dir: Path) -> None:
    if args.api_check_after_run:
        api_results_file = output_dir / "api_check.jsonl"
        api_summary_file = output_dir / "api_check_summary.json"
        try:
            summary = check_generated_alphas(
                input_paths=[results_file],
                output_path=api_results_file,
                summary_output_path=api_summary_file,
                account=args.account,
                include_all=args.api_check_all,
                delay_seconds=max(0, args.api_check_delay_seconds),
                record_ledger=args.use_ledger,
            )
            payload["api_check"] = {
                "ok": True,
                "results_file": str(api_results_file),
                "summary_file": str(api_summary_file),
                "summary": summary,
            }
        except Exception as exc:
            payload["api_check"] = {
                "ok": False,
                "error": str(exc),
                "results_file": str(api_results_file),
                "summary_file": str(api_summary_file),
            }
    write_json(status_file, payload)


def _ledger_block(expression: str, settings: dict, args: argparse.Namespace) -> dict:
    try:
        from quantgpt.wq_alpha_ledger import should_block_expression_sync

        return should_block_expression_sync(
            expression,
            settings=settings,
            threshold=args.ledger_similarity_threshold,
        )
    except Exception as exc:
        return {"blocked": False, "error": str(exc)}


def _record_ledger_entry(
    entry: dict,
    settings: dict,
    output_dir: Path,
    results_file: Path,
    args: argparse.Namespace,
) -> None:
    if not args.use_ledger:
        return
    try:
        from quantgpt.wq_alpha_ledger import record_find_only_entry_sync

        record_find_only_entry_sync(
            entry,
            settings=settings,
            source_run_id=output_dir.name,
            source_file=str(results_file),
            source_type="find_only",
        )
    except Exception:
        pass


def _best(existing: dict | None, candidate: dict) -> dict:
    if existing is None:
        return candidate
    old_key = (_score(existing.get("fitness")), _score(existing.get("sharpe")))
    new_key = (_score(candidate.get("fitness")), _score(candidate.get("sharpe")))
    return candidate if new_key > old_key else existing


def _score(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("-inf")


if __name__ == "__main__":
    raise SystemExit(main())
