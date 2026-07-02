"""Stable identifiers and lifecycle helpers for WQ alpha efficiency tracking."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .expression_parser import normalize_expression
from .wq_expression_utils import field_signature
from .wq_failure_memory import expression_hash

IDENTITY_VERSION = 1


def normalized_efficiency_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    settings = settings or {}
    return {
        "account": str(settings.get("account") or "primary"),
        "region": str(settings.get("region") or "USA").upper(),
        "universe": str(settings.get("universe") or "TOP3000").upper(),
        "delay": _int_setting(settings.get("delay"), default=1),
        "decay": _int_setting(settings.get("decay"), default=0),
        "neutralization": str(settings.get("neutralization") or "SUBINDUSTRY").upper(),
        "truncation": _float_setting(settings.get("truncation"), default=0.08),
        "maxTrade": str(settings.get("maxTrade") or settings.get("max_trade") or "OFF").upper(),
        "maxPosition": str(settings.get("maxPosition") or settings.get("max_position") or "OFF").upper(),
    }


def settings_hash(settings: dict[str, Any] | None) -> str:
    normalized = normalized_efficiency_settings(settings)
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def candidate_uid(expression: str, settings: dict[str, Any] | None = None) -> str:
    expr_hash = expression_hash(expression or "")
    params = settings_hash(settings)
    raw = f"wq-alpha-candidate-v{IDENTITY_VERSION}|{expr_hash}|{params}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def settings_from_row(row: dict[str, Any], default_settings: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(default_settings or {})
    for key in (
        "account",
        "region",
        "universe",
        "delay",
        "decay",
        "neutralization",
        "truncation",
        "maxTrade",
        "maxPosition",
    ):
        if row.get(key) not in (None, ""):
            merged[key] = row.get(key)
    for nested_key in ("efficiency_settings", "effective_simulation_settings", "actual_simulation_settings", "simulation_settings"):
        nested = row.get(nested_key)
        if isinstance(nested, dict):
            merged.update({key: value for key, value in nested.items() if value not in (None, "")})
    gate = row.get("presubmit_gate")
    if isinstance(gate, dict):
        for key in ("region", "universe", "delay", "decay", "neutralization", "truncation"):
            if gate.get(key) not in (None, ""):
                merged[key] = gate.get(key)
    return normalized_efficiency_settings(merged)


def annotate_candidate_identity(
    row: dict[str, Any],
    default_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expression = str(row.get("expression") or "")
    settings = settings_from_row(row, default_settings)
    annotated = dict(row)
    annotated.setdefault("identity_version", IDENTITY_VERSION)
    annotated["expression_normalized"] = normalize_expression(expression) if expression else ""
    annotated["expression_hash"] = expression_hash(expression) if expression else ""
    annotated["efficiency_settings"] = settings
    annotated["settings_hash"] = settings_hash(settings)
    annotated["candidate_uid"] = str(row.get("candidate_uid") or candidate_uid(expression, settings)) if expression else ""
    annotated["field_signature"] = str(row.get("field_signature") or field_signature(expression))
    return annotated


def lifecycle_event(
    event_type: str,
    row: dict[str, Any],
    *,
    default_settings: dict[str, Any] | None = None,
    artifact_path: str | None = None,
    run_id: str | None = None,
    experiment_id: str | None = None,
) -> dict[str, Any]:
    annotated = annotate_candidate_identity(row, default_settings)
    event = {
        "event_type": event_type,
        "candidate_uid": annotated.get("candidate_uid"),
        "expression_hash": annotated.get("expression_hash"),
        "settings_hash": annotated.get("settings_hash"),
        "alpha_id": annotated.get("alpha_id"),
        "expression": annotated.get("expression"),
        "tag": annotated.get("tag"),
        "source_family": annotated.get("source_family"),
        "field_signature": annotated.get("field_signature"),
        "cycle_index": annotated.get("cycle_index"),
        "status": annotated.get("status"),
        "triage_bucket": annotated.get("triage_bucket"),
        "reason": _event_reason(annotated),
        "metrics": {
            "sharpe": annotated.get("sharpe"),
            "fitness": annotated.get("fitness"),
            "returns": annotated.get("returns"),
            "turnover": annotated.get("turnover"),
            "sc_value": annotated.get("sc_value"),
            "prod_corr_value": annotated.get("prod_corr_value"),
        },
        "efficiency_settings": annotated.get("efficiency_settings"),
    }
    if row.get("created_at"):
        event["created_at"] = row.get("created_at")
    if artifact_path:
        event["artifact_path"] = artifact_path
    if run_id:
        event["run_id"] = run_id
    if experiment_id:
        event["experiment_id"] = experiment_id
    return {key: value for key, value in event.items() if value not in (None, "", {}, [])}


def _event_reason(row: dict[str, Any]) -> str:
    return str(
        row.get("presubmit_reject_reason")
        or row.get("candidate_skip_reason")
        or row.get("reject_reason")
        or row.get("api_check_status")
        or row.get("triage_reason")
        or row.get("final_status")
        or row.get("platform_status")
        or ""
    )


def _int_setting(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_setting(value: Any, *, default: float) -> float:
    try:
        return round(float(value), 8)
    except (TypeError, ValueError):
        return default
