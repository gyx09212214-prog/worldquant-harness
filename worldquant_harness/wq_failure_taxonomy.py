"""Shared WQ failure, check, and community-route taxonomy."""

from __future__ import annotations

from typing import Any, Iterable

from .record_utils import first_float as _first_float
from .record_utils import nested as _nested

SELF_CORRELATION_CHECK = "SELF_CORRELATION"
PROD_CORRELATION_CHECK = "PROD_CORRELATION"
CONCENTRATION_CHECKS = {"CONCENTRATED_WEIGHT"}
SUB_UNIVERSE_CHECKS = {"LOW_SUB_UNIVERSE_SHARPE", "LOW_SUB_UNIVERSE_FITNESS"}
TURNOVER_CHECKS = {"HIGH_TURNOVER", "LOW_TURNOVER"}
METRIC_CHECKS = {"LOW_SHARPE", "LOW_FITNESS"}
LOW_COVERAGE_PREFIXES = ("rp_css_", "rp_ess_", "pcr_")

CHECK_FAILURE_KIND_PRIORITY = (
    (SELF_CORRELATION_CHECK, "self_correlation_fail"),
    (PROD_CORRELATION_CHECK, "prod_correlation_fail"),
    ("CONCENTRATED_WEIGHT", "concentrated_weight"),
    ("LOW_SUB_UNIVERSE_SHARPE", "sub_universe_fail"),
    ("LOW_SUB_UNIVERSE_FITNESS", "sub_universe_fail"),
    ("HIGH_TURNOVER", "high_turnover"),
    ("LOW_TURNOVER", "low_turnover"),
    ("LOW_SHARPE", "low_sharpe"),
    ("LOW_FITNESS", "low_fitness"),
)

ROOT_CAUSE_NEXT_ACTION = {
    "self_correlation": "change field/operator family before more window tuning",
    "prod_correlation": "block current signature and restart from a new source family",
    "duplicate_or_similarity": "block duplicate signature and diversify field family",
    "policy_block": "transform template grammar before simulation",
    "pending_check": "refresh readable platform check before submit",
    "distribution_concentration": "reduce sparse legs and add broad dispersion before retest",
    "turnover_density": "tune smoothing and participation together",
    "subuniverse_coverage": "add high-coverage breadth before submit",
    "platform_check": "classify failed check and route to repair bucket",
    "metric_fail": "demote weak standalone signal to overlay or drop",
    "legal_input": "probe legal fields/operators before broader simulation",
    "infra_timeout": "retry with longer polling budget",
}


def failed_check_names(row: dict[str, Any], *, include_passed: bool = False) -> list[str]:
    checks = row.get("failed_platform_checks") or row.get("failed_checks") or []
    failed_source = bool(checks)
    if not checks:
        checks = row.get("is_checks") or row.get("checks") or []
    if not checks and isinstance(row.get("result"), dict):
        checks = ((row.get("result") or {}).get("is_metrics") or {}).get("checks") or []
    names = []
    for item in checks:
        if isinstance(item, dict):
            result = str(item.get("result") or "").upper()
            if failed_source or include_passed or result == "FAIL":
                name = str(item.get("name") or "").upper()
                if name:
                    names.append(name)
        elif item:
            names.append(str(item).upper())
    return sorted(set(names))


def failure_kind_from_check_names(checks: Iterable[str]) -> str | None:
    check_set = {str(check).upper() for check in checks if check}
    for check, failure in CHECK_FAILURE_KIND_PRIORITY:
        if check in check_set:
            return failure
    return None


def canonical_failure_kind(
    row: dict[str, Any],
    *,
    api_status: str | None = None,
    platform_status: str = "",
    sc_result: str | None = None,
    prod_result: str | None = None,
    sc_value: float | None = None,
    similarity_cutoff: float = 0.65,
) -> str | None:
    raw = str(row.get("failure_kind") or row.get("review_failure_kind") or "").lower()
    reason = str(row.get("presubmit_reject_reason") or row.get("triage_reason") or row.get("status") or row.get("final_status") or "").lower()
    api = str(api_status or row.get("api_check_status") or "").lower()
    platform = str(platform_status or row.get("platform_status") or row.get("status") or "").upper()
    sc = str(sc_result or row.get("sc_result") or "").upper()
    prod = str(prod_result or row.get("prod_corr_result") or "").upper()
    sc_float = _first_float(sc_value, row.get("sc_value"), _nested(row, ("self_correlation", "value")))
    if platform in {"ACTIVE", "SUBMITTED"}:
        return "platform_alpha"
    if api in {"self_correlation_fail", "prod_correlation_fail", "platform_active_sc_above_cutoff", "platform_active_check_readable"}:
        return "platform_alpha" if api.startswith("platform_active") else api
    if raw in {"self_correlation", "self_correlation_high", "self_correlation_fail"}:
        return "self_correlation_fail"
    if raw in {"prod_correlation", "prod_correlation_fail"}:
        return "prod_correlation_fail"
    if raw in {"high_similarity", "too_similar_to_real_or_virtual_active"}:
        return "high_similarity"
    if prod == "FAIL" or "prod_correlation" in reason:
        return "prod_correlation_fail"
    if sc == "FAIL" or (sc_float is not None and sc_float >= 0.70) or "self_correlation" in reason:
        return "self_correlation_fail"
    nearest = _first_float(row.get("nearest_similarity"), _nested(row, ("presubmit_gate", "nearest_similarity")))
    if nearest is not None and nearest > similarity_cutoff:
        return "high_similarity"
    if "too_similar" in reason or "duplicate" in reason or "skipped_similar" in reason:
        return "high_similarity"
    check_failure = failure_kind_from_check_names(failed_check_names(row))
    if check_failure:
        return check_failure
    if raw:
        return raw
    return None


def audit_root_cause(row: dict[str, Any], *, stage: str = "", failed_checks: Iterable[str] | None = None) -> str:
    checks = {str(name).upper() for name in (failed_checks or failed_check_names(row)) if name}
    text = " ".join(str(row.get(key) or "") for key in (
        "failure_kind",
        "review_failure_kind",
        "triage_reason",
        "presubmit_reject_reason",
        "candidate_skip_reason",
        "api_check_status",
        "forum_policy_reason",
        "status",
    )).lower()
    failure = canonical_failure_kind(row)
    if failure in {"self_correlation_fail", "self_correlation_high"}:
        return "self_correlation"
    if failure == "prod_correlation_fail":
        return "prod_correlation"
    if failure == "high_similarity":
        return "duplicate_or_similarity"
    if failure == "concentrated_weight":
        return "distribution_concentration"
    if failure == "sub_universe_fail":
        return "subuniverse_coverage"
    if failure in {"high_turnover", "low_turnover"}:
        return "turnover_density"
    if failure in {"low_sharpe", "low_fitness", "base_metric_fail"}:
        return "metric_fail"
    if "legal" in text or "unsupported" in text or "unknown_or_unsupported" in text:
        return "legal_input"
    if "too_similar" in text or "duplicate" in text or "similarity" in text:
        return "duplicate_or_similarity"
    if "policy" in text or "template" in text or "forum_direct" in text:
        return "policy_block"
    if "pending" in text or "missing" in text or "not_readable" in text:
        return "pending_check"
    if "timeout" in text:
        return "infra_timeout"
    if checks & CONCENTRATION_CHECKS or "concentrat" in text or "sparse" in text:
        return "distribution_concentration"
    if checks & TURNOVER_CHECKS or "turnover" in text:
        return "turnover_density"
    if checks & SUB_UNIVERSE_CHECKS or "subuniverse" in text or "sub_universe" in text:
        return "subuniverse_coverage"
    if checks or "platform check" in text:
        return "platform_check"
    if "metric" in text or "threshold" in text or "not submit eligible" in text:
        return "metric_fail"
    if stage in {"candidate_skipped", "presubmit_rejected"}:
        return "policy_block"
    return "none"


def next_action_for_root_cause(root_cause: str, *, hints: Iterable[Any] | None = None) -> str:
    if root_cause in ROOT_CAUSE_NEXT_ACTION:
        return ROOT_CAUSE_NEXT_ACTION[root_cause]
    for hint in hints or []:
        if hint:
            return str(hint)
    return "continue current gate"


def community_skill_route_for_flags(flags: Iterable[Any]) -> list[str]:
    values = {str(flag) for flag in flags if flag}
    routes: list[str] = []
    if values & {"metric_near_pass"}:
        routes.extend(["community::near_pass_repair", "community_failure::metric_near_pass_overlay_repair"])
    if "correlation_risk" in values:
        routes.append(
            "community_failure::correlation_near_pass_or_highscore_repair"
            if "metric_near_pass" in values
            else "community_failure::correlation_similarity_block_or_family_shift"
        )
    if values & {"template_clone_risk", "possible_complete_alpha", "private_code"}:
        routes.extend(["community::alpha_template_transform", "community_failure::template_clone_blocker"])
    if values & {"high_turnover", "low_turnover", "unit_check", "platform_limit", "operator_availability_risk"}:
        routes.append("community::operation_attribution")
    if values & {"high_turnover", "low_turnover"}:
        routes.append("community_failure::turnover_density_repair")
    if values & {"unit_check", "platform_limit", "operator_availability_risk", "unknown_or_unsupported"}:
        routes.append("community_failure::operator_platform_unit_probe")
    if values & {"correlation_risk", "stale_precheck_risk", "field_family_crowding", "unknown_or_unsupported"}:
        routes.append("community::submission_gate")
    if "stale_precheck_risk" in values:
        routes.append("community_failure::pending_check_not_submit_ready")
    if "field_family_crowding" in values:
        routes.extend([
            "community_failure::correlation_similarity_block_or_family_shift",
            "community_failure::concentration_sparse_leg_or_distribution_repair",
        ])
    return list(dict.fromkeys(routes))


def community_repair_annotations(row: dict[str, Any], *, near_miss_bucket: str = "near_miss_repair") -> dict[str, list[str]]:
    flags = {
        str(flag)
        for key in ("risk_flags", "community_skill_risk_flags")
        for flag in _as_list(row.get(key))
        if flag
    }
    tags = community_skill_route_for_flags(flags)
    failure_tags: list[str] = []
    hints: list[str] = []
    reason_text = " ".join(str(row.get(key) or "") for key in ("triage_reason", "status", "api_check_status")).lower()
    failed_names = set(failed_check_names(row))
    if row.get("triage_bucket") == near_miss_bucket:
        tags.extend(["community::near_pass_repair", "community_failure::metric_near_pass_overlay_repair"])
        failure_tags.append("near_pass_repair")
        hints.append("preserve_economic_idea_before_broad_regeneration")
    if "self-correlation" in reason_text or str(row.get("sc_result") or "").upper() == "FAIL":
        tags.append("community_failure::correlation_near_pass_or_highscore_repair")
        failure_tags.append("near_pass_self_corr")
        hints.append("change_field_or_operator_family_before_window_tuning")
    if failed_names & {"LOW_FITNESS", "LOW_SHARPE", "LOW_SUB_UNIVERSE_SHARPE"}:
        tags.append("community_failure::metric_near_pass_overlay_repair")
        failure_tags.append("metric_threshold_near_pass")
        hints.append("keep_structure_and_try_small_settings_or_smoothing_grid")
    if failed_names & SUB_UNIVERSE_CHECKS:
        tags.append("community_failure::subuniverse_coverage_breadth_repair")
        failure_tags.append("subuniverse_coverage_breadth")
        hints.append("add_high_coverage_breadth_before_submit")
    if failed_names & CONCENTRATION_CHECKS:
        tags.append("community_failure::concentration_sparse_leg_or_distribution_repair")
        failure_tags.append("concentration_sparse_leg")
        hints.append("reduce_sparse_legs_before_truncation_only_retest")
    if "HIGH_TURNOVER" in failed_names or "high_turnover" in flags:
        tags.extend(["community::operation_attribution", "community_failure::turnover_density_repair"])
        failure_tags.append("high_turnover")
        hints.append("reduce_trading_speed_with_decay_trade_when_or_hump")
    if "LOW_TURNOVER" in failed_names or "low_turnover" in flags:
        tags.extend(["community::operation_attribution", "community_failure::turnover_density_repair"])
        failure_tags.append("low_turnover")
        hints.append("increase_signal_refresh_or_relax_trade_condition")
    if "template_clone_risk" in flags:
        tags.extend(["community::alpha_template_transform", "community_failure::template_clone_blocker"])
        failure_tags.append("template_clone_risk")
        hints.append("require_field_family_or_operator_family_transform")
    if "field_family_crowding" in flags:
        tags.extend([
            "community::submission_gate",
            "community_failure::correlation_similarity_block_or_family_shift",
            "community_failure::concentration_sparse_leg_or_distribution_repair",
        ])
        failure_tags.append("field_family_crowding")
        hints.append("limit_same_field_signature_budget")
    return {
        "community_skill_tags": list(dict.fromkeys(tags)),
        "skill_failure_tags": list(dict.fromkeys(failure_tags)),
        "repair_strategy_hints": list(dict.fromkeys(hints)),
    }


def has_low_coverage_field(fields: Iterable[Any]) -> bool:
    return any(str(field).startswith(LOW_COVERAGE_PREFIXES) for field in fields)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]

