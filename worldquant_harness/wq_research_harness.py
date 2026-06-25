"""Harness-style evaluation and evolution for WQ research experiments.

This module keeps the research loop explicit: it evaluates local sandbox
artifacts, writes replayable reports, and proposes the next mining generation.
It never calls WorldQuant submit endpoints.
"""

from __future__ import annotations

import csv
import json
import math
import secrets
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .wq_profile_evolver import evolve_research_profile
from .wq_research_miner import field_signature
from .wq_research_profile import default_research_profile
from .wq_research_sandbox import DEFAULT_EXPERIMENT_ROOT, ResearchSandboxPaths, new_research_experiment

SCHEMA_VERSION = 1

SELF_CORRELATION_REASONS = {
    "self_correlation_not_pass",
    "self_correlation_value_above_strict_cutoff",
    "self_correlation_fail",
    "self_correlation_high",
}
TOO_SIMILAR_REASONS = {
    "too_similar_to_real_or_virtual_active",
    "too_similar_to_inventory",
    "high_similarity",
    "exact_active_duplicate",
    "exact_inventory_duplicate",
    "duplicate_or_active_expression",
}
ILLEGAL_INPUT_REASONS = {
    "illegal_candidate_schema",
    "illegal_expression",
    "illegal_field",
    "illegal_operator",
    "illegal_field_type",
    "known_invalid_wq_field",
    "local_wq_validation_failed",
    "unavailable_dataset_field",
}
INVALID_FIELD_REASONS = {
    "illegal_field",
    "known_invalid_wq_field",
    "unavailable_dataset_field",
}
INVALID_OPERATOR_REASONS = {"illegal_operator", "local_wq_validation_failed"}
INVALID_FIELD_TYPE_REASONS = {"illegal_field_type"}
SUCCESS_STATUSES = {"ACTIVE", "SUBMITTED"}


@dataclass(frozen=True)
class WQHarnessEvalConfig:
    experiment: Path
    submit_run_dirs: tuple[Path, ...] = field(default_factory=tuple)
    eval_id: str | None = None
    output_dir: Path | None = None


@dataclass(frozen=True)
class WQHarnessEvolutionConfig:
    experiment: Path
    eval_dir: Path | None = None
    output_root: Path | None = None
    min_improvement: float = 0.02
    create_child_experiment: bool = True


def run_wq_harness_evaluation(config: WQHarnessEvalConfig) -> dict[str, Any]:
    """Evaluate one sandbox experiment and write harness artifacts."""

    paths = ResearchSandboxPaths.for_dir(_resolve_experiment_dir(config.experiment))
    record = _read_json(paths.experiment)
    if not record:
        raise FileNotFoundError(f"experiment record not found: {paths.experiment}")

    eval_dir = _evaluation_dir(paths.experiment_dir, config)
    presubmit_summary = _read_json(paths.presubmit_run / "summary.json")
    loop_status = _read_json(paths.presubmit_run / "loop_status.json")
    candidates = _read_jsonl(paths.candidate_specs)
    ready_rows = _read_jsonl(paths.presubmit_run / "presubmit_ready_sequential.jsonl")
    rejected_rows = _read_jsonl(paths.presubmit_run / "presubmit_rejected.jsonl")
    review_rows = _read_jsonl(paths.presubmit_run / "review_queue.jsonl")
    critic = _read_json(paths.critic_report)
    decision = _read_json(paths.decision)

    candidate_skip_counts = _candidate_skip_reason_counts(loop_status, presubmit_summary)
    reject_counts = _reject_reason_counts(rejected_rows, candidate_skip_counts)
    records = _evaluation_records(
        candidates=candidates,
        ready_rows=ready_rows,
        rejected_rows=rejected_rows,
        review_rows=review_rows,
        candidate_skip_counts=candidate_skip_counts,
    )
    field_summary = _field_signature_summary(records)
    submit_stats = _submit_success_stats(config.submit_run_dirs, ready_rows=ready_rows, decision=decision)
    metrics = _summary_metrics(
        record=record,
        loop_status=loop_status,
        presubmit_summary=presubmit_summary,
        ready_rows=ready_rows,
        rejected_rows=rejected_rows,
        review_rows=review_rows,
        reject_counts=reject_counts,
        field_summary=field_summary,
        submit_stats=submit_stats,
    )
    score = _harness_score(metrics)
    gate = _gate_report(metrics, score)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "eval_id": eval_dir.name,
        "experiment_id": record.get("id"),
        "experiment_dir": str(paths.experiment_dir),
        "harness_score": score,
        "metrics": metrics,
        "gate": gate,
        "reject_counts": dict(sorted(reject_counts.items())),
        "field_signature": field_summary,
        "submit_stats": submit_stats,
        "critic_decision": critic.get("decision"),
        "sandbox_decision": decision.get("decision"),
        "files": {
            "eval_records": str(eval_dir / "eval_records.csv"),
            "eval_summary": str(eval_dir / "eval_summary.json"),
            "eval_summary_csv": str(eval_dir / "eval_summary.csv"),
            "summary_by_field_signature": str(eval_dir / "summary_by_field_signature.csv"),
            "summary_by_reject_reason": str(eval_dir / "summary_by_reject_reason.csv"),
            "gate_report": str(eval_dir / "gate_report.json"),
            "run_report": str(eval_dir / "run_report.md"),
            "manifest": str(eval_dir / "manifest.json"),
        },
    }

    eval_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(eval_dir / "eval_records.csv", records, _eval_record_fields())
    _write_csv(eval_dir / "eval_summary.csv", [_flatten_summary_row(summary)], None)
    _write_csv(eval_dir / "summary_by_field_signature.csv", field_summary["rows"], None)
    _write_csv(
        eval_dir / "summary_by_reject_reason.csv",
        [{"reason": reason, "count": count} for reason, count in sorted(reject_counts.items())],
        None,
    )
    _write_json(eval_dir / "eval_summary.json", summary)
    _write_json(eval_dir / "gate_report.json", gate)
    _write_json(eval_dir / "manifest.json", _manifest(record, paths, eval_dir, config))
    (eval_dir / "run_report.md").write_text(_run_report(summary), encoding="utf-8")
    return {
        "ok": True,
        "experiment_id": record.get("id"),
        "eval_id": eval_dir.name,
        "eval_dir": str(eval_dir),
        "harness_score": score,
        "metrics": metrics,
        "gate": gate,
        "files": summary["files"],
    }


def evolve_wq_research_experiment(config: WQHarnessEvolutionConfig) -> dict[str, Any]:
    """Reflect on the latest evaluation and create the next experiment config."""

    paths = ResearchSandboxPaths.for_dir(_resolve_experiment_dir(config.experiment))
    record = _read_json(paths.experiment)
    if not record:
        raise FileNotFoundError(f"experiment record not found: {paths.experiment}")

    eval_dir = _resolve_eval_dir(paths.experiment_dir, config.eval_dir)
    if eval_dir is None:
        eval_result = run_wq_harness_evaluation(WQHarnessEvalConfig(experiment=paths.experiment_dir))
        eval_dir = Path(eval_result["eval_dir"])
    summary = _read_json(eval_dir / "eval_summary.json")
    if not summary:
        raise FileNotFoundError(f"eval summary not found: {eval_dir / 'eval_summary.json'}")

    metrics = summary.get("metrics") or {}
    parent_score = _safe_float(summary.get("harness_score"))
    generation = int((record.get("evolution") or {}).get("generation") or 0) + 1
    base_mine_config = _base_mine_config(record, summary)
    next_config, actions = _evolution_overrides(record, metrics, base_mine_config)
    gate_overrides = _gate_overrides(record, metrics)
    field_blacklist = _field_signature_blacklist(summary)
    active_profile = record.get("research_profile") if isinstance(record.get("research_profile"), dict) else default_research_profile()
    profile_evolution = evolve_research_profile(
        active_profile,
        summary,
        field_signature_blacklist=field_blacklist,
        min_improvement=config.min_improvement,
    )
    recommended_profile = _recommended_profile(profile_evolution)
    next_payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "type": "wq_research_next_generation",
        "parent_experiment_id": record.get("id"),
        "parent_experiment_dir": str(paths.experiment_dir),
        "source_eval_dir": str(eval_dir),
        "generation": generation,
        "harness_score": parent_score,
        "mine_config_overrides": next_config,
        "gate_overrides": gate_overrides,
        "field_signature_blacklist": field_blacklist,
        "profile_evolution": profile_evolution,
        "recommended_profile_candidate": profile_evolution.get("recommended_candidate"),
        "recommended_research_profile": recommended_profile,
        "actions": actions,
        "submit_guard": "Evolution only proposes the next local mining generation; it never submits alphas.",
    }

    child_result: dict[str, Any] | None = None
    if config.create_child_experiment:
        child_result = _create_child_experiment(
            parent_record=record,
            parent_paths=paths,
            generation=generation,
            next_payload=next_payload,
            output_root=config.output_root,
        )
        next_payload["child_experiment"] = child_result

    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "created_at": _now(),
        "experiment_id": record.get("id"),
        "eval_dir": str(eval_dir),
        "decision": _evolution_decision(summary, min_improvement=config.min_improvement),
        "next_generation": next_payload,
        "files": {
            "evolution_result": str(eval_dir / "evolution_result.json"),
            "reflector_report": str(eval_dir / "reflector_report.md"),
            "next_experiment": str(eval_dir / "next_experiment.yaml"),
        },
    }
    _write_json(eval_dir / "next_experiment.yaml", next_payload)
    _write_json(eval_dir / "evolution_result.json", result)
    (eval_dir / "reflector_report.md").write_text(_reflector_report(record, summary, next_payload, result), encoding="utf-8")
    return result


def render_wq_harness_report(eval_dir: Path) -> dict[str, Any]:
    """Return the persisted evaluation report and make sure markdown exists."""

    eval_dir = Path(eval_dir)
    summary = _read_json(eval_dir / "eval_summary.json")
    if not summary:
        raise FileNotFoundError(f"eval summary not found: {eval_dir / 'eval_summary.json'}")
    report_path = eval_dir / "run_report.md"
    if not report_path.is_file():
        report_path.write_text(_run_report(summary), encoding="utf-8")
    return {
        "ok": True,
        "eval_dir": str(eval_dir),
        "harness_score": summary.get("harness_score"),
        "metrics": summary.get("metrics") or {},
        "gate": summary.get("gate") or {},
        "run_report": str(report_path),
    }


def _evaluation_records(
    *,
    candidates: list[dict[str, Any]],
    ready_rows: list[dict[str, Any]],
    rejected_rows: list[dict[str, Any]],
    review_rows: list[dict[str, Any]],
    candidate_skip_counts: Counter[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in candidates:
        records.append(_record("candidate_spec", row, reason="selected_by_research_miner"))
    for row in ready_rows:
        records.append(_record("ready", row, reason=str(row.get("presubmit_accept_reason") or "accepted")))
    for row in rejected_rows:
        records.append(_record("rejected", row, reason=_reject_reason(row)))
    for row in review_rows:
        records.append(_record("review", row, reason=str(row.get("triage_reason") or row.get("triage_bucket") or "")))
    for reason, count in sorted(candidate_skip_counts.items()):
        records.append({
            "record_type": "candidate_skip",
            "source_file": "loop_status.cycles.candidate_skip",
            "reason": reason,
            "count": count,
        })
    return records


def _record(record_type: str, row: dict[str, Any], *, reason: str) -> dict[str, Any]:
    expression = str(row.get("expression") or "")
    signature = str(row.get("field_signature") or (field_signature(expression) if expression else ""))
    gate = row.get("presubmit_gate") if isinstance(row.get("presubmit_gate"), dict) else {}
    return {
        "record_type": record_type,
        "source_file": _record_source(record_type),
        "alpha_id": row.get("alpha_id"),
        "candidate_spec_id": row.get("candidate_spec_id"),
        "tag": row.get("tag"),
        "expression": expression,
        "field_signature": signature,
        "source_family": row.get("source_family") or row.get("mutation_strategy"),
        "status": row.get("status") or row.get("platform_status") or row.get("triage_bucket"),
        "reason": reason,
        "created_at": row.get("created_at"),
        "cycle_index": row.get("cycle_index"),
        "sharpe": row.get("sharpe"),
        "fitness": row.get("fitness"),
        "turnover": row.get("turnover"),
        "sc_value": row.get("sc_value"),
        "nearest_similarity": row.get("nearest_similarity") or gate.get("nearest_similarity"),
        "presubmit_accepted": row.get("presubmit_accepted"),
        "count": 1,
    }


def _record_source(record_type: str) -> str:
    return {
        "candidate_spec": "candidate_specs.jsonl",
        "ready": "presubmit_ready_sequential.jsonl",
        "rejected": "presubmit_rejected.jsonl",
        "review": "review_queue.jsonl",
    }.get(record_type, "")


def _summary_metrics(
    *,
    record: dict[str, Any],
    loop_status: dict[str, Any],
    presubmit_summary: dict[str, Any],
    ready_rows: list[dict[str, Any]],
    rejected_rows: list[dict[str, Any]],
    review_rows: list[dict[str, Any]],
    reject_counts: Counter[str],
    field_summary: dict[str, Any],
    submit_stats: dict[str, Any],
) -> dict[str, Any]:
    total_simulations = int(
        _first_number(
            loop_status.get("total_simulations"),
            _nested(presubmit_summary, "presubmit_loop", "total_simulations"),
            0,
        )
        or 0
    )
    ready_count = len(ready_rows)
    rejection_count = sum(reject_counts.values())
    self_rejects = _matching_reason_count(reject_counts, SELF_CORRELATION_REASONS)
    too_similar_rejects = _matching_reason_count(reject_counts, TOO_SIMILAR_REASONS)
    illegal_input_rejects = _matching_reason_count(reject_counts, ILLEGAL_INPUT_REASONS)
    invalid_field_rejects = _exact_reason_count(reject_counts, INVALID_FIELD_REASONS)
    invalid_operator_rejects = _exact_reason_count(reject_counts, INVALID_OPERATOR_REASONS)
    illegal_field_type_rejects = _exact_reason_count(reject_counts, INVALID_FIELD_TYPE_REASONS)
    first_ready_seconds = _hypothesis_to_first_ready_seconds(record, ready_rows)
    promote_submit_success_rate = None
    if submit_stats["submit_attempt_count"] > 0:
        promote_submit_success_rate = _ratio(submit_stats["real_submit_success_count"], submit_stats["submit_attempt_count"])

    return {
        "ready_count": ready_count,
        "review_count": len(review_rows),
        "presubmit_rejected_count": len(rejected_rows),
        "total_rejection_count": rejection_count,
        "total_simulations": total_simulations,
        "ready_per_100_simulations": _ratio(ready_count * 100.0, total_simulations),
        "self_correlation_reject_count": self_rejects,
        "self_correlation_reject_share": _ratio(self_rejects, rejection_count),
        "too_similar_reject_count": too_similar_rejects,
        "too_similar_reject_share": _ratio(too_similar_rejects, rejection_count),
        "illegal_input_reject_count": illegal_input_rejects,
        "illegal_input_reject_share": _ratio(illegal_input_rejects, rejection_count),
        "invalid_field_reject_count": invalid_field_rejects,
        "invalid_operator_reject_count": invalid_operator_rejects,
        "illegal_field_type_reject_count": illegal_field_type_rejects,
        "duplicate_field_signature_count": field_summary["duplicate_field_signature_count"],
        "field_signature_total_rows": field_summary["field_signature_total_rows"],
        "field_signature_unique_count": field_summary["field_signature_unique_count"],
        "field_signature_duplicate_ratio": _ratio(
            field_summary["duplicate_field_signature_count"],
            field_summary["field_signature_total_rows"],
        ),
        "hypothesis_to_first_ready_seconds": first_ready_seconds,
        "promoted_candidate_count": submit_stats["promoted_candidate_count"],
        "real_submit_attempt_count": submit_stats["submit_attempt_count"],
        "real_submit_success_count": submit_stats["real_submit_success_count"],
        "promote_submit_success_rate": promote_submit_success_rate,
        "stop_reason": loop_status.get("stop_reason") or _nested(presubmit_summary, "presubmit_loop", "stop_reason"),
        "virtual_similarity_cutoff": loop_status.get("virtual_similarity_cutoff"),
        "max_virtual_field_signature_count": loop_status.get("max_virtual_field_signature_count"),
        "target_ready": loop_status.get("target_ready"),
        "max_total_simulations": loop_status.get("max_total_simulations"),
    }


def _reject_reason_counts(rejected_rows: list[dict[str, Any]], candidate_skip_counts: Counter[str]) -> Counter[str]:
    counts: Counter[str] = Counter(candidate_skip_counts)
    for row in rejected_rows:
        counts[_reject_reason(row)] += 1
    return counts


def _reject_reason(row: dict[str, Any]) -> str:
    return str(
        row.get("presubmit_reject_reason")
        or row.get("candidate_skip_reason")
        or row.get("reject_reason")
        or row.get("triage_reason")
        or row.get("failure_kind")
        or "unknown"
    )


def _candidate_skip_reason_counts(loop_status: dict[str, Any], presubmit_summary: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for cycle in _all_cycles(loop_status, presubmit_summary):
        skip = cycle.get("candidate_skip") if isinstance(cycle.get("candidate_skip"), dict) else {}
        for reason, count in (skip.get("skip_reasons") or {}).items():
            counts[str(reason)] += int(count or 0)
    return counts


def _all_cycles(loop_status: dict[str, Any], presubmit_summary: dict[str, Any]) -> list[dict[str, Any]]:
    cycles = loop_status.get("cycles")
    if isinstance(cycles, list):
        return [row for row in cycles if isinstance(row, dict)]
    cycles = _nested(presubmit_summary, "presubmit_loop", "cycles")
    if isinstance(cycles, list):
        return [row for row in cycles if isinstance(row, dict)]
    return []


def _field_signature_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    signature_records = [row for row in records if row.get("record_type") == "candidate_spec" and row.get("field_signature")]
    if not signature_records:
        signature_records = [row for row in records if row.get("field_signature")]

    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter(str(row.get("field_signature") or "") for row in signature_records)
    ready_counts: Counter[str] = Counter(
        str(row.get("field_signature") or "")
        for row in records
        if row.get("record_type") == "ready" and row.get("field_signature")
    )
    rejected_counts: Counter[str] = Counter(
        str(row.get("field_signature") or "")
        for row in records
        if row.get("record_type") == "rejected" and row.get("field_signature")
    )
    for signature, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        rows.append({
            "field_signature": signature,
            "count": count,
            "duplicate_count": max(0, count - 1),
            "ready_count": ready_counts.get(signature, 0),
            "rejected_count": rejected_counts.get(signature, 0),
        })
    return {
        "field_signature_total_rows": sum(counts.values()),
        "field_signature_unique_count": len(counts),
        "duplicate_field_signature_count": sum(max(0, count - 1) for count in counts.values()),
        "top_duplicate_field_signatures": [row for row in rows if row["duplicate_count"] > 0][:10],
        "rows": rows,
    }


def _submit_success_stats(
    submit_run_dirs: tuple[Path, ...],
    *,
    ready_rows: list[dict[str, Any]],
    decision: dict[str, Any],
) -> dict[str, Any]:
    promoted = len([row for row in ready_rows if row.get("presubmit_accepted", True)])
    if decision.get("decision") and decision.get("decision") != "promote_candidate":
        promoted = 0
    successes: dict[str, dict[str, Any]] = {}
    attempts: dict[str, dict[str, Any]] = {}
    summary_successes = 0
    summary_attempts = 0
    files: list[str] = []

    for run_dir in submit_run_dirs:
        run_dir = Path(run_dir)
        summary = _read_json(run_dir / "summary.json")
        if summary:
            summary_successes += int(summary.get("submitted_successes") or _nested(summary, "run_submit_loop", "submitted_successes") or 0)
            summary_attempts += int(summary.get("submit_attempts") or 0)
            files.append(str(run_dir / "summary.json"))
        loop_status = _read_json(run_dir / "loop_status.json")
        if loop_status:
            summary_successes += int(loop_status.get("submitted_successes") or 0)
            files.append(str(run_dir / "loop_status.json"))
            for row in loop_status.get("submitted") or []:
                _collect_submit_row(row, attempts, successes)
        for name in ("submit_results.jsonl", "submitted_accumulator.jsonl"):
            path = run_dir / name
            rows = _read_jsonl(path)
            if rows:
                files.append(str(path))
            for row in rows:
                _collect_submit_row(row, attempts, successes)

    attempt_count = len(attempts) if attempts else summary_attempts
    success_count = len(successes) if successes else summary_successes
    if attempt_count == 0 and success_count:
        attempt_count = max(success_count, promoted)
    return {
        "promoted_candidate_count": promoted,
        "submit_attempt_count": attempt_count,
        "real_submit_success_count": success_count,
        "active_alpha_ids": sorted(successes) if successes else [],
        "submit_run_dirs": [str(path) for path in submit_run_dirs],
        "source_files": files,
    }


def _collect_submit_row(
    row: dict[str, Any],
    attempts: dict[str, dict[str, Any]],
    successes: dict[str, dict[str, Any]],
) -> None:
    key = str(row.get("alpha_id") or row.get("candidate_key") or row.get("source_index") or len(attempts) + 1)
    attempts[key] = row
    status = str(row.get("final_status") or row.get("platform_status") or row.get("status") or "").upper()
    if bool(row.get("ok")) and status in SUCCESS_STATUSES:
        successes[key] = row


def _harness_score(metrics: dict[str, Any]) -> float:
    ready_value = min((_safe_float(metrics.get("ready_per_100_simulations")) or 0.0) / 5.0, 1.0)
    submit_value = _safe_float(metrics.get("promote_submit_success_rate"))
    self_value = 1.0 - min(_safe_float(metrics.get("self_correlation_reject_share")) or 0.0, 1.0)
    similar_value = 1.0 - min(_safe_float(metrics.get("too_similar_reject_share")) or 0.0, 1.0)
    diversity_value = 1.0 - min(_safe_float(metrics.get("field_signature_duplicate_ratio")) or 0.0, 1.0)
    speed_seconds = _safe_float(metrics.get("hypothesis_to_first_ready_seconds"))
    speed_value = None
    if speed_seconds is not None:
        speed_value = max(0.0, 1.0 - min(speed_seconds, 86400.0) / 86400.0)
    weighted = [
        (ready_value, 0.35),
        (submit_value, 0.20),
        (self_value, 0.15),
        (similar_value, 0.15),
        (diversity_value, 0.10),
        (speed_value, 0.05),
    ]
    available = [(value, weight) for value, weight in weighted if value is not None and math.isfinite(value)]
    if not available:
        return 0.0
    total_weight = sum(weight for _, weight in available)
    return round(sum(value * weight for value, weight in available) / total_weight, 6)


def _gate_report(metrics: dict[str, Any], score: float) -> dict[str, Any]:
    reasons: list[str] = []
    total_simulations = int(metrics.get("total_simulations") or 0)
    ready_count = int(metrics.get("ready_count") or 0)
    if total_simulations <= 0 and ready_count == 0:
        decision = "hold"
        reasons.append("missing presubmit simulation artifacts")
    elif ready_count == 0 and total_simulations < 30:
        decision = "hold"
        reasons.append("insufficient simulation budget for a stable harness score")
    elif ready_count == 0:
        decision = "fail"
        reasons.append("no ready candidates produced")
    elif score >= 0.60:
        decision = "pass"
        reasons.append("ready candidates with acceptable rejection structure")
    else:
        decision = "hold"
        reasons.append("ready candidates exist but rejection structure needs improvement")
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "decision": decision,
        "harness_score": score,
        "reasons": reasons,
        "metrics": metrics,
    }


def _evolution_overrides(
    record: dict[str, Any],
    metrics: dict[str, Any],
    base_mine_config: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = dict(base_mine_config)
    actions: list[dict[str, Any]] = []
    self_share = _safe_float(metrics.get("self_correlation_reject_share")) or 0.0
    similar_share = _safe_float(metrics.get("too_similar_reject_share")) or 0.0
    illegal_input_share = _safe_float(metrics.get("illegal_input_reject_share")) or 0.0
    duplicate_count = int(metrics.get("duplicate_field_signature_count") or 0)
    ready_per_100 = _safe_float(metrics.get("ready_per_100_simulations")) or 0.0
    submit_rate = _safe_float(metrics.get("promote_submit_success_rate"))

    if self_share >= 0.30:
        config["max_family_count"] = max(3, int(config["max_family_count"]) - 2)
        config["max_field_signature_count"] = max(2, int(config["max_field_signature_count"]) - 1)
        _append_bias(config, "cross_domain_overlay")
        _append_bias(config, "standalone_low_overlap_research_family")
        actions.append({
            "trigger": "high_self_correlation_reject_share",
            "metric_value": self_share,
            "change": "reduce family/signature reuse and prefer cross-domain overlays",
        })

    if similar_share >= 0.25:
        config["similarity_cutoff"] = round(max(0.55, float(config["similarity_cutoff"]) - 0.05), 3)
        config["max_family_count"] = max(3, int(config["max_family_count"]) - 1)
        _append_bias(config, "low_overlap_field_family")
        actions.append({
            "trigger": "high_too_similar_reject_share",
            "metric_value": similar_share,
            "change": "tighten similarity cutoff and lower family capacity",
        })

    if duplicate_count > 0:
        config["max_field_signature_count"] = max(1, int(config["max_field_signature_count"]) - 1)
        actions.append({
            "trigger": "duplicate_field_signatures",
            "metric_value": duplicate_count,
            "change": "lower field signature capacity and blacklist crowded signatures",
        })

    if illegal_input_share >= 0.10:
        config["require_legal_inputs"] = True
        config["strict_legal_inputs"] = True
        _append_bias(config, "legal_input_registry_refresh")
        actions.append({
            "trigger": "high_illegal_input_reject_share",
            "metric_value": illegal_input_share,
            "change": "require strict legal input registry and reduce exploration from unknown fields",
        })

    if ready_per_100 < 1.0 and self_share < 0.25 and similar_share < 0.25:
        config["max_candidates"] = int(config["max_candidates"]) + 80
        config["cycle_candidate_count"] = int(config["cycle_candidate_count"]) + 5
        config["max_total_simulations"] = int(config["max_total_simulations"]) + 40
        _append_bias(config, "systematic_local_factor_grid")
        actions.append({
            "trigger": "low_ready_yield_without_similarity_blocker",
            "metric_value": ready_per_100,
            "change": "expand exploration budget for the next generation",
        })

    if submit_rate is not None and submit_rate < 0.50:
        _append_bias(config, "stricter_promote_gate_before_real_submit")
        actions.append({
            "trigger": "low_real_submit_success_rate_after_promote",
            "metric_value": submit_rate,
            "change": "raise promote caution and require stronger presubmit evidence",
        })

    if not actions:
        config["max_candidates"] = int(config["max_candidates"]) + 40
        _append_bias(config, "incremental_diversification")
        actions.append({
            "trigger": "stable_or_inconclusive_metrics",
            "metric_value": None,
            "change": "make a conservative diversified next generation",
        })

    config["evolution_parent_experiment_id"] = record.get("id")
    config["no_real_submit"] = True
    return config, actions


def _gate_overrides(record: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    gate = dict(record.get("gate") or {})
    submit_rate = _safe_float(metrics.get("promote_submit_success_rate"))
    if submit_rate is not None and submit_rate < 0.50:
        gate["min_ready"] = max(2, int(gate.get("min_ready") or 1))
        gate["promote_requires_linked_submit_review"] = True
    return gate


def _base_mine_config(record: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    metrics = summary.get("metrics") or {}
    allow_model = _nested(record, "suggested_mine_config", "allow_model")
    use_ledger = _nested(record, "suggested_mine_config", "use_ledger")
    strict_legal_inputs = _nested(record, "suggested_mine_config", "strict_legal_inputs")
    settings_strict_legal_inputs = _nested(record, "settings", "strict_legal_inputs")
    return {
        "max_candidates": int(_nested(record, "suggested_mine_config", "max_candidates") or 200),
        "similarity_cutoff": float(
            _nested(record, "suggested_mine_config", "similarity_cutoff")
            or metrics.get("virtual_similarity_cutoff")
            or 0.72
        ),
        "max_family_count": int(_nested(record, "suggested_mine_config", "max_family_count") or 8),
        "max_field_signature_count": int(
            _nested(record, "suggested_mine_config", "max_field_signature_count")
            or metrics.get("max_virtual_field_signature_count")
            or 4
        ),
        "target_ready": int(_nested(record, "suggested_mine_config", "target_ready") or 3),
        "max_total_simulations": int(
            _nested(record, "suggested_mine_config", "max_total_simulations")
            or metrics.get("max_total_simulations")
            or 120
        ),
        "cycle_candidate_count": int(_nested(record, "suggested_mine_config", "cycle_candidate_count") or 20),
        "max_cycles": int(_nested(record, "suggested_mine_config", "max_cycles") or 10),
        "allow_model": bool(allow_model) if allow_model is not None else False,
        "use_ledger": bool(use_ledger) if use_ledger is not None else True,
        "legal_inputs_file": (
            _nested(record, "suggested_mine_config", "legal_inputs_file")
            or _nested(record, "settings", "legal_inputs_file")
            or ""
        ),
        "strict_legal_inputs": (
            bool(strict_legal_inputs)
            if strict_legal_inputs is not None
            else bool(settings_strict_legal_inputs) if settings_strict_legal_inputs is not None else True
        ),
    }


def _field_signature_blacklist(summary: dict[str, Any]) -> list[str]:
    field_summary = summary.get("field_signature") or {}
    rows = field_summary.get("top_duplicate_field_signatures") or []
    return [str(row.get("field_signature")) for row in rows if row.get("field_signature")]


def _create_child_experiment(
    *,
    parent_record: dict[str, Any],
    parent_paths: ResearchSandboxPaths,
    generation: int,
    next_payload: dict[str, Any],
    output_root: Path | None,
) -> dict[str, Any]:
    root = output_root or parent_paths.experiment_dir.parent
    topic = f"{parent_record.get('topic') or parent_record.get('id') or 'wq research'} g{generation}"
    parent_hypothesis = str(_nested(parent_record, "hypothesis", "statement") or "")
    action_text = "; ".join(str(action.get("change")) for action in next_payload["actions"])
    hypothesis = (parent_hypothesis + "\n\n" if parent_hypothesis else "") + f"Evolution hypothesis g{generation}: {action_text}."
    child = new_research_experiment(
        topic,
        root=root,
        hypothesis=hypothesis,
        citations=list(_nested(parent_record, "hypothesis", "citations") or []),
        settings=dict(parent_record.get("settings") or {}),
        gate=next_payload["gate_overrides"],
    )
    child_paths = ResearchSandboxPaths.for_dir(Path(child["experiment_dir"]))
    child_record = _read_json(child_paths.experiment)
    child_record["evolution"] = {
        "generation": generation,
        "parent_experiment_id": parent_record.get("id"),
        "parent_experiment_dir": str(parent_paths.experiment_dir),
        "source_eval_dir": next_payload["source_eval_dir"],
        "parent_harness_score": next_payload["harness_score"],
        "actions": next_payload["actions"],
    }
    child_record["suggested_mine_config"] = next_payload["mine_config_overrides"]
    child_record["field_signature_blacklist"] = next_payload["field_signature_blacklist"]
    if isinstance(next_payload.get("recommended_research_profile"), dict):
        child_record["research_profile"] = next_payload["recommended_research_profile"]
        child_record["profile_evolution"] = {
            "recommended_candidate": next_payload.get("recommended_profile_candidate"),
            "source_eval_dir": next_payload.get("source_eval_dir"),
            "baseline_score": _nested(next_payload, "profile_evolution", "baseline_score"),
        }
    child_record["submit_guard"] = "No real submit is allowed from evolved experiments; run explicit submit commands outside the sandbox."
    _write_json(child_paths.experiment, child_record)
    return {
        "ok": True,
        "experiment_id": child.get("experiment_id"),
        "experiment_dir": child.get("experiment_dir"),
        "experiment": str(child_paths.experiment),
    }


def _evolution_decision(summary: dict[str, Any], *, min_improvement: float) -> dict[str, Any]:
    score = _safe_float(summary.get("harness_score")) or 0.0
    metrics = summary.get("metrics") or {}
    parent_score = _safe_float(_nested(summary, "parent", "harness_score"))
    if parent_score is None:
        return {
            "status": "seed_next_generation",
            "reason": "no parent harness score recorded; create a tracked child generation",
            "harness_score": score,
        }
    delta = score - parent_score
    return {
        "status": "accept" if delta >= min_improvement else "hold",
        "reason": "score improved over parent" if delta >= min_improvement else "score did not clear min_improvement",
        "harness_score": score,
        "parent_harness_score": parent_score,
        "score_delta": delta,
        "ready_count": metrics.get("ready_count"),
    }


def _recommended_profile(profile_evolution: dict[str, Any]) -> dict[str, Any] | None:
    key = profile_evolution.get("recommended_candidate")
    candidates = profile_evolution.get("candidates") if isinstance(profile_evolution.get("candidates"), dict) else {}
    candidate = candidates.get(key) if isinstance(key, str) else None
    profile = candidate.get("profile") if isinstance(candidate, dict) else None
    return profile if isinstance(profile, dict) else None


def _append_bias(config: dict[str, Any], value: str) -> None:
    biases = list(config.get("priority_biases") or [])
    if value not in biases:
        biases.append(value)
    config["priority_biases"] = biases


def _matching_reason_count(counts: Counter[str], targets: set[str]) -> int:
    total = 0
    for reason, count in counts.items():
        lowered = str(reason).lower()
        if lowered in targets or any(target in lowered for target in targets):
            total += count
    return total


def _exact_reason_count(counts: Counter[str], targets: set[str]) -> int:
    return sum(count for reason, count in counts.items() if str(reason).lower() in targets)


def _hypothesis_to_first_ready_seconds(record: dict[str, Any], ready_rows: list[dict[str, Any]]) -> int | None:
    created = _parse_time(str(record.get("created_at") or ""))
    if created is None:
        return None
    ready_times = [
        parsed
        for parsed in (_parse_time(str(row.get("created_at") or "")) for row in ready_rows)
        if parsed is not None
    ]
    if not ready_times:
        return None
    return max(0, int((min(ready_times) - created).total_seconds()))


def _parse_time(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _evaluation_dir(experiment_dir: Path, config: WQHarnessEvalConfig) -> Path:
    if config.output_dir is not None:
        return Path(config.output_dir)
    eval_id = config.eval_id or f"eval-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    return experiment_dir / "evaluations" / eval_id


def _resolve_eval_dir(experiment_dir: Path, eval_dir: Path | None) -> Path | None:
    if eval_dir:
        path = Path(eval_dir)
        return path if path.is_absolute() else (Path.cwd() / path)
    evaluations = experiment_dir / "evaluations"
    if not evaluations.is_dir():
        return None
    candidates = [path for path in evaluations.iterdir() if (path / "eval_summary.json").is_file()]
    return sorted(candidates)[-1] if candidates else None


def _resolve_experiment_dir(experiment: Path) -> Path:
    path = Path(experiment)
    if (path / "experiment.yaml").is_file():
        return path
    if path.is_file():
        return path.parent
    candidate = DEFAULT_EXPERIMENT_ROOT / str(experiment)
    if (candidate / "experiment.yaml").is_file():
        return candidate
    named_candidate = DEFAULT_EXPERIMENT_ROOT / path.name
    if (named_candidate / "experiment.yaml").is_file():
        return named_candidate
    raise FileNotFoundError(f"experiment not found: {experiment}")


def _manifest(
    record: dict[str, Any],
    paths: ResearchSandboxPaths,
    eval_dir: Path,
    config: WQHarnessEvalConfig,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "kind": "wq_research_harness_evaluation",
        "eval_id": eval_dir.name,
        "experiment_id": record.get("id"),
        "experiment_dir": str(paths.experiment_dir),
        "submit_run_dirs": [str(path) for path in config.submit_run_dirs],
        "submit_guard": "Evaluation reads submit artifacts only when explicitly supplied; it never submits.",
    }


def _run_report(summary: dict[str, Any]) -> str:
    metrics = summary.get("metrics") or {}
    gate = summary.get("gate") or {}
    return "\n".join([
        f"# WQ Harness Evaluation {summary.get('eval_id')}",
        "",
        f"- Experiment: {summary.get('experiment_id')}",
        f"- Harness score: {summary.get('harness_score')}",
        f"- Gate: {gate.get('decision')}",
        f"- Ready / 100 simulations: {metrics.get('ready_per_100_simulations')}",
        f"- Self-correlation reject share: {metrics.get('self_correlation_reject_share')}",
        f"- Too-similar reject share: {metrics.get('too_similar_reject_share')}",
        f"- Duplicate field signatures: {metrics.get('duplicate_field_signature_count')}",
        f"- Hypothesis to first ready seconds: {metrics.get('hypothesis_to_first_ready_seconds')}",
        f"- Promote submit success rate: {metrics.get('promote_submit_success_rate')}",
        "",
        "## Gate Reasons",
        "",
        *[f"- {reason}" for reason in gate.get("reasons") or []],
        "",
    ])


def _reflector_report(
    record: dict[str, Any],
    summary: dict[str, Any],
    next_payload: dict[str, Any],
    result: dict[str, Any],
) -> str:
    actions = next_payload.get("actions") or []
    return "\n".join([
        f"# WQ Evolution Reflection g{next_payload.get('generation')}",
        "",
        f"- Parent experiment: {record.get('id')}",
        f"- Source eval: {next_payload.get('source_eval_dir')}",
        f"- Harness score: {summary.get('harness_score')}",
        f"- Decision: {(result.get('decision') or {}).get('status')}",
        "",
        "## Actions",
        "",
        *[f"- {action.get('trigger')}: {action.get('change')}" for action in actions],
        "",
        "## Next Mining Overrides",
        "",
        "```json",
        json.dumps(next_payload.get("mine_config_overrides") or {}, ensure_ascii=False, indent=2, default=str),
        "```",
        "",
        "## Profile Evolution",
        "",
        f"- Recommended candidate: {next_payload.get('recommended_profile_candidate')}",
        f"- Baseline score: {_nested(next_payload, 'profile_evolution', 'baseline_score')}",
        "",
    ])


def _flatten_summary_row(summary: dict[str, Any]) -> dict[str, Any]:
    metrics = summary.get("metrics") or {}
    gate = summary.get("gate") or {}
    return {
        "eval_id": summary.get("eval_id"),
        "experiment_id": summary.get("experiment_id"),
        "harness_score": summary.get("harness_score"),
        "gate_decision": gate.get("decision"),
        **metrics,
    }


def _eval_record_fields() -> list[str]:
    return [
        "record_type",
        "source_file",
        "alpha_id",
        "candidate_spec_id",
        "tag",
        "expression",
        "field_signature",
        "source_family",
        "status",
        "reason",
        "created_at",
        "cycle_index",
        "sharpe",
        "fitness",
        "turnover",
        "sc_value",
        "nearest_similarity",
        "presubmit_accepted",
        "count",
    ]


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        fieldnames = keys or ["empty"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    return json.loads(text)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _ratio(numerator: float | int, denominator: float | int) -> float | None:
    try:
        denominator_float = float(denominator)
        if denominator_float == 0.0:
            return None
        return round(float(numerator) / denominator_float, 6)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_number(*values: Any) -> float | None:
    for value in values:
        parsed = _safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
