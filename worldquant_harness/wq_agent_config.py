"""Configuration and artifact paths for the WQ agent workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass
class WQAgentWorkflowConfig:
    output_dir: Path
    candidate_files: list[Path] = field(default_factory=list)
    seed_ready_files: list[Path] = field(default_factory=list)
    seed_rejected_files: list[Path] = field(default_factory=list)
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
    include_platform_candidates: bool = True
    target_submissions: int = 0
    target_ready: int = 0
    max_total_simulations: int = 2000
    cycle_candidate_count: int = 40
    max_cycles: int = 50
    max_consecutive_empty_cycles: int = 3
    max_consecutive_submit_failures: int = 5
    virtual_similarity_cutoff: float = 0.65
    presubmit_self_correlation_cutoff: float | None = None
    presubmit_daily_return_correlation_cutoff: float | None = 0.70
    presubmit_daily_return_correlation_warn: float | None = 0.50
    max_virtual_family_count: int = 2
    max_virtual_field_signature_count: int = 2
    submission_policy_file: Path | None = None
    legal_inputs_file: Path | None = None
    strict_legal_inputs: bool = True
    enrich_pnl: bool = False
    pnl_enrichment_limit: int = 8
    pnl_min_stability_score: float = 0.0
    post_submit_review_enabled: bool = True
    post_submit_baseline_roots: list[Path] = field(default_factory=list)
    post_submit_profile_dir: Path | None = None
    post_submit_window_days: int = 14
    iteration_audit_enabled: bool = True
    audit_history_limit: int = 20
    audit_include_expressions: bool = False


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
    candidate_skipped: Path
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
    lifecycle_events: Path
    iteration_audit: Path
    iteration_audit_summary: Path
    iteration_audit_markdown: Path
    summary: Path

    @classmethod
    def for_output_dir(cls, output_dir: Path) -> WorkflowPaths:
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
            candidate_skipped=output_dir / "candidate_skipped.jsonl",
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
            lifecycle_events=output_dir / "alpha_lifecycle_events.jsonl",
            iteration_audit=output_dir / "iteration_audit.jsonl",
            iteration_audit_summary=output_dir / "iteration_audit_summary.json",
            iteration_audit_markdown=output_dir / "iteration_audit.md",
            summary=output_dir / "summary.json",
        )
