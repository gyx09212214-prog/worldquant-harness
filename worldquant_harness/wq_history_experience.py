"""Collect WQ history into canonical experience artifacts and ledger memory."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .wq_failure_memory import (
    expression_components,
    expression_hash,
    normalized_settings,
    pattern_signature,
)
from .wq_pnl_analysis import aligned_daily_return_correlation
from .wq_research_profile import default_research_profile

SCHEMA_VERSION = 1
BLOCK_FAILURES = {"platform_alpha", "self_correlation_fail", "prod_correlation_fail", "high_similarity", "duplicate", "validation_error"}
PENALIZE_FAILURES = {"low_sharpe", "low_fitness", "low_turnover", "high_turnover", "concentrated_weight", "sub_universe_fail", "platform_check_fail"}
LOCAL_FILE_PATTERNS = (
    "submit_results.jsonl",
    "submit_existing_results.jsonl",
    "check_results.jsonl",
    "api_check*.jsonl",
    "presubmit_rejected.jsonl",
    "presubmit_ready_sequential.jsonl",
    "review_queue.jsonl",
    "simulation_results.jsonl",
    "platform_alphas.jsonl",
)


@dataclass(frozen=True)
class WQHistoryExperienceConfig:
    reports_dir: Path
    output_dir: Path
    account: str = "primary"
    check_policy: str = "all"
    write_ledger: bool = False
    platform_enabled: bool = True
    resume: bool = True
    chunk_size: int = 25
    delay_seconds: float = 1.0
    platform_limit: int = 0
    max_checks: int = 0
    check_polls: int = 2
    check_interval: int = 5
    probe_pnl_limit: int = 0
    local_file_limit: int = 0
    event_limit: int = 0
    pnl_min_overlap: int = 20
    pnl_island_abs_corr: float = 0.70
    pnl_warn_abs_corr: float = 0.50


def collect_history_experience(
    config: WQHistoryExperienceConfig,
    *,
    client_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Collect local and platform history into replayable experience artifacts."""

    output_dir = Path(config.output_dir)
    raw_dir = output_dir / "raw"
    canonical_dir = output_dir / "canonical"
    raw_dir.mkdir(parents=True, exist_ok=True)
    canonical_dir.mkdir(parents=True, exist_ok=True)

    local_files = discover_local_history_files(config.reports_dir)
    if config.local_file_limit > 0:
        local_files = local_files[: config.local_file_limit]
    inventory = local_file_inventory(local_files)
    local_events = collect_local_history_events(local_files)
    if config.event_limit > 0:
        local_events = local_events[: config.event_limit]

    platform_result = {
        "ok": False,
        "enabled": config.platform_enabled,
        "skipped": True,
        "reason": "platform sync disabled",
        "alphas": [],
        "check_records": [],
        "probe_summaries": [],
    }
    if config.platform_enabled:
        platform_result = collect_platform_history(config, output_dir=output_dir, client_factory=client_factory)

    platform_alpha_events = [
        normalize_platform_alpha(row, source_file="platform:/users/self/alphas")
        for row in platform_result.get("alphas") or []
    ]
    platform_check_events = [
        normalize_event(row, source_type="api_check", source_file="platform:/alphas/check")
        for row in platform_result.get("check_records") or []
    ]
    events = merge_history_events([*local_events, *platform_alpha_events, *platform_check_events])

    memory = build_history_experience_memory(events)
    elite = build_history_elite_archive(events)
    pnl_index = collect_pnl_curve_index(config.reports_dir, events=events)
    pnl_islands = build_pnl_corr_islands(
        pnl_index,
        min_overlap=config.pnl_min_overlap,
        island_abs_corr=config.pnl_island_abs_corr,
        warn_abs_corr=config.pnl_warn_abs_corr,
    )
    profile_candidate = build_history_profile_candidate(
        events=events,
        memory=memory,
        pnl_islands=pnl_islands,
        output_dir=output_dir,
    )

    files = {
        "local_file_inventory": str(raw_dir / "local_file_inventory.csv"),
        "platform_alphas": str(raw_dir / "platform_alphas.jsonl"),
        "platform_check_results": str(raw_dir / "platform_check_results.jsonl"),
        "platform_probe_summary": str(raw_dir / "platform_probe_summary.json"),
        "history_alpha_events": str(canonical_dir / "history_alpha_events.jsonl"),
        "history_experience_memory": str(canonical_dir / "history_experience_memory.jsonl"),
        "history_elite_archive": str(canonical_dir / "history_elite_archive.jsonl"),
        "pnl_curve_index": str(canonical_dir / "pnl_curve_index.jsonl"),
        "pnl_corr_islands": str(canonical_dir / "pnl_corr_islands.json"),
        "history_research_profile_candidate": str(canonical_dir / "history_research_profile_candidate.json"),
        "ledger_write_report": str(canonical_dir / "ledger_write_report.json"),
        "summary": str(output_dir / "summary.json"),
        "summary_md": str(output_dir / "summary.md"),
    }
    _write_csv(Path(files["local_file_inventory"]), inventory)
    _write_jsonl(Path(files["platform_alphas"]), platform_result.get("alphas") or [])
    _write_jsonl(Path(files["platform_check_results"]), platform_result.get("check_records") or [])
    _write_json(Path(files["platform_probe_summary"]), {
        "ok": bool(platform_result.get("ok")),
        "probe_summaries": platform_result.get("probe_summaries") or [],
    })
    _write_jsonl(Path(files["history_alpha_events"]), events)
    _write_jsonl(Path(files["history_experience_memory"]), memory)
    _write_jsonl(Path(files["history_elite_archive"]), elite)
    _write_jsonl(Path(files["pnl_curve_index"]), pnl_index)
    _write_json(Path(files["pnl_corr_islands"]), pnl_islands)
    _write_json(Path(files["history_research_profile_candidate"]), profile_candidate)

    ledger_report = {"ok": True, "write_ledger": False, "recorded": 0, "skipped": len(events)}
    if config.write_ledger:
        ledger_report = write_history_to_ledger(events, account=config.account)
    _write_json(Path(files["ledger_write_report"]), ledger_report)

    summary = history_summary(
        config=config,
        inventory=inventory,
        events=events,
        memory=memory,
        elite=elite,
        pnl_islands=pnl_islands,
        platform_result=platform_result,
        ledger_report=ledger_report,
        files=files,
    )
    _write_json(Path(files["summary"]), summary)
    Path(files["summary_md"]).write_text(render_history_summary_markdown(summary), encoding="utf-8")
    return summary


def discover_local_history_files(reports_dir: Path | str) -> list[Path]:
    root = Path(reports_dir)
    files: list[Path] = []
    seen: set[Path] = set()
    for pattern in LOCAL_FILE_PATTERNS:
        for path in sorted(root.rglob(pattern)):
            if path.is_file() and path not in seen:
                seen.add(path)
                files.append(path)
    return files


def local_file_inventory(files: list[Path]) -> list[dict[str, Any]]:
    return [
        {
            "path": str(path),
            "name": path.name,
            "source_type": source_type_for_path(path),
            "size_bytes": path.stat().st_size,
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
        }
        for path in files
    ]


def collect_local_history_events(files: list[Path]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in files:
        source_type = source_type_for_path(path)
        for index, row in enumerate(_read_jsonl(path)):
            events.append(normalize_event(row, source_type=source_type, source_file=str(path), row_index=index))
    return events


def source_type_for_path(path: Path | str) -> str:
    name = Path(path).name.lower()
    if name == "submit_existing_results.jsonl":
        return "submit_existing_result"
    if name == "submit_results.jsonl":
        return "submit_result"
    if name == "check_results.jsonl" or name.startswith("api_check"):
        return "api_check"
    if name == "presubmit_rejected.jsonl":
        return "presubmit_rejected"
    if name == "presubmit_ready_sequential.jsonl":
        return "presubmit_ready"
    if name == "review_queue.jsonl":
        return "review_queue"
    if name == "simulation_results.jsonl":
        return "simulation_result"
    if name == "platform_alphas.jsonl":
        return "platform_alpha"
    return "history_file"


def normalize_platform_alpha(row: dict[str, Any], *, source_file: str) -> dict[str, Any]:
    is_data = row.get("is") if isinstance(row.get("is"), dict) else {}
    settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
    expression = _first_text(_nested(row, "regular", "code"), row.get("expression"), row.get("code"))
    status = str(row.get("status") or row.get("platform_status") or "").upper()
    event = {
        **row,
        "expression": expression,
        "alpha_id": row.get("alpha_id") or row.get("id"),
        "platform_status": status,
        "status": status.lower() if status else row.get("status"),
        "sharpe": _first_float(row.get("sharpe"), is_data.get("sharpe")),
        "fitness": _first_float(row.get("fitness"), is_data.get("fitness")),
        "returns": _first_float(row.get("returns"), is_data.get("returns")),
        "turnover": _first_float(row.get("turnover"), is_data.get("turnover")),
        "drawdown": _first_float(row.get("drawdown"), is_data.get("drawdown")),
        "margin": _first_float(row.get("margin"), is_data.get("margin")),
        "settings": settings,
    }
    return normalize_event(event, source_type="platform_alpha", source_file=source_file)


def normalize_event(
    row: dict[str, Any],
    *,
    source_type: str,
    source_file: str,
    row_index: int | None = None,
) -> dict[str, Any]:
    expression = _first_text(row.get("expression"), _nested(row, "result", "expression"), _nested(row, "regular", "code"))
    alpha_id = _first_text(row.get("alpha_id"), _nested(row, "result", "alpha_id"), row.get("id"))
    settings = normalized_settings(_settings_from_row(row))
    review = row.get("review_checks") if isinstance(row.get("review_checks"), dict) else {}
    self_check = row.get("self_correlation") if isinstance(row.get("self_correlation"), dict) else {}
    prod_check = row.get("prod_correlation") if isinstance(row.get("prod_correlation"), dict) else {}
    if review:
        self_check = self_check or review.get("self_correlation") or {}
        prod_check = prod_check or review.get("prod_correlation") or {}

    sc_result = _first_text(row.get("sc_result"), self_check.get("result"))
    prod_result = _first_text(row.get("prod_corr_result"), prod_check.get("result"))
    sc_value = _first_float(row.get("sc_value"), row.get("sc"), self_check.get("value"), _detail_value(row.get("detail"), "self"))
    prod_value = _first_float(row.get("prod_corr_value"), row.get("prod_value"), prod_check.get("value"), _detail_value(row.get("detail"), "prod"))
    platform_status = str(_first_text(row.get("platform_status"), row.get("final_status"), row.get("status")) or "").upper()
    api_status = _first_text(row.get("api_check_status"), _api_status_from_row(row, sc_result, prod_result, sc_value, prod_value))
    failure_kind = canonical_failure_kind(row, api_status=api_status, platform_status=platform_status, sc_result=sc_result, prod_result=prod_result, sc_value=sc_value)
    severity = severity_for_failure(failure_kind, platform_status=platform_status, source_type=source_type)
    lifecycle = lifecycle_for_event(row, api_status=api_status, platform_status=platform_status, source_type=source_type, severity=severity)
    metrics = {
        "sharpe": _first_float(row.get("sharpe"), _nested(row, "candidate_metrics", "sharpe"), _nested(row, "result", "wq_brain", "wq_sharpe"), _nested(row, "is_metrics", "sharpe")),
        "fitness": _first_float(row.get("fitness"), _nested(row, "candidate_metrics", "fitness"), _nested(row, "result", "wq_brain", "wq_fitness"), _nested(row, "is_metrics", "fitness")),
        "returns": _first_float(row.get("returns"), _nested(row, "candidate_metrics", "returns"), _nested(row, "result", "wq_brain", "wq_returns"), _nested(row, "is_metrics", "returns")),
        "turnover": _first_float(row.get("turnover"), _nested(row, "candidate_metrics", "turnover"), _nested(row, "result", "wq_brain", "wq_turnover"), _nested(row, "is_metrics", "turnover")),
        "drawdown": _first_float(row.get("drawdown")),
        "margin": _first_float(row.get("margin")),
    }
    source_run_id = source_run_id_from_file(source_file)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "event_id": stable_event_id(source_file, row_index, alpha_id, expression, source_type),
        "created_at": _first_text(row.get("created_at"), row.get("dateCreated"), row.get("dateSubmitted")),
        "collected_at": _now(),
        "source_type": source_type,
        "source_file": source_file,
        "source_run_id": source_run_id,
        "source_row_index": row_index,
        "alpha_id": alpha_id,
        "expression": expression,
        "tag": _first_text(row.get("tag"), row.get("source_tag"), row.get("domain")),
        "source_family": _first_text(row.get("source_family"), row.get("mutation_strategy"), row.get("domain")),
        "lifecycle_status": lifecycle,
        "platform_status": platform_status,
        "api_check_status": api_status,
        "failure_kind": failure_kind,
        "severity": severity,
        "submit_eligible": _first_bool(row.get("submit_eligible"), row.get("source_submit_eligible")),
        "submitted": _first_bool(row.get("submitted"), row.get("source_submitted")),
        "settings": settings,
        "metrics": metrics,
        "sharpe": metrics["sharpe"],
        "fitness": metrics["fitness"],
        "returns": metrics["returns"],
        "turnover": metrics["turnover"],
        "sc_result": sc_result,
        "sc_value": sc_value,
        "sc_limit": _first_float(row.get("sc_limit"), self_check.get("limit")),
        "prod_corr_result": prod_result,
        "prod_corr_value": prod_value,
        "prod_corr_limit": _first_float(row.get("prod_corr_limit"), prod_check.get("limit")),
        "nearest_similarity": _first_float(row.get("nearest_similarity"), _nested(row, "presubmit_gate", "nearest_similarity")),
        "presubmit_reject_reason": row.get("presubmit_reject_reason"),
        "failed_platform_checks": row.get("failed_platform_checks") or [],
        "review_failure_kind": row.get("review_failure_kind"),
        "raw_status": row.get("status"),
        "raw_final_status": row.get("final_status"),
    }
    return payload


def collect_platform_history(
    config: WQHistoryExperienceConfig,
    *,
    output_dir: Path,
    client_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    if client_factory is None:
        from .wq_brain_client import get_client, is_configured

        if not is_configured(config.account):
            return {"ok": False, "enabled": True, "skipped": True, "reason": f"WQ credentials not configured for {config.account}", "alphas": [], "check_records": [], "probe_summaries": []}
        client_factory = get_client

    client = client_factory(config.account)
    try:
        if hasattr(client, "authenticate") and not client.authenticate():
            return {"ok": False, "enabled": True, "skipped": True, "reason": "WQ authentication failed", "alphas": [], "check_records": [], "probe_summaries": []}
        alphas = fetch_platform_alphas(client, limit=config.platform_limit)
        check_records = check_platform_alphas(client, alphas, config=config, output_dir=output_dir)
        probe_summaries = probe_platform_pnl(client, alphas, limit=config.probe_pnl_limit)
        return {
            "ok": True,
            "enabled": True,
            "skipped": False,
            "alphas": alphas,
            "check_records": check_records,
            "probe_summaries": probe_summaries,
        }
    finally:
        if hasattr(client, "close"):
            client.close()


def fetch_platform_alphas(client: Any, *, limit: int = 0) -> list[dict[str, Any]]:
    alphas: list[dict[str, Any]] = []
    offset = 0
    page_size = 100
    while True:
        payload = client.get_json(
            "/users/self/alphas",
            params={"limit": page_size, "offset": offset, "order": "-dateCreated"},
        )
        results = payload.get("results") if isinstance(payload, dict) else []
        if not isinstance(results, list) or not results:
            break
        alphas.extend(row for row in results if isinstance(row, dict))
        if limit and len(alphas) >= limit:
            return alphas[:limit]
        offset += len(results)
        total = payload.get("count") if isinstance(payload, dict) else None
        if isinstance(total, int) and offset >= total:
            break
    return alphas


def check_platform_alphas(
    client: Any,
    alphas: list[dict[str, Any]],
    *,
    config: WQHistoryExperienceConfig,
    output_dir: Path,
) -> list[dict[str, Any]]:
    if config.check_policy == "none":
        return []
    existing = {}
    existing_path = output_dir / "raw" / "platform_check_results.jsonl"
    if config.resume and existing_path.is_file():
        existing = {str(row.get("alpha_id") or ""): row for row in _read_jsonl(existing_path) if row.get("alpha_id")}

    source_rows = [platform_source_row(row) for row in alphas if row.get("id") or row.get("alpha_id")]
    if config.check_policy == "pending":
        source_rows = [row for row in source_rows if needs_platform_check(row)]
    if config.resume:
        source_rows = [row for row in source_rows if str(row.get("alpha_id") or "") not in existing]
    if config.max_checks > 0:
        source_rows = source_rows[: config.max_checks]

    records = list(existing.values())
    for index, row in enumerate(source_rows):
        if config.delay_seconds > 0 and index > 0 and index % max(1, config.chunk_size) == 0:
            time.sleep(config.delay_seconds)
        alpha_id = str(row["alpha_id"])
        result = client_check_alpha_submission(
            client,
            alpha_id,
            max_polls=config.check_polls,
            interval=config.check_interval,
        )
        records.append(api_check_record_from_result(row, result))
    return records


def client_check_alpha_submission(client: Any, alpha_id: str, *, max_polls: int, interval: int) -> dict[str, Any]:
    try:
        return client.check_alpha_submission(alpha_id, max_polls=max_polls, interval=interval)
    except TypeError:
        return client.check_alpha_submission(alpha_id)


def probe_platform_pnl(client: Any, alphas: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not hasattr(client, "probe_alpha_detail"):
        return []
    summaries: list[dict[str, Any]] = []
    for row in alphas[:limit]:
        alpha_id = str(row.get("id") or row.get("alpha_id") or "")
        if not alpha_id:
            continue
        probe = client.probe_alpha_detail(alpha_id)
        summaries.append({
            "alpha_id": alpha_id,
            "ok": bool(probe.get("ok")),
            "read_only": True,
            "endpoint_count": len(probe.get("endpoints") or []),
        })
    return summaries


def platform_source_row(row: dict[str, Any]) -> dict[str, Any]:
    is_data = row.get("is") if isinstance(row.get("is"), dict) else {}
    return {
        "alpha_id": row.get("alpha_id") or row.get("id"),
        "expression": _first_text(_nested(row, "regular", "code"), row.get("expression"), row.get("code")),
        "tag": row.get("tag"),
        "source_status": row.get("status"),
        "source_submit_eligible": True,
        "source_submitted": str(row.get("status") or "").upper() in {"ACTIVE", "SUBMITTED"},
        "status": row.get("status"),
        "grade": row.get("grade"),
        "dateCreated": row.get("dateCreated"),
        "sharpe": _first_float(row.get("sharpe"), is_data.get("sharpe")),
        "fitness": _first_float(row.get("fitness"), is_data.get("fitness")),
        "returns": _first_float(row.get("returns"), is_data.get("returns")),
        "turnover": _first_float(row.get("turnover"), is_data.get("turnover")),
        "source_file": "platform:/users/self/alphas",
    }


def needs_platform_check(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or row.get("source_status") or "").upper()
    return status not in {"ACTIVE"} or row.get("sc_result") in {None, "", "PENDING"} or row.get("prod_corr_result") in {None, "", "PENDING"}


def api_check_record_from_result(source_row: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    review = result.get("review_checks") if isinstance(result.get("review_checks"), dict) else {}
    self_check = review.get("self_correlation") or {}
    prod_check = review.get("prod_correlation") or {}
    sc_result = _first_text(result.get("sc_result"), self_check.get("result"))
    prod_result = _first_text(result.get("prod_corr_result"), prod_check.get("result"))
    sc_value = _first_float(result.get("sc_value"), result.get("sc"), self_check.get("value"))
    prod_value = _first_float(result.get("prod_corr_value"), result.get("prod_value"), prod_check.get("value"))
    record = {
        "created_at": _now(),
        "alpha_id": source_row.get("alpha_id"),
        "expression": source_row.get("expression"),
        "tag": source_row.get("tag"),
        "platform_status": result.get("status") or source_row.get("status"),
        "grade": result.get("grade") or source_row.get("grade"),
        "dateCreated": result.get("dateCreated") or source_row.get("dateCreated"),
        "sharpe": _first_float(result.get("sharpe"), source_row.get("sharpe")),
        "fitness": _first_float(result.get("fitness"), source_row.get("fitness")),
        "returns": _first_float(result.get("returns"), source_row.get("returns")),
        "turnover": _first_float(result.get("turnover"), source_row.get("turnover")),
        "sc_result": sc_result,
        "sc_value": sc_value,
        "sc_limit": _first_float(result.get("sc_limit"), self_check.get("limit")),
        "prod_corr_result": prod_result,
        "prod_corr_value": prod_value,
        "prod_corr_limit": _first_float(result.get("prod_corr_limit"), prod_check.get("limit")),
        "review_failure_kind": result.get("review_failure_kind") or result.get("failure_kind"),
        "error": result.get("error"),
        "source_status": source_row.get("source_status"),
        "source_submit_eligible": source_row.get("source_submit_eligible"),
        "source_submitted": source_row.get("source_submitted"),
        "source_file": source_row.get("source_file"),
        "raw_check": result,
    }
    record["api_check_status"] = classify_api_check_record(record)
    return record


def classify_api_check_record(record: dict[str, Any]) -> str:
    failure = str(record.get("review_failure_kind") or "").lower()
    if failure == "prod_correlation" or record.get("prod_corr_result") == "FAIL":
        return "prod_correlation_fail"
    if failure == "self_correlation" or record.get("sc_result") == "FAIL":
        return "self_correlation_fail"
    if failure == "correlation_pending":
        return "api_check_pending"
    if record.get("error"):
        return "api_check_failed"
    if record.get("sc_result") == "PENDING" or record.get("prod_corr_result") == "PENDING":
        return "api_check_pending"
    status = str(record.get("platform_status") or "").upper()
    if status == "ACTIVE":
        return "platform_active_check_readable"
    return "api_check_readable"


def merge_history_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for event in events:
        key = event_merge_key(event)
        current = by_key.get(key)
        if current is None or status_rank(event.get("lifecycle_status")) >= status_rank(current.get("lifecycle_status")):
            if current is not None:
                event["evidence_count"] = int(current.get("evidence_count") or 1) + 1
            else:
                event["evidence_count"] = int(event.get("evidence_count") or 1)
            by_key[key] = event
        elif current is not None:
            current["evidence_count"] = int(current.get("evidence_count") or 1) + 1
    return sorted(by_key.values(), key=lambda row: (str(row.get("alpha_id") or ""), str(row.get("event_id") or "")))


def event_merge_key(event: dict[str, Any]) -> str:
    alpha_id = event.get("alpha_id")
    if alpha_id:
        return f"alpha:{alpha_id}"
    expression = event.get("expression")
    if expression:
        return f"expr:{expression_hash(expression)}:{event.get('source_run_id') or ''}"
    return f"event:{event.get('event_id')}"


def build_history_experience_memory(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        expression = str(event.get("expression") or "").strip()
        if not expression:
            continue
        severity = event.get("severity")
        if severity not in {"block", "penalize"}:
            continue
        components = expression_components(expression)
        rows.append({
            "schema_version": SCHEMA_VERSION,
            "memory_kind": f"history_{severity}",
            "memory_type": "expression_exact" if severity == "block" else "expression_family",
            "severity": severity,
            "failure_kind": event.get("failure_kind"),
            "alpha_id": event.get("alpha_id"),
            "expression": expression,
            "expression_normalized": event.get("expression_normalized"),
            "expression_hash": expression_hash(expression),
            "pattern_signature": pattern_signature(expression),
            "fields": components["fields"],
            "operators": components["operators"],
            "source_type": event.get("source_type"),
            "source_file": event.get("source_file"),
            "source_run_id": event.get("source_run_id"),
            "retrieval_text": retrieval_text_for_event(event),
            "evidence_count": event.get("evidence_count") or 1,
            "created_at": _now(),
        })
    return _dedupe_memory_rows(rows)


def build_history_elite_archive(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        expression = str(event.get("expression") or "").strip()
        if not expression:
            continue
        lifecycle = str(event.get("lifecycle_status") or "")
        if lifecycle not in {"active", "submitted", "pre_submit_pass"} and event.get("severity") != "positive":
            continue
        rows.append({
            "schema_version": SCHEMA_VERSION,
            "alpha_id": event.get("alpha_id"),
            "expression": expression,
            "lifecycle_status": lifecycle,
            "source_type": event.get("source_type"),
            "source_file": event.get("source_file"),
            "sharpe": event.get("sharpe"),
            "fitness": event.get("fitness"),
            "returns": event.get("returns"),
            "turnover": event.get("turnover"),
            "sc_value": event.get("sc_value"),
            "prod_corr_value": event.get("prod_corr_value"),
            "created_at": event.get("created_at"),
        })
    return _dedupe_by_expression(rows)


def collect_pnl_curve_index(reports_dir: Path | str, *, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    status_by_id = {str(event.get("alpha_id")): event.get("lifecycle_status") for event in events if event.get("alpha_id")}
    rows: list[dict[str, Any]] = []
    for path in sorted(Path(reports_dir).rglob("*_pnl_curve.jsonl")):
        alpha_id = path.name.removesuffix("_pnl_curve.jsonl")
        curve = _read_jsonl(path)
        rows.append({
            "alpha_id": alpha_id,
            "path": str(path),
            "points": len(curve),
            "lifecycle_status": status_by_id.get(alpha_id),
        })
    return rows


def build_pnl_corr_islands(
    pnl_index: list[dict[str, Any]],
    *,
    min_overlap: int,
    island_abs_corr: float,
    warn_abs_corr: float,
) -> dict[str, Any]:
    curves = {row["alpha_id"]: _read_jsonl(Path(row["path"])) for row in pnl_index if row.get("alpha_id") and row.get("path")}
    parent = {alpha_id: alpha_id for alpha_id in curves}
    edges: list[dict[str, Any]] = []
    warn_edges: list[dict[str, Any]] = []
    ids = sorted(curves)
    for left_index, left_id in enumerate(ids):
        for right_id in ids[left_index + 1 :]:
            corr = aligned_daily_return_correlation(
                curves[left_id],
                curves[right_id],
                min_overlap=min_overlap,
                warn_abs_correlation=warn_abs_corr,
                reject_abs_correlation=island_abs_corr,
            )
            abs_corr = corr.get("abs_correlation")
            if abs_corr is None:
                continue
            edge = {
                "left_alpha_id": left_id,
                "right_alpha_id": right_id,
                "correlation": corr.get("correlation"),
                "abs_correlation": abs_corr,
                "overlap_days": corr.get("overlap_days"),
            }
            if abs_corr >= island_abs_corr:
                union(parent, left_id, right_id)
                edges.append(edge)
            elif abs_corr >= warn_abs_corr:
                warn_edges.append(edge)

    groups: dict[str, list[str]] = defaultdict(list)
    for alpha_id in ids:
        groups[find(parent, alpha_id)].append(alpha_id)
    islands = [
        {
            "island_id": f"pnl_island_{index:03d}",
            "members": sorted(members),
            "member_count": len(members),
        }
        for index, members in enumerate(sorted(groups.values(), key=lambda values: (-len(values), values[0])), start=1)
        if len(members) >= 2
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "alpha_count": len(ids),
        "island_abs_corr": island_abs_corr,
        "warn_abs_corr": warn_abs_corr,
        "min_overlap": min_overlap,
        "islands": islands,
        "edges": edges,
        "warn_edges": warn_edges,
    }


def write_history_to_ledger(events: list[dict[str, Any]], *, account: str) -> dict[str, Any]:
    return _run_coro_sync(_write_history_to_ledger_async(events, account=account))


async def _write_history_to_ledger_async(events: list[dict[str, Any]], *, account: str) -> dict[str, Any]:
    from .db import _get_session_factory, init_db
    from .wq_alpha_ledger import (
        record_api_check_record,
        record_find_only_entry,
        record_submitted_alpha_in_ledger,
    )

    counts: Counter[str] = Counter()
    errors: list[dict[str, Any]] = []
    await init_db()
    factory = _get_session_factory()
    async with factory() as session:
        try:
            for event in events:
                expression = str(event.get("expression") or "").strip()
                alpha_id = str(event.get("alpha_id") or "").strip()
                try:
                    lifecycle = str(event.get("lifecycle_status") or "")
                    if expression and alpha_id and lifecycle in {"active", "submitted"}:
                        settings = event.get("settings") or {}
                        await record_submitted_alpha_in_ledger(
                            session,
                            user_id=None,
                            alpha_id=alpha_id,
                            expression=expression,
                            region=settings.get("region", "USA"),
                            universe=settings.get("universe", "TOP3000"),
                            delay=int(settings.get("delay", 1)),
                            decay=int(settings.get("decay", 0)),
                            neutralization=settings.get("neutralization", "SUBINDUSTRY"),
                            truncation=float(settings.get("truncation", 0.08)),
                            sharpe=event.get("sharpe"),
                            fitness=event.get("fitness"),
                            returns=event.get("returns"),
                            turnover=event.get("turnover"),
                            tag=event.get("tag"),
                            status=lifecycle,
                        )
                        counts["submitted_alpha"] += 1
                    elif event.get("api_check_status"):
                        if await record_api_check_record(
                            session,
                            event,
                            settings={**(event.get("settings") or {}), "account": account},
                            source_run_id=event.get("source_run_id"),
                        ):
                            counts["api_check"] += 1
                        else:
                            counts["skipped_unmatched_api_check"] += 1
                    elif expression:
                        await record_find_only_entry(
                            session,
                            event,
                            settings={**(event.get("settings") or {}), "account": account},
                            source_run_id=event.get("source_run_id"),
                            source_file=event.get("source_file"),
                            source_type=event.get("source_type"),
                        )
                        counts["find_only"] += 1
                    else:
                        counts["skipped_missing_expression"] += 1
                except Exception as exc:
                    errors.append({"alpha_id": alpha_id, "source_file": event.get("source_file"), "error": str(exc)})
                    counts["errors"] += 1
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return {"ok": not errors, "write_ledger": True, "recorded": sum(counts.values()) - counts.get("errors", 0), "counts": dict(sorted(counts.items())), "errors": errors[:50]}


def build_history_profile_candidate(
    *,
    events: list[dict[str, Any]],
    memory: list[dict[str, Any]],
    pnl_islands: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    profile = default_research_profile()
    profile["candidate_key"] = "history_experience_candidate"
    profile["candidate_label"] = "historical_submit_failure_experience"
    profile["profile_version"] = int(profile.get("profile_version") or 0) + 1
    profile["updated_at"] = _now()
    profile["history_experience"] = {
        "source": str(output_dir),
        "event_count": len(events),
        "memory_count": len(memory),
        "pnl_island_count": len(pnl_islands.get("islands") or []),
        "policy": "balanced",
        "apply_guard": "candidate only; do not auto-apply real submit history to active profile",
    }
    biases = list(profile.get("priority_biases") or [])
    for value in ("history_experience_memory", "pnl_corr_island_decorrelation", "ledger_blocklist_sync"):
        if value not in biases:
            biases.append(value)
    profile["priority_biases"] = biases
    profile.setdefault("mine_defaults", {})["weak_memory_files"] = [str(output_dir / "canonical" / "history_experience_memory.jsonl")]
    profile.setdefault("mine_defaults", {})["no_real_submit"] = True
    return profile


def history_summary(
    *,
    config: WQHistoryExperienceConfig,
    inventory: list[dict[str, Any]],
    events: list[dict[str, Any]],
    memory: list[dict[str, Any]],
    elite: list[dict[str, Any]],
    pnl_islands: dict[str, Any],
    platform_result: dict[str, Any],
    ledger_report: dict[str, Any],
    files: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "created_at": _now(),
        "mode": "wq_history_experience_collect",
        "reports_dir": str(config.reports_dir),
        "output_dir": str(config.output_dir),
        "account": config.account,
        "check_policy": config.check_policy,
        "write_ledger": config.write_ledger,
        "local_file_count": len(inventory),
        "event_count": len(events),
        "memory_count": len(memory),
        "elite_count": len(elite),
        "pnl_curve_count": pnl_islands.get("alpha_count"),
        "pnl_island_count": len(pnl_islands.get("islands") or []),
        "platform": {
            "enabled": platform_result.get("enabled"),
            "ok": platform_result.get("ok"),
            "skipped": platform_result.get("skipped"),
            "reason": platform_result.get("reason"),
            "alpha_count": len(platform_result.get("alphas") or []),
            "check_count": len(platform_result.get("check_records") or []),
            "probe_count": len(platform_result.get("probe_summaries") or []),
        },
        "counts": {
            "lifecycle_status": dict(sorted(Counter(str(row.get("lifecycle_status") or "unknown") for row in events).items())),
            "failure_kind": dict(sorted(Counter(str(row.get("failure_kind") or "none") for row in events).items())),
            "severity": dict(sorted(Counter(str(row.get("severity") or "none") for row in events).items())),
            "source_type": dict(sorted(Counter(str(row.get("source_type") or "unknown") for row in events).items())),
        },
        "ledger": ledger_report,
        "files": files,
    }


def render_history_summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# WQ History Experience",
        "",
        f"- Events: {summary.get('event_count')}",
        f"- Memory rows: {summary.get('memory_count')}",
        f"- Elite rows: {summary.get('elite_count')}",
        f"- PnL islands: {summary.get('pnl_island_count')}",
        f"- Platform alphas: {(summary.get('platform') or {}).get('alpha_count')}",
        f"- Platform checks: {(summary.get('platform') or {}).get('check_count')}",
        f"- Ledger write: {(summary.get('ledger') or {}).get('write_ledger')}",
        "",
        "## Lifecycle",
        "",
    ]
    for key, value in (summary.get("counts", {}).get("lifecycle_status") or {}).items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Failure Kinds", ""])
    for key, value in (summary.get("counts", {}).get("failure_kind") or {}).items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines).rstrip() + "\n"


def canonical_failure_kind(
    row: dict[str, Any],
    *,
    api_status: str | None,
    platform_status: str,
    sc_result: str | None,
    prod_result: str | None,
    sc_value: float | None,
) -> str | None:
    raw = str(row.get("failure_kind") or row.get("review_failure_kind") or "").lower()
    reason = str(row.get("presubmit_reject_reason") or row.get("triage_reason") or row.get("status") or row.get("final_status") or "").lower()
    if platform_status in {"ACTIVE", "SUBMITTED"}:
        return "platform_alpha"
    if api_status in {"self_correlation_fail", "prod_correlation_fail", "platform_active_sc_above_cutoff", "platform_active_check_readable"}:
        return "platform_alpha" if api_status.startswith("platform_active") else api_status
    if raw in {"self_correlation", "self_correlation_high", "self_correlation_fail"}:
        return "self_correlation_fail"
    if raw in {"prod_correlation", "prod_correlation_fail"}:
        return "prod_correlation_fail"
    if raw in {"high_similarity", "too_similar_to_real_or_virtual_active"}:
        return "high_similarity"
    if prod_result == "FAIL" or "prod_correlation" in reason:
        return "prod_correlation_fail"
    if sc_result == "FAIL" or (sc_value is not None and sc_value >= 0.70) or "self_correlation" in reason:
        return "self_correlation_fail"
    nearest = _first_float(row.get("nearest_similarity"), _nested(row, "presubmit_gate", "nearest_similarity"))
    if nearest is not None and nearest > 0.65:
        return "high_similarity"
    if "too_similar" in reason or "duplicate" in reason or "skipped_similar" in reason:
        return "high_similarity"
    for check in row.get("failed_platform_checks") or []:
        name = str(check.get("name") or "").upper()
        if name in {"CONCENTRATED_WEIGHT"}:
            return "concentrated_weight"
        if name in {"LOW_SUB_UNIVERSE_SHARPE", "LOW_SUB_UNIVERSE_FITNESS"}:
            return "sub_universe_fail"
        if name == "LOW_SHARPE":
            return "low_sharpe"
        if name == "LOW_FITNESS":
            return "low_fitness"
        if name == "LOW_TURNOVER":
            return "low_turnover"
        if name == "HIGH_TURNOVER":
            return "high_turnover"
    if raw:
        return raw
    return None


def severity_for_failure(failure_kind: str | None, *, platform_status: str, source_type: str) -> str:
    if platform_status in {"ACTIVE", "SUBMITTED"} or failure_kind in BLOCK_FAILURES:
        return "block"
    if failure_kind in PENALIZE_FAILURES:
        return "penalize"
    if source_type == "presubmit_ready":
        return "positive"
    return "note"


def lifecycle_for_event(
    row: dict[str, Any],
    *,
    api_status: str | None,
    platform_status: str,
    source_type: str,
    severity: str,
) -> str:
    if platform_status == "ACTIVE":
        return "active"
    if platform_status == "SUBMITTED":
        return "submitted"
    if api_status == "self_correlation_fail":
        return "self_corr_fail"
    if api_status == "prod_correlation_fail":
        return "prod_corr_fail"
    if api_status == "api_check_pending":
        return "correlation_pending"
    if api_status == "api_check_failed":
        return "api_check_failed"
    status = str(row.get("status") or row.get("source_status") or "").lower()
    if status in {"eligible", "pre_submit_pass"} or source_type == "presubmit_ready":
        return "pre_submit_pass"
    if status == "pending_correlation_check":
        return "correlation_pending"
    if severity == "block" and (row.get("failure_kind") or "").lower() == "high_similarity":
        return "skipped_similar"
    if severity == "block":
        return "invalid"
    if severity == "penalize":
        return "weak"
    return "candidate"


def _api_status_from_row(
    row: dict[str, Any],
    sc_result: str | None,
    prod_result: str | None,
    sc_value: float | None,
    prod_value: float | None,
) -> str | None:
    failure = str(row.get("failure_kind") or row.get("review_failure_kind") or "").lower()
    detail = str(row.get("detail") or "").upper()
    if failure == "self_correlation" or sc_result == "FAIL" or "SELF_CORRELATION FAIL" in detail:
        return "self_correlation_fail"
    if failure == "prod_correlation" or prod_result == "FAIL" or "PROD_CORRELATION FAIL" in detail:
        return "prod_correlation_fail"
    if failure == "correlation_pending" or sc_result == "PENDING" or prod_result == "PENDING":
        return "api_check_pending"
    if str(row.get("platform_status") or row.get("status") or "").upper() == "ACTIVE":
        return "platform_active_check_readable"
    return None


def status_rank(status: Any) -> int:
    order = {
        "candidate": 0,
        "weak": 1,
        "invalid": 1,
        "api_check_failed": 2,
        "correlation_pending": 3,
        "pre_submit_pass": 4,
        "skipped_similar": 5,
        "self_corr_fail": 6,
        "prod_corr_fail": 6,
        "submitted": 7,
        "active": 8,
    }
    return order.get(str(status or ""), 0)


def retrieval_text_for_event(event: dict[str, Any]) -> str:
    return (
        f"{event.get('failure_kind') or event.get('lifecycle_status')}: "
        f"{event.get('source_family') or event.get('tag') or ''}; "
        f"sharpe={event.get('sharpe')} fitness={event.get('fitness')} "
        f"sc={event.get('sc_value')} prod={event.get('prod_corr_value')}"
    )


def source_run_id_from_file(source_file: str) -> str:
    if source_file.startswith("platform:"):
        return "platform_history"
    path = Path(source_file)
    if path.parent.name == "reports":
        return path.stem
    return path.parent.name


def stable_event_id(source_file: str, row_index: int | None, alpha_id: str | None, expression: str | None, source_type: str) -> str:
    raw = "|".join(str(value or "") for value in (source_file, row_index, alpha_id, expression, source_type))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def find(parent: dict[str, str], value: str) -> str:
    while parent[value] != value:
        parent[value] = parent[parent[value]]
        value = parent[value]
    return value


def union(parent: dict[str, str], left: str, right: str) -> None:
    left_root = find(parent, left)
    right_root = find(parent, right)
    if left_root != right_root:
        parent[right_root] = left_root


def _settings_from_row(row: dict[str, Any]) -> dict[str, Any]:
    settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
    return {
        **settings,
        "account": settings.get("account") or row.get("account"),
        "region": settings.get("region") or row.get("region"),
        "universe": settings.get("universe") or row.get("universe"),
        "delay": settings.get("delay") if settings.get("delay") is not None else row.get("delay"),
        "decay": settings.get("decay") if settings.get("decay") is not None else row.get("decay"),
        "neutralization": settings.get("neutralization") or row.get("neutralization"),
        "truncation": settings.get("truncation") if settings.get("truncation") is not None else row.get("truncation"),
    }


def _detail_value(detail: Any, kind: str) -> float | None:
    text = str(detail or "")
    if not text:
        return None
    if kind == "self":
        match = re.search(r"SELF_CORRELATION[^0-9]+(?:value=)?([0-9.]+)", text, flags=re.IGNORECASE)
    else:
        match = re.search(r"PROD_CORRELATION[^0-9]+(?:value=)?([0-9.]+)", text, flags=re.IGNORECASE)
    return _first_float(match.group(1)) if match else None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
    path.write_text(text + ("\n" if text else ""), encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys or ["empty"], extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _dedupe_memory_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, Any, Any]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (row.get("memory_type"), row.get("failure_kind"), row.get("expression_hash") or row.get("pattern_signature"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _dedupe_by_expression(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = expression_hash(str(row.get("expression") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is not None and str(value).strip() != "":
            return str(value)
    return None


def _first_float(*values: Any) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_bool(*values: Any) -> bool | None:
    for value in values:
        if value is None:
            continue
        return bool(value)
    return None


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _run_coro_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            result_box["result"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - defensive bridge
            error_box["error"] = exc

    import threading

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=300)
    if thread.is_alive():
        raise TimeoutError("timed out waiting for WQ history ledger write")
    if error_box:
        raise error_box["error"]
    return result_box.get("result")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
