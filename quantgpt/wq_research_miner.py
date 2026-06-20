"""Local research planner for WorldQuant factor mining.

This module turns previous presubmit artifacts into a deterministic candidate
file. It never calls an LLM provider, never talks to WQ BRAIN, and never submits.
The output is intended to be fed into ``wq_agent_workflow.py presubmit-sequential``.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .alpha_tracker import compute_similarity
from .expression_parser import extract_components, normalize_expression
from .wq_auto_mining import validate_wq_expression
from .wq_brain_service import submit_threshold_checks
from .wq_forum_submission_optimizer import annotate_candidate_with_policy, load_submission_policy


INVALID_WQ_FIELDS = {"short_interest", "short_ratio"}

OPTION_FIELDS = {
    "implied_volatility_call_30",
    "implied_volatility_put_30",
    "implied_volatility_call_60",
    "implied_volatility_put_60",
    "implied_volatility_call_90",
    "implied_volatility_put_90",
    "implied_volatility_call_120",
    "implied_volatility_put_120",
    "implied_volatility_skew",
    "implied_volatility_slope",
    "industry",
}


@dataclass(frozen=True)
class WQResearchMinerConfig:
    output: Path
    run_dirs: tuple[Path, ...] = field(default_factory=tuple)
    ready_files: tuple[Path, ...] = field(default_factory=tuple)
    rejected_files: tuple[Path, ...] = field(default_factory=tuple)
    active_inventory_files: tuple[Path, ...] = field(default_factory=tuple)
    platform_files: tuple[Path, ...] = field(default_factory=tuple)
    weak_memory_files: tuple[Path, ...] = field(default_factory=tuple)
    submission_policy_file: Path | None = None
    memory_output: Path | None = None
    summary_output: Path | None = None
    max_candidates: int = 40
    similarity_cutoff: float = 0.65
    max_family_count: int = 3
    max_field_signature_count: int = 2
    max_expression_length: int = 500
    max_nesting: int = 10
    llm_provider: str = "none"


def run_research_miner(config: WQResearchMinerConfig) -> dict:
    """Generate a local candidate JSONL and research memory from prior runs."""

    if config.llm_provider != "none":
        raise ValueError("Only llm_provider='none' is supported; this planner is local-only")

    run_ready_files = _run_artifact_paths(config.run_dirs, "presubmit_ready_sequential.jsonl")
    run_rejected_files = _run_artifact_paths(config.run_dirs, "presubmit_rejected.jsonl")
    run_cycle_rejected_files = _run_cycle_artifact_paths(
        config.run_dirs,
        "review_queue.jsonl",
        "simulation_results.jsonl",
    )
    run_active_files = _run_artifact_paths(config.run_dirs, "active_inventory.json", "virtual_active_inventory.json")
    run_platform_files = _run_artifact_paths(config.run_dirs, "platform_alphas.jsonl")

    ready_rows = _load_rows(_dedupe_paths((*config.ready_files, *run_ready_files)))
    rejected_rows = _load_rows(_dedupe_paths((
        *config.rejected_files,
        *run_rejected_files,
        *run_cycle_rejected_files,
    )))
    active_rows = _load_inventory_rows(_dedupe_paths((*config.active_inventory_files, *run_active_files)))
    platform_rows = _load_rows(_dedupe_paths((*config.platform_files, *run_platform_files)))
    weak_memory_rows = normalize_weak_active_memory(_load_rows(_dedupe_paths(config.weak_memory_files)))
    submission_policy = load_submission_policy(config.submission_policy_file)
    platform_active = [
        row for row in platform_rows
        if str(row.get("status") or "").upper() in {"ACTIVE", "SUBMITTED"}
    ]
    comparison_rows = _dedupe_rows([
        *active_rows,
        *platform_active,
        *_virtual_active_rows(ready_rows),
        *_weak_memory_inventory_rows(weak_memory_rows),
    ])

    memory = _dedupe_memory([
        *build_experience_memory(ready_rows, rejected_rows, similarity_cutoff=config.similarity_cutoff),
        *weak_memory_rows,
    ])
    drafts = build_candidate_drafts(
        ready_rows,
        rejected_rows,
        memory,
        weak_memory_rows=weak_memory_rows,
        platform_rows=platform_rows,
        limit=max(config.max_candidates * 5, 20),
    )
    candidates, rejected_drafts = screen_candidate_drafts(
        drafts,
        comparison_rows,
        config=config,
        blocked_rows=rejected_rows,
        weak_memory_rows=weak_memory_rows,
        submission_policy=submission_policy,
    )
    if submission_policy:
        candidates = sorted(candidates, key=_candidate_priority_sort_key)
    candidates = candidates[: max(0, config.max_candidates)]
    for index, row in enumerate(candidates, start=1):
        row["candidate_rank"] = index

    memory_output = config.memory_output or config.output.with_name("experience_memory.jsonl")
    summary_output = config.summary_output or config.output.with_name("wq_research_miner_summary.json")
    _write_jsonl(config.output, candidates)
    _write_jsonl(memory_output, memory)

    summary = {
        "ok": True,
        "mode": "wq-research-miner-generate",
        "no_external_llm": True,
        "llm_provider": config.llm_provider,
        "inputs": {
            "run_dirs": len([path for path in config.run_dirs if path.exists()]),
            "ready": len(ready_rows),
            "rejected": len(rejected_rows),
            "active_inventory": len(active_rows),
            "platform": len(platform_rows),
            "weak_active_memory": len(weak_memory_rows),
            "comparison_inventory": len(comparison_rows),
            "submission_policy": str(config.submission_policy_file) if config.submission_policy_file else "",
        },
        "outputs": {
            "candidates": len(candidates),
            "experience_memory": len(memory),
            "screened_out": len(rejected_drafts),
        },
        "counts": {
            "candidate_family": dict(sorted(Counter(row.get("source_family") for row in candidates).items())),
            "memory_kind": dict(sorted(Counter(row.get("memory_kind") for row in memory).items())),
            "screen_reject_reason": dict(sorted(Counter(row.get("reject_reason") for row in rejected_drafts).items())),
            "forum_policy_action": dict(sorted(Counter(
                row.get("forum_policy_action") for row in candidates if row.get("forum_policy_action")
            ).items())),
        },
        "files": {
            "candidates": str(config.output),
            "experience_memory": str(memory_output),
            "summary": str(summary_output),
        },
        "selected_preview": [
            {
                "tag": row.get("tag"),
                "source_family": row.get("source_family"),
                "nearest_similarity": row.get("nearest_similarity"),
                "expression": row.get("expression"),
            }
            for row in candidates[:10]
        ],
    }
    _write_json(summary_output, summary)
    return summary


def build_experience_memory(
    ready_rows: list[dict],
    rejected_rows: list[dict],
    *,
    similarity_cutoff: float = 0.65,
) -> list[dict]:
    memory: list[dict] = []
    for row in ready_rows:
        expression = str(row.get("expression") or "").strip()
        if not expression:
            continue
        memory.append(_memory_record(
            row,
            memory_kind="success_ready",
            severity="positive",
            failure_kind="accepted",
            lesson="Accepted by strict presubmit; use as elite archive but avoid close duplicates.",
            similarity_cutoff=similarity_cutoff,
        ))

    for row in rejected_rows:
        expression = str(row.get("expression") or "").strip()
        if not expression:
            continue
        failure_kind = infer_failure_kind(row, similarity_cutoff=similarity_cutoff)
        memory.append(_memory_record(
            row,
            memory_kind="failure_constraint",
            severity=_failure_severity(failure_kind),
            failure_kind=failure_kind,
            lesson=_failure_lesson(row, failure_kind),
            similarity_cutoff=similarity_cutoff,
        ))

    return _dedupe_memory(memory)


def infer_failure_kind(row: dict, *, similarity_cutoff: float = 0.65) -> str:
    reason = str(row.get("presubmit_reject_reason") or row.get("triage_reason") or row.get("status") or "").lower()
    api_status = str(row.get("api_check_status") or "").lower()
    sc_value = _safe_float(row.get("sc_value"))
    nearest = _safe_float(row.get("nearest_similarity"))
    failed_checks = [str(item.get("name") or "").upper() for item in row.get("failed_platform_checks") or []]

    if sc_value is not None and sc_value >= 0.7:
        return "self_correlation_high"
    if "self_correlation" in reason or api_status == "self_correlation_fail":
        return "self_correlation_high"
    if nearest is not None and nearest > similarity_cutoff:
        return "high_similarity"
    if "too_similar" in reason or "duplicate" in reason:
        return "high_similarity"
    if failed_checks:
        if any(name in {"CONCENTRATED_WEIGHT", "LOW_SUB_UNIVERSE_SHARPE", "LOW_SUB_UNIVERSE_FITNESS"} for name in failed_checks):
            return "platform_distribution_fail"
        return "platform_check_fail"
    if not submit_threshold_checks({"sharpe": row.get("sharpe"), "fitness": row.get("fitness"), "turnover": row.get("turnover")})["eligible"]:
        return "base_metric_fail"
    if api_status == "prod_correlation_fail" or "prod_correlation" in reason:
        return "prod_correlation_fail"
    return str(row.get("presubmit_reject_reason") or row.get("status") or "unknown_failure")


def build_candidate_drafts(
    ready_rows: list[dict],
    rejected_rows: list[dict],
    memory: list[dict],
    *,
    weak_memory_rows: list[dict] | None = None,
    platform_rows: list[dict] | None = None,
    limit: int,
) -> list[dict]:
    drafts: list[dict] = []
    blocked_platform_norms = {
        normalize_expression(str(row.get("expression") or ""))
        for row in rejected_rows
        if str(row.get("expression") or "").strip()
        and infer_failure_kind(row) in {"platform_distribution_fail", "platform_check_fail", "self_correlation_high"}
    }
    for row in sorted(rejected_rows, key=_repair_priority):
        if len(drafts) >= limit * 4:
            break
        if not _is_repairable(row):
            continue
        drafts.extend(_repair_variants(row, memory))

    for row in sorted(ready_rows, key=_elite_priority):
        if len(drafts) >= limit * 4:
            break
        drafts.extend(_elite_variants(row))

    drafts.extend(_platform_memory_candidates(
        platform_rows or [],
        limit=max(limit // 4, 20),
        blocked_norms=blocked_platform_norms,
    ))
    drafts.extend(_weak_active_memory_candidates(
        weak_memory_rows or [],
        limit=max(limit // 3, 20),
    ))
    drafts.extend(_exploration_templates(memory))
    drafts.extend(_systematic_research_templates())
    return _interleave_drafts(_dedupe_candidates(drafts), limit=limit)


def screen_candidate_drafts(
    drafts: list[dict],
    active_rows: list[dict],
    *,
    config: WQResearchMinerConfig,
    blocked_rows: list[dict] | None = None,
    weak_memory_rows: list[dict] | None = None,
    submission_policy: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    selected: list[dict] = []
    rejected: list[dict] = []
    active_norms = {
        normalize_expression(str(row.get("expression") or ""))
        for row in active_rows
        if str(row.get("expression") or "").strip()
    }
    blocked_norms = {
        normalize_expression(str(row.get("expression") or ""))
        for row in blocked_rows or []
        if str(row.get("expression") or "").strip()
    }
    weak_signatures = {
        str(row.get("field_signature") or field_signature(str(row.get("expression") or "")))
        for row in weak_memory_rows or []
        if str(row.get("field_signature") or field_signature(str(row.get("expression") or "")))
    }
    seen = set(active_norms)
    family_counts: Counter[str] = Counter()
    signature_counts: Counter[str] = Counter()

    for draft in drafts:
        expression = str(draft.get("expression") or "").strip()
        norm = normalize_expression(expression)
        if not expression:
            rejected.append({**draft, "reject_reason": "empty_expression"})
            continue
        unsafe_reason = _unsafe_expression_reason(expression)
        if unsafe_reason:
            rejected.append({**draft, "reject_reason": unsafe_reason})
            continue
        if norm in blocked_norms:
            rejected.append({**draft, "reject_reason": "historical_rejected_expression"})
            continue
        components = components_for(expression)
        invalid_fields = sorted(components["fields"] & INVALID_WQ_FIELDS)
        if invalid_fields:
            rejected.append({**draft, "reject_reason": "known_invalid_wq_field", "invalid_fields": invalid_fields})
            continue
        if _is_option_only_fields(components["fields"]):
            rejected.append({**draft, "reject_reason": "pure_options_only_distribution_risk"})
            continue
        if norm in seen:
            rejected.append({**draft, "reject_reason": "duplicate_or_active_expression"})
            continue
        if len(expression) > config.max_expression_length:
            rejected.append({**draft, "reject_reason": "expression_too_long"})
            continue
        if _max_parenthesis_depth(expression) > config.max_nesting:
            rejected.append({**draft, "reject_reason": "expression_too_nested"})
            continue
        if expression.count("group_neutralize(") > 1:
            rejected.append({**draft, "reject_reason": "repeated_group_neutralize"})
            continue
        try:
            validate_wq_expression(expression)
        except Exception as exc:
            rejected.append({**draft, "reject_reason": "local_wq_validation_failed", "error": str(exc)})
            continue

        comparison_rows = [*active_rows, *selected]
        nearest = nearest_similarity(expression, comparison_rows)
        nearest_score = (
            _safe_float((nearest or {}).get("similarity", {}).get("overall_similarity")) or 0.0
            if nearest else 0.0
        )
        if nearest and nearest.get("exact"):
            rejected.append({**draft, "reject_reason": "exact_inventory_duplicate", "nearest_active": nearest})
            continue
        if nearest_score > config.similarity_cutoff:
            rejected.append({
                **draft,
                "reject_reason": "too_similar_to_inventory",
                "nearest_similarity": nearest_score,
                "nearest_active": nearest,
            })
            continue

        family = str(draft.get("source_family") or "local_research_miner")
        signature = field_signature(expression)
        if config.max_family_count > 0 and family_counts[family] >= config.max_family_count:
            rejected.append({**draft, "reject_reason": "family_capacity_reached", "source_family": family})
            continue
        if config.max_field_signature_count > 0 and signature_counts[signature] >= config.max_field_signature_count:
            rejected.append({**draft, "reject_reason": "field_signature_capacity_reached", "field_signature": signature})
            continue
        if (
            signature in weak_signatures
            and not str(draft.get("mutation_strategy") or "").startswith("weak_active_")
        ):
            rejected.append({**draft, "reject_reason": "weak_active_signature_risk", "field_signature": signature})
            continue
        policy_row = {
            **draft,
            "nearest_similarity": nearest_score,
            "field_signature": signature,
            "source_fields": sorted(components["fields"]),
            "operators": sorted(components["operators"]),
        }
        if submission_policy:
            policy_row = annotate_candidate_with_policy(policy_row, submission_policy)
            if policy_row.get("forum_policy_action") == "block":
                rejected.append({
                    **policy_row,
                    "reject_reason": policy_row.get("forum_policy_reason") or "forum_policy_block",
                })
                continue

        selected_row = {
            **policy_row,
            "source": "wq_research_miner",
            "source_fields": sorted(components["fields"]),
            "operators": sorted(components["operators"]),
            "field_signature": signature,
            "active_similarity": nearest,
            "nearest_similarity": nearest_score,
            "llm_provider": "none",
            "no_external_llm": True,
        }
        selected.append(selected_row)
        seen.add(norm)
        family_counts[family] += 1
        signature_counts[signature] += 1

    return selected, rejected


def _candidate_priority_sort_key(row: dict) -> tuple:
    return (
        -(_safe_float(row.get("research_priority_score")) or 0.0),
        _safe_float(row.get("nearest_similarity")) or 0.0,
        str(row.get("source_family") or ""),
        str(row.get("tag") or ""),
    )


def nearest_similarity(expression: str, rows: list[dict]) -> dict | None:
    nearest = None
    normalized = normalize_expression(expression)
    for row in rows:
        other = str(row.get("expression") or "")
        if not other:
            continue
        similarity = compute_similarity(expression, other)
        item = {
            "alpha_id": row.get("alpha_id"),
            "expression": other,
            "status": row.get("status"),
            "similarity": similarity,
            "exact": normalized == normalize_expression(other),
        }
        if nearest is None or similarity.get("overall_similarity", 0.0) > nearest["similarity"].get("overall_similarity", 0.0):
            nearest = item
    return nearest


def field_signature(expression: str) -> str:
    return "|".join(sorted(components_for(expression)["fields"]))


def components_for(expression: str) -> dict[str, set[str]]:
    try:
        parts = extract_components(expression or "")
    except Exception:
        return {"fields": set(), "operators": set()}
    return {
        "fields": {str(item) for item in parts.get("fields", set())},
        "operators": {str(item) for item in parts.get("operators", set())},
    }


def _unsafe_expression_reason(expression: str) -> str | None:
    """Reject platform snippets that are not a single FASTEXPR formula."""

    if ";" in expression:
        return "unsupported_statement_separator"
    if "/*" in expression or "*/" in expression or "//" in expression:
        return "unsupported_embedded_comment"
    return None


def _repair_variants(row: dict, memory: list[dict]) -> list[dict]:
    expression = str(row.get("expression") or "").strip()
    if not expression:
        return []
    fields = components_for(expression)["fields"]
    failure_kind = infer_failure_kind(row)
    base = _strip_outer_rank(expression)
    parent = [row.get("alpha_id")] if row.get("alpha_id") else []
    tag = str(row.get("tag") or row.get("alpha_id") or "near-miss")
    variants: list[dict] = []

    if {"cashflow_op", "cashflow_efficiency_rank_derivative"} <= fields:
        variants.extend([
            _draft(
                "rank(0.35 * ts_rank(cashflow_op / cap, 100) + 0.65 * rank(-1 * cashflow_efficiency_rank_derivative) - ts_rank(returns, 40))",
                tag=f"repair-{tag}-cfop-eff-65-r40",
                family="research_cashflow_weight_repair",
                strategy="strict_sc_weight_repair",
                parent_alpha_ids=parent,
                rationale="Retain the strong cashflow-efficiency relationship while changing the return horizon.",
            ),
            _draft(
                "rank(0.30 * ts_rank(cashflow_op / enterprise_value, 80) + 0.70 * rank(-1 * cashflow_efficiency_rank_derivative) - ts_rank(returns, 40))",
                tag=f"repair-{tag}-cfop-ev-eff",
                family="research_cashflow_denominator_repair",
                strategy="denominator_decorrelation",
                parent_alpha_ids=parent,
                rationale="Switch denominator from cap to enterprise value to reduce crowding.",
            ),
            _draft(
                "rank(0.40 * ts_rank(cashflow_op / assets, 80) + 0.50 * rank(-1 * cashflow_efficiency_rank_derivative) - ts_rank(returns, 40) + 0.10 * rank(-1 * earnings_certainty_rank_derivative))",
                tag=f"repair-{tag}-cfop-assets-certainty",
                family="research_cashflow_denominator_repair",
                strategy="denominator_decorrelation",
                parent_alpha_ids=parent,
                rationale="Use an asset-scaled cashflow leg and a small certainty derivative overlay.",
            ),
            _draft(
                "rank(0.35 * ts_rank(cashflow_op / sales, 80) + 0.55 * rank(-1 * cashflow_efficiency_rank_derivative) - ts_rank(returns, 40) + 0.10 * rank(-1 * earnings_certainty_rank_derivative))",
                tag=f"repair-{tag}-cfop-sales-certainty",
                family="research_cashflow_denominator_repair",
                strategy="denominator_decorrelation",
                parent_alpha_ids=parent,
                rationale="Use sales-scaled cashflow to diversify the field signature.",
            ),
            _draft(
                "rank(0.25 * ts_rank(cashflow_op / cap, 80) + 0.45 * rank(-1 * cashflow_efficiency_rank_derivative) + 0.30 * rank(-1 * earnings_certainty_rank_derivative) - ts_rank(returns, 40))",
                tag=f"repair-{tag}-cfop-eff-certainty",
                family="research_cashflow_derivative_overlay",
                strategy="earnings_certainty_overlay",
                parent_alpha_ids=parent,
                rationale="Blend a second model derivative to lower dependence on one cashflow-efficiency feature.",
            ),
            _draft(
                "rank(0.45 * ts_rank(cashflow_op / cap, 80) + 0.40 * rank(-1 * cashflow_efficiency_rank_derivative) + 0.15 * rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5)) - ts_rank(returns, 30))",
                tag=f"repair-{tag}-cfop-eff-iv90",
                family="research_cashflow_iv_repair",
                strategy="experience_memory_sc_repair",
                parent_alpha_ids=parent,
                rationale="Historical memory shows a small options-skew overlay can reduce self-correlation on cashflow-op efficiency alphas.",
            ),
            _draft(
                "rank(0.40 * ts_rank(cashflow_op / cap, 100) + 0.40 * rank(-1 * cashflow_efficiency_rank_derivative) + 0.20 * rank(ts_mean((implied_volatility_call_90 - implied_volatility_put_90) / (implied_volatility_call_90 + implied_volatility_put_90), 5)) - ts_rank(returns, 40))",
                tag=f"repair-{tag}-cfop-eff-ivratio90",
                family="research_cashflow_iv_repair",
                strategy="experience_memory_sc_repair",
                parent_alpha_ids=parent,
                rationale="Use normalized options skew to diversify a high-performing but correlated cashflow signal.",
            ),
            _draft(
                "rank(0.45 * ts_rank(cashflow_op / cap, 80) + 0.40 * rank(-1 * cashflow_efficiency_rank_derivative) + 0.15 * rank(ts_mean(implied_volatility_call_120 - implied_volatility_put_120, 5)) - ts_rank(returns, 30))",
                tag=f"repair-{tag}-cfop-eff-iv120",
                family="research_cashflow_iv_repair",
                strategy="experience_memory_sc_repair",
                parent_alpha_ids=parent,
                rationale="Use 120-day options skew as a term-structure variant of the successful IV overlay.",
            ),
            _draft(
                "rank(group_neutralize(0.45 * ts_rank(cashflow_op / cap, 80) + 0.40 * rank(-1 * cashflow_efficiency_rank_derivative) + 0.15 * rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5)) - ts_rank(returns, 30), industry))",
                tag=f"repair-{tag}-cfop-eff-iv90-industry",
                family="research_cashflow_iv_distribution_repair",
                strategy="group_neutralized_iv_overlay",
                parent_alpha_ids=parent,
                rationale="Group neutralization can repair concentration while retaining the IV-decorrelated cashflow signal.",
            ),
        ])

    variants.extend(_targeted_near_pass_variants(fields, tag, parent))

    if _is_option_only_fields(fields) and failure_kind in {"platform_distribution_fail", "platform_check_fail"}:
        variants.extend(_option_distribution_repair_variants(tag, parent))

    if failure_kind in {"self_correlation_high", "high_similarity", "prod_correlation_fail"}:
        variants.extend([
            _blend_draft(base, "rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5))", "iv90", tag, parent, 0.82, 0.18),
            _blend_draft(base, "rank(-1 * ts_rank(returns, 120))", "long-reversal", tag, parent, 0.78, 0.22),
            _blend_draft(base, "rank(ts_corr(vwap, volume, 40))", "volume-vwap-corr", tag, parent, 0.75, 0.25),
        ])

    if failure_kind in {"platform_distribution_fail", "platform_check_fail"}:
        variants.extend([
            _draft(
                f"rank(ts_mean({base}, 5))",
                tag=f"repair-{tag}-smooth5",
                family="research_platform_distribution_repair",
                strategy="smooth_concentration_or_subuniverse_fail",
                parent_alpha_ids=parent,
                rationale="Smooth signal to reduce concentration and sub-universe instability.",
            ),
            _draft(
                f"rank(group_neutralize({base}, industry))",
                tag=f"repair-{tag}-industry-neutralized",
                family="research_platform_distribution_repair",
                strategy="group_neutralize_distribution_fail",
                parent_alpha_ids=parent,
                rationale="Group neutralize signal to reduce distributional platform check failures.",
            ),
        ])

    if _safe_float(row.get("fitness")) is not None and (_safe_float(row.get("fitness")) or 0) < 1.0:
        variants.append(_blend_draft(base, "rank(-1 * ts_std(returns, 20))", "low-vol", tag, parent, 0.80, 0.20))

    return variants


def _targeted_near_pass_variants(fields: set[str], tag: str, parent: list[Any]) -> list[dict]:
    """Local near-pass repairs learned from high-fitness SC failures."""

    variants: list[dict] = []
    if {"actual_eps_value_quarterly", "anl4_afv4_eps_mean", "earnings_momentum_composite_score"} <= fields:
        variants.extend([
            _draft(
                "rank(0.54 * rank(0.36 * ts_rank(actual_eps_value_quarterly / vwap, 80) + 0.24 * ts_rank(anl4_afv4_eps_mean, 80) + 0.22 * ts_rank(earnings_momentum_composite_score, 70) - 0.18 * ts_rank(returns, 40)) + 0.26 * rank(ts_mean((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), 5)) + 0.20 * rank(ts_mean(ts_rank(vwap / close, 20), 3)))",
                tag=f"repair-{tag}-eps-vwap-ivratio120-micro",
                family="research_decorrelation_eps_vwap_ivratio",
                strategy="targeted_near_pass_decorrelation",
                parent_alpha_ids=parent,
                rationale="Shift actual EPS denominator and return horizon while adding IV-ratio and VWAP microstructure overlays.",
            ),
            _draft(
                "rank(0.50 * rank(group_neutralize(0.40 * ts_rank(actual_eps_value_quarterly / close, 80) + 0.25 * ts_rank(anl4_afv4_eps_mean, 90) + 0.20 * ts_rank(earnings_momentum_composite_score, 70) - 0.15 * ts_rank(returns, 40), industry)) + 0.30 * rank(ts_corr(close, volume, 20)) + 0.20 * rank(ts_mean((implied_volatility_call_90 - implied_volatility_put_90) / (implied_volatility_call_90 + implied_volatility_put_90), 5)))",
                tag=f"repair-{tag}-eps-industry-closevol-ivratio90",
                family="research_decorrelation_eps_industry_micro",
                strategy="targeted_near_pass_decorrelation",
                parent_alpha_ids=parent,
                rationale="Neutralize the EPS core by industry and replace the crowded VWAP-volume overlay with close-volume plus normalized IV skew.",
            ),
        ])

    if {"actual_eps_value_quarterly", "change_in_eps_surprise"} <= fields:
        variants.extend([
            _draft(
                "rank(0.45 * ts_rank(actual_sales_value_quarterly / enterprise_value, 80) + 0.25 * ts_rank(actual_eps_value_quarterly / vwap, 80) + 0.15 * ts_rank(change_in_eps_surprise, 60) + 0.15 * rank(ts_corr(close, volume, 20)) - ts_rank(returns, 40))",
                tag=f"repair-{tag}-sales-ev-eps-vwap-closevol",
                family="research_decorrelation_sales_eps_micro",
                strategy="targeted_near_pass_decorrelation",
                parent_alpha_ids=parent,
                rationale="Move an EPS-surprise near miss toward sales/EV and a different microstructure overlay.",
            ),
            _draft(
                "rank(0.55 * rank(0.35 * ts_rank(actual_eps_value_quarterly / close, 90) + 0.25 * ts_rank(anl4_af_eps_value, 80) + 0.20 * ts_rank(change_in_eps_surprise, 80) - 0.20 * ts_mean(ts_rank(returns, 20), 2)) + 0.25 * rank(ts_mean((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), 5)) + 0.20 * rank(ts_mean(ts_rank(vwap / close, 20), 3)))",
                tag=f"repair-{tag}-eps-surprise-ivratio120-vwapclose",
                family="research_decorrelation_eps_surprise_ivratio",
                strategy="targeted_near_pass_decorrelation",
                parent_alpha_ids=parent,
                rationale="Use a longer EPS-surprise horizon with normalized IV term and VWAP-close overlays.",
            ),
        ])

    if {"cashflow_op", "cashflow", "cashflow_fin"} <= fields:
        variants.extend([
            _draft(
                "rank(0.42 * ts_rank(cashflow_op / enterprise_value, 100) + 0.22 * ts_rank(cashflow / assets, 80) - 0.16 * ts_rank(cashflow_fin / enterprise_value, 80) + 0.20 * rank(ts_mean(ts_rank(vwap / close, 20), 3)) - ts_rank(returns, 40))",
                tag=f"repair-{tag}-cf-ev-assets-vwapclose",
                family="research_decorrelation_cashflow_denominator_micro",
                strategy="targeted_near_pass_decorrelation",
                parent_alpha_ids=parent,
                rationale="Repair cashflow triad crowding with enterprise-value/assets denominators and a VWAP-close overlay.",
            ),
            _draft(
                "rank(0.55 * rank(group_neutralize(0.40 * ts_rank(cashflow_op / assets, 80) + 0.25 * ts_rank(cashflow / assets, 80) - 0.20 * ts_rank(cashflow_fin / assets, 80) - 0.15 * ts_rank(returns, 40), industry)) + 0.25 * rank(ts_mean((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), 5)) + 0.20 * rank(ts_corr(close, volume, 20)))",
                tag=f"repair-{tag}-cf-assets-industry-ivratio120",
                family="research_decorrelation_cashflow_assets_ivratio",
                strategy="targeted_near_pass_decorrelation",
                parent_alpha_ids=parent,
                rationale="Industry-neutral asset-scaled cashflow triad plus normalized IV and close-volume overlays.",
            ),
            _draft(
                "rank(0.35 * ts_rank(cashflow_op / cap, 120) + 0.25 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 80) - 0.15 * ts_rank(cashflow_fin / cap, 80) + 0.25 * rank(ts_corr(close, volume, 20)) - ts_rank(returns, 40))",
                tag=f"repair-{tag}-cfop-cfps-closevol",
                family="research_decorrelation_cashflow_per_share_micro",
                strategy="targeted_near_pass_decorrelation",
                parent_alpha_ids=parent,
                rationale="Replace one cashflow leg with per-share quarterly cashflow and a close-volume overlay.",
            ),
        ])

    if {"anl4_adjusted_netincome_ft", "anl4_afv4_eps_mean", "anl4_afv4_dts_spe"} <= fields:
        variants.extend([
            _draft(
                "rank(0.42 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + 0.24 * ts_rank(anl4_afv4_eps_mean / vwap, 80) - 0.16 * ts_rank(anl4_afv4_dts_spe, 80) + 0.18 * rank(ts_mean((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), 5)) - ts_rank(returns, 40))",
                tag=f"repair-{tag}-netincome-ev-epsvwap-ivratio",
                family="research_decorrelation_netincome_ivratio",
                strategy="targeted_near_pass_decorrelation",
                parent_alpha_ids=parent,
                rationale="Move analyst net-income near miss away from cap/close and long-reversal crowding.",
            ),
            _draft(
                "rank(0.50 * rank(group_neutralize(0.40 * ts_rank(anl4_adjusted_netincome_ft / cap, 90) + 0.20 * ts_rank(anl4_afv4_eps_mean / close, 90) - 0.15 * ts_rank(anl4_afv4_dts_spe, 90) - 0.25 * ts_rank(returns, 40), industry)) + 0.30 * rank(ts_corr(close, volume, 20)) + 0.20 * rank(ts_mean(ts_rank(vwap / close, 20), 3)))",
                tag=f"repair-{tag}-netincome-industry-closevol",
                family="research_decorrelation_netincome_micro",
                strategy="targeted_near_pass_decorrelation",
                parent_alpha_ids=parent,
                rationale="Use industry-neutral analyst income with close-volume and VWAP-close overlays instead of long reversal.",
            ),
        ])

    if {"cashflow_op", "cashflow_efficiency_rank_derivative"} <= fields:
        variants.append(_draft(
            "rank(0.48 * ts_rank(cashflow_op / enterprise_value, 100) + 0.22 * rank(-1 * cashflow_efficiency_rank_derivative) + 0.15 * rank(ts_corr(close, volume, 20)) + 0.15 * rank(ts_mean((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), 5)) - ts_rank(returns, 40))",
            tag=f"repair-{tag}-cfop-ev-eff-closevol-ivratio",
            family="research_decorrelation_cfop_eff_micro_ivratio",
            strategy="targeted_near_pass_decorrelation",
            parent_alpha_ids=parent,
            rationale="Keep the cashflow-efficiency edge while changing denominator and orthogonal overlays.",
        ))

    return variants


def _option_distribution_repair_variants(tag: str, parent: list[Any]) -> list[dict]:
    return [
        _draft(
            "rank(0.45 * rank(group_neutralize((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), industry)) + 0.35 * ts_rank(actual_sales_value_quarterly / enterprise_value, 60) - 0.20 * ts_rank(returns, 40))",
            tag=f"repair-{tag}-ivratio120-sales-ev",
            family="research_options_distribution_fundamental_repair",
            strategy="option_distribution_fundamental_overlay",
            parent_alpha_ids=parent,
            rationale="Use a fundamental value overlay to repair pure options concentration or sub-universe failures.",
        ),
        _draft(
            "rank(0.40 * rank(group_neutralize((implied_volatility_call_90 - implied_volatility_put_90) / (implied_volatility_call_90 + implied_volatility_put_90), industry)) + 0.35 * ts_rank(cashflow_op / enterprise_value, 80) + 0.25 * rank(ts_corr(close, volume, 20)) - ts_rank(returns, 40))",
            tag=f"repair-{tag}-ivratio90-cfop-closevol",
            family="research_options_distribution_cashflow_repair",
            strategy="option_distribution_fundamental_overlay",
            parent_alpha_ids=parent,
            rationale="Blend normalized options skew with cashflow value and close-volume to reduce distribution risk.",
        ),
    ]


def _elite_variants(row: dict) -> list[dict]:
    expression = str(row.get("expression") or "").strip()
    if not expression:
        return []
    base = _strip_outer_rank(expression)
    tag = str(row.get("tag") or row.get("alpha_id") or "elite")
    parent = [row.get("alpha_id")] if row.get("alpha_id") else []
    return [
        _blend_draft(base, "rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5))", "elite-iv90", tag, parent, 0.80, 0.20),
        _blend_draft(base, "rank(ts_mean((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), 5))", "elite-iv120-ratio", tag, parent, 0.78, 0.22),
        _blend_draft(base, "rank(ts_corr(vwap, volume, 40))", "elite-vwap-volume", tag, parent, 0.75, 0.25),
        _blend_draft(base, "rank(ts_rank(snt1_d1_netearningsrevision, 60))", "elite-earnings-revision", tag, parent, 0.78, 0.22),
        _blend_draft(base, "rank(-1 * ts_rank(pcr_oi_10, 60))", "elite-pcr-oi", tag, parent, 0.80, 0.20),
    ]


def _exploration_templates(memory: list[dict]) -> list[dict]:
    return [
        _draft(
            "rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 10))",
            tag="explore-ivdiff90-mean10",
            family="research_options_skew_exploration",
            strategy="low_overlap_field_family",
            rationale="Explore options skew as a low-overlap field family.",
        ),
        _draft(
            "rank(group_neutralize((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), industry))",
            tag="explore-ivratio120-industry",
            family="research_options_skew_exploration",
            strategy="low_overlap_field_family",
            rationale="Historical platform memory showed standalone normalized 120-day option skew can be strong.",
        ),
        _draft(
            "rank(0.60 * rank(group_neutralize((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), industry)) + 0.40 * rank(-1 * ts_rank(returns, 120)))",
            tag="explore-ivratio120-industry-reversal",
            family="research_options_skew_exploration",
            strategy="option_term_reversal_overlay",
            rationale="Blend normalized option skew with long-horizon reversal to reduce single-signal risk.",
        ),
        _draft(
            "rank(0.55 * rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5)) + 0.45 * rank(-1 * ts_rank(returns, 120)))",
            tag="explore-ivdiff90-long-reversal",
            family="research_options_skew_exploration",
            strategy="low_overlap_field_family",
            rationale="Blend options skew with long-horizon reversal to improve robustness.",
        ),
        _draft(
            "rank(0.30 * ts_rank(forward_cash_flow_to_price, 80) + 0.25 * ts_rank(forward_book_value_to_price, 80) + 0.25 * rank(-1 * cashflow_efficiency_rank_derivative) + 0.20 * rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5)) - ts_rank(returns, 40))",
            tag="explore-forward-cashflow-eff-iv90",
            family="research_forward_cashflow_decorrelation",
            strategy="forward_value_iv_overlay",
            rationale="Combine forward valuation, model derivative, and options skew to avoid single-family crowding.",
        ),
        _draft(
            "rank(0.35 * ts_rank(anl4_adjusted_netincome_ft / cap, 50) + 0.35 * ts_rank(cashflow_op / cap, 80) + 0.30 * rank(-1 * cashflow_efficiency_rank_derivative) - ts_rank(returns, 40))",
            tag="explore-netincome-cfop-eff",
            family="research_analyst_cashflow_blend",
            strategy="analyst_cashflow_blend",
            rationale="Cross analyst net income with statement cashflow and model derivative.",
        ),
        _draft(
            "rank(0.45 * ts_rank(equity / cap, 60) + 0.25 * ts_rank(forward_sales_to_price, 60) + 0.15 * ts_rank(change_in_eps_surprise, 60) + 0.15 * ts_rank(snt1_d1_netearningsrevision, 60) - ts_mean(ts_rank(returns, 20), 2))",
            tag="explore-equity-sales-eps-revision",
            family="research_forward_revision_value",
            strategy="analyst_revision_value_blend",
            rationale="Use analyst revision and forward sales with value exposure seen in platform memory.",
        ),
        _draft(
            "rank(0.50 * ts_rank(actual_sales_value_quarterly / enterprise_value, 60) + 0.30 * ts_rank(earnings_momentum_composite_score, 50) + 0.20 * rank(ts_corr(vwap, volume, 40)) - ts_rank(returns, 30))",
            tag="explore-sales-ev-earnmom-vwap",
            family="research_sales_momentum_microstructure",
            strategy="fundamental_microstructure_blend",
            rationale="Combine actual sales, earnings momentum, and VWAP-volume microstructure.",
        ),
        _draft(
            "rank(0.40 * ts_rank(ebit / enterprise_value, 80) + 0.30 * ts_rank(forward_book_value_to_price, 80) + 0.30 * rank(pcr_oi_60) - ts_rank(returns, 30))",
            tag="explore-ebit-book-pcr-oi",
            family="research_value_options_oi",
            strategy="value_options_oi_blend",
            rationale="Value plus options open-interest pressure creates a distinct field family.",
        ),
    ]


def _platform_memory_candidates(rows: list[dict], *, limit: int, blocked_norms: set[str] | None = None) -> list[dict]:
    drafts: list[dict] = []
    seen: set[str] = set()
    blocked_norms = blocked_norms or set()
    for row in sorted(rows, key=_platform_memory_priority):
        if len(drafts) >= limit:
            break
        if str(row.get("status") or "").upper() != "UNSUBMITTED":
            continue
        expression = str(row.get("expression") or "").strip()
        if not expression:
            continue
        key = normalize_expression(expression)
        if key in seen:
            continue
        if key in blocked_norms:
            continue
        metrics = {"sharpe": row.get("sharpe"), "fitness": row.get("fitness"), "turnover": row.get("turnover")}
        if not submit_threshold_checks(metrics)["eligible"] and not _near_submit_metrics(row):
            continue
        draft = _draft(
            expression,
            tag=f"platform-memory-{row.get('alpha_id') or len(drafts) + 1}",
            family=_platform_memory_family(expression),
            strategy="recent_platform_memory_retest",
            parent_alpha_ids=[row.get("alpha_id")] if row.get("alpha_id") else [],
            rationale="Recent UNSUBMITTED platform alpha had usable metrics; recheck under current strict inventory.",
        )
        draft.update({
            "sharpe": row.get("sharpe"),
            "fitness": row.get("fitness"),
            "turnover": row.get("turnover"),
            "candidate_meta": {
                "platform_alpha_id": row.get("alpha_id"),
                "platform_status": row.get("status"),
                "platform_metrics": metrics,
            },
        })
        drafts.append(draft)
        seen.add(key)
    return drafts


def _weak_active_memory_candidates(rows: list[dict], *, limit: int) -> list[dict]:
    drafts: list[dict] = []
    seen: set[str] = set()
    for row in sorted(rows, key=_weak_memory_priority):
        if len(drafts) >= limit:
            break
        expression = str(row.get("expression") or "").strip()
        if not expression:
            continue
        for draft in _weak_active_repair_variants(row):
            key = normalize_expression(str(draft.get("expression") or ""))
            if not key or key in seen:
                continue
            drafts.append(draft)
            seen.add(key)
            if len(drafts) >= limit:
                break
    return drafts


def _weak_active_repair_variants(row: dict) -> list[dict]:
    expression = str(row.get("expression") or "").strip()
    if not expression:
        return []
    base = _strip_outer_rank(expression)
    tag = str(row.get("tag") or row.get("alpha_id") or "weak-active")
    parent = [row.get("alpha_id")] if row.get("alpha_id") else []
    failure_kind = str(row.get("failure_kind") or "active_metric_mixed")
    reasons = {str(item) for item in row.get("weak_reasons") or []}
    hints = [str(item) for item in row.get("repair_hints") or []]
    variants: list[dict] = []

    if failure_kind in {"active_correlation_risk", "active_crowded_family"} or reasons & {
        "correlation_risk",
        "crowded_field_signature",
        "crowded_fields",
    }:
        variants.extend([
            _blend_draft(base, "rank(ts_corr(vwap, volume, 40))", "weak-active-vwap-volume", tag, parent, 0.58, 0.42),
            _blend_draft(
                base,
                "rank(ts_mean((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), 5))",
                "weak-active-ivratio120",
                tag,
                parent,
                0.56,
                0.44,
            ),
            _draft(
                "rank(0.35 * rank(ts_corr(vwap, volume, 40)) + 0.35 * rank(-1 * ts_rank(returns, 120)) + 0.30 * rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5)))",
                tag=f"repair-{tag}-weak-active-replacement-micro-iv",
                family="research_weak_active_replacement",
                strategy="weak_active_replace_crowded_or_correlated",
                parent_alpha_ids=parent,
                rationale="Replace a weak active structure with lower-overlap microstructure, reversal, and options legs.",
            ),
        ])

    if failure_kind in {"active_low_returns", "active_low_fitness", "active_metric_mixed"} or reasons & {
        "negative_returns",
        "low_returns",
        "low_fitness",
        "near_low_fitness",
        "relative_laggard",
    }:
        variants.extend([
            _draft(
                f"rank(0.45 * rank(-1 * ({base})) + 0.35 * rank(-1 * ts_std(returns, 20)) + 0.20 * rank(ts_corr(vwap, volume, 40)))",
                tag=f"repair-{tag}-weak-active-invert-lowvol",
                family="research_weak_active_return_repair",
                strategy="weak_active_invert_and_low_vol_overlay",
                parent_alpha_ids=parent,
                rationale="Invert a weak active signal and blend with lower-volatility and microstructure legs.",
            ),
            _draft(
                "rank(0.35 * ts_rank(forward_cash_flow_to_price, 80) + 0.30 * ts_rank(forward_book_value_to_price, 80) + 0.20 * rank(ts_corr(vwap, volume, 40)) + 0.15 * rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5)) - ts_rank(returns, 40))",
                tag=f"repair-{tag}-weak-active-forward-value-replacement",
                family="research_weak_active_replacement",
                strategy="weak_active_replace_low_return_fitness",
                parent_alpha_ids=parent,
                rationale="Use a stronger forward-value replacement instead of spending budget on the weak standalone structure.",
            ),
        ])

    if failure_kind == "active_turnover_drag" or reasons & {
        "turnover_outside_submit_band",
        "turnover_drag",
        "turnover_missing",
    }:
        variants.extend([
            _draft(
                f"rank(ts_mean({base}, 5))",
                tag=f"repair-{tag}-weak-active-smooth5",
                family="research_weak_active_turnover_repair",
                strategy="weak_active_smooth_turnover",
                parent_alpha_ids=parent,
                rationale="Smooth the weak active signal to move turnover away from inefficient edges.",
            ),
            _draft(
                f"rank(0.70 * rank(ts_mean({base}, 10)) + 0.30 * rank(-1 * ts_rank(returns, 120)))",
                tag=f"repair-{tag}-weak-active-smooth10-reversal",
                family="research_weak_active_turnover_repair",
                strategy="weak_active_smooth_with_reversal",
                parent_alpha_ids=parent,
                rationale="Use a slower version plus reversal overlay to repair turnover drag.",
            ),
        ])

    if hints and not variants:
        variants.append(_blend_draft(base, "rank(-1 * ts_rank(returns, 120))", "weak-active-reversal", tag, parent, 0.60, 0.40))
    return variants


def _weak_memory_priority(row: dict) -> tuple:
    return (
        -(_safe_float(row.get("weak_score")) or 0.0),
        _safe_float(row.get("quality_percentile")) if _safe_float(row.get("quality_percentile")) is not None else 1.0,
        str(row.get("alpha_id") or ""),
    )


def _platform_memory_family(expression: str) -> str:
    fields = components_for(expression)["fields"] if expression else set()
    prefix = "research_platform_unsubmitted"
    if _is_option_only_fields(fields):
        return f"{prefix}_options_only"
    if fields & {
        "analyst_revision_rank_derivative",
        "cashflow_efficiency_rank_derivative",
        "composite_factor_score_derivative",
        "earnings_certainty_rank_derivative",
        "growth_potential_rank_derivative",
        "multi_factor_acceleration_score_derivative",
        "relative_valuation_rank_derivative",
    }:
        return f"{prefix}_model_derivative"
    if fields & {
        "forward_book_value_to_price",
        "forward_cash_flow_to_price",
        "forward_earnings_yield",
        "forward_sales_to_price",
    }:
        return f"{prefix}_forward_value"
    if fields & {
        "actual_eps_value_quarterly",
        "anl4_af_eps_value",
        "anl4_adjusted_netincome_ft",
        "anl4_afv4_eps_mean",
        "change_in_eps_surprise",
        "snt1_d1_netearningsrevision",
    }:
        return f"{prefix}_analyst_revision"
    if fields & {
        "actual_cashflow_per_share_value_quarterly",
        "cashflow",
        "cashflow_fin",
        "cashflow_op",
    }:
        return f"{prefix}_cashflow_value"
    if {"high", "low", "close"} <= fields:
        return f"{prefix}_intraday_reversal"
    return f"{prefix}_memory"


def _systematic_research_templates() -> list[dict]:
    """Broad deterministic library used when memory repairs are too narrow."""

    base_legs = [
        ("cfop-cap80", "ts_rank(cashflow_op / cap, 80)", "cashflow_value"),
        ("cfop-ev80", "ts_rank(cashflow_op / enterprise_value, 80)", "cashflow_value"),
        ("cfop-assets80", "ts_rank(cashflow_op / assets, 80)", "cashflow_value"),
        ("sales-assets60", "ts_rank(actual_sales_value_quarterly / assets, 60)", "sales_value"),
        ("sales-ev60", "ts_rank(actual_sales_value_quarterly / enterprise_value, 60)", "sales_value"),
        ("cashps-close60", "ts_rank(actual_cashflow_per_share_value_quarterly / close, 60)", "cashflow_value"),
        ("ebit-ev80", "ts_rank(ebit / enterprise_value, 80)", "earnings_value"),
        ("equity-cap60", "ts_rank(equity / cap, 60)", "balance_sheet_value"),
        ("forward-sales60", "ts_rank(forward_sales_to_price, 60)", "forward_value"),
        ("forward-book80", "ts_rank(forward_book_value_to_price, 80)", "forward_value"),
        ("forward-cf80", "ts_rank(forward_cash_flow_to_price, 80)", "forward_value"),
        ("divgp60", "ts_rank(dividends_to_gross_profit, 60)", "dividend_quality"),
        ("mdf-quality60", "ts_rank(mdf_quality, 60)", "model_factor"),
        ("mdf-cfp60", "ts_rank(mdf_cfp, 60)", "model_factor"),
        ("anl-netincome-cap50", "ts_rank(anl4_adjusted_netincome_ft / cap, 50)", "analyst_income"),
    ]
    overlay_legs = [
        ("earnmom50", "ts_rank(earnings_momentum_composite_score, 50)", "analyst_momentum"),
        ("eps-surprise60", "ts_rank(change_in_eps_surprise, 60)", "analyst_revision"),
        ("netearnrev60", "ts_rank(snt1_d1_netearningsrevision, 60)", "analyst_revision"),
        ("sentiment20", "ts_rank(scl12_sentiment_fast_d1, 20)", "sentiment"),
        ("iv90diff5", "rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5))", "options_skew"),
        (
            "iv120ratio5",
            "rank(ts_mean((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), 5))",
            "options_skew",
        ),
        ("vwap-close20", "rank(ts_mean(ts_rank(vwap / close, 20), 3))", "microstructure"),
        ("vwap-volume40", "rank(ts_corr(vwap, volume, 40))", "microstructure"),
        ("intraday-adv20", "rank((high - close) / (high - low) * volume / adv20)", "microstructure"),
        ("lowvol20", "rank(-1 * ts_std(returns, 20))", "risk_reversal"),
        ("pcr-vol10", "rank(-1 * ts_rank(pcr_vol_10, 60))", "options_flow"),
        ("pcr-oi10", "rank(-1 * ts_rank(pcr_oi_10, 60))", "options_flow"),
    ]
    return_legs = [
        ("ret20", "ts_rank(returns, 20)"),
        ("ret40", "ts_rank(returns, 40)"),
        ("retmean20", "ts_mean(ts_rank(returns, 20), 2)"),
    ]

    drafts: list[dict] = []
    for base_name, base_expr, base_family in base_legs:
        for overlay_name, overlay_expr, overlay_family in overlay_legs:
            for return_name, return_expr in return_legs:
                drafts.append(_draft(
                    f"rank(0.50 * {base_expr} + 0.30 * {overlay_expr} - 0.20 * {return_expr})",
                    tag=f"explore-{base_name}-{overlay_name}-{return_name}",
                    family=f"research_{base_family}_{overlay_family}",
                    strategy="systematic_local_factor_grid",
                    rationale="Local deterministic grid combining value, orthogonal overlay, and return reversal.",
                ))

    option_terms = [
        (
            "option-ivratio90-industry",
            "rank(group_neutralize((implied_volatility_call_90 - implied_volatility_put_90) / (implied_volatility_call_90 + implied_volatility_put_90), industry))",
            "research_options_term_structure",
        ),
        (
            "option-ivratio120-industry",
            "rank(group_neutralize((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), industry))",
            "research_options_term_structure",
        ),
        (
            "option-skew-slope",
            "rank(0.55 * ts_rank(implied_volatility_skew, 60) + 0.45 * ts_rank(implied_volatility_slope, 60) - ts_rank(returns, 30))",
            "research_options_term_structure",
        ),
        (
            "option-flow-reversal",
            "rank(0.45 * rank(-1 * ts_rank(pcr_oi_10, 60)) + 0.35 * rank(-1 * ts_rank(pcr_vol_10, 60)) + 0.20 * rank(-1 * ts_rank(returns, 120)))",
            "research_options_flow",
        ),
        (
            "sentiment-revision-reversal",
            "rank(0.40 * ts_rank(scl12_sentiment_fast_d1, 20) + 0.40 * ts_rank(snt1_d1_netearningsrevision, 60) - 0.20 * ts_rank(returns, 30))",
            "research_sentiment_revision",
        ),
        (
            "relationship-pcr-value",
            "rank(0.45 * ts_rank(rel_ret_cust, 60) + 0.35 * rank(-1 * ts_rank(pcr_oi_10, 60)) + 0.20 * ts_rank(forward_cash_flow_to_price, 80) - ts_rank(returns, 30))",
            "research_relationship_options_flow",
        ),
        (
            "sentiment-delta5",
            "rank(ts_delta(scl12_sentiment_fast_d1, 5))",
            "research_sentiment_standalone",
        ),
        (
            "revision-delta10",
            "rank(ts_delta(snt1_d1_netearningsrevision, 10))",
            "research_sentiment_revision",
        ),
        (
            "options-oi-reversal",
            "rank(rank(-1 * ts_rank(pcr_oi_10, 60)) + rank(-1 * ts_rank(returns, 120)))",
            "research_options_flow",
        ),
        (
            "relationship-supplier-return",
            "rank(ts_rank(rel_ret_supp, 60) - ts_rank(returns, 30))",
            "research_relationship_standalone",
        ),
        (
            "relationship-customer-momentum",
            "rank(ts_rank(rel_momentum, 60) + 0.25 * rank(-1 * ts_rank(returns, 120)))",
            "research_relationship_standalone",
        ),
        (
            "micro-vwap-close-delta",
            "rank(-1 * ts_delta(vwap / close, 5))",
            "research_microstructure_standalone",
        ),
        (
            "micro-volume-corr",
            "rank(ts_corr(close, volume, 20))",
            "research_microstructure_standalone",
        ),
        (
            "mdf-quality-leverage",
            "rank(ts_rank(mdf_quality, 60) - ts_rank(mdf_leverage, 60) - ts_rank(returns, 30))",
            "research_model_factor_standalone",
        ),
        (
            "mdf-roic-value",
            "rank(0.50 * ts_rank(mdf_roic, 60) + 0.50 * ts_rank(mdf_cfp, 60) - ts_rank(returns, 30))",
            "research_model_factor_standalone",
        ),
    ]
    standalone_drafts: list[dict] = []
    for tag, expression, family in option_terms:
        standalone_drafts.append(_draft(
            expression,
            tag=f"explore-{tag}",
            family=family,
            strategy="standalone_low_overlap_research_family",
            rationale="Standalone low-overlap family from local research memory and platform field coverage.",
        ))

    return [*standalone_drafts, *drafts]


def _blend_draft(
    base: str,
    overlay: str,
    overlay_name: str,
    tag: str,
    parent_alpha_ids: list[Any],
    base_weight: float,
    overlay_weight: float,
) -> dict:
    expression = f"rank({base_weight:.2f} * rank({base}) + {overlay_weight:.2f} * {overlay})"
    return _draft(
        expression,
        tag=f"repair-{tag}-{overlay_name}-{int(base_weight * 100)}{int(overlay_weight * 100)}",
        family=f"research_decorrelation_{overlay_name.replace('-', '_')}",
        strategy=f"blend_with_{overlay_name}",
        parent_alpha_ids=parent_alpha_ids,
        rationale=f"Blend near-miss signal with {overlay_name} to reduce similarity while preserving core exposure.",
    )


def _draft(
    expression: str,
    *,
    tag: str,
    family: str,
    strategy: str,
    rationale: str,
    parent_alpha_ids: list[Any] | None = None,
) -> dict:
    return {
        "expression": expression,
        "tag": tag,
        "source_family": family,
        "mutation_strategy": strategy,
        "rationale": rationale,
        "expected_low_corr_reason": "Generated by local experience-memory planner; screened against active/virtual inventory before WQ simulation.",
        "parent_alpha_ids": [str(item) for item in parent_alpha_ids or [] if item],
        "risk_flags": [],
    }


def _memory_record(
    row: dict,
    *,
    memory_kind: str,
    severity: str,
    failure_kind: str,
    lesson: str,
    similarity_cutoff: float,
) -> dict:
    expression = str(row.get("expression") or "")
    components = components_for(expression)
    return {
        "memory_kind": memory_kind,
        "severity": severity,
        "failure_kind": failure_kind,
        "lesson": lesson,
        "alpha_id": row.get("alpha_id"),
        "tag": row.get("tag"),
        "expression": expression,
        "expression_normalized": normalize_expression(expression),
        "fields": sorted(components["fields"]),
        "operators": sorted(components["operators"]),
        "field_signature": field_signature(expression),
        "source_family": _row_family(row),
        "sharpe": row.get("sharpe"),
        "fitness": row.get("fitness"),
        "turnover": row.get("turnover"),
        "sc_value": row.get("sc_value"),
        "prod_corr_value": row.get("prod_corr_value"),
        "nearest_similarity": row.get("nearest_similarity"),
        "similarity_cutoff": similarity_cutoff,
        "presubmit_reject_reason": row.get("presubmit_reject_reason"),
        "failed_platform_checks": row.get("failed_platform_checks") or [],
    }


def _failure_lesson(row: dict, failure_kind: str) -> str:
    if failure_kind == "self_correlation_high":
        return "Do not rerun the same structure; change field family or add a small orthogonal overlay before WQ simulation."
    if failure_kind == "high_similarity":
        return "Avoid formulaic near-duplicates; require field-family or operator-family change."
    if failure_kind == "platform_distribution_fail":
        return "Repair concentration or sub-universe failures with smoothing, group neutralization, or lower-concentration overlays."
    if failure_kind == "base_metric_fail":
        return "Treat as weak unless it is part of a stronger blend; do not spend standalone simulation budget."
    if failure_kind == "prod_correlation_fail":
        return "Switch signal source and structure, not just numeric parameters."
    return str(row.get("triage_reason") or row.get("presubmit_reject_reason") or "Historical rejected pattern.")


def _failure_severity(failure_kind: str) -> str:
    if failure_kind in {"self_correlation_high", "high_similarity", "prod_correlation_fail"}:
        return "block_exact_penalize_family"
    if failure_kind in {"platform_distribution_fail", "base_metric_fail", "platform_check_fail"}:
        return "penalize"
    return "note"


def _is_repairable(row: dict) -> bool:
    metrics = {"sharpe": row.get("sharpe"), "fitness": row.get("fitness"), "turnover": row.get("turnover")}
    gate = submit_threshold_checks(metrics)
    if gate["eligible"]:
        return True
    sharpe = _safe_float(row.get("sharpe")) or 0.0
    fitness = _safe_float(row.get("fitness")) or 0.0
    turnover = _safe_float(row.get("turnover"))
    return sharpe >= 1.15 and fitness >= 0.85 and turnover is not None and 0.005 <= turnover <= 0.8


def _repair_priority(row: dict) -> tuple:
    failure_rank = {
        "self_correlation_high": 0,
        "high_similarity": 1,
        "platform_distribution_fail": 2,
        "base_metric_fail": 3,
    }.get(infer_failure_kind(row), 9)
    return (
        failure_rank,
        -(_safe_float(row.get("fitness")) or -999.0),
        -(_safe_float(row.get("sharpe")) or -999.0),
        abs((_safe_float(row.get("sc_value")) or 0.7) - 0.7),
    )


def _draft_priority(row: dict) -> tuple:
    """Prefer structurally different local ideas before near-miss repair loops."""

    family = str(row.get("source_family") or "")
    strategy = str(row.get("mutation_strategy") or "")
    tag = str(row.get("tag") or "")
    expression = str(row.get("expression") or "")
    fields = components_for(expression)["fields"] if expression else set()

    if family.startswith("research_platform_unsubmitted_"):
        return (
            0,
            -(_safe_float(row.get("fitness")) or 0.0),
            -(_safe_float(row.get("sharpe")) or 0.0),
            _safe_float(row.get("turnover")) or 999.0,
            tag,
        )

    if strategy.startswith("weak_active_"):
        family_rank = 1
    elif family.startswith("research_weak_active_"):
        family_rank = 1
    elif strategy == "targeted_near_pass_decorrelation":
        family_rank = 0
    elif strategy == "standalone_low_overlap_research_family":
        family_rank = 0
    elif strategy in {"systematic_local_factor_grid", "low_overlap_field_family", "option_term_reversal_overlay"}:
        family_rank = 1
    elif family == "research_platform_distribution_repair":
        family_rank = 2
    elif family in {"research_options_skew_exploration", "research_forward_revision_value", "research_value_options_oi"}:
        family_rank = 3
    elif family.startswith("research_platform_unsubmitted_"):
        family_rank = 4
    elif strategy.startswith("elite"):
        family_rank = 5
    elif family.startswith("research_decorrelation_"):
        family_rank = 7
    else:
        family_rank = 6

    repair_chain_penalty = tag.count("repair-")
    repeated_overlay_penalty = expression.count("ts_rank(returns, 120)")
    field_count_bonus = -min(len(fields), 8)
    metric_bonus = -(_safe_float(row.get("fitness")) or 0.0)
    return (
        family_rank,
        repair_chain_penalty,
        repeated_overlay_penalty,
        field_count_bonus,
        metric_bonus,
        tag,
    )


def _draft_bucket(row: dict) -> str:
    family = str(row.get("source_family") or "")
    strategy = str(row.get("mutation_strategy") or "")
    if _is_pure_options_distribution(row):
        return "low_priority"
    if strategy.startswith("weak_active_") or family.startswith("research_weak_active_"):
        return "weak_active_repair"
    if strategy == "targeted_near_pass_decorrelation":
        return "targeted_near_pass"
    if strategy == "standalone_low_overlap_research_family":
        return "standalone"
    if family.startswith("research_platform_unsubmitted_"):
        return "platform_memory"
    if strategy in {"systematic_local_factor_grid", "low_overlap_field_family", "option_term_reversal_overlay"}:
        return "exploration"
    if family == "research_platform_distribution_repair":
        return "distribution_repair"
    if strategy.startswith("elite"):
        return "elite"
    if family.startswith("research_decorrelation_"):
        return "decorrelation_repair"
    return "other"


def _interleave_drafts(drafts: list[dict], *, limit: int) -> list[dict]:
    buckets: dict[str, list[dict]] = {
        "targeted_near_pass": [],
        "weak_active_repair": [],
        "standalone": [],
        "platform_memory": [],
        "exploration": [],
        "distribution_repair": [],
        "other": [],
        "elite": [],
        "decorrelation_repair": [],
        "low_priority": [],
    }
    for draft in drafts:
        buckets.setdefault(_draft_bucket(draft), []).append(draft)
    for bucket_rows in buckets.values():
        bucket_rows.sort(key=_draft_priority)

    ordered: list[dict] = []
    bucket_order = [
        "targeted_near_pass",
        "decorrelation_repair",
        "weak_active_repair",
        "platform_memory",
        "elite",
        "exploration",
        "standalone",
        "distribution_repair",
        "other",
        "low_priority",
    ]
    while len(ordered) < limit and any(buckets.get(name) for name in bucket_order):
        for name in bucket_order:
            rows = buckets.get(name) or []
            if not rows:
                continue
            ordered.append(rows.pop(0))
            if len(ordered) >= limit:
                break
    return ordered


def _is_pure_options_distribution(row: dict) -> bool:
    family = str(row.get("source_family") or "")
    if family != "research_platform_distribution_repair" and not family.startswith("research_platform_unsubmitted_"):
        return False
    expression = str(row.get("expression") or "")
    fields = components_for(expression)["fields"] if expression else set()
    if not fields:
        return False
    return _is_option_only_fields(fields)


def _is_option_only_fields(fields: set[str]) -> bool:
    return bool(fields) and fields <= OPTION_FIELDS


def _elite_priority(row: dict) -> tuple:
    return (
        -(_safe_float(row.get("fitness")) or -999.0),
        -(_safe_float(row.get("sharpe")) or -999.0),
        _safe_float(row.get("turnover")) or 999.0,
    )


def _platform_memory_priority(row: dict) -> tuple:
    return (
        0 if submit_threshold_checks({"sharpe": row.get("sharpe"), "fitness": row.get("fitness"), "turnover": row.get("turnover")})["eligible"] else 1,
        -(_safe_float(row.get("fitness")) or -999.0),
        -(_safe_float(row.get("sharpe")) or -999.0),
        _safe_float(row.get("turnover")) or 999.0,
    )


def _near_submit_metrics(row: dict) -> bool:
    sharpe = _safe_float(row.get("sharpe")) or 0.0
    fitness = _safe_float(row.get("fitness")) or 0.0
    turnover = _safe_float(row.get("turnover"))
    return sharpe >= 1.25 and fitness >= 0.85 and turnover is not None and 0.01 <= turnover <= 0.7


def _row_family(row: dict) -> str:
    return str(row.get("source_family") or row.get("mutation_strategy") or (row.get("candidate_meta") or {}).get("source_family") or "")


def _strip_outer_rank(expression: str) -> str:
    text = expression.strip()
    lower = text.lower()
    if not lower.startswith("rank(") or not text.endswith(")"):
        return text
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and index != len(text) - 1:
                return text
    return text[text.find("(") + 1:-1].strip()


def _max_parenthesis_depth(expression: str) -> int:
    depth = 0
    max_depth = 0
    for char in expression:
        if char == "(":
            depth += 1
            max_depth = max(max_depth, depth)
        elif char == ")":
            depth = max(0, depth - 1)
    return max_depth


def _virtual_active_rows(ready_rows: list[dict]) -> list[dict]:
    rows = []
    for row in ready_rows:
        expression = str(row.get("expression") or "")
        if not expression:
            continue
        rows.append({
            "alpha_id": row.get("alpha_id"),
            "expression": expression,
            "status": "VIRTUAL_ACTIVE",
            "source_family": _row_family(row),
        })
    return rows


def normalize_weak_active_memory(rows: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for row in rows:
        expression = str(row.get("expression") or "").strip()
        if not expression:
            continue
        components = components_for(expression)
        normalized.append({
            **row,
            "memory_kind": row.get("memory_kind") or "weak_active_constraint",
            "severity": row.get("severity") or "penalize",
            "failure_kind": row.get("failure_kind") or "active_metric_mixed",
            "lesson": row.get("lesson") or "Weak ACTIVE/SUBMITTED pattern; avoid standalone reuse without material change.",
            "expression": expression,
            "expression_normalized": row.get("expression_normalized") or normalize_expression(expression),
            "fields": row.get("fields") or sorted(components["fields"]),
            "operators": row.get("operators") or sorted(components["operators"]),
            "field_signature": row.get("field_signature") or field_signature(expression),
        })
    return _dedupe_memory(normalized)


def _weak_memory_inventory_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        expression = str(row.get("expression") or "")
        if not expression:
            continue
        out.append({
            "alpha_id": row.get("alpha_id"),
            "expression": expression,
            "status": "WEAK_ACTIVE_MEMORY",
            "source_family": row.get("failure_kind") or row.get("memory_kind"),
        })
    return out


def _dedupe_rows(rows: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for row in rows:
        expression = str(row.get("expression") or "")
        key = normalize_expression(expression) if expression else f"id:{row.get('alpha_id')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _dedupe_candidates(rows: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for row in rows:
        expression = str(row.get("expression") or "")
        if not expression:
            continue
        key = normalize_expression(expression)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _dedupe_memory(rows: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for row in rows:
        key = (row.get("memory_kind"), row.get("failure_kind"), row.get("expression_normalized"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _run_artifact_paths(run_dirs: tuple[Path, ...], *names: str) -> tuple[Path, ...]:
    paths: list[Path] = []
    for run_dir in run_dirs:
        if not run_dir or not run_dir.exists() or not run_dir.is_dir():
            continue
        for name in names:
            path = run_dir / name
            if path.is_file():
                paths.append(path)
    return _dedupe_paths(tuple(paths))


def _run_cycle_artifact_paths(run_dirs: tuple[Path, ...], *names: str) -> tuple[Path, ...]:
    paths: list[Path] = []
    for run_dir in run_dirs:
        cycles_dir = run_dir / "cycles"
        if not cycles_dir.is_dir():
            continue
        for cycle_dir in sorted(cycles_dir.glob("cycle_*")):
            if not cycle_dir.is_dir():
                continue
            for name in names:
                path = cycle_dir / name
                if path.is_file():
                    paths.append(path)
    return _dedupe_paths(tuple(paths))


def _dedupe_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        if not path:
            continue
        key = str(path.resolve() if path.exists() else path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return tuple(out)


def _load_rows(paths: tuple[Path, ...]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        if not path or not path.exists():
            continue
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        if path.suffix.lower() == ".json":
            payload = json.loads(text)
            rows.extend(_rows_from_payload(payload))
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rows.extend(_rows_from_payload(json.loads(line)))
    return rows


def _load_inventory_rows(paths: tuple[Path, ...]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        if not path or not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            rows.extend(_rows_from_payload(payload.get("active") or payload.get("rows") or payload))
        else:
            rows.extend(_rows_from_payload(payload))
    return rows


def _rows_from_payload(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("ready"), list):
            return [item for item in payload["ready"] if isinstance(item, dict)]
        if isinstance(payload.get("active"), list):
            return [item for item in payload["active"] if isinstance(item, dict)]
        return [payload]
    return []


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
    path.write_text(text + ("\n" if text else ""), encoding="utf-8")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
