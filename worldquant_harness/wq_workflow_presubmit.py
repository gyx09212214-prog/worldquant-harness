"""Presubmit gates and virtual-active inventory for the WQ workflow."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .artifact_io import read_jsonl as _read_jsonl
from .artifact_io import utc_now as _now
from .artifact_io import write_jsonl as _write_jsonl
from .record_utils import safe_float as _safe_float
from .wq_agent_config import WQAgentWorkflowConfig
from .wq_agent_records import candidate_dedupe_key as _candidate_dedupe_key
from .wq_agent_records import workflow_settings as _settings
from .wq_brain_service import submit_threshold_checks
from .wq_efficiency import annotate_candidate_identity
from .wq_forum_submission_optimizer import annotate_candidate_with_policy, evaluate_candidate_policy
from .wq_repair_screening import repair_candidate_concentration_risk
from .wq_similarity import nearest_similarity
from .wq_workflow_active import (
    _active_family_counts,
    _active_field_signature_counts,
    _field_signature,
    _fields,
    _has_unsupported_statement_separator,
    _is_option_only_expression,
    _operators,
    _row_family,
    _virtual_active_row,
)
from .wq_workflow_constants import CONFIRMED_READY, SUBMIT_PROBE_NEEDED
from .wq_workflow_context import (
    _legal_input_registry_for_config,
    _submission_policy_for_config,
)
from .wq_workflow_scoring import (
    _score,
    review_sort_key,
)


def build_virtual_active_inventory(real_active_rows: list[dict], virtual_ready_records: list[dict]) -> dict:
    real_rows = [{**row, "active_source": row.get("active_source") or "platform"} for row in real_active_rows]
    virtual_rows = [_virtual_active_row(row) for row in virtual_ready_records]
    active = real_rows + virtual_rows
    field_counts: Counter[str] = Counter()
    operator_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    field_signature_counts: Counter[str] = Counter()
    for row in active:
        expression = str(row.get("expression") or "")
        fields = _fields(expression)
        field_counts.update(fields)
        operator_counts.update(_operators(expression))
        family = _row_family(row)
        if family:
            family_counts[family] += 1
        signature = _field_signature(expression)
        if signature:
            field_signature_counts[signature] += 1
    return {
        "created_at": _now(),
        "active_count": len(active),
        "real_active_count": len(real_rows),
        "virtual_active_count": len(virtual_rows),
        "field_counts": dict(sorted(field_counts.items())),
        "operator_counts": dict(sorted(operator_counts.items())),
        "source_family_counts": dict(sorted(family_counts.items())),
        "field_signature_counts": dict(sorted(field_signature_counts.items())),
        "active": active,
        "real_active": real_rows,
        "virtual_active": virtual_rows,
    }


def _filter_candidate_pool_for_presubmit(
    path: Path,
    *,
    skip_normalized_expressions: set[str],
    active_rows: list[dict] | None = None,
    config: WQAgentWorkflowConfig | None = None,
) -> dict:
    rows = _read_jsonl(path)
    if not rows:
        return {"ok": True, "input": 0, "kept": 0, "skipped": 0}
    kept = []
    skipped = []
    skip_reasons: Counter[str] = Counter()
    policy_actions: Counter[str] = Counter()
    skill_risk_flags: Counter[str] = Counter()
    active_rows = active_rows or []
    active_family_counts = _active_family_counts(active_rows)
    active_field_signature_counts = _active_field_signature_counts(active_rows)
    submission_policy = _submission_policy_for_config(config)
    legal_registry = _legal_input_registry_for_config(config)
    kept_family_counts: Counter[str] = Counter()
    kept_field_signature_counts: Counter[str] = Counter()
    for row in rows:
        expression = str(row.get("expression") or "")
        if expression and _candidate_dedupe_key(row) in skip_normalized_expressions:
            skipped.append({**row, "candidate_skip_reason": "previous_presubmit_rejection"})
            skip_reasons["previous_presubmit_rejection"] += 1
            continue
        if expression and _has_unsupported_statement_separator(expression):
            skipped.append({**row, "candidate_skip_reason": "unsupported_statement_separator"})
            skip_reasons["unsupported_statement_separator"] += 1
            continue
        if expression and _is_option_only_expression(expression):
            skipped.append({**row, "candidate_skip_reason": "pure_options_only_distribution_risk"})
            skip_reasons["pure_options_only_distribution_risk"] += 1
            continue
        concentration_risk = repair_candidate_concentration_risk(expression) if expression else None
        if concentration_risk:
            skipped.append({
                **row,
                "candidate_skip_reason": "sparse_group_distribution_risk",
                "concentration_risk": concentration_risk,
            })
            skip_reasons["sparse_group_distribution_risk"] += 1
            continue
        if config is not None and expression and legal_registry is not None:
            legal_validation = legal_registry.validate_candidate(
                row,
                account=config.account,
                region=config.region,
                universe=config.universe,
                delay=config.delay,
                strict=config.strict_legal_inputs,
            )
            if not legal_validation.ok:
                reason = legal_validation.primary_error_code()
                skipped.append({
                    **row,
                    "candidate_skip_reason": reason,
                    "legal_input_validation": legal_validation.to_dict(),
                })
                skip_reasons[reason] += 1
                continue
            row = {**row, "legal_input_validation": legal_validation.to_dict()}
        if config is not None and expression:
            nearest = nearest_similarity(expression, active_rows)
            nearest_score = _score((nearest or {}).get("similarity", {}).get("overall_similarity"), default=0.0) if nearest else 0.0
            if nearest and nearest.get("exact"):
                skipped.append({**row, "candidate_skip_reason": "exact_active_duplicate", "nearest_active": nearest})
                skip_reasons["exact_active_duplicate"] += 1
                continue
            if nearest_score > config.virtual_similarity_cutoff:
                skipped.append({
                    **row,
                    "candidate_skip_reason": "too_similar_to_real_or_virtual_active",
                    "nearest_similarity": nearest_score,
                    "nearest_active": nearest,
                })
                skip_reasons["too_similar_to_real_or_virtual_active"] += 1
                continue
            family = _row_family(row)
            if config.max_virtual_family_count > 0 and family:
                family_count = active_family_counts.get(family, 0) + kept_family_counts.get(family, 0)
                if family_count >= config.max_virtual_family_count:
                    skipped.append({**row, "candidate_skip_reason": "source_family_capacity_reached"})
                    skip_reasons["source_family_capacity_reached"] += 1
                    continue
            field_signature = _field_signature(expression)
            if config.max_virtual_field_signature_count > 0 and field_signature:
                field_signature_count = (
                    active_field_signature_counts.get(field_signature, 0)
                    + kept_field_signature_counts.get(field_signature, 0)
                )
                if field_signature_count >= config.max_virtual_field_signature_count:
                    skipped.append({**row, "candidate_skip_reason": "field_signature_capacity_reached"})
                    skip_reasons["field_signature_capacity_reached"] += 1
                    continue
            policy_row = annotate_candidate_with_policy(
                {
                    **row,
                    "nearest_similarity": nearest_score,
                    "nearest_active": nearest,
                    "field_signature": field_signature,
                },
                submission_policy,
            )
            if policy_row.get("forum_policy_action"):
                policy_actions[str(policy_row.get("forum_policy_action"))] += 1
            skill_risk_flags.update(str(flag) for flag in policy_row.get("community_skill_risk_flags") or [] if flag)
            if policy_row.get("forum_policy_action") == "block":
                reason = str(policy_row.get("forum_policy_reason") or "forum_policy_block")
                skipped.append({**policy_row, "candidate_skip_reason": reason})
                skip_reasons[reason] += 1
                continue
            row = annotate_candidate_identity(policy_row, _settings(config))
            if family:
                kept_family_counts[family] += 1
            if field_signature:
                kept_field_signature_counts[field_signature] += 1
        kept.append(row)
    for index, row in enumerate(kept, start=1):
        row["candidate_rank"] = index
    _write_jsonl(path, kept)
    _write_jsonl(path.with_name("candidate_skipped.jsonl"), skipped)
    return {
        "ok": True,
        "input": len(rows),
        "kept": len(kept),
        "skipped": len(skipped),
        "skip_reasons": dict(sorted(skip_reasons.items())),
        "policy_actions": dict(sorted(policy_actions.items())),
        "community_skill_risk_flags": dict(skill_risk_flags.most_common()),
    }


def select_presubmit_ready_candidate(
    review_rows: list[dict],
    active_rows: list[dict],
    *,
    config: WQAgentWorkflowConfig,
    cycle_index: int,
) -> tuple[dict | None, list[dict]]:
    rejected: list[dict] = []
    accepted: dict | None = None
    for row in sorted(review_rows, key=review_sort_key):
        ok, reason, gate = presubmit_acceptance_gate(row, active_rows, config=config)
        if ok:
            if accepted is None:
                accepted = row
            continue
        if _should_defer_presubmit_recheck(row, reason):
            continue
        rejected.append(annotate_candidate_identity({
            **row,
            "cycle_index": cycle_index,
            "presubmit_reject_reason": reason,
            "presubmit_gate": gate,
        }, _settings(config)))
    return accepted, rejected


def _should_defer_presubmit_recheck(row: dict, reason: str) -> bool:
    """Keep transient check-only gaps eligible for a later correlation check."""

    if row.get("triage_bucket") == SUBMIT_PROBE_NEEDED:
        return True
    if reason != "check_submission_not_readable":
        return False
    sc_result = str(row.get("sc_result") or "").upper()
    prod_result = str(row.get("prod_corr_result") or "").upper()
    return sc_result in {"", "MISSING", "PENDING"} or prod_result in {"", "MISSING", "PENDING"}


def presubmit_acceptance_gate(
    row: dict,
    active_rows: list[dict],
    *,
    config: WQAgentWorkflowConfig,
) -> tuple[bool, str, dict]:
    metrics = {
        "sharpe": row.get("sharpe"),
        "fitness": row.get("fitness"),
        "turnover": row.get("turnover"),
    }
    threshold_gate = submit_threshold_checks(metrics)
    sc_result = str(row.get("sc_result") or "").upper()
    sc_value = _score(row.get("sc_value"), default=float("inf"))
    prod_result = str(row.get("prod_corr_result") or "").upper()
    daily_return_corr = _safe_float(row.get("active_daily_return_corr_max") or row.get("active_daily_return_corr"))
    daily_return_corr_gate = str(row.get("active_daily_return_corr_gate") or "").lower()
    platform_status = str(row.get("platform_status") or "").upper()
    expression = str(row.get("expression") or "")
    nearest = nearest_similarity(expression, active_rows)
    nearest_score = (
        _score((nearest or {}).get("similarity", {}).get("overall_similarity"), default=0.0)
        if nearest else 0.0
    )
    family = _row_family(row)
    family_count = _active_family_counts(active_rows).get(family, 0) if family else 0
    field_signature = _field_signature(expression)
    field_signature_count = _active_field_signature_counts(active_rows).get(field_signature, 0) if field_signature else 0
    gate = {
        "threshold_gate": threshold_gate,
        "platform_status": platform_status,
        "sc_result": row.get("sc_result"),
        "sc_value": row.get("sc_value"),
        "presubmit_self_correlation_cutoff": config.presubmit_self_correlation_cutoff,
        "prod_corr_result": row.get("prod_corr_result"),
        "active_daily_return_corr_max": daily_return_corr,
        "active_daily_return_corr_gate": row.get("active_daily_return_corr_gate"),
        "daily_return_corr_cutoff": config.presubmit_daily_return_correlation_cutoff,
        "daily_return_corr_warn": config.presubmit_daily_return_correlation_warn,
        "nearest_active": nearest,
        "nearest_similarity": nearest_score,
        "virtual_similarity_cutoff": config.virtual_similarity_cutoff,
        "source_family": family,
        "source_family_count_before": family_count,
        "source_family_limit": config.max_virtual_family_count,
        "field_signature": field_signature,
        "field_signature_count_before": field_signature_count,
        "field_signature_limit": config.max_virtual_field_signature_count,
    }
    policy_eval = evaluate_candidate_policy(
        {
            **row,
            "nearest_similarity": nearest_score,
            "nearest_active": nearest,
            "field_signature": field_signature,
        },
        _submission_policy_for_config(config),
    )
    gate["forum_policy"] = policy_eval

    if policy_eval.get("action") == "block":
        return False, str(policy_eval.get("reason") or "forum_policy_block"), gate
    if row.get("triage_bucket") != CONFIRMED_READY:
        return False, "not_confirmed_ready", gate
    if row.get("api_check_status") != "api_check_readable":
        return False, "check_submission_not_readable", gate
    if platform_status in {"ACTIVE", "SUBMITTED"} or bool(row.get("submitted")):
        return False, "platform_status_not_unsubmitted", gate
    if not threshold_gate["eligible"]:
        return False, "base_submit_thresholds_failed", gate
    if sc_result != "PASS":
        return False, "self_correlation_not_pass", gate
    if config.presubmit_self_correlation_cutoff is not None and sc_value >= config.presubmit_self_correlation_cutoff:
        return False, "self_correlation_value_above_strict_cutoff", gate
    if prod_result == "FAIL":
        return False, "prod_correlation_failed", gate
    if daily_return_corr_gate == "reject":
        return False, "daily_return_correlation_rejected", gate
    if (
        config.presubmit_daily_return_correlation_cutoff is not None
        and daily_return_corr is not None
        and abs(daily_return_corr) >= config.presubmit_daily_return_correlation_cutoff
    ):
        return False, "daily_return_correlation_above_cutoff", gate
    if row.get("failed_platform_checks"):
        return False, "platform_checks_failed", gate
    if nearest and nearest.get("exact"):
        return False, "exact_active_duplicate", gate
    if nearest_score > config.virtual_similarity_cutoff:
        return False, "too_similar_to_real_or_virtual_active", gate
    if config.max_virtual_family_count > 0 and family and family_count >= config.max_virtual_family_count:
        return False, "source_family_capacity_reached", gate
    if (
        config.max_virtual_field_signature_count > 0
        and field_signature
        and field_signature_count >= config.max_virtual_field_signature_count
    ):
        return False, "field_signature_capacity_reached", gate
    return True, "accepted", gate


def build_virtual_ready_record(
    row: dict,
    active_rows: list[dict],
    *,
    config: WQAgentWorkflowConfig,
    cycle_index: int,
    ready_index: int,
    cycle_output_dir: Path,
) -> dict:
    ok, reason, gate = presubmit_acceptance_gate(row, active_rows, config=config)
    return annotate_candidate_identity({
        **row,
        "created_at": _now(),
        "cycle_index": cycle_index,
        "ready_index": ready_index,
        "virtual_active_status": "VIRTUAL_ACTIVE",
        "presubmit_accepted": bool(ok),
        "presubmit_accept_reason": reason,
        "presubmit_gate": gate,
        "cycle_output_dir": str(cycle_output_dir),
    }, _settings(config))
