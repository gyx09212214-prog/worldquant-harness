"""Review, metric, and sorting helpers for WQ workflow stages."""

from __future__ import annotations

from typing import Any

from .record_utils import first_float as _first_float
from .record_utils import safe_float as _safe_float
from .wq_repair_screening import repair_candidate_concentration_risk
from .wq_workflow_constants import (
    ACTIVE_OR_SUBMITTED,
    BLOCKED_REPAIR_MUTATION_STRATEGIES,
    BLOCKED_REPAIR_SOURCE_FAMILIES,
    CONFIRMED_READY,
    HARD_FAIL,
    NEAR_MISS_REPAIR,
    SUBMIT_PROBE_NEEDED,
)


def _score(value: Any, default: float = float("-inf")) -> float:
    parsed = _safe_float(value)
    return default if parsed is None else parsed


def review_sort_key(row: dict) -> tuple:
    bucket_score = {
        CONFIRMED_READY: 0,
        SUBMIT_PROBE_NEEDED: 1,
        NEAR_MISS_REPAIR: 2,
        ACTIVE_OR_SUBMITTED: 3,
        HARD_FAIL: 4,
    }.get(row.get("triage_bucket"), 9)
    return (
        bucket_score,
        -_score(row.get("temporal_stability_score"), default=50),
        -_score(row.get("fitness")),
        -_score(row.get("sharpe")),
        _score(row.get("turnover"), default=999),
    )


def _repair_candidate_sort_key(row: dict) -> tuple:
    action_score = {
        "allow": 0,
        "penalize": 1,
        "block": 2,
    }.get(str(row.get("forum_policy_action") or "").lower(), 3)
    return (
        action_score,
        -_score(row.get("repair_priority_score")),
        -_score(row.get("research_priority_score")),
        str(row.get("tag") or ""),
    )


def _repair_candidate_block_reason(row: dict) -> str:
    expression = str(row.get("expression") or "")
    family = str(row.get("source_family") or "")
    strategy = str(row.get("mutation_strategy") or "")
    tag = str(row.get("tag") or "")
    if family in BLOCKED_REPAIR_SOURCE_FAMILIES or strategy in BLOCKED_REPAIR_MUTATION_STRATEGIES:
        return "settings_only_or_smooth_original_repair"
    lowered_tag = tag.lower()
    if "metric-retest" in lowered_tag or "metric-smooth" in lowered_tag or lowered_tag.endswith("smooth-industry"):
        return "settings_only_or_smooth_original_repair"
    concentration_risk = repair_candidate_concentration_risk(expression)
    if concentration_risk:
        row["repair_concentration_risk"] = concentration_risk
        return "repair_concentration_sparse_group_risk"
    return ""


def _api_check_status(check_result: dict, *, sc_result: Any, prod_result: Any) -> str:
    if not check_result:
        return "api_check_missing"
    failure = str(check_result.get("review_failure_kind") or check_result.get("failure_kind") or "")
    if failure == "self_correlation" or str(sc_result).upper() == "FAIL":
        return "self_correlation_fail"
    if failure == "prod_correlation" or str(prod_result).upper() == "FAIL":
        return "prod_correlation_fail"
    if str(sc_result).upper() == "PENDING" or str(prod_result).upper() == "PENDING" or failure == "correlation_pending":
        return "api_check_pending"
    if str(check_result.get("status") or "").upper() in {"ACTIVE", "SUBMITTED"}:
        return "platform_active_check_readable"
    return "api_check_readable"


def _needs_check(row: dict) -> bool:
    return bool(row.get("alpha_id")) and str(row.get("status") or "") in {
        "eligible",
        "pending_correlation_check",
        "pre_submit_pass",
    }


def _row_can_submit(row: dict | None, *, allow_submit_probe: bool) -> bool:
    if not row:
        return False
    if row.get("triage_bucket") == CONFIRMED_READY:
        return True
    return allow_submit_probe and row.get("triage_bucket") == SUBMIT_PROBE_NEEDED


def _metrics_from_result(result: dict) -> dict:
    wq = result.get("wq_brain") if isinstance(result.get("wq_brain"), dict) else {}
    is_metrics = result.get("is_metrics") if isinstance(result.get("is_metrics"), dict) else {}
    return {
        "sharpe": _first_float(wq.get("wq_sharpe"), is_metrics.get("sharpe"), result.get("sharpe")),
        "fitness": _first_float(wq.get("wq_fitness"), is_metrics.get("fitness"), result.get("fitness")),
        "returns": _first_float(wq.get("wq_returns"), is_metrics.get("returns"), result.get("returns")),
        "turnover": _first_float(wq.get("wq_turnover"), is_metrics.get("turnover"), result.get("turnover")),
    }


def _failed_platform_checks(checks: list[dict]) -> list[dict]:
    ignored = {"SELF_CORRELATION", "PROD_CORRELATION", "MATCHES_COMPETITION"}
    return [
        check for check in checks
        if str(check.get("result") or "").upper() == "FAIL" and str(check.get("name") or "").upper() not in ignored
    ]


def _review_check(checks: list[dict], name: str) -> dict | None:
    for check in checks:
        if str(check.get("name") or "").upper() == name:
            return check
    return None


def _check_result(check: dict | None) -> str:
    return str((check or {}).get("result") or "").upper()


def _is_simulation_timeout_result(result: dict) -> bool:
    error = str((result or {}).get("error") or "").lower()
    return "simulation polling timeout" in error or "wq simulation polling timeout" in error


def _is_repairable_platform_fail(row: dict, failed_checks: list[dict]) -> bool:
    names = {str(check.get("name") or "").upper() for check in failed_checks}
    if bool(names & {"CONCENTRATED_WEIGHT", "LOW_SUB_UNIVERSE_SHARPE", "LOW_SUB_UNIVERSE_FITNESS"}):
        return _score(row.get("sharpe")) >= 1.5 and _score(row.get("fitness")) >= 1.0
    if bool(names & {"LOW_SHARPE", "LOW_FITNESS"}):
        return _is_metric_near_miss({
            "sharpe": row.get("sharpe"),
            "fitness": row.get("fitness"),
            "turnover": row.get("turnover"),
        })
    return False


def _is_metric_near_miss(metrics: dict) -> bool:
    sharpe = _score(metrics.get("sharpe"), default=0)
    fitness = _score(metrics.get("fitness"), default=0)
    turnover = metrics.get("turnover")
    turnover_ok = turnover is not None and 0.005 <= turnover <= 0.8
    return turnover_ok and sharpe >= 1.15 and fitness >= 0.85


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]
