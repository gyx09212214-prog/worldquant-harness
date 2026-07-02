# WQ Redundancy And Module Boundary Audit

Audit date: 2026-07-02

This note records the current project-level redundancy and module-design audit for the WorldQuant harness code path.

## Completed In This Pass

- Removed the only detected internal import cycle: `alpha_tracker -> wq_alpha_ledger -> alpha_tracker`.
- Added `worldquant_harness/wq_similarity.py` for shared `compute_similarity()` and `nearest_similarity()`.
- Kept `alpha_tracker.compute_similarity` as a compatibility import while moving internal modules and scripts to `wq_similarity`.
- Added `worldquant_harness/wq_platform_artifacts.py` for shared read-only platform alpha pagination and local file inventory rows.
- Kept `wq_complete_submission_records.fetch_platform_alphas/local_file_inventory` and `wq_history_experience.fetch_platform_alphas/local_file_inventory` as compatibility wrappers.
- Moved robust outer `rank(...)` stripping into `wq_expression_utils.strip_outer_rank()`.
- Moved CSV cell serialization into `artifact_io.csv_value()`.
- Split the former monolithic `wq_agent_workflow.py` into workflow-stage modules:
  `wq_workflow_platform.py`, `wq_workflow_memory.py`,
  `wq_workflow_candidate_design.py`, `wq_workflow_execution.py`,
  `wq_workflow_presubmit.py`, `wq_workflow_submit_repair.py`,
  `wq_workflow_constants.py`, and `wq_workflow_support.py`.
- Kept `wq_agent_workflow.py` as the compatibility facade and top-level orchestrator so existing imports from scripts/tests keep working.
- Split `wq_workflow_support.py` into narrower helper modules while keeping it as a 75-line compatibility re-export layer.
- Added `wq_research_paths.py` for shared research experiment root and path resolution.
- Delegated `harness_contracts.read_jsonl()` to `artifact_io.read_jsonl()` while keeping no-submit validation on writes.
- Added `report_utils.py` and migrated common markdown cell, first-present, ratio, and numeric formatting helpers where semantics matched exactly.
- Added `wq_candidate_generation.py` and migrated 22 of 23 `generate_wq_submit5_more_*` static records scripts to the shared generator harness.
- Added `async_utils.py` for the sync wrapper around async ledger/history operations.
- Added `source_utils.py` for repeated source-run-id extraction across search memory, submission experience, complete submission records, and ledger backfill.
- Split repair candidate helper boundaries out of `wq_policy_repair_planner.py`:
  `wq_repair_screening.py` owns concentration sparse-leg guard logic,
  `wq_repair_scoring.py` owns repair priority scoring/sorting,
  and `wq_repair_records.py` owns candidate record construction, dedupe keys, and local expression validation.
- Split the remaining policy repair template banks by failure kind:
  `wq_repair_templates_self_corr.py`, `wq_repair_templates_concentration.py`, and
  `wq_repair_templates_metric_threshold.py`.
- Added `record_utils.dedupe_rows_by_key()` and migrated first-wins `_dedupe_memory`, `_dedupe_records`, `_dedupe_by_expression`, and repair-candidate dedupe wrappers where their key semantics could be preserved exactly.
- Added `wq_active_records.py` for active-node JSONL normalization used by focused/submit10 candidate scripts.
- Added `wq_progress.py` for shared WQ simulation progress-message normalization.
- Moved report `matching_reason_count()` into `report_utils.py` and reused it from research harness and submit-efficiency reporting.
- Replaced remaining exact duplicate script JSONL write/read and alpha-id first-wins dedupe helpers with `artifact_io` and `record_utils` helpers where behavior matched.

## Current Structure Findings

### 0. Follow-Up Static Audit: Cycles And Duplication

Latest static import graph scan across `worldquant_harness` found 118 top-level modules, 398 internal import edges, and 0 import cycles. The previous `alpha_tracker -> wq_alpha_ledger -> alpha_tracker` cycle remains resolved.

The former support bucket is now split into `wq_workflow_active.py`, `wq_workflow_context.py`, `wq_workflow_prompts.py`, `wq_workflow_scoring.py`, `wq_workflow_loop_status.py`, `wq_workflow_seed_records.py`, and `wq_workflow_lifecycle.py`. `wq_workflow_support.py` remains only for private compatibility re-exports.

Current highest fan-in modules are `expression_parser`, `artifact_io`, `record_utils`, `models`, `wq_expression_utils`, `db`, `auth`, `wq_brain_client`, `wq_brain_service`, `market_data`, `wq_agent_config`, and `wq_agent_records`. This mostly reflects healthy utility/config reuse.

Exact duplicate-body scan across `worldquant_harness` and `scripts` now finds 3 groups, down from 42 at the start of this audit series and down from 9 at the start of the latest pass. The largest previous duplicate clusters, the 35-45 line static generator `main()` bodies, repeated JSONL IO helpers, active-row loaders, alpha-id dedupe loops, progress-message mappers, and matching-reason counters were removed.

- Remaining exact duplicate function bodies are low-payoff local helpers: dataclass `to_dict()` methods in `harness_contracts.py`, two CLI `_resolve_path()` helpers, and local `_spearman()` closures in validation modules.
- Remaining non-exact script generator duplication is mostly inside formula-template helpers such as `_iv_ratio`, `_cf`, `_lln_proxy`, and `_omyo_proxy`; this is lower risk but still worth a later template-bank pass if those scripts keep changing.
- Contract IO: `harness_contracts.read_jsonl()` now delegates to `artifact_io.read_jsonl()`; `harness_contracts.write_jsonl()` stays local because it performs no-submit validation.
- Report helpers: `_md`, `_fmt`, `_ratio`, `_mean`, `_first_present`, and `_safe_normalize` remain repeated across WQ report modules.
- Research experiment path resolution now delegates to `wq_research_paths.resolve_research_experiment_dir()` from both `wq_research_harness` and `wq_research_sandbox`.
- Record identity helpers: `_source_run_id` has moved to `source_utils.py`. First-wins dedupe loops now delegate to `record_utils.dedupe_rows_by_key()` while preserving local key semantics.

### 1. `wq_agent_workflow.py` Was Reduced To A Facade

The main workflow file has been reduced from roughly 3k lines to the run/loop orchestrator plus compatibility re-exports. Stage implementation now lives in dedicated workflow modules:

- `wq_workflow_platform.py`: `PlatformSyncAgent` and platform inventory writing.
- `wq_workflow_memory.py`: community scouting and compact memory-context generation.
- `wq_workflow_candidate_design.py`: model/file/repair/platform/fallback candidate design.
- `wq_workflow_execution.py`: simulation, review, PnL enrichment, and result classification.
- `wq_workflow_presubmit.py`: gate, similarity filtering, virtual active handling, and presubmit artifacts.
- `wq_workflow_submit_repair.py`: submit-mode boundaries and repair/postmortem rows.
- `wq_workflow_constants.py` and `wq_workflow_support.py`: shared status constants and cross-stage helpers.

Remaining boundary risk has shifted away from `wq_workflow_support.py`. The larger WQ modules are now `wq_research_miner.py`, `wq_policy_repair_planner.py`, `wq_alpha_search_memory.py`, `wq_complete_submission_records.py`, `wq_forum_submission_optimizer.py`, and `wq_auto_mining.py`.

Only these local consumers still import from the workflow facade directly: `scripts/run_public_harness_demo.py`, `scripts/wq_agent_workflow.py`, `worldquant_harness/wq_research_sandbox.py`, and `tests/test_wq_agent_workflow.py`. The scripts/tests are acceptable compatibility consumers. `wq_research_sandbox.py` can be tightened later by importing config/constants from `wq_agent_config` and `wq_workflow_constants`, while keeping only `run_workflow` from the facade.

### 2. Script-Level Candidate Generators Have A Shared Harness

`wq_candidate_generation.py` now owns the repeated static generator loop: parse `--output/--limit`, dedupe by expression/settings, validate FASTEXPR, write JSONL, write summary, write invalid rows, and print summary.

Migration status:

- 22 of 23 `scripts/generate_wq_submit5_more_*` files now call `run_static_candidate_generator(...)`.
- `generate_wq_submit5_more_existing_diverse_candidates.py` is intentionally not migrated because it is not a static `_records()` generator; it loads existing inventory and has custom diversity filtering.
- The shared harness preserves older schema behavior for scripts that did not write `candidate_rank`.

### 3. Report Formatting Helpers Are Still Repeated

Remaining repeated names include `_md`, `_fmt`, `_ratio`, `_mean`, `_truncate`, `_first_present`, and `_safe_normalize`. Most are low-risk but scattered across report-producing modules.

Recommended migration:

- Continue using `worldquant_harness/report_utils.py` for truly presentation-shaped helpers.
- Migrate only helpers with matching semantics; keep local helpers where escaping, precision, or fallback behavior differs.
- Avoid mixing report formatting into `artifact_io.py`, which should stay focused on file formats.

### 4. WQ Record Collection Has Compatibility Wrappers

`wq_complete_submission_records` and `wq_history_experience` now delegate repeated platform fetch/inventory logic to `wq_platform_artifacts`, but still expose local wrapper functions.

Recommended migration:

- Keep wrappers for one compatibility cycle.
- Later update callers to import `wq_platform_artifacts` directly.
- Then remove wrappers if no external script/test imports remain.

### 5. Research Miner And Policy Repair Template Banks

`wq_policy_repair_planner.py` is now a 431-line planner/facade for repair plan loading, row eligibility, artifact writing, and generic fallback repair. The large deterministic repair templates have moved into failure-kind modules:

- `wq_repair_templates_self_corr.py`: field-family replacement templates for self-correlation misses.
- `wq_repair_templates_concentration.py`: smoothing, dispersion, max-position, and lower-truncation templates for concentrated-weight misses.
- `wq_repair_templates_metric_threshold.py`: Sharpe/Fitness near-threshold tuning templates.

`wq_research_miner.py` is still large because it combines field/operator policy, hand-written candidate templates, screening, and artifact output.

Recommended migration:

- Move formula template banks into `wq_research_templates.py`.
- Keep `wq_research_miner.py` focused on artifact loading, planning, and output.

### 6. Dedupe Key Semantics

The `_dedupe_memory`, `_dedupe_records`, and `_dedupe_by_expression` audit found that the loop mechanics were duplicate but the identity keys are intentionally different:

- Complete submission memory: `(expression_hash, experience_label)`.
- Submission records: `(alpha_id, expression_hash, source_type, source_run_id)`.
- Submission memory: `(memory_kind, failure_kind, expression_hash, field_signature)`.
- Research miner memory: `(memory_kind, failure_kind, expression_normalized)`.
- History memory rows: `(memory_type, failure_kind, expression_hash or pattern_signature)`.
- History expression rows: `expression_hash(expression)`.
- Forum expression expansion: compact lowercase expression text, with empty expressions skipped.
- Iteration audit records: `audit_record_id`.

These wrappers now delegate to `dedupe_rows_by_key()` where they are first-wins. `wq_alpha_quality_review._dedupe_records()` and history event grouping remain local because they select the highest-ranked record and accumulate `evidence_count`, which is not a first-wins dedupe.

### 7. `harness_contracts.py` Mostly Avoids Artifact IO Duplication

`harness_contracts.read_jsonl()` now delegates to `artifact_io.read_jsonl()`.

Remaining decision:

- Keep `write_jsonl()` local unless `artifact_io` gains an explicit validation hook, because contract writes must reject real-submit artifacts.

## Residual High-Confidence Cleanup Candidates

- Continue consolidating `_md`/`_fmt`/`_ratio`/`_mean` into report utilities only when semantics match exactly.
- Decide whether `scripts/summarize_wq_active_alpha_map.py` should use `wq_platform_artifacts.fetch_platform_alphas()` or keep its custom return shape.
- Consider a second candidate-template pass for repeated `_iv_ratio`, `_cf`, `_lln_proxy`, and `_omyo_proxy` helpers inside the static submit5-more scripts.
- Split `wq_research_miner.py` rule/template banks if future candidate-generation edits continue growing that file.
- Leave the 3 remaining exact duplicate-body groups alone unless nearby edits already touch those files; each is currently small and local-context-specific.

## Verification Snapshot

- Follow-up static audit after this pass: `worldquant_harness` import graph has `0` detected cycles (`118` top-level modules, `398` internal import edges).
- Exact duplicate-body groups after this pass: `3`, down from `42` at the start of the audit series.
- Static submit5-more script harness migration: `22/23` scripts converted.
- Workflow regression after the module split: `tests/test_wq_agent_workflow.py` has `36 passed`.
- Policy repair boundary regression after async/source/repair helper split:
  `tests/test_wq_policy_repair_planner.py tests/test_wq_post_submit_review.py tests/test_wq_agent_workflow.py` has `49 passed`.
- Template/dedupe focused regression after this pass:
  `tests/test_wq_policy_repair_planner.py tests/test_wq_post_submit_review.py tests/test_wq_agent_workflow.py tests/test_wq_history_experience.py tests/test_wq_submission_experience.py tests/test_wq_complete_submission_records.py tests/test_wq_research_miner.py tests/test_wq_forum_expression_expander.py tests/test_wq_alpha_quality_review.py` has `78 passed`.
- Script/helper duplicate cleanup focused regression after this pass:
  `tests/test_wq_existing_diverse_candidates.py tests/test_submit_wq_existing_until_target.py tests/test_wq_live_submit_candidates.py tests/test_wq_find_only.py tests/test_codex_direct_wq_submit_loop.py tests/test_wq_agent_workflow.py` has `55 passed`;
  `tests/test_wq_research_harness.py tests/test_wq_submit_efficiency_report.py tests/test_wq_auto_mining.py tests/test_wq_loop_runner.py tests/test_wq_agent_workflow.py` has `57 passed`.
- Sandbox/harness focused regression after the module split: `tests/test_wq_research_sandbox.py tests/test_harness_runner.py tests/test_wq_research_harness.py` has `10 passed`.
- Compile check after this pass: `python -m compileall -q worldquant_harness scripts`.
- Full regression after this pass: `639 passed, 2 skipped`.
