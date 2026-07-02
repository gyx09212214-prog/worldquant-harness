"""Failure classification helpers for WorldQuant alpha mining."""

from __future__ import annotations

import hashlib
from typing import Any

from .expression_parser import normalize_expression
from .wq_expression_utils import expression_component_lists

CORRELATION_FAILURES = {"self_correlation_fail", "prod_correlation_fail"}
BLOCKING_FAILURES = CORRELATION_FAILURES | {"high_similarity", "platform_alpha", "duplicate"}

CHECK_FAILURE_KIND = {
    "LOW_SHARPE": "low_sharpe",
    "LOW_FITNESS": "low_fitness",
    "LOW_TURNOVER": "low_turnover",
    "HIGH_TURNOVER": "high_turnover",
    "CONCENTRATED_WEIGHT": "concentrated_weight",
    "LOW_SUB_UNIVERSE_SHARPE": "sub_universe_fail",
    "LOW_SUB_UNIVERSE_FITNESS": "sub_universe_fail",
}


def expression_hash(expression: str) -> str:
    normalized = normalize_expression(expression or "")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def params_hash(settings: dict | None) -> str:
    settings = normalized_settings(settings)
    raw = "|".join(f"{key}={settings[key]}" for key in sorted(settings))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def normalized_settings(settings: dict | None) -> dict:
    settings = settings or {}
    return {
        "account": str(settings.get("account") or "primary"),
        "region": str(settings.get("region") or "USA"),
        "universe": str(settings.get("universe") or "TOP3000"),
        "delay": int(settings.get("delay") if settings.get("delay") is not None else 1),
        "decay": int(settings.get("decay") if settings.get("decay") is not None else 0),
        "neutralization": str(settings.get("neutralization") or "SUBINDUSTRY"),
        "truncation": float(settings.get("truncation") if settings.get("truncation") is not None else 0.08),
    }


def pattern_signature(expression: str) -> str:
    components = expression_components(expression)
    fields = ",".join(components["fields"])
    operators = ",".join(components["operators"])
    return f"fields={fields}|ops={operators}"


def expression_components(expression: str) -> dict:
    return expression_component_lists(expression, strict=True)


def classify_failures(record: dict) -> list[dict]:
    """Classify a simulation/API-check record into canonical failure reasons."""
    failures: list[dict] = []
    api_status = str(record.get("api_check_status") or "").lower()
    status = str(record.get("status") or record.get("source_status") or "").lower()

    if api_status:
        failures.extend(_classify_api_status(record, api_status))

    if status in {"skipped_similar", "skipped_similar_to_hit"}:
        failures.append({
            "failure_kind": "high_similarity",
            "severity": "block",
            "reason": status,
            "value": _similarity_value(record),
        })

    if status == "failed_validation":
        failures.append({
            "failure_kind": "validation_error",
            "severity": "block",
            "reason": record.get("error") or "expression validation failed",
        })

    if status == "failed_correlation_check":
        failures.extend(_classify_correlation_checks(record))

    if status == "failed_platform_check":
        for check in record.get("failed_platform_checks") or []:
            failure = _classify_platform_check(check)
            if failure:
                failures.append(failure)

    if not failures and record.get("submit_eligible") is False:
        failures.extend(_classify_submit_checks(record.get("submit_checks") or {}))

    return _dedupe_failures(failures)


def lifecycle_status(record: dict) -> str:
    """Map a raw result/API record to the stable ledger lifecycle status."""
    api_status = str(record.get("api_check_status") or "").lower()
    status = str(record.get("status") or record.get("source_status") or "").lower()

    if api_status == "self_correlation_fail":
        return "self_corr_fail"
    if api_status == "prod_correlation_fail":
        return "prod_corr_fail"
    if api_status == "api_check_pending":
        return "correlation_pending"
    if api_status == "api_check_failed":
        return "api_check_failed"
    if api_status.startswith("platform_active"):
        return "active"
    if api_status == "api_check_readable" and record.get("source_submit_eligible") is True:
        return "pre_submit_pass"

    if status == "eligible":
        return "pre_submit_pass"
    if status == "pending_correlation_check":
        return "correlation_pending"
    if status == "failed_correlation_check":
        failures = {item["failure_kind"] for item in _classify_correlation_checks(record)}
        if "prod_correlation_fail" in failures:
            return "prod_corr_fail"
        if "self_correlation_fail" in failures:
            return "self_corr_fail"
        return "correlation_pending"
    if status == "failed_platform_check":
        return "weak"
    if status.startswith("skipped_similar"):
        return "skipped_similar"
    if status == "failed_validation":
        return "invalid"
    if status == "failed":
        return "invalid"
    if record.get("submit_eligible") is True:
        return "pre_submit_pass"
    if record.get("submit_eligible") is False:
        return "weak"
    return status if status else "candidate"


def primary_failure_kind(record: dict) -> str | None:
    failures = classify_failures(record)
    if not failures:
        return None
    block = [item for item in failures if item.get("severity") == "block"]
    chosen = block[0] if block else failures[0]
    return str(chosen.get("failure_kind") or "") or None


def memory_specs_for_record(record: dict, *, experiment_id: str | None = None) -> list[dict]:
    expression = record.get("expression") or ""
    if not expression:
        return []

    normalized = normalize_expression(expression)
    expr_hash = expression_hash(expression)
    components = expression_components(expression)
    signature = pattern_signature(expression)
    failures = classify_failures(record)
    specs: list[dict] = []

    for failure in failures:
        failure_kind = failure["failure_kind"]
        severity = failure["severity"]
        memory_type = "expression_exact"
        if failure_kind == "platform_alpha":
            memory_type = "platform_alpha"
        specs.append({
            "memory_type": memory_type,
            "scope": "global",
            "expression": expression,
            "expression_normalized": normalized,
            "expression_hash": expr_hash,
            "pattern_signature": signature,
            "fields": components["fields"],
            "operators": components["operators"],
            "failure_kind": failure_kind,
            "severity": severity,
            "evidence": {**failure, "alpha_id": record.get("alpha_id"), "status": record.get("status")},
            "source_experiment_ids": [experiment_id] if experiment_id else [],
        })

        if severity == "penalize":
            specs.append({
                "memory_type": "expression_family",
                "scope": "global",
                "expression": None,
                "expression_normalized": None,
                "expression_hash": None,
                "pattern_signature": signature,
                "fields": components["fields"],
                "operators": components["operators"],
                "failure_kind": failure_kind,
                "severity": "penalize",
                "evidence": {**failure, "alpha_id": record.get("alpha_id"), "status": record.get("status")},
                "source_experiment_ids": [experiment_id] if experiment_id else [],
            })

    return specs


def _classify_api_status(record: dict, api_status: str) -> list[dict]:
    if api_status == "self_correlation_fail":
        return [{
            "failure_kind": "self_correlation_fail",
            "severity": "block",
            "reason": "API check self-correlation failed",
            "value": record.get("sc_value"),
            "limit": 0.70,
        }]
    if api_status == "prod_correlation_fail":
        return [{
            "failure_kind": "prod_correlation_fail",
            "severity": "block",
            "reason": "API check production correlation failed",
            "value": record.get("prod_corr_value"),
        }]
    if api_status == "platform_active_sc_above_cutoff":
        return [{
            "failure_kind": "high_similarity",
            "severity": "block",
            "reason": "platform ACTIVE alpha has self-correlation above cutoff",
            "value": record.get("sc_value"),
            "limit": 0.70,
        }]
    if api_status.startswith("platform_active"):
        return [{
            "failure_kind": "platform_alpha",
            "severity": "block",
            "reason": "platform ACTIVE alpha should not be rediscovered",
            "value": record.get("sc_value"),
        }]
    if api_status == "api_check_failed":
        return [{
            "failure_kind": "api_check_failed",
            "severity": "note",
            "reason": record.get("error") or "API check failed",
        }]
    return []


def _classify_correlation_checks(record: dict) -> list[dict]:
    failures: list[dict] = []
    for key, failure_kind in [
        ("self_correlation", "self_correlation_fail"),
        ("prod_correlation", "prod_correlation_fail"),
    ]:
        check = record.get(key) or {}
        if str(check.get("result") or "").upper() == "FAIL":
            failures.append({
                "failure_kind": failure_kind,
                "severity": "block",
                "reason": f"{check.get('name') or key} failed",
                "value": check.get("value"),
                "limit": check.get("limit"),
            })
    return failures


def _classify_platform_check(check: dict) -> dict | None:
    name = str(check.get("name") or "").upper()
    kind = CHECK_FAILURE_KIND.get(name)
    if not kind:
        return None
    return {
        "failure_kind": kind,
        "severity": "penalize",
        "reason": f"{name} failed",
        "value": check.get("value"),
        "limit": check.get("limit"),
    }


def _classify_submit_checks(checks: dict) -> list[dict]:
    failures: list[dict] = []
    mapping = {
        "sharpe": "low_sharpe",
        "fitness": "low_fitness",
        "turnover_min": "low_turnover",
        "turnover_max": "high_turnover",
    }
    for key, kind in mapping.items():
        if checks.get(key) is False:
            failures.append({
                "failure_kind": kind,
                "severity": "penalize",
                "reason": f"submit check {key} failed",
            })
    return failures


def _similarity_value(record: dict) -> Any:
    for key in ("similarity_to_blocked", "similarity_to_hits", "similarity"):
        value = record.get(key)
        if isinstance(value, dict) and value.get("overall_similarity") is not None:
            return value.get("overall_similarity")
    return None


def _dedupe_failures(failures: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict] = []
    for failure in failures:
        key = (
            str(failure.get("failure_kind") or ""),
            str(failure.get("severity") or ""),
            str(failure.get("reason") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(failure)
    return deduped
