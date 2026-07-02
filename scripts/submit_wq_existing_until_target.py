"""Submit existing WQ alpha IDs until a target number of ACTIVE results is reached."""

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

from worldquant_harness.wq_auto_mining import load_dotenv
from worldquant_harness.wq_brain_client import get_client, is_configured
from worldquant_harness.wq_post_submit_review import WQPostSubmitReviewConfig, build_post_submit_review

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
    args = _parse_args(argv)
    if args.check_only:
        args.check_before_submit = True
    load_dotenv(ROOT)
    output_dir = _resolve(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results_path = output_dir / "submit_existing_results.jsonl"
    summary_path = output_dir / "submit_existing_summary.json"

    if not is_configured(args.account):
        print(json.dumps({"ok": False, "error": f"WQ credentials not configured for {args.account}"}), file=sys.stderr)
        return 2

    candidates = load_candidates(_resolve(args.candidate_file), [_resolve(path) for path in args.check_files])
    if args.limit_candidates > 0:
        candidates = candidates[: args.limit_candidates]
    existing = read_jsonl(results_path) if args.resume else []
    tried = {str(row.get("alpha_id") or "") for row in existing if row.get("alpha_id")}
    active_count = sum(1 for row in existing if row.get("ok") and str(row.get("final_status") or "").upper() == "ACTIVE")
    attempts = 0

    client = get_client(args.account)
    try:
        print(json.dumps({"event": "auth_start", "account": args.account}, ensure_ascii=False), flush=True)
        if not client.authenticate(_max_retries=args.auth_retries):
            raise RuntimeError("WQ BRAIN authentication failed")
        print(json.dumps({"event": "auth_ok", "account": args.account}, ensure_ascii=False), flush=True)

        for candidate in candidates:
            alpha_id = str(candidate.get("alpha_id") or "")
            if not alpha_id or alpha_id in tried:
                continue
            if active_count >= args.target:
                break
            if args.max_attempts > 0 and attempts >= args.max_attempts:
                break
            attempts += 1
            tried.add(alpha_id)

            row = {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "alpha_id": alpha_id,
                "candidate_rank": candidate.get("rank"),
                "domain": candidate.get("domain"),
                "expression": candidate.get("expression"),
                "candidate_metrics": {
                    "sharpe": candidate.get("sharpe"),
                    "fitness": candidate.get("fitness"),
                    "returns": candidate.get("returns"),
                    "turnover": candidate.get("turnover"),
                    "max_active_similarity": candidate.get("max_active_similarity"),
                },
                "precheck": candidate.get("precheck") or {},
            }

            precheck = candidate.get("precheck") or {}
            if is_precheck_blocked(precheck):
                row.update({
                    "ok": False,
                    "final_status": "PRECHECK_BLOCKED",
                    "failure_kind": precheck.get("review_failure_kind") or precheck.get("failure_kind"),
                    "detail": precheck.get("detail") or "precheck blocked",
                })
                append_jsonl(results_path, row)
                existing.append(row)
                write_summary(summary_path, existing, target=args.target, attempts=attempts, candidate_count=len(candidates))
                print(json.dumps({"alpha_id": alpha_id, "status": row["final_status"], "active_count": active_count}, ensure_ascii=False), flush=True)
                continue

            if args.check_before_submit and should_run_live_check(precheck):
                print(json.dumps({"event": "live_check_start", "alpha_id": alpha_id}, ensure_ascii=False), flush=True)
                check = client.check_alpha_submission(alpha_id, max_polls=args.check_polls, interval=args.check_interval)
                row["live_precheck"] = check
                if is_already_submitted(check):
                    row.update({
                        "ok": False,
                        "final_status": "ALREADY_SUBMITTED",
                        "failure_kind": "already_submitted",
                        "detail": "live precheck reported ALREADY_SUBMITTED",
                    })
                    append_jsonl(results_path, row)
                    existing.append(row)
                    write_summary(summary_path, existing, target=args.target, attempts=attempts, candidate_count=len(candidates))
                    print(json.dumps({"alpha_id": alpha_id, "status": row["final_status"], "active_count": active_count}, ensure_ascii=False), flush=True)
                    continue
                if is_live_check_blocked(check):
                    failed_checks = platform_failed_checks(check)
                    failure_kind = check.get("failure_kind") or ("platform_check_fail" if failed_checks else None)
                    detail = check.get("detail") or "live precheck blocked"
                    if failed_checks:
                        detail = "live precheck failed: " + ", ".join(failed_checks)
                    row.update({
                        "ok": False,
                        "final_status": "PRECHECK_BLOCKED",
                        "failure_kind": failure_kind,
                        "detail": detail,
                    })
                    append_jsonl(results_path, row)
                    existing.append(row)
                    write_summary(summary_path, existing, target=args.target, attempts=attempts, candidate_count=len(candidates))
                    print(json.dumps({"alpha_id": alpha_id, "status": row["final_status"], "active_count": active_count}, ensure_ascii=False), flush=True)
                    continue
                if check.get("failure_kind") == "correlation_pending" and not args.allow_pending:
                    row.update({
                        "ok": False,
                        "final_status": "PRECHECK_PENDING",
                        "failure_kind": "correlation_pending",
                        "detail": check.get("detail") or "live precheck pending",
                    })
                    append_jsonl(results_path, row)
                    existing.append(row)
                    write_summary(summary_path, existing, target=args.target, attempts=attempts, candidate_count=len(candidates))
                    print(json.dumps({"alpha_id": alpha_id, "status": row["final_status"], "active_count": active_count}, ensure_ascii=False), flush=True)
                    continue

            if args.check_only:
                row.update({
                    "ok": True,
                    "final_status": "PRECHECK_PASS",
                    "failure_kind": None,
                    "detail": "precheck passed; check-only mode skipped submit",
                })
                append_jsonl(results_path, row)
                existing.append(row)
                write_summary(summary_path, existing, target=args.target, attempts=attempts, candidate_count=len(candidates))
                print(json.dumps({"alpha_id": alpha_id, "status": row["final_status"], "active_count": active_count}, ensure_ascii=False), flush=True)
                continue

            print(json.dumps({"event": "submit_start", "alpha_id": alpha_id}, ensure_ascii=False), flush=True)
            result = client.submit_alpha(alpha_id)
            final_status = final_status_from_submit(result)
            row.update({
                "ok": bool(result.get("ok")),
                "final_status": final_status,
                "status_code": result.get("status_code"),
                "platform_status": result.get("platform_status"),
                "failure_kind": result.get("failure_kind"),
                "detail": result.get("detail"),
                "review_checks": result.get("review_checks"),
                "sc_value": result.get("sc_value"),
                "sc_limit": result.get("sc_limit"),
                "prod_value": result.get("prod_value"),
                "prod_limit": result.get("prod_limit"),
            })
            if row["ok"] and final_status == "ACTIVE":
                active_count += 1
            append_jsonl(results_path, row)
            existing.append(row)
            write_summary(summary_path, existing, target=args.target, attempts=attempts, candidate_count=len(candidates))
            print(json.dumps({
                "alpha_id": alpha_id,
                "status": final_status,
                "ok": row["ok"],
                "active_count": active_count,
                "target": args.target,
            }, ensure_ascii=False), flush=True)

            if active_count >= args.target:
                break
            if args.delay_seconds > 0:
                time.sleep(args.delay_seconds)
    finally:
        client.close()

    final_rows = read_jsonl(results_path)
    summary = write_summary(summary_path, final_rows, target=args.target, attempts=attempts, candidate_count=len(candidates))
    if args.check_only:
        summary["ok"] = True
        summary["check_only"] = True
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    elif not args.no_post_submit_review:
        review = run_post_submit_review(args, output_dir)
        summary["post_submit_review"] = review
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if args.check_only or summary.get("active", 0) >= args.target else 1


def load_candidates(candidate_file: Path, check_files: list[Path]) -> list[dict[str, Any]]:
    candidates = read_jsonl(candidate_file)
    checks: dict[str, dict[str, Any]] = {}
    for path in check_files:
        for row in read_jsonl(path):
            alpha_id = str(row.get("alpha_id") or "")
            if alpha_id:
                checks[alpha_id] = row
    for candidate in candidates:
        alpha_id = str(candidate.get("alpha_id") or "")
        if alpha_id in checks:
            candidate["precheck"] = checks[alpha_id]
    candidates.sort(key=candidate_sort_key)
    return candidates


def candidate_sort_key(row: dict[str, Any]) -> tuple:
    precheck = row.get("precheck") or {}
    status = str(precheck.get("api_check_status") or "")
    blocked = is_precheck_blocked(precheck)
    pending_priority = 0 if status == "api_check_pending" else 1
    score = safe_float(row.get("score"))
    has_score = row.get("score") is not None
    domain_priority = {
        "options_vol_pcr": 0,
        "sentiment_revision": 1,
        "sales_forward": 2,
        "eps_analyst": 3,
        "missingness_coverage": 4,
        "cashflow_noncap": 5,
        "intraday_micro": 6,
        "risk_credit": 7,
        "other": 8,
        "cashflow_cap_crowded": 9,
    }.get(str(row.get("domain") or ""), 8)
    return (
        blocked,
        pending_priority,
        float(row.get("max_active_similarity") or 0) >= 0.85,
        float(row.get("max_active_similarity") or 0) >= 0.75,
        not has_score,
        -score,
        domain_priority,
        safe_float(row.get("rank"), default=999999.0),
        -safe_float(row.get("fitness")),
        -safe_float(row.get("sharpe")),
        safe_float(row.get("turnover"), default=999.0),
    )


def is_precheck_blocked(precheck: dict[str, Any]) -> bool:
    status = str(precheck.get("api_check_status") or "").lower()
    failure = str(precheck.get("review_failure_kind") or precheck.get("failure_kind") or "").lower()
    return (
        status in {"self_correlation_fail", "prod_correlation_fail"}
        or failure in {"self_correlation", "prod_correlation", "platform_check_fail"}
        or bool(platform_failed_checks(precheck))
    )


def should_run_live_check(precheck: dict[str, Any]) -> bool:
    if not precheck:
        return True
    if is_precheck_blocked(precheck):
        return False
    status = str(precheck.get("api_check_status") or "").lower()
    return status not in {"api_check_readable", "platform_active_check_readable"}


def is_live_check_blocked(check: dict[str, Any]) -> bool:
    failure = str(check.get("failure_kind") or "").lower()
    return failure in {"self_correlation", "prod_correlation"} or bool(platform_failed_checks(check))


def platform_failed_checks(payload: dict[str, Any]) -> list[str]:
    checks = extract_check_items(payload)
    failed: list[str] = []
    for item in checks:
        name = str(item.get("name") or "").upper()
        result = str(item.get("result") or "").upper()
        if name in BLOCKING_PLATFORM_CHECKS and result == "FAIL":
            failed.append(name)
    return sorted(set(failed))


def is_already_submitted(payload: dict[str, Any]) -> bool:
    checks = extract_check_items(payload)
    return any(
        str(item.get("name") or "").upper() == "ALREADY_SUBMITTED"
        and str(item.get("result") or "").upper() == "FAIL"
        for item in checks
        if isinstance(item, dict)
    )


def extract_check_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for container in (
        payload,
        payload.get("raw_check") or {},
        (payload.get("raw_check") or {}).get("is") or {},
        payload.get("is") or {},
        (payload.get("live_precheck") or {}) if isinstance(payload.get("live_precheck"), dict) else {},
    ):
        value = container.get("checks") if isinstance(container, dict) else None
        if isinstance(value, list):
            checks.extend(item for item in value if isinstance(item, dict))
    return checks


def final_status_from_submit(result: dict[str, Any]) -> str:
    if result.get("ok"):
        return "ACTIVE"
    detail = str(result.get("detail") or "")
    if "ALREADY_SUBMITTED" in detail:
        return "ALREADY_SUBMITTED"
    if any(token in detail for token in ("CONCENTRATED_WEIGHT", "LOW_SUB_UNIVERSE_SHARPE", "LOW_SUB_UNIVERSE_FITNESS")):
        return "PLATFORM_CHECK_FAIL"
    failure = str(result.get("failure_kind") or "").lower()
    if failure == "self_correlation":
        return "SC_FAIL"
    if failure == "prod_correlation":
        return "PROD_CORR_FAIL"
    if str(result.get("platform_status") or "").upper() == "TIMEOUT":
        return "CORR_PENDING"
    return "OTHER_FAIL"


def write_summary(path: Path, rows: list[dict[str, Any]], *, target: int, attempts: int, candidate_count: int) -> dict[str, Any]:
    summary = {
        "ok": True,
        "target": target,
        "candidate_count": candidate_count,
        "attempts_this_run": attempts,
        "total_records": len(rows),
        "active": sum(1 for row in rows if row.get("ok") and row.get("final_status") == "ACTIVE"),
        "sc_fail": sum(1 for row in rows if row.get("final_status") == "SC_FAIL"),
        "prod_corr_fail": sum(1 for row in rows if row.get("final_status") == "PROD_CORR_FAIL"),
        "precheck_blocked": sum(1 for row in rows if row.get("final_status") == "PRECHECK_BLOCKED"),
        "precheck_pass": sum(1 for row in rows if row.get("final_status") == "PRECHECK_PASS"),
        "corr_pending": sum(1 for row in rows if row.get("final_status") in {"CORR_PENDING", "PRECHECK_PENDING"}),
        "platform_check_fail": sum(1 for row in rows if row.get("final_status") == "PLATFORM_CHECK_FAIL"),
        "already_submitted": sum(1 for row in rows if row.get("final_status") == "ALREADY_SUBMITTED"),
        "other_fail": sum(1 for row in rows if row.get("final_status") == "OTHER_FAIL"),
        "active_alpha_ids": [row.get("alpha_id") for row in rows if row.get("ok") and row.get("final_status") == "ACTIVE"],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary


def run_post_submit_review(args: argparse.Namespace, output_dir: Path) -> dict[str, Any]:
    try:
        return build_post_submit_review(WQPostSubmitReviewConfig(
            run_dirs=(output_dir,),
            output_dir=output_dir / "post_submit_review",
            baseline_roots=tuple(_resolve(path) for path in args.post_submit_baseline_roots),
            profile_dir=_resolve(args.post_submit_profile_dir) if args.post_submit_profile_dir else None,
            window_days=max(1, args.post_submit_window_days),
        ))
    except Exception as exc:
        return {"ok": False, "error": str(exc), "output_dir": str(output_dir / "post_submit_review")}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def safe_float(value: Any, *, default: float = -999.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Submit existing WQ alpha IDs until target ACTIVE count")
    parser.add_argument("--candidate-file", required=True)
    parser.add_argument("--check-files", nargs="*", default=[])
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--account", default="primary")
    parser.add_argument("--target", type=int, default=10)
    parser.add_argument("--max-attempts", type=int, default=80)
    parser.add_argument("--limit-candidates", type=int, default=0)
    parser.add_argument("--delay-seconds", type=float, default=5.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--auth-retries", type=int, default=2)
    parser.add_argument("--check-before-submit", action="store_true")
    parser.add_argument("--check-polls", type=int, default=3)
    parser.add_argument("--check-interval", type=int, default=10)
    parser.add_argument("--allow-pending", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--no-post-submit-review", action="store_true")
    parser.add_argument("--post-submit-baseline-roots", nargs="*", default=[])
    parser.add_argument("--post-submit-profile-dir", default="")
    parser.add_argument("--post-submit-window-days", type=int, default=14)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
