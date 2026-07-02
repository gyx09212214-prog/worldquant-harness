"""Role-based WorldQuant alpha mining workflow.

This module coordinates existing worldquant-harness WQ components into a conservative
multi-agent pipeline. The default path is find/check/review only; real submit is
isolated behind the submit mode and an explicit count or alpha id list.
"""

# ruff: noqa: F401
# Legacy callers import many stage helpers from this facade during the workflow split.

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .artifact_io import append_jsonl as _append_jsonl
from .artifact_io import read_json as _read_json
from .artifact_io import read_jsonl as _read_jsonl
from .artifact_io import utc_now as _now
from .artifact_io import write_json as _write_json
from .artifact_io import write_jsonl as _write_jsonl
from .wq_agent_config import (
    GENERATION_EVOLUTIONARY,
    GENERATION_MIXED,
    GENERATION_MIXED_EVOLUTIONARY,
    GENERATION_MODEL_PRIMARY,
    GENERATION_MODES,
    GENERATION_TEMPLATE_FALLBACK,
    WorkflowPaths,
    WQAgentWorkflowConfig,
)
from .wq_agent_records import candidate_dedupe_key as _candidate_dedupe_key
from .wq_agent_records import workflow_config_dict as _config_dict
from .wq_auto_mining import load_dotenv
from .wq_workflow_candidate_design import CandidateDesignerAgent, ModelCandidateDesignerAgent
from .wq_workflow_constants import (
    ACTIVE_OR_SUBMITTED,
    BLOCKED_REPAIR_MUTATION_STRATEGIES,
    BLOCKED_REPAIR_SOURCE_FAMILIES,
    CONFIRMED_READY,
    HARD_FAIL,
    INFRA_TIMEOUT,
    NEAR_MISS_REPAIR,
    ROOT,
    SUBMIT_PROBE_NEEDED,
    SUCCESS_FAMILY_SEEDS,
)
from .wq_workflow_execution import (
    ReviewAgent,
    SimulationAgent,
    _apply_pnl_report_to_review_row,
    _pnl_enrichment_targets,
    classify_review_row,
    classify_simulation_result,
)
from .wq_workflow_memory import CommunityScoutAgent, MemoryContextBuilder, _latest_post_submit_lessons
from .wq_workflow_platform import PlatformSyncAgent, build_active_inventory
from .wq_workflow_presubmit import (
    _filter_candidate_pool_for_presubmit,
    _should_defer_presubmit_recheck,
    build_virtual_active_inventory,
    build_virtual_ready_record,
    presubmit_acceptance_gate,
    select_presubmit_ready_candidate,
)
from .wq_workflow_submit_repair import (
    FailureReviewAgent,
    SubmissionAgent,
    _attach_repair_skill_annotations,
    build_repair_record,
    select_submission_candidates,
    should_repair,
)
from .wq_workflow_support import (
    _active_family_counts,
    _active_field_signature_counts,
    _api_check_status,
    _append_lifecycle_event,
    _check_result,
    _chunks,
    _community_context_for_config,
    _community_repair_annotations,
    _community_skill_route_for_flags,
    _compact_cycle_summary,
    _compact_presubmit_cycle_summary,
    _failed_platform_checks,
    _field_signature,
    _fields,
    _finish,
    _has_unsupported_statement_separator,
    _is_metric_near_miss,
    _is_option_only_expression,
    _is_repairable_platform_fail,
    _is_simulation_timeout_result,
    _jaccard,
    _legal_input_registry_for_config,
    _load_rejected_expression_keys,
    _load_seed_ready_records,
    _metrics_from_result,
    _operators,
    _platform_candidate_family,
    _repair_candidate_block_reason,
    _repair_candidate_sort_key,
    _resolve_output_dir,
    _response_items,
    _review_check,
    _row_can_submit,
    _row_family,
    _run_post_submit_review,
    _run_submit_cycle_limit,
    _score,
    _short_expr,
    _submission_entry_succeeded,
    _submission_policy_for_config,
    _successful_submission_records,
    _summarize_rows,
    _virtual_active_row,
    _workflow_community_skill_report,
    _workflow_iteration_audit,
    _write_loop_status,
    _write_presubmit_loop_status,
    build_candidate_generation_prompt,
    build_repair_generation_prompt,
    default_model_generate_candidates,
    default_model_generate_repairs,
    parse_model_candidate_response,
    parse_model_repair_response,
    render_memory_context_markdown,
    review_sort_key,
)


def run_workflow(
    config: WQAgentWorkflowConfig,
    *,
    mode: str = "run",
    dependencies: dict[str, Any] | None = None,
) -> dict:
    """Run one workflow mode and write the standard artifacts."""

    load_dotenv(ROOT)
    dependencies = dependencies or {}
    if config.generation_mode not in GENERATION_MODES:
        raise ValueError(f"unsupported generation_mode: {config.generation_mode}")
    paths = WorkflowPaths.for_output_dir(_resolve_output_dir(config.output_dir))
    paths.output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(paths.manifest, {
        "schema_version": 1,
        "created_at": _now(),
        "mode": mode,
        "submit_guard": (
            "run/sync/forum/postmortem/presubmit-sequential never call submit; "
            "submit requires explicit ids/count; run-submit requires explicit target_submissions"
        ),
        "canonical_entrypoint": "scripts/wq_agent_workflow.py",
        "authoritative_status_file": str(paths.loop_status if mode in {"run-submit", "presubmit-sequential"} else paths.summary),
        "config": _config_dict(config),
    })

    platform_agent = PlatformSyncAgent(config, paths, dependencies=dependencies)
    community_agent = CommunityScoutAgent(config, paths)
    memory_agent = MemoryContextBuilder(config, paths, dependencies=dependencies)
    designer_agent = ModelCandidateDesignerAgent(config, paths, dependencies=dependencies)
    simulation_agent = SimulationAgent(config, paths, dependencies=dependencies)
    review_agent = ReviewAgent(config, paths, dependencies=dependencies)
    failure_agent = FailureReviewAgent(config, paths, dependencies=dependencies)
    submission_agent = SubmissionAgent(config, paths, dependencies=dependencies)

    if mode == "run-submit":
        return run_submit_loop(config, paths, dependencies=dependencies)

    if mode == "presubmit-sequential":
        return run_presubmit_sequential(config, paths, dependencies=dependencies)

    if mode == "sync":
        platform_summary = platform_agent.run()
        return _finish(paths, config, mode, {"platform_sync": platform_summary})

    if mode == "forum":
        active_inventory = _read_json(paths.active_inventory) if paths.active_inventory.is_file() else {"active": []}
        forum_summary = community_agent.run(active_inventory=active_inventory)
        return _finish(paths, config, mode, {"community_scout": forum_summary})

    if mode == "postmortem":
        postmortem = failure_agent.run()
        return _finish(paths, config, mode, {"postmortem": postmortem})

    if mode == "submit":
        submit_summary = submission_agent.run()
        platform_summary = platform_agent.run() if not config.dry_run else {"ok": True, "skipped": True, "reason": "dry_run"}
        postmortem = failure_agent.run()
        post_submit_review = _run_post_submit_review(config, paths, run_dirs=[paths.output_dir])
        return _finish(
            paths,
            config,
            mode,
            {
                "submission": submit_summary,
                "platform_sync_after_submit": platform_summary,
                "postmortem": postmortem,
                "post_submit_review": post_submit_review,
            },
        )

    if mode != "run":
        raise ValueError(f"unsupported workflow mode: {mode}")

    platform_summary = platform_agent.run()
    active_inventory = _read_json(paths.active_inventory)
    community_summary = community_agent.run(active_inventory=active_inventory)
    memory_summary = memory_agent.run(active_inventory=active_inventory)
    design_summary = designer_agent.run(active_inventory=active_inventory)
    simulation_summary = simulation_agent.run()
    review_summary = review_agent.run()
    postmortem = failure_agent.run()
    return _finish(
        paths,
        config,
        mode,
        {
            "platform_sync": platform_summary,
            "community_scout": community_summary,
            "memory_context": memory_summary,
            "candidate_design": design_summary,
            "simulation": simulation_summary,
            "review": review_summary,
            "postmortem": postmortem,
        },
    )


def run_submit_loop(
    config: WQAgentWorkflowConfig,
    paths: WorkflowPaths,
    *,
    dependencies: dict[str, Any] | None = None,
) -> dict:
    """Run generate/simulate/review/submit cycles until the target is reached."""

    dependencies = dependencies or {}
    if config.target_submissions <= 0:
        raise ValueError("run-submit requires target_submissions > 0")
    if config.max_total_simulations <= 0:
        raise ValueError("run-submit requires max_total_simulations > 0")

    cycles_dir = paths.output_dir / "cycles"
    cycles_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(paths.submitted_accumulator, [])

    submitted_records: list[dict] = []
    cycle_summaries: list[dict] = []
    carryover_repairs: list[dict] = []
    rejected_normalized_expressions: set[str] = _load_rejected_expression_keys(config.seed_rejected_files)
    total_simulations = 0
    consecutive_empty_cycles = 0
    consecutive_submit_failures = 0
    stop_reason = "max_cycles_reached"

    _write_loop_status(
        paths,
        config,
        cycle_summaries=cycle_summaries,
        submitted_records=submitted_records,
        total_simulations=total_simulations,
        stop_reason="running",
        consecutive_empty_cycles=consecutive_empty_cycles,
        consecutive_submit_failures=consecutive_submit_failures,
    )

    for cycle_index in range(1, max(1, config.max_cycles) + 1):
        if len(submitted_records) >= config.target_submissions:
            stop_reason = "target_submissions_reached"
            break
        if total_simulations >= config.max_total_simulations:
            stop_reason = "max_total_simulations_reached"
            break
        if consecutive_empty_cycles >= max(1, config.max_consecutive_empty_cycles):
            stop_reason = "max_consecutive_empty_cycles_reached"
            break
        if consecutive_submit_failures >= max(1, config.max_consecutive_submit_failures):
            stop_reason = "max_consecutive_submit_failures_reached"
            break

        remaining_target = config.target_submissions - len(submitted_records)
        remaining_sim_budget = config.max_total_simulations - total_simulations
        per_cycle_limit = _run_submit_cycle_limit(config, remaining_sim_budget)
        design_candidate_limit = max(
            per_cycle_limit,
            per_cycle_limit + len(rejected_normalized_expressions),
        )
        cycle_dir = cycles_dir / f"cycle_{cycle_index:03d}"
        cycle_config = replace(
            config,
            output_dir=cycle_dir,
            target_candidates=max(1, design_candidate_limit),
            max_simulations=per_cycle_limit,
            submit_count=remaining_target,
            submit_alpha_ids=[],
        )
        cycle_paths = WorkflowPaths.for_output_dir(cycle_dir)

        cycle_summary = _run_submit_cycle(
            cycle_config,
            cycle_paths,
            cycle_index=cycle_index,
            parent_output_dir=paths.output_dir,
            dependencies=dependencies,
            carryover_repairs=carryover_repairs,
            skip_normalized_expressions=rejected_normalized_expressions,
        )
        cycle_summaries.append(_compact_cycle_summary(cycle_summary))

        simulated = int((cycle_summary.get("simulation") or {}).get("simulated") or 0)
        total_simulations += simulated
        if simulated <= 0:
            consecutive_empty_cycles += 1
        else:
            consecutive_empty_cycles = 0

        review_rows = _read_jsonl(cycle_paths.review_queue)
        new_records = _successful_submission_records(
            cycle_summary.get("submission") or {},
            review_rows,
            cycle_index=cycle_index,
        )
        for record in new_records:
            _append_jsonl(paths.submitted_accumulator, record)
        submitted_records.extend(new_records)

        selected = (cycle_summary.get("submission") or {}).get("selected") or []
        if selected and not new_records:
            consecutive_submit_failures += 1
        elif new_records:
            consecutive_submit_failures = 0

        carryover_repairs = _read_jsonl(cycle_paths.repair_queue)
        for row in _read_jsonl(cycle_paths.review_queue) or _read_jsonl(cycle_paths.simulation_results):
            expression = str(row.get("expression") or "")
            if expression:
                rejected_normalized_expressions.add(_candidate_dedupe_key(row))

        if len(submitted_records) >= config.target_submissions:
            stop_reason = "target_submissions_reached"
        elif total_simulations >= config.max_total_simulations:
            stop_reason = "max_total_simulations_reached"
        else:
            stop_reason = "running"

        _write_loop_status(
            paths,
            config,
            cycle_summaries=cycle_summaries,
            submitted_records=submitted_records,
            total_simulations=total_simulations,
            stop_reason=stop_reason,
            consecutive_empty_cycles=consecutive_empty_cycles,
            consecutive_submit_failures=consecutive_submit_failures,
        )

        if stop_reason != "running":
            break
    else:
        if len(submitted_records) >= config.target_submissions:
            stop_reason = "target_submissions_reached"
        elif total_simulations >= config.max_total_simulations:
            stop_reason = "max_total_simulations_reached"
        else:
            stop_reason = "max_cycles_reached"

    final_status = _write_loop_status(
        paths,
        config,
        cycle_summaries=cycle_summaries,
        submitted_records=submitted_records,
        total_simulations=total_simulations,
        stop_reason=stop_reason,
        consecutive_empty_cycles=consecutive_empty_cycles,
        consecutive_submit_failures=consecutive_submit_failures,
    )
    post_submit_review = _run_post_submit_review(config, paths, run_dirs=[paths.output_dir])
    summary = _finish(paths, config, "run-submit", {"run_submit_loop": final_status, "post_submit_review": post_submit_review})
    summary["ok"] = final_status["ok"]
    _write_json(paths.summary, summary)
    return summary


def run_presubmit_sequential(
    config: WQAgentWorkflowConfig,
    paths: WorkflowPaths,
    *,
    dependencies: dict[str, Any] | None = None,
) -> dict:
    """Find check-passed candidates sequentially without submitting them.

    Each accepted candidate is added to a local virtual ACTIVE inventory before
    the next cycle, so later candidates are generated and locally screened
    against both platform ACTIVE alphas and earlier accepted candidates.
    """

    dependencies = dependencies or {}
    if config.target_ready <= 0:
        raise ValueError("presubmit-sequential requires target_ready > 0")
    if config.max_total_simulations <= 0:
        raise ValueError("presubmit-sequential requires max_total_simulations > 0")

    cycles_dir = paths.output_dir / "cycles"
    cycles_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(paths.presubmit_ready_sequential, [])
    _write_jsonl(paths.presubmit_rejected, [])
    _write_jsonl(paths.review_queue, [])
    _write_jsonl(paths.simulation_results, [])
    _write_jsonl(paths.submit_results, [])
    _write_jsonl(paths.lifecycle_events, [])

    platform_agent = PlatformSyncAgent(config, paths, dependencies=dependencies)
    platform_summary = platform_agent.run()
    platform_rows = _read_jsonl(paths.platform_alphas)
    real_inventory = _read_json(paths.active_inventory)
    real_active_rows = list(real_inventory.get("active") or [])
    virtual_records = _load_seed_ready_records(config.seed_ready_files)
    cycle_summaries: list[dict] = []
    carryover_repairs: list[dict] = []
    rejected_normalized_expressions: set[str] = _load_rejected_expression_keys(config.seed_rejected_files)
    rejected_normalized_expressions.update(
        _candidate_dedupe_key(row)
        for row in virtual_records
        if str(row.get("expression") or "").strip()
    )
    total_simulations = 0
    consecutive_empty_cycles = 0
    stop_reason = "max_cycles_reached"

    for ready_record in virtual_records:
        _append_jsonl(paths.presubmit_ready_sequential, ready_record)
        _append_lifecycle_event(paths, "candidate_ready", ready_record, config=config)

    initial_inventory = build_virtual_active_inventory(real_active_rows, virtual_records)
    _write_json(paths.virtual_active_inventory, initial_inventory)
    _write_presubmit_loop_status(
        paths,
        config,
        platform_summary=platform_summary,
        cycle_summaries=cycle_summaries,
        ready_records=virtual_records,
        total_simulations=total_simulations,
        stop_reason="running",
        consecutive_empty_cycles=consecutive_empty_cycles,
    )

    for cycle_index in range(1, max(1, config.max_cycles) + 1):
        if len(virtual_records) >= config.target_ready:
            stop_reason = "target_ready_reached"
            break
        if total_simulations >= config.max_total_simulations:
            stop_reason = "max_total_simulations_reached"
            break
        if consecutive_empty_cycles >= max(1, config.max_consecutive_empty_cycles):
            stop_reason = "max_consecutive_empty_cycles_reached"
            break

        remaining_sim_budget = config.max_total_simulations - total_simulations
        per_cycle_limit = _run_submit_cycle_limit(config, remaining_sim_budget)
        design_candidate_limit = max(
            per_cycle_limit,
            per_cycle_limit + len(rejected_normalized_expressions) + len(virtual_records),
        )
        cycle_dir = cycles_dir / f"cycle_{cycle_index:03d}"
        cycle_config = replace(
            config,
            output_dir=cycle_dir,
            target_candidates=max(1, design_candidate_limit),
            max_simulations=per_cycle_limit,
            submit_count=0,
            submit_alpha_ids=[],
        )
        cycle_paths = WorkflowPaths.for_output_dir(cycle_dir)
        combined_inventory = build_virtual_active_inventory(real_active_rows, virtual_records)
        _write_json(paths.virtual_active_inventory, combined_inventory)

        cycle_summary = _run_presubmit_cycle(
            cycle_config,
            cycle_paths,
            cycle_index=cycle_index,
            parent_output_dir=paths.output_dir,
            dependencies=dependencies,
            carryover_repairs=carryover_repairs,
            platform_rows=platform_rows,
            active_inventory=combined_inventory,
            skip_normalized_expressions=rejected_normalized_expressions,
        )
        cycle_summaries.append(_compact_presubmit_cycle_summary(cycle_summary))

        cycle_sim_rows = _read_jsonl(cycle_paths.simulation_results)
        cycle_review_rows = _read_jsonl(cycle_paths.review_queue)
        for row in cycle_sim_rows:
            _append_jsonl(paths.simulation_results, {**row, "cycle_index": cycle_index})
            _append_lifecycle_event(paths, "simulation_finished", {**row, "cycle_index": cycle_index}, config=config)
        for row in cycle_review_rows:
            _append_jsonl(paths.review_queue, {**row, "cycle_index": cycle_index})
            _append_lifecycle_event(paths, "review_finished", {**row, "cycle_index": cycle_index}, config=config)

        simulated = int((cycle_summary.get("simulation") or {}).get("simulated") or 0)
        total_simulations += simulated
        accepted, rejected = select_presubmit_ready_candidate(
            cycle_review_rows,
            combined_inventory.get("active") or [],
            config=config,
            cycle_index=cycle_index,
        )
        for row in rejected:
            _append_jsonl(paths.presubmit_rejected, row)
            _append_lifecycle_event(paths, "candidate_rejected", row, config=config)
            expression = str(row.get("expression") or "")
            if expression:
                rejected_normalized_expressions.add(_candidate_dedupe_key(row))

        if accepted:
            ready_record = build_virtual_ready_record(
                accepted,
                combined_inventory.get("active") or [],
                config=config,
                cycle_index=cycle_index,
                ready_index=len(virtual_records) + 1,
                cycle_output_dir=cycle_paths.output_dir,
            )
            virtual_records.append(ready_record)
            _append_jsonl(paths.presubmit_ready_sequential, ready_record)
            _append_lifecycle_event(paths, "candidate_ready", ready_record, config=config)
            consecutive_empty_cycles = 0
        else:
            consecutive_empty_cycles += 1

        carryover_repairs = _read_jsonl(cycle_paths.repair_queue)
        stop_reason = "running"
        if len(virtual_records) >= config.target_ready:
            stop_reason = "target_ready_reached"
        elif total_simulations >= config.max_total_simulations:
            stop_reason = "max_total_simulations_reached"

        _write_json(paths.virtual_active_inventory, build_virtual_active_inventory(real_active_rows, virtual_records))
        _write_presubmit_loop_status(
            paths,
            config,
            platform_summary=platform_summary,
            cycle_summaries=cycle_summaries,
            ready_records=virtual_records,
            total_simulations=total_simulations,
            stop_reason=stop_reason,
            consecutive_empty_cycles=consecutive_empty_cycles,
        )

        if stop_reason != "running":
            break
    else:
        if len(virtual_records) >= config.target_ready:
            stop_reason = "target_ready_reached"
        elif total_simulations >= config.max_total_simulations:
            stop_reason = "max_total_simulations_reached"
        else:
            stop_reason = "max_cycles_reached"

    final_status = _write_presubmit_loop_status(
        paths,
        config,
        platform_summary=platform_summary,
        cycle_summaries=cycle_summaries,
        ready_records=virtual_records,
        total_simulations=total_simulations,
        stop_reason=stop_reason,
        consecutive_empty_cycles=consecutive_empty_cycles,
    )
    _write_json(paths.virtual_active_inventory, build_virtual_active_inventory(real_active_rows, virtual_records))
    summary = _finish(paths, config, "presubmit-sequential", {"platform_sync": platform_summary, "presubmit_loop": final_status})
    summary["ok"] = final_status["ok"]
    _write_json(paths.summary, summary)
    return summary


def _run_submit_cycle(
    config: WQAgentWorkflowConfig,
    paths: WorkflowPaths,
    *,
    cycle_index: int,
    parent_output_dir: Path,
    dependencies: dict[str, Any],
    carryover_repairs: list[dict],
    skip_normalized_expressions: set[str] | None = None,
) -> dict:
    paths.output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(paths.manifest, {
        "schema_version": 1,
        "created_at": _now(),
        "mode": "run-submit-cycle",
        "cycle_index": cycle_index,
        "parent_output_dir": str(parent_output_dir),
        "submit_guard": "cycle submits only through parent run-submit authorization",
        "canonical_entrypoint": "scripts/wq_agent_workflow.py run-submit",
        "authoritative_status_file": str(paths.summary),
        "config": _config_dict(config),
    })
    if carryover_repairs:
        _write_jsonl(paths.repair_queue, carryover_repairs)

    platform_agent = PlatformSyncAgent(config, paths, dependencies=dependencies)
    community_agent = CommunityScoutAgent(config, paths)
    memory_agent = MemoryContextBuilder(config, paths, dependencies=dependencies)
    designer_agent = ModelCandidateDesignerAgent(config, paths, dependencies=dependencies)
    simulation_agent = SimulationAgent(config, paths, dependencies=dependencies)
    review_agent = ReviewAgent(config, paths, dependencies=dependencies)
    submission_agent = SubmissionAgent(config, paths, dependencies=dependencies)
    failure_agent = FailureReviewAgent(config, paths, dependencies=dependencies)

    platform_summary = platform_agent.run()
    active_inventory = _read_json(paths.active_inventory)
    community_summary = community_agent.run(active_inventory=active_inventory)
    memory_summary = memory_agent.run(active_inventory=active_inventory)
    design_summary = designer_agent.run(active_inventory=active_inventory)
    if skip_normalized_expressions:
        skip_summary = _filter_candidate_pool_for_presubmit(
            paths.candidate_pool,
            skip_normalized_expressions=skip_normalized_expressions,
            active_rows=active_inventory.get("active") or [],
            config=config,
        )
    else:
        designed_count = int(design_summary.get("candidates") or 0)
        skip_summary = {
            "ok": True,
            "input": designed_count,
            "kept": designed_count,
            "skipped": 0,
            "skip_reasons": {},
            "output": str(paths.candidate_pool),
        }
    simulation_summary = simulation_agent.run()
    review_summary = review_agent.run()
    submission_summary = submission_agent.run()
    platform_after_submit = (
        platform_agent.run()
        if submission_summary.get("selected") and not config.dry_run
        else {"ok": True, "skipped": True, "reason": "no real submission"}
    )
    postmortem = failure_agent.run()

    return _finish(
        paths,
        config,
        "run-submit-cycle",
        {
            "cycle_index": cycle_index,
            "cycle_output_dir": str(paths.output_dir),
            "platform_sync": platform_summary,
            "community_scout": community_summary,
            "memory_context": memory_summary,
            "candidate_design": design_summary,
            "candidate_skip": skip_summary,
            "simulation": simulation_summary,
            "review": review_summary,
            "submission": submission_summary,
            "platform_sync_after_submit": platform_after_submit,
            "postmortem": postmortem,
        },
    )


def _run_presubmit_cycle(
    config: WQAgentWorkflowConfig,
    paths: WorkflowPaths,
    *,
    cycle_index: int,
    parent_output_dir: Path,
    dependencies: dict[str, Any],
    carryover_repairs: list[dict],
    platform_rows: list[dict],
    active_inventory: dict,
    skip_normalized_expressions: set[str],
) -> dict:
    paths.output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(paths.manifest, {
        "schema_version": 1,
        "created_at": _now(),
        "mode": "presubmit-sequential-cycle",
        "cycle_index": cycle_index,
        "parent_output_dir": str(parent_output_dir),
        "submit_guard": "presubmit cycle never calls submit; accepted rows are virtual ACTIVE only",
        "canonical_entrypoint": "scripts/wq_agent_workflow.py presubmit-sequential",
        "authoritative_status_file": str(paths.summary),
        "config": _config_dict(config),
    })
    _write_jsonl(paths.platform_alphas, platform_rows)
    _write_json(paths.active_inventory, active_inventory)
    _write_json(paths.virtual_active_inventory, active_inventory)
    _write_jsonl(paths.submit_results, [])
    if carryover_repairs:
        _write_jsonl(paths.repair_queue, carryover_repairs)

    community_agent = CommunityScoutAgent(config, paths)
    memory_agent = MemoryContextBuilder(config, paths, dependencies=dependencies)
    designer_agent = ModelCandidateDesignerAgent(config, paths, dependencies=dependencies)
    simulation_agent = SimulationAgent(config, paths, dependencies=dependencies)
    review_agent = ReviewAgent(config, paths, dependencies=dependencies)
    failure_agent = FailureReviewAgent(config, paths, dependencies=dependencies)

    community_summary = community_agent.run(active_inventory=active_inventory)
    memory_summary = memory_agent.run(active_inventory=active_inventory)
    design_summary = designer_agent.run(active_inventory=active_inventory)
    skip_summary = _filter_candidate_pool_for_presubmit(
        paths.candidate_pool,
        skip_normalized_expressions=skip_normalized_expressions,
        active_rows=active_inventory.get("active") or [],
        config=config,
    )
    simulation_summary = simulation_agent.run()
    review_summary = review_agent.run()
    postmortem = failure_agent.run()

    return _finish(
        paths,
        config,
        "presubmit-sequential-cycle",
        {
            "cycle_index": cycle_index,
            "cycle_output_dir": str(paths.output_dir),
            "community_scout": community_summary,
            "memory_context": memory_summary,
            "candidate_design": design_summary,
            "candidate_skip": skip_summary,
            "simulation": simulation_summary,
            "review": review_summary,
            "submission": {"ok": True, "skipped": True, "reason": "presubmit-sequential never submits"},
            "postmortem": postmortem,
        },
    )
