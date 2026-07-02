"""Policy-aware deterministic repair planning for WQ presubmit misses.

The planner consumes reviewed presubmit rows and emits local-only repair
candidates. It is intentionally conservative: self-correlation repairs change
field families, while concentration repairs smooth and diversify weights.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifact_io import read_jsonish_rows_many as _read_jsonish_rows_many
from .artifact_io import write_json as artifact_write_json
from .artifact_io import write_jsonl as artifact_write_jsonl
from .artifact_io import write_text as artifact_write_text
from .expression_parser import normalize_expression
from .record_utils import safe_float as _safe_float
from .report_utils import markdown_cell as _md
from .wq_agent_records import clean_simulation_settings as _clean_simulation_settings
from .wq_expression_utils import strip_outer_rank as _strip_outer_rank
from .wq_forum_submission_optimizer import annotate_candidate_with_policy, load_submission_policy
from .wq_repair_records import dedupe_repair_candidates as _dedupe_candidates
from .wq_repair_records import is_locally_valid_expression as _locally_valid
from .wq_repair_records import make_repair_candidate as _candidate
from .wq_repair_records import repair_candidate_dedupe_key as _candidate_dedupe_key
from .wq_repair_review import repair_row_fields as _fields
from .wq_repair_scoring import repair_candidate_sort_key as _candidate_sort_key
from .wq_repair_screening import repair_candidate_concentration_risk
from .wq_repair_templates_concentration import concentration_repairs as _concentration_repairs
from .wq_repair_templates_metric_threshold import metric_threshold_repairs as _metric_threshold_repairs
from .wq_repair_templates_self_corr import self_corr_repairs as _self_corr_repairs

BLOCKED_SETTINGS_ONLY_REPAIR_FAMILIES = {
    "repair_metric_threshold_settings",
    "repair_metric_threshold_smoothing",
    "repair_concentration_generic",
}
BLOCKED_SETTINGS_ONLY_REPAIR_STRATEGIES = {
    "metric_near_miss_decay_truncation_retest",
    "metric_near_miss_max_position_retest",
    "metric_near_miss_smooth_group_neutralize",
    "smooth_group_neutralize",
}
@dataclass(frozen=True)
class PolicyRepairPlannerConfig:
    review_paths: tuple[Path, ...] = field(default_factory=tuple)
    output_dir: Path | None = None
    submission_policy_file: Path | None = None
    obsidian_output: Path | None = None
    max_candidates: int = 40
    max_repairs_per_row: int = 4
    title: str = "worldquant-harness presubmit repair plan"


def build_policy_repair_plan(config: PolicyRepairPlannerConfig) -> dict[str, Any]:
    review_rows = _load_rows(config.review_paths)
    policy = load_submission_policy(config.submission_policy_file)
    repair_records = build_policy_repair_records(
        review_rows,
        submission_policy=policy,
        max_repairs_per_row=config.max_repairs_per_row,
    )
    candidates = build_policy_repair_candidates(repair_records, submission_policy=policy)
    candidates = sorted(candidates, key=_candidate_sort_key)[: max(0, config.max_candidates)]
    for index, row in enumerate(candidates, start=1):
        row["candidate_rank"] = index
    guard_summary = _repair_guard_summary(repair_records)
    markdown = render_policy_repair_markdown(
        review_rows=review_rows,
        repair_records=repair_records,
        candidates=candidates,
        config=config,
    )
    plan = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "review_rows": len(review_rows),
            "repair_records": len(repair_records),
            "candidates": len(candidates),
            "review_buckets": dict(sorted(Counter(row.get("triage_bucket") for row in review_rows).items())),
            "repair_kinds": dict(sorted(Counter(row.get("failure_kind") for row in repair_records).items())),
            "candidate_policy_actions": dict(sorted(Counter(
                row.get("forum_policy_action") for row in candidates if row.get("forum_policy_action")
            ).items())),
            "candidate_guard_blocks": guard_summary["total"],
            "candidate_guard_block_reasons": guard_summary["reasons"],
        },
        "repair_records": repair_records,
        "candidates": candidates,
        "markdown": markdown,
    }
    if config.output_dir or config.obsidian_output:
        write_policy_repair_artifacts(plan, output_dir=config.output_dir, obsidian_output=config.obsidian_output)
    return plan


def build_policy_repair_records(
    review_rows: list[dict],
    *,
    submission_policy: dict[str, Any] | None = None,
    max_repairs_per_row: int = 4,
) -> list[dict]:
    records = []
    for row in review_rows:
        if not _is_repairable_review_row(row):
            continue
        candidates = _repair_candidates_for_row(row)
        candidates = _dedupe_candidates(candidates)[: max(0, max_repairs_per_row)]
        if not candidates:
            continue
        source = str(row.get("expression") or row.get("source_expression") or "")
        candidate_records = [
            annotate_candidate_with_policy(item, submission_policy) if submission_policy else item
            for item in candidates
        ]
        guard_rejected = _guard_rejected_candidates(candidate_records)
        record = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "alpha_id": row.get("alpha_id"),
            "tag": row.get("tag"),
            "source_expression": source,
            "source_fields": sorted(_fields(row)),
            "failure_kind": _failure_kind(row),
            "triage_reason": row.get("triage_reason"),
            "sc_value": _safe_float(row.get("sc_value")),
            "sharpe": _safe_float(row.get("sharpe")),
            "fitness": _safe_float(row.get("fitness")),
            "turnover": _safe_float(row.get("turnover")),
            "repair_objective": _repair_objective(row),
            "candidate_expressions": [item["expression"] for item in candidates],
            "candidate_records": candidate_records,
            "guard_rejected_candidates": guard_rejected,
            "repair_guard_rejected": len(guard_rejected),
            "risk_notes": _risk_notes(row),
            "source_row": row,
        }
        records.append(record)
    return records


def build_policy_repair_candidates(
    repair_records: list[dict],
    *,
    submission_policy: dict[str, Any] | None = None,
) -> list[dict]:
    out = []
    seen = set()
    blocked_no_settings_keys = {
        normalize_expression(str(record.get("source_expression") or ""))
        for record in repair_records
        if str(record.get("source_expression") or "").strip()
    }
    for record in repair_records:
        for item in record.get("candidate_records") or []:
            expression = str(item.get("expression") or "")
            if not expression:
                continue
            key = _candidate_dedupe_key(item)
            settings = _clean_simulation_settings(item.get("simulation_settings") or item.get("settings_override"))
            if not settings and normalize_expression(expression) in blocked_no_settings_keys:
                continue
            if not _locally_valid(expression):
                continue
            if _is_settings_only_repair(item):
                continue
            if repair_candidate_concentration_risk(expression):
                continue
            if key in seen:
                continue
            seen.add(key)
            row = annotate_candidate_with_policy(item, submission_policy) if submission_policy else dict(item)
            if row.get("forum_policy_action") == "block":
                continue
            out.append({
                **row,
                "source": "wq_policy_repair_planner",
                "source_alpha_id": record.get("alpha_id"),
                "repair_failure_kind": record.get("failure_kind"),
                "repair_source_tag": record.get("tag"),
                "repair_objective": record.get("repair_objective"),
            })
    return out


def write_policy_repair_artifacts(
    plan: dict[str, Any],
    *,
    output_dir: Path | None = None,
    obsidian_output: Path | None = None,
) -> dict[str, str]:
    files: dict[str, str] = {}
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        summary_path = output_dir / "summary.json"
        repair_records_path = output_dir / "repair_records.jsonl"
        candidates_path = output_dir / "repair_candidates.jsonl"
        markdown_path = output_dir / "repair_plan.md"
        artifact_write_json(summary_path, _summary(plan))
        artifact_write_jsonl(repair_records_path, plan["repair_records"])
        artifact_write_jsonl(candidates_path, plan["candidates"])
        artifact_write_text(markdown_path, plan["markdown"])
        files["summary"] = str(summary_path)
        files["repair_records"] = str(repair_records_path)
        files["candidates"] = str(candidates_path)
        files["markdown"] = str(markdown_path)
    if obsidian_output:
        obsidian_output.parent.mkdir(parents=True, exist_ok=True)
        artifact_write_text(obsidian_output, plan["markdown"])
        files["obsidian"] = str(obsidian_output)
    plan.setdefault("files", {}).update(files)
    return files


def render_policy_repair_markdown(
    *,
    review_rows: list[dict],
    repair_records: list[dict],
    candidates: list[dict],
    config: PolicyRepairPlannerConfig,
) -> str:
    lines = [
        "---",
        "tags:",
        "  - worldquant_harness",
        "  - worldquant",
        "  - presubmit-repair",
        f"generated_at: {datetime.now(timezone.utc).isoformat()}",
        "---",
        "",
        f"# {config.title}",
        "",
        "## Summary",
        "",
        f"- Reviewed rows: {len(review_rows)}",
        f"- Repair records: {len(repair_records)}",
        f"- Candidate expressions: {len(candidates)}",
        "",
        "## Repair Candidates",
        "",
        "| Rank | Source alpha | Kind | Score | Settings | Expression |",
        "|---:|---|---|---:|---|---|",
    ]
    for row in candidates[:20]:
        lines.append(
            f"| {row.get('candidate_rank', '')} | {_md(row.get('source_alpha_id'))} | "
            f"{_md(row.get('repair_failure_kind'))} | {row.get('repair_priority_score', '')} | "
            f"{_md(row.get('settings_hint'))} | "
            f"`{_md(row.get('expression'))}` |"
        )
    lines.extend([
        "",
        "## Rules Applied",
        "",
        "- Self-correlation repairs replace at least one crowded field family instead of only changing windows.",
        "- Concentrated-weight repairs now block sparse-field/group stacks before simulation.",
        "- Forum-direct templates remain blocked by submission policy unless an orthogonal overlay is present.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def _guard_rejected_candidates(candidates: list[dict]) -> list[dict]:
    rejected = []
    for row in candidates:
        expression = str(row.get("expression") or "")
        risk = repair_candidate_concentration_risk(expression)
        if not risk:
            continue
        rejected.append({
            **row,
            "repair_guard_action": "block",
            "repair_guard_reason": "concentration_sparse_group_risk",
            "concentration_risk": risk,
        })
    return rejected


def _repair_guard_summary(repair_records: list[dict]) -> dict[str, Any]:
    reasons: Counter[str] = Counter()
    total = 0
    for record in repair_records:
        for row in record.get("guard_rejected_candidates") or []:
            total += 1
            reasons.update((row.get("concentration_risk") or {}).get("reasons") or [])
    return {
        "total": total,
        "reasons": dict(sorted(reasons.items())),
    }


def _is_settings_only_repair(row: dict) -> bool:
    family = str(row.get("source_family") or "")
    strategy = str(row.get("mutation_strategy") or "")
    tag = str(row.get("tag") or "").lower()
    if family in BLOCKED_SETTINGS_ONLY_REPAIR_FAMILIES:
        return True
    if strategy in BLOCKED_SETTINGS_ONLY_REPAIR_STRATEGIES:
        return True
    return "metric-retest" in tag or "metric-smooth" in tag or tag.endswith("smooth-industry")


def _repair_candidates_for_row(row: dict) -> list[dict]:
    fields = _fields(row)
    expression = str(row.get("expression") or row.get("source_expression") or "")
    base_tag = str(row.get("tag") or row.get("alpha_id") or "repair")
    parent = [row.get("alpha_id")] if row.get("alpha_id") else []
    failure = _failure_kind(row)
    out: list[dict] = []
    if failure == "concentrated_weight":
        out.extend(_concentration_repairs(base_tag, parent, source_expression=expression, source_row=row))
    if failure == "self_correlation_fail":
        out.extend(_self_corr_repairs(fields, base_tag, parent))
    if failure == "metric_threshold_near_miss":
        out.extend(_metric_threshold_repairs(fields, base_tag, parent, source_expression=expression, source_row=row))
    if not out and expression:
        out.extend(_generic_repairs(expression, base_tag, parent, failure))
    return out








def _generic_repairs(expression: str, tag: str, parent: list[Any], failure: str) -> list[dict]:
    base = _strip_outer_rank(expression)
    if failure == "concentrated_weight":
        return [_candidate(
            f"rank(ts_decay_linear(group_neutralize({base}, industry), 5))",
            tag=f"repair-{tag}-smooth-industry",
            family="repair_concentration_generic",
            strategy="smooth_group_neutralize",
            parent_alpha_ids=parent,
            rationale="Generic concentration repair: smooth and group neutralize the original signal.",
        )]
    return [_candidate(
        "rank(0.42 * ts_rank(forward_sales_to_price, 120) + 0.24 * ts_rank(snt1_d1_netearningsrevision, 90) + 0.18 * rank(ts_corr(vwap, volume, 100)) + 0.16 * rank(-1 * ts_rank(pcr_oi_60, 90)))",
        tag=f"repair-{tag}-generic-forward-revision-pcr",
        family="repair_self_corr_generic_orthogonal",
        strategy="field_family_replacement",
        parent_alpha_ids=parent,
        rationale="Generic self-correlation repair using forward value, revision, price-volume dispersion, and low-weight option flow.",
    )]


def _is_repairable_review_row(row: dict) -> bool:
    bucket = str(row.get("triage_bucket") or "")
    if bucket not in {"near_miss_repair", "hard_fail"}:
        return False
    failure = _failure_kind(row)
    if failure == "concentrated_weight":
        return _is_concentration_metric_repairable(row)
    if failure == "metric_threshold_near_miss":
        return _is_metric_threshold_repairable(row)
    if failure == "self_correlation_fail":
        sharpe = _safe_float(row.get("sharpe")) or 0.0
        fitness = _safe_float(row.get("fitness")) or 0.0
        sc_value = _safe_float(row.get("sc_value")) or 1.0
        return sharpe >= 1.25 and fitness >= 1.0 and sc_value <= 0.93
    return False




def _is_concentration_metric_repairable(row: dict) -> bool:
    sharpe = _safe_float(row.get("sharpe")) or 0.0
    fitness = _safe_float(row.get("fitness")) or 0.0
    turnover = _safe_float(row.get("turnover"))
    turnover_ok = turnover is None or 0.01 <= turnover <= 0.7
    return turnover_ok and sharpe >= 1.25 and fitness >= 0.85


def _is_metric_threshold_repairable(row: dict) -> bool:
    sharpe = _safe_float(row.get("sharpe")) or 0.0
    fitness = _safe_float(row.get("fitness")) or 0.0
    turnover = _safe_float(row.get("turnover"))
    turnover_ok = turnover is None or 0.01 <= turnover <= 0.7
    return turnover_ok and sharpe >= 1.15 and fitness >= 0.85


def _failure_kind(row: dict) -> str:
    failed_names = {str(check.get("name") or "").upper() for check in row.get("failed_platform_checks") or []}
    for check in row.get("failed_platform_checks") or []:
        if str(check.get("name") or "").upper() == "CONCENTRATED_WEIGHT":
            return "concentrated_weight"
    if failed_names <= {"LOW_SHARPE", "LOW_FITNESS"} and failed_names:
        return "metric_threshold_near_miss"
    reason = str(row.get("triage_reason") or row.get("api_check_status") or "").lower()
    if "self-correlation" in reason or "self_correlation" in reason:
        return "self_correlation_fail"
    if "platform check" in reason:
        return "platform_check_fail"
    return str(row.get("failure_kind") or "unknown")


def _repair_objective(row: dict) -> str:
    failure = _failure_kind(row)
    if failure == "concentrated_weight":
        return "Reduce concentrated weights by smoothing, diversifying legs, and group-ranking components."
    if failure == "self_correlation_fail":
        return "Reduce self-correlation by replacing crowded field families rather than only changing windows."
    if failure == "metric_threshold_near_miss":
        return "Lift near-threshold Sharpe/Fitness with slower windows, smoother transforms, and settings retests."
    return "Repair presubmit failure with deterministic field-family changes."


def _risk_notes(row: dict) -> list[str]:
    notes = []
    failure = _failure_kind(row)
    if failure == "self_correlation_fail":
        notes.append("Do not reuse IV90/returns/price-volume as the only overlay.")
    if failure == "concentrated_weight":
        notes.append("Consider lower truncation, e.g. 0.05, when running this repair batch.")
    if failure == "metric_threshold_near_miss":
        notes.append("Prefer slow-window and low-truncation retests before changing the core field family.")
    return notes




def _load_rows(paths: tuple[Path, ...]) -> list[dict]:
    return _read_jsonish_rows_many(paths, collection_keys=("rows", "records", "review", "ready", "active", "results", "alphas"))


def _summary(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: plan[key] for key in ("ok", "generated_at", "summary", "files") if key in plan}
