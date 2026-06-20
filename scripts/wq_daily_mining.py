"""Run the daily WQ find-only/check-only alpha mining workflow.

This script intentionally never calls the WQ submit endpoint. It orchestrates:
1. optional read-only platform alpha sync into the ledger,
2. rechecking pending ledger/file candidates with Check Submission,
3. find-only simulations, and
4. Check Submission review for new hits.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.expression_parser import normalize_expression
from quantgpt.wq_auto_mining import load_dotenv, write_json
from quantgpt.wq_brain_client import get_client, is_configured
from quantgpt.wq_brain_service import run_list_alphas
from quantgpt.wq_forum_submission_optimizer import annotate_candidate_with_policy, load_submission_policy
from scripts import check_wq_submissions as submission_checks
from scripts import wq_find_only


PASS_STATUSES = {"api_check_readable"}
FAIL_STATUSES = {"self_correlation_fail", "prod_correlation_fail"}


@dataclass
class DailyMiningConfig:
    output_dir: Path
    candidate_files: list[Path] = field(default_factory=list)
    pending_inputs: list[Path] = field(default_factory=list)
    account: str = "primary"
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    decay: int = 6
    neutralization: str = "SUBINDUSTRY"
    truncation: float = 0.08
    target_ready: int = 10
    target_sim_hits: int = 20
    max_runs: int = 120
    cycles: int = 1
    start_index: int = 1
    pending_limit: int = 50
    platform_sync_limit: int = 400
    check_chunk_size: int = 1
    check_delay_seconds: int = 0
    similarity_threshold: float = 0.82
    hit_similarity_threshold: float = 0.82
    ledger_similarity_threshold: float = 0.70
    submission_policy_file: Path | None = None
    use_ledger: bool = True
    sync_platform: bool = True
    recheck_pending: bool = True
    mine: bool = True
    check_hits: bool = True
    dry_run: bool = False


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = config_from_args(args)
    summary = run_daily_mining(config)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("ok") else 1


def run_daily_mining(config: DailyMiningConfig) -> dict:
    load_dotenv(ROOT)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    status_file = config.output_dir / "status.json"
    summary_file = config.output_dir / "daily_summary.json"
    report_file = config.output_dir / "summary.md"

    _write_status(status_file, config, "STARTING", "building daily candidate set")
    candidates_file = build_daily_candidate_file(config)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "submit_guard": "find-only/check-only; no submit endpoint is called",
        "canonical_entrypoint": "scripts/wq_daily_mining.py",
        "authoritative_status_file": str(status_file),
        "config": config_to_dict(config),
        "candidates_file": str(candidates_file),
        "candidate_count": _count_jsonl_rows(candidates_file),
    }
    write_json(config.output_dir / "manifest.json", manifest)

    platform_summary = {"ok": True, "skipped": True, "reason": "sync_platform disabled"}
    if config.sync_platform and not config.dry_run:
        _write_status(status_file, config, "SYNCING_PLATFORM", "reading existing platform alphas")
        platform_summary = sync_platform_alphas(config)

    pending_summary = {"ok": True, "skipped": True, "reason": "recheck_pending disabled"}
    pending_check_file = config.output_dir / "pending_submission_check.jsonl"
    if config.recheck_pending:
        _write_status(status_file, config, "COLLECTING_PENDING", "collecting pending correlation checks")
        pending_file = config.output_dir / "pending_candidates.jsonl"
        pending_rows = collect_pending_rows(config, pending_file)
        if pending_rows and not config.dry_run:
            _write_status(status_file, config, "CHECKING_PENDING", f"checking {len(pending_rows)} pending candidates")
            pending_summary = submission_checks.check_wq_submissions(
                input_paths=[pending_file],
                alpha_ids=[],
                output_path=pending_check_file,
                summary_output_path=config.output_dir / "pending_submission_check_summary.json",
                account=config.account,
                include_all=True,
                delay_seconds=config.check_delay_seconds,
                chunk_size=config.check_chunk_size,
                max_checks=config.pending_limit,
                resume=True,
                only_pending=True,
                record_ledger=config.use_ledger,
            )
        else:
            pending_summary = {
                "ok": True,
                "skipped": config.dry_run,
                "reason": "dry_run" if config.dry_run else "no pending candidates",
                "total": len(pending_rows),
                "output": str(pending_check_file),
            }

    check_files: list[Path] = []
    if pending_check_file.is_file():
        check_files.append(pending_check_file)

    cycles: list[dict] = []
    ready_records = submit_ready_records(check_files)
    if config.mine:
        for cycle_index in range(1, max(1, config.cycles) + 1):
            if len(ready_records) >= config.target_ready:
                break
            cycle_dir = config.output_dir / f"cycle_{cycle_index:02d}"
            cycle_dir.mkdir(parents=True, exist_ok=True)
            if config.dry_run:
                cycles.append({
                    "cycle": cycle_index,
                    "skipped": True,
                    "reason": "dry_run",
                    "find_output_dir": str(cycle_dir),
                })
                continue

            _write_status(status_file, config, "MINING", f"running find-only cycle {cycle_index}")
            find_rc = run_find_only_cycle(config, candidates_file, cycle_dir, cycle_index)
            results_file = cycle_dir / "results.jsonl"
            hits_file = cycle_dir / "hits.jsonl"
            hit_check_file = cycle_dir / "candidate_submission_check.jsonl"
            hit_check_summary: dict[str, Any] = {"ok": True, "skipped": True, "reason": "no hits"}
            check_input = results_file if results_file.is_file() else hits_file
            if config.check_hits and check_input.is_file() and _count_checkable_rows(check_input) > 0:
                _write_status(status_file, config, "CHECKING_HITS", f"checking cycle {cycle_index} hits")
                hit_check_summary = submission_checks.check_wq_submissions(
                    input_paths=[check_input],
                    alpha_ids=[],
                    output_path=hit_check_file,
                    summary_output_path=cycle_dir / "hit_submission_check_summary.json",
                    account=config.account,
                    include_all=False,
                    delay_seconds=config.check_delay_seconds,
                    chunk_size=config.check_chunk_size,
                    resume=True,
                    only_pending=False,
                    record_ledger=config.use_ledger,
                )
                check_files.append(hit_check_file)
                ready_records = submit_ready_records(check_files)
            cycles.append({
                "cycle": cycle_index,
                "find_return_code": find_rc,
                "find_output_dir": str(cycle_dir),
                "hits_file": str(hits_file),
                "hits": _count_jsonl_rows(hits_file),
                "check_summary": hit_check_summary,
            })
    else:
        cycles.append({"skipped": True, "reason": "mine disabled"})

    ready_records = submit_ready_records(check_files)
    final_summary = {
        "schema_version": 1,
        "ok": True,
        "status": "TARGET_REACHED" if len(ready_records) >= config.target_ready else "PARTIAL",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "submit_guard": "No real submit was attempted.",
        "canonical_entrypoint": "scripts/wq_daily_mining.py",
        "authoritative_status_file": str(status_file),
        "target_ready": config.target_ready,
        "ready_count": len(ready_records),
        "ready_records": ready_records[: config.target_ready],
        "platform_sync": platform_summary,
        "pending_check": pending_summary,
        "cycles": cycles,
        "files": {
            "output_dir": str(config.output_dir),
            "manifest": str(config.output_dir / "manifest.json"),
            "candidates": str(candidates_file),
            "summary": str(summary_file),
            "report": str(report_file),
        },
    }
    write_json(summary_file, final_summary)
    write_daily_report(report_file, config, final_summary)
    _write_status(status_file, config, final_summary["status"], f"ready_count={len(ready_records)}")
    return final_summary


def build_daily_candidate_file(config: DailyMiningConfig) -> Path:
    rows: list[dict] = []
    for path in config.candidate_files or default_candidate_files():
        if path.is_file():
            rows.extend(_load_candidate_rows(path))
    unique: list[dict] = []
    skipped: list[dict] = []
    submission_policy = load_submission_policy(config.submission_policy_file)
    seen: set[str] = set()
    for row in rows:
        expression = str(row.get("expression") or "").strip()
        if not expression:
            continue
        normalized = normalize_expression(expression)
        if normalized in seen:
            continue
        seen.add(normalized)
        policy_row = annotate_candidate_with_policy(row, submission_policy) if submission_policy else row
        if policy_row.get("forum_policy_action") == "block":
            skipped.append({
                **policy_row,
                "candidate_skip_reason": policy_row.get("forum_policy_reason") or "forum_policy_block",
            })
            continue
        unique.append(policy_row)

    output_path = config.output_dir / "daily_candidates.jsonl"
    _write_jsonl(output_path, unique)
    if skipped:
        _write_jsonl(config.output_dir / "daily_policy_skipped.jsonl", skipped)
    return output_path


def collect_pending_rows(config: DailyMiningConfig, output_path: Path) -> list[dict]:
    rows: list[dict] = []
    for path in config.pending_inputs:
        if path.is_file():
            rows.extend(submission_checks.load_submission_rows([path], include_all=True, only_pending=True))
    if config.use_ledger:
        try:
            from quantgpt.wq_alpha_ledger import query_alpha_experiment_rows_sync

            rows.extend(query_alpha_experiment_rows_sync(
                statuses=["correlation_pending"],
                limit=config.pending_limit,
                require_alpha_id=True,
            ))
        except Exception:
            pass

    unique = _dedupe_alpha_rows(rows)[: max(0, config.pending_limit)]
    _write_jsonl(output_path, unique)
    return unique


def run_find_only_cycle(config: DailyMiningConfig, candidates_file: Path, output_dir: Path, cycle_index: int) -> int:
    start_index = config.start_index + (cycle_index - 1) * config.max_runs
    argv = [
        "--candidates",
        str(candidates_file),
        "--output-dir",
        str(output_dir),
        "--region",
        config.region,
        "--universe",
        config.universe,
        "--delay",
        str(config.delay),
        "--decay",
        str(config.decay),
        "--neutralization",
        config.neutralization,
        "--truncation",
        str(config.truncation),
        "--account",
        config.account,
        "--max-runs",
        str(config.max_runs),
        "--start-index",
        str(start_index),
        "--target-eligible",
        str(config.target_sim_hits),
        "--similarity-threshold",
        str(config.similarity_threshold),
        "--hit-similarity-threshold",
        str(config.hit_similarity_threshold),
        "--ledger-similarity-threshold",
        str(config.ledger_similarity_threshold),
    ]
    if config.use_ledger:
        argv.append("--use-ledger")
    return wq_find_only.main(argv)


def sync_platform_alphas(config: DailyMiningConfig) -> dict:
    if not is_configured(config.account):
        return {"ok": False, "reason": f"WQ credentials are not configured (account={config.account})"}

    client = get_client(config.account)
    records: list[dict] = []
    try:
        if not client.authenticate():
            return {"ok": False, "reason": f"WQ authentication failed (account={config.account})"}
        offset = 0
        while offset < config.platform_sync_limit:
            limit = min(100, config.platform_sync_limit - offset)
            result = run_list_alphas(client, limit=limit, offset=offset)
            if not result.get("ok"):
                return {"ok": False, "reason": result.get("error", "platform alpha list failed"), "records": len(records)}
            page = result.get("alphas") or []
            records.extend(page)
            if len(page) < limit:
                break
            offset += len(page)
    finally:
        client.close()

    output_path = config.output_dir / "platform_alphas.jsonl"
    _write_jsonl(output_path, records)
    ledger_result = None
    if config.use_ledger:
        ledger_result = _record_platform_alphas_in_ledger(records, output_path, config)
    return {
        "ok": True,
        "records": len(records),
        "active_or_submitted": sum(1 for row in records if str(row.get("status") or "").upper() in {"ACTIVE", "SUBMITTED"}),
        "output": str(output_path),
        "ledger": ledger_result,
    }


def submit_ready_records(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        rows.extend(_read_jsonl(path))
    ready = [row for row in rows if _is_submit_ready(row)]
    deduped = _dedupe_alpha_rows(ready)
    deduped.sort(key=lambda row: (_score(row.get("fitness")), _score(row.get("sharpe"))), reverse=True)
    return deduped


def write_daily_report(path: Path, config: DailyMiningConfig, summary: dict) -> None:
    lines = [
        "# WQ Daily Mining Summary",
        "",
        f"- Updated: {summary.get('updated_at')}",
        f"- Status: {summary.get('status')}",
        f"- Submit guard: {summary.get('submit_guard')}",
        f"- Target ready: {config.target_ready}",
        f"- Ready count: {summary.get('ready_count')}",
        f"- Output: {config.output_dir}",
        "",
        "## Submit-Ready Candidates",
    ]
    ready = summary.get("ready_records") or []
    if not ready:
        lines.append("- None")
    for row in ready:
        lines.append(
            "- "
            f"{row.get('alpha_id')}: "
            f"sharpe={row.get('sharpe')} fitness={row.get('fitness')} turnover={row.get('turnover')} "
            f"sc={row.get('sc_result')}:{row.get('sc_value')}"
        )
        lines.extend(["", "```text", str(row.get("expression") or ""), "```"])
    lines.extend([
        "",
        "## Files",
        f"- Candidates: {summary.get('files', {}).get('candidates')}",
        f"- JSON summary: {summary.get('files', {}).get('summary')}",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def config_from_args(args: argparse.Namespace) -> DailyMiningConfig:
    file_config = _load_config_file(args.config)
    output_dir = _path_value(args.output_dir, file_config.get("output_dir"))
    if output_dir is None:
        output_dir = ROOT / "reports" / "wq_daily" / datetime.now().strftime("%Y%m%d_%H%M%S")

    candidate_files = _path_list(
        [*(file_config.get("candidate_files") or []), *(args.candidate_file or []), *(args.community_candidates or [])]
    )
    pending_inputs = _path_list([*(file_config.get("pending_inputs") or []), *(args.pending_input or [])])
    submission_policy_value = _value(args.submission_policy_file, file_config, "submission_policy_file", "")

    return DailyMiningConfig(
        output_dir=output_dir,
        candidate_files=candidate_files,
        pending_inputs=pending_inputs,
        account=_value(args.account, file_config, "account", "primary"),
        region=_value(args.region, file_config, "region", "USA"),
        universe=_value(args.universe, file_config, "universe", "TOP3000"),
        delay=int(_value(args.delay, file_config, "delay", 1)),
        decay=int(_value(args.decay, file_config, "decay", 6)),
        neutralization=_value(args.neutralization, file_config, "neutralization", "SUBINDUSTRY"),
        truncation=float(_value(args.truncation, file_config, "truncation", 0.08)),
        target_ready=int(_value(args.target_ready, file_config, "target_ready", 10)),
        target_sim_hits=int(_value(args.target_sim_hits, file_config, "target_sim_hits", 20)),
        max_runs=int(_value(args.max_runs, file_config, "max_runs", 120)),
        cycles=int(_value(args.cycles, file_config, "cycles", 1)),
        start_index=int(_value(args.start_index, file_config, "start_index", 1)),
        pending_limit=int(_value(args.pending_limit, file_config, "pending_limit", 50)),
        platform_sync_limit=int(_value(args.platform_sync_limit, file_config, "platform_sync_limit", 400)),
        check_chunk_size=int(_value(args.check_chunk_size, file_config, "check_chunk_size", 1)),
        check_delay_seconds=int(_value(args.check_delay_seconds, file_config, "check_delay_seconds", 0)),
        similarity_threshold=float(_value(args.similarity_threshold, file_config, "similarity_threshold", 0.82)),
        hit_similarity_threshold=float(_value(args.hit_similarity_threshold, file_config, "hit_similarity_threshold", 0.82)),
        ledger_similarity_threshold=float(_value(args.ledger_similarity_threshold, file_config, "ledger_similarity_threshold", 0.70)),
        submission_policy_file=_resolve_path(submission_policy_value) if submission_policy_value else None,
        use_ledger=bool(_value(args.use_ledger, file_config, "use_ledger", True)),
        sync_platform=bool(_value(args.sync_platform, file_config, "sync_platform", True)),
        recheck_pending=bool(_value(args.recheck_pending, file_config, "recheck_pending", True)),
        mine=bool(_value(args.mine, file_config, "mine", True)),
        check_hits=bool(_value(args.check_hits, file_config, "check_hits", True)),
        dry_run=bool(_value(args.dry_run, file_config, "dry_run", False)),
    )


def default_candidate_files() -> list[Path]:
    files = [ROOT / "scripts" / "local_seed_expressions.json"]
    community_files = sorted(
        (ROOT / "reports").glob("wq_community_triage*/community_wq_candidates.jsonl"),
        key=lambda path: path.stat().st_mtime if path.is_file() else 0,
        reverse=True,
    )
    if community_files:
        files.append(community_files[0])
    return files


def config_to_dict(config: DailyMiningConfig) -> dict:
    data = asdict(config)
    for key, value in list(data.items()):
        if isinstance(value, Path):
            data[key] = str(value)
        elif isinstance(value, list):
            data[key] = [str(item) if isinstance(item, Path) else item for item in value]
    return data


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the WQ daily find-only/check-only mining workflow")
    parser.add_argument("--config", default="")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--candidate-file", action="append", default=None, help="Candidate JSON/JSONL file")
    parser.add_argument("--community-candidates", action="append", default=None, help="Community triage candidate JSONL file")
    parser.add_argument("--pending-input", action="append", default=None, help="Prior check/find output to recheck if pending")
    parser.add_argument("--account", default=None)
    parser.add_argument("--region", default=None)
    parser.add_argument("--universe", default=None)
    parser.add_argument("--delay", type=int, default=None)
    parser.add_argument("--decay", type=int, default=None)
    parser.add_argument("--neutralization", default=None)
    parser.add_argument("--truncation", type=float, default=None)
    parser.add_argument("--target-ready", type=int, default=None)
    parser.add_argument("--target-sim-hits", type=int, default=None)
    parser.add_argument("--max-runs", type=int, default=None)
    parser.add_argument("--cycles", type=int, default=None)
    parser.add_argument("--start-index", type=int, default=None)
    parser.add_argument("--pending-limit", type=int, default=None)
    parser.add_argument("--platform-sync-limit", type=int, default=None)
    parser.add_argument("--check-chunk-size", type=int, default=None)
    parser.add_argument("--check-delay-seconds", type=int, default=None)
    parser.add_argument("--similarity-threshold", type=float, default=None)
    parser.add_argument("--hit-similarity-threshold", type=float, default=None)
    parser.add_argument("--ledger-similarity-threshold", type=float, default=None)
    parser.add_argument("--submission-policy-file", default=None)
    parser.set_defaults(use_ledger=None, sync_platform=None, recheck_pending=None, mine=None, check_hits=None, dry_run=None)
    parser.add_argument("--use-ledger", dest="use_ledger", action="store_true")
    parser.add_argument("--no-ledger", dest="use_ledger", action="store_false")
    parser.add_argument("--sync-platform", dest="sync_platform", action="store_true")
    parser.add_argument("--no-sync-platform", dest="sync_platform", action="store_false")
    parser.add_argument("--recheck-pending", dest="recheck_pending", action="store_true")
    parser.add_argument("--no-recheck-pending", dest="recheck_pending", action="store_false")
    parser.add_argument("--mine", dest="mine", action="store_true")
    parser.add_argument("--no-mine", dest="mine", action="store_false")
    parser.add_argument("--check-hits", dest="check_hits", action="store_true")
    parser.add_argument("--no-check-hits", dest="check_hits", action="store_false")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    return parser.parse_args(argv)


def _load_config_file(path_value: str) -> dict:
    if not path_value:
        return {}
    path = _resolve_path(path_value)
    if not path.is_file():
        raise FileNotFoundError(f"config file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _load_candidate_rows(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig")
    rows: list[dict] = []
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError(f"candidate JSON must be an array: {path}")
        iterable = data
    else:
        iterable = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            iterable.append(json.loads(line) if line.startswith("{") or line.startswith('"') else line)

    for index, value in enumerate(iterable):
        if isinstance(value, str):
            rows.append({"expression": value, "tag": path.stem, "source_file": str(path), "source_index": index})
        elif isinstance(value, dict):
            row = dict(value)
            row.setdefault("tag", path.stem)
            row["source_file"] = str(path)
            row.setdefault("source_index", index)
            rows.append(row)
    return rows


def _record_platform_alphas_in_ledger(records: list[dict], output_path: Path, config: DailyMiningConfig) -> dict:
    ledger_records = []
    for row in records:
        status = str(row.get("status") or "").upper()
        if status not in {"ACTIVE", "SUBMITTED"}:
            continue
        ledger_records.append({
            "alpha_id": row.get("alpha_id"),
            "expression": row.get("expression"),
            "api_check_status": "platform_active_check_readable",
            "platform_status": status,
            "source_submit_eligible": True,
            "sharpe": row.get("sharpe"),
            "fitness": row.get("fitness"),
            "returns": row.get("returns"),
            "turnover": row.get("turnover"),
            "source_file": str(output_path),
        })
    if not ledger_records:
        return {"ok": True, "recorded": 0}
    try:
        from quantgpt.wq_alpha_ledger import record_api_check_records_sync

        return record_api_check_records_sync(
            ledger_records,
            settings={
                "account": config.account,
                "region": config.region,
                "universe": config.universe,
                "delay": config.delay,
                "decay": config.decay,
                "neutralization": config.neutralization,
                "truncation": config.truncation,
            },
            source_run_id=config.output_dir.name,
        )
    except Exception as exc:
        return {"ok": False, "recorded": 0, "error": str(exc)}


def _is_submit_ready(row: dict) -> bool:
    if str(row.get("api_check_status") or "") not in PASS_STATUSES:
        return False
    if str(row.get("platform_status") or "").upper() in {"ACTIVE", "SUBMITTED"}:
        return False
    source_status = str(row.get("source_status") or "").lower()
    if source_status and source_status not in {"eligible", "pending_correlation_check"}:
        return False
    if str(row.get("sc_result") or "").upper() == "FAIL":
        return False
    if str(row.get("prod_corr_result") or "").upper() == "FAIL":
        return False
    return (
        _score(row.get("sharpe")) >= 1.25
        and _score(row.get("fitness")) >= 1.0
        and 0.01 <= _score(row.get("turnover")) <= 0.70
    )


def _dedupe_alpha_rows(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for row in rows:
        alpha_id = str(row.get("alpha_id") or "")
        if not alpha_id or alpha_id in seen:
            continue
        seen.add(alpha_id)
        unique.append(row)
    return unique


def _write_status(path: Path, config: DailyMiningConfig, status: str, message: str) -> None:
    write_json(path, {
        "schema_version": 1,
        "status": status,
        "message": message,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "output_dir": str(config.output_dir),
        "target_ready": config.target_ready,
        "submit_guard": "find-only/check-only; no submit endpoint is called",
        "canonical_entrypoint": "scripts/wq_daily_mining.py",
        "authoritative_status_file": str(path),
    })


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip().startswith("{")]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _count_jsonl_rows(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip())


def _count_checkable_rows(path: Path) -> int:
    count = 0
    for row in _read_jsonl(path):
        status = str(row.get("status") or row.get("source_status") or "").lower()
        if status in {"eligible", "pending_correlation_check"} or row.get("submit_eligible") is True:
            count += 1
    return count


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("-inf")


def _value(cli_value, file_config: dict, key: str, default):
    if cli_value is not None:
        return cli_value
    if key in file_config:
        return file_config[key]
    return default


def _path_value(cli_value: str | None, file_value: str | None) -> Path | None:
    value = cli_value if cli_value is not None else file_value
    return _resolve_path(value) if value else None


def _path_list(values: list[str]) -> list[Path]:
    return [_resolve_path(value) for value in values if value]


def _resolve_path(value: str | Path) -> Path:
    path = value if isinstance(value, Path) else Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
