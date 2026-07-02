"""Loop status, summary, and post-submit helpers for WQ workflow orchestration."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .artifact_io import read_json as _read_json
from .artifact_io import read_jsonl as _read_jsonl
from .artifact_io import utc_now as _now
from .artifact_io import write_json as _write_json
from .wq_agent_config import WorkflowPaths, WQAgentWorkflowConfig
from .wq_agent_records import workflow_files as _workflow_files
from .wq_iteration_audit import build_iteration_audit
from .wq_post_submit_review import WQPostSubmitReviewConfig, build_post_submit_review
from .wq_workflow_constants import ROOT


def _run_submit_cycle_limit(config: WQAgentWorkflowConfig, remaining_sim_budget: int) -> int:
    requested = config.cycle_candidate_count if config.cycle_candidate_count > 0 else config.max_simulations
    if requested <= 0:
        requested = config.target_candidates if config.target_candidates > 0 else 1
    if config.max_simulations > 0:
        requested = min(requested, config.max_simulations)
    return max(1, min(requested, remaining_sim_budget))


def _compact_cycle_summary(summary: dict) -> dict:
    submission = summary.get("submission") or {}
    results = (submission.get("result") or {}).get("results") or {}
    return {
        "cycle_index": summary.get("cycle_index"),
        "output_dir": str(summary.get("cycle_output_dir") or ""),
        "simulated": (summary.get("simulation") or {}).get("simulated", 0),
        "review_counts": (summary.get("review") or {}).get("counts") or {},
        "selected": submission.get("selected") or [],
        "submission_successes": sum(1 for entry in results.values() if _submission_entry_succeeded(entry)),
        "candidate_skip": summary.get("candidate_skip") or {},
        "candidate_pool": (summary.get("candidate_design") or {}).get("output"),
        "review_queue": (summary.get("review") or {}).get("output"),
        "submit_results": submission.get("output"),
        "summary": (summary.get("files") or {}).get("summary"),
    }


def _compact_presubmit_cycle_summary(summary: dict) -> dict:
    return {
        "cycle_index": summary.get("cycle_index"),
        "output_dir": str(summary.get("cycle_output_dir") or ""),
        "simulated": (summary.get("simulation") or {}).get("simulated", 0),
        "review_counts": (summary.get("review") or {}).get("counts") or {},
        "candidate_skip": summary.get("candidate_skip") or {},
        "candidate_pool": (summary.get("candidate_design") or {}).get("output"),
        "review_queue": (summary.get("review") or {}).get("output"),
        "summary": (summary.get("files") or {}).get("summary"),
    }


def _successful_submission_records(submit_summary: dict, review_rows: list[dict], *, cycle_index: int) -> list[dict]:
    results = (submit_summary.get("result") or {}).get("results") or {}
    by_id = {str(row.get("alpha_id") or ""): row for row in review_rows}
    records = []
    for alpha_id, entry in results.items():
        if not _submission_entry_succeeded(entry):
            continue
        source = by_id.get(str(alpha_id), {})
        records.append({
            "created_at": _now(),
            "cycle_index": cycle_index,
            "alpha_id": alpha_id,
            "expression": source.get("expression"),
            "tag": source.get("tag"),
            "triage_bucket": source.get("triage_bucket"),
            "sharpe": source.get("sharpe"),
            "fitness": source.get("fitness"),
            "turnover": source.get("turnover"),
            "sc_value": source.get("sc_value"),
            "prod_corr_value": source.get("prod_corr_value"),
            "temporal_stability_score": source.get("temporal_stability_score"),
            "temporal_stability": source.get("temporal_stability"),
            "pnl_warnings": source.get("pnl_warnings"),
            "submit_entry": entry,
        })
    return records


def _run_post_submit_review(config: WQAgentWorkflowConfig, paths: WorkflowPaths, *, run_dirs: list[Path]) -> dict:
    if not config.post_submit_review_enabled:
        return {"ok": True, "skipped": True, "reason": "post_submit_review_disabled"}
    if config.dry_run:
        return {"ok": True, "skipped": True, "reason": "dry_run"}
    try:
        return build_post_submit_review(WQPostSubmitReviewConfig(
            run_dirs=tuple(run_dirs),
            output_dir=paths.output_dir / "post_submit_review",
            baseline_roots=tuple(config.post_submit_baseline_roots),
            profile_dir=config.post_submit_profile_dir,
            window_days=max(1, int(config.post_submit_window_days or 14)),
        ))
    except Exception as exc:
        return {"ok": False, "error": str(exc), "output_dir": str(paths.output_dir / "post_submit_review")}


def _submission_entry_succeeded(entry: dict) -> bool:
    if bool(entry.get("ok")):
        return True
    final_status = str(entry.get("final_status") or entry.get("platform_status") or entry.get("status") or "").upper()
    return final_status in {"ACTIVE", "SUBMITTED"}


def _write_loop_status(
    paths: WorkflowPaths,
    config: WQAgentWorkflowConfig,
    *,
    cycle_summaries: list[dict],
    submitted_records: list[dict],
    total_simulations: int,
    stop_reason: str,
    consecutive_empty_cycles: int,
    consecutive_submit_failures: int,
) -> dict:
    target_reached = len(submitted_records) >= config.target_submissions
    payload = {
        "schema_version": 1,
        "ok": target_reached,
        "mode": "run-submit",
        "updated_at": _now(),
        "running": stop_reason == "running",
        "canonical_entrypoint": "scripts/wq_agent_workflow.py run-submit",
        "status_reader": "summary.json / loop_status.json",
        "authoritative_status_file": str(paths.loop_status),
        "stop_reason": stop_reason,
        "target_submissions": config.target_submissions,
        "submitted_successes": len(submitted_records),
        "total_simulations": total_simulations,
        "max_total_simulations": config.max_total_simulations,
        "cycle_count": len(cycle_summaries),
        "max_cycles": config.max_cycles,
        "consecutive_empty_cycles": consecutive_empty_cycles,
        "consecutive_submit_failures": consecutive_submit_failures,
        "allow_submit_probe": config.allow_submit_probe,
        "dry_run": config.dry_run,
        "submitted": submitted_records,
        "cycles": cycle_summaries,
        "files": {
            "loop_status": str(paths.loop_status),
            "submitted_accumulator": str(paths.submitted_accumulator),
            "iteration_audit": str(paths.iteration_audit),
            "iteration_audit_summary": str(paths.iteration_audit_summary),
            "iteration_audit_markdown": str(paths.iteration_audit_markdown),
            "cycles_dir": str(paths.output_dir / "cycles"),
        },
    }
    _write_json(paths.loop_status, payload)
    return payload


def _write_presubmit_loop_status(
    paths: WorkflowPaths,
    config: WQAgentWorkflowConfig,
    *,
    platform_summary: dict,
    cycle_summaries: list[dict],
    ready_records: list[dict],
    total_simulations: int,
    stop_reason: str,
    consecutive_empty_cycles: int,
) -> dict:
    target_reached = len(ready_records) >= config.target_ready
    payload = {
        "schema_version": 1,
        "ok": target_reached,
        "mode": "presubmit-sequential",
        "updated_at": _now(),
        "running": stop_reason == "running",
        "canonical_entrypoint": "scripts/wq_agent_workflow.py presubmit-sequential",
        "status_reader": "summary.json / loop_status.json",
        "authoritative_status_file": str(paths.loop_status),
        "stop_reason": stop_reason,
        "target_ready": config.target_ready,
        "ready_count": len(ready_records),
        "total_simulations": total_simulations,
        "max_total_simulations": config.max_total_simulations,
        "cycle_count": len(cycle_summaries),
        "max_cycles": config.max_cycles,
        "consecutive_empty_cycles": consecutive_empty_cycles,
        "no_real_submit": True,
        "strict_self_correlation_cutoff": config.presubmit_self_correlation_cutoff,
        "presubmit_self_correlation_cutoff": config.presubmit_self_correlation_cutoff,
        "presubmit_daily_return_correlation_cutoff": config.presubmit_daily_return_correlation_cutoff,
        "presubmit_daily_return_correlation_warn": config.presubmit_daily_return_correlation_warn,
        "virtual_similarity_cutoff": config.virtual_similarity_cutoff,
        "max_virtual_family_count": config.max_virtual_family_count,
        "max_virtual_field_signature_count": config.max_virtual_field_signature_count,
        "platform_sync": platform_summary,
        "ready": ready_records,
        "cycles": cycle_summaries,
        "files": {
            "loop_status": str(paths.loop_status),
            "virtual_active_inventory": str(paths.virtual_active_inventory),
            "presubmit_ready_sequential": str(paths.presubmit_ready_sequential),
            "presubmit_rejected": str(paths.presubmit_rejected),
            "alpha_lifecycle_events": str(paths.lifecycle_events),
            "iteration_audit": str(paths.iteration_audit),
            "iteration_audit_summary": str(paths.iteration_audit_summary),
            "iteration_audit_markdown": str(paths.iteration_audit_markdown),
            "cycles_dir": str(paths.output_dir / "cycles"),
        },
    }
    _write_json(paths.loop_status, payload)
    return payload


def _finish(paths: WorkflowPaths, config: WQAgentWorkflowConfig, mode: str, sections: dict[str, Any]) -> dict:
    review_rows = _read_jsonl(paths.review_queue) if paths.review_queue.is_file() else []
    iteration_audit = _workflow_iteration_audit(paths, config, mode)
    summary = {
        "schema_version": 1,
        "ok": True,
        "mode": mode,
        "updated_at": _now(),
        "submit_guard": "No real submit unless mode=submit or mode=run-submit with explicit authorization.",
        "canonical_entrypoint": "scripts/wq_agent_workflow.py",
        "authoritative_status_file": str(paths.summary),
        "bucket_counts": dict(sorted(Counter(row.get("triage_bucket") for row in review_rows).items())),
        "community_skill_report": _workflow_community_skill_report(paths, review_rows),
        "iteration_audit": iteration_audit,
        "files": _workflow_files(paths),
        **sections,
    }
    _write_json(paths.summary, summary)
    return summary


def _workflow_iteration_audit(paths: WorkflowPaths, config: WQAgentWorkflowConfig, mode: str) -> dict:
    if not config.iteration_audit_enabled:
        return {
            "enabled": False,
            "files": {
                "audit": str(paths.iteration_audit),
                "summary": str(paths.iteration_audit_summary),
                "markdown": str(paths.iteration_audit_markdown),
            },
        }
    try:
        summary = build_iteration_audit(
            paths.output_dir,
            mode=mode,
            include_expressions=config.audit_include_expressions,
            history_limit=max(0, int(config.audit_history_limit or 0)),
        )
        return {"enabled": True, "ok": True, **summary}
    except Exception as exc:
        return {
            "enabled": True,
            "ok": False,
            "error": str(exc),
            "files": {
                "audit": str(paths.iteration_audit),
                "summary": str(paths.iteration_audit_summary),
                "markdown": str(paths.iteration_audit_markdown),
            },
        }


def _workflow_community_skill_report(paths: WorkflowPaths, review_rows: list[dict]) -> dict:
    memory = _read_json(paths.memory_context) if paths.memory_context.is_file() else {}
    candidate_rows = _read_jsonl(paths.candidate_pool) if paths.candidate_pool.is_file() else []
    policy_rows = candidate_rows + review_rows
    return {
        "community_context_dir": memory.get("community_context_dir") or "",
        "loaded_skill_count": memory.get("community_skill_count") or len(memory.get("community_skills") or []),
        "loaded_skills": [row.get("skill_id") for row in (memory.get("community_skills") or [])[:12] if row.get("skill_id")],
        "forum_policy_actions": dict(sorted(Counter(
            row.get("forum_policy_action") for row in policy_rows if row.get("forum_policy_action")
        ).items())),
        "forum_policy_reasons": dict(Counter(
            row.get("forum_policy_reason") for row in policy_rows if row.get("forum_policy_reason")
        ).most_common(20)),
        "community_skill_risk_flags": dict(Counter(
            flag for row in policy_rows for flag in (row.get("community_skill_risk_flags") or [])
        ).most_common(20)),
        "community_skill_tags": dict(Counter(
            tag for row in review_rows for tag in (row.get("community_skill_tags") or [])
        ).most_common(20)),
        "repair_strategy_hints": dict(Counter(
            hint for row in review_rows for hint in (row.get("repair_strategy_hints") or [])
        ).most_common(20)),
    }


def _resolve_output_dir(output_dir: Path) -> Path:
    return output_dir if output_dir.is_absolute() else ROOT / output_dir
