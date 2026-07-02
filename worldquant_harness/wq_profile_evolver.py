"""Profile-level evolution for WQ research mining runs."""

from __future__ import annotations

import copy
import math
from typing import Any

from .artifact_io import utc_now as _now
from .record_utils import safe_float as _safe_float
from .wq_research_profile import profile_to_gate, profile_to_mine_config

PROFILE_EVOLUTION_SCHEMA_VERSION = 1


def evolve_research_profile(
    active_profile: dict[str, Any],
    summary: dict[str, Any],
    *,
    field_signature_blacklist: list[str] | None = None,
    min_improvement: float = 0.02,
) -> dict[str, Any]:
    """Generate A/B/C profile candidates from the latest harness summary."""

    metrics = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else summary
    metrics = metrics or {}
    baseline_score = _safe_float(summary.get("harness_score"))
    if baseline_score is None:
        baseline_score = profile_harness_score(metrics)

    blacklist = [str(value) for value in (field_signature_blacklist or []) if value]
    candidates = {
        "candidate_a": _candidate_a(active_profile, metrics, blacklist),
        "candidate_b": _candidate_b(active_profile, metrics, blacklist),
        "candidate_c": _candidate_c(active_profile, metrics, blacklist),
    }
    for key, candidate in candidates.items():
        candidate["score"] = score_profile_candidate(candidate["profile"], metrics, key, baseline_score=baseline_score)
        candidate["mine_config_overrides"] = profile_to_mine_config(candidate["profile"])
        candidate["gate_overrides"] = profile_to_gate(candidate["profile"])
        candidate["recommended"] = candidate["score"] >= baseline_score + min_improvement

    best_key = max(candidates, key=lambda key: candidates[key]["score"])
    recommended = best_key if candidates[best_key]["recommended"] else best_key
    candidates[recommended]["recommended"] = True
    return {
        "schema_version": PROFILE_EVOLUTION_SCHEMA_VERSION,
        "created_at": _now(),
        "active_profile_name": active_profile.get("profile_name"),
        "active_profile_version": active_profile.get("profile_version"),
        "baseline_score": round(baseline_score, 6),
        "min_improvement": min_improvement,
        "recommended_candidate": recommended,
        "candidates": candidates,
        "decision_note": "Recommended profile candidates are local-only; apply explicitly before a real mining run.",
    }


def profile_harness_score(metrics: dict[str, Any]) -> float:
    """Score harness metrics on the same rough scale as the harness report."""

    ready_value = min((_safe_float(metrics.get("ready_per_100_simulations")) or 0.0) / 5.0, 1.0)
    submit_value = _safe_float(metrics.get("promote_submit_success_rate"))
    self_value = 1.0 - min(_safe_float(metrics.get("self_correlation_reject_share")) or 0.0, 1.0)
    similar_value = 1.0 - min(_safe_float(metrics.get("too_similar_reject_share")) or 0.0, 1.0)
    diversity_value = 1.0 - min(_safe_float(metrics.get("field_signature_duplicate_ratio")) or 0.0, 1.0)
    weighted = [
        (ready_value, 0.35),
        (submit_value, 0.20),
        (self_value, 0.15),
        (similar_value, 0.15),
        (diversity_value, 0.15),
    ]
    available = [(value, weight) for value, weight in weighted if value is not None and math.isfinite(value)]
    if not available:
        return 0.0
    total_weight = sum(weight for _, weight in available)
    return round(sum(value * weight for value, weight in available) / total_weight, 6)


def score_profile_candidate(
    profile: dict[str, Any],
    metrics: dict[str, Any],
    candidate_key: str,
    *,
    baseline_score: float | None = None,
) -> float:
    """Deterministically score a candidate profile against current blockers."""

    base = baseline_score if baseline_score is not None else profile_harness_score(metrics)
    self_share = _safe_float(metrics.get("self_correlation_reject_share")) or 0.0
    similar_share = _safe_float(metrics.get("too_similar_reject_share")) or 0.0
    illegal_share = _safe_float(metrics.get("illegal_input_reject_share")) or 0.0
    duplicate_count = int(metrics.get("duplicate_field_signature_count") or 0)
    ready_per_100 = _safe_float(metrics.get("ready_per_100_simulations")) or 0.0
    submit_rate = _safe_float(metrics.get("promote_submit_success_rate"))
    biases = set(profile.get("priority_biases") or [])
    signature_blacklist = set((profile.get("field_signature_policy") or {}).get("blacklist") or [])

    score = base
    if self_share >= 0.25 and {"cross_domain_overlay", "standalone_low_overlap_research_family"} & biases:
        score += 0.04
    if similar_share >= 0.20 and "low_overlap_field_family" in biases:
        score += 0.04
    if duplicate_count > 0 and signature_blacklist:
        score += 0.03
    if illegal_share >= 0.08 and bool((profile.get("legal_input_policy") or {}).get("strict", True)):
        score += 0.03
    if ready_per_100 < 1.0 and self_share < 0.25 and similar_share < 0.25 and candidate_key == "candidate_c":
        score += 0.05
    if submit_rate is not None and submit_rate < 0.50 and (profile.get("promotion_gate") or {}).get("min_ready", 1) >= 2:
        score += 0.02
    if candidate_key == "candidate_c" and (self_share >= 0.30 or similar_share >= 0.30):
        score -= 0.03
    return round(max(0.0, min(score, 1.0)), 6)


def _candidate_a(active_profile: dict[str, Any], metrics: dict[str, Any], blacklist: list[str]) -> dict[str, Any]:
    profile = _candidate_base(active_profile, "candidate_a", "conservative_constraint_tightening")
    actions: list[dict[str, Any]] = []
    self_share = _safe_float(metrics.get("self_correlation_reject_share")) or 0.0
    similar_share = _safe_float(metrics.get("too_similar_reject_share")) or 0.0
    illegal_share = _safe_float(metrics.get("illegal_input_reject_share")) or 0.0
    duplicate_count = int(metrics.get("duplicate_field_signature_count") or 0)

    if self_share >= 0.25:
        _set_family_count(profile, -2, floor=3)
        _set_signature_count(profile, -1, floor=2)
        _append_bias(profile, "cross_domain_overlay")
        _append_bias(profile, "standalone_low_overlap_research_family")
        actions.append(_action("self_correlation_reject_share", self_share, "tighten family and field-signature reuse"))
    if similar_share >= 0.20:
        _lower_similarity_cutoff(profile, 0.05)
        _append_bias(profile, "low_overlap_field_family")
        actions.append(_action("too_similar_reject_share", similar_share, "tighten virtual similarity and prefer low-overlap families"))
    if duplicate_count > 0 or blacklist:
        _merge_signature_blacklist(profile, blacklist)
        _set_signature_count(profile, -1, floor=1)
        actions.append(_action("duplicate_field_signature_count", duplicate_count, "blacklist crowded field signatures"))
    if illegal_share >= 0.08:
        profile.setdefault("legal_input_policy", {})["strict"] = True
        _append_bias(profile, "legal_input_registry_refresh")
        actions.append(_action("illegal_input_reject_share", illegal_share, "force strict legal-input registry use"))
    if not actions:
        _append_bias(profile, "incremental_diversification")
        actions.append(_action("stable_metrics", None, "keep profile conservative with small diversification"))
    return {"profile": profile, "actions": actions}


def _candidate_b(active_profile: dict[str, Any], metrics: dict[str, Any], blacklist: list[str]) -> dict[str, Any]:
    profile = _candidate_base(active_profile, "candidate_b", "balanced_cross_domain_overlay")
    actions: list[dict[str, Any]] = []
    _append_bias(profile, "cross_domain_overlay")
    _append_bias(profile, "low_overlap_field_family")
    _append_bias(profile, "underused_reference_dataset")
    _merge_signature_blacklist(profile, blacklist)
    _set_mine_default(profile, "max_candidates", 40)
    _lower_similarity_cutoff(profile, 0.03)
    if (_safe_float(metrics.get("promote_submit_success_rate")) is not None) and (
        (_safe_float(metrics.get("promote_submit_success_rate")) or 0.0) < 0.50
    ):
        profile.setdefault("promotion_gate", {})["min_ready"] = max(2, int(profile.get("promotion_gate", {}).get("min_ready") or 1))
        profile.setdefault("promotion_gate", {})["promote_requires_linked_submit_review"] = True
    actions.append(_action("balanced_profile", None, "add cross-domain and underused-reference bias with moderate budget"))
    return {"profile": profile, "actions": actions}


def _candidate_c(active_profile: dict[str, Any], metrics: dict[str, Any], blacklist: list[str]) -> dict[str, Any]:
    profile = _candidate_base(active_profile, "candidate_c", "exploratory_reference_catalog_grid")
    actions: list[dict[str, Any]] = []
    _append_bias(profile, "systematic_local_factor_grid")
    _append_bias(profile, "underexplored_reference_fields")
    _append_bias(profile, "reference_catalog_long_tail")
    _set_mine_default(profile, "max_candidates", 80)
    _set_mine_default(profile, "cycle_candidate_count", 5)
    _set_mine_default(profile, "max_total_simulations", 40)
    _merge_signature_blacklist(profile, blacklist)
    if (_safe_float(metrics.get("illegal_input_reject_share")) or 0.0) >= 0.08:
        profile.setdefault("legal_input_policy", {})["strict"] = True
        _append_bias(profile, "legal_input_registry_refresh")
    if (_safe_float(metrics.get("self_correlation_reject_share")) or 0.0) >= 0.30:
        _set_signature_count(profile, -1, floor=2)
    actions.append(_action("exploration_budget", None, "expand local-only reference-catalog exploration"))
    return {"profile": profile, "actions": actions}


def _candidate_base(active_profile: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    profile = copy.deepcopy(active_profile)
    profile["candidate_key"] = key
    profile["candidate_label"] = label
    profile["profile_version"] = int(profile.get("profile_version") or 0) + 1
    profile["updated_at"] = _now()
    profile.setdefault("mine_defaults", {})["no_real_submit"] = True
    return profile


def _append_bias(profile: dict[str, Any], value: str) -> None:
    biases = list(profile.get("priority_biases") or [])
    if value not in biases:
        biases.append(value)
    profile["priority_biases"] = biases


def _merge_signature_blacklist(profile: dict[str, Any], values: list[str]) -> None:
    policy = profile.setdefault("field_signature_policy", {})
    current = [str(value) for value in policy.get("blacklist") or [] if value]
    for value in values:
        if value not in current:
            current.append(value)
    policy["blacklist"] = current


def _set_family_count(profile: dict[str, Any], delta: int, *, floor: int) -> None:
    policy = profile.setdefault("family_policy", {})
    current = int(policy.get("max_family_count") or 8)
    policy["max_family_count"] = max(floor, current + delta)


def _set_signature_count(profile: dict[str, Any], delta: int, *, floor: int) -> None:
    policy = profile.setdefault("field_signature_policy", {})
    current = int(policy.get("max_field_signature_count") or 4)
    policy["max_field_signature_count"] = max(floor, current + delta)


def _lower_similarity_cutoff(profile: dict[str, Any], delta: float) -> None:
    policy = profile.setdefault("similarity_policy", {})
    current = _safe_float(policy.get("cutoff")) or 0.72
    policy["cutoff"] = round(max(0.55, current - delta), 3)


def _set_mine_default(profile: dict[str, Any], key: str, delta: int) -> None:
    defaults = profile.setdefault("mine_defaults", {})
    current = int(defaults.get(key) or 0)
    defaults[key] = current + delta


def _action(trigger: str, value: Any, change: str) -> dict[str, Any]:
    return {"trigger": trigger, "metric_value": value, "change": change}
