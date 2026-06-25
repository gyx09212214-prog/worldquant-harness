"""Normalize WorldQuant BRAIN review checks.

The platform can expose submission review state through several payload shapes:
submit responses, alpha detail responses, and scalar ``is`` fields. Keep the
shape handling here so callers can reason in terms of stable review keys.
"""

from __future__ import annotations

from typing import Any

CORRELATION_KINDS = ("self_correlation", "prod_correlation")
_RESULT_ORDER = {"FAIL": 4, "PENDING": 3, "WARNING": 2, "PASS": 1, "MISSING": 0}


def parse_review_checks(payload: dict | None) -> dict:
    """Return normalized self/prod correlation review checks from a WQ payload."""
    payload = payload or {}
    checks = _collect_checks(payload)

    normalized = {
        "self_correlation": _missing_check("SELF_CORRELATION"),
        "prod_correlation": _missing_check("PROD_CORRELATION"),
        "failed": [],
        "pending": [],
    }

    for kind in CORRELATION_KINDS:
        check = _select_check(checks, kind)
        scalar = _scalar_correlation(payload, kind)
        if check:
            normalized[kind] = check
        elif scalar is not None:
            normalized[kind] = {
                "name": "SELF_CORRELATION" if kind == "self_correlation" else "PROD_CORRELATION",
                "result": "MISSING",
                "value": scalar,
                "limit": None,
            }

        result = normalized[kind]["result"]
        if result == "FAIL":
            normalized["failed"].append(kind)
        elif result == "PENDING":
            normalized["pending"].append(kind)

    return normalized


def primary_failure_kind(review_checks: dict | None) -> str | None:
    """Return the most actionable failed correlation kind, if any."""
    failed = list((review_checks or {}).get("failed", []))
    if "prod_correlation" in failed:
        return "prod_correlation"
    if "self_correlation" in failed:
        return "self_correlation"
    return failed[0] if failed else None


def review_has_pending_correlation(review_checks: dict | None) -> bool:
    return bool((review_checks or {}).get("pending"))


def review_checks_passed(review_checks: dict | None) -> bool:
    """True when known correlation checks have passed and none are pending/failed."""
    review_checks = review_checks or {}
    if review_checks.get("failed") or review_checks.get("pending"):
        return False
    return any(
        (review_checks.get(kind) or {}).get("result") == "PASS"
        for kind in CORRELATION_KINDS
    )


def correlation_failure_detail(review_checks: dict, kind: str) -> str:
    check = review_checks.get(kind, {}) if review_checks else {}
    name = check.get("name") or kind.upper()
    value = check.get("value")
    limit = check.get("limit")
    if limit is None:
        return f"{name} FAIL: value={value}"
    return f"{name} FAIL: value={value} > limit={limit}"


def correlation_result_label(review_checks: dict | None) -> str:
    review_checks = review_checks or {}
    self_result = (review_checks.get("self_correlation") or {}).get("result", "MISSING")
    prod_result = (review_checks.get("prod_correlation") or {}).get("result", "MISSING")
    return f"SELF={self_result}, PROD={prod_result}"


def _missing_check(name: str) -> dict:
    return {"name": name, "result": "MISSING", "value": None, "limit": None}


def _collect_checks(payload: dict) -> list[dict]:
    checks: list[dict] = []

    def add_many(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    checks.append(dict(item))
        elif isinstance(value, dict):
            for name, item in value.items():
                if isinstance(item, dict):
                    row = dict(item)
                    row.setdefault("name", name)
                    checks.append(row)

    add_many(payload.get("checks"))
    is_data = payload.get("is")
    if isinstance(is_data, dict):
        add_many(is_data.get("checks"))
    return checks


def _select_check(checks: list[dict], kind: str) -> dict | None:
    matches = [_normalize_check(check) for check in checks if _check_matches(check, kind)]
    if not matches:
        return None
    return max(matches, key=lambda item: _RESULT_ORDER.get(item["result"], 0))


def _normalize_check(check: dict) -> dict:
    return {
        "name": str(check.get("name") or ""),
        "result": _normalize_result(check.get("result")),
        "value": _first_present(check, "value", "correlation", "score"),
        "limit": _first_present(check, "limit", "threshold"),
    }


def _normalize_result(value: Any) -> str:
    result = str(value or "MISSING").upper()
    if result in {"PASS", "FAIL", "PENDING", "WARNING"}:
        return result
    return "MISSING"


def _check_matches(check: dict, kind: str) -> bool:
    name = str(check.get("name") or "").upper()
    if kind == "self_correlation":
        return "SELF" in name and "CORRELATION" in name
    if kind == "prod_correlation":
        return "PROD" in name and "CORRELATION" in name
    return False


def _scalar_correlation(payload: dict, kind: str) -> Any:
    is_data = payload.get("is")
    if not isinstance(is_data, dict):
        return None
    keys = (
        ("selfCorrelation", "self_correlation")
        if kind == "self_correlation"
        else ("prodCorrelation", "prod_correlation")
    )
    for key in keys:
        value = is_data.get(key)
        if value is not None:
            if isinstance(value, dict):
                return _first_present(value, "value", "correlation", "score")
            return value
    return None


def _first_present(payload: dict, *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None
