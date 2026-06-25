"""Local research planner for WorldQuant factor mining.

This module turns previous presubmit artifacts into a deterministic candidate
file. It never calls an LLM provider, never talks to WQ BRAIN, and never submits.
The output is intended to be fed into ``wq_agent_workflow.py presubmit-sequential``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .alpha_tracker import compute_similarity
from .expression_parser import extract_components, normalize_expression
from .wq_auto_mining import validate_wq_expression
from .wq_brain_service import submit_threshold_checks
from .wq_forum_submission_optimizer import annotate_candidate_with_policy, load_submission_policy
from .wq_legal_inputs import load_optional_legal_input_registry

INVALID_WQ_FIELDS = {
    "short_interest",
    "short_ratio",
    "implied_volatility_skew",
    "implied_volatility_slope",
    "open_interest",
    "rel_ret_cust",
    "rel_ret_supp",
    "short_sale_cost",
    "mdf_roic",
    "mdf_cfp",
    "rel_momentum",
    "mdf_quality",
    "mdf_leverage",
}

OPTION_FIELDS = {
    "implied_volatility_call_30",
    "implied_volatility_put_30",
    "implied_volatility_call_60",
    "implied_volatility_put_60",
    "implied_volatility_call_90",
    "implied_volatility_put_90",
    "implied_volatility_call_120",
    "implied_volatility_put_120",
    "pcr_oi_10",
    "pcr_oi_60",
    "pcr_vol_10",
    "industry",
}

PLATFORM_BLOCKER_COMMON_FIELDS = {
    "returns",
    "industry",
    "sector",
    "subindustry",
    "market",
    "range",
}

SPARSE_CONCENTRATION_FIELDS = {
    "actual_dividend_value_quarterly",
    "cashflow_op",
    "dividends_to_gross_profit",
    "enterprise_value",
}
SPARSE_CONCENTRATION_PREFIXES = ("pcr_",)
GROUP_DISTRIBUTION_OPERATORS = {
    "group_neutralize",
    "group_rank",
    "group_zscore",
}
GROUP_FIELDS = {"industry", "sector", "subindustry", "market"}
BROAD_DISPERSION_FIELDS = {
    "adv20",
    "cap",
    "close",
    "high",
    "low",
    "open",
    "volume",
    "vwap",
    "forward_book_value_to_price",
    "forward_cash_flow_to_price",
    "forward_sales_to_price",
    "coefficient_variation_fy1_eps",
    "credit_risk_premium_indicator",
    "earnings_certainty_rank_derivative",
    "relative_valuation_rank_derivative",
}
BROAD_DISPERSION_DATASETS = {"model16", "model77"}
PRICE_VOLUME_DISPERSION_FIELDS = {"adv20", "close", "high", "low", "open", "volume", "vwap"}
FRESH_ANCHOR_EXCLUDE_FIELDS = PRICE_VOLUME_DISPERSION_FIELDS | GROUP_FIELDS | {
    "returns",
    "cap",
    "range",
}
EVENT_REACTION_FIELDS = {
    "news_open_gap",
    "news_max_dn_ret",
    "news_mins_10_chg",
    "news_mov_vol",
    "scl12_buzz_fast_d1",
    "scl12_sentiment_fast_d1",
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
    legal_inputs_file: Path | None = None
    strict_legal_inputs: bool = True
    memory_output: Path | None = None
    summary_output: Path | None = None
    account: str = "primary"
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    max_candidates: int = 40
    similarity_cutoff: float = 0.65
    max_family_count: int = 3
    max_field_signature_count: int = 2
    max_expression_length: int = 500
    max_nesting: int = 10
    platform_blocker_min_correlation: float = 0.70
    platform_blocker_field_jaccard_cutoff: float = 0.62
    llm_provider: str = "none"


def run_research_miner(config: WQResearchMinerConfig) -> dict:
    """Generate a local candidate JSONL and research memory from prior runs."""

    if config.llm_provider != "none":
        raise ValueError("Only llm_provider='none' is supported; this planner is local-only")

    run_ready_files = _run_artifact_paths(config.run_dirs, "presubmit_ready_sequential.jsonl")
    run_rejected_files = _run_artifact_paths(
        config.run_dirs,
        "presubmit_rejected.jsonl",
        "check_results.jsonl",
        "submit_existing_results.jsonl",
    )
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

    platform_blocker_memory = build_platform_self_correlation_memory(
        rejected_rows,
        active_rows,
        min_correlation=config.platform_blocker_min_correlation,
        similarity_cutoff=config.similarity_cutoff,
    )
    memory = _dedupe_memory([
        *build_experience_memory(ready_rows, rejected_rows, similarity_cutoff=config.similarity_cutoff),
        *platform_blocker_memory,
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
        platform_blocker_rows=platform_blocker_memory,
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
            "platform_self_correlation_blockers": len(platform_blocker_memory),
            "comparison_inventory": len(comparison_rows),
            "submission_policy": str(config.submission_policy_file) if config.submission_policy_file else "",
            "legal_inputs": str(config.legal_inputs_file) if config.legal_inputs_file else "",
            "strict_legal_inputs": bool(config.strict_legal_inputs),
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


def build_platform_self_correlation_memory(
    rejected_rows: list[dict],
    active_rows: list[dict],
    *,
    min_correlation: float = 0.70,
    similarity_cutoff: float = 0.65,
) -> list[dict]:
    """Build hard blocker memory from live platform self-correlation returns."""

    active_by_id = _active_rows_by_alpha_id(active_rows)
    memory: list[dict] = []
    for row in rejected_rows:
        records = [
            item for item in _self_correlated_records(row)
            if (_safe_float(item.get("correlation")) or 0.0) >= min_correlation
        ]
        sc_value = _row_self_correlation_value(row)
        is_sc_failure = (
            records
            or (sc_value is not None and sc_value >= min_correlation)
            or infer_failure_kind(row, similarity_cutoff=similarity_cutoff) == "self_correlation_high"
        )
        if not is_sc_failure:
            continue

        expression = str(row.get("expression") or "").strip()
        anchor_ids = [str(item.get("id") or "") for item in records if item.get("id")]
        if expression:
            memory.append(_platform_blocker_record(
                row,
                expression=expression,
                blocker_type="failed_candidate",
                failure_kind="platform_self_correlation_fail",
                anchor_ids=anchor_ids,
                platform_correlation=sc_value,
                similarity_cutoff=similarity_cutoff,
            ))

        for record in records:
            anchor_id = str(record.get("id") or "")
            anchor = active_by_id.get(anchor_id)
            anchor_expression = str((anchor or {}).get("expression") or "").strip()
            if not anchor_expression:
                continue
            memory.append(_platform_blocker_record(
                anchor,
                expression=anchor_expression,
                blocker_type="active_anchor",
                failure_kind="platform_self_correlation_anchor",
                anchor_ids=[anchor_id],
                platform_correlation=_safe_float(record.get("correlation")),
                source_failed_alpha_id=row.get("alpha_id"),
                source_failed_expression=expression,
                similarity_cutoff=similarity_cutoff,
            ))

    return _dedupe_memory(memory)


def infer_failure_kind(row: dict, *, similarity_cutoff: float = 0.65) -> str:
    reason = str(row.get("presubmit_reject_reason") or row.get("triage_reason") or row.get("status") or "").lower()
    api_status = str(row.get("api_check_status") or "").lower()
    if not api_status and isinstance(row.get("live_precheck"), dict):
        live_failure = str(row["live_precheck"].get("failure_kind") or "").lower()
        if live_failure == "self_correlation":
            api_status = "self_correlation_fail"
        elif live_failure == "prod_correlation":
            api_status = "prod_correlation_fail"
    sc_value = _safe_float(row.get("sc_value"))
    if sc_value is None:
        sc_value = _row_self_correlation_value(row)
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

    drafts.extend(_fresh_anchor_templates())
    drafts.extend(_platform_memory_candidates(
        platform_rows or [],
        limit=min(12, max(limit // 20, 6)),
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
    platform_blocker_rows: list[dict] | None = None,
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
    distribution_signature_counts = _platform_distribution_signature_counts(blocked_rows or [])
    blocked_distribution_signatures = {
        signature for signature, count in distribution_signature_counts.items() if count >= 2
    }
    platform_blockers = list(platform_blocker_rows or [])
    seen = set(active_norms)
    family_counts: Counter[str] = Counter()
    signature_counts: Counter[str] = Counter()
    legal_registry = load_optional_legal_input_registry(config.legal_inputs_file)

    for draft in drafts:
        expression = str(draft.get("expression") or "").strip()
        legal_validation = None
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
        if legal_registry is not None:
            legal_validation = legal_registry.validate_candidate(
                draft,
                account=config.account,
                region=config.region,
                universe=config.universe,
                delay=config.delay,
                strict=config.strict_legal_inputs,
            )
            if not legal_validation.ok:
                rejected.append({
                    **draft,
                    "reject_reason": legal_validation.primary_error_code(),
                    "legal_input_validation": legal_validation.to_dict(),
                })
                continue

        concentration_risk = _concentration_sparse_group_risk(
            expression,
            components,
            draft=draft,
            legal_validation=legal_validation.to_dict() if legal_validation is not None else None,
        )
        if concentration_risk:
            rejected.append({
                **draft,
                "reject_reason": "concentration_sparse_group_risk",
                "concentration_risk": concentration_risk,
            })
            continue
        fresh_anchor_risk = _fresh_anchor_submission_risk(
            expression,
            components,
            draft=draft,
            legal_validation=legal_validation.to_dict() if legal_validation is not None else None,
        )
        if fresh_anchor_risk:
            rejected.append({
                **draft,
                "reject_reason": "fresh_anchor_submission_risk",
                "fresh_anchor_risk": fresh_anchor_risk,
            })
            continue

        signature = field_signature(expression)
        if signature in blocked_distribution_signatures:
            rejected.append({
                **draft,
                "reject_reason": "platform_distribution_signature_risk",
                "field_signature": signature,
                "historical_distribution_fail_count": distribution_signature_counts[signature],
            })
            continue
        platform_risk = platform_blocker_match(
            components,
            signature,
            platform_blockers,
            field_jaccard_cutoff=config.platform_blocker_field_jaccard_cutoff,
        )
        if platform_risk:
            rejected.append({
                **draft,
                "reject_reason": "platform_self_correlation_anchor_risk",
                "field_signature": signature,
                "platform_blocker_match": platform_risk,
            })
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
        if legal_registry is not None:
            policy_row["legal_input_validation"] = legal_validation.to_dict()
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
    family = str(row.get("source_family") or "")
    policy = row.get("forum_policy") if isinstance(row.get("forum_policy"), dict) else {}
    submitted_eval = policy.get("submitted_alpha_constraints") if isinstance(policy.get("submitted_alpha_constraints"), dict) else {}
    policy_reasons = {str(item) for item in submitted_eval.get("reasons") or []}
    platform_memory_penalty = 1 if family.startswith("research_platform_unsubmitted_") else 0
    returns_main_penalty = 1 if "returns_main_anchor" in policy_reasons else 0
    high_similarity_penalty = 1 if (_safe_float(row.get("nearest_similarity")) or 0.0) >= 0.62 else 0
    strategy = str(row.get("mutation_strategy") or "")
    fresh_family_penalty = 0 if strategy == "fresh_anchor_research_family" or family.startswith("research_fresh_anchor_") else 1
    fresh_anchor_bonus = -min(_fresh_anchor_field_count(str(row.get("expression") or "")), 6)
    return (
        returns_main_penalty,
        platform_memory_penalty,
        high_similarity_penalty,
        fresh_family_penalty,
        fresh_anchor_bonus,
        -(_safe_float(row.get("research_priority_score")) or 0.0),
        _safe_float(row.get("nearest_similarity")) or 0.0,
        family,
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


def _fresh_anchor_field_count(expression: str) -> int:
    fields = components_for(expression)["fields"] if expression else set()
    return len({field for field in fields if field not in FRESH_ANCHOR_EXCLUDE_FIELDS})


def _returns_usage_penalty(expression: str) -> int:
    fields = components_for(expression)["fields"] if expression else set()
    if "returns" not in fields:
        return 0
    compact = re.sub(r"\s+", "", str(expression or "").lower())
    explicit_weights = [
        abs(float(match.group(1)))
        for match in re.finditer(
            r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\*(?:rank\([^)]*returns|ts_rank\(returns|returns)",
            compact,
        )
    ]
    if explicit_weights and max(explicit_weights) <= 0.25:
        return 1
    return 2


def _platform_distribution_signature_counts(rows: list[dict]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        if infer_failure_kind(row) != "platform_distribution_fail":
            continue
        signature = str(row.get("field_signature") or "")
        expression = str(row.get("expression") or "")
        if not signature and expression:
            signature = field_signature(expression)
        if signature:
            counts[signature] += 1
    return counts


def _concentration_sparse_group_risk(
    expression: str,
    components: dict[str, set[str]],
    *,
    draft: dict,
    legal_validation: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Pre-screen candidates likely to fail WQ's concentrated-weight check.

    The rule is intentionally structural. Recent live probes showed that
    lowering truncation or adding another group transform did not repair
    expressions that combine multiple sparse legs with group operations.
    """

    fields = set(components.get("fields") or set())
    sparse_fields = sorted(field for field in fields if _is_sparse_concentration_field(field))
    if not sparse_fields:
        return None

    group_operator_count = _group_distribution_operator_count(expression, components)
    pcr_fields = sorted(field for field in sparse_fields if field.startswith("pcr_"))
    denominator_sparse_fields = sorted(
        field for field in sparse_fields if _field_used_as_denominator(expression, field)
    )
    specs_by_field = _field_specs_by_id(legal_validation)
    low_coverage_fields = sorted(
        field for field in fields
        if field not in GROUP_FIELDS
        and _field_coverage(specs_by_field.get(field)) is not None
        and (_field_coverage(specs_by_field.get(field)) or 1.0) < 0.82
    )
    known_coverages = [
        coverage for field in fields - GROUP_FIELDS
        for coverage in [_field_coverage(specs_by_field.get(field))]
        if coverage is not None
    ]
    estimated_effective_coverage = min(known_coverages) if known_coverages else None
    broad_dispersion_fields = sorted(
        field for field in fields
        if _is_broad_dispersion_field(field, specs_by_field.get(field))
    )
    price_volume_dispersion_fields = sorted(
        field for field in fields
        if _is_price_volume_dispersion_field(field)
    )

    reasons: list[str] = []
    if len(sparse_fields) > 1 and group_operator_count > 0:
        reasons.append("multiple_sparse_legs_with_group_ops")
    if denominator_sparse_fields and len(sparse_fields) > 1 and group_operator_count > 0:
        reasons.append("sparse_denominator_plus_other_sparse_group_leg")
    if len(sparse_fields) == 1 and group_operator_count > 0 and not broad_dispersion_fields:
        reasons.append("single_sparse_group_without_broad_dispersion_leg")
    if pcr_fields and not price_volume_dispersion_fields:
        reasons.append("pcr_sparse_leg_without_price_volume_dispersion")
    if (
        estimated_effective_coverage is not None
        and estimated_effective_coverage < 0.72
        and group_operator_count > 0
        and not broad_dispersion_fields
    ):
        reasons.append("low_estimated_coverage_group_signal")
    if not reasons:
        return None

    settings = draft.get("simulation_settings") if isinstance(draft.get("simulation_settings"), dict) else {}
    return {
        "reasons": sorted(set(reasons)),
        "sparse_fields": sparse_fields,
        "sparse_field_count": len(sparse_fields),
        "pcr_fields": pcr_fields,
        "denominator_sparse_fields": denominator_sparse_fields,
        "low_coverage_fields": low_coverage_fields,
        "estimated_effective_coverage": round(estimated_effective_coverage, 6)
        if estimated_effective_coverage is not None else None,
        "broad_dispersion_fields": broad_dispersion_fields,
        "price_volume_dispersion_fields": price_volume_dispersion_fields,
        "group_operator_count": group_operator_count,
        "requested_neutralization": str(settings.get("neutralization") or "SUBINDUSTRY"),
        "lesson": (
            "Avoid combining multiple low-coverage/sparse legs with group transforms. "
            "Keep at most one sparse main leg; PCR legs need explicit price-volume dispersion."
        ),
    }


def _fresh_anchor_submission_risk(
    expression: str,
    components: dict[str, set[str]],
    *,
    draft: dict,
    legal_validation: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    fields = set(components.get("fields") or set())
    family = str(draft.get("source_family") or "")
    strategy = str(draft.get("mutation_strategy") or "")
    is_fresh_or_standalone = (
        strategy == "fresh_anchor_research_family"
        or strategy == "standalone_low_overlap_research_family"
        or family.startswith("research_fresh_anchor_")
    )
    if not is_fresh_or_standalone:
        return None

    specs_by_field = _field_specs_by_id(legal_validation)
    data_fields = sorted(fields - GROUP_FIELDS - {"returns"})
    low_coverage_fields = sorted(
        field for field in data_fields
        if _field_coverage(specs_by_field.get(field)) is not None
        and (_field_coverage(specs_by_field.get(field)) or 1.0) < 0.82
    )
    event_fields = sorted(field for field in data_fields if field in EVENT_REACTION_FIELDS)
    broad_dispersion_fields = sorted(
        field for field in fields
        if _is_broad_dispersion_field(field, specs_by_field.get(field))
    )
    price_volume_dispersion_fields = sorted(
        field for field in fields
        if _is_price_volume_dispersion_field(field)
    )
    operators = set(components.get("operators") or set())
    has_temporal_smoothing = bool({"ts_mean", "ts_decay_linear", "ts_av_diff", "ts_zscore"} & operators)

    reasons: list[str] = []
    if low_coverage_fields and not broad_dispersion_fields and not price_volume_dispersion_fields:
        reasons.append("low_coverage_anchor_without_broad_dispersion_leg")
    if event_fields and not has_temporal_smoothing and not broad_dispersion_fields and not price_volume_dispersion_fields:
        reasons.append("event_reaction_without_smoothing_or_broad_overlay")
    if len(data_fields) == 1 and not price_volume_dispersion_fields:
        reasons.append("single_non_price_standalone_submission_risk")
    if not reasons:
        return None

    return {
        "reasons": sorted(set(reasons)),
        "data_fields": data_fields,
        "low_coverage_fields": low_coverage_fields,
        "event_fields": event_fields,
        "broad_dispersion_fields": broad_dispersion_fields,
        "price_volume_dispersion_fields": price_volume_dispersion_fields,
        "has_temporal_smoothing": has_temporal_smoothing,
        "lesson": (
            "Do not spend submit simulations on naked fresh-anchor legs. "
            "Blend low-coverage or event-driven anchors with smoothing and a broad price-volume/model dispersion overlay."
        ),
    }


def _is_sparse_concentration_field(field: str) -> bool:
    text = str(field or "")
    if text in SPARSE_CONCENTRATION_FIELDS:
        return True
    if any(text.startswith(prefix) for prefix in SPARSE_CONCENTRATION_PREFIXES):
        return True
    return "dividend" in text


def _group_distribution_operator_count(expression: str, components: dict[str, set[str]]) -> int:
    operators = set(components.get("operators") or set())
    count = sum(1 for operator in operators if operator in GROUP_DISTRIBUTION_OPERATORS)
    text = str(expression or "")
    return max(count, sum(text.count(f"{operator}(") for operator in GROUP_DISTRIBUTION_OPERATORS))


def _field_specs_by_id(legal_validation: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    specs = {}
    for spec in (legal_validation or {}).get("field_specs") or []:
        if not isinstance(spec, dict):
            continue
        field = str(spec.get("id") or "")
        if field:
            specs[field] = spec
    return specs


def _field_coverage(spec: dict[str, Any] | None) -> float | None:
    if not isinstance(spec, dict):
        return None
    return _safe_float(spec.get("coverage"))


def _field_used_as_denominator(expression: str, field: str) -> bool:
    compact = re.sub(r"\s+", "", str(expression or "").lower())
    escaped_field = re.escape(str(field or "").lower())
    return bool(re.search(rf"/(?:ts_backfill\()?{escaped_field}\b", compact))


def _is_broad_dispersion_field(field: str, spec: dict[str, Any] | None) -> bool:
    text = str(field or "")
    if text in GROUP_FIELDS or text == "returns" or _is_sparse_concentration_field(text):
        return False
    if text in BROAD_DISPERSION_FIELDS or re.fullmatch(r"adv\d+", text or ""):
        return True
    if not isinstance(spec, dict):
        return False
    coverage = _field_coverage(spec)
    if coverage is not None and coverage < 0.9:
        return False
    dataset = str(spec.get("dataset_id") or "")
    domain = str(spec.get("domain") or "")
    category = str(spec.get("category") or "")
    return dataset in BROAD_DISPERSION_DATASETS or domain in {"pv", "core", "model"} or category in {"pv", "model"}


def _is_price_volume_dispersion_field(field: str) -> bool:
    text = str(field or "")
    return text in PRICE_VOLUME_DISPERSION_FIELDS or bool(re.fullmatch(r"adv\d+", text or ""))


def platform_blocker_match(
    components: dict[str, set[str]],
    signature: str,
    blocker_rows: list[dict],
    *,
    field_jaccard_cutoff: float = 0.62,
) -> dict | None:
    candidate_fields = set(components.get("fields") or set())
    candidate_ops = set(components.get("operators") or set())
    candidate_distinct = _distinctive_blocker_fields(candidate_fields)
    for blocker in blocker_rows:
        blocker_signature = str(blocker.get("field_signature") or "")
        if blocker_signature and signature == blocker_signature:
            return _blocker_match_detail(blocker, reason="exact_field_signature", field_jaccard=1.0)
        blocker_fields = set(str(item) for item in blocker.get("fields") or [])
        if not blocker_fields:
            continue
        blocker_distinct = _distinctive_blocker_fields(blocker_fields)
        shared = candidate_distinct & blocker_distinct
        if len(shared) < 3:
            continue
        field_jaccard = _jaccard(candidate_distinct, blocker_distinct)
        if field_jaccard < field_jaccard_cutoff:
            continue
        blocker_ops = set(str(item) for item in blocker.get("operators") or [])
        op_jaccard = _jaccard(candidate_ops, blocker_ops)
        if blocker_ops and candidate_ops and op_jaccard < 0.25:
            continue
        return _blocker_match_detail(
            blocker,
            reason="field_operator_overlap",
            field_jaccard=field_jaccard,
            operator_jaccard=op_jaccard,
            shared_fields=sorted(shared),
        )
    return None


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


def _fresh_anchor_templates() -> list[dict]:
    """Fresh-anchor ideas that avoid using returns as the main alpha signal."""

    rows = [
        (
            "sentiment-revision-delta",
            "rank(0.45 * ts_mean(ts_delta(scl12_sentiment_fast_d1, 5), 5) + 0.35 * ts_rank(snt1_d1_netearningsrevision, 60) + 0.20 * rank(ts_corr(vwap, volume, 60)))",
            "research_fresh_anchor_sentiment_revision",
            "Sentiment and earnings-revision deltas are less tied to the current submitted return-reversal stack.",
        ),
        (
            "news-social-reaction",
            "rank(0.42 * ts_mean(ts_rank(news_open_gap, 20), 5) + 0.28 * ts_mean(ts_delta(scl12_buzz_fast_d1, 5), 5) + 0.20 * rank(ts_corr(vwap, volume, 60)) + 0.10 * rank(volume / adv20))",
            "research_fresh_anchor_news_social",
            "Blend event reaction and social attention without a return-rank anchor.",
        ),
        (
            "credit-risk-premium",
            "rank(0.35 * ts_rank(rp_css_credit, 40) + 0.30 * ts_rank(rp_ess_credit, 40) + 0.20 * rank(ts_corr(vwap, volume, 60)) + 0.15 * rank(volume / adv20))",
            "research_fresh_anchor_credit_sentiment",
            "Use credit-related risk-premium sentiment as an independent anchor family.",
        ),
        (
            "relationship-option-flow",
            "rank(0.45 * ts_rank(news_open_gap, 20) + 0.30 * rank(-1 * ts_rank(pcr_oi_10, 60)) + 0.25 * ts_rank(forward_sales_to_price, 80))",
            "research_fresh_anchor_news_option_flow",
            "Event reaction plus options pressure and forward sales value creates a non-return primary anchor.",
        ),
        (
            "model-roic-cfp",
            "rank(0.45 * ts_rank(actual_sales_value_quarterly / assets, 60) + 0.35 * ts_rank(earnings_momentum_composite_score, 50) + 0.20 * ts_rank(snt1_d1_netearningsrevision, 60))",
            "research_fresh_anchor_sales_revision_value",
            "Use discovered sales and revision fields instead of saturated cashflow/returns stacks.",
        ),
        (
            "option-skew-open-interest",
            "rank(0.45 * rank(ts_mean((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), 5)) + 0.35 * rank(-1 * ts_rank(pcr_oi_10, 60)) + 0.20 * rank(-1 * ts_rank(pcr_vol_10, 60)))",
            "research_fresh_anchor_option_positioning",
            "Discovered option volatility and put/call pressure avoid compatibility-only option fields.",
        ),
        (
            "short-cost-credit",
            "rank(0.34 * ts_rank(rp_css_credit_ratings, 40) + 0.26 * ts_rank(rp_ess_credit_ratings, 40) + 0.20 * ts_rank(rp_css_credit, 40) + 0.20 * rank(ts_corr(vwap, volume, 60)))",
            "research_fresh_anchor_credit_rating",
            "Credit-rating and credit sentiment pressure form a fresh risk-pressure anchor.",
        ),
    ]
    return [
        _draft(
            expression,
            tag=f"fresh-{tag}",
            family=family,
            strategy="fresh_anchor_research_family",
            rationale=rationale,
        )
        for tag, expression, family, rationale in rows
    ]


def _exploration_templates(memory: list[dict]) -> list[dict]:
    return [
        _draft(
            "rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 10))",
            tag="explore-ivdiff90-mean10",
            family="research_fresh_anchor_options_positioning",
            strategy="low_overlap_field_family",
            rationale="Explore options skew as a low-overlap field family.",
        ),
        _draft(
            "rank(group_neutralize((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), industry))",
            tag="explore-ivratio120-industry",
            family="research_fresh_anchor_options_flow",
            strategy="low_overlap_field_family",
            rationale="Historical platform memory showed standalone normalized 120-day option skew can be strong.",
        ),
        _draft(
            "rank(0.55 * rank(group_neutralize((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), industry)) + 0.45 * rank(-1 * ts_rank(pcr_oi_10, 60)))",
            tag="explore-ivratio120-industry-openinterest",
            family="research_fresh_anchor_options_positioning",
            strategy="fresh_anchor_research_family",
            rationale="Blend normalized option skew with discovered put/call open-interest pressure instead of return reversal.",
        ),
        _draft(
            "rank(0.55 * rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5)) + 0.45 * rank(-1 * ts_rank(pcr_oi_10, 60)))",
            tag="explore-ivdiff90-pcr-oi",
            family="research_fresh_anchor_options_flow",
            strategy="fresh_anchor_research_family",
            rationale="Blend options skew with put/call open-interest pressure instead of returns.",
        ),
        _draft(
            "rank(0.32 * ts_rank(forward_cash_flow_to_price, 80) + 0.26 * ts_rank(forward_book_value_to_price, 80) + 0.22 * rank(-1 * cashflow_efficiency_rank_derivative) + 0.20 * rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5)))",
            tag="explore-forward-cashflow-eff-iv90",
            family="research_forward_cashflow_decorrelation",
            strategy="forward_value_iv_overlay",
            rationale="Combine forward valuation, model derivative, and options skew to avoid single-family crowding.",
        ),
        _draft(
            "rank(0.38 * ts_rank(anl4_adjusted_netincome_ft / cap, 50) + 0.34 * ts_rank(cashflow_op / cap, 80) + 0.18 * rank(-1 * cashflow_efficiency_rank_derivative) + 0.10 * rank(-1 * ts_rank(returns, 60)))",
            tag="explore-netincome-cfop-eff",
            family="research_analyst_cashflow_blend",
            strategy="analyst_cashflow_blend",
            rationale="Cross analyst net income with statement cashflow and model derivative.",
        ),
        _draft(
            "rank(0.42 * ts_rank(equity / cap, 60) + 0.24 * ts_rank(forward_sales_to_price, 60) + 0.14 * ts_rank(change_in_eps_surprise, 60) + 0.14 * ts_rank(snt1_d1_netearningsrevision, 60) + 0.06 * rank(-1 * ts_rank(returns, 60)))",
            tag="explore-equity-sales-eps-revision",
            family="research_forward_revision_value",
            strategy="analyst_revision_value_blend",
            rationale="Use analyst revision and forward sales with value exposure seen in platform memory.",
        ),
        _draft(
            "rank(0.50 * ts_rank(actual_sales_value_quarterly / enterprise_value, 60) + 0.30 * ts_rank(earnings_momentum_composite_score, 50) + 0.15 * rank(ts_corr(vwap, volume, 40)) + 0.05 * rank(-1 * ts_rank(returns, 60)))",
            tag="explore-sales-ev-earnmom-vwap",
            family="research_sales_momentum_microstructure",
            strategy="fundamental_microstructure_blend",
            rationale="Combine actual sales, earnings momentum, and VWAP-volume microstructure.",
        ),
        _draft(
            "rank(0.42 * ts_rank(ebit / enterprise_value, 80) + 0.32 * ts_rank(forward_book_value_to_price, 80) + 0.26 * rank(pcr_oi_60))",
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
        ("earnmom70", "ts_rank(earnings_momentum_composite_score, 70)", "analyst_momentum"),
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
        ("pcr-vol10", "rank(-1 * ts_rank(pcr_vol_10, 60))", "options_flow"),
        ("pcr-oi10", "rank(-1 * ts_rank(pcr_oi_10, 60))", "options_flow"),
    ]
    return_legs = [
        ("ret20ctrl", "rank(-1 * ts_rank(returns, 20))", 0.05),
        ("ret60ctrl", "rank(-1 * ts_rank(returns, 60))", 0.10),
    ]

    drafts: list[dict] = []
    for base_name, base_expr, base_family in base_legs:
        for overlay_name, overlay_expr, overlay_family in overlay_legs:
            for return_name, return_expr, return_weight in return_legs:
                base_weight = 0.58 if return_weight <= 0.05 else 0.55
                overlay_weight = round(1.0 - base_weight - return_weight, 2)
                drafts.append(_draft(
                    f"rank({base_weight:.2f} * {base_expr} + {overlay_weight:.2f} * {overlay_expr} + {return_weight:.2f} * {return_expr})",
                    tag=f"explore-{base_name}-{overlay_name}-{return_name}",
                    family=f"research_{base_family}_{overlay_family}",
                    strategy="systematic_local_factor_grid",
                    rationale="Local deterministic grid combining value, orthogonal overlay, and a small return-risk control.",
                ))

    option_terms = [
        (
            "news-open-gap-reversal",
            "rank(0.42 * ts_mean(ts_rank(news_open_gap, 20), 5) + 0.28 * rank(-1 * ts_mean(ts_rank(news_max_dn_ret, 20), 5)) + 0.20 * rank(ts_corr(vwap, volume, 60)) + 0.10 * rank(volume / adv20))",
            "research_news_intraday_reaction",
        ),
        (
            "news-mins10-pressure",
            "rank(0.45 * ts_mean(ts_rank(news_mins_10_chg, 30), 5) - 0.25 * ts_mean(ts_rank(news_mov_vol, 30), 5) + 0.20 * rank(ts_corr(vwap, volume, 60)) + 0.10 * rank(volume / adv20))",
            "research_news_intraday_reaction",
        ),
        (
            "social-buzz-sentiment-delta",
            "rank(0.40 * ts_mean(ts_delta(scl12_buzz_fast_d1, 5), 5) + 0.30 * ts_mean(ts_delta(scl12_sentiment_fast_d1, 5), 5) + 0.20 * rank(ts_corr(vwap, volume, 60)) + 0.10 * rank(volume / adv20))",
            "research_socialmedia_delta",
        ),
        (
            "short-sale-cost-reversal",
            "rank(0.40 * ts_rank(rp_css_credit_ratings, 40) + 0.30 * ts_rank(rp_ess_credit_ratings, 40) + 0.20 * rank(ts_corr(vwap, volume, 60)) + 0.10 * rank(volume / adv20))",
            "research_credit_rating_pressure",
        ),
        (
            "supplier-customer-relationship",
            "rank(0.42 * ts_mean(ts_rank(news_open_gap, 20), 5) + 0.28 * ts_mean(ts_delta(scl12_buzz_fast_d1, 5), 5) + 0.20 * rank(ts_corr(vwap, volume, 60)) + 0.10 * rank(volume / adv20))",
            "research_news_social_reaction",
        ),
        (
            "customer-momentum-open-interest",
            "rank(0.50 * ts_rank(forward_sales_to_price, 80) + 0.25 * rank(-1 * ts_rank(pcr_vol_10, 60)) + 0.15 * rank(ts_corr(vwap, volume, 60)) + 0.10 * rank(volume / adv20))",
            "research_forward_sales_options_flow",
        ),
        (
            "credit-sentiment-spread",
            "rank(0.35 * ts_rank(rp_css_credit, 40) + 0.30 * ts_rank(rp_ess_credit, 40) + 0.20 * rank(ts_corr(vwap, volume, 60)) + 0.15 * rank(volume / adv20))",
            "research_news_credit_sentiment",
        ),
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
            "rank(0.45 * rank(ts_mean((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120), 5)) + 0.35 * rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5)) + 0.20 * rank(-1 * ts_rank(pcr_oi_10, 60)))",
            "research_options_term_structure",
        ),
        (
            "option-flow-reversal",
            "rank(0.55 * rank(-1 * ts_rank(pcr_oi_10, 60)) + 0.45 * rank(-1 * ts_rank(pcr_vol_10, 60)))",
            "research_options_flow",
        ),
        (
            "sentiment-revision-reversal",
            "rank(0.50 * ts_rank(scl12_sentiment_fast_d1, 20) + 0.50 * ts_rank(snt1_d1_netearningsrevision, 60))",
            "research_sentiment_revision",
        ),
        (
            "relationship-pcr-value",
            "rank(0.45 * ts_rank(news_open_gap, 20) + 0.35 * rank(-1 * ts_rank(pcr_oi_10, 60)) + 0.20 * ts_rank(forward_cash_flow_to_price, 80))",
            "research_news_options_flow",
        ),
        (
            "sentiment-delta5",
            "rank(0.65 * ts_mean(ts_delta(scl12_sentiment_fast_d1, 5), 5) + 0.35 * rank(ts_corr(vwap, volume, 60)))",
            "research_sentiment_standalone",
        ),
        (
            "revision-delta10",
            "rank(ts_delta(snt1_d1_netearningsrevision, 10))",
            "research_sentiment_revision",
        ),
        (
            "options-oi-reversal",
            "rank(0.55 * rank(-1 * ts_rank(pcr_oi_10, 60)) + 0.45 * rank(-1 * ts_rank(pcr_vol_10, 60)))",
            "research_options_flow",
        ),
        (
            "relationship-supplier-return",
            "rank(0.55 * ts_mean(ts_rank(news_max_dn_ret, 20), 5) + 0.30 * rank(ts_corr(vwap, volume, 60)) + 0.15 * rank(volume / adv20))",
            "research_news_reaction_standalone",
        ),
        (
            "relationship-customer-momentum",
            "rank(0.60 * ts_rank(forward_sales_to_price, 80) + 0.25 * rank(ts_corr(vwap, volume, 60)) + 0.15 * rank(volume / adv20))",
            "research_forward_sales_standalone",
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
            "mdf-roic-value",
            "rank(0.50 * ts_rank(actual_sales_value_quarterly / assets, 60) + 0.50 * ts_rank(earnings_momentum_composite_score, 50))",
            "research_sales_revision_standalone",
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


def _platform_blocker_record(
    row: dict | None,
    *,
    expression: str,
    blocker_type: str,
    failure_kind: str,
    anchor_ids: list[str],
    platform_correlation: float | None,
    similarity_cutoff: float,
    source_failed_alpha_id: Any | None = None,
    source_failed_expression: str = "",
) -> dict:
    components = components_for(expression)
    alpha_id = (row or {}).get("alpha_id") or (anchor_ids[0] if anchor_ids else None)
    return {
        "memory_kind": "platform_self_correlation_blocker",
        "severity": "block_exact_penalize_family",
        "failure_kind": failure_kind,
        "blocker_type": blocker_type,
        "lesson": "Platform self-correlation linked this structure to an ACTIVE anchor; require a material field-family change before spending simulation budget.",
        "alpha_id": alpha_id,
        "anchor_alpha_ids": [str(item) for item in anchor_ids if item],
        "source_failed_alpha_id": source_failed_alpha_id,
        "source_failed_expression": source_failed_expression,
        "tag": (row or {}).get("tag"),
        "expression": expression,
        "expression_normalized": normalize_expression(expression),
        "fields": sorted(components["fields"]),
        "operators": sorted(components["operators"]),
        "field_signature": field_signature(expression),
        "source_family": _row_family(row or {}),
        "sharpe": (row or {}).get("sharpe"),
        "fitness": (row or {}).get("fitness"),
        "turnover": (row or {}).get("turnover"),
        "sc_value": platform_correlation,
        "platform_correlation": platform_correlation,
        "similarity_cutoff": similarity_cutoff,
    }


def _failure_lesson(row: dict, failure_kind: str) -> str:
    if failure_kind == "self_correlation_high":
        return "Do not rerun the same structure; change field family or add a small orthogonal overlay before WQ simulation."
    if failure_kind == "high_similarity":
        return "Avoid formulaic near-duplicates; require field-family or operator-family change."
    if failure_kind == "platform_distribution_fail":
        return (
            "Do not repair concentrated-weight failures by only lowering truncation, adding decay, or stacking group ranks. "
            "Avoid sparse field ratios plus group transforms; keep at most one sparse main leg and add a broad "
            "price-volume or model-field dispersion leg before spending WQ simulation budget."
        )
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

    if strategy == "fresh_anchor_research_family" or family.startswith("research_fresh_anchor_"):
        family_rank = 0
    elif strategy.startswith("weak_active_") or family.startswith("research_weak_active_") or strategy == "targeted_near_pass_decorrelation" or strategy == "standalone_low_overlap_research_family" or strategy in {"systematic_local_factor_grid", "low_overlap_field_family", "option_term_reversal_overlay"}:
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
    returns_penalty = _returns_usage_penalty(expression)
    fresh_anchor_bonus = -min(_fresh_anchor_field_count(expression), 6)
    field_count_bonus = -min(len(fields), 8)
    metric_bonus = -(_safe_float(row.get("fitness")) or 0.0)
    return (
        family_rank,
        returns_penalty,
        fresh_anchor_bonus,
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
    if strategy == "fresh_anchor_research_family" or family.startswith("research_fresh_anchor_"):
        return "fresh_anchor"
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
        "fresh_anchor": [],
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
        "fresh_anchor",
        "targeted_near_pass",
        "standalone",
        "exploration",
        "weak_active_repair",
        "decorrelation_repair",
        "elite",
        "distribution_repair",
        "other",
        "platform_memory",
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


def _active_rows_by_alpha_id(rows: list[dict]) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for row in rows:
        alpha_id = str(row.get("alpha_id") or "")
        if alpha_id:
            by_id.setdefault(alpha_id, row)
        for item in row.get("alpha_ids") or []:
            key = str(item or "")
            if key:
                by_id.setdefault(key, row)
    return by_id


def _self_correlated_records(row: dict) -> list[dict]:
    records: list[dict] = []
    seen: set[tuple[str, float | None]] = set()
    for payload in _self_correlation_payloads(row):
        schema = payload.get("schema") if isinstance(payload.get("schema"), dict) else {}
        names = [
            str(item.get("name") or "")
            for item in schema.get("properties") or []
            if isinstance(item, dict)
        ]
        if not names:
            names = ["id", "name", "instrumentType", "region", "universe", "correlation", "sharpe", "returns", "turnover", "fitness", "margin"]
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            continue
        for raw in raw_records:
            if isinstance(raw, dict):
                item = dict(raw)
            elif isinstance(raw, list):
                item = {name: raw[index] for index, name in enumerate(names) if name and index < len(raw)}
            else:
                continue
            alpha_id = str(item.get("id") or "")
            correlation = _safe_float(item.get("correlation"))
            key = (alpha_id, correlation)
            if key in seen:
                continue
            seen.add(key)
            records.append(item)
    return records


def _self_correlation_payloads(row: dict) -> list[dict]:
    payloads: list[dict] = []
    containers: list[Any] = [
        row,
        row.get("raw_check"),
        row.get("live_precheck"),
    ]
    live = row.get("live_precheck") if isinstance(row.get("live_precheck"), dict) else {}
    containers.append(live.get("raw_check"))
    for container in containers:
        if not isinstance(container, dict):
            continue
        direct = container.get("selfCorrelated")
        if isinstance(direct, dict):
            payloads.append(direct)
        is_payload = container.get("is")
        if isinstance(is_payload, dict) and isinstance(is_payload.get("selfCorrelated"), dict):
            payloads.append(is_payload["selfCorrelated"])
    return payloads


def _row_self_correlation_value(row: dict) -> float | None:
    direct = _safe_float(row.get("sc_value"))
    if direct is not None:
        return direct
    review = row.get("review_checks") if isinstance(row.get("review_checks"), dict) else {}
    sc = review.get("self_correlation") if isinstance(review.get("self_correlation"), dict) else {}
    value = _safe_float(sc.get("value"))
    if value is not None:
        return value
    live = row.get("live_precheck") if isinstance(row.get("live_precheck"), dict) else {}
    live_review = live.get("review_checks") if isinstance(live.get("review_checks"), dict) else {}
    live_sc = live_review.get("self_correlation") if isinstance(live_review.get("self_correlation"), dict) else {}
    value = _safe_float(live_sc.get("value") or live.get("sc_value"))
    if value is not None:
        return value
    for check in _extract_platform_check_items(row):
        if str(check.get("name") or "").upper() != "SELF_CORRELATION":
            continue
        value = _safe_float(check.get("value"))
        if value is not None:
            return value
    return None


def _extract_platform_check_items(row: dict) -> list[dict]:
    checks: list[dict] = []
    containers: list[Any] = [
        row,
        row.get("raw_check"),
        row.get("live_precheck"),
    ]
    live = row.get("live_precheck") if isinstance(row.get("live_precheck"), dict) else {}
    containers.append(live.get("raw_check"))
    for container in containers:
        if not isinstance(container, dict):
            continue
        for candidate in (container, container.get("is") if isinstance(container.get("is"), dict) else {}):
            value = candidate.get("checks") if isinstance(candidate, dict) else None
            if isinstance(value, list):
                checks.extend(item for item in value if isinstance(item, dict))
    return checks


def _distinctive_blocker_fields(fields: set[str]) -> set[str]:
    distinct = {field for field in fields if field not in PLATFORM_BLOCKER_COMMON_FIELDS}
    return distinct if len(distinct) >= 3 else set(fields)


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _blocker_match_detail(
    blocker: dict,
    *,
    reason: str,
    field_jaccard: float,
    operator_jaccard: float | None = None,
    shared_fields: list[str] | None = None,
) -> dict:
    return {
        "reason": reason,
        "alpha_id": blocker.get("alpha_id"),
        "anchor_alpha_ids": blocker.get("anchor_alpha_ids") or [],
        "blocker_type": blocker.get("blocker_type"),
        "failure_kind": blocker.get("failure_kind"),
        "platform_correlation": blocker.get("platform_correlation") or blocker.get("sc_value"),
        "field_signature": blocker.get("field_signature"),
        "field_jaccard": round(field_jaccard, 6),
        "operator_jaccard": round(operator_jaccard, 6) if operator_jaccard is not None else None,
        "shared_fields": shared_fields or [],
    }


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
        text = path.read_text(encoding="utf-8-sig").strip()
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
        text = path.read_text(encoding="utf-8-sig").strip()
        if not text:
            continue
        if path.suffix.lower() == ".json":
            payload = json.loads(text)
            if isinstance(payload, dict):
                rows.extend(_rows_from_payload(payload.get("active") or payload.get("rows") or payload))
            else:
                rows.extend(_rows_from_payload(payload))
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rows.extend(_rows_from_payload(json.loads(line)))
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
