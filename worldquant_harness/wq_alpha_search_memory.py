"""Build Alpha-GPT style search memory from WQ run artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifact_io import read_jsonl as _read_jsonl
from .artifact_io import utc_now as _now
from .artifact_io import write_json
from .artifact_io import write_jsonl as _write_jsonl
from .expression_parser import normalize_expression
from .record_utils import first_float as _first_float
from .record_utils import first_stripped_text as _first_text
from .record_utils import nested as _nested
from .record_utils import safe_float as _safe_float
from .report_utils import ratio as _ratio
from .source_utils import source_run_id_from_cycle_path as _source_run_id
from .wq_brain_service import submit_threshold_checks
from .wq_expression_utils import expression_components as _components

SCHEMA_VERSION = 1

ALPHA_SEARCH_ARTIFACT_NAMES = {
    "simulation_results.jsonl",
    "review_queue.jsonl",
    "check_results.jsonl",
    "pre_submit_check_historical_readable.jsonl",
    "pre_submit_check_kq3n_variants.jsonl",
    "pre_submit_check_top5.jsonl",
    "pre_submit_check_batch2.jsonl",
    "presubmit_ready.jsonl",
    "presubmit_ready_sequential.jsonl",
    "presubmit_rejected.jsonl",
    "submit_results.jsonl",
    "submit_existing_results.jsonl",
    "submitted_accumulator.jsonl",
}

SUCCESS_STATUSES = {"ACTIVE", "SUBMITTED"}
PRECHECK_PASS_STATUSES = {"api_check_readable", "platform_active_check_readable"}
SELF_CORRELATION_STATUSES = {"self_correlation_fail", "platform_active_sc_above_cutoff"}
SUB_UNIVERSE_CHECKS = {"LOW_SUB_UNIVERSE_SHARPE", "LOW_SUB_UNIVERSE_FITNESS"}


@dataclass(frozen=True)
class WQAlphaSearchMemoryConfig:
    """Configuration for local Alpha-GPT search memory generation."""

    reports_dir: Path
    output_dir: Path
    run_dirs: tuple[Path, ...] = field(default_factory=tuple)
    local_file_limit: int = 0
    record_limit: int = 0
    target_submit_count: int = 5
    min_high_score: float = 1.0
    min_parent_score: float = 1.0
    preferred_corr_max: float = 0.70
    min_turnover: float = 0.01
    max_turnover: float = 0.70
    sc_min: float = 0.70
    sc_max: float = 0.82
    max_parents: int = 20
    max_candidates_per_parent: int = 12
    decays: tuple[int, ...] = (2, 4, 6, 8)
    truncations: tuple[float, ...] = (0.02, 0.03, 0.05)
    neutralizations: tuple[str, ...] = ("SUBINDUSTRY", "INDUSTRY", "SECTOR")


def build_alpha_search_memory(config: WQAlphaSearchMemoryConfig) -> dict[str, Any]:
    """Build trajectory ledger, skill memory, near-pass candidates, and report files."""

    output_dir = config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    artifacts = _artifact_paths(config)
    events = _load_events(artifacts, limit=config.record_limit)
    trajectories = _merge_trajectory_events(events, config=config)
    family_scores = _family_scores(trajectories, config=config)
    near_pass_candidates = generate_near_pass_repair_candidates(trajectories, config=config)
    active_expression_hashes = {
        str(row.get("expression_hash"))
        for row in trajectories
        if row.get("lifecycle") == "active" and row.get("expression_hash")
    }
    top_submit_targets = _top_submit_targets(trajectories, config=config, active_expression_hashes=active_expression_hashes)
    top_check_targets = _top_check_targets(trajectories, config=config, active_expression_hashes=active_expression_hashes)
    skill_memory = _build_skill_memory(
        trajectories,
        family_scores=family_scores,
        repair_candidates=near_pass_candidates,
        top_submit_targets=top_submit_targets,
        top_check_targets=top_check_targets,
        config=config,
    )
    summary = _summary(
        artifacts=artifacts,
        events=events,
        trajectories=trajectories,
        family_scores=family_scores,
        skill_memory=skill_memory,
        repair_candidates=near_pass_candidates,
        top_submit_targets=top_submit_targets,
        top_check_targets=top_check_targets,
        config=config,
    )

    files = {
        "trajectory_ledger": str(output_dir / "trajectory_ledger.jsonl"),
        "skill_memory": str(output_dir / "skill_memory.jsonl"),
        "family_scores": str(output_dir / "family_scores.json"),
        "near_pass_repair_candidates": str(output_dir / "near_pass_repair_candidates.jsonl"),
        "top_submit_targets": str(output_dir / "top_submit_targets.jsonl"),
        "top_check_targets": str(output_dir / "top_check_targets.jsonl"),
        "summary": str(output_dir / "summary.json"),
        "markdown": str(output_dir / "alpha_search_report.md"),
    }
    _write_jsonl(Path(files["trajectory_ledger"]), trajectories)
    _write_jsonl(Path(files["skill_memory"]), skill_memory)
    write_json(files["family_scores"], family_scores)
    _write_jsonl(Path(files["near_pass_repair_candidates"]), near_pass_candidates)
    _write_jsonl(Path(files["top_submit_targets"]), top_submit_targets)
    _write_jsonl(Path(files["top_check_targets"]), top_check_targets)

    result = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "artifact_count": len(artifacts),
        "event_count": len(events),
        "trajectory_count": len(trajectories),
        "skill_count": len(skill_memory),
        "near_pass_repair_candidate_count": len(near_pass_candidates),
        "top_submit_target_count": len(top_submit_targets),
        "top_check_target_count": len(top_check_targets),
        "summary": summary,
        "files": files,
    }
    write_json(files["summary"], result)
    Path(files["markdown"]).write_text(render_alpha_search_report(result), encoding="utf-8")
    return result


def generate_near_pass_repair_candidates(
    trajectories: list[dict[str, Any]],
    *,
    config: WQAlphaSearchMemoryConfig,
) -> list[dict[str, Any]]:
    """Generate settings-only repairs for strong alphas that failed near the self-corr cutoff."""

    parents = [
        row
        for row in trajectories
        if _is_near_sc_repair_parent(row, config=config)
    ]
    parents = sorted(
        parents,
        key=lambda row: (
            -(_safe_float(row.get("wq_score")) or 0.0),
            -(_safe_float(row.get("fitness")) or 0.0),
            -(_safe_float(row.get("returns")) or 0.0),
            abs((_safe_float(row.get("sc_value")) or config.sc_min) - config.sc_min),
            str(row.get("alpha_id") or row.get("trajectory_id") or ""),
        ),
    )[: max(config.max_parents, 0)]

    candidates: list[dict[str, Any]] = []
    for parent in parents:
        base_settings = _settings_from_record(parent)
        parent_id = str(parent.get("alpha_id") or parent.get("trajectory_id") or parent.get("expression_hash") or "")
        parent_tag = str(parent.get("tag") or parent_id or "parent")
        used = 0
        for neutralization in config.neutralizations:
            for decay in config.decays:
                for truncation in config.truncations:
                    settings = dict(base_settings)
                    settings.update({
                        "neutralization": neutralization,
                        "decay": int(decay),
                        "truncation": float(truncation),
                    })
                    if _same_core_settings(settings, base_settings):
                        continue
                    key = _hash(json.dumps({
                        "parent": parent_id,
                        "expression": parent.get("expression"),
                        "settings": settings,
                    }, sort_keys=True, default=str))[:16]
                    candidates.append({
                        "schema_version": SCHEMA_VERSION,
                        "candidate_key": key,
                        "tag": _repair_tag(parent_tag, neutralization, decay, truncation),
                        "source_family": "near_sc_cutoff_settings_repair",
                        "mutation_strategy": "settings_grid",
                        "parent_alpha_id": parent.get("alpha_id"),
                        "parent_trajectory_id": parent.get("trajectory_id"),
                        "parent_tag": parent.get("tag"),
                        "parent_metrics": {
                            "wq_score": parent.get("wq_score"),
                            "sharpe": parent.get("sharpe"),
                            "fitness": parent.get("fitness"),
                            "returns": parent.get("returns"),
                            "turnover": parent.get("turnover"),
                            "sc_value": parent.get("sc_value"),
                            "correlation_risk": parent.get("correlation_risk"),
                            "api_check_status": parent.get("api_check_status"),
                        },
                        "expression": parent.get("expression"),
                        "simulation_settings": settings,
                        "repair_hints": [
                            "freeze_expression_to_isolate_platform_correlation_effect",
                            "vary_neutralization_decay_truncation_before_new_expression_budget",
                            "promote_only_after_check_only_api_check_readable",
                        ],
                        "expected_effect": "Lower live self-correlation while preserving the parent alpha thesis and in-sample Sharpe.",
                        "priority_score": _repair_priority(parent, config=config),
                        "created_at": _now(),
                    })
                    used += 1
                    if used >= config.max_candidates_per_parent:
                        break
                if used >= config.max_candidates_per_parent:
                    break
            if used >= config.max_candidates_per_parent:
                break
    return candidates


def render_alpha_search_report(result: dict[str, Any]) -> str:
    """Render a short human-readable search memory report."""

    summary = result.get("summary") or {}
    funnel = summary.get("funnel") or {}
    rates = funnel.get("rates") or {}
    lines = [
        "# WQ Alpha Search Memory",
        "",
        "## Funnel",
        "",
        f"- Artifacts: {result.get('artifact_count')}",
        f"- Events: {result.get('event_count')}",
        f"- Trajectories: {result.get('trajectory_count')}",
        f"- Simulated: {funnel.get('simulated_count')}",
        f"- WQ high-score hits: {funnel.get('high_score_count')}",
        f"- Platform metric eligible: {funnel.get('platform_eligible_count')}",
        f"- Check-readable: {funnel.get('check_readable_count')}",
        f"- Active/submitted: {funnel.get('active_count')}",
        f"- Self-corr failures: {funnel.get('self_corr_fail_count')}",
        f"- Near self-corr repair parents: {funnel.get('near_sc_repair_parent_count')}",
        f"- Submit target slots: {summary.get('target_submit_count')}",
        f"- Current submit targets: {summary.get('top_submit_target_count')}",
        f"- Check queue targets: {summary.get('top_check_target_count')}",
        f"- High-score per simulated: {rates.get('high_score_per_simulated')}",
        f"- Active per high-score: {rates.get('active_per_high_score')}",
        "",
        "## Top Families",
        "",
    ]
    for row in (summary.get("top_families") or [])[:10]:
        lines.append(
            f"- `{row.get('family')}` score={row.get('priority_score')} "
            f"active={row.get('active_count')} ready={row.get('check_readable_count')} "
            f"high_score={row.get('high_score_count')} self_corr={row.get('self_corr_fail_count')}"
        )
    if not summary.get("top_families"):
        lines.append("- none")

    lines.extend(["", "## Repair Queue", ""])
    for row in (summary.get("repair_queue_preview") or [])[:10]:
        metrics = row.get("parent_metrics") or {}
        lines.append(
            f"- `{row.get('tag')}` parent={row.get('parent_alpha_id')} "
            f"sharpe={metrics.get('sharpe')} fitness={metrics.get('fitness')} sc={metrics.get('sc_value')} "
            f"settings={row.get('simulation_settings')}"
        )
    if not summary.get("repair_queue_preview"):
        lines.append("- none")

    lines.extend(["", "## Submit Targets", ""])
    for row in (summary.get("submit_target_preview") or [])[:10]:
        lines.append(
            f"- `{row.get('alpha_id')}` score={row.get('wq_score')} priority={row.get('submit_priority')} "
            f"corr={row.get('correlation_risk')} fitness={row.get('fitness')} turnover={row.get('turnover')}"
        )
    if not summary.get("submit_target_preview"):
        lines.append("- none")

    lines.extend(["", "## Check Queue", ""])
    for row in (summary.get("check_target_preview") or [])[:10]:
        lines.append(
            f"- `{row.get('alpha_id')}` score={row.get('wq_score')} priority={row.get('submit_priority')} "
            f"corr={row.get('correlation_risk')} lifecycle={row.get('lifecycle')}"
        )
    if not summary.get("check_target_preview"):
        lines.append("- none")

    lines.extend(["", "## Skills", ""])
    for row in summary.get("skill_preview") or []:
        lines.append(f"- `{row.get('skill_id')}`: {row.get('action')}")
    if not summary.get("skill_preview"):
        lines.append("- none")
    return "\n".join(lines).rstrip() + "\n"


def _artifact_paths(config: WQAlphaSearchMemoryConfig) -> list[Path]:
    roots = list(config.run_dirs) or [config.reports_dir]
    paths: list[Path] = []
    for root in roots:
        root = Path(root)
        if root.is_file():
            if _is_alpha_search_artifact(root):
                paths.append(root)
            continue
        if not root.exists():
            continue
        paths.extend(path for path in root.rglob("*.jsonl") if _is_alpha_search_artifact(path))
    unique = {str(path.resolve()).lower(): path for path in paths}
    out = sorted(unique.values(), key=lambda item: item.stat().st_mtime, reverse=True)
    if config.local_file_limit > 0:
        return out[: config.local_file_limit]
    return out


def _is_alpha_search_artifact(path: Path) -> bool:
    return path.name in ALPHA_SEARCH_ARTIFACT_NAMES or (
        path.name.startswith("selected_candidate") and path.suffix == ".jsonl"
    )


def _load_events(paths: list[Path], *, limit: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in paths:
        source_type = path.name.removesuffix(".jsonl")
        for row_index, row in enumerate(_read_jsonl(path)):
            event = _normalize_event(row, source_file=path, source_type=source_type, row_index=row_index)
            if not event:
                continue
            events.append(event)
            if limit > 0 and len(events) >= limit:
                return events
    return _dedupe_events(events)


def _normalize_event(
    row: dict[str, Any],
    *,
    source_file: Path,
    source_type: str,
    row_index: int,
) -> dict[str, Any] | None:
    expression = _first_text(
        row.get("expression"),
        _nested(row, ("regular", "code")),
        _nested(row, ("settings", "regular", "code")),
        _nested(row, ("precheck", "expression")),
    )
    alpha_id = _first_text(row.get("alpha_id"), _nested(row, ("precheck", "alpha_id")))
    candidate_key = _first_text(row.get("candidate_key"), row.get("simulation_id"), row.get("candidate_spec_id"))
    if not expression and not alpha_id and not candidate_key:
        return None

    metrics = _metrics(row)
    wq_score, wq_score_source = _wq_score(metrics)
    sc_result = _first_text(row.get("sc_result"), _check_result(row, "SELF_CORRELATION"), _nested(row, ("precheck", "sc_result")))
    prod_result = _first_text(row.get("prod_corr_result"), _check_result(row, "PROD_CORRELATION"), _nested(row, ("precheck", "prod_corr_result")))
    sc_value = _first_float(
        row.get("sc_value"),
        row.get("self_correlation_value"),
        _check_value(row, "SELF_CORRELATION"),
        _nested(row, ("review_checks", "self_correlation", "value")),
        _nested(row, ("precheck", "sc_value")),
    )
    prod_value = _first_float(
        row.get("prod_corr_value"),
        row.get("prod_value"),
        _check_value(row, "PROD_CORRELATION"),
        _nested(row, ("review_checks", "prod_correlation", "value")),
        _nested(row, ("precheck", "prod_corr_value")),
    )
    failed_checks = _failed_check_names(row)
    api_check_status = _first_text(row.get("api_check_status"), _nested(row, ("precheck", "api_check_status")))
    platform_status = _first_text(row.get("platform_status"), row.get("status"), _nested(row, ("precheck", "platform_status")))
    final_status = _first_text(row.get("final_status"), platform_status)
    failure_kind = _failure_kind(
        row,
        api_check_status=api_check_status,
        platform_status=platform_status,
        final_status=final_status,
        sc_result=sc_result,
        prod_result=prod_result,
        failed_checks=failed_checks,
    )
    lifecycle = _lifecycle(
        row,
        source_type=source_type,
        api_check_status=api_check_status,
        platform_status=platform_status,
        final_status=final_status,
        failure_kind=failure_kind,
        failed_checks=failed_checks,
    )
    expression_hash = _hash(_safe_normalize(expression)) if expression else None
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": _record_id(source_file, row_index, expression or alpha_id or candidate_key or ""),
        "source_file": str(source_file),
        "source_type": source_type,
        "source_run_id": _source_run_id(source_file),
        "row_index": row_index,
        "lifecycle": lifecycle,
        "failure_kind": failure_kind or "none",
        "alpha_id": alpha_id,
        "candidate_key": candidate_key,
        "parent_alpha_id": _first_text(row.get("parent_alpha_id"), _nested(row, ("candidate_meta", "parent_alpha_id"))),
        "tag": _first_text(row.get("tag"), _nested(row, ("precheck", "tag"))),
        "source_family": _first_text(row.get("source_family"), row.get("domain"), _nested(row, ("candidate_meta", "source_family"))),
        "mutation_strategy": _first_text(row.get("mutation_strategy"), _nested(row, ("candidate_meta", "mutation_strategy"))),
        "expression": expression,
        "expression_normalized": _safe_normalize(expression) if expression else None,
        "expression_hash": expression_hash,
        "fields": sorted(_components(expression)["fields"]) if expression else [],
        "operators": sorted(_components(expression)["operators"]) if expression else [],
        "sharpe": metrics.get("sharpe"),
        "fitness": metrics.get("fitness"),
        "returns": metrics.get("returns"),
        "turnover": metrics.get("turnover"),
        "wq_score": wq_score,
        "wq_score_source": wq_score_source,
        "sc_result": sc_result.upper() if sc_result else None,
        "sc_value": sc_value,
        "prod_corr_result": prod_result.upper() if prod_result else None,
        "prod_corr_value": prod_value,
        "api_check_status": api_check_status,
        "platform_status": platform_status,
        "final_status": final_status,
        "failed_checks": failed_checks,
        "submit_eligible": bool(row.get("submit_eligible") or row.get("source_submit_eligible")),
        "submitted": bool(row.get("submitted") or row.get("source_submitted") or row.get("ok")),
        "settings": _settings_from_row(row),
        "created_at": _first_text(row.get("created_at"), _nested(row, ("precheck", "created_at")), _now()),
    }


def _merge_trajectory_events(
    events: list[dict[str, Any]],
    *,
    config: WQAlphaSearchMemoryConfig,
) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        key = _trajectory_key(event)
        buckets[key].append(event)

    trajectories = []
    for key, rows in buckets.items():
        rows = sorted(rows, key=lambda row: str(row.get("created_at") or ""))
        best_metric_row = max(
            rows,
            key=lambda row: (
                _safe_float(row.get("wq_score")) is not None,
                _safe_float(row.get("wq_score")) or -999.0,
                _safe_float(row.get("fitness")) or -999.0,
                _safe_float(row.get("returns")) or -999.0,
                -abs((_safe_float(row.get("turnover")) or 0.35) - 0.35),
            ),
        )
        best_lifecycle_row = max(rows, key=lambda row: _lifecycle_rank(row.get("lifecycle")))
        expression_row = next((row for row in rows if row.get("expression")), {})
        check_row = max(rows, key=lambda row: (_check_rank(row), str(row.get("created_at") or "")))
        fields = sorted({field for row in rows for field in (row.get("fields") or [])})
        operators = sorted({op for row in rows for op in (row.get("operators") or [])})
        failed_checks = sorted({check for row in rows for check in (row.get("failed_checks") or [])})
        family = _first_text(
            best_metric_row.get("source_family"),
            expression_row.get("source_family"),
            best_lifecycle_row.get("source_family"),
            _family_from_tag(_first_text(best_metric_row.get("tag"), expression_row.get("tag"))),
            "unknown",
        )
        trajectory = {
            "schema_version": SCHEMA_VERSION,
            "trajectory_id": key,
            "alpha_id": _first_text(*(row.get("alpha_id") for row in rows)),
            "candidate_key": _first_text(*(row.get("candidate_key") for row in rows)),
            "parent_alpha_id": _first_text(*(row.get("parent_alpha_id") for row in rows)),
            "tag": _first_text(best_metric_row.get("tag"), expression_row.get("tag"), best_lifecycle_row.get("tag")),
            "source_family": family,
            "mutation_strategy": _first_text(*(row.get("mutation_strategy") for row in rows)),
            "lifecycle": best_lifecycle_row.get("lifecycle"),
            "failure_kind": _best_failure_kind(rows),
            "expression": expression_row.get("expression"),
            "expression_normalized": expression_row.get("expression_normalized"),
            "expression_hash": expression_row.get("expression_hash"),
            "fields": fields,
            "operators": operators,
            "field_signature": "|".join(field for field in fields if field),
            "sharpe": best_metric_row.get("sharpe"),
            "fitness": best_metric_row.get("fitness"),
            "returns": best_metric_row.get("returns"),
            "turnover": best_metric_row.get("turnover"),
            "wq_score": best_metric_row.get("wq_score"),
            "wq_score_source": best_metric_row.get("wq_score_source"),
            "sc_result": _first_text(check_row.get("sc_result"), *(row.get("sc_result") for row in rows)),
            "sc_value": _first_float(check_row.get("sc_value"), *(row.get("sc_value") for row in rows)),
            "prod_corr_result": _first_text(check_row.get("prod_corr_result"), *(row.get("prod_corr_result") for row in rows)),
            "prod_corr_value": _first_float(check_row.get("prod_corr_value"), *(row.get("prod_corr_value") for row in rows)),
            "api_check_status": _best_api_check_status(rows),
            "platform_status": _first_text(best_lifecycle_row.get("platform_status"), *(row.get("platform_status") for row in rows)),
            "final_status": _first_text(best_lifecycle_row.get("final_status"), *(row.get("final_status") for row in rows)),
            "failed_checks": failed_checks,
            "submit_eligible": any(bool(row.get("submit_eligible")) for row in rows),
            "submitted": any(bool(row.get("submitted")) for row in rows),
            "settings": _first_dict(*(row.get("settings") for row in rows)),
            "event_count": len(rows),
            "source_files": sorted({str(row.get("source_file")) for row in rows if row.get("source_file")}),
            "event_types": dict(sorted(Counter(str(row.get("source_type") or "unknown") for row in rows).items())),
            "first_seen_at": rows[0].get("created_at"),
            "last_seen_at": rows[-1].get("created_at"),
            "created_at": _now(),
        }
        trajectory["is_high_score"] = (_safe_float(trajectory.get("wq_score")) or -999.0) >= config.min_high_score
        trajectory["platform_metric_gate"] = submit_threshold_checks({
            "sharpe": trajectory.get("sharpe"),
            "fitness": trajectory.get("fitness"),
            "turnover": trajectory.get("turnover"),
        })
        trajectory["correlation_risk"] = _correlation_risk(trajectory)
        trajectory["submit_priority"] = _submit_priority(trajectory)
        trajectory["is_near_sc_repair_parent"] = _is_near_sc_repair_parent(
            trajectory,
            config=config,
        )
        trajectories.append(trajectory)

    return sorted(
        trajectories,
        key=lambda row: (
            -_lifecycle_rank(row.get("lifecycle")),
            -(_safe_float(row.get("wq_score")) or -999.0),
            -(_safe_float(row.get("fitness")) or -999.0),
            str(row.get("alpha_id") or row.get("trajectory_id") or ""),
        ),
    )


def _family_scores(trajectories: list[dict[str, Any]], *, config: WQAlphaSearchMemoryConfig) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in trajectories:
        buckets[str(row.get("source_family") or row.get("mutation_strategy") or "unknown")].append(row)

    scores = []
    for family, rows in buckets.items():
        simulated_count = sum(1 for row in rows if _has_metric(row) or row.get("lifecycle") == "simulated")
        high_score_count = sum(1 for row in rows if (_safe_float(row.get("wq_score")) or -999.0) >= config.min_high_score)
        platform_eligible_count = sum(1 for row in rows if _metric_gate(row)["eligible"])
        precheck_pass_count = sum(1 for row in rows if row.get("lifecycle") == "precheck_pass")
        active_count = sum(1 for row in rows if row.get("lifecycle") == "active")
        check_readable_count = precheck_pass_count + active_count
        self_corr_count = sum(1 for row in rows if row.get("failure_kind") == "self_correlation_fail")
        sub_universe_count = sum(1 for row in rows if row.get("failure_kind") == "sub_universe_fail")
        near_count = sum(1 for row in rows if _is_near_sc_repair_parent(row, config=config))
        promotion_score = (
            active_count * 5.0
            + precheck_pass_count * 2.5
            + high_score_count * 0.4
            + platform_eligible_count * 0.2
            - self_corr_count * 0.7
            - sub_universe_count * 1.25
        )
        repair_priority_score = near_count * 2.0 + max(high_score_count - check_readable_count, 0) * 0.25
        repair_priority_score -= max(self_corr_count - near_count, 0) * 0.2
        repair_priority_score -= sub_universe_count * 1.0
        priority = promotion_score + repair_priority_score
        scores.append({
            "family": family,
            "alpha_count": len(rows),
            "simulated_count": simulated_count,
            "high_score_count": high_score_count,
            "platform_eligible_count": platform_eligible_count,
            "precheck_pass_count": precheck_pass_count,
            "check_readable_count": check_readable_count,
            "active_count": active_count,
            "self_corr_fail_count": self_corr_count,
            "sub_universe_fail_count": sub_universe_count,
            "near_sc_repair_parent_count": near_count,
            "avg_wq_score": _mean(row.get("wq_score") for row in rows),
            "avg_sharpe": _mean(row.get("sharpe") for row in rows),
            "avg_fitness": _mean(row.get("fitness") for row in rows),
            "check_readable_rate": _ratio(check_readable_count, simulated_count),
            "precheck_pass_rate": _ratio(precheck_pass_count, simulated_count),
            "active_rate": _ratio(active_count, simulated_count),
            "promotion_score": round(promotion_score, 6),
            "repair_priority_score": round(repair_priority_score, 6),
            "priority_score": round(priority, 6),
            "example_alpha_ids": [row.get("alpha_id") for row in rows if row.get("alpha_id")][:5],
        })
    return sorted(scores, key=lambda row: (-row["priority_score"], -row["active_count"], row["family"]))


def _build_skill_memory(
    trajectories: list[dict[str, Any]],
    *,
    family_scores: list[dict[str, Any]],
    repair_candidates: list[dict[str, Any]],
    top_submit_targets: list[dict[str, Any]],
    top_check_targets: list[dict[str, Any]],
    config: WQAlphaSearchMemoryConfig,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    near_parents = [row for row in trajectories if _is_near_sc_repair_parent(row, config=config)]
    active_high_score_examples = [
        row
        for row in trajectories
        if row.get("lifecycle") == "active" and (_safe_float(row.get("wq_score")) or -999.0) >= config.min_high_score
    ]
    rows.append({
        "schema_version": SCHEMA_VERSION,
        "memory_kind": "repair_skill",
        "skill_id": "near_sc_cutoff_settings_repair",
        "action": "For high-WQ-score alphas blocked by near-cutoff SELF_CORRELATION, freeze the expression and run a small neutralization/decay/truncation grid before spending budget on new expressions.",
        "selection_rule": {
            "min_parent_score": config.min_parent_score,
            "turnover_range": [config.min_turnover, config.max_turnover],
            "self_correlation_range": [config.sc_min, config.sc_max],
        },
        "source_papers": [
            "Alpha-GPT",
            "RD-Agent-Quant",
            "QuantaAlpha",
            "FactorMiner",
            "Hubble",
        ],
        "evidence": {
            "near_parent_count": len(near_parents),
            "candidate_count": len(repair_candidates),
            "active_high_score_examples": [_compact_example(row) for row in active_high_score_examples[:5]],
            "near_parent_examples": [_compact_example(row) for row in near_parents[:5]],
        },
        "created_at": _now(),
    })
    rows.append({
        "schema_version": SCHEMA_VERSION,
        "memory_kind": "submit_skill",
        "skill_id": "top5_high_score_low_corr_submit",
        "action": "Rank submission work by WQ score first, then correlation risk; fill five ACTIVE slots through check-readable candidates before submitting.",
        "selection_rule": {
            "target_submit_count": config.target_submit_count,
            "min_high_score": config.min_high_score,
            "preferred_corr_max": config.preferred_corr_max,
            "platform_metric_gate": "submit_threshold_checks",
        },
        "evidence": {
            "current_submit_target_count": len(top_submit_targets),
            "check_queue_target_count": len(top_check_targets),
            "submit_target_examples": top_submit_targets[:5],
            "check_target_examples": top_check_targets[:5],
        },
        "created_at": _now(),
    })

    for family in family_scores[:20]:
        if family["active_count"] == 0 and family["precheck_pass_count"] == 0 and family["near_sc_repair_parent_count"] == 0:
            continue
        rows.append({
            "schema_version": SCHEMA_VERSION,
            "memory_kind": "family_skill",
            "skill_id": f"family::{family['family']}",
            "family": family["family"],
            "action": _family_action(family),
            "evidence": {
                "alpha_count": family["alpha_count"],
                "active_count": family["active_count"],
                "precheck_pass_count": family["precheck_pass_count"],
                "high_score_count": family["high_score_count"],
                "platform_eligible_count": family["platform_eligible_count"],
                "near_sc_repair_parent_count": family["near_sc_repair_parent_count"],
                "example_alpha_ids": family["example_alpha_ids"],
            },
            "priority_score": family["priority_score"],
            "created_at": _now(),
        })
    return rows


def _summary(
    *,
    artifacts: list[Path],
    events: list[dict[str, Any]],
    trajectories: list[dict[str, Any]],
    family_scores: list[dict[str, Any]],
    skill_memory: list[dict[str, Any]],
    repair_candidates: list[dict[str, Any]],
    top_submit_targets: list[dict[str, Any]],
    top_check_targets: list[dict[str, Any]],
    config: WQAlphaSearchMemoryConfig,
) -> dict[str, Any]:
    simulated_count = sum(1 for row in trajectories if _has_metric(row) or row.get("lifecycle") == "simulated")
    high_score_count = sum(1 for row in trajectories if (_safe_float(row.get("wq_score")) or -999.0) >= config.min_high_score)
    platform_eligible_count = sum(1 for row in trajectories if _metric_gate(row)["eligible"])
    precheck_pass_count = sum(1 for row in trajectories if row.get("lifecycle") == "precheck_pass")
    active_count = sum(1 for row in trajectories if row.get("lifecycle") == "active")
    check_readable_count = precheck_pass_count + active_count
    self_corr_count = sum(1 for row in trajectories if row.get("failure_kind") == "self_correlation_fail")
    near_parent_count = sum(1 for row in trajectories if _is_near_sc_repair_parent(row, config=config))
    return {
        "config": {
            "target_submit_count": config.target_submit_count,
            "min_high_score": config.min_high_score,
            "min_parent_score": config.min_parent_score,
            "preferred_corr_max": config.preferred_corr_max,
            "turnover_range": [config.min_turnover, config.max_turnover],
            "self_correlation_range": [config.sc_min, config.sc_max],
            "max_parents": config.max_parents,
            "max_candidates_per_parent": config.max_candidates_per_parent,
        },
        "source_counts": {
            "artifacts": len(artifacts),
            "events": len(events),
            "trajectories": len(trajectories),
        },
        "target_submit_count": config.target_submit_count,
        "top_submit_target_count": len(top_submit_targets),
        "top_check_target_count": len(top_check_targets),
        "funnel": {
            "simulated_count": simulated_count,
            "high_score_count": high_score_count,
            "platform_eligible_count": platform_eligible_count,
            "precheck_pass_count": precheck_pass_count,
            "check_readable_count": check_readable_count,
            "active_count": active_count,
            "self_corr_fail_count": self_corr_count,
            "near_sc_repair_parent_count": near_parent_count,
            "rates": {
                "high_score_per_simulated": _ratio(high_score_count, simulated_count),
                "eligible_per_high_score": _ratio(platform_eligible_count, high_score_count),
                "check_readable_per_high_score": _ratio(check_readable_count, high_score_count),
                "precheck_pass_per_high_score": _ratio(precheck_pass_count, high_score_count),
                "active_per_high_score": _ratio(active_count, high_score_count),
                "active_per_simulated": _ratio(active_count, simulated_count),
            },
        },
        "lifecycle_counts": dict(sorted(Counter(str(row.get("lifecycle") or "unknown") for row in trajectories).items())),
        "failure_kind_counts": dict(sorted(Counter(str(row.get("failure_kind") or "none") for row in trajectories).items())),
        "top_families": family_scores[:20],
        "repair_queue_preview": repair_candidates[:20],
        "submit_target_preview": top_submit_targets[:20],
        "check_target_preview": top_check_targets[:20],
        "skill_preview": skill_memory[:10],
    }


def _top_submit_targets(
    trajectories: list[dict[str, Any]],
    *,
    config: WQAlphaSearchMemoryConfig,
    active_expression_hashes: set[str],
) -> list[dict[str, Any]]:
    rows = [
        _target_row(row, rank=index + 1, target_kind="submit")
        for index, row in enumerate(_ranked_submit_candidates(
            trajectories,
            config=config,
            require_check_readable=True,
            active_expression_hashes=active_expression_hashes,
        ))
    ]
    return rows[: max(config.target_submit_count, 0)]


def _top_check_targets(
    trajectories: list[dict[str, Any]],
    *,
    config: WQAlphaSearchMemoryConfig,
    active_expression_hashes: set[str],
) -> list[dict[str, Any]]:
    limit = max(config.target_submit_count * 10, config.target_submit_count)
    rows = [
        _target_row(row, rank=index + 1, target_kind="check")
        for index, row in enumerate(_ranked_submit_candidates(
            trajectories,
            config=config,
            require_check_readable=False,
            active_expression_hashes=active_expression_hashes,
        ))
    ]
    return rows[:limit]


def _ranked_submit_candidates(
    trajectories: list[dict[str, Any]],
    *,
    config: WQAlphaSearchMemoryConfig,
    require_check_readable: bool,
    active_expression_hashes: set[str],
) -> list[dict[str, Any]]:
    out = []
    for row in trajectories:
        if row.get("lifecycle") == "active":
            continue
        if not row.get("alpha_id") or not row.get("expression"):
            continue
        expression_hash = str(row.get("expression_hash") or "")
        if expression_hash and expression_hash in active_expression_hashes:
            continue
        if _safe_float(row.get("wq_score")) is None or (_safe_float(row.get("wq_score")) or -999.0) < config.min_high_score:
            continue
        if not _metric_gate(row)["eligible"]:
            continue
        if _is_hard_submission_block(row):
            continue
        check_readable = _is_check_readable(row)
        if require_check_readable and not check_readable:
            continue
        if not require_check_readable and check_readable:
            continue
        out.append(row)
    ranked = sorted(
        out,
        key=lambda row: (
            -(_safe_float(row.get("submit_priority")) or -999.0),
            _safe_float(row.get("correlation_risk"),) if _safe_float(row.get("correlation_risk")) is not None else 999.0,
            -(_safe_float(row.get("wq_score")) or -999.0),
            str(row.get("alpha_id") or ""),
        ),
    )
    deduped = []
    seen_expressions = set()
    for row in ranked:
        expression_key = row.get("expression_hash") or _hash(str(row.get("expression") or ""))
        if expression_key in seen_expressions:
            continue
        seen_expressions.add(expression_key)
        deduped.append(row)
    return deduped


def _target_row(row: dict[str, Any], *, rank: int, target_kind: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "rank": rank,
        "target_kind": target_kind,
        "alpha_id": row.get("alpha_id"),
        "domain": row.get("source_family"),
        "source_family": row.get("source_family"),
        "mutation_strategy": row.get("mutation_strategy"),
        "tag": row.get("tag"),
        "score": row.get("submit_priority"),
        "submit_priority": row.get("submit_priority"),
        "wq_score": row.get("wq_score"),
        "wq_score_source": row.get("wq_score_source"),
        "correlation_risk": row.get("correlation_risk"),
        "sharpe": row.get("sharpe"),
        "fitness": row.get("fitness"),
        "returns": row.get("returns"),
        "turnover": row.get("turnover"),
        "sc_result": row.get("sc_result"),
        "sc_value": row.get("sc_value"),
        "prod_corr_result": row.get("prod_corr_result"),
        "prod_corr_value": row.get("prod_corr_value"),
        "api_check_status": row.get("api_check_status"),
        "platform_metric_gate": row.get("platform_metric_gate") or _metric_gate(row),
        "lifecycle": row.get("lifecycle"),
        "expression_hash": row.get("expression_hash"),
        "field_signature": row.get("field_signature"),
        "expression": row.get("expression"),
        "simulation_settings": row.get("settings") or {},
        "source_files": row.get("source_files") or [],
        "created_at": _now(),
    }


def _is_check_readable(row: dict[str, Any]) -> bool:
    api_status = str(row.get("api_check_status") or "").lower()
    return row.get("lifecycle") == "precheck_pass" or api_status in PRECHECK_PASS_STATUSES


def _is_hard_submission_block(row: dict[str, Any]) -> bool:
    failure = str(row.get("failure_kind") or "").lower()
    if failure in {"self_correlation_fail", "prod_correlation_fail", "sub_universe_fail", "concentrated_weight"}:
        return True
    failed_checks = set(row.get("failed_checks") or [])
    if failed_checks & (SUB_UNIVERSE_CHECKS | {"SELF_CORRELATION", "PROD_CORRELATION", "CONCENTRATED_WEIGHT"}):
        return True
    if str(row.get("sc_result") or "").upper() == "FAIL":
        return True
    if str(row.get("prod_corr_result") or "").upper() == "FAIL":
        return True
    return False


def _is_near_sc_repair_parent(row: dict[str, Any], *, config: WQAlphaSearchMemoryConfig) -> bool:
    if not row.get("expression"):
        return False
    if row.get("lifecycle") in {"active", "precheck_pass"}:
        return False
    failed_checks = set(row.get("failed_checks") or [])
    if failed_checks & SUB_UNIVERSE_CHECKS or row.get("failure_kind") == "sub_universe_fail":
        return False
    wq_score = _safe_float(row.get("wq_score"))
    turnover = _safe_float(row.get("turnover"))
    sc_value = _safe_float(row.get("sc_value"))
    if wq_score is None or wq_score < config.min_parent_score:
        return False
    if turnover is not None and not (config.min_turnover <= turnover <= config.max_turnover):
        return False
    if sc_value is None or not (config.sc_min <= sc_value <= config.sc_max):
        return False
    api_status = str(row.get("api_check_status") or "").lower()
    sc_result = str(row.get("sc_result") or "").upper()
    return (
        api_status in SELF_CORRELATION_STATUSES
        or row.get("failure_kind") == "self_correlation_fail"
        or sc_result == "FAIL"
        or "SELF_CORRELATION" in failed_checks
    )


def _metrics(row: dict[str, Any]) -> dict[str, float | None]:
    return {
        key: _first_float(
            row.get(key),
            _nested(row, ("metrics", key)),
            _nested(row, ("candidate_metrics", key)),
            _nested(row, ("result", "is_metrics", key)),
            _nested(row, ("is_metrics", key)),
        )
        for key in ("sharpe", "fitness", "returns", "turnover")
    }


def _wq_score(metrics: dict[str, float | None]) -> tuple[float | None, str | None]:
    fitness = _safe_float(metrics.get("fitness"))
    if fitness is not None:
        return round(fitness, 6), "platform_fitness"
    sharpe = _safe_float(metrics.get("sharpe"))
    returns = _safe_float(metrics.get("returns"))
    turnover = _safe_float(metrics.get("turnover"))
    if sharpe is None or returns is None or turnover is None or turnover <= 0:
        return None, None
    effective_turnover = max(turnover, 0.125)
    return round(float(sharpe) * math.sqrt(abs(float(returns)) / effective_turnover), 6), "estimated_fitness"


def _metric_gate(row: dict[str, Any]) -> dict[str, Any]:
    return submit_threshold_checks({
        "sharpe": row.get("sharpe"),
        "fitness": row.get("fitness"),
        "turnover": row.get("turnover"),
    })


def _correlation_risk(row: dict[str, Any]) -> float:
    sc_result = str(row.get("sc_result") or "").upper()
    prod_result = str(row.get("prod_corr_result") or "").upper()
    sc_value = _safe_float(row.get("sc_value"))
    prod_value = _safe_float(row.get("prod_corr_value"))
    failure = str(row.get("failure_kind") or "").lower()

    if sc_result == "FAIL" or failure == "self_correlation_fail":
        base = max(sc_value or 1.0, 1.0)
    elif sc_result == "PASS":
        base = sc_value if sc_value is not None else 0.05
    elif sc_value is not None:
        base = sc_value
    else:
        base = 0.55

    if prod_result == "FAIL" or failure == "prod_correlation_fail":
        base += 1.0
    elif prod_value is not None:
        base += max(prod_value, 0.0) * 0.5
    return round(base, 6)


def _submit_priority(row: dict[str, Any]) -> float:
    wq_score = _safe_float(row.get("wq_score")) or 0.0
    returns = max(_safe_float(row.get("returns")) or 0.0, 0.0)
    turnover = _safe_float(row.get("turnover"))
    corr_risk = _correlation_risk(row)
    readiness_bonus = 2.0 if _is_check_readable(row) else 0.0
    active_penalty = 5.0 if row.get("lifecycle") == "active" else 0.0
    turnover_penalty = 0.0
    if turnover is None:
        turnover_penalty = 1.0
    elif turnover < 0.01:
        turnover_penalty = (0.01 - turnover) * 20.0
    elif turnover > 0.70:
        turnover_penalty = (turnover - 0.70) * 8.0
    elif turnover > 0.55:
        turnover_penalty = (turnover - 0.55) * 1.5
    metric_gate = _metric_gate(row)
    gate_penalty = 0.0 if metric_gate["eligible"] else 3.0
    score = wq_score * 10.0 + returns * 4.0 + readiness_bonus
    score -= corr_risk * 4.0 + turnover_penalty + active_penalty + gate_penalty
    return round(score, 6)


def _settings_from_row(row: dict[str, Any]) -> dict[str, Any]:
    settings = _first_dict(
        row.get("simulation_settings"),
        row.get("effective_settings"),
        row.get("settings"),
        _nested(row, ("candidate_meta", "simulation_settings")),
        _nested(row, ("candidate_meta", "settings")),
    )
    return {str(key): value for key, value in settings.items() if value is not None}


def _settings_from_record(row: dict[str, Any]) -> dict[str, Any]:
    settings = row.get("settings")
    if isinstance(settings, dict):
        return {str(key): value for key, value in settings.items() if value is not None}
    return {}


def _same_core_settings(settings: dict[str, Any], base_settings: dict[str, Any]) -> bool:
    keys = ("neutralization", "decay", "truncation")
    return all(settings.get(key) == base_settings.get(key) for key in keys)


def _failure_kind(
    row: dict[str, Any],
    *,
    api_check_status: str | None,
    platform_status: str | None,
    final_status: str | None,
    sc_result: str | None,
    prod_result: str | None,
    failed_checks: list[str],
) -> str | None:
    platform = str(platform_status or final_status or "").upper()
    api_status = str(api_check_status or "").lower()
    raw = str(row.get("failure_kind") or row.get("review_failure_kind") or "").lower()
    reason = str(row.get("presubmit_reject_reason") or row.get("triage_reason") or row.get("detail") or "").lower()
    if platform in SUCCESS_STATUSES or bool(row.get("ok")) and platform in SUCCESS_STATUSES:
        return "none"
    if api_status in SELF_CORRELATION_STATUSES or raw in {"self_correlation", "self_correlation_high", "self_correlation_fail"}:
        return "self_correlation_fail"
    if api_status == "prod_correlation_fail" or raw in {"prod_correlation", "prod_correlation_fail"}:
        return "prod_correlation_fail"
    if str(sc_result or "").upper() == "FAIL" or "self_correlation" in reason:
        return "self_correlation_fail"
    if str(prod_result or "").upper() == "FAIL" or "prod_correlation" in reason:
        return "prod_correlation_fail"
    check_set = set(failed_checks)
    if check_set & SUB_UNIVERSE_CHECKS:
        return "sub_universe_fail"
    if "CONCENTRATED_WEIGHT" in check_set:
        return "concentrated_weight"
    if "HIGH_TURNOVER" in check_set:
        return "high_turnover"
    if "LOW_TURNOVER" in check_set:
        return "low_turnover"
    if "LOW_SHARPE" in check_set:
        return "low_sharpe"
    if "LOW_FITNESS" in check_set:
        return "low_fitness"
    if "too_similar" in reason or "duplicate" in reason:
        return "high_similarity"
    if raw:
        return raw
    return None


def _lifecycle(
    row: dict[str, Any],
    *,
    source_type: str,
    api_check_status: str | None,
    platform_status: str | None,
    final_status: str | None,
    failure_kind: str | None,
    failed_checks: list[str],
) -> str:
    platform = str(platform_status or final_status or "").upper()
    final = str(final_status or "").upper()
    api_status = str(api_check_status or "").lower()
    if platform in SUCCESS_STATUSES or final in SUCCESS_STATUSES or bool(row.get("ok")) and final in SUCCESS_STATUSES:
        return "active"
    if api_status in PRECHECK_PASS_STATUSES or source_type in {"presubmit_ready", "presubmit_ready_sequential"}:
        return "precheck_pass"
    if api_status in SELF_CORRELATION_STATUSES or failure_kind == "self_correlation_fail":
        return "self_corr_fail"
    if final in {"PRECHECK_BLOCKED", "SC_FAIL", "SELF_CORRELATION_FAIL"}:
        return "blocked"
    if failed_checks or failure_kind not in {None, "none"}:
        return "platform_fail"
    if source_type in {"simulation_results", "review_queue"}:
        return "simulated"
    return "candidate"


def _best_api_check_status(rows: list[dict[str, Any]]) -> str | None:
    statuses = [str(row.get("api_check_status") or "") for row in rows if row.get("api_check_status")]
    if not statuses:
        return None
    rank = {
        "platform_active_check_readable": 5,
        "api_check_readable": 4,
        "self_correlation_fail": 3,
        "prod_correlation_fail": 2,
        "api_check_pending": 1,
    }
    return max(statuses, key=lambda item: rank.get(item.lower(), 0))


def _best_failure_kind(rows: list[dict[str, Any]]) -> str:
    if any(row.get("lifecycle") == "active" for row in rows):
        return "none"
    priority = [
        "self_correlation_fail",
        "prod_correlation_fail",
        "sub_universe_fail",
        "concentrated_weight",
        "high_turnover",
        "low_turnover",
        "low_sharpe",
        "low_fitness",
        "platform_fail",
        "high_similarity",
    ]
    counts = Counter(str(row.get("failure_kind") or "none") for row in rows)
    for item in priority:
        if counts.get(item):
            return item
    return "none"


def _lifecycle_rank(value: Any) -> int:
    return {
        "active": 6,
        "precheck_pass": 5,
        "self_corr_fail": 4,
        "blocked": 3,
        "platform_fail": 2,
        "simulated": 1,
        "candidate": 0,
    }.get(str(value or ""), 0)


def _check_rank(row: dict[str, Any]) -> int:
    if row.get("api_check_status") or row.get("sc_result") or row.get("sc_value") is not None:
        return 1
    return 0


def _failed_check_names(row: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in _check_items(row):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        result = str(item.get("result") or "").upper()
        if name and (result == "FAIL" or item in (row.get("failed_platform_checks") or [])):
            names.append(str(name).upper())
    for item in row.get("failed_checks") or row.get("blocking_failed_checks") or []:
        if isinstance(item, str):
            names.append(item.upper())
        elif isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]).upper())
    if str(row.get("sc_result") or "").upper() == "FAIL" or str(_nested(row, ("precheck", "sc_result")) or "").upper() == "FAIL":
        names.append("SELF_CORRELATION")
    if str(row.get("prod_corr_result") or "").upper() == "FAIL" or str(_nested(row, ("precheck", "prod_corr_result")) or "").upper() == "FAIL":
        names.append("PROD_CORRELATION")
    return sorted(set(names))


def _check_items(row: dict[str, Any]) -> list[Any]:
    items: list[Any] = []
    containers = [
        row,
        row.get("is") if isinstance(row.get("is"), dict) else {},
        row.get("raw_check") if isinstance(row.get("raw_check"), dict) else {},
        _nested(row, ("raw_check", "is")) or {},
        row.get("live_precheck") if isinstance(row.get("live_precheck"), dict) else {},
        _nested(row, ("live_precheck", "is")) or {},
        _nested(row, ("live_precheck", "raw_check")) or {},
        _nested(row, ("live_precheck", "raw_check", "is")) or {},
        row.get("precheck") if isinstance(row.get("precheck"), dict) else {},
        _nested(row, ("precheck", "is")) or {},
        _nested(row, ("precheck", "raw_check")) or {},
        _nested(row, ("precheck", "raw_check", "is")) or {},
    ]
    for container in containers:
        if isinstance(container, dict) and isinstance(container.get("checks"), list):
            items.extend(container.get("checks") or [])
    for value in (
        row.get("failed_platform_checks"),
        row.get("is_checks"),
        row.get("checks"),
        _nested(row, ("result", "is_metrics", "checks")),
        _nested(row, ("is_metrics", "checks")),
    ):
        if isinstance(value, list):
            items.extend(value)
    review_checks = row.get("review_checks")
    if isinstance(review_checks, dict):
        for value in review_checks.values():
            if isinstance(value, dict):
                items.append(value)
    return items


def _check_result(row: dict[str, Any], name: str) -> str | None:
    target = name.upper()
    for item in _check_items(row):
        if isinstance(item, dict) and str(item.get("name") or "").upper() == target:
            result = item.get("result")
            if result is not None:
                return str(result).upper()
    return None


def _check_value(row: dict[str, Any], name: str) -> float | None:
    target = name.upper()
    for item in _check_items(row):
        if isinstance(item, dict) and str(item.get("name") or "").upper() == target:
            value = _safe_float(item.get("value"))
            if value is not None:
                return value
    return None


def _safe_normalize(expression: str | None) -> str:
    if not expression:
        return ""
    try:
        return normalize_expression(expression)
    except Exception:
        return " ".join(str(expression).split())


def _trajectory_key(event: dict[str, Any]) -> str:
    alpha_id = event.get("alpha_id")
    if alpha_id:
        return f"alpha::{alpha_id}"
    candidate_key = event.get("candidate_key")
    if candidate_key:
        return f"candidate::{candidate_key}"
    expression_hash = event.get("expression_hash")
    if expression_hash:
        return f"expr::{expression_hash}"
    return f"event::{event.get('event_id')}"


def _record_id(source_file: Path, row_index: int, identity: str) -> str:
    return _hash(f"{source_file}|{row_index}|{identity}")[:24]


def _hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()


def _repair_tag(parent_tag: str, neutralization: str, decay: int, truncation: float) -> str:
    parent = "".join(ch.lower() if ch.isalnum() else "-" for ch in parent_tag)[:24].strip("-") or "parent"
    neut = str(neutralization).lower().replace("subindustry", "subind")
    trunc = int(round(float(truncation) * 1000))
    return f"{parent}-scfix-{neut}-d{int(decay)}-t{trunc:03d}"


def _repair_priority(parent: dict[str, Any], *, config: WQAlphaSearchMemoryConfig) -> float:
    wq_score = _safe_float(parent.get("wq_score")) or 0.0
    returns = max(_safe_float(parent.get("returns")) or 0.0, 0.0)
    sc_value = _safe_float(parent.get("sc_value")) or config.sc_max
    turnover = _safe_float(parent.get("turnover")) or config.max_turnover
    score = (wq_score - config.min_parent_score) * 3.0 + returns
    score += max(config.sc_max - sc_value, 0.0)
    score -= max(turnover - 0.5, 0.0)
    return round(score, 6)


def _family_action(row: dict[str, Any]) -> str:
    if row.get("active_count", 0) > 0:
        return "Prioritize as proven family; mine variants only through check-only gate before submit."
    if row.get("precheck_pass_count", 0) > 0:
        return "Promote cautiously; require explicit submit-one review."
    return "Use as repair queue input before allocating fresh generation budget."


def _compact_example(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "alpha_id": row.get("alpha_id"),
        "tag": row.get("tag"),
        "source_family": row.get("source_family"),
        "wq_score": row.get("wq_score"),
        "sharpe": row.get("sharpe"),
        "fitness": row.get("fitness"),
        "returns": row.get("returns"),
        "turnover": row.get("turnover"),
        "sc_value": row.get("sc_value"),
        "correlation_risk": row.get("correlation_risk"),
        "lifecycle": row.get("lifecycle"),
    }


def _has_metric(row: dict[str, Any]) -> bool:
    return any(row.get(key) is not None for key in ("sharpe", "fitness", "returns", "turnover"))


def _family_from_tag(tag: str | None) -> str | None:
    if not tag:
        return None
    text = str(tag)
    for sep in ("-", "_", ":"):
        if sep in text:
            return text.split(sep, 1)[0]
    return text[:32]


def _first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return dict(value)
    return {}


def _mean(values: Any) -> float | None:
    nums = [_safe_float(value) for value in values]
    clean = [value for value in nums if value is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 6)


def _dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen = set()
    for row in events:
        key = (
            row.get("source_file"),
            row.get("row_index"),
            row.get("alpha_id"),
            row.get("candidate_key"),
            row.get("expression_hash"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out
