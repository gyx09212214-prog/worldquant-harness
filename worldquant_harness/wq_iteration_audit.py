"""Detailed iteration audit artifacts for WQ workflow runs."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .artifact_io import read_json as _read_json
from .artifact_io import read_jsonl as _read_jsonl
from .artifact_io import utc_now as _now
from .artifact_io import write_json as _write_json
from .artifact_io import write_jsonl as _write_jsonl
from .artifact_io import write_text as _write_text
from .record_utils import dedupe_rows_by_key as _dedupe_rows_by_key
from .record_utils import nested as _nested
from .wq_efficiency import annotate_candidate_identity, field_signature
from .wq_expression_utils import expression_components
from .wq_failure_taxonomy import audit_root_cause, failed_check_names, next_action_for_root_cause

SCHEMA_VERSION = 1
AUDIT_FILE = "iteration_audit.jsonl"
SUMMARY_FILE = "iteration_audit_summary.json"
MARKDOWN_FILE = "iteration_audit.md"
CANDIDATE_SKIPPED_FILE = "candidate_skipped.jsonl"

STAGE_FILES = (
    ("candidate_kept", "candidate_pool.jsonl"),
    ("candidate_skipped", CANDIDATE_SKIPPED_FILE),
    ("simulation", "simulation_results.jsonl"),
    ("review", "review_queue.jsonl"),
    ("repair_plan", "repair_queue.jsonl"),
    ("presubmit_ready", "presubmit_ready_sequential.jsonl"),
    ("presubmit_rejected", "presubmit_rejected.jsonl"),
    ("submit_result", "submit_results.jsonl"),
    ("submitted_success", "submitted_accumulator.jsonl"),
)


def build_iteration_audit(
    output_dir: Path | str,
    *,
    mode: str = "",
    include_expressions: bool = False,
    history_limit: int = 20,
) -> dict[str, Any]:
    """Build detailed audit JSONL/JSON/Markdown artifacts for one workflow run."""

    root = Path(output_dir)
    records = _collect_records(root, mode=mode, include_expressions=include_expressions)
    history = _history_baseline(root, history_limit=history_limit)
    summary = _summary(root, mode=mode, records=records, history=history)
    files = {
        "audit": str(root / AUDIT_FILE),
        "summary": str(root / SUMMARY_FILE),
        "markdown": str(root / MARKDOWN_FILE),
    }
    summary["files"] = files
    _write_jsonl(root / AUDIT_FILE, records)
    _write_json(root / SUMMARY_FILE, summary)
    _write_text(root / MARKDOWN_FILE, _render_markdown(summary, records))
    return summary


def _collect_records(root: Path, *, mode: str, include_expressions: bool) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    has_cycles = any(path.is_dir() for path in (root / "cycles").glob("cycle_*")) if (root / "cycles").is_dir() else False
    for artifact_dir in _artifact_dirs(root):
        cycle_index = _cycle_index(artifact_dir)
        for stage, filename in STAGE_FILES:
            if has_cycles and artifact_dir == root and stage in {"simulation", "review"}:
                continue
            path = artifact_dir / filename
            if not path.is_file():
                continue
            for row_index, row in enumerate(_read_jsonl(path), start=1):
                record = _audit_record(
                    row,
                    stage=stage,
                    mode=mode,
                    artifact_dir=artifact_dir,
                    artifact_path=path,
                    row_index=row_index,
                    cycle_index=cycle_index,
                    include_expressions=include_expressions,
                )
                if record:
                    records.append(record)
    return _dedupe_records(records)


def _audit_record(
    row: dict[str, Any],
    *,
    stage: str,
    mode: str,
    artifact_dir: Path,
    artifact_path: Path,
    row_index: int,
    cycle_index: int | None,
    include_expressions: bool,
) -> dict[str, Any]:
    expression = str(row.get("expression") or row.get("source_expression") or "").strip()
    annotated = annotate_candidate_identity({**row, "expression": expression}, {}) if expression else dict(row)
    metrics = _metrics(annotated)
    failed_checks = failed_check_names(annotated)
    root_cause = audit_root_cause(annotated, stage=stage, failed_checks=failed_checks)
    tweak_type = _tweak_type(annotated, root_cause=root_cause)
    outcome = _outcome(annotated, stage=stage, root_cause=root_cause)
    next_action = next_action_for_root_cause(root_cause, hints=annotated.get("repair_strategy_hints") or [])
    fields, operators = _components(expression, annotated)
    signature = annotated.get("field_signature") or (field_signature(expression) if expression else "")
    audit = {
        "schema_version": SCHEMA_VERSION,
        "audit_record_id": _audit_id(stage, artifact_path, row_index, annotated),
        "created_at": _now(),
        "mode": mode,
        "stage": stage,
        "outcome": outcome,
        "artifact_path": str(artifact_path),
        "artifact_dir": str(artifact_dir),
        "row_index": row_index,
        "cycle_index": cycle_index if cycle_index is not None else annotated.get("cycle_index"),
        "candidate_uid": annotated.get("candidate_uid"),
        "expression_hash": annotated.get("expression_hash"),
        "settings_hash": annotated.get("settings_hash"),
        "alpha_id": annotated.get("alpha_id"),
        "tag": annotated.get("tag"),
        "source_family": annotated.get("source_family") or annotated.get("mutation_strategy"),
        "mutation_strategy": annotated.get("mutation_strategy"),
        "parent_alpha_ids": annotated.get("parent_alpha_ids") or [],
        "tweak_type": tweak_type,
        "tweak_reason": _tweak_reason(annotated),
        "community_skill_tags": annotated.get("community_skill_tags") or [],
        "skill_failure_tags": annotated.get("skill_failure_tags") or [],
        "field_signature": signature,
        "fields": fields,
        "operators": operators,
        "settings_changed": _settings_changed(annotated),
        "failure_kind": annotated.get("failure_kind") or annotated.get("review_failure_kind") or "",
        "root_cause_bucket": root_cause,
        "triage_bucket": annotated.get("triage_bucket"),
        "triage_reason": annotated.get("triage_reason"),
        "presubmit_reject_reason": annotated.get("presubmit_reject_reason"),
        "candidate_skip_reason": annotated.get("candidate_skip_reason"),
        "failed_platform_checks": failed_checks,
        "forum_policy_action": annotated.get("forum_policy_action"),
        "forum_policy_reason": annotated.get("forum_policy_reason"),
        "community_skill_risk_flags": annotated.get("community_skill_risk_flags") or [],
        "next_action": next_action,
        **metrics,
    }
    if include_expressions and expression:
        audit["expression"] = expression
    return {key: value for key, value in audit.items() if value not in (None, "", [], {})}


def _summary(root: Path, *, mode: str, records: list[dict[str, Any]], history: dict[str, Any]) -> dict[str, Any]:
    unique_candidates = {
        str(row.get("candidate_uid") or row.get("expression_hash") or row.get("alpha_id") or row.get("audit_record_id"))
        for row in records
    }
    stage_counts = Counter(str(row.get("stage") or "unknown") for row in records)
    outcome_counts = Counter(str(row.get("outcome") or "unknown") for row in records)
    root_cause_counts = Counter(str(row.get("root_cause_bucket") or "unknown") for row in records)
    tweak_stats = _tweak_stats(records)
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "output_dir": str(root),
        "mode": mode,
        "record_count": len(records),
        "unique_candidate_count": len(unique_candidates),
        "stage_counts": dict(sorted(stage_counts.items())),
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "root_cause_counts": dict(root_cause_counts.most_common()),
        "tweak_type_stats": tweak_stats,
        "top_failure_examples": _failure_examples(records, limit=12),
        "history_baseline": history,
        "privacy": {
            "default_expression_policy": "withheld",
            "note": "Markdown and default JSONL use hashes, fields, operators, and metrics instead of full expressions.",
        },
    }


def _tweak_stats(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        buckets[str(row.get("tweak_type") or "unknown")].append(row)
    stats = []
    for tweak_type, rows in buckets.items():
        outcomes = Counter(str(row.get("outcome") or "unknown") for row in rows)
        failures = Counter(str(row.get("root_cause_bucket") or "unknown") for row in rows if _is_failure(row))
        stats.append({
            "tweak_type": tweak_type,
            "record_count": len(rows),
            "simulated_count": outcomes.get("simulated", 0) + outcomes.get("reviewed", 0),
            "ready_count": outcomes.get("ready", 0) + outcomes.get("confirmed_ready", 0),
            "submitted_success_count": outcomes.get("submitted_success", 0),
            "rejected_count": sum(count for outcome, count in outcomes.items() if "reject" in outcome or "fail" in outcome or outcome in {"skipped", "near_miss_repair"}),
            "outcomes": dict(sorted(outcomes.items())),
            "top_failure": failures.most_common(1)[0][0] if failures else "",
        })
    return sorted(stats, key=lambda row: (-int(row["record_count"]), str(row["tweak_type"])))


def _history_baseline(root: Path, *, history_limit: int) -> dict[str, Any]:
    if history_limit <= 0:
        return {"runs_considered": 0, "tweak_type_stats": []}
    candidates = []
    parent = root.parent
    if parent.is_dir():
        candidates.extend(path for path in parent.glob(f"*/{SUMMARY_FILE}") if path.parent != root)
    candidates = sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[:history_limit]
    aggregate: dict[str, Counter[str]] = defaultdict(Counter)
    considered = 0
    for path in candidates:
        summary = _read_json(path)
        if not summary:
            continue
        considered += 1
        for row in summary.get("tweak_type_stats") or []:
            tweak = str(row.get("tweak_type") or "unknown")
            aggregate[tweak]["record_count"] += int(row.get("record_count") or 0)
            aggregate[tweak]["ready_count"] += int(row.get("ready_count") or 0)
            aggregate[tweak]["submitted_success_count"] += int(row.get("submitted_success_count") or 0)
            aggregate[tweak]["rejected_count"] += int(row.get("rejected_count") or 0)
    stats = []
    for tweak, counter in aggregate.items():
        total = counter["record_count"]
        success_like = counter["ready_count"] + counter["submitted_success_count"]
        stats.append({
            "tweak_type": tweak,
            "record_count": total,
            "ready_count": counter["ready_count"],
            "submitted_success_count": counter["submitted_success_count"],
            "rejected_count": counter["rejected_count"],
            "success_like_rate": round(success_like / total, 4) if total else 0.0,
        })
    return {
        "runs_considered": considered,
        "tweak_type_stats": sorted(stats, key=lambda row: (-int(row["record_count"]), str(row["tweak_type"])))[:20],
    }


def _render_markdown(summary: dict[str, Any], records: list[dict[str, Any]]) -> str:
    lines = [
        "# WQ Iteration Audit",
        "",
        f"- Mode: {summary.get('mode') or 'unknown'}",
        f"- Records: {summary.get('record_count', 0)}",
        f"- Unique candidates: {summary.get('unique_candidate_count', 0)}",
        f"- Expression policy: {summary.get('privacy', {}).get('default_expression_policy', 'withheld')}",
        "",
        "## Failure Diagnosis",
        "",
    ]
    if summary.get("root_cause_counts"):
        lines.extend(_counter_table(summary["root_cause_counts"], "Root cause"))
    else:
        lines.append("_No failures detected._")
    lines.extend(["", "## Tweak Effectiveness", "", "| Tweak | Records | Ready | Submitted | Rejected | Top failure |", "| --- | ---: | ---: | ---: | ---: | --- |"])
    for row in summary.get("tweak_type_stats") or []:
        lines.append(
            f"| `{row.get('tweak_type')}` | {row.get('record_count', 0)} | {row.get('ready_count', 0)} | "
            f"{row.get('submitted_success_count', 0)} | {row.get('rejected_count', 0)} | {row.get('top_failure') or ''} |"
        )
    history = summary.get("history_baseline") or {}
    if history.get("runs_considered"):
        lines.extend(["", "## Historical Baseline", "", "| Tweak | Historical records | Ready | Submitted | Success-like rate |", "| --- | ---: | ---: | ---: | ---: |"])
        for row in history.get("tweak_type_stats") or []:
            lines.append(
                f"| `{row.get('tweak_type')}` | {row.get('record_count', 0)} | {row.get('ready_count', 0)} | "
                f"{row.get('submitted_success_count', 0)} | {row.get('success_like_rate', 0.0)} |"
            )
    lines.extend(["", "## Representative Failures", ""])
    examples = summary.get("top_failure_examples") or []
    if not examples:
        lines.append("_No representative failure examples._")
    else:
        lines.extend(["| Candidate | Stage | Outcome | Root cause | Metrics | Next action |", "| --- | --- | --- | --- | --- | --- |"])
        for row in examples:
            metrics = f"sharpe={_fmt(row.get('sharpe'))} fitness={_fmt(row.get('fitness'))} turnover={_fmt(row.get('turnover'))} sc={_fmt(row.get('sc_value'))}"
            candidate = row.get("candidate_uid") or row.get("expression_hash") or row.get("alpha_id") or "unknown"
            lines.append(
                f"| `{str(candidate)[:16]}` | {row.get('stage') or ''} | {row.get('outcome') or ''} | "
                f"{row.get('root_cause_bucket') or ''} | {metrics} | {row.get('next_action') or ''} |"
            )
    lines.extend(["", "## Next Actions", ""])
    actions = Counter(str(row.get("next_action") or "") for row in records if row.get("next_action") and _is_failure(row))
    if actions:
        for action, count in actions.most_common(8):
            lines.append(f"- {action}: {count}")
    else:
        lines.append("- Continue current gate; no dominant failure action detected.")
    return "\n".join(lines).rstrip() + "\n"


def _failure_examples(records: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    examples = []
    for row in records:
        if not _is_failure(row):
            continue
        examples.append({
            key: row.get(key)
            for key in (
                "candidate_uid",
                "expression_hash",
                "alpha_id",
                "stage",
                "outcome",
                "root_cause_bucket",
                "tweak_type",
                "sharpe",
                "fitness",
                "turnover",
                "sc_value",
                "prod_corr_value",
                "next_action",
            )
            if row.get(key) not in (None, "", [], {})
        })
    return examples[:limit]


def _artifact_dirs(root: Path) -> list[Path]:
    dirs = [root]
    cycles = root / "cycles"
    if cycles.is_dir():
        dirs.extend(path for path in sorted(cycles.glob("cycle_*")) if path.is_dir())
    return dirs


def _cycle_index(path: Path) -> int | None:
    name = path.name
    if not name.startswith("cycle_"):
        return None
    try:
        return int(name.split("_", 1)[1])
    except ValueError:
        return None


def _tweak_type(row: dict[str, Any], *, root_cause: str) -> str:
    text = " ".join(str(row.get(key) or "") for key in (
        "mutation_strategy",
        "source_family",
        "tag",
        "tweak_reason",
        "triage_reason",
        "presubmit_reject_reason",
        "candidate_skip_reason",
    )).lower()
    tags = {str(value) for value in row.get("community_skill_tags") or []}
    flags = {str(value) for value in row.get("community_skill_risk_flags") or row.get("risk_flags") or []}
    if root_cause == "legal_input" or "operator_platform_unit_probe" in tags:
        return "legal_input_probe"
    if root_cause == "duplicate_or_similarity":
        return "similarity_duplicate_block"
    if root_cause == "policy_block" or flags & {"template_clone_risk", "possible_complete_alpha"}:
        return "template_clone_block"
    if root_cause in {"subuniverse_coverage"} or "coverage" in text:
        return "coverage_breadth_repair"
    if "setting" in text or "truncation" in text or "decay" in text:
        return "settings_grid"
    if "smooth" in text or "hump" in text or root_cause == "turnover_density":
        return "smoothing_decay_truncation"
    if "field" in text or "family_shift" in text:
        return "field_family_shift"
    if "operator" in text:
        return "operator_family_shift"
    if "overlay" in text or "blend" in text:
        return "overlay_addition"
    if root_cause in {"policy_block", "pending_check"}:
        return "policy_gate"
    return str(row.get("mutation_strategy") or row.get("source_family") or "unknown")


def _outcome(row: dict[str, Any], *, stage: str, root_cause: str) -> str:
    if stage == "candidate_skipped":
        return "skipped"
    if stage == "presubmit_ready":
        return "ready"
    if stage == "presubmit_rejected":
        return "presubmit_rejected"
    if stage == "submitted_success":
        return "submitted_success"
    if stage == "repair_plan":
        return "repair_requested"
    if stage == "review":
        bucket = str(row.get("triage_bucket") or "")
        if bucket == "confirmed_ready":
            return "confirmed_ready"
        if bucket:
            return bucket
        return "reviewed"
    if stage == "submit_result":
        if bool(row.get("ok")) or str(row.get("final_status") or row.get("status") or "").upper() in {"ACTIVE", "SUBMITTED"}:
            return "submitted_success"
        return "submit_failed" if root_cause != "none" else "submit_recorded"
    if stage == "simulation":
        if row.get("status") == "simulation_timeout":
            return "simulation_failed"
        return "simulated"
    return stage


def _is_failure(row: dict[str, Any]) -> bool:
    outcome = str(row.get("outcome") or "")
    root_cause = str(row.get("root_cause_bucket") or "")
    return root_cause not in {"", "none"} and outcome not in {"ready", "confirmed_ready", "submitted_success"}


def _metrics(row: dict[str, Any]) -> dict[str, Any]:
    nested = row.get("result") if isinstance(row.get("result"), dict) else {}
    is_metrics = nested.get("is_metrics") if isinstance(nested.get("is_metrics"), dict) else {}
    return {
        "sharpe": _first(row.get("sharpe"), is_metrics.get("sharpe")),
        "fitness": _first(row.get("fitness"), is_metrics.get("fitness")),
        "returns": _first(row.get("returns"), is_metrics.get("returns")),
        "turnover": _first(row.get("turnover"), is_metrics.get("turnover")),
        "sc_value": _first(row.get("sc_value"), _nested(row, ("self_correlation", "value"))),
        "prod_corr_value": _first(row.get("prod_corr_value"), _nested(row, ("prod_correlation", "value"))),
    }


def _components(expression: str, row: dict[str, Any]) -> tuple[list[str], list[str]]:
    fields = row.get("fields") or row.get("source_fields") or []
    operators = row.get("operators") or []
    if expression and (not fields or not operators):
        components = expression_components(expression)
        fields = fields or components.get("fields") or []
        operators = operators or components.get("operators") or []
    return sorted(str(value) for value in fields if value), sorted(str(value) for value in operators if value)


def _settings_changed(row: dict[str, Any]) -> dict[str, Any]:
    mismatches = row.get("simulation_setting_mismatches")
    if isinstance(mismatches, dict) and mismatches:
        return mismatches
    settings = row.get("simulation_settings") or row.get("effective_simulation_settings")
    return settings if isinstance(settings, dict) else {}


def _tweak_reason(row: dict[str, Any]) -> str:
    return str(
        row.get("expected_low_corr_reason")
        or row.get("rationale")
        or row.get("triage_reason")
        or row.get("presubmit_reject_reason")
        or row.get("candidate_skip_reason")
        or ""
    )


def _audit_id(stage: str, artifact_path: Path, row_index: int, row: dict[str, Any]) -> str:
    raw = "|".join([
        stage,
        str(artifact_path),
        str(row_index),
        str(row.get("candidate_uid") or ""),
        str(row.get("expression_hash") or ""),
        str(row.get("alpha_id") or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _dedupe_rows_by_key(records, lambda row: str(row.get("audit_record_id") or ""))


def _counter_table(counter: dict[str, Any], label: str) -> list[str]:
    lines = [f"| {label} | Count |", "| --- | ---: |"]
    for key, value in counter.items():
        lines.append(f"| `{key}` | {value} |")
    return lines


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.4g}"
    except (TypeError, ValueError):
        return str(value)


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None
