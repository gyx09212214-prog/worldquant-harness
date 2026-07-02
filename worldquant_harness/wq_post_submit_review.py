"""Post-submit review artifacts for WorldQuant submission runs."""

from __future__ import annotations

import json
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .artifact_io import read_jsonl as _read_jsonl
from .artifact_io import utc_now as _now
from .artifact_io import write_json as _write_json
from .artifact_io import write_jsonl as _write_jsonl
from .expression_parser import normalize_expression
from .record_utils import first_float as _first_number
from .report_utils import ratio as _ratio
from .wq_expression_utils import expression_components as _components
from .wq_repair_screening import repair_candidate_concentration_risk
from .wq_research_profile import load_profile

SCHEMA_VERSION = 1
SUCCESS_STATUSES = {"ACTIVE", "SUBMITTED"}
SUBMIT_ARTIFACT_NAMES = {
    "submit_results.jsonl",
    "submit_existing_results.jsonl",
    "submitted_accumulator.jsonl",
}
CONTEXT_ARTIFACT_NAMES = {
    "simulation_results.jsonl",
    "review_queue.jsonl",
    "presubmit_ready_sequential.jsonl",
    "presubmit_rejected.jsonl",
    "candidate_pool.jsonl",
    "candidates.jsonl",
}
GROUP_OPS = {"group_rank", "group_neutralize", "group_zscore"}
PRICE_VOLUME_FIELDS = {"adv20", "close", "high", "low", "open", "volume", "vwap"}
GROUP_FIELDS = {"industry", "sector", "subindustry", "market"}
BLOCKER_STATUSES = {
    "SC_FAIL",
    "SELF_CORRELATION_FAIL",
    "PROD_CORR_FAIL",
    "PROD_CORRELATION_FAIL",
    "PLATFORM_CHECK_FAIL",
    "PRECHECK_BLOCKED",
}


@dataclass(frozen=True)
class WQPostSubmitReviewConfig:
    run_dirs: tuple[Path, ...]
    output_dir: Path
    baseline_dirs: tuple[Path, ...] = field(default_factory=tuple)
    baseline_roots: tuple[Path, ...] = field(default_factory=tuple)
    profile_dir: Path | None = None
    write_profile_candidate: bool = True
    window_days: int = 14


def build_post_submit_review(config: WQPostSubmitReviewConfig) -> dict[str, Any]:
    """Build local post-submit labels, lessons, and next-run constraints."""

    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    run_dirs = tuple(_existing_dirs(config.run_dirs))
    records = _records_for_dirs(run_dirs)
    alpha_labels = [_label_record(row) for row in records]
    experience_delta = [_experience_row(row) for row in alpha_labels]
    next_constraints = _next_run_constraints(alpha_labels)
    current = _batch_metrics(alpha_labels)

    baseline_dirs = tuple(_existing_dirs((*config.baseline_dirs, *_dirs_from_roots(config.baseline_roots))))
    baseline = _batch_metrics([_label_record(row) for row in _records_for_dirs(baseline_dirs)])
    profile_candidate = _profile_candidate(config, next_constraints=next_constraints, current=current)

    files = {
        "summary": str(output_dir / "summary.json"),
        "markdown": str(output_dir / "review.md"),
        "alpha_labels": str(output_dir / "alpha_labels.jsonl"),
        "experience_delta": str(output_dir / "experience_delta.jsonl"),
        "next_run_constraints": str(output_dir / "next_run_constraints.json"),
        "profile_candidate": str(output_dir / "profile_candidate.json"),
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "created_at": _now(),
        "mode": "wq_post_submit_review",
        "run_dirs": [str(path) for path in run_dirs],
        "baseline_dirs": [str(path) for path in baseline_dirs],
        "counts": {
            "submit_records": len(records),
            "alpha_labels": len(alpha_labels),
            "active": sum(1 for row in alpha_labels if row.get("is_active")),
        },
        "current": current,
        "baseline": baseline,
        "delta": _metrics_delta(current.get("metrics") or {}, baseline.get("metrics") or {}),
        "next_run_constraints": next_constraints,
        "profile_candidate": profile_candidate,
        "files": files,
    }

    _write_jsonl(Path(files["alpha_labels"]), alpha_labels)
    _write_jsonl(Path(files["experience_delta"]), experience_delta)
    _write_json(Path(files["next_run_constraints"]), next_constraints)
    _write_json(Path(files["profile_candidate"]), profile_candidate)
    _write_json(Path(files["summary"]), report)
    Path(files["markdown"]).write_text(render_post_submit_review_markdown(report, alpha_labels), encoding="utf-8")
    return report


def render_post_submit_review_markdown(report: dict[str, Any], labels: list[dict[str, Any]] | None = None) -> str:
    current = report.get("current") or {}
    metrics = current.get("metrics") or {}
    label_counts = current.get("label_counts") or {}
    labels = labels or []
    lines = [
        "# WQ Post Submit Review",
        "",
        f"- Created: `{report.get('created_at')}`",
        f"- Submit records: {report.get('counts', {}).get('submit_records', 0)}",
        f"- ACTIVE: {report.get('counts', {}).get('active', 0)}",
        "",
        "## Batch Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key in (
        "submit_attempt_count",
        "active_count",
        "active_per_submit_attempt",
        "mean_sharpe",
        "median_sharpe",
        "mean_fitness",
        "mean_turnover",
        "mean_subuniverse_margin",
        "group_ops_share",
        "returns_anchor_share",
        "field_signature_duplicate_ratio",
    ):
        lines.append(f"| `{key}` | {_fmt(metrics.get(key))} |")
    lines.extend(["", "## Labels", ""])
    if label_counts:
        for label, count in label_counts.items():
            lines.append(f"- `{label}`: {count}")
    else:
        lines.append("- none")
    lines.extend(["", "## Alpha Notes", ""])
    for row in labels[:30]:
        lines.append(
            f"- `{row.get('alpha_id') or ''}` `{row.get('label')}` "
            f"sharpe={_fmt(row.get('sharpe'))} fitness={_fmt(row.get('fitness'))} "
            f"turnover={_fmt(row.get('turnover'))} sc={_fmt(row.get('self_correlation'))} "
            f"sub_margin={_fmt(row.get('subuniverse_margin'))}: {row.get('lesson')}"
        )
    constraints = report.get("next_run_constraints") or {}
    lines.extend(["", "## Next Run Constraints", ""])
    for key in (
        "preferred_seed_alpha_ids",
        "threshold_only_alpha_ids",
        "blocked_alpha_ids",
        "avoid_field_signatures",
        "avoid_expression_patterns",
        "preferred_field_families",
        "required_repairs",
    ):
        value = constraints.get(key) or []
        lines.append(f"- `{key}`: {json.dumps(value[:12] if isinstance(value, list) else value, ensure_ascii=False)}")
    return "\n".join(lines).rstrip() + "\n"


def _records_for_dirs(run_dirs: Iterable[Path]) -> list[dict[str, Any]]:
    submit_rows = _submit_rows(run_dirs)
    context_rows = _context_rows(run_dirs)
    by_alpha: dict[str, dict[str, Any]] = {}
    by_expression: dict[str, dict[str, Any]] = {}
    for row in context_rows:
        alpha_id = str(row.get("alpha_id") or "")
        expression = str(row.get("expression") or "")
        if alpha_id and alpha_id not in by_alpha:
            by_alpha[alpha_id] = row
        if expression:
            by_expression.setdefault(_safe_normalize(expression), row)

    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in submit_rows:
        alpha_id = str(row.get("alpha_id") or "")
        expression = str(row.get("expression") or "")
        source = by_alpha.get(alpha_id) or (by_expression.get(_safe_normalize(expression)) if expression else {}) or {}
        merged = _merge_submit_with_context(row, source)
        key = (
            alpha_id,
            str(merged.get("final_status") or merged.get("platform_status") or merged.get("status") or ""),
            _safe_normalize(str(merged.get("expression") or "")),
        )
        if key not in deduped or _row_priority(merged) > _row_priority(deduped[key]):
            deduped[key] = merged
    return list(deduped.values())


def _submit_rows(run_dirs: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for root in run_dirs:
        for path in _artifact_paths(root, SUBMIT_ARTIFACT_NAMES):
            for row in _read_jsonl(path):
                if not row.get("alpha_id") and not row.get("expression"):
                    continue
                rows.append({**row, "source_file": str(path), "source_type": path.name.removesuffix(".jsonl")})
    return rows


def _context_rows(run_dirs: Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for root in run_dirs:
        for path in _artifact_paths(root, CONTEXT_ARTIFACT_NAMES):
            for row in _read_jsonl(path):
                if row.get("alpha_id") or row.get("expression"):
                    rows.append(row)
    return rows


def _merge_submit_with_context(row: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    merged = {**source, **row}
    metrics = dict(source.get("candidate_metrics") or {})
    metrics.update(row.get("candidate_metrics") or {})
    for key in ("sharpe", "fitness", "returns", "turnover", "max_active_similarity"):
        if metrics.get(key) is None:
            metrics[key] = _first_number(row.get(key), source.get(key))
    merged["candidate_metrics"] = metrics
    if not merged.get("expression"):
        merged["expression"] = source.get("expression")
    if not merged.get("tag"):
        merged["tag"] = source.get("tag")
    if not merged.get("source_family"):
        merged["source_family"] = source.get("source_family") or (source.get("candidate_meta") or {}).get("source_family")
    return merged


def _label_record(row: dict[str, Any]) -> dict[str, Any]:
    expression = str(row.get("expression") or "")
    metrics = row.get("candidate_metrics") or {}
    sharpe = _first_number(metrics.get("sharpe"), row.get("sharpe"))
    fitness = _first_number(metrics.get("fitness"), row.get("fitness"))
    returns = _first_number(metrics.get("returns"), row.get("returns"))
    turnover = _first_number(metrics.get("turnover"), row.get("turnover"))
    sc_value = _self_correlation_value(row)
    sub = _subuniverse_check(row)
    sub_value = _first_number((sub or {}).get("value"))
    sub_limit = _first_number((sub or {}).get("limit"))
    sub_margin = round(sub_value - sub_limit, 6) if sub_value is not None and sub_limit is not None else None
    components = _components(expression)
    operators = sorted(components.get("operators") or [])
    fields = sorted(components.get("fields") or [])
    group_ops = sorted(set(operators) & GROUP_OPS)
    final_status = _final_status(row)
    is_active = _is_active(row)
    sparse_risk = repair_candidate_concentration_risk(expression) if expression else None
    label = _label_for(
        is_active=is_active,
        final_status=final_status,
        sharpe=sharpe,
        fitness=fitness,
        turnover=turnover,
        sc_value=sc_value,
        sub_margin=sub_margin,
    )
    lesson = _lesson_for(label, row, group_ops=group_ops, sparse_risk=sparse_risk, sub_margin=sub_margin, sc_value=sc_value)
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "alpha_id": row.get("alpha_id"),
        "label": label,
        "is_active": is_active,
        "final_status": final_status,
        "tag": row.get("tag"),
        "source_family": row.get("source_family"),
        "expression": expression,
        "expression_normalized": _safe_normalize(expression),
        "field_signature": "|".join(fields),
        "fields": fields,
        "operators": operators,
        "group_ops": group_ops,
        "has_returns_anchor": "returns" in fields,
        "has_sparse_group_risk": bool(sparse_risk),
        "sparse_group_risk": sparse_risk,
        "sharpe": sharpe,
        "fitness": fitness,
        "returns": returns,
        "turnover": turnover,
        "self_correlation": sc_value,
        "subuniverse_value": sub_value,
        "subuniverse_limit": sub_limit,
        "subuniverse_margin": sub_margin,
        "failure_kind": row.get("failure_kind"),
        "detail": row.get("detail"),
        "source_file": row.get("source_file"),
        "lesson": lesson,
        "next_action": _next_action_for(label),
    }


def _label_for(
    *,
    is_active: bool,
    final_status: str,
    sharpe: float | None,
    fitness: float | None,
    turnover: float | None,
    sc_value: float | None,
    sub_margin: float | None,
) -> str:
    metric_pass = (sharpe or 0.0) >= 1.25 and (fitness or 0.0) >= 1.0
    turnover_ok = turnover is None or 0.01 <= turnover <= 0.70
    strong_turnover_ok = turnover is None or 0.05 <= turnover <= 0.25
    if is_active:
        if (
            (sharpe or 0.0) >= 1.60
            and (fitness or 0.0) >= 1.25
            and (sub_margin is not None and sub_margin >= 0.25)
            and strong_turnover_ok
            and (sc_value is not None and sc_value <= 0.67)
        ):
            return "strong_seed_active"
        if (sharpe is not None and sharpe < 1.45) or (sub_margin is not None and sub_margin < 0.20) or (
            sc_value is not None and sc_value >= 0.67
        ):
            return "threshold_repair_active"
        return "quality_active"
    if final_status in BLOCKER_STATUSES and metric_pass and turnover_ok:
        return "blocked_near_miss"
    return "do_not_seed"


def _lesson_for(
    label: str,
    row: dict[str, Any],
    *,
    group_ops: list[str],
    sparse_risk: dict[str, Any] | None,
    sub_margin: float | None,
    sc_value: float | None,
) -> str:
    if label == "strong_seed_active":
        return "Strong ACTIVE seed; may expand only with field/operator diversity and fresh self-correlation checks."
    if label == "quality_active":
        return "Usable ACTIVE reference; expand conservatively and preserve sub-universe margin."
    if label == "threshold_repair_active":
        reasons = []
        if sub_margin is not None and sub_margin < 0.20:
            reasons.append("thin sub-universe margin")
        if sc_value is not None and sc_value >= 0.67:
            reasons.append("near self-correlation limit")
        if (_first_number((row.get("candidate_metrics") or {}).get("sharpe"), row.get("sharpe")) or 0.0) < 1.45:
            reasons.append("low Sharpe cushion")
        return "Threshold ACTIVE; retain repair idea but do not use as dense seed" + (f" ({', '.join(reasons)})." if reasons else ".")
    if label == "blocked_near_miss":
        status = _final_status(row)
        if status in {"SC_FAIL", "SELF_CORRELATION_FAIL"}:
            return "Near-miss blocked by self-correlation; repair must change field or operator family, not only windows."
        if status in {"PLATFORM_CHECK_FAIL", "PRECHECK_BLOCKED"}:
            return "Near-miss blocked by platform checks; repair should improve sub-universe/weight distribution before resubmit."
        return "Near-miss blocked after submit; keep as repair source but avoid direct variants."
    if sparse_risk:
        return "Do not seed; sparse fields with expression-level group transforms create distribution risk."
    if group_ops:
        return "Do not seed; failed grouped template should not be copied."
    return "Do not seed; use as negative evidence only."


def _next_action_for(label: str) -> str:
    return {
        "strong_seed_active": "expand_with_diversity",
        "quality_active": "reference_conservatively",
        "threshold_repair_active": "record_repair_pattern_only",
        "blocked_near_miss": "repair_before_recheck",
        "do_not_seed": "negative_memory_only",
    }.get(label, "review")


def _experience_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "alpha_id": row.get("alpha_id"),
        "label": row.get("label"),
        "source_family": row.get("source_family"),
        "field_signature": row.get("field_signature"),
        "operators": row.get("operators") or [],
        "metrics": {
            "sharpe": row.get("sharpe"),
            "fitness": row.get("fitness"),
            "returns": row.get("returns"),
            "turnover": row.get("turnover"),
        },
        "checks": {
            "self_correlation": row.get("self_correlation"),
            "subuniverse_value": row.get("subuniverse_value"),
            "subuniverse_limit": row.get("subuniverse_limit"),
            "subuniverse_margin": row.get("subuniverse_margin"),
        },
        "lesson": row.get("lesson"),
        "next_action": row.get("next_action"),
        "expression": row.get("expression"),
    }


def _next_run_constraints(labels: list[dict[str, Any]]) -> dict[str, Any]:
    preferred = [str(row.get("alpha_id")) for row in labels if row.get("label") == "strong_seed_active" and row.get("alpha_id")]
    threshold = [str(row.get("alpha_id")) for row in labels if row.get("label") == "threshold_repair_active" and row.get("alpha_id")]
    blocked = [
        str(row.get("alpha_id"))
        for row in labels
        if row.get("label") in {"blocked_near_miss", "do_not_seed"} and row.get("alpha_id")
    ]
    avoid_signatures = sorted({
        str(row.get("field_signature"))
        for row in labels
        if row.get("field_signature") and row.get("label") in {"blocked_near_miss", "do_not_seed"}
    })
    patterns: list[str] = []
    if any(row.get("has_sparse_group_risk") for row in labels):
        patterns.append("avoid sparse fundamental/PCR legs with expression-level group_* transforms")
    if _share(labels, lambda row: row.get("has_returns_anchor")) >= 0.50:
        patterns.append("reduce returns main-anchor usage")
    if _share(labels, lambda row: bool(row.get("group_ops"))) >= 0.50:
        patterns.append("reduce direct group_rank/group_neutralize template reuse")
    required_repairs = []
    if any(row.get("final_status") in {"SC_FAIL", "SELF_CORRELATION_FAIL"} for row in labels):
        required_repairs.append("self-correlation failures must change field/operator family before recheck")
    if any((row.get("subuniverse_margin") is not None and row.get("subuniverse_margin") < 0.20) for row in labels):
        required_repairs.append("thin sub-universe margin candidates require fresh anchors or broader dispersion legs")
    return {
        "preferred_seed_alpha_ids": preferred,
        "threshold_only_alpha_ids": threshold,
        "blocked_alpha_ids": blocked,
        "avoid_field_signatures": avoid_signatures[:40],
        "avoid_expression_patterns": patterns,
        "preferred_field_families": _preferred_field_families(labels),
        "required_repairs": required_repairs,
        "generation_notes": _generation_notes(labels),
    }


def _preferred_field_families(labels: list[dict[str, Any]]) -> list[str]:
    counts: Counter[str] = Counter()
    for row in labels:
        if row.get("label") not in {"strong_seed_active", "quality_active"}:
            continue
        for field_name in row.get("fields") or []:
            if field_name in PRICE_VOLUME_FIELDS or field_name in GROUP_FIELDS or field_name == "returns":
                continue
            counts[str(field_name)] += 1
    return [field for field, _ in counts.most_common(20)]


def _generation_notes(labels: list[dict[str, Any]]) -> list[str]:
    notes = []
    if any(row.get("label") == "threshold_repair_active" for row in labels):
        notes.append("Use threshold_repair_active rows as repair-pattern memory, not dense generation seeds.")
    if any(row.get("label") == "strong_seed_active" for row in labels):
        notes.append("Strong seeds may be expanded only after checking active similarity and current self-correlation.")
    if any(row.get("has_sparse_group_risk") for row in labels):
        notes.append("Prefer plain rank/ts_rank plus platform neutralization for sparse multi-leg repairs.")
    return notes


def _batch_metrics(labels: list[dict[str, Any]]) -> dict[str, Any]:
    label_counts = Counter(str(row.get("label") or "unknown") for row in labels)
    active = [row for row in labels if row.get("is_active")]
    signatures = [str(row.get("field_signature") or "") for row in labels if row.get("field_signature")]
    duplicate_signatures = len(signatures) - len(set(signatures))
    metrics = {
        "submit_attempt_count": len(labels),
        "active_count": len(active),
        "active_per_submit_attempt": _ratio(len(active), len(labels)),
        "mean_sharpe": _mean(row.get("sharpe") for row in active),
        "median_sharpe": _median(row.get("sharpe") for row in active),
        "mean_fitness": _mean(row.get("fitness") for row in active),
        "mean_turnover": _mean(row.get("turnover") for row in active),
        "mean_subuniverse_margin": _mean(row.get("subuniverse_margin") for row in active),
        "mean_self_correlation": _mean(row.get("self_correlation") for row in active),
        "group_ops_share": _share(labels, lambda row: bool(row.get("group_ops"))),
        "returns_anchor_share": _share(labels, lambda row: row.get("has_returns_anchor")),
        "sparse_group_risk_share": _share(labels, lambda row: row.get("has_sparse_group_risk")),
        "field_signature_duplicate_ratio": _ratio(duplicate_signatures, len(signatures)),
    }
    return {
        "metrics": metrics,
        "label_counts": dict(sorted(label_counts.items())),
        "active_alpha_ids": [row.get("alpha_id") for row in active if row.get("alpha_id")],
    }


def _profile_candidate(config: WQPostSubmitReviewConfig, *, next_constraints: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    profile = {}
    if config.profile_dir:
        try:
            profile = load_profile(profile_dir=config.profile_dir)
        except Exception:
            profile = {}
    candidate = dict(profile) if profile else {
        "schema_version": 1,
        "profile_name": "default",
        "profile_version": 0,
        "strategy_notes": [],
        "priority_biases": [],
        "field_signature_policy": {"blacklist": []},
        "memory_policy": {},
    }
    candidate["updated_at"] = _now()
    candidate["post_submit_review"] = {
        "current_metrics": current.get("metrics") or {},
        "next_run_constraints": next_constraints,
    }
    notes = list(candidate.get("strategy_notes") or [])
    for note in next_constraints.get("generation_notes") or []:
        if note not in notes:
            notes.append(note)
    candidate["strategy_notes"] = notes
    signature_policy = dict(candidate.get("field_signature_policy") or {})
    blacklist = list(signature_policy.get("blacklist") or [])
    for signature in next_constraints.get("avoid_field_signatures") or []:
        if signature not in blacklist:
            blacklist.append(signature)
    signature_policy["blacklist"] = blacklist[:100]
    candidate["field_signature_policy"] = signature_policy
    return {
        "ok": True,
        "skipped": not config.write_profile_candidate,
        "profile": candidate if config.write_profile_candidate else {},
    }


def _metrics_delta(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in current.items():
        left = _first_number(value)
        right = _first_number(baseline.get(key))
        if left is not None and right is not None:
            out[key] = round(left - right, 6)
    return out


def _artifact_paths(root: Path, names: set[str]) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file():
        return [root] if root.name in names else []
    return [path for path in sorted(root.rglob("*.jsonl")) if path.name in names]


def _dirs_from_roots(roots: Iterable[Path]) -> list[Path]:
    dirs: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if root.is_dir():
            dirs.extend(path for path in root.iterdir() if path.is_dir())
    return dirs


def _existing_dirs(paths: Iterable[Path]) -> list[Path]:
    return [Path(path) for path in paths if Path(path).exists()]


def _final_status(row: dict[str, Any]) -> str:
    status = row.get("final_status") or row.get("platform_status") or row.get("status")
    if status:
        return str(status).upper()
    if bool(row.get("ok")):
        return "ACTIVE"
    return ""


def _is_active(row: dict[str, Any]) -> bool:
    return bool(row.get("ok")) and _final_status(row) in SUCCESS_STATUSES or _final_status(row) in SUCCESS_STATUSES


def _self_correlation_value(row: dict[str, Any]) -> float | None:
    direct = _first_number(row.get("sc_value"), row.get("self_correlation"), row.get("self_correlation_value"))
    if direct is not None:
        return direct
    review = row.get("review_checks") if isinstance(row.get("review_checks"), dict) else {}
    value = _first_number((review.get("self_correlation") or {}).get("value") if isinstance(review.get("self_correlation"), dict) else None)
    if value is not None:
        return value
    live = row.get("live_precheck") if isinstance(row.get("live_precheck"), dict) else {}
    live_review = live.get("review_checks") if isinstance(live.get("review_checks"), dict) else {}
    value = _first_number((live_review.get("self_correlation") or {}).get("value") if isinstance(live_review.get("self_correlation"), dict) else None)
    if value is not None:
        return value
    for item in _check_items(row):
        if str(item.get("name") or "").upper() == "SELF_CORRELATION":
            return _first_number(item.get("value"))
    return None


def _subuniverse_check(row: dict[str, Any]) -> dict[str, Any] | None:
    for item in _check_items(row):
        if str(item.get("name") or "").upper() == "LOW_SUB_UNIVERSE_SHARPE":
            return item
    return None


def _check_items(row: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for container in (
        row,
        row.get("raw_check") if isinstance(row.get("raw_check"), dict) else {},
        (row.get("raw_check") or {}).get("is") if isinstance(row.get("raw_check"), dict) else {},
        row.get("is") if isinstance(row.get("is"), dict) else {},
        row.get("live_precheck") if isinstance(row.get("live_precheck"), dict) else {},
        (row.get("live_precheck") or {}).get("is") if isinstance(row.get("live_precheck"), dict) else {},
    ):
        value = container.get("checks") if isinstance(container, dict) else None
        if isinstance(value, list):
            checks.extend(item for item in value if isinstance(item, dict))
    for item in row.get("failed_platform_checks") or []:
        if isinstance(item, dict):
            checks.append(item)
    return checks


def _safe_normalize(expression: str) -> str:
    try:
        return normalize_expression(expression or "")
    except Exception:
        return "".join(str(expression or "").split())


def _row_priority(row: dict[str, Any]) -> int:
    if _is_active(row):
        return 3
    if row.get("final_status"):
        return 2
    return 1


def _mean(values: Iterable[Any]) -> float | None:
    nums = [_first_number(value) for value in values]
    nums = [value for value in nums if value is not None]
    return round(statistics.mean(nums), 6) if nums else None


def _median(values: Iterable[Any]) -> float | None:
    nums = [_first_number(value) for value in values]
    nums = [value for value in nums if value is not None]
    return round(statistics.median(nums), 6) if nums else None


def _share(rows: list[dict[str, Any]], predicate: Any) -> float | None:
    if not rows:
        return None
    return _ratio(sum(1 for row in rows if predicate(row)), len(rows))


def _fmt(value: Any) -> str:
    number = _first_number(value)
    if number is None:
        return ""
    return f"{number:.4f}".rstrip("0").rstrip(".")
