"""Role-based WorldQuant alpha mining workflow.

This module coordinates existing QuantGPT WQ components into a conservative
multi-agent pipeline. The default path is find/check/review only; real submit is
isolated behind the submit mode and an explicit count or alpha id list.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .alpha_tracker import compute_similarity
from .community_context import CommunityContext
from .expression_parser import extract_components, normalize_expression
from .llm_service import clean_expression
from .wq_auto_mining import load_dotenv, validate_wq_expression
from .wq_brain_client import get_client, is_configured
from .wq_brain_service import (
    run_check_submissions,
    run_list_alphas,
    run_single_simulation,
    run_submit_by_ids,
    safe_float,
    submit_threshold_checks,
)
from .wq_evolutionary_generator import generate_evolutionary_candidates
from .wq_forum_submission_optimizer import (
    annotate_candidate_with_policy,
    evaluate_candidate_policy,
    load_submission_policy,
)
from .wq_alpha_detail import summarize_alpha_probe
from .wq_pnl_analysis import (
    analyze_alpha_probe_summary,
    build_pnl_analysis_report,
    write_pnl_analysis_artifacts,
)
from .wq_policy_repair_planner import build_policy_repair_records


ROOT = Path(__file__).resolve().parents[1]

CONFIRMED_READY = "confirmed_ready"
SUBMIT_PROBE_NEEDED = "submit_probe_needed"
NEAR_MISS_REPAIR = "near_miss_repair"
HARD_FAIL = "hard_fail"
ACTIVE_OR_SUBMITTED = "active_or_submitted"

SUCCESS_FAMILY_SEEDS = [
    {
        "expression": "rank(ts_rank(ebit / enterprise_value, 60) - ts_rank(returns, 20))",
        "tag": "legacy-value-reversal-ebit-ev",
        "source_family": "legacy_fundamental_reversal",
    },
    {
        "expression": "rank(ts_mean(ts_rank(vwap / close, 20), 3) - ts_rank(returns, 20))",
        "tag": "legacy-vwap-close-reversal",
        "source_family": "legacy_price_volume_reversal",
    },
    {
        "expression": "rank((high - close) / (high - low) * volume / ts_mean(volume, 20))",
        "tag": "legacy-intraday-volume-pressure",
        "source_family": "legacy_price_volume_reversal",
    },
    {
        "expression": (
            "rank(0.50 * rank(-ts_delta(close, 3) / close) + "
            "0.30 * rank(ts_mean((implied_volatility_call_120 - implied_volatility_put_120) / "
            "(implied_volatility_call_120 + implied_volatility_put_120), 5)) + "
            "0.20 * rank(-1 * ts_rank(cash_burn_rate, 60)))"
        ),
        "tag": "legacy-option-reversal-cashburn",
        "source_family": "legacy_option_reversal",
    },
]

GENERATION_MODEL_PRIMARY = "model-primary"
GENERATION_MIXED = "mixed"
GENERATION_TEMPLATE_FALLBACK = "template-fallback"
GENERATION_EVOLUTIONARY = "evolutionary"
GENERATION_MIXED_EVOLUTIONARY = "mixed-evolutionary"
GENERATION_MODES = {
    GENERATION_MODEL_PRIMARY,
    GENERATION_MIXED,
    GENERATION_TEMPLATE_FALLBACK,
    GENERATION_EVOLUTIONARY,
    GENERATION_MIXED_EVOLUTIONARY,
}

OPTION_FIELDS = {
    "implied_volatility_call_120",
    "implied_volatility_call_180",
    "implied_volatility_call_30",
    "implied_volatility_call_60",
    "implied_volatility_call_90",
    "implied_volatility_put_120",
    "implied_volatility_put_180",
    "implied_volatility_put_30",
    "implied_volatility_put_60",
    "implied_volatility_put_90",
    "pcr_oi_10",
    "pcr_oi_120",
    "pcr_oi_180",
    "pcr_oi_20",
    "pcr_oi_30",
    "pcr_oi_5",
    "pcr_oi_60",
    "pcr_oi_90",
    "pcr_volume_10",
    "pcr_volume_120",
    "pcr_volume_180",
    "pcr_volume_20",
    "pcr_volume_30",
    "pcr_volume_5",
    "pcr_volume_60",
    "pcr_volume_90",
}

PLATFORM_DERIVATIVE_FIELDS = {
    "analyst_revision_rank_derivative",
    "cashflow_efficiency_rank_derivative",
    "composite_factor_score_derivative",
    "earnings_certainty_rank_derivative",
    "growth_potential_rank_derivative",
    "multi_factor_acceleration_score_derivative",
    "relative_valuation_rank_derivative",
}

PLATFORM_FORWARD_VALUE_FIELDS = {
    "forward_book_value_to_price",
    "forward_cash_flow_to_price",
    "forward_earnings_yield",
    "forward_sales_to_price",
}

PLATFORM_ANALYST_REVISION_FIELDS = {
    "actual_eps_value_quarterly",
    "anl4_af_eps_value",
    "anl4_adjusted_netincome_ft",
    "anl4_afv4_eps_mean",
    "change_in_eps_surprise",
    "snt1_d1_netearningsrevision",
}

PLATFORM_CASHFLOW_FIELDS = {
    "actual_cashflow_per_share_value_quarterly",
    "cashflow",
    "cashflow_fin",
    "cashflow_op",
}


@dataclass
class WQAgentWorkflowConfig:
    output_dir: Path
    candidate_files: list[Path] = field(default_factory=list)
    seed_ready_files: list[Path] = field(default_factory=list)
    community_context_dir: Path | None = None
    account: str = "primary"
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    decay: int = 8
    neutralization: str = "SUBINDUSTRY"
    truncation: float = 0.08
    target_candidates: int = 20
    max_simulations: int = 40
    platform_sync_limit: int = 2000
    check_chunk_size: int = 1
    run_checks: bool = True
    use_ledger: bool = True
    dry_run: bool = False
    allow_submit_probe: bool = False
    submit_count: int = 0
    submit_alpha_ids: list[str] = field(default_factory=list)
    generation_mode: str = GENERATION_MODEL_PRIMARY
    model_candidates: int = 0
    evolutionary_candidates: int = 0
    model_retries: int = 2
    fallback_template_limit: int = 3
    no_model: bool = False
    target_submissions: int = 0
    target_ready: int = 0
    max_total_simulations: int = 2000
    cycle_candidate_count: int = 40
    max_cycles: int = 50
    max_consecutive_empty_cycles: int = 3
    max_consecutive_submit_failures: int = 5
    virtual_similarity_cutoff: float = 0.65
    max_virtual_family_count: int = 2
    max_virtual_field_signature_count: int = 2
    submission_policy_file: Path | None = None
    enrich_pnl: bool = False
    pnl_enrichment_limit: int = 8
    pnl_min_stability_score: float = 0.0


@dataclass
class WorkflowPaths:
    output_dir: Path
    manifest: Path
    platform_alphas: Path
    active_inventory: Path
    field_opportunities: Path
    memory_context: Path
    memory_context_markdown: Path
    model_design_requests: Path
    model_candidates_raw: Path
    candidate_pool: Path
    simulation_results: Path
    simulation_progress: Path
    review_queue: Path
    repair_queue: Path
    model_repair_requests: Path
    model_repairs_raw: Path
    pnl_analysis_summary: Path
    pnl_alpha_metrics: Path
    pnl_yearly_metrics: Path
    pnl_analysis_markdown: Path
    submit_results: Path
    postmortem: Path
    loop_status: Path
    submitted_accumulator: Path
    virtual_active_inventory: Path
    presubmit_ready_sequential: Path
    presubmit_rejected: Path
    summary: Path

    @classmethod
    def for_output_dir(cls, output_dir: Path) -> "WorkflowPaths":
        return cls(
            output_dir=output_dir,
            manifest=output_dir / "manifest.json",
            platform_alphas=output_dir / "platform_alphas.jsonl",
            active_inventory=output_dir / "active_inventory.json",
            field_opportunities=output_dir / "field_opportunities.jsonl",
            memory_context=output_dir / "memory_context.json",
            memory_context_markdown=output_dir / "memory_context.md",
            model_design_requests=output_dir / "model_design_requests.jsonl",
            model_candidates_raw=output_dir / "model_candidates_raw.jsonl",
            candidate_pool=output_dir / "candidate_pool.jsonl",
            simulation_results=output_dir / "simulation_results.jsonl",
            simulation_progress=output_dir / "simulation_progress.json",
            review_queue=output_dir / "review_queue.jsonl",
            repair_queue=output_dir / "repair_queue.jsonl",
            model_repair_requests=output_dir / "model_repair_requests.jsonl",
            model_repairs_raw=output_dir / "model_repairs_raw.jsonl",
            pnl_analysis_summary=output_dir / "pnl_analysis_summary.json",
            pnl_alpha_metrics=output_dir / "pnl_alpha_metrics.jsonl",
            pnl_yearly_metrics=output_dir / "pnl_yearly_metrics.jsonl",
            pnl_analysis_markdown=output_dir / "pnl_analysis.md",
            submit_results=output_dir / "submit_results.jsonl",
            postmortem=output_dir / "postmortem.json",
            loop_status=output_dir / "loop_status.json",
            submitted_accumulator=output_dir / "submitted_accumulator.jsonl",
            virtual_active_inventory=output_dir / "virtual_active_inventory.json",
            presubmit_ready_sequential=output_dir / "presubmit_ready_sequential.jsonl",
            presubmit_rejected=output_dir / "presubmit_rejected.jsonl",
            summary=output_dir / "summary.json",
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
        return _finish(
            paths,
            config,
            mode,
            {"submission": submit_summary, "platform_sync_after_submit": platform_summary, "postmortem": postmortem},
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
        cycle_dir = cycles_dir / f"cycle_{cycle_index:03d}"
        cycle_config = replace(
            config,
            output_dir=cycle_dir,
            target_candidates=max(1, per_cycle_limit),
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
    summary = _finish(paths, config, "run-submit", {"run_submit_loop": final_status})
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

    platform_agent = PlatformSyncAgent(config, paths, dependencies=dependencies)
    platform_summary = platform_agent.run()
    platform_rows = _read_jsonl(paths.platform_alphas)
    real_inventory = _read_json(paths.active_inventory)
    real_active_rows = list(real_inventory.get("active") or [])
    virtual_records = _load_seed_ready_records(config.seed_ready_files)
    cycle_summaries: list[dict] = []
    carryover_repairs: list[dict] = []
    rejected_normalized_expressions: set[str] = {
        _candidate_dedupe_key(row)
        for row in virtual_records
        if str(row.get("expression") or "").strip()
    }
    total_simulations = 0
    consecutive_empty_cycles = 0
    stop_reason = "max_cycles_reached"

    for ready_record in virtual_records:
        _append_jsonl(paths.presubmit_ready_sequential, ready_record)

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
        for row in cycle_review_rows:
            _append_jsonl(paths.review_queue, {**row, "cycle_index": cycle_index})

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


class PlatformSyncAgent:
    """Fetch platform alphas and mirror active records into the local ledger."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths, *, dependencies: dict[str, Any] | None = None):
        self.config = config
        self.paths = paths
        self.dependencies = dependencies or {}

    def run(self) -> dict:
        if self.config.dry_run:
            rows = self.dependencies.get("platform_rows", [])
        else:
            rows = self._fetch_platform_alphas()

        _write_jsonl(self.paths.platform_alphas, rows)
        inventory = build_active_inventory(rows)
        _write_json(self.paths.active_inventory, inventory)
        ledger_summary = self._record_active_rows(rows) if self.config.use_ledger else {"ok": True, "skipped": True}
        return {
            "ok": True,
            "total": len(rows),
            "active": inventory["active_count"],
            "output": str(self.paths.platform_alphas),
            "active_inventory": str(self.paths.active_inventory),
            "ledger": ledger_summary,
        }

    def _fetch_platform_alphas(self) -> list[dict]:
        fetcher = self.dependencies.get("list_alphas")
        if fetcher:
            return list(fetcher(self.config))
        if not is_configured(self.config.account):
            raise RuntimeError(f"WQ BRAIN credentials are not configured (account={self.config.account})")

        client = get_client(self.config.account)
        try:
            if not client.authenticate():
                raise RuntimeError("WQ BRAIN authentication failed")
            out: list[dict] = []
            page_size = 100
            for offset in range(0, max(1, self.config.platform_sync_limit), page_size):
                result = run_list_alphas(client, limit=page_size, offset=offset)
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or "list alphas failed")
                page = result.get("alphas") or []
                out.extend(page)
                if len(page) < page_size or len(out) >= self.config.platform_sync_limit:
                    break
            return out[: self.config.platform_sync_limit]
        finally:
            client.close()

    def _record_active_rows(self, rows: list[dict]) -> dict:
        records = []
        for row in rows:
            status = str(row.get("status") or "").upper()
            if status not in {"ACTIVE", "SUBMITTED"}:
                continue
            records.append({
                **row,
                "api_check_status": "platform_active_check_readable",
                "platform_status": status,
                "source_submit_eligible": True,
                "source_file": str(self.paths.platform_alphas),
            })
        if not records:
            return {"ok": True, "recorded": 0}
        try:
            from .wq_alpha_ledger import record_api_check_records_sync

            return record_api_check_records_sync(
                records,
                settings=_settings(self.config),
                source_run_id=self.paths.output_dir.name,
            )
        except Exception as exc:
            return {"ok": False, "recorded": 0, "error": str(exc)}


class CommunityScoutAgent:
    """Extract low-overlap field opportunities from existing community triage."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths):
        self.config = config
        self.paths = paths

    def run(self, *, active_inventory: dict | None = None) -> dict:
        active_fields = set((active_inventory or {}).get("field_counts") or {})
        rows: list[dict] = []
        context = CommunityContext.from_dir(self.config.community_context_dir) if self.config.community_context_dir else None
        if context:
            for seed in context.seed_candidates(limit=max(10, self.config.target_candidates * 2)):
                fields = _fields(seed.expression)
                rows.append({
                    "created_at": _now(),
                    "source": "community_context",
                    "tag": seed.tag,
                    "expression": seed.expression,
                    "fields": fields,
                    "operators": _operators(seed.expression),
                    "low_overlap_fields": sorted(set(fields) - active_fields),
                    "field_overlap_with_active": _jaccard(set(fields), active_fields),
                    "diagnosis": seed.diagnosis,
                })
        _write_jsonl(self.paths.field_opportunities, rows)
        return {"ok": True, "opportunities": len(rows), "output": str(self.paths.field_opportunities)}


class MemoryContextBuilder:
    """Build the compact memory packet used by model-driven agents."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths, *, dependencies: dict[str, Any] | None = None):
        self.config = config
        self.paths = paths
        self.dependencies = dependencies or {}

    def run(self, *, active_inventory: dict | None = None) -> dict:
        active_inventory = active_inventory or {"active": []}
        context = {
            "created_at": _now(),
            "settings": _settings(self.config),
            "active": _summarize_rows(active_inventory.get("active") or [], limit=20),
            "active_field_counts": active_inventory.get("field_counts") or {},
            "active_operator_counts": active_inventory.get("operator_counts") or {},
            "field_opportunities": _summarize_rows(_read_jsonl(self.paths.field_opportunities), limit=30),
            "ledger_failures": _summarize_rows(self._ledger_rows(["self_corr_fail", "prod_corr_fail", "weak", "invalid"], 40), limit=40),
            "ledger_near_miss": _summarize_rows(self._ledger_rows(["pre_submit_pass", "correlation_pending"], 20), limit=20),
            "current_near_miss": _summarize_rows(
                [row for row in _read_jsonl(self.paths.review_queue) if row.get("triage_bucket") == NEAR_MISS_REPAIR],
                limit=20,
            ),
            "instructions": [
                "Generate new WorldQuant BRAIN FASTEXPR alphas using memory as constraints.",
                "Prefer old successful families as examples, but do not copy exact active expressions.",
                "Use community fields as low-correlation data inspiration.",
                "After self-correlation failures, change field or operator family, not only windows.",
            ],
        }
        _write_json(self.paths.memory_context, context)
        markdown = render_memory_context_markdown(context)
        self.paths.memory_context_markdown.write_text(markdown, encoding="utf-8")
        return {
            "ok": True,
            "active": len(context["active"]),
            "ledger_failures": len(context["ledger_failures"]),
            "field_opportunities": len(context["field_opportunities"]),
            "output": str(self.paths.memory_context),
            "markdown": str(self.paths.memory_context_markdown),
        }

    def _ledger_rows(self, statuses: list[str], limit: int) -> list[dict]:
        provider = self.dependencies.get("ledger_rows")
        if provider:
            return list(provider(statuses, limit, self.config))
        if not self.config.use_ledger:
            return []
        try:
            from .wq_alpha_ledger import query_alpha_experiment_rows_sync

            return query_alpha_experiment_rows_sync(statuses=statuses, limit=limit, require_alpha_id=False)
        except Exception:
            return []


class ModelCandidateDesignerAgent:
    """Build a candidate pool with model-generated candidates as the primary source."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths, *, dependencies: dict[str, Any] | None = None):
        self.config = config
        self.paths = paths
        self.dependencies = dependencies or {}

    def run(self, *, active_inventory: dict | None = None) -> dict:
        active_rows = (active_inventory or {}).get("active") or []
        model_rows, model_summary = self._model_candidates()
        file_rows = self._file_candidates()
        repair_rows = self._repair_candidates()
        platform_rows = self._platform_candidates()
        fallback_rows = self._fallback_candidates()
        evolutionary_rows, evolutionary_summary = self._evolutionary_candidates(
            active_rows=active_rows,
            file_rows=file_rows,
            platform_rows=platform_rows,
            fallback_rows=fallback_rows,
        )

        rows: list[dict] = []
        if self.config.generation_mode == GENERATION_EVOLUTIONARY:
            rows.extend(evolutionary_rows)
            rows.extend(file_rows)
            rows.extend(repair_rows)
            rows.extend(platform_rows)
            rows.extend(fallback_rows)
        elif self.config.generation_mode == GENERATION_MIXED_EVOLUTIONARY:
            rows.extend(model_rows)
            rows.extend(file_rows)
            rows.extend(evolutionary_rows)
            rows.extend(repair_rows)
            rows.extend(platform_rows)
            rows.extend(fallback_rows)
        elif self.config.generation_mode == GENERATION_TEMPLATE_FALLBACK or self.config.no_model:
            rows.extend(file_rows)
            rows.extend(repair_rows)
            rows.extend(platform_rows)
            rows.extend(fallback_rows)
        elif self.config.generation_mode == GENERATION_MIXED:
            rows.extend(model_rows)
            rows.extend(file_rows)
            rows.extend(repair_rows)
            rows.extend(platform_rows)
            rows.extend(fallback_rows)
        else:
            rows.extend(model_rows)
            rows.extend(file_rows)
            rows.extend(repair_rows)
            rows.extend(platform_rows)
            if len(rows) < self.config.target_candidates:
                rows.extend(fallback_rows[: max(0, self.config.target_candidates - len(rows))])

        unique = []
        seen: set[str] = set()
        for index, row in enumerate(rows):
            expression = str(row.get("expression") or "").strip()
            if not expression:
                continue
            dedupe_key = _candidate_dedupe_key(row)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            nearest = nearest_similarity(expression, active_rows)
            if nearest and nearest["exact"]:
                continue
            try:
                validate_wq_expression(expression)
            except Exception as exc:
                row = {**row, "validation_error": str(exc), "triage_bucket": HARD_FAIL}
                continue
            unique.append({
                "created_at": _now(),
                "candidate_rank": len(unique) + 1,
                "agent_stage": "candidate_design",
                "expression": expression,
                "tag": row.get("tag") or f"agent-candidate-{index + 1}",
                "source_family": row.get("source_family") or row.get("mutation_strategy") or "model_generated",
                "source": row.get("source"),
                "rationale": row.get("rationale"),
                "expected_low_corr_reason": row.get("expected_low_corr_reason"),
                "source_fields": row.get("source_fields") or _fields(expression),
                "mutation_strategy": row.get("mutation_strategy"),
                "parent_alpha_ids": row.get("parent_alpha_ids") or [],
                "risk_flags": row.get("risk_flags") or [],
                "simulation_settings": _candidate_settings_override(row),
                "active_similarity": nearest,
                "candidate_meta": row.get("candidate_meta") or {"model_generation": row.get("model_generation")},
            })
            if len(unique) >= self.config.target_candidates:
                break

        _write_jsonl(self.paths.candidate_pool, unique)
        return {
            "ok": True,
            "candidates": len(unique),
            "model": model_summary,
            "evolutionary": evolutionary_summary,
            "repair_candidates": len(repair_rows),
            "platform_candidates": len(platform_rows),
            "fallback_candidates": len(fallback_rows),
            "output": str(self.paths.candidate_pool),
            "raw_model_output": str(self.paths.model_candidates_raw),
        }

    def _model_candidates(self) -> tuple[list[dict], dict]:
        if self.config.no_model or self.config.generation_mode == GENERATION_TEMPLATE_FALLBACK:
            return [], {"ok": True, "skipped": True, "reason": "model disabled"}
        prompt = build_candidate_generation_prompt(
            self.paths.memory_context_markdown.read_text(encoding="utf-8") if self.paths.memory_context_markdown.is_file() else "",
            target=self.config.model_candidates or max(self.config.target_candidates * 2, self.config.target_candidates),
            examples=self._fallback_candidates(),
        )
        request = {"created_at": _now(), "kind": "candidate_generation", "prompt": prompt}
        _append_jsonl(self.paths.model_design_requests, request)

        generator = self.dependencies.get("model_generate_candidates") or default_model_generate_candidates
        raw_records: list[dict] = []
        parsed: list[dict] = []
        last_error = ""
        for attempt in range(max(1, self.config.model_retries + 1)):
            try:
                response = generator(prompt, self.config)
                candidates = parse_model_candidate_response(response)
                parsed = [{**row, "source": "model_candidate_designer", "model_generation": {"attempt": attempt + 1}} for row in candidates]
                raw_records.append({"created_at": _now(), "attempt": attempt + 1, "ok": True, "response": response})
                if parsed:
                    break
            except Exception as exc:
                last_error = str(exc)
                raw_records.append({"created_at": _now(), "attempt": attempt + 1, "ok": False, "error": last_error})
        _write_jsonl(self.paths.model_candidates_raw, raw_records)
        return parsed, {
            "ok": bool(parsed),
            "generated": len(parsed),
            "attempts": len(raw_records),
            "error": "" if parsed else last_error,
        }

    def _evolutionary_candidates(
        self,
        *,
        active_rows: list[dict],
        file_rows: list[dict],
        platform_rows: list[dict],
        fallback_rows: list[dict],
    ) -> tuple[list[dict], dict]:
        if self.config.generation_mode not in {GENERATION_EVOLUTIONARY, GENERATION_MIXED_EVOLUTIONARY}:
            return [], {"ok": True, "skipped": True, "reason": "generation mode does not request evolutionary"}
        provider = self.dependencies.get("evolutionary_generate_candidates")
        target = self.config.evolutionary_candidates or max(self.config.target_candidates * 2, self.config.target_candidates)
        if provider:
            rows = list(provider(active_rows, file_rows, platform_rows, fallback_rows, self.config))
            return rows, {"ok": True, "generated": len(rows), "provider": "dependency", "target_count": target}
        field_rows = _read_jsonl(self.paths.field_opportunities) if self.paths.field_opportunities.is_file() else []
        repair_rows = _read_jsonl(self.paths.repair_queue) if self.paths.repair_queue.is_file() else []
        return generate_evolutionary_candidates(
            active_rows=active_rows,
            candidate_rows=[*file_rows, *platform_rows, *fallback_rows],
            field_opportunity_rows=field_rows,
            repair_rows=repair_rows,
            target_count=target,
            region=self.config.region,
            universe=self.config.universe,
        )

    def _file_candidates(self) -> list[dict]:
        rows: list[dict] = []
        for path in self.config.candidate_files:
            if not path.is_file():
                continue
            for row in _read_candidate_rows(path):
                rows.append({**row, "source": str(path)})
        return rows

    def _repair_candidates(self) -> list[dict]:
        if not self.paths.repair_queue.is_file():
            return []
        rows: list[dict] = []
        for item in _read_jsonl(self.paths.repair_queue):
            for record in item.get("candidate_records") or []:
                expr = str(record.get("expression") or "").strip()
                if not expr:
                    continue
                rows.append({
                    **record,
                    "expression": expr,
                    "tag": record.get("tag") or f"repair-{item.get('alpha_id') or item.get('tag') or 'candidate'}",
                    "source_family": record.get("source_family") or "near_miss_repair",
                    "source": str(self.paths.repair_queue),
                    "candidate_meta": {
                        **(record.get("candidate_meta") or {}),
                        "repair_source": item,
                    },
                })
            for expr in item.get("candidate_expressions") or item.get("repair_expressions") or []:
                rows.append({
                    "expression": expr,
                    "tag": f"repair-{item.get('alpha_id') or item.get('tag') or 'candidate'}",
                    "source_family": "near_miss_repair",
                    "source": str(self.paths.repair_queue),
                    "candidate_meta": {"repair_source": item},
                })
        rows.sort(key=_repair_candidate_sort_key)
        return rows

    def _platform_candidates(self) -> list[dict]:
        rows = []
        for row in _read_jsonl(self.paths.platform_alphas):
            if str(row.get("status") or "").upper() != "UNSUBMITTED":
                continue
            expression = clean_expression(str(row.get("expression") or ""))
            if not expression:
                continue
            metrics = {
                "sharpe": row.get("sharpe"),
                "fitness": row.get("fitness"),
                "turnover": row.get("turnover"),
            }
            gate = submit_threshold_checks(metrics)
            if not gate["eligible"]:
                continue
            rows.append({
                "expression": expression,
                "tag": f"platform-memory-{row.get('alpha_id') or len(rows) + 1}",
                "source_family": _platform_candidate_family(expression),
                "source": str(self.paths.platform_alphas),
                "rationale": "Recent platform alpha already passed base submit metrics; re-simulate and check against current active inventory.",
                "expected_low_corr_reason": "Selected from non-active platform memory; exact active duplicates are filtered before simulation.",
                "source_fields": _fields(expression),
                "mutation_strategy": "platform_memory_retest",
                "parent_alpha_ids": [row.get("alpha_id")] if row.get("alpha_id") else [],
                "risk_flags": ["requires fresh self-correlation check"],
                "sharpe": metrics["sharpe"],
                "fitness": metrics["fitness"],
                "turnover": metrics["turnover"],
                "candidate_meta": {
                    "platform_alpha_id": row.get("alpha_id"),
                    "platform_status": row.get("status"),
                    "platform_metrics": metrics,
                },
            })
        rows.sort(key=review_sort_key)
        return rows[: max(self.config.target_candidates * 3, self.config.target_candidates)]

    def _fallback_candidates(self) -> list[dict]:
        rows: list[dict] = []
        limit = max(0, self.config.fallback_template_limit)
        for row in SUCCESS_FAMILY_SEEDS[:limit]:
            rows.append({**row, "source": "fallback_legacy_example"})
        if not self.paths.repair_queue.is_file():
            return rows
        for item in _read_jsonl(self.paths.repair_queue):
            for record in item.get("candidate_records") or []:
                expr = str(record.get("expression") or "").strip()
                if not expr:
                    continue
                if len(rows) >= limit:
                    return rows
                rows.append({
                    **record,
                    "expression": expr,
                    "tag": record.get("tag") or f"repair-{item.get('alpha_id') or item.get('tag') or 'candidate'}",
                    "source_family": record.get("source_family") or "near_miss_repair",
                    "source": str(self.paths.repair_queue),
                    "candidate_meta": {
                        **(record.get("candidate_meta") or {}),
                        "repair_source": item,
                    },
                })
            for expr in item.get("candidate_expressions") or item.get("repair_expressions") or []:
                if len(rows) >= limit:
                    return rows
                rows.append({
                    "expression": expr,
                    "tag": f"repair-{item.get('alpha_id') or item.get('tag') or 'candidate'}",
                    "source_family": "near_miss_repair",
                    "source": str(self.paths.repair_queue),
                    "candidate_meta": {"repair_source": item},
                })
        return rows


CandidateDesignerAgent = ModelCandidateDesignerAgent


class SimulationAgent:
    """Run WQ simulations with auto_submit disabled."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths, *, dependencies: dict[str, Any] | None = None):
        self.config = config
        self.paths = paths
        self.dependencies = dependencies or {}

    def run(self) -> dict:
        candidates = _read_jsonl(self.paths.candidate_pool)[: self.config.max_simulations]
        if self.config.dry_run:
            rows = [self._dry_run_row(candidate) for candidate in candidates]
        else:
            _write_jsonl(self.paths.simulation_results, [])
            rows = self._simulate_candidates(candidates)
        _write_jsonl(self.paths.simulation_results, rows)
        counts = Counter(row.get("status") for row in rows)
        return {"ok": True, "simulated": len(rows), "counts": dict(sorted(counts.items())), "output": str(self.paths.simulation_results)}

    def _simulate_candidates(self, candidates: list[dict]) -> list[dict]:
        simulator = self.dependencies.get("simulate")
        if simulator:
            rows = []
            total = len(candidates)
            for index, candidate in enumerate(candidates, start=1):
                self._write_progress(index, total, candidate, status="started")
                effective_settings = _simulation_settings_for_candidate(candidate, self.config)
                sim_candidate = {**candidate, "effective_simulation_settings": effective_settings}
                row = classify_simulation_result(sim_candidate, simulator(sim_candidate, self.config))
                rows.append(row)
                _append_jsonl(self.paths.simulation_results, row)
                self._write_progress(index, total, candidate, status=row.get("status"), alpha_id=row.get("alpha_id"))
            return rows
        if not is_configured(self.config.account):
            raise RuntimeError(f"WQ BRAIN credentials are not configured (account={self.config.account})")
        client = get_client(self.config.account)
        try:
            if not client.authenticate():
                raise RuntimeError("WQ BRAIN authentication failed")
            rows = []
            total = len(candidates)
            for index, candidate in enumerate(candidates, start=1):
                self._write_progress(index, total, candidate, status="started")
                def _progress(percent: int, message: str, *, candidate=candidate, index=index) -> None:
                    self._write_progress(index, total, candidate, status="running", percent=percent, message=message)

                settings = _simulation_settings_for_candidate(candidate, self.config)
                result = run_single_simulation(
                    client,
                    candidate["expression"],
                    region=settings["region"],
                    universe=settings["universe"],
                    delay=settings["delay"],
                    decay=settings["decay"],
                    neutralization=settings["neutralization"],
                    truncation=settings["truncation"],
                    max_trade=settings["maxTrade"],
                    max_position=settings["maxPosition"],
                    auto_submit=False,
                    tag=candidate.get("tag"),
                    progress_callback=_progress,
                )
                row = classify_simulation_result(candidate, result)
                row["effective_simulation_settings"] = settings
                rows.append(row)
                _append_jsonl(self.paths.simulation_results, row)
                self._write_progress(index, total, candidate, status=row.get("status"), alpha_id=row.get("alpha_id"))
            return rows
        finally:
            client.close()

    def _write_progress(
        self,
        index: int,
        total: int,
        candidate: dict,
        *,
        status: str,
        percent: int | None = None,
        message: str | None = None,
        alpha_id: str | None = None,
    ) -> None:
        _write_json(self.paths.simulation_progress, {
            "updated_at": _now(),
            "current_index": index,
            "total": total,
            "status": status,
            "percent": percent,
            "message": message,
            "alpha_id": alpha_id,
            "candidate_rank": candidate.get("candidate_rank"),
            "tag": candidate.get("tag"),
            "expression": candidate.get("expression"),
        })

    def _dry_run_row(self, candidate: dict) -> dict:
        return {
            **candidate,
            "status": "dry_run",
            "submit_eligible": False,
            "submitted": False,
            "result": {"ok": True, "dry_run": True},
        }


class ReviewAgent:
    """Check and bucket simulated candidates into actionable queues."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths, *, dependencies: dict[str, Any] | None = None):
        self.config = config
        self.paths = paths
        self.dependencies = dependencies or {}

    def run(self) -> dict:
        rows = _read_jsonl(self.paths.simulation_results)
        check_results = self._check_rows(rows) if self.config.run_checks and not self.config.dry_run else {}
        reviewed = [classify_review_row(row, check_results.get(str(row.get("alpha_id") or ""))) for row in rows]
        pnl_summary = self._enrich_pnl(reviewed) if self.config.enrich_pnl and not self.config.dry_run else {
            "ok": True,
            "skipped": True,
            "reason": "pnl enrichment disabled",
        }
        reviewed.sort(key=review_sort_key)
        _write_jsonl(self.paths.review_queue, reviewed)
        counts = Counter(row.get("triage_bucket") for row in reviewed)
        return {
            "ok": True,
            "reviewed": len(reviewed),
            "counts": dict(sorted(counts.items())),
            "output": str(self.paths.review_queue),
            "pnl_enrichment": pnl_summary,
        }

    def _check_rows(self, rows: list[dict]) -> dict[str, dict]:
        ids = [str(row.get("alpha_id") or "") for row in rows if _needs_check(row)]
        ids = [alpha_id for alpha_id in dict.fromkeys(ids) if alpha_id]
        if not ids:
            return {}
        checker = self.dependencies.get("check_submissions") or self.dependencies.get("check_alphas")
        if checker:
            return checker(ids, self.config)
        client = get_client(self.config.account)
        try:
            if not client.authenticate():
                raise RuntimeError("WQ BRAIN authentication failed")
            out: dict[str, dict] = {}
            for chunk in _chunks(ids, max(1, self.config.check_chunk_size)):
                result = run_check_submissions(client, chunk)
                out.update(result.get("alphas") or {})
            return out
        finally:
            client.close()

    def _enrich_pnl(self, reviewed: list[dict]) -> dict:
        targets = _pnl_enrichment_targets(reviewed, limit=self.config.pnl_enrichment_limit)
        if not targets:
            _write_jsonl(self.paths.pnl_alpha_metrics, [])
            _write_jsonl(self.paths.pnl_yearly_metrics, [])
            return {"ok": True, "skipped": True, "reason": "no eligible alpha ids"}

        reports: list[dict] = []
        enricher = self.dependencies.get("pnl_enrichment")
        if enricher:
            result = enricher(targets, self.config)
            if isinstance(result, dict) and "alpha_reports" in result:
                reports = [row for row in result.get("alpha_reports") or [] if isinstance(row, dict)]
            elif isinstance(result, dict):
                reports = [row for row in result.values() if isinstance(row, dict)]
            elif isinstance(result, list):
                reports = [row for row in result if isinstance(row, dict)]
        else:
            reports = self._probe_pnl_reports(targets)

        by_id = {str(report.get("alpha_id") or ""): report for report in reports if report.get("alpha_id")}
        enriched = 0
        for row in reviewed:
            alpha_id = str(row.get("alpha_id") or "")
            report = by_id.get(alpha_id)
            if not report:
                continue
            _apply_pnl_report_to_review_row(row, report, min_score=self.config.pnl_min_stability_score)
            enriched += 1

        report_payload = build_pnl_analysis_report(reports, probe_dir=self.paths.output_dir)
        files = write_pnl_analysis_artifacts(report_payload, self.paths.output_dir)
        return {
            "ok": True,
            "requested": len(targets),
            "reported": len(reports),
            "enriched": enriched,
            "pnl_found": sum(1 for report in reports if report.get("pnl_curve_found") and report.get("yearly")),
            "files": files,
        }

    def _probe_pnl_reports(self, targets: list[dict]) -> list[dict]:
        if not is_configured(self.config.account):
            return []
        client = get_client(self.config.account)
        reports: list[dict] = []
        try:
            if not client.authenticate():
                raise RuntimeError("WQ BRAIN authentication failed")
            for row in targets:
                alpha_id = str(row.get("alpha_id") or "")
                if not alpha_id:
                    continue
                probe = client.probe_alpha_detail(alpha_id)
                summary = summarize_alpha_probe(probe)
                reports.append(
                    analyze_alpha_probe_summary(
                        summary,
                        probe=probe,
                        tag=str(row.get("tag") or ""),
                    )
                )
        finally:
            client.close()
        return reports


class SubmissionAgent:
    """Submit explicitly authorized candidates only."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths, *, dependencies: dict[str, Any] | None = None):
        self.config = config
        self.paths = paths
        self.dependencies = dependencies or {}

    def run(self) -> dict:
        selected = select_submission_candidates(
            _read_jsonl(self.paths.review_queue),
            explicit_ids=self.config.submit_alpha_ids,
            submit_count=self.config.submit_count,
            allow_submit_probe=self.config.allow_submit_probe,
        )
        if not selected:
            summary = {"ok": False, "submitted": 0, "reason": "no authorized candidates selected"}
            _write_jsonl(self.paths.submit_results, [])
            return summary
        if self.config.dry_run:
            rows = [{"alpha_id": alpha_id, "status": "dry_run_not_submitted"} for alpha_id in selected]
            _write_jsonl(self.paths.submit_results, rows)
            return {"ok": True, "dry_run": True, "submitted": 0, "selected": selected}

        submitter = self.dependencies.get("submit_by_ids")
        if submitter:
            result = submitter(selected, self.config)
        else:
            client = get_client(self.config.account)
            try:
                if not client.authenticate():
                    raise RuntimeError("WQ BRAIN authentication failed")
                result = run_submit_by_ids(client, selected)
            finally:
                client.close()

        rows = []
        for alpha_id, entry in (result.get("results") or {}).items():
            rows.append({"created_at": _now(), "alpha_id": alpha_id, **entry})
        _write_jsonl(self.paths.submit_results, rows)
        return {"ok": True, "selected": selected, "result": result, "output": str(self.paths.submit_results)}


class FailureReviewAgent:
    """Persist repairable misses and summarize failure modes."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths, *, dependencies: dict[str, Any] | None = None):
        self.config = config
        self.paths = paths
        self.dependencies = dependencies or {}

    def run(self) -> dict:
        rows = _read_jsonl(self.paths.review_queue) if self.paths.review_queue.is_file() else _read_jsonl(self.paths.simulation_results)
        repairable = [row for row in rows if should_repair(row)]
        repair_rows, model_summary = self._model_repairs(repairable)
        if not repair_rows:
            repair_rows = build_policy_repair_records(
                repairable,
                submission_policy=_submission_policy_for_config(self.config),
                max_repairs_per_row=3,
            )
            if repair_rows:
                model_summary = {
                    "ok": True,
                    "skipped": True,
                    "reason": "deterministic_policy_repair",
                    "generated": sum(len(row.get("candidate_expressions") or []) for row in repair_rows),
                }
        if not repair_rows:
            repair_rows = [build_repair_record(row) for row in repairable]
            repair_rows = [row for row in repair_rows if row]
        _write_jsonl(self.paths.repair_queue, repair_rows)
        postmortem = {
            "ok": True,
            "created_at": _now(),
            "total": len(rows),
            "bucket_counts": dict(sorted(Counter(row.get("triage_bucket") or row.get("status") for row in rows).items())),
            "repairable": len(repair_rows),
            "model_repairs": model_summary,
            "repair_queue": str(self.paths.repair_queue),
        }
        _write_json(self.paths.postmortem, postmortem)
        return postmortem

    def _model_repairs(self, repairable: list[dict]) -> tuple[list[dict], dict]:
        if not repairable:
            return [], {"ok": True, "skipped": True, "reason": "no repairable rows"}
        if self.config.no_model or self.config.generation_mode == GENERATION_TEMPLATE_FALLBACK:
            return [], {"ok": True, "skipped": True, "reason": "model disabled"}
        prompt = build_repair_generation_prompt(
            self.paths.memory_context_markdown.read_text(encoding="utf-8") if self.paths.memory_context_markdown.is_file() else "",
            repairable,
        )
        _append_jsonl(self.paths.model_repair_requests, {"created_at": _now(), "kind": "repair_generation", "prompt": prompt})
        generator = self.dependencies.get("model_generate_repairs") or default_model_generate_repairs
        raw_records: list[dict] = []
        parsed: list[dict] = []
        last_error = ""
        for attempt in range(max(1, self.config.model_retries + 1)):
            try:
                response = generator(prompt, self.config)
                parsed = parse_model_repair_response(response)
                raw_records.append({"created_at": _now(), "attempt": attempt + 1, "ok": True, "response": response})
                if parsed:
                    break
            except Exception as exc:
                last_error = str(exc)
                raw_records.append({"created_at": _now(), "attempt": attempt + 1, "ok": False, "error": last_error})
        _write_jsonl(self.paths.model_repairs_raw, raw_records)
        return parsed, {
            "ok": bool(parsed),
            "generated": len(parsed),
            "attempts": len(raw_records),
            "error": "" if parsed else last_error,
        }


def classify_simulation_result(candidate: dict, result: dict) -> dict:
    metrics = _metrics_from_result(result)
    submit_gate = submit_threshold_checks(metrics)
    submit_eligible = bool(result.get("submit_eligible", submit_gate["eligible"]))
    checks = result.get("is_metrics", {}).get("checks") or []
    failed_platform_checks = _failed_platform_checks(checks)
    sc = _review_check(checks, "SELF_CORRELATION")
    prod = _review_check(checks, "PROD_CORRELATION")

    status = "simulated"
    if not result.get("ok", False):
        status = "failed"
    elif submit_eligible and failed_platform_checks:
        status = "failed_platform_check"
    elif _check_result(sc) == "FAIL" or _check_result(prod) == "FAIL":
        status = "failed_correlation_check"
    elif submit_eligible and (_check_result(sc) == "PENDING" or _check_result(prod) == "PENDING"):
        status = "pending_correlation_check"
    elif submit_eligible:
        status = "eligible"
    elif submit_gate["eligible"]:
        status = "eligible"

    return {
        **candidate,
        "created_at": _now(),
        "status": status,
        "alpha_id": result.get("alpha_id"),
        "sharpe": metrics.get("sharpe"),
        "fitness": metrics.get("fitness"),
        "returns": metrics.get("returns"),
        "turnover": metrics.get("turnover"),
        "submit_eligible": submit_eligible,
        "submitted": bool(result.get("submitted")),
        "submit_checks": result.get("submit_checks") or submit_gate["checks"],
        "is_checks": checks,
        "failed_platform_checks": failed_platform_checks,
        "self_correlation": sc,
        "prod_correlation": prod,
        "result": result,
    }


def classify_review_row(source_row: dict, check_result: dict | None = None) -> dict:
    check_result = check_result or {}
    row = {**source_row}
    metrics = {
        "sharpe": _first_float(check_result.get("sharpe"), source_row.get("sharpe")),
        "fitness": _first_float(check_result.get("fitness"), source_row.get("fitness")),
        "returns": _first_float(check_result.get("returns"), source_row.get("returns")),
        "turnover": _first_float(check_result.get("turnover"), source_row.get("turnover")),
    }
    gate = submit_threshold_checks(metrics)
    review_checks = check_result.get("review_checks") or {}
    sc_result = _first_text(check_result.get("sc_result"), source_row.get("sc_result"), (source_row.get("self_correlation") or {}).get("result"))
    prod_result = _first_text(check_result.get("prod_corr_result"), source_row.get("prod_corr_result"), (source_row.get("prod_correlation") or {}).get("result"))
    sc_value = _first_float(check_result.get("sc_value"), source_row.get("sc_value"), (source_row.get("self_correlation") or {}).get("value"))
    prod_value = _first_float(check_result.get("prod_corr_value"), source_row.get("prod_corr_value"), (source_row.get("prod_correlation") or {}).get("value"))
    api_status = _api_check_status(check_result, sc_result=sc_result, prod_result=prod_result)
    platform_status = str(check_result.get("status") or check_result.get("platform_status") or source_row.get("platform_status") or "").upper()
    failed_platform = source_row.get("failed_platform_checks") or []
    base_ok = bool(source_row.get("submit_eligible") or gate["eligible"])

    bucket = HARD_FAIL
    reason = "not submit eligible"
    if platform_status in {"ACTIVE", "SUBMITTED"}:
        bucket = ACTIVE_OR_SUBMITTED
        reason = f"platform status is {platform_status}"
    elif failed_platform:
        bucket = NEAR_MISS_REPAIR if _is_repairable_platform_fail(source_row, failed_platform) else HARD_FAIL
        reason = "platform check failed"
    elif str(sc_result).upper() == "FAIL":
        bucket = NEAR_MISS_REPAIR if sc_value is not None and sc_value <= 0.85 else HARD_FAIL
        reason = f"self-correlation failed ({sc_value})"
    elif str(prod_result).upper() == "FAIL":
        bucket = HARD_FAIL
        reason = f"prod-correlation failed ({prod_value})"
    elif base_ok and api_status == "api_check_readable":
        bucket = CONFIRMED_READY
        reason = "check-only readable and no failed review checks"
    elif base_ok and api_status in {"api_check_pending", "api_check_missing"}:
        bucket = SUBMIT_PROBE_NEEDED
        reason = "base checks pass but correlation review is pending or missing"
    elif _is_metric_near_miss(metrics):
        bucket = NEAR_MISS_REPAIR
        reason = "metrics are near submit thresholds"

    row.update({
        "agent_stage": "review",
        "triage_bucket": bucket,
        "triage_reason": reason,
        "api_check_status": api_status,
        "platform_status": platform_status or None,
        "sharpe": metrics["sharpe"],
        "fitness": metrics["fitness"],
        "returns": metrics["returns"],
        "turnover": metrics["turnover"],
        "sc_result": sc_result,
        "sc_value": sc_value,
        "prod_corr_result": prod_result,
        "prod_corr_value": prod_value,
        "review_checks": review_checks,
        "submit_probe_reason": reason if bucket == SUBMIT_PROBE_NEEDED else None,
    })
    return row


def _pnl_enrichment_targets(reviewed: list[dict], *, limit: int) -> list[dict]:
    if limit <= 0:
        return []
    eligible_buckets = {CONFIRMED_READY, SUBMIT_PROBE_NEEDED, NEAR_MISS_REPAIR}
    targets: list[dict] = []
    seen: set[str] = set()
    for row in sorted(reviewed, key=review_sort_key):
        alpha_id = str(row.get("alpha_id") or "")
        if not alpha_id or alpha_id in seen:
            continue
        if row.get("triage_bucket") not in eligible_buckets:
            continue
        if not (row.get("submit_eligible") or _is_metric_near_miss({
            "sharpe": row.get("sharpe"),
            "fitness": row.get("fitness"),
            "turnover": row.get("turnover"),
        })):
            continue
        targets.append(row)
        seen.add(alpha_id)
        if len(targets) >= limit:
            break
    return targets


def _apply_pnl_report_to_review_row(row: dict, report: dict, *, min_score: float = 0.0) -> None:
    stability = report.get("stability") if isinstance(report.get("stability"), dict) else {}
    yearly = report.get("yearly") if isinstance(report.get("yearly"), list) else []
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    score = _score(stability.get("temporal_stability_score"), default=0.0)
    row["pnl_curve_found"] = bool(report.get("pnl_curve_found"))
    row["pnl_points"] = report.get("pnl_points")
    row["pnl_curve_path"] = report.get("pnl_curve_path") or ""
    row["temporal_stability"] = stability
    row["temporal_stability_score"] = score if yearly else None
    row["yearly_metrics"] = yearly
    row["pnl_warnings"] = warnings
    row["pnl_enrichment_status"] = "ok" if yearly else "missing_pnl_curve"
    if min_score > 0 and yearly and score < min_score:
        row["temporal_stability_warning"] = "below_min_stability_score"


def select_submission_candidates(
    review_rows: list[dict],
    *,
    explicit_ids: list[str],
    submit_count: int,
    allow_submit_probe: bool,
) -> list[str]:
    if explicit_ids:
        allowed = {str(row.get("alpha_id") or ""): row for row in review_rows}
        return [alpha_id for alpha_id in explicit_ids if alpha_id and _row_can_submit(allowed.get(alpha_id), allow_submit_probe=True)]
    if submit_count <= 0:
        return []
    eligible_buckets = {CONFIRMED_READY}
    if allow_submit_probe:
        eligible_buckets.add(SUBMIT_PROBE_NEEDED)
    selected = [
        str(row.get("alpha_id") or "")
        for row in sorted(review_rows, key=review_sort_key)
        if row.get("triage_bucket") in eligible_buckets and row.get("alpha_id")
    ]
    return selected[:submit_count]


def build_active_inventory(rows: list[dict]) -> dict:
    active = [row for row in rows if str(row.get("status") or "").upper() in {"ACTIVE", "SUBMITTED"}]
    field_counts: Counter[str] = Counter()
    operator_counts: Counter[str] = Counter()
    for row in active:
        field_counts.update(_fields(row.get("expression") or ""))
        operator_counts.update(_operators(row.get("expression") or ""))
    return {
        "created_at": _now(),
        "active_count": len(active),
        "field_counts": dict(sorted(field_counts.items())),
        "operator_counts": dict(sorted(operator_counts.items())),
        "active": active,
    }


def build_virtual_active_inventory(real_active_rows: list[dict], virtual_ready_records: list[dict]) -> dict:
    real_rows = [{**row, "active_source": row.get("active_source") or "platform"} for row in real_active_rows]
    virtual_rows = [_virtual_active_row(row) for row in virtual_ready_records]
    active = real_rows + virtual_rows
    field_counts: Counter[str] = Counter()
    operator_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    field_signature_counts: Counter[str] = Counter()
    for row in active:
        expression = str(row.get("expression") or "")
        fields = _fields(expression)
        field_counts.update(fields)
        operator_counts.update(_operators(expression))
        family = _row_family(row)
        if family:
            family_counts[family] += 1
        signature = _field_signature(expression)
        if signature:
            field_signature_counts[signature] += 1
    return {
        "created_at": _now(),
        "active_count": len(active),
        "real_active_count": len(real_rows),
        "virtual_active_count": len(virtual_rows),
        "field_counts": dict(sorted(field_counts.items())),
        "operator_counts": dict(sorted(operator_counts.items())),
        "source_family_counts": dict(sorted(family_counts.items())),
        "field_signature_counts": dict(sorted(field_signature_counts.items())),
        "active": active,
        "real_active": real_rows,
        "virtual_active": virtual_rows,
    }


def _load_seed_ready_records(paths: list[Path]) -> list[dict]:
    records: list[dict] = []
    seen: set[str] = set()
    for path in paths:
        if not path or not path.exists():
            continue
        for row in _read_jsonl(path):
            expression = str(row.get("expression") or "").strip()
            if not expression:
                continue
            key = _candidate_dedupe_key(row)
            if key in seen:
                continue
            seen.add(key)
            ready = dict(row)
            ready.setdefault("virtual_active_status", "VIRTUAL_ACTIVE")
            ready.setdefault("presubmit_accepted", True)
            ready["seed_ready"] = True
            records.append(ready)
    return records


_SUBMISSION_POLICY_CACHE: dict[str, dict[str, Any] | None] = {}


def _submission_policy_for_config(config: WQAgentWorkflowConfig | None) -> dict[str, Any] | None:
    if config is None or not config.submission_policy_file:
        return None
    key = str(config.submission_policy_file)
    if key not in _SUBMISSION_POLICY_CACHE:
        _SUBMISSION_POLICY_CACHE[key] = load_submission_policy(config.submission_policy_file)
    return _SUBMISSION_POLICY_CACHE[key]


def _filter_candidate_pool_for_presubmit(
    path: Path,
    *,
    skip_normalized_expressions: set[str],
    active_rows: list[dict] | None = None,
    config: WQAgentWorkflowConfig | None = None,
) -> dict:
    rows = _read_jsonl(path)
    if not rows:
        return {"ok": True, "input": 0, "kept": 0, "skipped": 0}
    kept = []
    skipped = []
    skip_reasons: Counter[str] = Counter()
    active_rows = active_rows or []
    active_family_counts = _active_family_counts(active_rows)
    active_field_signature_counts = _active_field_signature_counts(active_rows)
    submission_policy = _submission_policy_for_config(config)
    kept_family_counts: Counter[str] = Counter()
    kept_field_signature_counts: Counter[str] = Counter()
    for row in rows:
        expression = str(row.get("expression") or "")
        if expression and _candidate_dedupe_key(row) in skip_normalized_expressions:
            skipped.append({**row, "candidate_skip_reason": "previous_presubmit_rejection"})
            skip_reasons["previous_presubmit_rejection"] += 1
            continue
        if expression and _has_unsupported_statement_separator(expression):
            skipped.append({**row, "candidate_skip_reason": "unsupported_statement_separator"})
            skip_reasons["unsupported_statement_separator"] += 1
            continue
        if expression and _is_option_only_expression(expression):
            skipped.append({**row, "candidate_skip_reason": "pure_options_only_distribution_risk"})
            skip_reasons["pure_options_only_distribution_risk"] += 1
            continue
        if config is not None and expression:
            nearest = nearest_similarity(expression, active_rows)
            nearest_score = _score((nearest or {}).get("similarity", {}).get("overall_similarity"), default=0.0) if nearest else 0.0
            if nearest and nearest.get("exact"):
                skipped.append({**row, "candidate_skip_reason": "exact_active_duplicate", "nearest_active": nearest})
                skip_reasons["exact_active_duplicate"] += 1
                continue
            if nearest_score > config.virtual_similarity_cutoff:
                skipped.append({
                    **row,
                    "candidate_skip_reason": "too_similar_to_real_or_virtual_active",
                    "nearest_similarity": nearest_score,
                    "nearest_active": nearest,
                })
                skip_reasons["too_similar_to_real_or_virtual_active"] += 1
                continue
            family = _row_family(row)
            if config.max_virtual_family_count > 0 and family:
                family_count = active_family_counts.get(family, 0) + kept_family_counts.get(family, 0)
                if family_count >= config.max_virtual_family_count:
                    skipped.append({**row, "candidate_skip_reason": "source_family_capacity_reached"})
                    skip_reasons["source_family_capacity_reached"] += 1
                    continue
            field_signature = _field_signature(expression)
            if config.max_virtual_field_signature_count > 0 and field_signature:
                field_signature_count = (
                    active_field_signature_counts.get(field_signature, 0)
                    + kept_field_signature_counts.get(field_signature, 0)
                )
                if field_signature_count >= config.max_virtual_field_signature_count:
                    skipped.append({**row, "candidate_skip_reason": "field_signature_capacity_reached"})
                    skip_reasons["field_signature_capacity_reached"] += 1
                    continue
            policy_row = annotate_candidate_with_policy(
                {
                    **row,
                    "nearest_similarity": nearest_score,
                    "nearest_active": nearest,
                    "field_signature": field_signature,
                },
                submission_policy,
            )
            if policy_row.get("forum_policy_action") == "block":
                reason = str(policy_row.get("forum_policy_reason") or "forum_policy_block")
                skipped.append({**policy_row, "candidate_skip_reason": reason})
                skip_reasons[reason] += 1
                continue
            row = policy_row
            if family:
                kept_family_counts[family] += 1
            if field_signature:
                kept_field_signature_counts[field_signature] += 1
        kept.append(row)
    for index, row in enumerate(kept, start=1):
        row["candidate_rank"] = index
    _write_jsonl(path, kept)
    return {
        "ok": True,
        "input": len(rows),
        "kept": len(kept),
        "skipped": len(skipped),
        "skip_reasons": dict(sorted(skip_reasons.items())),
    }


def select_presubmit_ready_candidate(
    review_rows: list[dict],
    active_rows: list[dict],
    *,
    config: WQAgentWorkflowConfig,
    cycle_index: int,
) -> tuple[dict | None, list[dict]]:
    rejected: list[dict] = []
    accepted: dict | None = None
    for row in sorted(review_rows, key=review_sort_key):
        ok, reason, gate = presubmit_acceptance_gate(row, active_rows, config=config)
        if ok:
            if accepted is None:
                accepted = row
            continue
        if _should_defer_presubmit_recheck(row, reason):
            continue
        rejected.append({
            **row,
            "cycle_index": cycle_index,
            "presubmit_reject_reason": reason,
            "presubmit_gate": gate,
        })
    return accepted, rejected


def _should_defer_presubmit_recheck(row: dict, reason: str) -> bool:
    """Keep transient check-only gaps eligible for a later correlation check."""

    if row.get("triage_bucket") == SUBMIT_PROBE_NEEDED:
        return True
    if reason != "check_submission_not_readable":
        return False
    sc_result = str(row.get("sc_result") or "").upper()
    prod_result = str(row.get("prod_corr_result") or "").upper()
    return sc_result in {"", "MISSING", "PENDING"} or prod_result in {"", "MISSING", "PENDING"}


def presubmit_acceptance_gate(
    row: dict,
    active_rows: list[dict],
    *,
    config: WQAgentWorkflowConfig,
) -> tuple[bool, str, dict]:
    metrics = {
        "sharpe": row.get("sharpe"),
        "fitness": row.get("fitness"),
        "turnover": row.get("turnover"),
    }
    threshold_gate = submit_threshold_checks(metrics)
    sc_result = str(row.get("sc_result") or "").upper()
    sc_value = _score(row.get("sc_value"), default=float("inf"))
    prod_result = str(row.get("prod_corr_result") or "").upper()
    platform_status = str(row.get("platform_status") or "").upper()
    expression = str(row.get("expression") or "")
    nearest = nearest_similarity(expression, active_rows)
    nearest_score = (
        _score((nearest or {}).get("similarity", {}).get("overall_similarity"), default=0.0)
        if nearest else 0.0
    )
    family = _row_family(row)
    family_count = _active_family_counts(active_rows).get(family, 0) if family else 0
    field_signature = _field_signature(expression)
    field_signature_count = _active_field_signature_counts(active_rows).get(field_signature, 0) if field_signature else 0
    gate = {
        "threshold_gate": threshold_gate,
        "platform_status": platform_status,
        "sc_result": row.get("sc_result"),
        "sc_value": row.get("sc_value"),
        "prod_corr_result": row.get("prod_corr_result"),
        "nearest_active": nearest,
        "nearest_similarity": nearest_score,
        "virtual_similarity_cutoff": config.virtual_similarity_cutoff,
        "source_family": family,
        "source_family_count_before": family_count,
        "source_family_limit": config.max_virtual_family_count,
        "field_signature": field_signature,
        "field_signature_count_before": field_signature_count,
        "field_signature_limit": config.max_virtual_field_signature_count,
    }
    policy_eval = evaluate_candidate_policy(
        {
            **row,
            "nearest_similarity": nearest_score,
            "nearest_active": nearest,
            "field_signature": field_signature,
        },
        _submission_policy_for_config(config),
    )
    gate["forum_policy"] = policy_eval

    if policy_eval.get("action") == "block":
        return False, str(policy_eval.get("reason") or "forum_policy_block"), gate
    if row.get("triage_bucket") != CONFIRMED_READY:
        return False, "not_confirmed_ready", gate
    if row.get("api_check_status") != "api_check_readable":
        return False, "check_submission_not_readable", gate
    if platform_status in {"ACTIVE", "SUBMITTED"} or bool(row.get("submitted")):
        return False, "platform_status_not_unsubmitted", gate
    if not threshold_gate["eligible"]:
        return False, "base_submit_thresholds_failed", gate
    if sc_result != "PASS":
        return False, "self_correlation_not_pass", gate
    if sc_value >= 0.7:
        return False, "self_correlation_value_above_strict_cutoff", gate
    if prod_result == "FAIL":
        return False, "prod_correlation_failed", gate
    if row.get("failed_platform_checks"):
        return False, "platform_checks_failed", gate
    if nearest and nearest.get("exact"):
        return False, "exact_active_duplicate", gate
    if nearest_score > config.virtual_similarity_cutoff:
        return False, "too_similar_to_real_or_virtual_active", gate
    if config.max_virtual_family_count > 0 and family and family_count >= config.max_virtual_family_count:
        return False, "source_family_capacity_reached", gate
    if (
        config.max_virtual_field_signature_count > 0
        and field_signature
        and field_signature_count >= config.max_virtual_field_signature_count
    ):
        return False, "field_signature_capacity_reached", gate
    return True, "accepted", gate


def build_virtual_ready_record(
    row: dict,
    active_rows: list[dict],
    *,
    config: WQAgentWorkflowConfig,
    cycle_index: int,
    ready_index: int,
    cycle_output_dir: Path,
) -> dict:
    ok, reason, gate = presubmit_acceptance_gate(row, active_rows, config=config)
    return {
        **row,
        "created_at": _now(),
        "cycle_index": cycle_index,
        "ready_index": ready_index,
        "virtual_active_status": "VIRTUAL_ACTIVE",
        "presubmit_accepted": bool(ok),
        "presubmit_accept_reason": reason,
        "presubmit_gate": gate,
        "cycle_output_dir": str(cycle_output_dir),
    }


def nearest_similarity(expression: str, rows: list[dict]) -> dict | None:
    nearest = None
    normalized = normalize_expression(expression)
    for row in rows:
        other = str(row.get("expression") or "")
        if not other:
            continue
        similarity = compute_similarity(expression, other)
        item = {
            "alpha_id": row.get("alpha_id"),
            "expression": other,
            "status": row.get("status"),
            "similarity": similarity,
            "exact": normalized == normalize_expression(other),
        }
        if nearest is None or similarity.get("overall_similarity", 0.0) > nearest["similarity"].get("overall_similarity", 0.0):
            nearest = item
    return nearest


def should_repair(row: dict) -> bool:
    return row.get("triage_bucket") == NEAR_MISS_REPAIR


def build_repair_record(row: dict) -> dict:
    expression = str(row.get("expression") or "")
    return {
        "created_at": _now(),
        "alpha_id": row.get("alpha_id"),
        "source_expression": expression,
        "tag": row.get("tag"),
        "failure_kind": row.get("review_failure_kind") or row.get("api_check_status") or row.get("status"),
        "triage_reason": row.get("triage_reason"),
        "diagnosis": row.get("triage_reason"),
        "repair_objective": "model repair required; no hard-coded expression generated",
        "sharpe": row.get("sharpe"),
        "fitness": row.get("fitness"),
        "turnover": row.get("turnover"),
        "candidate_expressions": [],
        "risk_notes": row.get("risk_flags") or [],
        "source_row": row,
    }


def render_memory_context_markdown(context: dict) -> str:
    lines = [
        "# WQ Agent Memory Context",
        "",
        "## Active Alpha Inventory",
    ]
    for row in context.get("active") or []:
        lines.append(
            f"- {row.get('alpha_id') or 'unknown'}: sharpe={row.get('sharpe')} "
            f"fitness={row.get('fitness')} turnover={row.get('turnover')} expr={_short_expr(row.get('expression'))}"
        )
    lines.extend(["", "## Ledger Failure Memory"])
    for row in context.get("ledger_failures") or []:
        lines.append(
            f"- status={row.get('status') or row.get('source_status')} failure={row.get('failure_kind')} "
            f"sc={row.get('sc_value')} expr={_short_expr(row.get('expression'))}"
        )
    lines.extend(["", "## Near Miss / Pending Candidates"])
    for row in (context.get("ledger_near_miss") or []) + (context.get("current_near_miss") or []):
        lines.append(
            f"- {row.get('alpha_id') or 'candidate'}: status={row.get('status') or row.get('triage_bucket')} "
            f"sharpe={row.get('sharpe')} fitness={row.get('fitness')} turnover={row.get('turnover')} "
            f"expr={_short_expr(row.get('expression') or row.get('source_expression'))}"
        )
    lines.extend(["", "## Community Field Opportunities"])
    for row in context.get("field_opportunities") or []:
        fields = ", ".join(str(field) for field in (row.get("low_overlap_fields") or row.get("fields") or [])[:8])
        lines.append(f"- {row.get('tag') or row.get('source') or 'community'}: fields={fields}; expr={_short_expr(row.get('expression'))}")
    lines.extend([
        "",
        "## Generation Rules",
        "- Return only valid WorldQuant BRAIN FASTEXPR expressions.",
        "- Do not copy exact ACTIVE expressions.",
        "- Prefer behaviorally different fields/operators when avoiding self-correlation.",
        "- Use simple, testable structures before adding complex blends.",
    ])
    return "\n".join(lines) + "\n"


def build_candidate_generation_prompt(memory_markdown: str, *, target: int, examples: list[dict]) -> str:
    example_text = "\n".join(f"- {row['expression']}" for row in examples[:3] if row.get("expression"))
    return (
        "You are the model-driven CandidateDesignerAgent for WorldQuant BRAIN.\n"
        f"Generate up to {target} diverse candidate alphas as JSON.\n"
        "Return a JSON array. Each object must include: expression, rationale, "
        "expected_low_corr_reason, source_fields, mutation_strategy, parent_alpha_ids, risk_flags.\n"
        "Do not include markdown or commentary outside JSON.\n"
        "Hard constraints: valid FASTEXPR, no exact copies of active alphas, keep expressions concise.\n\n"
        "Fallback examples for style only, do not copy verbatim:\n"
        f"{example_text}\n\n"
        f"{memory_markdown}"
    )


def build_repair_generation_prompt(memory_markdown: str, repairable: list[dict]) -> str:
    rows = []
    for row in repairable[:20]:
        rows.append({
            "alpha_id": row.get("alpha_id"),
            "expression": row.get("expression"),
            "triage_reason": row.get("triage_reason"),
            "sharpe": row.get("sharpe"),
            "fitness": row.get("fitness"),
            "turnover": row.get("turnover"),
            "failed_platform_checks": row.get("failed_platform_checks"),
            "sc_value": row.get("sc_value"),
        })
    return (
        "You are the model-driven FailureReviewAgent for WorldQuant BRAIN.\n"
        "For each repairable row, propose repair plans and candidate expressions as JSON.\n"
        "Return a JSON array. Each object must include: source_expression, failure_kind, diagnosis, "
        "repair_objective, candidate_expressions, risk_notes.\n"
        "Do not include markdown or commentary outside JSON.\n"
        "If self-correlation is the issue, change field/operator family rather than just windows.\n\n"
        f"Repairable rows:\n{json.dumps(rows, ensure_ascii=False, default=str)[:8000]}\n\n"
        f"{memory_markdown}"
    )


def default_model_generate_candidates(prompt: str, config: WQAgentWorkflowConfig) -> str:
    return _call_deepseek_json(prompt, temperature=0.7, max_tokens=1800)


def default_model_generate_repairs(prompt: str, config: WQAgentWorkflowConfig) -> str:
    return _call_deepseek_json(prompt, temperature=0.4, max_tokens=1800)


def parse_model_candidate_response(response: Any) -> list[dict]:
    candidates = _response_items(response, preferred_key="candidates")
    parsed: list[dict] = []
    for index, item in enumerate(candidates):
        if not isinstance(item, dict):
            continue
        expression = clean_expression(str(item.get("expression") or ""))
        if not expression:
            continue
        parsed.append({
            "expression": expression,
            "tag": item.get("tag") or f"model-candidate-{index + 1}",
            "rationale": item.get("rationale"),
            "expected_low_corr_reason": item.get("expected_low_corr_reason"),
            "source_fields": item.get("source_fields") if isinstance(item.get("source_fields"), list) else _fields(expression),
            "mutation_strategy": item.get("mutation_strategy") or "model_generated",
            "parent_alpha_ids": item.get("parent_alpha_ids") if isinstance(item.get("parent_alpha_ids"), list) else [],
            "risk_flags": item.get("risk_flags") if isinstance(item.get("risk_flags"), list) else [],
            "source_family": item.get("source_family") or item.get("mutation_strategy") or "model_generated",
        })
    return parsed


def parse_model_repair_response(response: Any) -> list[dict]:
    repairs = _response_items(response, preferred_key="repairs")
    parsed: list[dict] = []
    for item in repairs:
        if not isinstance(item, dict):
            continue
        expressions = []
        for expr in item.get("candidate_expressions") or item.get("repair_expressions") or []:
            cleaned = clean_expression(str(expr))
            if cleaned:
                expressions.append(cleaned)
        parsed.append({
            "created_at": _now(),
            "source_expression": item.get("source_expression") or item.get("expression"),
            "failure_kind": item.get("failure_kind"),
            "diagnosis": item.get("diagnosis"),
            "repair_objective": item.get("repair_objective"),
            "candidate_expressions": expressions,
            "risk_notes": item.get("risk_notes") if isinstance(item.get("risk_notes"), list) else [],
            "model_generated": True,
        })
    return parsed


def _response_items(response: Any, *, preferred_key: str) -> list[Any]:
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        value = response.get(preferred_key)
        if isinstance(value, list):
            return value
        if isinstance(response.get("items"), list):
            return response["items"]
        return [response]
    if isinstance(response, str):
        payload = _extract_json_payload(response)
        return _response_items(payload, preferred_key=preferred_key)
    return []


def _extract_json_payload(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"(\[.*\]|\{.*\})", cleaned, flags=re.DOTALL)
    if not match:
        raise ValueError("model response did not contain JSON")
    return json.loads(match.group(1))


def _call_deepseek_json(prompt: str, *, temperature: float, max_tokens: int) -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"))
    response = client.chat.completions.create(
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        messages=[
            {"role": "system", "content": "Return strict JSON only. No markdown. No prose outside JSON."},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=60,
    )
    return response.choices[0].message.content or ""


def _summarize_rows(rows: list[dict], *, limit: int) -> list[dict]:
    out = []
    for row in rows[:limit]:
        expression = row.get("expression") or row.get("source_expression")
        out.append({
            "alpha_id": row.get("alpha_id"),
            "status": row.get("status") or row.get("source_status") or row.get("triage_bucket"),
            "failure_kind": row.get("failure_kind") or row.get("review_failure_kind"),
            "expression": expression,
            "fields": row.get("fields") or _fields(expression or ""),
            "operators": row.get("operators") or _operators(expression or ""),
            "sharpe": row.get("sharpe"),
            "fitness": row.get("fitness"),
            "turnover": row.get("turnover"),
            "sc_value": row.get("sc_value"),
            "triage_reason": row.get("triage_reason"),
            "tag": row.get("tag"),
            "low_overlap_fields": row.get("low_overlap_fields"),
        })
    return out


def _short_expr(expression: Any, limit: int = 180) -> str:
    text = str(expression or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def review_sort_key(row: dict) -> tuple:
    bucket_score = {
        CONFIRMED_READY: 0,
        SUBMIT_PROBE_NEEDED: 1,
        NEAR_MISS_REPAIR: 2,
        ACTIVE_OR_SUBMITTED: 3,
        HARD_FAIL: 4,
    }.get(row.get("triage_bucket"), 9)
    return (
        bucket_score,
        -_score(row.get("temporal_stability_score"), default=50),
        -_score(row.get("fitness")),
        -_score(row.get("sharpe")),
        _score(row.get("turnover"), default=999),
    )


def _repair_candidate_sort_key(row: dict) -> tuple:
    action_score = {
        "allow": 0,
        "penalize": 1,
        "block": 2,
    }.get(str(row.get("forum_policy_action") or "").lower(), 3)
    return (
        action_score,
        -_score(row.get("repair_priority_score")),
        -_score(row.get("research_priority_score")),
        str(row.get("tag") or ""),
    )


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
        "strict_self_correlation_cutoff": 0.7,
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
            "cycles_dir": str(paths.output_dir / "cycles"),
        },
    }
    _write_json(paths.loop_status, payload)
    return payload


def _finish(paths: WorkflowPaths, config: WQAgentWorkflowConfig, mode: str, sections: dict[str, Any]) -> dict:
    review_rows = _read_jsonl(paths.review_queue) if paths.review_queue.is_file() else []
    summary = {
        "schema_version": 1,
        "ok": True,
        "mode": mode,
        "updated_at": _now(),
        "submit_guard": "No real submit unless mode=submit or mode=run-submit with explicit authorization.",
        "canonical_entrypoint": "scripts/wq_agent_workflow.py",
        "authoritative_status_file": str(paths.summary),
        "bucket_counts": dict(sorted(Counter(row.get("triage_bucket") for row in review_rows).items())),
        "files": {key: str(value) for key, value in asdict(paths).items() if key != "output_dir"},
        **sections,
    }
    _write_json(paths.summary, summary)
    return summary


def _api_check_status(check_result: dict, *, sc_result: Any, prod_result: Any) -> str:
    if not check_result:
        return "api_check_missing"
    failure = str(check_result.get("review_failure_kind") or check_result.get("failure_kind") or "")
    if failure == "self_correlation" or str(sc_result).upper() == "FAIL":
        return "self_correlation_fail"
    if failure == "prod_correlation" or str(prod_result).upper() == "FAIL":
        return "prod_correlation_fail"
    if str(sc_result).upper() == "PENDING" or str(prod_result).upper() == "PENDING" or failure == "correlation_pending":
        return "api_check_pending"
    if str(check_result.get("status") or "").upper() in {"ACTIVE", "SUBMITTED"}:
        return "platform_active_check_readable"
    return "api_check_readable"


def _needs_check(row: dict) -> bool:
    return bool(row.get("alpha_id")) and str(row.get("status") or "") in {
        "eligible",
        "pending_correlation_check",
        "pre_submit_pass",
    }


def _row_can_submit(row: dict | None, *, allow_submit_probe: bool) -> bool:
    if not row:
        return False
    if row.get("triage_bucket") == CONFIRMED_READY:
        return True
    return allow_submit_probe and row.get("triage_bucket") == SUBMIT_PROBE_NEEDED


def _metrics_from_result(result: dict) -> dict:
    wq = result.get("wq_brain") if isinstance(result.get("wq_brain"), dict) else {}
    is_metrics = result.get("is_metrics") if isinstance(result.get("is_metrics"), dict) else {}
    return {
        "sharpe": _first_float(wq.get("wq_sharpe"), is_metrics.get("sharpe"), result.get("sharpe")),
        "fitness": _first_float(wq.get("wq_fitness"), is_metrics.get("fitness"), result.get("fitness")),
        "returns": _first_float(wq.get("wq_returns"), is_metrics.get("returns"), result.get("returns")),
        "turnover": _first_float(wq.get("wq_turnover"), is_metrics.get("turnover"), result.get("turnover")),
    }


def _failed_platform_checks(checks: list[dict]) -> list[dict]:
    ignored = {"SELF_CORRELATION", "PROD_CORRELATION", "MATCHES_COMPETITION"}
    return [
        check for check in checks
        if str(check.get("result") or "").upper() == "FAIL" and str(check.get("name") or "").upper() not in ignored
    ]


def _review_check(checks: list[dict], name: str) -> dict | None:
    for check in checks:
        if str(check.get("name") or "").upper() == name:
            return check
    return None


def _check_result(check: dict | None) -> str:
    return str((check or {}).get("result") or "").upper()


def _is_repairable_platform_fail(row: dict, failed_checks: list[dict]) -> bool:
    names = {str(check.get("name") or "").upper() for check in failed_checks}
    return bool(names & {"CONCENTRATED_WEIGHT", "LOW_SUB_UNIVERSE_SHARPE", "LOW_SUB_UNIVERSE_FITNESS"}) and _score(row.get("sharpe")) >= 1.5 and _score(row.get("fitness")) >= 1.0


def _is_metric_near_miss(metrics: dict) -> bool:
    sharpe = _score(metrics.get("sharpe"), default=0)
    fitness = _score(metrics.get("fitness"), default=0)
    turnover = metrics.get("turnover")
    turnover_ok = turnover is not None and 0.005 <= turnover <= 0.8
    return turnover_ok and sharpe >= 1.15 and fitness >= 0.85


def _virtual_active_row(row: dict) -> dict:
    expression = str(row.get("expression") or "")
    return {
        "alpha_id": row.get("alpha_id"),
        "expression": expression,
        "status": "VIRTUAL_ACTIVE",
        "active_source": "virtual_presubmit",
        "virtual_active": True,
        "cycle_index": row.get("cycle_index"),
        "ready_index": row.get("ready_index"),
        "tag": row.get("tag"),
        "source_family": _row_family(row),
        "fields": _fields(expression),
        "operators": _operators(expression),
        "sharpe": row.get("sharpe"),
        "fitness": row.get("fitness"),
        "returns": row.get("returns"),
        "turnover": row.get("turnover"),
        "sc_value": row.get("sc_value"),
        "prod_corr_value": row.get("prod_corr_value"),
    }


def _active_family_counts(rows: list[dict]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        family = _row_family(row)
        if family:
            counts[family] += 1
    return counts


def _active_field_signature_counts(rows: list[dict]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        signature = _field_signature(str(row.get("expression") or ""))
        if signature:
            counts[signature] += 1
    return counts


def _row_family(row: dict) -> str:
    return str(
        row.get("source_family")
        or row.get("mutation_strategy")
        or (row.get("candidate_meta") or {}).get("source_family")
        or ""
    )


def _candidate_dedupe_key(row: dict) -> str:
    expression = normalize_expression(str(row.get("expression") or ""))
    settings = _candidate_settings_override(row)
    if not settings:
        return expression
    return f"{expression}||settings={json.dumps(settings, sort_keys=True, separators=(',', ':'))}"


def _candidate_settings_override(row: dict) -> dict:
    raw = row.get("simulation_settings") or row.get("settings_override") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("region", "universe", "neutralization"):
        value = raw.get(key)
        if value not in (None, ""):
            out[key] = str(value)
    for out_key, *input_keys in (
        ("maxTrade", "maxTrade", "max_trade"),
        ("maxPosition", "maxPosition", "max_position"),
    ):
        value = next((raw.get(key) for key in input_keys if raw.get(key) not in (None, "")), None)
        if value not in (None, ""):
            text = str(value).upper()
            if text in {"ON", "OFF"}:
                out[out_key] = text
    for key in ("delay", "decay"):
        value = raw.get(key)
        if value in (None, ""):
            continue
        try:
            out[key] = int(value)
        except (TypeError, ValueError):
            continue
    if raw.get("truncation") not in (None, ""):
        try:
            truncation = float(raw["truncation"])
        except (TypeError, ValueError):
            truncation = None
        if truncation is not None and 0 < truncation <= 0.2:
            out["truncation"] = truncation
    return out


def _simulation_settings_for_candidate(candidate: dict, config: WQAgentWorkflowConfig) -> dict:
    settings = {
        "region": config.region,
        "universe": config.universe,
        "delay": config.delay,
        "decay": config.decay,
        "neutralization": config.neutralization,
        "truncation": config.truncation,
        "maxTrade": "OFF",
        "maxPosition": "OFF",
    }
    settings.update(_candidate_settings_override(candidate))
    return settings


def _field_signature(expression: str) -> str:
    fields = _fields(expression)
    return "|".join(fields)


def _is_option_only_expression(expression: str) -> bool:
    fields = set(_fields(expression))
    return bool(fields) and fields <= OPTION_FIELDS


def _platform_candidate_family(expression: str) -> str:
    fields = set(_fields(expression))
    prefix = "platform_recent_unsubmitted"
    if _is_option_only_expression(expression):
        return f"{prefix}_options_only"
    if fields & PLATFORM_DERIVATIVE_FIELDS:
        return f"{prefix}_model_derivative"
    if fields & PLATFORM_FORWARD_VALUE_FIELDS:
        return f"{prefix}_forward_value"
    if fields & PLATFORM_ANALYST_REVISION_FIELDS:
        return f"{prefix}_analyst_revision"
    if fields & PLATFORM_CASHFLOW_FIELDS:
        return f"{prefix}_cashflow_value"
    if {"high", "low", "close"} <= fields:
        return f"{prefix}_intraday_reversal"
    return f"{prefix}_memory"


def _has_unsupported_statement_separator(expression: str) -> bool:
    return ";" in (expression or "")


def _fields(expression: str) -> list[str]:
    try:
        return sorted(str(field) for field in extract_components(expression or "").get("fields", []))
    except Exception:
        return []


def _operators(expression: str) -> list[str]:
    try:
        return sorted(str(op) for op in extract_components(expression or "").get("operators", []))
    except Exception:
        return []


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    return round(len(left & right) / len(left | right), 4)


def _score(value: Any, default: float = float("-inf")) -> float:
    parsed = safe_float(value)
    return default if parsed is None else parsed


def _first_float(*values: Any) -> float | None:
    for value in values:
        parsed = safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is not None and str(value) != "":
            return str(value)
    return None


def _read_candidate_rows(path: Path) -> list[dict]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, list):
            return [_candidate_from_value(value, str(path)) for value in data]
    return [_candidate_from_value(value, str(path)) for value in _iter_jsonish(path)]


def _candidate_from_value(value: Any, source: str) -> dict:
    if isinstance(value, str):
        return {"expression": value, "source": source}
    if isinstance(value, dict):
        return {
            "expression": value.get("expression") or (value.get("result") or {}).get("expression"),
            "tag": value.get("tag"),
            "source_family": value.get("source_family"),
            "mutation_strategy": value.get("mutation_strategy"),
            "rationale": value.get("rationale"),
            "expected_low_corr_reason": value.get("expected_low_corr_reason"),
            "source_fields": value.get("source_fields") or value.get("fields"),
            "parent_alpha_ids": value.get("parent_alpha_ids") or [],
            "risk_flags": value.get("risk_flags") or [],
            "simulation_settings": _candidate_settings_override(value),
            "candidate_meta": {
                **(value.get("candidate_meta") or {}),
                **{key: value.get(key) for key in ("alpha_id", "status", "source_family") if value.get(key) is not None},
            },
            "source": source,
        }
    return {"expression": "", "source": source}


def _iter_jsonish(path: Path) -> Iterable[Any]:
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("{"):
            yield line
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for value in _iter_jsonish(path):
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _settings(config: WQAgentWorkflowConfig) -> dict:
    return {
        "account": config.account,
        "region": config.region,
        "universe": config.universe,
        "delay": config.delay,
        "decay": config.decay,
        "neutralization": config.neutralization,
        "truncation": config.truncation,
    }


def _config_dict(config: WQAgentWorkflowConfig) -> dict:
    data = asdict(config)
    for key in ("output_dir", "community_context_dir", "submission_policy_file"):
        if data.get(key) is not None:
            data[key] = str(data[key])
    data["candidate_files"] = [str(path) for path in config.candidate_files]
    data["seed_ready_files"] = [str(path) for path in config.seed_ready_files]
    return data


def _resolve_output_dir(output_dir: Path) -> Path:
    return output_dir if output_dir.is_absolute() else ROOT / output_dir


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
