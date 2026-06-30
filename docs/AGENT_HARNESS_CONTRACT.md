# Agent Harness Contract

The contract layer turns an agent research run into stable JSON and JSONL artifacts. It is implemented in `worldquant_harness/harness_contracts.py` and wrapped by `worldquant_harness/harness_runner.py`.

契约层把 agent 研究过程转为稳定 JSON/JSONL。核心实现：`harness_contracts.py`、`harness_runner.py`。

## Public Eval Command

```bash
python scripts/run_public_harness_eval.py --output-root reports/public_harness_eval
```

This command runs the deterministic public demo, then writes the contract artifacts below. It does not call WQ BRAIN, DeepSeek, Wind, or private credentials.

## Standard Files

| File | Format | Purpose |
|:--|:--|:--|
| `harness_run.json` | JSON | Run envelope, roles, steps, artifacts, decisions, metrics |
| `hypotheses.jsonl` | JSONL | Alpha-GPT-style hypothesis records |
| `alpha_gpt_candidate_specs.jsonl` | JSONL | Candidate specs linked to hypothesis, placeholder template, bindings, and constraints |
| `review_decisions.jsonl` | JSONL | Promote, retry, or reject decisions for the semantic research loop |
| `reflection_records.jsonl` | JSONL | Memory/profile lessons proposed after evaluation |
| `submit_evidence.json` | JSON | Explicit-submit boundary evidence; public eval records no real submit attempt |
| `agent_trace.jsonl` | JSONL | Append-only event trace for agent state transitions |
| `artifacts.jsonl` | JSONL | Artifact references with path, type, producer step, content hash |
| `decisions.jsonl` | JSONL | Sandbox gate, harness gate, submit boundary decisions |
| `memory_delta.jsonl` | JSONL | Proposed memory updates from rejection and maintenance signals |
| `profile_patch.json` | JSON | Reviewable profile candidate patch, not auto-applied |
| `eval_cases.jsonl` | JSONL | Public regression cases |
| `eval_result.json` | JSON | Case summary, score, metrics, reject counts |
| `manifest.json` | JSON | File index and entrypoint metadata |

## Core Schemas

### `HarnessRun`

Fields: `schema_version`, `run_id`, `topic`, `mode`, `status`, `no_submit`, `profile_name`, `source_refs`, `steps`, `artifacts`, `decisions`, `metrics`, `created_at`, `updated_at`.

`no_submit` must be `true` for public harness runs.

### `HarnessStep`

Fields: `step_id`, `run_id`, `role`, `action`, `status`, `input_refs`, `output_refs`, `metrics`, `started_at`, `finished_at`.

Allowed roles: `researcher`, `verifier`, `simulator`, `critic`, `reflector`, `submitter`.

### `HarnessEvent`

Fields: `event_id`, `run_id`, `event_type`, `role`, `step_id`, `candidate_uid`, `payload`, `created_at`.

Allowed event types include `context_loaded`, `hypothesis_created`, `candidates_proposed`, `candidate_specs_constrained`, `presubmit_ran`, `gate_reviewed`, `review_decision_recorded`, `evaluated`, `reflected`, `submit_evidence_recorded`, `profile_candidate_written`, `memory_delta_written`.

### Alpha-GPT Semantic Records

The public eval runner also writes semantic records that make the research loop
explicit:

- `AlphaGPTHypothesis`: `hypothesis_id`, `run_id`, `topic`, `statement`, `rationale`, `expected_signal`, `status`.
- `AlphaGPTCandidateSpec`: `candidate_uid`, `hypothesis_id`, `expression`, `research_intent`, `placeholder_template`, `placeholder_bindings`, `generation_constraints`.
- `AlphaGPTReviewDecision`: `candidate_uid`, `hypothesis_id`, `decision`, `reason`, `metrics`, `next_action`, `human_required`.
- `AlphaGPTReflectionRecord`: `reflection_id`, `run_id`, `hypothesis_id`, `conclusion`, `memory_actions`, `profile_actions`.
- `AlphaGPTSubmitEvidence`: `run_id`, `boundary_role`, `status`, `explicit_submit_required`, `selected_alpha_ids`, `real_submit_attempted`.

All public semantic records carry `no_submit=true`. `AlphaGPTSubmitEvidence`
uses `real_submit_attempted=false` in public eval; real submission evidence can
only come from explicitly selected alpha IDs in separate submit runs.

### `DecisionGate`

Fields: `gate_name`, `decision`, `reasons`, `metrics`, `human_required`, `created_at`.

The submit boundary is represented as a decision with `decision=hold` and `human_required=true`.

### `MemoryDelta`

Fields: `memory_kind`, `action`, `key`, `reason`, `evidence_refs`, `payload`, `created_at`.

Common actions: `block`, `down_weight`, `compress`, `absorb`.

### `ProfilePatch`

Fields: `target_profile`, `patch_ops`, `evidence_refs`, `risk_notes`, `no_submit`, `created_at`.

Patch operations carry `auto_applied=false` in the public eval runner.

## Public Eval Cases

| Case | Expected Signal |
|:--|:--|
| `ready_candidate` | exactly one ready candidate |
| `strict_self_correlation_rejected` | one strict local self-correlation reject |
| `illegal_field_rejected` | one illegal input reject |
| `duplicate_active_rejected` | one duplicate active expression reject |
| `no_real_submit` | no real submit attempt |
| `profile_patch_generated_not_applied` | profile patch exists and every op has `auto_applied=false` |
| `alpha_gpt_hypothesis_written` | one structured hypothesis artifact |
| `alpha_gpt_candidate_specs_link_hypothesis` | five candidate specs linked to the hypothesis |
| `alpha_gpt_review_decisions_written` | promote, retry, and reject review decisions |
| `submit_evidence_requires_explicit_submit` | submit evidence requires explicit human selection and records no public submit |

These cases make the public demo a harness regression suite, not only a screenshot source.

这些 case 把公开 demo 变成回归套件。它不只是截图来源。

## MCP Tools

The MCP layer exposes no-submit harness wrappers:

- `wq_harness_new`
- `wq_harness_run_presubmit`
- `wq_harness_evaluate`
- `wq_harness_evolve`
- `wq_harness_history_ingest`
- `wq_harness_memory_maintain`
- `wq_harness_status`

Real submission remains in the explicit `wq_brain_*` tools.
