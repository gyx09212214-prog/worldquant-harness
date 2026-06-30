"""Build reusable skill memory from WorldQuant Community triage output."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

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
    files["manifest"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    manifest["files"] = {key: str(value) for key, value in files.items()}
    files["manifest"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
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
                "Repair near-pass alphas before spending fresh budget: preserve the economic idea, rerun current checks, "
                "try a small settings grid, and change field or operator family when correlation is the blocker."
            ),
            records=grouped["near_pass_repair"],
            selection_rule={
                "route_when": ["metric_near_pass", "correlation_risk near pass", "almost passing public metrics"],
                "first_actions": ["refresh precheck", "settings grid", "field-family or operator-family change"],
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
                "hard_blocks": ["private_code", "direct template clone", "unsupported operator"],
                "preferred_submit": ["fresh platform check", "low self/prod correlation", "diversified field family"],
            },
            top_sources=config.top_sources,
            now=now,
        ),
    ]
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
        lines.append(f"- `{skill.get('skill_id')}` ({skill.get('memory_kind')}): {skill.get('action')}")
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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


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


def _short(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
