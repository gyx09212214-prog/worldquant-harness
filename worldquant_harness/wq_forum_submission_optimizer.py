"""Forum-informed submission planning for WQ factor mining.

This module turns forum idea memory and the factor map into a small policy file
that can guide candidate generation and presubmit filtering. It is local-only:
it never calls WQ BRAIN and never submits alphas.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifact_io import read_jsonl as artifact_read_jsonl
from .artifact_io import write_json as artifact_write_json
from .artifact_io import write_jsonl as artifact_write_jsonl
from .artifact_io import write_text as artifact_write_text
from .record_utils import nested as _nested
from .record_utils import safe_float as _safe_float
from .record_utils import safe_int as _safe_int
from .report_utils import markdown_cell as _md
from .wq_evolutionary_generator import classify_domain
from .wq_expression_utils import expression_components

THEME_DOMAIN_HINTS = {
    "fundamental_value_quality": ("fundamental_quality",),
    "sentiment_news_revision": ("sentiment_news", "analyst_revision"),
    "field_update_event": ("analyst_revision", "fundamental_quality", "sentiment_news"),
    "missingness_coverage": ("model_derivative", "unknown"),
    "internal_group_compare": ("fundamental_quality", "analyst_revision", "model_derivative"),
    "regime_trade_when": ("risk_defensive", "liquidity_microstructure"),
    "robustness_turnover": ("risk_defensive", "liquidity_microstructure"),
    "correlation_similarity": (),
}
RULE_ONLY_THEMES = {"correlation_similarity", "robustness_turnover"}
DIRECT_FORUM_MARKERS = ("forum_direct", "forum-direct", "community_seed", "worldquant_community")
PRICE_LIQUIDITY_FIELDS = {"open", "high", "low", "close", "returns", "volume", "vwap", "adv20", "adv60", "adv120"}
NEUTRALIZATION_FIELDS = {"industry", "subindustry", "sector"}
RETURN_ANCHOR_FIELD = "returns"
COMMUNITY_SKILL_HARD_BLOCK_FLAGS = {"private_code", "unknown_or_unsupported"}
COMMUNITY_SKILL_TEMPLATE_FLAGS = {"template_clone_risk", "possible_complete_alpha"}
COMMUNITY_SKILL_PENALIZE_FLAGS = {
    "field_family_crowding",
    "metric_near_pass",
    "operator_availability_risk",
    "platform_limit",
    "stale_precheck_risk",
    "unit_check",
}


@dataclass(frozen=True)
class ForumSubmissionOptimizerConfig:
    factor_map_dir: Path
    forum_memory_dirs: tuple[Path, ...] = field(default_factory=tuple)
    output_dir: Path | None = None
    obsidian_output: Path | None = None
    submitted_alpha_map_dir: Path | None = None
    community_skill_memory_file: Path | None = None
    region: str = "USA"
    universe: str = "TOP3000"
    account: str = "primary"
    max_directions: int = 30
    strict_similarity_cutoff: float = 0.62
    default_similarity_cutoff: float = 0.70
    title: str = "worldquant-harness 论坛提交优化"


def build_forum_submission_plan(config: ForumSubmissionOptimizerConfig) -> dict[str, Any]:
    factor_map = load_factor_map(config.factor_map_dir)
    forum_memory = load_forum_memory(
        config.forum_memory_dirs,
        extra_skill_files=(config.community_skill_memory_file,) if config.community_skill_memory_file else (),
    )
    submitted_alpha_map = load_submitted_alpha_map(config.submitted_alpha_map_dir)
    directions = build_direction_scores(factor_map=factor_map, forum_memory=forum_memory, config=config)
    policy = build_submission_policy(
        directions,
        config=config,
        submitted_alpha_map=submitted_alpha_map,
        community_skills=forum_memory.get("skills") or [],
    )
    budget = build_candidate_budget(directions, max_directions=config.max_directions)
    markdown = render_forum_submission_playbook(
        directions=directions,
        policy=policy,
        budget=budget,
        config=config,
        factor_map=factor_map,
        forum_memory=forum_memory,
        submitted_alpha_map=submitted_alpha_map,
    )
    plan = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "factor_map_dir": str(config.factor_map_dir),
            "forum_memory_dirs": [str(path) for path in config.forum_memory_dirs],
            "submitted_alpha_map_dir": str(config.submitted_alpha_map_dir) if config.submitted_alpha_map_dir else "",
            "community_skill_memory_file": str(config.community_skill_memory_file) if config.community_skill_memory_file else "",
            "region": config.region,
            "universe": config.universe,
            "account": config.account,
            "strict_similarity_cutoff": config.strict_similarity_cutoff,
            "default_similarity_cutoff": config.default_similarity_cutoff,
        },
        "summary": {
            "directions": len(directions),
            "clusters": len(forum_memory["clusters"]),
            "recipes": len(forum_memory["recipes"]),
            "rules": len(forum_memory["rules"]),
            "community_skills": len(forum_memory["skills"]),
            "factor_nodes": len(factor_map["nodes"]),
            "factor_domains": len(factor_map["domain_summary"]),
            "submitted_active_alphas": _nested(submitted_alpha_map, "summary", "active_or_submitted_count") or 0,
        },
        "directions": directions,
        "submission_policy": policy,
        "candidate_budget": budget,
        "markdown": markdown,
    }
    if config.output_dir or config.obsidian_output:
        write_forum_submission_artifacts(plan, output_dir=config.output_dir, obsidian_output=config.obsidian_output)
    return plan


def load_factor_map(factor_map_dir: Path) -> dict[str, Any]:
    return {
        "nodes": artifact_read_jsonl(factor_map_dir / "nodes.jsonl"),
        "edges": artifact_read_jsonl(factor_map_dir / "edges.jsonl"),
        "domain_summary": _read_csv(factor_map_dir / "domain_summary.csv"),
        "field_summary": _read_csv(factor_map_dir / "field_summary.csv"),
    }


def load_forum_memory(
    memory_dirs: tuple[Path, ...],
    *,
    extra_skill_files: tuple[Path | None, ...] = (),
) -> dict[str, Any]:
    clusters: list[dict[str, Any]] = []
    recipes: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    combinations: list[dict[str, Any]] = []
    skills: list[dict[str, Any]] = []
    for directory in memory_dirs:
        clusters.extend(artifact_read_jsonl(_first_existing(directory, "forum_idea_clusters_strict.jsonl", "forum_idea_clusters.jsonl")))
        recipes.extend(artifact_read_jsonl(directory / "forum_candidate_recipes.jsonl"))
        rules.extend(artifact_read_jsonl(directory / "forum_pattern_rules.jsonl"))
        combinations.extend(artifact_read_jsonl(directory / "forum_idea_theme_combinations.jsonl"))
        skills.extend(artifact_read_jsonl(_first_existing(directory, "community_skill_memory.jsonl", "skill_memory.jsonl")))
        skills.extend(artifact_read_jsonl(directory / "skill_memory" / "community_skill_memory.jsonl"))
    for path in extra_skill_files:
        if path:
            skills.extend(artifact_read_jsonl(path))
    return {
        "clusters": _dedupe_by_keys(clusters, ("theme_id", "title", "label")),
        "recipes": _dedupe_by_keys(recipes, ("recipe_id", "template")),
        "rules": _dedupe_by_keys(rules, ("rule_id", "logic")),
        "combinations": combinations,
        "skills": _dedupe_by_keys(skills, ("skill_id", "action")),
    }


def load_submitted_alpha_map(map_dir: Path | None) -> dict[str, Any] | None:
    if not map_dir or not Path(map_dir).is_dir():
        return None
    directory = Path(map_dir)
    summary_path = directory / "submitted_alpha_map_summary.json"
    summary_payload = json.loads(summary_path.read_text(encoding="utf-8-sig")) if summary_path.is_file() else {}
    return {
        "summary": summary_payload.get("summary") if isinstance(summary_payload.get("summary"), dict) else summary_payload,
        "domain_summary": _read_csv(directory / "submitted_domain_summary.csv"),
        "field_summary": _read_csv(directory / "submitted_field_summary.csv"),
        "operator_summary": _read_csv(directory / "submitted_operator_summary.csv"),
        "similarity_pairs": _read_csv(directory / "submitted_similarity_pairs.csv"),
        "alpha_details": artifact_read_jsonl(directory / "submitted_alpha_details.jsonl"),
    }


def build_direction_scores(
    *,
    factor_map: dict[str, Any],
    forum_memory: dict[str, Any],
    config: ForumSubmissionOptimizerConfig,
) -> list[dict[str, Any]]:
    domain_by_name = {str(row.get("domain") or ""): row for row in factor_map.get("domain_summary", [])}
    theme_combination_counts = _theme_combination_counts(forum_memory.get("combinations", []))
    recipe_by_theme: dict[str, list[dict[str, Any]]] = {}
    for recipe in forum_memory.get("recipes", []):
        recipe_by_theme.setdefault(str(recipe.get("source_theme") or ""), []).append(recipe)

    directions: list[dict[str, Any]] = []
    for cluster in forum_memory.get("clusters", []):
        theme_id = str(cluster.get("theme_id") or "")
        domains = _domains_for_theme_or_fields(theme_id, _cluster_fields(cluster))
        matched_domain_rows = [domain_by_name[domain] for domain in domains if domain in domain_by_name]
        member_count = _safe_int(cluster.get("member_count") or _nested(cluster, "stats", "count")) or 0
        non_course_count = _safe_int(cluster.get("non_course_count") or _nested(cluster, "stats", "non_course")) or 0
        course_noise_count = _safe_int(cluster.get("course_noise_count") or _nested(cluster, "stats", "course_noise")) or 0
        related_failures = sum(_safe_int(row.get("self_corr_fail_count")) or 0 for row in matched_domain_rows)
        related_failures += sum(_safe_int(row.get("high_similarity_fail_count")) or 0 for row in matched_domain_rows)
        related_active = sum(_safe_int(row.get("active_or_submitted_count")) or 0 for row in matched_domain_rows)
        map_crowding = _avg([_safe_float(row.get("crowded_score")) for row in matched_domain_rows])
        map_opportunity = _avg([_safe_float(row.get("opportunity_score")) for row in matched_domain_rows])
        avg_similarity = _avg([_safe_float(row.get("avg_intra_similarity")) for row in matched_domain_rows])
        rule_only = theme_id in RULE_ONLY_THEMES
        score = _direction_score(
            member_count=member_count,
            non_course_count=non_course_count,
            course_noise_count=course_noise_count,
            related_failures=related_failures,
            related_active=related_active,
            map_crowding=map_crowding,
            map_opportunity=map_opportunity,
            avg_similarity=avg_similarity,
            rule_only=rule_only,
        )
        action = _direction_action(theme_id, score=score, crowding=map_crowding, failures=related_failures)
        directions.append({
            "direction_id": f"theme:{theme_id}",
            "kind": "theme",
            "theme_id": theme_id,
            "recipe_id": None,
            "title": cluster.get("title") or cluster.get("label") or theme_id,
            "logic": cluster.get("logic"),
            "candidate_policy": cluster.get("candidate_policy"),
            "domains": domains,
            "top_fields": _top_pairs(cluster.get("top_fields"), limit=10),
            "top_operators": _top_pairs(cluster.get("top_operators"), limit=8),
            "forum_evidence": {
                "member_count": member_count,
                "non_course_count": non_course_count,
                "course_noise_count": course_noise_count,
                "course_noise_ratio": round(course_noise_count / max(1, member_count), 4),
                "combination_count": theme_combination_counts.get(theme_id, 0),
            },
            "map_stats": {
                "crowding": round(map_crowding, 4),
                "opportunity": round(map_opportunity, 4),
                "avg_similarity": round(avg_similarity, 4),
                "related_failures": related_failures,
                "related_active_count": related_active,
            },
            "recipes": [recipe.get("recipe_id") for recipe in recipe_by_theme.get(theme_id, []) if recipe.get("recipe_id")],
            "action": action,
            "research_priority_score": score,
            "reasons": _direction_reasons(theme_id, action, map_crowding, map_opportunity, related_failures, rule_only),
        })

    for recipe in forum_memory.get("recipes", []):
        theme_id = str(recipe.get("source_theme") or "")
        fields = [str(value) for value in recipe.get("fields") or []]
        domains = _domains_for_theme_or_fields(theme_id, fields)
        matched_domain_rows = [domain_by_name[domain] for domain in domains if domain in domain_by_name]
        map_crowding = _avg([_safe_float(row.get("crowded_score")) for row in matched_domain_rows])
        related_failures = sum(_safe_int(row.get("self_corr_fail_count")) or 0 for row in matched_domain_rows)
        related_failures += sum(_safe_int(row.get("high_similarity_fail_count")) or 0 for row in matched_domain_rows)
        evidence = _safe_int(recipe.get("evidence_records")) or 0
        score = round(
            35.0
            + min(evidence, 50) * 0.30
            - map_crowding * 0.15
            - min(related_failures, 80) * 0.10
            + (8.0 if theme_id not in RULE_ONLY_THEMES else -20.0),
            4,
        )
        action = _recipe_action(theme_id, score=score, recipe=recipe)
        directions.append({
            "direction_id": f"recipe:{recipe.get('recipe_id')}",
            "kind": "recipe",
            "theme_id": theme_id,
            "recipe_id": recipe.get("recipe_id"),
            "title": recipe.get("recipe_id"),
            "logic": recipe.get("template"),
            "candidate_policy": "; ".join(str(item) for item in recipe.get("stop_if") or []),
            "domains": domains,
            "top_fields": [{"value": field, "count": 1} for field in fields],
            "top_operators": _operators_from_template(str(recipe.get("template") or "")),
            "forum_evidence": {
                "member_count": evidence,
                "non_course_count": _safe_int(recipe.get("non_course_sources")) or 0,
                "course_noise_count": 0,
                "course_noise_ratio": 0.0,
                "combination_count": theme_combination_counts.get(theme_id, 0),
            },
            "map_stats": {
                "crowding": round(map_crowding, 4),
                "opportunity": _avg([_safe_float(row.get("opportunity_score")) for row in matched_domain_rows]),
                "avg_similarity": _avg([_safe_float(row.get("avg_intra_similarity")) for row in matched_domain_rows]),
                "related_failures": related_failures,
                "related_active_count": sum(_safe_int(row.get("active_or_submitted_count")) or 0 for row in matched_domain_rows),
            },
            "recipes": [recipe.get("recipe_id")],
            "max_initial_sims": _safe_int(recipe.get("max_initial_sims")),
            "neutralization": recipe.get("neutralization"),
            "action": action,
            "research_priority_score": score,
            "reasons": _direction_reasons(theme_id, action, map_crowding, 0.0, related_failures, theme_id in RULE_ONLY_THEMES),
        })

    directions.sort(key=lambda row: (-_safe_float(row.get("research_priority_score"), 0.0), row.get("direction_id") or ""))
    return _dedupe_directions_by_id(directions)


def build_submission_policy(
    directions: list[dict[str, Any]],
    *,
    config: ForumSubmissionOptimizerConfig,
    submitted_alpha_map: dict[str, Any] | None = None,
    community_skills: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    theme_policies = {
        row["theme_id"]: _policy_entry(row)
        for row in directions
        if row.get("kind") == "theme" and row.get("theme_id")
    }
    recipe_policies = {
        row["recipe_id"]: _policy_entry(row)
        for row in directions
        if row.get("kind") == "recipe" and row.get("recipe_id")
    }
    crowded_domains = _top_domains(directions, metric="crowding", minimum=70.0)
    underexplored_domains = _underexplored_domains(directions)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "gates": {
            "strict_similarity_cutoff": config.strict_similarity_cutoff,
            "default_similarity_cutoff": config.default_similarity_cutoff,
            "direct_forum_template_action": "block",
            "crowded_forum_direct_action": "penalize",
            "low_priority_reject_below": 15.0,
        },
        "crowded_domains": crowded_domains,
        "underexplored_domains": underexplored_domains,
        "submitted_alpha_map": build_submitted_alpha_policy(submitted_alpha_map),
        "community_skill_policy": build_community_skill_policy(community_skills or []),
        "theme_policies": theme_policies,
        "recipe_policies": recipe_policies,
        "rules": {
            "direct_forum_formula_graylist": {
                "action": "block_unless_orthogonal_overlay",
                "reason": "Direct forum snippets are similarity-prone; require field-family or operator-family change.",
            },
            "correlation_similarity": {
                "action": "gate_only",
                "reason": "Use forum correlation discussion as a constraint, not a standalone alpha source.",
            },
            "robustness_turnover": {
                "action": "budget_and_filter_only",
                "reason": "Use turnover/robustness discussion to allocate simulations and stop bad families early.",
            },
            "community_skill_risks": {
                "action": "block_or_penalize",
                "reason": "Use community skill memory as conservative risk routing, not as direct formula generation.",
            },
        },
    }


def build_community_skill_policy(skills: list[dict[str, Any]]) -> dict[str, Any]:
    risk_counts: Counter[str] = Counter()
    skill_ids: list[str] = []
    kinds: Counter[str] = Counter()
    recipe_ids: list[str] = []
    for skill in skills:
        skill_id = str(skill.get("skill_id") or "")
        if skill_id:
            skill_ids.append(skill_id)
        kind = str(skill.get("memory_kind") or "")
        if kind:
            kinds[kind] += 1
        if skill_id.startswith("forum_recipe::"):
            recipe_ids.append(skill_id.removeprefix("forum_recipe::"))
        evidence = skill.get("evidence") if isinstance(skill.get("evidence"), dict) else {}
        for flag, count in (evidence.get("risk_counts") or {}).items():
            risk_counts[str(flag)] += _safe_int(count) or 0
    enabled = bool(skills)
    return {
        "schema_version": 1,
        "enabled": enabled,
        "skill_count": len(skills),
        "skill_ids": skill_ids[:50],
        "skill_kinds": dict(sorted(kinds.items())),
        "recipe_ids": recipe_ids[:30],
        "risk_counts": dict(risk_counts.most_common(40)),
        "actions": {
            "hard_block_flags": sorted(COMMUNITY_SKILL_HARD_BLOCK_FLAGS),
            "template_transform_flags": sorted(COMMUNITY_SKILL_TEMPLATE_FLAGS),
            "penalize_flags": sorted(COMMUNITY_SKILL_PENALIZE_FLAGS),
            "template_action": "block_unless_orthogonal_overlay",
            "near_pass_action": "repair_or_fresh_recheck_before_submit",
            "field_family_crowding_action": "penalize_and_limit_budget",
        },
    }


def build_submitted_alpha_policy(submitted_alpha_map: dict[str, Any] | None) -> dict[str, Any]:
    if not submitted_alpha_map:
        return {}
    field_rows = submitted_alpha_map.get("field_summary") or []
    domain_rows = submitted_alpha_map.get("domain_summary") or []
    pair_rows = submitted_alpha_map.get("similarity_pairs") or []
    summary = submitted_alpha_map.get("summary") or {}
    saturated_fields = [
        _submitted_field_policy_entry(row)
        for row in field_rows
        if (
            (_safe_float(row.get("active_share")) or 0.0) >= 0.20
            or (_safe_float(row.get("active_alpha_count")) or 0.0) >= 6
            or (_safe_float(row.get("map_self_corr_fail_count")) or 0.0) >= 10
        )
    ]
    saturated_fields.sort(
        key=lambda row: (
            -(_safe_float(row.get("active_share")) or 0.0),
            -(_safe_float(row.get("map_self_corr_fail_count")) or 0.0),
            str(row.get("field") or ""),
        )
    )
    overused_anchor_fields = [
        row["field"]
        for row in saturated_fields
        if row.get("field") in PRICE_LIQUIDITY_FIELDS or (_safe_float(row.get("active_share")) or 0.0) >= 0.35
    ]
    if RETURN_ANCHOR_FIELD not in overused_anchor_fields:
        overused_anchor_fields.insert(0, RETURN_ANCHOR_FIELD)
    crowded_domains = [
        _submitted_domain_policy_entry(row)
        for row in domain_rows
        if (
            (_safe_float(row.get("active_count")) or 0.0) >= 4
            or (_safe_float(row.get("avg_nearest_similarity")) or 0.0) >= 0.65
            or (_safe_float(row.get("map_crowded_score")) or 0.0) >= 150.0
        )
    ]
    high_similarity_pairs = [
        {
            "alpha_id_a": row.get("alpha_id_a"),
            "alpha_id_b": row.get("alpha_id_b"),
            "overall_similarity": _safe_float(row.get("overall_similarity")) or 0.0,
            "field_overlap": _safe_float(row.get("field_overlap")) or 0.0,
            "operator_overlap": _safe_float(row.get("operator_overlap")) or 0.0,
        }
        for row in pair_rows
        if (_safe_float(row.get("overall_similarity")) or 0.0) >= 0.70
    ][:20]
    return {
        "schema_version": 1,
        "active_or_submitted_count": _safe_int(summary.get("active_or_submitted_count")) or len(submitted_alpha_map.get("alpha_details") or []),
        "median_nearest_similarity": _safe_float(summary.get("median_nearest_similarity")),
        "high_internal_similarity_pairs_ge_0_70": _safe_int(summary.get("high_internal_similarity_pairs_ge_0_70")) or len(high_similarity_pairs),
        "saturated_fields": saturated_fields,
        "overused_anchor_fields": overused_anchor_fields,
        "crowded_submitted_domains": crowded_domains,
        "high_similarity_pairs": high_similarity_pairs,
        "risk_control_only_fields": {
            RETURN_ANCHOR_FIELD: {
                "reason": "returns appears in most active alphas; use it as a small risk/control leg, not as the main alpha anchor.",
                "max_main_weight": 0.25,
                "penalize_if_unweighted": True,
            }
        },
        "gates": {
            "nearest_similarity_block_above": 0.70,
            "nearest_similarity_penalize_above": 0.62,
            "returns_main_anchor_action": "penalize",
            "price_liquidity_only_action": "block",
            "saturated_stack_penalty_threshold": 3,
            "require_fresh_anchor_for_saturated_stack": True,
        },
    }


def build_candidate_budget(directions: list[dict[str, Any]], *, max_directions: int = 30) -> dict[str, Any]:
    actionable = [
        row for row in directions
        if row.get("action") in {"explore", "probe_first", "prefer", "penalize"} and row.get("theme_id") not in RULE_ONLY_THEMES
    ][:max(1, max_directions)]
    total_score = sum(max(0.0, _safe_float(row.get("research_priority_score")) or 0.0) for row in actionable) or 1.0
    allocations = []
    for row in actionable:
        score = max(0.0, _safe_float(row.get("research_priority_score")) or 0.0)
        share = round(score / total_score, 4)
        allocations.append({
            "direction_id": row.get("direction_id"),
            "theme_id": row.get("theme_id"),
            "recipe_id": row.get("recipe_id"),
            "domains": row.get("domains", []),
            "action": row.get("action"),
            "budget_share": share,
            "suggested_initial_sims": max(1, round(share * 120)),
        })
    return {
        "schema_version": 1,
        "total_reference_sims": 120,
        "allocations": allocations,
    }


def load_submission_policy(path: Path | None) -> dict[str, Any] | None:
    if not path or not Path(path).is_file():
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if "submission_policy" in payload and isinstance(payload["submission_policy"], dict):
        return payload["submission_policy"]
    return payload if isinstance(payload, dict) else None


def evaluate_candidate_policy(candidate: dict[str, Any], policy: dict[str, Any] | None) -> dict[str, Any]:
    if not policy:
        return {"action": "allow", "reason": "no_policy", "score_adjustment": 0.0}
    expression = str(candidate.get("expression") or "")
    components = expression_components(expression) if expression else {"fields": set(), "operators": set()}
    fields = set(components.get("fields", []))
    domain = classify_domain(sorted(fields), expression) if expression else "unknown"
    marker_text = " ".join(str(candidate.get(key) or "") for key in ("source", "source_family", "tag", "mutation_strategy")).lower()
    is_direct_forum = any(marker in marker_text for marker in DIRECT_FORUM_MARKERS)
    has_orthogonal_overlay = _has_orthogonal_overlay(fields, expression)
    theme_id = _candidate_theme_id(candidate, domain=domain)
    theme_policy = (policy.get("theme_policies") or {}).get(theme_id, {})
    recipe_id = str(candidate.get("recipe_id") or (candidate.get("candidate_meta") or {}).get("recipe_id") or "")
    recipe_policy = (policy.get("recipe_policies") or {}).get(recipe_id, {}) if recipe_id else {}
    crowded_domains = set(policy.get("crowded_domains") or [])
    underexplored_domains = set(policy.get("underexplored_domains") or [])
    submitted_eval = _evaluate_submitted_alpha_constraints(
        candidate,
        fields=fields,
        expression=expression,
        policy=policy.get("submitted_alpha_map") or {},
    )
    community_eval = _evaluate_community_skill_constraints(
        candidate,
        fields=fields,
        expression=expression,
        policy=policy.get("community_skill_policy") or {},
        has_orthogonal_overlay=has_orthogonal_overlay,
    )
    base_score = _safe_float(theme_policy.get("research_priority_score")) or _safe_float(recipe_policy.get("research_priority_score")) or 25.0
    reasons: list[str] = []
    action = "allow"
    score_adjustment = 0.0

    if is_direct_forum and not has_orthogonal_overlay:
        return {
            "action": "block",
            "reason": "forum_direct_template_risk",
            "score_adjustment": -100.0,
            "domain": domain,
            "theme_id": theme_id,
            "required": "orthogonal field or operator overlay",
            "community_skill": community_eval,
        }
    if community_eval.get("action") == "block":
        return {
            "action": "block",
            "reason": ",".join(community_eval.get("reasons") or ["community_skill_block"]),
            "score_adjustment": _safe_float(community_eval.get("score_adjustment")) or -100.0,
            "domain": domain,
            "theme_id": theme_id,
            "recipe_id": recipe_id or None,
            "direct_forum": is_direct_forum,
            "orthogonal_overlay": has_orthogonal_overlay,
            "community_skill": community_eval,
            "submitted_alpha_constraints": submitted_eval,
        }
    if theme_id in RULE_ONLY_THEMES:
        action = "penalize"
        score_adjustment -= 20.0
        reasons.append("rule_only_theme")
    if domain in crowded_domains and is_direct_forum:
        action = "penalize"
        score_adjustment -= 15.0
        reasons.append("crowded_forum_direction")
    if domain in underexplored_domains:
        score_adjustment += 10.0
        reasons.append("underexplored_domain_bonus")
    if recipe_policy.get("action") == "probe_first":
        score_adjustment += 5.0
        reasons.append("probe_first_recipe")
    if submitted_eval.get("action") == "block":
        action = "block"
    elif submitted_eval.get("action") == "penalize" and action != "block":
        action = "penalize"
    score_adjustment += _safe_float(submitted_eval.get("score_adjustment")) or 0.0
    reasons.extend(submitted_eval.get("reasons") or [])
    if community_eval.get("action") == "penalize" and action != "block":
        action = "penalize"
    score_adjustment += _safe_float(community_eval.get("score_adjustment")) or 0.0
    reasons.extend(community_eval.get("reasons") or [])
    if base_score < _safe_float((policy.get("gates") or {}).get("low_priority_reject_below"), 15.0):
        action = "block"
        reasons.append("low_forum_direction_score")

    return {
        "action": action,
        "reason": ",".join(reasons) if reasons else "policy_pass",
        "score_adjustment": round(score_adjustment, 4),
        "domain": domain,
        "theme_id": theme_id,
        "recipe_id": recipe_id or None,
        "research_priority_score": round(base_score + score_adjustment, 4),
        "direct_forum": is_direct_forum,
        "orthogonal_overlay": has_orthogonal_overlay,
        "submitted_alpha_constraints": submitted_eval,
        "community_skill": community_eval,
    }


def annotate_candidate_with_policy(candidate: dict[str, Any], policy: dict[str, Any] | None) -> dict[str, Any]:
    evaluation = evaluate_candidate_policy(candidate, policy)
    base_score = _safe_float(candidate.get("research_priority_score"))
    if base_score is None:
        base_score = 50.0 - ((_safe_float(candidate.get("nearest_similarity")) or 0.0) * 25.0)
    return {
        **candidate,
        "forum_policy": evaluation,
        "forum_policy_action": evaluation.get("action"),
        "forum_policy_reason": evaluation.get("reason"),
        "community_skill_risk_flags": (evaluation.get("community_skill") or {}).get("risk_flags") or [],
        "community_skill_policy_reasons": (evaluation.get("community_skill") or {}).get("reasons") or [],
        "research_priority_score": round(base_score + (_safe_float(evaluation.get("score_adjustment")) or 0.0), 4),
    }


def write_forum_submission_artifacts(
    plan: dict[str, Any],
    *,
    output_dir: Path | None = None,
    obsidian_output: Path | None = None,
) -> dict[str, str]:
    files: dict[str, str] = {}
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        direction_scores_path = output_dir / "forum_direction_scores.jsonl"
        submission_policy_path = output_dir / "submission_policy.json"
        candidate_budget_path = output_dir / "candidate_budget.json"
        summary_path = output_dir / "summary.json"
        markdown_path = output_dir / "forum_submission_playbook.md"
        artifact_write_jsonl(direction_scores_path, plan["directions"])
        artifact_write_json(submission_policy_path, plan["submission_policy"])
        artifact_write_json(candidate_budget_path, plan["candidate_budget"])
        artifact_write_json(summary_path, _summary_payload(plan))
        artifact_write_text(markdown_path, plan["markdown"])
        files["direction_scores"] = str(direction_scores_path)
        files["submission_policy"] = str(submission_policy_path)
        files["candidate_budget"] = str(candidate_budget_path)
        files["summary"] = str(summary_path)
        files["markdown"] = str(markdown_path)
    if obsidian_output:
        obsidian_output.parent.mkdir(parents=True, exist_ok=True)
        artifact_write_text(obsidian_output, plan["markdown"])
        files["obsidian"] = str(obsidian_output)
    plan.setdefault("files", {}).update(files)
    return files


def render_forum_submission_playbook(
    *,
    directions: list[dict[str, Any]],
    policy: dict[str, Any],
    budget: dict[str, Any],
    config: ForumSubmissionOptimizerConfig,
    factor_map: dict[str, Any],
    forum_memory: dict[str, Any],
    submitted_alpha_map: dict[str, Any] | None = None,
) -> str:
    top = directions[:10]
    crowded = sorted(directions, key=lambda row: -_safe_float(_nested(row, "map_stats", "crowding"), 0.0))[:8]
    lines = [
        "---",
        "tags:",
        "  - worldquant_harness",
        "  - worldquant",
        "  - forum-submission-optimizer",
        f"generated_at: {datetime.now(timezone.utc).isoformat()}",
        "---",
        "",
        f"# {config.title}",
        "",
        "## 概览",
        "",
        f"- Factor nodes: {len(factor_map.get('nodes', []))}",
        f"- Forum clusters: {len(forum_memory.get('clusters', []))}",
        f"- Forum recipes: {len(forum_memory.get('recipes', []))}",
        f"- Community skills: {len(forum_memory.get('skills', []))}",
        f"- Submitted active alphas: {_nested(submitted_alpha_map, 'summary', 'active_or_submitted_count') or 0}",
        "- Policy: direct forum templates are blocked unless they introduce an orthogonal overlay.",
        "- Policy: returns should be a small risk/control leg, not the main anchor.",
        "",
        "## 优先研究方向",
        "",
        _direction_table(top),
        "",
        "## 拥挤论坛方向",
        "",
        _direction_table(crowded),
        "",
        "## 预算建议",
        "",
        _budget_table(budget.get("allocations", [])[:12]),
        "",
        "## 提交优化规则",
        "",
        f"- Strict similarity cutoff: {policy.get('gates', {}).get('strict_similarity_cutoff')}",
        f"- Crowded domains: {', '.join(policy.get('crowded_domains') or []) or 'none'}",
        f"- Underexplored domains: {', '.join(policy.get('underexplored_domains') or []) or 'none'}",
        "- Forum direct snippets: block unless field/operator family changes materially.",
        "- Correlation and robustness forum themes are constraints, not standalone alpha sources.",
    ]
    skill_policy = policy.get("community_skill_policy") or {}
    if skill_policy.get("enabled"):
        top_risks = list((skill_policy.get("risk_counts") or {}).keys())[:10]
        lines.extend([
            "",
            "## Community Skill Gates",
            "",
            f"- Loaded skills: {skill_policy.get('skill_count')}",
            f"- Skill kinds: {', '.join(f'{key}={value}' for key, value in (skill_policy.get('skill_kinds') or {}).items()) or 'none'}",
            f"- Top risk flags: {', '.join(top_risks) or 'none'}",
            "- Template-clone risks are blocked unless the candidate has a field/operator-family change and an orthogonal overlay.",
            "- Near-pass and operation-attribution risks are penalized and routed to fresh checks or repair before submit.",
        ])
    submitted_policy = policy.get("submitted_alpha_map") or {}
    if submitted_policy:
        lines.extend([
            "",
            "## 已提交地图约束",
            "",
            _submitted_policy_table(submitted_policy.get("saturated_fields", [])[:12]),
            "",
            f"- Overused anchors: {', '.join(submitted_policy.get('overused_anchor_fields') or []) or 'none'}",
            "- `returns`: use as risk/control, avoid unweighted or dominant `ts_rank(returns, ...)` anchors.",
        ])
    return "\n".join(lines).rstrip() + "\n"


def _direction_score(
    *,
    member_count: int,
    non_course_count: int,
    course_noise_count: int,
    related_failures: int,
    related_active: int,
    map_crowding: float,
    map_opportunity: float,
    avg_similarity: float,
    rule_only: bool,
) -> float:
    score = 20.0
    score += min(member_count, 400) * 0.08
    score += min(non_course_count, 250) * 0.12
    score += min(map_opportunity, 250.0) * 0.08
    score -= min(course_noise_count, 250) * 0.08
    score -= min(related_failures, 150) * 0.08
    score -= min(related_active, 80) * 0.12
    score -= map_crowding * 0.10
    score -= avg_similarity * 8.0
    if rule_only:
        score -= 22.0
    return round(score, 4)


def _direction_action(theme_id: str, *, score: float, crowding: float, failures: int) -> str:
    if theme_id in RULE_ONLY_THEMES:
        return "gate_only"
    if score >= 45.0 and crowding < 160:
        return "prefer"
    if score >= 25.0:
        return "explore"
    if failures > 80 or crowding > 180:
        return "penalize"
    return "probe_first"


def _recipe_action(theme_id: str, *, score: float, recipe: dict[str, Any]) -> str:
    if theme_id in RULE_ONLY_THEMES:
        return "gate_only"
    if "small" in " ".join(str(item) for item in recipe.get("stop_if") or []).lower():
        return "probe_first"
    return "prefer" if score >= 35 else "explore"


def _direction_reasons(theme_id: str, action: str, crowding: float, opportunity: float, failures: int, rule_only: bool) -> list[str]:
    reasons = [f"action={action}"]
    if rule_only:
        reasons.append("forum theme should guide gating/budget only")
    if crowding > 150:
        reasons.append("factor map shows crowded domain")
    if opportunity > 50:
        reasons.append("forum evidence is high relative to current map coverage")
    if failures > 50:
        reasons.append("historical high-similarity/self-correlation failures are dense")
    if theme_id == "missingness_coverage":
        reasons.append("small-batch probe recommended")
    return reasons


def _policy_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": row.get("action"),
        "research_priority_score": row.get("research_priority_score"),
        "domains": row.get("domains", []),
        "top_fields": row.get("top_fields", []),
        "top_operators": row.get("top_operators", []),
        "reasons": row.get("reasons", []),
    }


def _submitted_field_policy_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "field": row.get("field"),
        "active_alpha_count": _safe_int(row.get("active_alpha_count")) or 0,
        "active_share": _safe_float(row.get("active_share")) or 0.0,
        "map_node_count": _safe_int(row.get("map_node_count")) or 0,
        "map_self_corr_fail_count": _safe_int(row.get("map_self_corr_fail_count")) or 0,
        "map_avg_fitness": _safe_float(row.get("map_avg_fitness")),
    }


def _submitted_domain_policy_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "domain": row.get("domain"),
        "active_count": _safe_int(row.get("active_count")) or 0,
        "active_share": _safe_float(row.get("share")) or 0.0,
        "avg_nearest_similarity": _safe_float(row.get("avg_nearest_similarity")) or 0.0,
        "map_crowded_score": _safe_float(row.get("map_crowded_score")) or 0.0,
        "map_opportunity_score": _safe_float(row.get("map_opportunity_score")) or 0.0,
        "map_failures": (_safe_int(row.get("map_high_similarity_fail_count")) or 0)
        + (_safe_int(row.get("map_self_corr_fail_count")) or 0),
    }


def _domains_for_theme_or_fields(theme_id: str, fields: list[str]) -> list[str]:
    hinted = [domain for domain in THEME_DOMAIN_HINTS.get(theme_id, ()) if domain]
    if hinted:
        return hinted
    detected = classify_domain(fields, " ".join(fields))
    return [detected or "unknown"]


def _candidate_theme_id(candidate: dict[str, Any], *, domain: str) -> str:
    for key in ("source_theme", "theme_id"):
        if candidate.get(key):
            return str(candidate[key])
    meta = candidate.get("candidate_meta") if isinstance(candidate.get("candidate_meta"), dict) else {}
    for key in ("source_theme", "theme_id"):
        if meta.get(key):
            return str(meta[key])
    family_text = " ".join(str(candidate.get(key) or "") for key in ("source_family", "tag", "mutation_strategy")).lower()
    for theme in THEME_DOMAIN_HINTS:
        if theme in family_text:
            return theme
    if domain == "fundamental_quality":
        return "fundamental_value_quality"
    if domain in {"sentiment_news", "analyst_revision"}:
        return "sentiment_news_revision"
    if domain == "liquidity_microstructure":
        return "robustness_turnover"
    return "correlation_similarity" if "similar" in family_text or "forum_direct" in family_text else ""


def _has_orthogonal_overlay(fields: set[str], expression: str) -> bool:
    if not fields:
        return False
    non_price_fields = fields - PRICE_LIQUIDITY_FIELDS - {"industry", "subindustry", "sector"}
    if len(non_price_fields) >= 1 and (fields & PRICE_LIQUIDITY_FIELDS):
        return True
    domains = {
        classify_domain([field], expression)
        for field in fields
        if field not in {"industry", "subindustry", "sector"}
    }
    domains.discard("unknown")
    return len(domains) >= 2


def _evaluate_community_skill_constraints(
    candidate: dict[str, Any],
    *,
    fields: set[str],
    expression: str,
    policy: dict[str, Any],
    has_orthogonal_overlay: bool,
) -> dict[str, Any]:
    if not policy or not policy.get("enabled"):
        return {"action": "allow", "reasons": [], "score_adjustment": 0.0, "risk_flags": []}
    risk_flags = _candidate_risk_flags(candidate)
    reasons: list[str] = []
    action = "allow"
    score_adjustment = 0.0

    hard_flags = risk_flags & set(_nested(policy, "actions", "hard_block_flags") or COMMUNITY_SKILL_HARD_BLOCK_FLAGS)
    if hard_flags:
        return {
            "action": "block",
            "reasons": [f"community_skill_hard_block:{flag}" for flag in sorted(hard_flags)],
            "score_adjustment": -100.0,
            "risk_flags": sorted(risk_flags),
            "required": "remove unsupported/private community-derived source before simulation",
        }

    template_flags = risk_flags & set(_nested(policy, "actions", "template_transform_flags") or COMMUNITY_SKILL_TEMPLATE_FLAGS)
    if template_flags and not has_orthogonal_overlay:
        return {
            "action": "block",
            "reasons": ["template_clone_risk"],
            "score_adjustment": -100.0,
            "risk_flags": sorted(risk_flags),
            "required": "field-family or operator-family change plus orthogonal overlay",
        }
    if template_flags:
        action = "penalize"
        score_adjustment -= 8.0
        reasons.append("template_transform_required")

    penalize_flags = risk_flags & set(_nested(policy, "actions", "penalize_flags") or COMMUNITY_SKILL_PENALIZE_FLAGS)
    if penalize_flags:
        action = "penalize" if action != "block" else action
        score_adjustment -= min(24.0, 6.0 * len(penalize_flags))
        reasons.extend(f"community_skill_risk:{flag}" for flag in sorted(penalize_flags))

    if "metric_near_pass" in risk_flags:
        reasons.append("near_pass_requires_repair_or_fresh_recheck")
    if "field_family_crowding" in risk_flags:
        reasons.append("limit_same_field_family_budget")

    return {
        "action": action,
        "reasons": reasons,
        "score_adjustment": round(score_adjustment, 4),
        "risk_flags": sorted(risk_flags),
        "field_count": len(fields),
        "has_orthogonal_overlay": has_orthogonal_overlay,
        "expression_hash": _stable_expression_key(expression),
    }


def _candidate_risk_flags(candidate: dict[str, Any]) -> set[str]:
    flags: set[str] = set()
    for key in ("risk_flags", "community_skill_risk_flags"):
        value = candidate.get(key)
        if isinstance(value, list):
            flags.update(str(item) for item in value if item)
        elif isinstance(value, str) and value:
            flags.add(value)
    meta = candidate.get("candidate_meta") if isinstance(candidate.get("candidate_meta"), dict) else {}
    for key in ("risk_flags", "community_skill_risk_flags"):
        value = meta.get(key)
        if isinstance(value, list):
            flags.update(str(item) for item in value if item)
        elif isinstance(value, str) and value:
            flags.add(value)
    diagnosis = candidate.get("diagnosis") if isinstance(candidate.get("diagnosis"), dict) else {}
    value = diagnosis.get("risk_flags")
    if isinstance(value, list):
        flags.update(str(item) for item in value if item)
    elif isinstance(value, str) and value:
        flags.add(value)
    return flags


def _stable_expression_key(expression: str) -> str:
    compact = re.sub(r"\s+", " ", expression or "").strip().lower()
    if not compact:
        return ""
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()[:16]


def _evaluate_submitted_alpha_constraints(
    candidate: dict[str, Any],
    *,
    fields: set[str],
    expression: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    if not policy:
        return {"action": "allow", "reasons": [], "score_adjustment": 0.0}
    gates = policy.get("gates") or {}
    saturated_fields = {
        str(row.get("field"))
        for row in policy.get("saturated_fields") or []
        if row.get("field")
    }
    overused_anchor_fields = set(policy.get("overused_anchor_fields") or [])
    used_saturated_fields = sorted((fields & saturated_fields) - NEUTRALIZATION_FIELDS)
    fresh_anchor_fields = sorted(_fresh_anchor_fields(fields, overused_anchor_fields=overused_anchor_fields))
    returns_assessment = _returns_anchor_assessment(expression, fields, policy)
    nearest_similarity = _safe_float(candidate.get("nearest_similarity"))
    reasons: list[str] = []
    action = "allow"
    score_adjustment = 0.0

    if nearest_similarity is not None and nearest_similarity >= (_safe_float(gates.get("nearest_similarity_block_above")) or 0.70):
        action = "block"
        score_adjustment -= 100.0
        reasons.append("submitted_nearest_similarity_block")
    elif nearest_similarity is not None and nearest_similarity >= (_safe_float(gates.get("nearest_similarity_penalize_above")) or 0.62):
        action = "penalize"
        score_adjustment -= 12.0
        reasons.append("submitted_nearest_similarity_penalty")

    if returns_assessment.get("returns_only"):
        action = "block"
        score_adjustment -= 100.0
        reasons.append("returns_or_price_liquidity_only")
    elif returns_assessment.get("main_anchor"):
        if action != "block":
            action = "penalize"
        score_adjustment -= 18.0
        reasons.append("returns_main_anchor")
    elif returns_assessment.get("risk_control_use"):
        score_adjustment += 3.0
        reasons.append("returns_risk_control_use")

    saturated_threshold = _safe_int(gates.get("saturated_stack_penalty_threshold")) or 3
    if len(used_saturated_fields) >= saturated_threshold and not fresh_anchor_fields:
        action = "block" if action != "block" else action
        score_adjustment -= 35.0
        reasons.append("saturated_field_stack_without_fresh_anchor")
    elif len(used_saturated_fields) >= saturated_threshold:
        if action != "block":
            action = "penalize"
        score_adjustment -= min(16.0, 4.0 * len(used_saturated_fields))
        reasons.append("saturated_field_stack")

    return {
        "action": action,
        "reasons": reasons,
        "score_adjustment": round(score_adjustment, 4),
        "used_saturated_fields": used_saturated_fields,
        "fresh_anchor_fields": fresh_anchor_fields,
        "returns_assessment": returns_assessment,
        "nearest_similarity": nearest_similarity,
    }


def _fresh_anchor_fields(fields: set[str], *, overused_anchor_fields: set[str]) -> set[str]:
    blocked = set(PRICE_LIQUIDITY_FIELDS) | set(NEUTRALIZATION_FIELDS) | set(overused_anchor_fields)
    return {field for field in fields if field not in blocked}


def _returns_anchor_assessment(expression: str, fields: set[str], policy: dict[str, Any]) -> dict[str, Any]:
    uses_returns = RETURN_ANCHOR_FIELD in fields
    if not uses_returns:
        return {"uses_returns": False, "main_anchor": False, "risk_control_use": False, "returns_only": False}
    config = (policy.get("risk_control_only_fields") or {}).get(RETURN_ANCHOR_FIELD, {})
    max_main_weight = _safe_float(config.get("max_main_weight")) or 0.25
    compact = re.sub(r"\s+", "", expression.lower())
    explicit_weights = [
        abs(float(match.group(1)))
        for match in re.finditer(
            r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\*(?:rank\([^)]*returns|ts_rank\(returns|returns)",
            compact,
        )
    ]
    has_ranked_returns = "ts_rank(returns" in compact or "rank(returns" in compact or "returns," in compact
    has_small_weight = bool(explicit_weights) and max(explicit_weights) <= max_main_weight
    has_large_weight = bool(explicit_weights) and max(explicit_weights) > max_main_weight
    implicit_anchor = has_ranked_returns and not explicit_weights
    non_price_fields = fields - PRICE_LIQUIDITY_FIELDS - NEUTRALIZATION_FIELDS
    returns_only = not non_price_fields
    main_anchor = returns_only or has_large_weight or implicit_anchor
    risk_control_use = bool(non_price_fields) and has_small_weight and not main_anchor
    return {
        "uses_returns": True,
        "main_anchor": bool(main_anchor),
        "risk_control_use": bool(risk_control_use),
        "returns_only": bool(returns_only),
        "max_explicit_weight": max(explicit_weights) if explicit_weights else None,
        "implicit_anchor": bool(implicit_anchor),
        "non_price_field_count": len(non_price_fields),
    }


def _top_domains(directions: list[dict[str, Any]], *, metric: str, minimum: float) -> list[str]:
    values: Counter[str] = Counter()
    for row in directions:
        score = _safe_float(_nested(row, "map_stats", metric)) or 0.0
        if score < minimum:
            continue
        for domain in row.get("domains") or []:
            values[domain] += 1
    return [domain for domain, _ in values.most_common()]


def _underexplored_domains(directions: list[dict[str, Any]]) -> list[str]:
    values: Counter[str] = Counter()
    for row in directions:
        opportunity = _safe_float(_nested(row, "map_stats", "opportunity")) or 0.0
        active = _safe_float(_nested(row, "map_stats", "related_active_count")) or 0.0
        if opportunity > 25 and active <= 3:
            for domain in row.get("domains") or []:
                values[domain] += 1
    return [domain for domain, _ in values.most_common()]


def _theme_combination_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        for key in ("theme_a", "theme_b"):
            if row.get(key):
                counts[str(row[key])] += _safe_int(row.get("shared_sources")) or 1
    return dict(counts)


def _cluster_fields(cluster: dict[str, Any]) -> list[str]:
    fields = []
    for item in cluster.get("top_fields") or []:
        if isinstance(item, (list, tuple)) and item:
            fields.append(str(item[0]))
        elif isinstance(item, dict) and item.get("value"):
            fields.append(str(item["value"]))
    return fields


def _operators_from_template(template: str) -> list[dict[str, Any]]:
    ops = sorted(expression_components(template).get("operators", [])) if template else []
    return [{"value": op, "count": 1} for op in ops]


def _top_pairs(value: Any, *, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in value or []:
        if isinstance(item, dict):
            name = item.get("value") or item.get("field") or item.get("operator") or item.get("name")
            count = item.get("count", 1)
        elif isinstance(item, (list, tuple)) and item:
            name = item[0]
            count = item[1] if len(item) > 1 else 1
        else:
            name = item
            count = 1
        if name:
            out.append({"value": str(name), "count": _safe_int(count) or 1})
    return out[:limit]


def _direction_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No directions_"
    lines = [
        "| Direction | Action | Score | Domains | Evidence | Crowding | Reasons |",
        "|---|---|---:|---|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            f"| {_md(row.get('direction_id'))} | {_md(row.get('action'))} | {row.get('research_priority_score')} | "
            f"{_md(', '.join(row.get('domains') or []))} | {_nested(row, 'forum_evidence', 'non_course_count') or 0} | "
            f"{_nested(row, 'map_stats', 'crowding') or 0} | {_md('; '.join(row.get('reasons') or []))} |"
        )
    return "\n".join(lines)


def _budget_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No budget allocations_"
    lines = ["| Direction | Share | Initial Sims | Action |", "|---|---:|---:|---|"]
    for row in rows:
        lines.append(
            f"| {_md(row.get('direction_id'))} | {row.get('budget_share')} | "
            f"{row.get('suggested_initial_sims')} | {_md(row.get('action'))} |"
        )
    return "\n".join(lines)


def _submitted_policy_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No submitted-map constraints_"
    lines = ["| Field | Active Share | Active Count | Map Self-Corr Fail |", "|---|---:|---:|---:|"]
    for row in rows:
        lines.append(
            f"| {_md(row.get('field'))} | {_safe_float(row.get('active_share')) or 0.0:.4f} | "
            f"{row.get('active_alpha_count') or 0} | {row.get('map_self_corr_fail_count') or 0} |"
        )
    return "\n".join(lines)


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _summary_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: plan[key] for key in ("ok", "generated_at", "config", "summary") if key in plan}


def _dedupe_by_keys(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for row in rows:
        key = tuple(str(row.get(item) or "") for item in keys)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _dedupe_directions_by_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for row in rows:
        direction_id = str(row.get("direction_id") or "")
        if direction_id and direction_id in seen:
            continue
        if direction_id:
            seen.add(direction_id)
        out.append(row)
    return out


def _first_existing(directory: Path, *names: str) -> Path:
    for name in names:
        path = directory / name
        if path.is_file():
            return path
    return directory / names[0]


def _avg(values: list[float | None]) -> float:
    clean = [value for value in values if value is not None and not math.isnan(value)]
    return sum(clean) / len(clean) if clean else 0.0
