"""Build reusable skill memory from WorldQuant Community triage output."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifact_io import read_jsonl as _read_jsonl
from .artifact_io import utc_now as _now
from .artifact_io import write_json
from .artifact_io import write_jsonl as _write_jsonl
from .wq_failure_taxonomy import LOW_COVERAGE_PREFIXES

SCHEMA_VERSION = 1
DEFAULT_OUTPUT_DIRNAME = "skill_memory"
SKILL_FILE = "community_skill_memory.jsonl"
REPORT_FILE = "community_skill_summary.md"
MANIFEST_FILE = "community_skill_manifest.json"


@dataclass(frozen=True)
class CommunitySkillMemoryConfig:
    triage_dir: Path
    output_dir: Path | None = None
    forum_memory_dirs: tuple[Path, ...] = field(default_factory=tuple)
    source_label: str = ""
    top_sources: int = 8
    min_recipe_evidence: int = 1


def build_community_skill_memory(config: CommunitySkillMemoryConfig) -> dict[str, Any]:
    triage_dir = Path(config.triage_dir)
    output_dir = Path(config.output_dir) if config.output_dir else triage_dir.parent / DEFAULT_OUTPUT_DIRNAME
    output_dir.mkdir(parents=True, exist_ok=True)

    records = _read_jsonl(triage_dir / "triage_records.jsonl")
    candidates = _read_jsonl(triage_dir / "community_wq_candidates.jsonl")
    forum_memory = _read_forum_memory(config.forum_memory_dirs)
    skills = _build_skills(records=records, candidates=candidates, forum_memory=forum_memory, config=config)
    manifest = _manifest(
        triage_dir=triage_dir,
        output_dir=output_dir,
        records=records,
        candidates=candidates,
        forum_memory=forum_memory,
        skills=skills,
        source_label=config.source_label,
    )

    files = {
        "skills": output_dir / SKILL_FILE,
        "report": output_dir / REPORT_FILE,
        "manifest": output_dir / MANIFEST_FILE,
    }
    _write_jsonl(files["skills"], skills)
    files["report"].write_text(_render_report(manifest, skills), encoding="utf-8")
    manifest["files"] = {key: str(value) for key, value in files.items()}
    write_json(files["manifest"], manifest)
    return manifest


def _build_skills(
    *,
    records: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    forum_memory: dict[str, list[dict[str, Any]]],
    config: CommunitySkillMemoryConfig,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {
        "near_pass_repair": [],
        "alpha_template": [],
        "operation_attribution": [],
        "submission_gate": [],
    }
    for record in records:
        category = _record_category(record)
        if category in grouped:
            grouped[category].append(record)
        elif _has_any(record, "risk_flags", {"metric_near_pass"}):
            grouped["near_pass_repair"].append(record)
        elif record.get("candidate_expressions"):
            grouped["alpha_template"].append(record)
        elif record.get("value_type") in {"failure_case", "submission_rule"}:
            grouped["submission_gate"].append(record)

    now = _now()
    skills = [
        _category_skill(
            skill_id="community::near_pass_repair",
            memory_kind="community_near_pass_repair_skill",
            action=(
                "Route near-pass alphas through the refined failure-action skills before spending fresh budget: preserve "
                "the thesis, repair metric-near misses with overlays, and repair correlation-near misses with settings or "
                "field/operator-family changes."
            ),
            records=grouped["near_pass_repair"],
            selection_rule={
                "route_when": ["metric_near_pass", "correlation_risk near pass", "almost passing public metrics"],
                "action_buckets": [
                    "community_failure::metric_near_pass_overlay_repair",
                    "community_failure::correlation_near_pass_or_highscore_repair",
                ],
                "first_actions": ["refresh precheck", "settings grid", "broad overlay", "field-family change", "operator-family change"],
                "do_not": ["only change lookback windows for correlation failures"],
            },
            top_sources=config.top_sources,
            now=now,
        ),
        _category_skill(
            skill_id="community::alpha_template_transform",
            memory_kind="community_alpha_template_skill",
            action=(
                "Use forum templates as structural grammar only. Before simulation, swap at least one field family or "
                "operator family and add an orthogonal overlay so the candidate is not a direct community clone."
            ),
            records=grouped["alpha_template"],
            selection_rule={
                "route_when": ["candidate_seed", "possible_complete_alpha", "template_clone_risk"],
                "action_buckets": ["community_failure::template_clone_blocker"],
                "required_transform": ["field-family change", "operator-family change", "orthogonal overlay"],
                "block": ["direct snippets", "private code", "unchanged forum templates"],
            },
            top_sources=config.top_sources,
            now=now,
        ),
        _category_skill(
            skill_id="community::operation_attribution",
            memory_kind="community_operation_attribution_skill",
            action=(
                "Attribute failures before mutating: turnover maps to decay/trade_when, unit checks to scale/rank or "
                "valid units, platform limits to legal field/operator filtering, and availability issues to small probes."
            ),
            records=grouped["operation_attribution"],
            selection_rule={
                "route_when": ["high_turnover", "low_turnover", "unit_check", "platform_limit", "operator_availability_risk"],
                "action_buckets": [
                    "community_failure::turnover_density_repair",
                    "community_failure::operator_platform_unit_probe",
                    "community_failure::low_coverage_concentration_repair",
                    "community_failure::concentration_sparse_leg_or_distribution_repair",
                ],
                "repair_map": {
                    "turnover": ["decay", "trade_when", "humpdecay"],
                    "unit_check": ["rank", "scale", "dimensionless ratio"],
                    "platform_limit": ["legal field/operator verification"],
                },
            },
            top_sources=config.top_sources,
            now=now,
        ),
        _category_skill(
            skill_id="community::submission_gate",
            memory_kind="community_submission_gate_skill",
            action=(
                "Gate submissions with forum-derived risks: block direct templates, stale checks, unsupported operators, "
                "and crowded field families unless the candidate has a fresh check and low-correlation evidence."
            ),
            records=grouped["submission_gate"],
            selection_rule={
                "route_when": ["correlation_risk", "stale_precheck_risk", "field_family_crowding", "submission_rule"],
                "action_buckets": [
                    "community_failure::pending_check_not_submit_ready",
                    "community_failure::correlation_similarity_block_or_family_shift",
                    "community_failure::ledger_duplicate_block",
                ],
                "hard_blocks": ["private_code", "direct template clone", "unsupported operator"],
                "preferred_submit": ["fresh platform check", "low self/prod correlation", "diversified field family"],
            },
            top_sources=config.top_sources,
            now=now,
        ),
    ]
    skills.extend(_failure_action_skills(records=records, top_sources=config.top_sources, now=now))
    skills.extend(_recipe_skills(forum_memory.get("recipes", []), config=config, now=now))
    skills.extend(_candidate_family_skills(candidates, records=records, top_sources=config.top_sources, now=now))
    return [skill for skill in skills if skill.get("evidence", {}).get("record_count", 0) > 0 or skill.get("evidence", {}).get("recipe_evidence", 0) > 0]


def _category_skill(
    *,
    skill_id: str,
    memory_kind: str,
    action: str,
    records: list[dict[str, Any]],
    selection_rule: dict[str, Any],
    top_sources: int,
    now: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "memory_kind": memory_kind,
        "skill_id": skill_id,
        "action": action,
        "selection_rule": selection_rule,
        "evidence": _evidence(records, top_sources=top_sources),
        "anti_patterns": _anti_patterns(memory_kind),
        "created_at": now,
    }


def _failure_action_skills(*, records: list[dict[str, Any]], top_sources: int, now: str) -> list[dict[str, Any]]:
    definitions = [
        {
            "bucket": "metric_near_pass_overlay_repair",
            "action": (
                "For LOW_SHARPE/LOW_FITNESS near-pass evidence, preserve the thesis, reduce the crowded main trunk, "
                "and add one broad overlay before rechecking metrics and correlation."
            ),
            "selector": lambda row: _is_near_pass_record(row) and not _has_any(row, "risk_flags", {"correlation_risk"}),
            "route_when": ["metric_near_pass", "low_sharpe near threshold", "low_fitness near threshold"],
            "sub_causes": ["fitness just below threshold", "crowded trunk too dominant", "standalone overlay is weak"],
            "recommended_actions": ["reduce main trunk weight", "add broad overlay", "recheck metric and correlation"],
            "stop_conditions": ["not near-pass", "overlay causes concentration", "field signature is blocked"],
        },
        {
            "bucket": "correlation_near_pass_or_highscore_repair",
            "action": (
                "For high-score or near-pass candidates blocked by self/prod correlation, first isolate settings effects; "
                "if similarity is structural, change field or operator family before more window tuning."
            ),
            "selector": lambda row: _is_near_pass_record(row) and _has_any(row, "risk_flags", {"correlation_risk"}),
            "route_when": ["metric_near_pass", "correlation_risk", "self correlation near cutoff"],
            "sub_causes": ["near-cutoff correlation", "structural similarity", "crowded family trajectory"],
            "recommended_actions": ["settings grid", "field-family change", "operator-family change", "cross-domain overlay"],
            "stop_conditions": ["correlation far above cutoff", "only windows changed", "no readable check"],
        },
        {
            "bucket": "correlation_similarity_block_or_family_shift",
            "action": (
                "For non-near-pass correlation or high-similarity failures, block the current signature and require a new "
                "field family, operator skeleton, or source family before simulation."
            ),
            "selector": lambda row: _has_any(row, "risk_flags", {"correlation_risk", "field_family_crowding"}) and not _is_near_pass_record(row),
            "route_when": ["correlation_risk", "field_family_crowding", "high similarity"],
            "sub_causes": ["inventory collision", "superficial template change", "structural similarity"],
            "recommended_actions": ["block current signature", "change source family", "change operator skeleton"],
            "stop_conditions": ["template clone risk", "no new field family", "no fresh check"],
        },
        {
            "bucket": "template_clone_blocker",
            "action": (
                "Treat direct forum snippets as grammar only: block unchanged templates and require field-family or "
                "operator-family transformation plus an orthogonal overlay."
            ),
            "selector": lambda row: _has_any(row, "risk_flags", {"template_clone_risk", "possible_complete_alpha", "private_code"}) or bool(row.get("candidate_expressions")),
            "route_when": ["template_clone_risk", "possible_complete_alpha", "private_code", "candidate_seed"],
            "sub_causes": ["direct forum snippet", "course/homework-like answer", "single-window tweak"],
            "recommended_actions": ["withhold raw template", "change field family", "change operator family", "add overlay"],
            "stop_conditions": ["private code", "unchanged template", "similarity above cutoff"],
        },
        {
            "bucket": "low_coverage_concentration_repair",
            "action": (
                "For low-coverage families such as PCR/RavenPack-style fields, use tiny probes and require a broad "
                "price-volume or model-dispersion leg before allocating more simulation budget."
            ),
            "selector": lambda row: _has_low_coverage_field(row) or _text_has(row, {"low coverage", "coverage", "subuniverse"}),
            "route_when": ["low coverage", "coverage failure", "subuniverse risk", "PCR/RP field"],
            "sub_causes": ["low-coverage field dominates", "backfill hides coverage risk", "sparse options/PCR leg"],
            "recommended_actions": ["small probe", "add broad dispersion", "use as overlay only"],
            "stop_conditions": ["repeated concentration", "coverage failure on first probes"],
        },
        {
            "bucket": "concentration_sparse_leg_or_distribution_repair",
            "action": (
                "For concentrated-weight or sparse-leg failures, repair source design by reducing sparse legs and adding "
                "broad dispersion before group transforms; do not rely on truncation-only retests."
            ),
            "selector": lambda row: _has_any(row, "risk_flags", {"field_family_crowding"}) or _text_has(row, {"concentrated", "concentration", "sparse", "max position"}),
            "route_when": ["concentrated weight", "sparse leg", "field_family_crowding"],
            "sub_causes": ["multiple sparse legs", "group transform after sparse inputs", "peaked weight distribution"],
            "recommended_actions": ["reduce sparse leg count", "add broad dispersion", "move dispersion before group transforms"],
            "stop_conditions": ["truncation-only repair", "distribution remains peaked"],
        },
        {
            "bucket": "weak_metric_relegate_to_overlay_or_drop",
            "action": (
                "For non-near-pass weak metric evidence, demote the idea to a secondary overlay or drop it after one "
                "small paired-anchor test."
            ),
            "selector": lambda row: _text_has(row, {"low sharpe", "low fitness", "weak metric", "underperform"}) and not _is_near_pass_record(row),
            "route_when": ["low_sharpe", "low_fitness", "weak standalone signal"],
            "sub_causes": ["standalone sentiment/news is weak", "short-window microstructure is unstable", "no slow anchor"],
            "recommended_actions": ["demote to overlay", "pair with slow anchor", "drop if still weak"],
            "stop_conditions": ["negative sharpe", "high turnover plus weak fitness"],
        },
        {
            "bucket": "subuniverse_coverage_breadth_repair",
            "action": (
                "For LOW_SUB_UNIVERSE or density failures, add high-coverage liquidity/price-volume breadth and reduce "
                "narrow-field dependency before any submit attempt."
            ),
            "selector": lambda row: _text_has(row, {"subuniverse", "sub universe", "long count", "short count", "density", "coverage density"}),
            "route_when": ["LOW_SUB_UNIVERSE", "coverage density low", "long/short count unstable"],
            "sub_causes": ["narrow field family", "insufficient breadth", "backfill not enough"],
            "recommended_actions": ["add high-coverage leg", "reduce narrow dependency", "check breadth"],
            "stop_conditions": ["subuniverse failure repeats", "narrow-field only"],
        },
        {
            "bucket": "turnover_density_repair",
            "action": (
                "For high/low turnover or trade_when density risk, tune smoothing and participation together while "
                "monitoring turnover, long/short count, and subuniverse breadth."
            ),
            "selector": lambda row: _has_any(row, "risk_flags", {"high_turnover", "low_turnover"}) or "trade_when" in _record_operators(row),
            "route_when": ["high_turnover", "low_turnover", "trade_when", "event/news reactive field"],
            "sub_causes": ["event field too reactive", "trade_when gate collapses density", "decay weakens signal"],
            "recommended_actions": ["smooth high-turnover fields", "relax low-turnover gates", "monitor long/short count"],
            "stop_conditions": ["LOW_SUB_UNIVERSE after repair", "metrics destroyed by smoothing"],
        },
        {
            "bucket": "pending_check_not_submit_ready",
            "action": "Keep pending or stale correlation evidence in the check queue only; never treat pending as submit-ready.",
            "selector": lambda row: _has_any(row, "risk_flags", {"stale_precheck_risk"}) or _text_has(row, {"pending", "stale check", "stale precheck"}),
            "route_when": ["correlation_pending", "stale_precheck_risk", "missing readable check"],
            "sub_causes": ["check latency", "stale precheck", "submit queue mixed with pending rows"],
            "recommended_actions": ["move to check queue", "track pending age", "refresh check"],
            "stop_conditions": ["pending stale beyond run window", "missing platform handle"],
        },
        {
            "bucket": "ledger_duplicate_block",
            "action": "Block already-submitted or duplicate records and keep them as ledger evidence only.",
            "selector": lambda row: _text_has(row, {"already submitted", "duplicate", "same expression", "exact duplicate"}),
            "route_when": ["already_submitted", "duplicate", "exact expression hash exists"],
            "sub_causes": ["already submitted", "ledger duplicate", "exact hash collision"],
            "recommended_actions": ["block exact alpha", "record ledger evidence", "restart via new field/operator family"],
            "stop_conditions": ["already submitted", "exact expression hash exists"],
        },
        {
            "bucket": "operator_platform_unit_probe",
            "action": (
                "For unit, operator-availability, platform-limit, or unknown-support issues, run tiny legal-input probes "
                "and convert units with rank/scale/ratios before broader simulation."
            ),
            "selector": lambda row: _has_any(row, "risk_flags", {"unit_check", "platform_limit", "operator_availability_risk", "unknown_or_unsupported"}),
            "route_when": ["unit_check", "platform_limit", "operator_availability_risk", "unknown_or_unsupported"],
            "sub_causes": ["unit mismatch", "unsupported operator", "field unavailable", "platform limit"],
            "recommended_actions": ["legal input probe", "rank/scale/ratio conversion", "stop unsupported operator family"],
            "stop_conditions": ["operator rejected twice", "field unsupported", "unit cannot be normalized"],
        },
    ]
    rows = []
    for definition in definitions:
        matched = [record for record in records if definition["selector"](record)]
        if not matched:
            continue
        bucket = str(definition["bucket"])
        rows.append(_category_skill(
            skill_id=f"community_failure::{bucket}",
            memory_kind="community_failure_action_skill",
            action=str(definition["action"]),
            records=matched,
            selection_rule={
                "action_bucket": bucket,
                "route_when": definition["route_when"],
                "sub_causes": definition["sub_causes"],
                "recommended_actions": definition["recommended_actions"],
                "stop_conditions": definition["stop_conditions"],
                "record_shape": [
                    "failure_kind",
                    "action_bucket",
                    "sub_cause",
                    "field_signature",
                    "operators",
                    "metrics",
                    "recommended_next_action",
                    "stop_condition",
                    "source_path",
                ],
            },
            top_sources=top_sources,
            now=now,
        ))
    return rows


def _recipe_skills(recipes: list[dict[str, Any]], *, config: CommunitySkillMemoryConfig, now: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sorted_recipes = sorted(
        recipes,
        key=lambda row: (int(row.get("evidence_records", 0) or 0), int(row.get("non_course_sources", 0) or 0)),
        reverse=True,
    )
    for recipe in sorted_recipes[:20]:
        evidence = int(recipe.get("evidence_records", 0) or 0)
        if evidence < config.min_recipe_evidence:
            continue
        recipe_id = str(recipe.get("recipe_id") or _stable_id(str(recipe.get("template") or "")))
        rows.append({
            "schema_version": SCHEMA_VERSION,
            "memory_kind": "community_alpha_template_skill",
            "skill_id": f"forum_recipe::{recipe_id}",
            "action": "Generate transformed candidates from this forum-derived recipe; do not submit the template unchanged.",
            "selection_rule": {
                "source_theme": recipe.get("source_theme"),
                "template": recipe.get("template"),
                "fields": recipe.get("fields") or [],
                "stop_if": recipe.get("stop_if") or ["self-correlation above strict cutoff"],
                "max_initial_sims": recipe.get("max_initial_sims"),
            },
            "evidence": {
                "record_count": 0,
                "recipe_evidence": evidence,
                "non_course_sources": int(recipe.get("non_course_sources", 0) or 0),
                "source_recipe": recipe_id,
            },
            "anti_patterns": ["unchanged forum template", "single-window tweak", "no orthogonal overlay"],
            "created_at": now,
        })
    return rows


def _candidate_family_skills(
    candidates: list[dict[str, Any]],
    *,
    records: list[dict[str, Any]],
    top_sources: int,
    now: str,
) -> list[dict[str, Any]]:
    by_category: dict[str, list[dict[str, Any]]] = {}
    category_by_source = {
        (str(row.get("post_id") or ""), str(row.get("comment_id") or "")): _record_category(row) for row in records
    }
    for candidate in candidates:
        key = (str(candidate.get("source_post_id") or ""), str(candidate.get("source_comment_id") or ""))
        category = str(candidate.get("experience_category") or category_by_source.get(key) or "alpha_template")
        by_category.setdefault(category, []).append(candidate)
    rows: list[dict[str, Any]] = []
    for category, items in sorted(by_category.items(), key=lambda item: len(item[1]), reverse=True)[:8]:
        if len(items) < 2:
            continue
        rows.append({
            "schema_version": SCHEMA_VERSION,
            "memory_kind": "community_candidate_family_skill",
            "skill_id": f"community_candidate_family::{category}",
            "action": "Use these derived templates as a seed family only after dedupe, transformation, and low-correlation gating.",
            "selection_rule": {
                "experience_category": category,
                "seed_limit": min(top_sources, len(items)),
                "required": ["dedupe against existing alphas", "transform direct templates", "run check before submit"],
            },
            "evidence": {
                "record_count": len(items),
                "top_candidates": [
                    {
                        "expression": item.get("expression"),
                        "tag": item.get("tag"),
                        "source_post_id": item.get("source_post_id"),
                        "source_comment_id": item.get("source_comment_id"),
                        "relevance_score": item.get("relevance_score"),
                    }
                    for item in sorted(items, key=lambda row: int(row.get("relevance_score", 0) or 0), reverse=True)[:top_sources]
                ],
            },
            "anti_patterns": ["submit seed expression directly", "ignore self/prod correlation"],
            "created_at": now,
        })
    return rows


def _evidence(records: list[dict[str, Any]], *, top_sources: int) -> dict[str, Any]:
    ranked = sorted(records, key=lambda row: int(row.get("relevance_score", 0) or 0), reverse=True)
    return {
        "record_count": len(records),
        "value_type_counts": dict(Counter(str(row.get("value_type") or "unknown") for row in records)),
        "risk_counts": dict(_counter_from_lists(records, "risk_flags").most_common(20)),
        "field_counts": dict(_counter_from_lists(records, "wq_fields").most_common(20)),
        "operator_counts": dict(_counter_from_lists(records, "operators").most_common(20)),
        "examples": [_compact_source(row) for row in ranked[:top_sources]],
    }


def _anti_patterns(memory_kind: str) -> list[str]:
    common = ["direct forum copy", "single-parameter tweak", "submit without fresh check"]
    if "near_pass" in memory_kind:
        return common + ["abandon high-score near-pass parent before repair"]
    if "operation" in memory_kind:
        return common + ["mutate expression without diagnosing failure source"]
    if "submission_gate" in memory_kind:
        return common + ["ignore stale precheck or correlation warnings"]
    return common + ["treat public template as proprietary edge"]


def _record_category(row: dict[str, Any]) -> str:
    category = str(row.get("experience_category") or "")
    if category and category != "unknown":
        return category
    risk_flags = {str(value) for value in row.get("risk_flags") or []}
    text = " ".join(str(row.get(key) or "") for key in ("title", "hypothesis", "excerpt")).lower()
    if "metric_near_pass" in risk_flags or any(token in text for token in ("near pass", "almost pass", "close to pass", "接近过线")):
        return "near_pass_repair"
    if "template_clone_risk" in risk_flags or row.get("candidate_expressions") or row.get("value_type") == "candidate_seed":
        return "alpha_template"
    if risk_flags & {
        "high_turnover",
        "low_turnover",
        "unit_check",
        "platform_limit",
        "unknown_or_unsupported",
        "operator_availability_risk",
    }:
        return "operation_attribution"
    if risk_flags & {"correlation_risk", "stale_precheck_risk", "field_family_crowding"}:
        return "submission_gate"
    if row.get("value_type") in {"submission_rule", "failure_case"}:
        return "submission_gate"
    if row.get("wq_fields") or row.get("operators"):
        return "operation_attribution"
    return str(row.get("value_type") or "unknown")


def _compact_source(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "post_id": row.get("post_id"),
        "comment_id": row.get("comment_id"),
        "source_type": row.get("source_type"),
        "title": _short(row.get("title"), 120),
        "url": row.get("url"),
        "relevance_score": row.get("relevance_score"),
        "value_type": row.get("value_type"),
        "experience_category": _record_category(row),
        "risk_flags": row.get("risk_flags") or [],
        "fields": (row.get("wq_fields") or [])[:10],
        "operators": (row.get("operators") or [])[:10],
        "excerpt": _short(row.get("excerpt"), 220),
    }


def _manifest(
    *,
    triage_dir: Path,
    output_dir: Path,
    records: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    forum_memory: dict[str, list[dict[str, Any]]],
    skills: list[dict[str, Any]],
    source_label: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "source_label": source_label,
        "triage_dir": str(triage_dir),
        "output_dir": str(output_dir),
        "record_count": len(records),
        "candidate_count": len(candidates),
        "skill_count": len(skills),
        "experience_categories": dict(Counter(_record_category(row) for row in records)),
        "skill_kinds": dict(Counter(str(row.get("memory_kind") or "unknown") for row in skills)),
        "action_buckets": dict(Counter(
            str((row.get("selection_rule") or {}).get("action_bucket"))
            for row in skills
            if (row.get("selection_rule") or {}).get("action_bucket")
        )),
        "forum_memory": {key: len(value) for key, value in forum_memory.items()},
        "privacy_note": "Community skills contain compact normalized evidence only; credentials and raw API payloads are not stored.",
    }


def _render_report(manifest: dict[str, Any], skills: list[dict[str, Any]]) -> str:
    lines = [
        "# WorldQuant Community Skill Memory",
        "",
        f"- Source: `{manifest.get('triage_dir')}`",
        f"- Records: {manifest.get('record_count')}",
        f"- Candidates: {manifest.get('candidate_count')}",
        f"- Skills: {manifest.get('skill_count')}",
        "",
        "## Experience Categories",
        "",
    ]
    for key, value in sorted((manifest.get("experience_categories") or {}).items()):
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Skills", ""])
    for skill in skills:
        evidence = skill.get("evidence") or {}
        rule = skill.get("selection_rule") if isinstance(skill.get("selection_rule"), dict) else {}
        suffix = f" [{rule.get('action_bucket')}]" if rule.get("action_bucket") else ""
        lines.append(f"- `{skill.get('skill_id')}` ({skill.get('memory_kind')}){suffix}: {skill.get('action')}")
        lines.append(f"  Evidence: {evidence.get('record_count', 0)} records; recipe evidence {evidence.get('recipe_evidence', 0)}")
    return "\n".join(lines).rstrip() + "\n"


def _read_forum_memory(directories: tuple[Path, ...]) -> dict[str, list[dict[str, Any]]]:
    out = {"clusters": [], "recipes": [], "rules": [], "combinations": []}
    for directory in directories:
        path = Path(directory)
        out["clusters"].extend(_read_jsonl(_first_existing(path, "forum_idea_clusters_strict.jsonl", "forum_idea_clusters.jsonl")))
        out["recipes"].extend(_read_jsonl(path / "forum_candidate_recipes.jsonl"))
        out["rules"].extend(_read_jsonl(path / "forum_pattern_rules.jsonl"))
        out["combinations"].extend(_read_jsonl(path / "forum_idea_theme_combinations.jsonl"))
    return out


def _first_existing(directory: Path, *names: str) -> Path:
    for name in names:
        path = directory / name
        if path.is_file():
            return path
    return directory / names[0]


def _counter_from_lists(records: list[dict[str, Any]], key: str) -> Counter[str]:
    counter: Counter[str] = Counter()
    for row in records:
        for value in row.get(key) or []:
            counter[str(value)] += 1
    return counter


def _has_any(row: dict[str, Any], key: str, wanted: set[str]) -> bool:
    return bool({str(value) for value in row.get(key) or []} & wanted)


def _is_near_pass_record(row: dict[str, Any]) -> bool:
    if _record_category(row) == "near_pass_repair":
        return True
    if _has_any(row, "risk_flags", {"metric_near_pass"}):
        return True
    return _text_has(row, {"near pass", "near-pass", "almost pass", "close to pass", "接近过线"})


def _record_fields(row: dict[str, Any]) -> set[str]:
    return {str(value) for value in row.get("wq_fields") or row.get("fields") or [] if value}


def _record_operators(row: dict[str, Any]) -> set[str]:
    return {str(value) for value in row.get("operators") or [] if value}


def _has_low_coverage_field(row: dict[str, Any]) -> bool:
    return any(field.startswith(LOW_COVERAGE_PREFIXES) for field in _record_fields(row))


def _text_has(row: dict[str, Any], terms: set[str]) -> bool:
    text = " ".join(str(row.get(key) or "") for key in ("title", "hypothesis", "excerpt", "value_type")).lower()
    return any(term.lower() in text for term in terms)


def _short(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
