"""Build complete read-only WorldQuant submission records.

This collector consolidates local run artifacts with platform read-only GET
responses. It deliberately avoids submit, simulate, delete, and check-only
submission endpoints.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .artifact_io import read_jsonl as _read_jsonl
from .artifact_io import utc_now as _now
from .artifact_io import write_csv as _write_csv
from .artifact_io import write_json as _write_json
from .artifact_io import write_jsonl as _write_jsonl
from .expression_parser import normalize_expression
from .record_utils import dedupe_rows_by_key as _dedupe_rows_by_key
from .record_utils import first_float as _safe_float
from .record_utils import first_text as _first_text
from .record_utils import nested as _nested
from .source_utils import source_run_id_from_platform_or_path as _source_run_id
from .wq_expression_utils import expression_components as _components
from .wq_platform_artifacts import fetch_platform_alphas as _fetch_platform_alphas
from .wq_platform_artifacts import local_file_inventory as _local_file_inventory
from .wq_review import parse_review_checks, primary_failure_kind

SCHEMA_VERSION = 1

LOCAL_ARTIFACT_NAMES = {
    "submit_existing_results.jsonl",
    "submit_results.jsonl",
    "submitted_accumulator.jsonl",
    "check_results.jsonl",
    "platform_check_results.jsonl",
    "simulation_results.jsonl",
    "review_queue.jsonl",
    "presubmit_ready_sequential.jsonl",
    "presubmit_rejected.jsonl",
    "platform_alphas.jsonl",
}

SUBMIT_SOURCE_TYPES = {
    "submit_existing_result",
    "submit_result",
    "submitted_accumulator",
}

CHECK_SOURCE_TYPES = {
    "check_result",
    "platform_check_result",
}

ACTIVE_STATUSES = {"ACTIVE", "SUBMITTED"}
SC_FAILURES = {"self_correlation", "self_correlation_fail", "sc_fail"}
PROD_FAILURES = {"prod_correlation", "prod_correlation_fail", "prod_corr_fail"}
NEGATIVE_FAILURES = {
    "self_correlation_fail",
    "prod_correlation_fail",
    "high_similarity",
    "concentrated_weight",
    "sub_universe_fail",
}
METRIC_KEYS = ("sharpe", "fitness", "returns", "turnover", "drawdown", "margin")


@dataclass(frozen=True)
class WQCompleteSubmissionRecordsConfig:
    reports_dir: Path
    output_dir: Path
    account: str = "primary"
    platform_enabled: bool = True
    detail_enabled: bool = True
    platform_limit: int = 0
    max_details: int = 0
    local_file_limit: int = 0
    record_limit: int = 0
    delay_seconds: float = 0.0
    chunk_size: int = 25


def collect_complete_submission_records(
    config: WQCompleteSubmissionRecordsConfig,
    *,
    client_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Collect and merge all known submission records into canonical artifacts."""

    output_dir = Path(config.output_dir)
    raw_dir = output_dir / "raw"
    canonical_dir = output_dir / "canonical"
    raw_dir.mkdir(parents=True, exist_ok=True)
    canonical_dir.mkdir(parents=True, exist_ok=True)

    local_files = discover_local_files(config.reports_dir)
    if config.local_file_limit > 0:
        local_files = local_files[: config.local_file_limit]
    inventory = local_file_inventory(local_files)
    local_events = collect_local_events(local_files, limit=config.record_limit)

    platform_result = collect_platform_readonly(
        config,
        local_events=local_events,
        client_factory=client_factory,
    )
    platform_events = [
        normalize_event(row, source_type="platform_alpha", source_file="platform:/users/self/alphas")
        for row in platform_result.get("alphas") or []
    ]
    detail_events = [
        normalize_event(_detail_payload(row), source_type="alpha_detail", source_file=f"platform:/alphas/{row.get('alpha_id')}")
        for row in platform_result.get("details") or []
        if row.get("ok")
    ]

    events = _dedupe_events([*local_events, *platform_events, *detail_events])
    alpha_records = build_alpha_records(events)
    active_records = [row for row in alpha_records if row.get("canonical_status") == "ACTIVE"]
    failure_records = [
        row for row in alpha_records
        if row.get("experience_label") in {"sc_hard_negative", "prod_hard_negative", "concentration_negative", "do_not_seed", "pending_uncertain"}
    ]
    memory = build_experience_memory(alpha_records, events)
    constraints = build_next_run_constraints(alpha_records, memory)

    files = {
        "local_file_inventory": str(raw_dir / "local_file_inventory.csv"),
        "platform_alphas": str(raw_dir / "platform_alphas.jsonl"),
        "alpha_details": str(raw_dir / "alpha_details.jsonl"),
        "submission_events": str(canonical_dir / "submission_events.jsonl"),
        "alpha_records": str(canonical_dir / "alpha_records.jsonl"),
        "active_records": str(canonical_dir / "active_records.jsonl"),
        "failure_records": str(canonical_dir / "failure_records.jsonl"),
        "experience_memory": str(canonical_dir / "experience_memory.jsonl"),
        "next_run_constraints": str(canonical_dir / "next_run_constraints.json"),
        "summary": str(output_dir / "summary.json"),
        "markdown": str(output_dir / "summary.md"),
    }
    _write_csv(Path(files["local_file_inventory"]), inventory, encoding="utf-8")
    _write_jsonl(Path(files["platform_alphas"]), platform_result.get("alphas") or [])
    _write_jsonl(Path(files["alpha_details"]), platform_result.get("details") or [])
    _write_jsonl(Path(files["submission_events"]), events)
    _write_jsonl(Path(files["alpha_records"]), alpha_records)
    _write_jsonl(Path(files["active_records"]), active_records)
    _write_jsonl(Path(files["failure_records"]), failure_records)
    _write_jsonl(Path(files["experience_memory"]), memory)
    _write_json(Path(files["next_run_constraints"]), constraints)

    summary = build_summary(
        config=config,
        inventory=inventory,
        events=events,
        alpha_records=alpha_records,
        active_records=active_records,
        failure_records=failure_records,
        memory=memory,
        platform_result=platform_result,
        files=files,
    )
    _write_json(Path(files["summary"]), summary)
    Path(files["markdown"]).write_text(render_complete_records_markdown(summary), encoding="utf-8")
    return summary


def discover_local_files(reports_dir: Path | str) -> list[Path]:
    root = Path(reports_dir)
    paths = [
        path
        for path in sorted(root.rglob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
        if path.name in LOCAL_ARTIFACT_NAMES
    ]
    return paths


def local_file_inventory(files: Iterable[Path]) -> list[dict[str, Any]]:
    return _local_file_inventory(files, source_type_for_path=source_type_for_path)


def collect_local_events(files: Iterable[Path], *, limit: int = 0) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in files:
        source_type = source_type_for_path(path)
        for row_index, row in enumerate(_read_jsonl(path)):
            event = normalize_event(row, source_type=source_type, source_file=str(path), row_index=row_index)
            if event.get("alpha_id") or event.get("expression"):
                events.append(event)
                if limit > 0 and len(events) >= limit:
                    return events
    return events


def source_type_for_path(path: Path | str) -> str:
    name = Path(path).name.lower()
    if name == "submit_existing_results.jsonl":
        return "submit_existing_result"
    if name == "submit_results.jsonl":
        return "submit_result"
    if name == "submitted_accumulator.jsonl":
        return "submitted_accumulator"
    if name == "check_results.jsonl":
        return "check_result"
    if name == "platform_check_results.jsonl":
        return "platform_check_result"
    if name == "simulation_results.jsonl":
        return "simulation_result"
    if name == "review_queue.jsonl":
        return "review_queue"
    if name == "presubmit_ready_sequential.jsonl":
        return "presubmit_ready"
    if name == "presubmit_rejected.jsonl":
        return "presubmit_rejected"
    if name == "platform_alphas.jsonl":
        return "local_platform_alpha"
    return "local_artifact"


def collect_platform_readonly(
    config: WQCompleteSubmissionRecordsConfig,
    *,
    local_events: list[dict[str, Any]],
    client_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    if not config.platform_enabled:
        return {"ok": False, "enabled": False, "skipped": True, "reason": "platform sync disabled", "alphas": [], "details": []}

    if client_factory is None:
        from .wq_brain_client import get_client, is_configured

        if not is_configured(config.account):
            return {
                "ok": False,
                "enabled": True,
                "skipped": True,
                "reason": f"WQ credentials not configured for {config.account}",
                "alphas": [],
                "details": [],
            }
        client_factory = get_client

    client = client_factory(config.account)
    try:
        if hasattr(client, "authenticate") and not client.authenticate():
            return {"ok": False, "enabled": True, "skipped": True, "reason": "WQ authentication failed", "alphas": [], "details": []}
        alphas = fetch_platform_alphas(client, limit=config.platform_limit)
        details: list[dict[str, Any]] = []
        if config.detail_enabled:
            detail_ids = detail_ids_to_fetch(local_events, alphas)
            details = fetch_alpha_details(
                client,
                detail_ids,
                max_details=config.max_details,
                delay_seconds=config.delay_seconds,
                chunk_size=config.chunk_size,
            )
        return {
            "ok": True,
            "enabled": True,
            "skipped": False,
            "reason": None,
            "alphas": alphas,
            "details": details,
        }
    finally:
        if hasattr(client, "close"):
            client.close()


def fetch_platform_alphas(client: Any, *, limit: int = 0) -> list[dict[str, Any]]:
    return _fetch_platform_alphas(client, limit=limit)


def detail_ids_to_fetch(local_events: list[dict[str, Any]], platform_alphas: list[dict[str, Any]]) -> list[str]:
    local_ids = {str(row.get("alpha_id")) for row in local_events if row.get("alpha_id")}
    platform_by_id = {str(row.get("id") or row.get("alpha_id")): row for row in platform_alphas if row.get("id") or row.get("alpha_id")}
    target_ids: set[str] = set(local_ids)
    for alpha_id, row in platform_by_id.items():
        status = str(row.get("status") or row.get("platform_status") or "").upper()
        if status in ACTIVE_STATUSES or row.get("dateSubmitted"):
            target_ids.add(alpha_id)
    return sorted(alpha_id for alpha_id in target_ids if _needs_detail(alpha_id, platform_by_id.get(alpha_id)))


def fetch_alpha_details(
    client: Any,
    alpha_ids: list[str],
    *,
    max_details: int = 0,
    delay_seconds: float = 0.0,
    chunk_size: int = 25,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    selected = alpha_ids[:max_details] if max_details > 0 else alpha_ids
    for index, alpha_id in enumerate(selected):
        if delay_seconds > 0 and index > 0 and index % max(1, chunk_size) == 0:
            time.sleep(delay_seconds)
        try:
            if hasattr(client, "get_alpha_raw"):
                payload = client.get_alpha_raw(alpha_id)
            else:
                payload = client.get_json(f"/alphas/{alpha_id}")
        except Exception as exc:  # pragma: no cover - defensive around external clients
            payload = {"ok": False, "error": str(exc)}
        records.append(_normalize_detail_record(alpha_id, payload))
    return records


def normalize_event(
    row: dict[str, Any],
    *,
    source_type: str,
    source_file: str,
    row_index: int | None = None,
) -> dict[str, Any]:
    expression = _expression_from(row)
    alpha_id = _alpha_id_from(row)
    status = _status_from(row)
    metrics = _metrics_from(row)
    review = _review_from(row)
    sc = review.get("self_correlation") or {}
    prod = review.get("prod_correlation") or {}
    failure_kind = _failure_kind(row, status=status, review=review)
    lifecycle = _lifecycle_status(status=status, failure_kind=failure_kind, source_type=source_type, row=row)
    attempt_kind = _attempt_kind(source_type=source_type, source_file=source_file, row=row)
    components = _components(expression)
    settings = _settings_from(row)
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": _event_id(source_file, row_index, alpha_id, expression, source_type),
        "alpha_id": alpha_id,
        "expression": expression,
        "expression_normalized": _normalize_expr(expression),
        "expression_hash": _hash(expression) if expression else None,
        "field_signature": "|".join(sorted(components["fields"])),
        "fields": sorted(components["fields"]),
        "operators": sorted(components["operators"]),
        "source_type": source_type,
        "source_file": source_file,
        "source_run_id": _source_run_id(source_file),
        "source_row_index": row_index,
        "attempt_kind": attempt_kind,
        "status": status,
        "lifecycle_status": lifecycle,
        "failure_kind": failure_kind or "none",
        "tag": _first_text(row.get("tag"), row.get("source_tag"), row.get("domain")),
        "source_family": _first_text(row.get("source_family"), row.get("mutation_strategy"), row.get("domain")),
        "created_at": _first_text(row.get("created_at"), row.get("dateCreated"), _nested(row, "result", "dateCreated")),
        "date_created": _first_text(row.get("dateCreated"), _nested(row, "result", "dateCreated")),
        "date_submitted": _first_text(row.get("dateSubmitted"), _nested(row, "result", "dateSubmitted")),
        "collected_at": _now(),
        "settings": settings,
        "metrics": metrics,
        "sharpe": metrics.get("sharpe"),
        "fitness": metrics.get("fitness"),
        "returns": metrics.get("returns"),
        "turnover": metrics.get("turnover"),
        "drawdown": metrics.get("drawdown"),
        "margin": metrics.get("margin"),
        "metric_count": _metric_count(metrics),
        "metrics_source_hint": source_type,
        "review_checks": review,
        "sc_result": sc.get("result"),
        "sc_value": _safe_float(sc.get("value"), row.get("sc_value"), _detail_value(row.get("detail"), "self")),
        "sc_limit": _safe_float(sc.get("limit"), row.get("sc_limit")),
        "prod_corr_result": prod.get("result"),
        "prod_corr_value": _safe_float(prod.get("value"), row.get("prod_corr_value"), row.get("prod_value"), _detail_value(row.get("detail"), "prod")),
        "prod_corr_limit": _safe_float(prod.get("limit"), row.get("prod_corr_limit"), row.get("prod_limit")),
        "subuniverse_value": _check_value(row, "LOW_SUB_UNIVERSE_SHARPE")[1],
        "subuniverse_limit": _check_value(row, "LOW_SUB_UNIVERSE_SHARPE")[2],
        "detail": _first_text(row.get("detail"), row.get("message"), row.get("error")),
    }
    return event


def build_alpha_records(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        alpha_id = str(event.get("alpha_id") or "")
        if alpha_id:
            grouped[alpha_id].append(event)

    rows = []
    for alpha_id, group in grouped.items():
        row = merge_alpha_events(alpha_id, group)
        row["experience_label"] = label_alpha_record(row)
        rows.append(row)
    return sorted(rows, key=lambda item: (str(item.get("date_submitted") or ""), str(item.get("date_created") or ""), str(item.get("alpha_id") or "")))


def merge_alpha_events(alpha_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    status_event = max(events, key=_status_score)
    metrics_event = max(events, key=_metrics_score)
    expression_event = max(events, key=_expression_score)
    review_event = max(events, key=_review_score)
    metrics = dict(metrics_event.get("metrics") or {})
    status = _canonical_status(events, status_event)
    failures = sorted({str(event.get("failure_kind") or "none") for event in events if event.get("failure_kind") and event.get("failure_kind") != "none"})
    source_files = sorted({str(event.get("source_file") or "") for event in events if event.get("source_file")})
    source_runs = sorted({str(event.get("source_run_id") or "") for event in events if event.get("source_run_id")})
    sc_value = _first_known(review_event.get("sc_value"), status_event.get("sc_value"), metrics_event.get("sc_value"))
    sub_value = _first_known(review_event.get("subuniverse_value"), status_event.get("subuniverse_value"), metrics_event.get("subuniverse_value"))
    sub_limit = _first_known(review_event.get("subuniverse_limit"), status_event.get("subuniverse_limit"), metrics_event.get("subuniverse_limit"))
    expression = str(expression_event.get("expression") or "")
    components = _components(expression)
    return {
        "schema_version": SCHEMA_VERSION,
        "alpha_id": alpha_id,
        "canonical_status": status,
        "lifecycle_statuses": dict(sorted(Counter(str(event.get("lifecycle_status") or "unknown") for event in events).items())),
        "statuses_seen": sorted({str(event.get("status") or "UNKNOWN") for event in events}),
        "failure_kinds_seen": failures,
        "expression": expression,
        "expression_normalized": _normalize_expr(expression),
        "expression_hash": _hash(expression) if expression else None,
        "field_signature": "|".join(sorted(components["fields"])),
        "fields": sorted(components["fields"]),
        "operators": sorted(components["operators"]),
        "settings": _best_settings(events),
        "metrics": metrics,
        "sharpe": metrics.get("sharpe"),
        "fitness": metrics.get("fitness"),
        "returns": metrics.get("returns"),
        "turnover": metrics.get("turnover"),
        "drawdown": metrics.get("drawdown"),
        "margin": metrics.get("margin"),
        "metrics_source": metrics_event.get("source_type"),
        "metrics_source_file": metrics_event.get("source_file"),
        "metric_count": _metric_count(metrics),
        "status_source": status_event.get("source_type"),
        "status_source_file": status_event.get("source_file"),
        "sc_result": review_event.get("sc_result"),
        "sc_value": sc_value,
        "sc_limit": _first_known(review_event.get("sc_limit"), status_event.get("sc_limit"), metrics_event.get("sc_limit")),
        "prod_corr_result": review_event.get("prod_corr_result"),
        "prod_corr_value": _first_known(review_event.get("prod_corr_value"), status_event.get("prod_corr_value"), metrics_event.get("prod_corr_value")),
        "prod_corr_limit": _first_known(review_event.get("prod_corr_limit"), status_event.get("prod_corr_limit"), metrics_event.get("prod_corr_limit")),
        "subuniverse_value": sub_value,
        "subuniverse_limit": sub_limit,
        "subuniverse_margin": _margin(sub_value, sub_limit),
        "date_created": _best_date(events, "date_created"),
        "date_submitted": _best_date(events, "date_submitted"),
        "source_files": source_files,
        "source_runs": source_runs,
        "evidence_count": len(events),
    }


def label_alpha_record(row: dict[str, Any]) -> str:
    status = str(row.get("canonical_status") or "").upper()
    failures = set(row.get("failure_kinds_seen") or [])
    sharpe = _safe_float(row.get("sharpe"))
    fitness = _safe_float(row.get("fitness"))
    turnover = _safe_float(row.get("turnover"))
    sc_value = _safe_float(row.get("sc_value"))
    sub_margin = _safe_float(row.get("subuniverse_margin"))
    if status == "ACTIVE":
        strong_metrics = (
            sharpe is not None and sharpe >= 1.60
            and fitness is not None and fitness >= 1.25
            and turnover is not None and 0.05 <= turnover <= 0.25
        )
        sc_ok = sc_value is None or sc_value <= 0.67
        sub_ok = sub_margin is None or sub_margin >= 0.20
        if strong_metrics and sc_ok and sub_ok:
            return "strong_seed_active"
        if sharpe is not None and fitness is not None and sharpe >= 1.45 and fitness >= 1.05:
            return "quality_active"
        return "threshold_active"
    if "self_correlation_fail" in failures or status == "SC_FAIL":
        return "sc_hard_negative"
    if "prod_correlation_fail" in failures or status == "PROD_CORR_FAIL":
        return "prod_hard_negative"
    if status in {"PRECHECK_PENDING", "CORR_PENDING"} or "correlation_pending" in failures:
        return "pending_uncertain"
    if "concentrated_weight" in failures or "sub_universe_fail" in failures:
        return "concentration_negative"
    return "do_not_seed"


def build_experience_memory(alpha_records: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    event_failures = Counter(
        (str(event.get("expression_hash") or ""), str(event.get("failure_kind") or "none"))
        for event in events
        if event.get("expression_hash")
    )
    for row in alpha_records:
        expression = str(row.get("expression") or "")
        if not expression:
            continue
        label = str(row.get("experience_label") or "")
        severity = _memory_severity(label)
        if severity == "ignore":
            continue
        rows.append({
            "schema_version": SCHEMA_VERSION,
            "memory_kind": f"complete_submission_{severity}",
            "memory_type": "expression_exact" if severity in {"block", "negative"} else "expression_family",
            "severity": severity,
            "experience_label": label,
            "alpha_id": row.get("alpha_id"),
            "expression": expression,
            "expression_normalized": row.get("expression_normalized"),
            "expression_hash": row.get("expression_hash"),
            "field_signature": row.get("field_signature"),
            "fields": row.get("fields") or [],
            "operators": row.get("operators") or [],
            "canonical_status": row.get("canonical_status"),
            "sharpe": row.get("sharpe"),
            "fitness": row.get("fitness"),
            "turnover": row.get("turnover"),
            "sc_value": row.get("sc_value"),
            "subuniverse_margin": row.get("subuniverse_margin"),
            "evidence_count": row.get("evidence_count"),
            "event_failure_counts": {
                failure: count
                for (expr_hash, failure), count in event_failures.items()
                if expr_hash == row.get("expression_hash")
            },
            "retrieval_text": _retrieval_text(row),
            "created_at": _now(),
        })
    return _dedupe_memory(rows)


def build_next_run_constraints(alpha_records: list[dict[str, Any]], memory: list[dict[str, Any]]) -> dict[str, Any]:
    preferred = [row["alpha_id"] for row in alpha_records if row.get("experience_label") == "strong_seed_active"]
    quality = [row["alpha_id"] for row in alpha_records if row.get("experience_label") == "quality_active"]
    threshold = [row["alpha_id"] for row in alpha_records if row.get("experience_label") == "threshold_active"]
    blocked = [row["alpha_id"] for row in alpha_records if row.get("experience_label") in {"sc_hard_negative", "prod_hard_negative", "do_not_seed"}]
    avoid_signatures = _top_values(
        row.get("field_signature")
        for row in alpha_records
        if row.get("experience_label") in {"sc_hard_negative", "prod_hard_negative", "concentration_negative"}
    )
    preferred_families = _family_counts([row for row in alpha_records if row.get("experience_label") in {"strong_seed_active", "quality_active"}])
    negative_families = _family_counts([row for row in alpha_records if row.get("experience_label") in {"sc_hard_negative", "concentration_negative"}])
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "preferred_seed_alpha_ids": preferred[:20],
        "quality_active_alpha_ids": quality[:30],
        "threshold_only_alpha_ids": threshold[:30],
        "blocked_alpha_ids": blocked[:100],
        "avoid_field_signatures": avoid_signatures[:50],
        "preferred_field_families": dict(preferred_families.most_common(12)),
        "negative_field_families": dict(negative_families.most_common(12)),
        "generation_notes": [
            "Final submit outcomes override check-only pass records.",
            "Use strong_seed_active as repair/expansion seeds only after changing field family or structure.",
            "Treat threshold_active as boundary evidence, not as a primary expansion anchor.",
            "Avoid exact or near-exact field signatures from sc_hard_negative records.",
            "Keep IV/PCR/news/event legs as small overlays unless strong active evidence says otherwise.",
        ],
        "memory_count": len(memory),
    }


def build_summary(
    *,
    config: WQCompleteSubmissionRecordsConfig,
    inventory: list[dict[str, Any]],
    events: list[dict[str, Any]],
    alpha_records: list[dict[str, Any]],
    active_records: list[dict[str, Any]],
    failure_records: list[dict[str, Any]],
    memory: list[dict[str, Any]],
    platform_result: dict[str, Any],
    files: dict[str, str],
) -> dict[str, Any]:
    actual_submit_events = [row for row in events if row.get("attempt_kind") == "actual_submit"]
    check_only_events = [row for row in events if row.get("attempt_kind") == "check_only"]
    daily = daily_submit_rates(actual_submit_events)
    periods = period_submit_rates(actual_submit_events)
    metric_complete = sum(1 for row in alpha_records if _metric_count(row.get("metrics") or {}) >= 4)
    active_metric_complete = sum(1 for row in active_records if _metric_count(row.get("metrics") or {}) >= 4)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "created_at": _now(),
        "mode": "wq_complete_submission_records",
        "read_only": True,
        "reports_dir": str(config.reports_dir),
        "output_dir": str(config.output_dir),
        "account": config.account,
        "local_file_count": len(inventory),
        "event_count": len(events),
        "alpha_count": len(alpha_records),
        "active_count": len(active_records),
        "failure_record_count": len(failure_records),
        "memory_count": len(memory),
        "coverage": {
            "metric_complete_count": metric_complete,
            "metric_complete_rate": _ratio(metric_complete, len(alpha_records)),
            "active_metric_complete_count": active_metric_complete,
            "active_metric_complete_rate": _ratio(active_metric_complete, len(active_records)),
        },
        "counts": {
            "canonical_status": dict(sorted(Counter(str(row.get("canonical_status") or "UNKNOWN") for row in alpha_records).items())),
            "experience_label": dict(sorted(Counter(str(row.get("experience_label") or "unknown") for row in alpha_records).items())),
            "event_source_type": dict(sorted(Counter(str(row.get("source_type") or "unknown") for row in events).items())),
            "event_attempt_kind": dict(sorted(Counter(str(row.get("attempt_kind") or "unknown") for row in events).items())),
            "event_failure_kind": dict(sorted(Counter(str(row.get("failure_kind") or "none") for row in events).items())),
        },
        "rates": {
            "actual_submit": _submit_rate_summary(actual_submit_events),
            "check_only": _check_rate_summary(check_only_events),
            "daily_actual_submit": daily,
            "period_actual_submit": periods,
        },
        "platform": {
            "enabled": platform_result.get("enabled"),
            "ok": platform_result.get("ok"),
            "skipped": platform_result.get("skipped"),
            "reason": platform_result.get("reason"),
            "alpha_count": len(platform_result.get("alphas") or []),
            "detail_count": len(platform_result.get("details") or []),
            "detail_ok_count": sum(1 for row in platform_result.get("details") or [] if row.get("ok")),
        },
        "top_active": _top_records(active_records, limit=20),
        "top_failures": _top_failure_records(failure_records, limit=20),
        "files": files,
    }


def daily_submit_rates(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        day = _event_day(event)
        buckets[day].append(event)
    return [_rate_row(day, rows) for day, rows in sorted(buckets.items())]


def period_submit_rates(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    periods = [
        ("May build-up", "2026-05-16", "2026-05-29"),
        ("Jun10-15 broad/fast submit", "2026-06-10", "2026-06-15"),
        ("Jun21-23 pending/history mining", "2026-06-21", "2026-06-23"),
        ("Jun24-25 experience-guided small batches", "2026-06-24", "2026-06-25"),
    ]
    rows = []
    for name, start, end in periods:
        selected = [event for event in events if start <= _event_day(event) <= end]
        item = _rate_row(name, selected)
        item["start"] = start
        item["end"] = end
        rows.append(item)
    return rows


def render_complete_records_markdown(summary: dict[str, Any]) -> str:
    coverage = summary.get("coverage") or {}
    counts = summary.get("counts") or {}
    rates = summary.get("rates") or {}
    platform = summary.get("platform") or {}
    lines = [
        "# WQ Complete Submission Records",
        "",
        "Read-only canonical submission ledger built from local artifacts and platform GET responses.",
        "",
        f"- Alpha records: {summary.get('alpha_count')}",
        f"- Events: {summary.get('event_count')}",
        f"- Active records: {summary.get('active_count')}",
        f"- Failure records: {summary.get('failure_record_count')}",
        f"- Active metric coverage: {coverage.get('active_metric_complete_count')}/{summary.get('active_count')} ({coverage.get('active_metric_complete_rate')})",
        f"- Platform alphas fetched: {platform.get('alpha_count')}",
        f"- Alpha details fetched: {platform.get('detail_count')} ok={platform.get('detail_ok_count')}",
        "",
        "## Status Counts",
        "",
    ]
    for key, value in (counts.get("canonical_status") or {}).items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Experience Labels", ""])
    for key, value in (counts.get("experience_label") or {}).items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Actual Submit Rate", ""])
    actual = rates.get("actual_submit") or {}
    lines.append(
        f"- attempts={actual.get('attempts')} active={actual.get('active')} "
        f"already={actual.get('already_submitted')} active_rate={actual.get('active_rate')}"
    )
    lines.extend(["", "## Period Rates", ""])
    for row in rates.get("period_actual_submit") or []:
        lines.append(
            f"- {row.get('bucket')}: attempts={row.get('attempts')} active={row.get('active')} "
            f"active_rate={row.get('active_rate')} sc_fail={row.get('sc_fail')} pending={row.get('pending')}"
        )
    lines.extend(["", "## Top Active", ""])
    for row in summary.get("top_active") or []:
        lines.append(
            f"- `{row.get('alpha_id')}` `{row.get('experience_label')}` "
            f"sharpe={row.get('sharpe')} fitness={row.get('fitness')} turnover={row.get('turnover')} sc={row.get('sc_value')}"
        )
    return "\n".join(lines).rstrip() + "\n"


def _submit_rate_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    return _rate_row("all", events)


def _check_rate_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(events)
    passed = sum(1 for event in events if event.get("status") in {"PRECHECK_PASS", "ACTIVE", "OK"})
    blocked = sum(1 for event in events if event.get("status") in {"PRECHECK_BLOCKED", "SC_FAIL"})
    return {
        "checks": total,
        "pass": passed,
        "blocked": blocked,
        "pass_rate": _ratio(passed, total),
    }


def _rate_row(bucket: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    attempts = len(events)
    active = sum(1 for event in events if event.get("status") == "ACTIVE")
    already = sum(1 for event in events if event.get("status") == "ALREADY_SUBMITTED" or event.get("failure_kind") == "already_submitted")
    sc_fail = sum(1 for event in events if event.get("status") == "SC_FAIL" or event.get("failure_kind") == "self_correlation_fail")
    pending = sum(1 for event in events if event.get("status") in {"PRECHECK_PENDING", "CORR_PENDING"} or event.get("failure_kind") == "correlation_pending")
    denominator = max(0, attempts - already)
    return {
        "bucket": bucket,
        "attempts": attempts,
        "active": active,
        "already_submitted": already,
        "sc_fail": sc_fail,
        "pending": pending,
        "active_rate": _ratio(active, attempts),
        "active_rate_ex_already": _ratio(active, denominator),
    }


def _dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_snapshot_key: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda row: (str(row.get("source_file") or ""), int(row.get("source_row_index") or 0), str(row.get("alpha_id") or ""))):
        source_type = str(event.get("source_type") or "")
        alpha_id = str(event.get("alpha_id") or "")
        if source_type in {"local_platform_alpha", "platform_alpha", "alpha_detail"} and alpha_id:
            key = f"{source_type}:{alpha_id}"
            current = by_snapshot_key.get(key)
            if current is None or _snapshot_event_score(event) > _snapshot_event_score(current):
                by_snapshot_key[key] = event
            continue
        key = "|".join(str(event.get(part) or "") for part in ("source_file", "source_row_index", "alpha_id", "status", "expression_hash"))
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    out.extend(by_snapshot_key.values())
    return sorted(out, key=lambda row: (str(row.get("alpha_id") or ""), str(row.get("source_type") or ""), str(row.get("source_file") or "")))


def _snapshot_event_score(event: dict[str, Any]) -> tuple[int, int, int, str]:
    return (
        _status_score(event)[0],
        _metric_count(event.get("metrics") or {}),
        _source_priority(event.get("source_type")),
        str(event.get("date_submitted") or event.get("date_created") or event.get("collected_at") or ""),
    )


def _normalize_detail_record(alpha_id: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"alpha_id": alpha_id, "ok": False, "error": "non-dict response", "data": payload}
    if "data" in payload and ("ok" in payload or "status_code" in payload):
        return {
            "alpha_id": alpha_id,
            "ok": bool(payload.get("ok")),
            "status_code": payload.get("status_code"),
            "error": payload.get("error"),
            "data": payload.get("data") if isinstance(payload.get("data"), dict) else {},
        }
    return {
        "alpha_id": alpha_id,
        "ok": bool(payload.get("ok", True)),
        "status_code": payload.get("status_code"),
        "error": payload.get("error"),
        "data": payload,
    }


def _detail_payload(row: dict[str, Any]) -> dict[str, Any]:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    payload = dict(data)
    payload.setdefault("alpha_id", row.get("alpha_id"))
    payload.setdefault("id", row.get("alpha_id"))
    return payload


def _needs_detail(alpha_id: str, platform_row: dict[str, Any] | None) -> bool:
    if not alpha_id:
        return False
    if platform_row is None:
        return True
    return _metric_count(_metrics_from(platform_row)) < 4 or not _expression_from(platform_row)


def _canonical_status(events: list[dict[str, Any]], status_event: dict[str, Any]) -> str:
    statuses = {str(event.get("status") or "").upper() for event in events}
    if "ACTIVE" in statuses:
        return "ACTIVE"
    if "SUBMITTED" in statuses:
        return "SUBMITTED"
    if "SC_FAIL" in statuses or any(event.get("failure_kind") == "self_correlation_fail" for event in events):
        return "SC_FAIL"
    if "PROD_CORR_FAIL" in statuses or any(event.get("failure_kind") == "prod_correlation_fail" for event in events):
        return "PROD_CORR_FAIL"
    if "ALREADY_SUBMITTED" in statuses:
        return "ALREADY_SUBMITTED"
    if "PRECHECK_PENDING" in statuses or any(event.get("failure_kind") == "correlation_pending" for event in events):
        return "PRECHECK_PENDING"
    if "PRECHECK_BLOCKED" in statuses:
        return "PRECHECK_BLOCKED"
    return str(status_event.get("status") or "UNKNOWN").upper()


def _status_score(event: dict[str, Any]) -> tuple[int, int, int]:
    status_rank = {
        "ACTIVE": 100,
        "SUBMITTED": 95,
        "SC_FAIL": 90,
        "PROD_CORR_FAIL": 90,
        "ALREADY_SUBMITTED": 80,
        "PRECHECK_PASS": 70,
        "PRECHECK_BLOCKED": 65,
        "PRECHECK_PENDING": 60,
        "PLATFORM_CHECK_FAIL": 55,
        "SIMULATED": 30,
        "UNKNOWN": 0,
    }
    return (
        status_rank.get(str(event.get("status") or "").upper(), 0),
        _source_priority(event.get("source_type")),
        _metric_count(event.get("metrics") or {}),
    )


def _metrics_score(event: dict[str, Any]) -> tuple[int, int, int]:
    return (
        _metric_count(event.get("metrics") or {}),
        _source_priority(event.get("source_type")),
        _status_score(event)[0],
    )


def _expression_score(event: dict[str, Any]) -> tuple[int, int, int]:
    return (
        1 if event.get("expression") else 0,
        _source_priority(event.get("source_type")),
        _metric_count(event.get("metrics") or {}),
    )


def _review_score(event: dict[str, Any]) -> tuple[int, int, int]:
    known = sum(1 for key in ("sc_result", "sc_value", "prod_corr_result", "prod_corr_value", "subuniverse_value") if event.get(key) is not None)
    failure_bonus = 2 if event.get("failure_kind") in {"self_correlation_fail", "prod_correlation_fail"} else 0
    return known + failure_bonus, _source_priority(event.get("source_type")), _status_score(event)[0]


def _source_priority(source_type: Any) -> int:
    order = {
        "alpha_detail": 100,
        "platform_alpha": 95,
        "local_platform_alpha": 85,
        "submit_existing_result": 80,
        "submit_result": 80,
        "submitted_accumulator": 78,
        "platform_check_result": 70,
        "check_result": 65,
        "presubmit_ready": 55,
        "review_queue": 45,
        "simulation_result": 40,
        "presubmit_rejected": 35,
    }
    return order.get(str(source_type or ""), 0)


def _memory_severity(label: str) -> str:
    if label == "strong_seed_active":
        return "positive"
    if label in {"quality_active", "threshold_active"}:
        return "note"
    if label in {"sc_hard_negative", "prod_hard_negative"}:
        return "block"
    if label in {"concentration_negative", "pending_uncertain", "do_not_seed"}:
        return "negative"
    return "ignore"


def _retrieval_text(row: dict[str, Any]) -> str:
    return (
        f"{row.get('experience_label')}: {row.get('canonical_status')} "
        f"alpha={row.get('alpha_id')} sharpe={row.get('sharpe')} fitness={row.get('fitness')} "
        f"turnover={row.get('turnover')} sc={row.get('sc_value')} fields={','.join((row.get('fields') or [])[:8])}"
    )


def _dedupe_memory(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _dedupe_rows_by_key(
        rows,
        lambda row: (str(row.get("expression_hash") or ""), str(row.get("experience_label") or "")),
    )


def _top_values(values: Iterable[Any]) -> list[str]:
    counts = Counter(str(value) for value in values if value)
    return [value for value, _ in counts.most_common()]


def _family_counts(rows: list[dict[str, Any]]) -> Counter:
    counts: Counter = Counter()
    for row in rows:
        for field in row.get("fields") or []:
            counts[_field_family(field)] += 1
    return counts


def _field_family(field: str) -> str:
    text = str(field or "").lower()
    if any(token in text for token in ("pcr", "implied_volatility", "option", "put", "call")):
        return "options_iv_pcr"
    if any(token in text for token in ("eps", "sales", "revenue", "ebit", "income", "profit", "book", "cash", "flow", "dividend", "debt", "leverage", "asset", "liab", "gross", "fcf")):
        return "fundamental"
    if any(token in text for token in ("analyst", "estimate", "target", "rating", "anl4", "mdl")):
        return "analyst_model"
    if any(token in text for token in ("news", "snt", "sentiment", "buzz", "social")):
        return "news_sentiment"
    if any(token in text for token in ("risk", "credit", "beta", "volatility")):
        return "risk_credit_vol"
    if text in {"industry", "sector", "subindustry", "market"}:
        return "grouping"
    if text in {"open", "close", "high", "low", "volume", "vwap", "returns", "adv20"}:
        return "price_volume"
    return "other"


def _top_records(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    def score(row: dict[str, Any]) -> float:
        return (_safe_float(row.get("sharpe")) or 0.0) + (_safe_float(row.get("fitness")) or 0.0)

    return [
        {
            "alpha_id": row.get("alpha_id"),
            "experience_label": row.get("experience_label"),
            "sharpe": row.get("sharpe"),
            "fitness": row.get("fitness"),
            "turnover": row.get("turnover"),
            "sc_value": row.get("sc_value"),
            "fields": (row.get("fields") or [])[:8],
        }
        for row in sorted(rows, key=score, reverse=True)[:limit]
    ]


def _top_failure_records(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    return [
        {
            "alpha_id": row.get("alpha_id"),
            "experience_label": row.get("experience_label"),
            "canonical_status": row.get("canonical_status"),
            "failure_kinds_seen": row.get("failure_kinds_seen"),
            "sharpe": row.get("sharpe"),
            "fitness": row.get("fitness"),
            "turnover": row.get("turnover"),
            "sc_value": row.get("sc_value"),
        }
        for row in rows[:limit]
    ]


def _event_day(event: dict[str, Any]) -> str:
    for key in ("date_submitted", "date_created", "created_at", "collected_at"):
        value = str(event.get(key) or "")
        if len(value) >= 10 and re.match(r"20\d{2}-\d{2}-\d{2}", value[:10]):
            return value[:10]
    return "unknown"


def _attempt_kind(*, source_type: str, source_file: str, row: dict[str, Any]) -> str:
    detail = str(row.get("detail") or "").lower()
    run = _source_run_id(source_file).lower()
    if source_type in SUBMIT_SOURCE_TYPES:
        if "check-only" in detail or "_check" in run:
            return "check_only"
        return "actual_submit"
    if source_type in CHECK_SOURCE_TYPES:
        return "check_only"
    if source_type == "presubmit_ready":
        return "presubmit_ready"
    if source_type == "presubmit_rejected":
        return "presubmit_rejected"
    if source_type in {"platform_alpha", "local_platform_alpha", "alpha_detail"}:
        return "platform_snapshot"
    return "candidate"


def _status_from(row: dict[str, Any]) -> str:
    raw_status = _first_text(row.get("final_status"), row.get("platform_status"), row.get("status"), row.get("source_status"))
    detail = str(row.get("detail") or "").lower()
    raw = str(raw_status or "").upper()
    failure = str(row.get("failure_kind") or row.get("review_failure_kind") or "").lower()
    if raw in ACTIVE_STATUSES or "submitted and active" in detail or row.get("ok") is True and "active" in detail:
        return "ACTIVE"
    if failure in SC_FAILURES or "self_correlation fail" in detail:
        return "SC_FAIL"
    if failure in PROD_FAILURES or "prod_correlation fail" in detail:
        return "PROD_CORR_FAIL"
    if failure == "already_submitted" or "already_submitted" in detail or "already submitted" in detail:
        return "ALREADY_SUBMITTED"
    if failure == "correlation_pending" or raw in {"PRECHECK_PENDING", "PENDING_CORRELATION_CHECK"}:
        return "PRECHECK_PENDING"
    if raw in {"PRECHECK_PASS", "ELIGIBLE"}:
        return "PRECHECK_PASS"
    if raw in {"PRECHECK_BLOCKED", "FAILED_PLATFORM_CHECK", "PLATFORM_CHECK_FAIL"}:
        return "PRECHECK_BLOCKED"
    if raw:
        return raw
    if row.get("ok") is True:
        return "OK"
    if row.get("ok") is False:
        return "FAIL"
    return "UNKNOWN"


def _failure_kind(row: dict[str, Any], *, status: str, review: dict[str, Any]) -> str | None:
    failure = str(row.get("failure_kind") or row.get("review_failure_kind") or "").lower()
    if failure in SC_FAILURES:
        return "self_correlation_fail"
    if failure in PROD_FAILURES:
        return "prod_correlation_fail"
    if failure == "correlation_pending":
        return "correlation_pending"
    if failure == "already_submitted":
        return "already_submitted"
    primary = primary_failure_kind(review)
    if primary == "self_correlation":
        return "self_correlation_fail"
    if primary == "prod_correlation":
        return "prod_correlation_fail"
    detail = str(row.get("detail") or row.get("message") or row.get("error") or "").lower()
    if "self_correlation fail" in detail:
        return "self_correlation_fail"
    if "prod_correlation fail" in detail:
        return "prod_correlation_fail"
    if "pending" in detail:
        return "correlation_pending"
    if "already" in detail and "submitted" in detail:
        return "already_submitted"
    for check in _all_checks(row):
        name = str(check.get("name") or "").upper()
        result = str(check.get("result") or "").upper()
        if result not in {"FAIL", "ERROR", "PENDING"}:
            continue
        if name == "SELF_CORRELATION":
            return "self_correlation_fail" if result == "FAIL" else "correlation_pending"
        if name == "PROD_CORRELATION":
            return "prod_correlation_fail" if result == "FAIL" else "correlation_pending"
        if name == "CONCENTRATED_WEIGHT":
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
    if status == "SC_FAIL":
        return "self_correlation_fail"
    if status == "PROD_CORR_FAIL":
        return "prod_correlation_fail"
    if status == "PRECHECK_PENDING":
        return "correlation_pending"
    if failure:
        return failure
    return None


def _lifecycle_status(*, status: str, failure_kind: str | None, source_type: str, row: dict[str, Any]) -> str:
    if status == "ACTIVE":
        return "active"
    if status == "SUBMITTED":
        return "submitted"
    if failure_kind == "self_correlation_fail":
        return "self_corr_fail"
    if failure_kind == "prod_correlation_fail":
        return "prod_corr_fail"
    if failure_kind == "correlation_pending":
        return "correlation_pending"
    if source_type == "presubmit_ready" or status == "PRECHECK_PASS":
        return "pre_submit_pass"
    if source_type == "presubmit_rejected" or failure_kind in NEGATIVE_FAILURES:
        return "invalid"
    if status == "SIMULATED":
        return "candidate"
    return "candidate"


def _review_from(row: dict[str, Any]) -> dict[str, Any]:
    review = row.get("review_checks") if isinstance(row.get("review_checks"), dict) else {}
    if review and ("self_correlation" in review or "prod_correlation" in review):
        normalized = {
            "self_correlation": review.get("self_correlation") or {"name": "SELF_CORRELATION", "result": "MISSING", "value": None, "limit": None},
            "prod_correlation": review.get("prod_correlation") or {"name": "PROD_CORRELATION", "result": "MISSING", "value": None, "limit": None},
            "failed": list(review.get("failed") or []),
            "pending": list(review.get("pending") or []),
        }
    else:
        normalized = parse_review_checks(row)
    if row.get("sc_value") is not None:
        normalized["self_correlation"] = {
            **(normalized.get("self_correlation") or {}),
            "name": "SELF_CORRELATION",
            "value": row.get("sc_value"),
            "limit": row.get("sc_limit"),
            "result": (normalized.get("self_correlation") or {}).get("result") or "MISSING",
        }
    if row.get("prod_value") is not None or row.get("prod_corr_value") is not None:
        normalized["prod_correlation"] = {
            **(normalized.get("prod_correlation") or {}),
            "name": "PROD_CORRELATION",
            "value": _first_known(row.get("prod_corr_value"), row.get("prod_value")),
            "limit": _first_known(row.get("prod_corr_limit"), row.get("prod_limit")),
            "result": (normalized.get("prod_correlation") or {}).get("result") or "MISSING",
        }
    return normalized


def _all_checks(row: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    holders = [row, row.get("is"), row.get("live_precheck"), row.get("raw_check"), row.get("precheck")]
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        values = None
        if isinstance(holder.get("is"), dict):
            values = holder["is"].get("checks")
        if values is None:
            values = holder.get("checks")
        if isinstance(values, dict):
            values = list(values.values())
        if isinstance(values, list):
            checks.extend(item for item in values if isinstance(item, dict))
    for value in row.get("failed_platform_checks") or []:
        if isinstance(value, dict):
            checks.append(value)
    return checks


def _check_value(row: dict[str, Any], name: str) -> tuple[str | None, float | None, float | None]:
    matches = []
    for check in _all_checks(row):
        if str(check.get("name") or "").upper() == name:
            matches.append((str(check.get("result") or "") or None, _safe_float(check.get("value")), _safe_float(check.get("limit"))))
    return matches[-1] if matches else (None, None, None)


def _metrics_from(row: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for holder in (
        row.get("candidate_metrics"),
        row.get("metrics"),
        row.get("is"),
        row.get("is_metrics"),
        _nested(row, "result", "wq_brain"),
        _nested(row, "result", "is_metrics"),
        row,
    ):
        if not isinstance(holder, dict):
            continue
        for key in METRIC_KEYS:
            value = holder.get(key)
            if key == "returns" and value is None:
                value = holder.get("wq_returns")
            if key == "sharpe" and value is None:
                value = holder.get("wq_sharpe")
            if key == "fitness" and value is None:
                value = holder.get("wq_fitness")
            if key == "turnover" and value is None:
                value = holder.get("wq_turnover")
            if value is not None and metrics.get(key) is None:
                metrics[key] = _safe_float(value)
    return {key: metrics.get(key) for key in METRIC_KEYS if metrics.get(key) is not None}


def _metric_count(metrics: dict[str, Any]) -> int:
    return sum(1 for key in ("sharpe", "fitness", "returns", "turnover") if metrics.get(key) is not None)


def _settings_from(row: dict[str, Any]) -> dict[str, Any]:
    settings = dict(row.get("settings") or {}) if isinstance(row.get("settings"), dict) else {}
    for key in ("region", "universe", "delay", "decay", "neutralization", "truncation"):
        if row.get(key) is not None and settings.get(key) is None:
            settings[key] = row.get(key)
    return settings


def _best_settings(events: list[dict[str, Any]]) -> dict[str, Any]:
    event = max(events, key=lambda row: (len(row.get("settings") or {}), _source_priority(row.get("source_type"))))
    return dict(event.get("settings") or {})


def _expression_from(row: dict[str, Any]) -> str:
    value = _first_text(
        row.get("expression"),
        _nested(row, "regular", "code"),
        row.get("regular") if isinstance(row.get("regular"), str) else None,
        row.get("code"),
        _nested(row, "result", "expression"),
    )
    return str(value or "").strip()


def _alpha_id_from(row: dict[str, Any]) -> str | None:
    value = _first_text(row.get("alpha_id"), row.get("id"), _nested(row, "result", "alpha_id"))
    return str(value).strip() if value else None


def _normalize_expr(expression: str) -> str:
    if not expression:
        return ""
    try:
        return normalize_expression(expression)
    except Exception:
        return re.sub(r"\s+", "", expression.lower())


def _hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]


def _event_id(source_file: str, row_index: int | None, alpha_id: str | None, expression: str, source_type: str) -> str:
    return _hash("|".join(str(value or "") for value in (source_file, row_index, alpha_id, expression, source_type)))


def _best_date(events: list[dict[str, Any]], key: str) -> str | None:
    values = [str(event.get(key)) for event in events if event.get(key)]
    return sorted(values)[-1] if values else None


def _margin(value: Any, limit: Any) -> float | None:
    left = _safe_float(value)
    right = _safe_float(limit)
    if left is None or right is None:
        return None
    return round(left - right, 6)


def _detail_value(detail: Any, kind: str) -> float | None:
    text = str(detail or "")
    if not text:
        return None
    if kind == "self":
        match = re.search(r"SELF_CORRELATION[^0-9]+(?:value=)?([0-9.]+)", text, flags=re.IGNORECASE)
    else:
        match = re.search(r"PROD_CORRELATION[^0-9]+(?:value=)?([0-9.]+)", text, flags=re.IGNORECASE)
    return _safe_float(match.group(1)) if match else None


def _first_known(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _ratio(num: int, den: int) -> float | None:
    if den <= 0:
        return None
    return round(num / den, 4)
