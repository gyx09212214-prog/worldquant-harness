# Harness Artifacts And Score

This document defines the public WQ research harness outputs. The same contract
is used by the deterministic public demo and by real guarded research sandbox
runs.

## Core Artifacts

| Artifact | Producer | Purpose |
| --- | --- | --- |
| `experiment.yaml` | `wq_research_sandbox.py new` | Experiment topic, hypothesis, settings, and gate configuration |
| `candidate_specs.jsonl` | sandbox / miner / demo | Candidate expressions and research metadata before simulation |
| `presubmit_run/summary.json` | `presubmit-sequential` | Compact run status, counts, stop reason, and file manifest |
| `presubmit_run/presubmit_ready_sequential.jsonl` | `presubmit-sequential` | Candidates that passed simulation and check-only review |
| `presubmit_run/presubmit_rejected.jsonl` | `presubmit-sequential` | Candidates rejected by legality, similarity, platform, or strict local gates |
| `critic_report.yaml` | sandbox gate | Research critique and review state |
| `decision.yaml` | sandbox gate | Gate decision such as `promote_candidate`, `hold`, or `retire` |
| `evaluations/<eval-id>/eval_summary.json` | harness eval | Harness metrics, score, gate report, and reject counts |
| `evaluations/<eval-id>/run_report.md` | harness report | Human-readable evaluation summary |
| `evaluations/<eval-id>/evolution_result.json` | harness evolve | Next-generation profile changes and optional child experiment |
| `presubmit_run/alpha_lifecycle_events.jsonl` | presubmit workflow | Append-only lifecycle events keyed by `candidate_uid` |
| `wq_alpha_quality_review_*/summary.json` | quality review | Period submitted/generated quality metrics and next-direction suggestions |
| `wq_alpha_quality_review_*/quality_alpha_events.jsonl` | quality review | Standardized submitted, generated, and check-only alpha rows |
| `wq_alpha_quality_review_*/self_correlation_pressure.csv` | quality review | SELF-correlation pressure by field signature, source family, and domain |

The public demo also writes `demo_summary.json`, a convenience manifest for
documentation and CI smoke checks. Generated files under `reports/` are runtime
artifacts and are intentionally ignored by git.

## Public Visual Pack

`scripts/build_public_visual_pack.py` converts the public demo artifacts into
static SVGs and a short visual guide:

![Artifact lifecycle](images/harness-artifact-lifecycle.svg)

```powershell
python scripts/build_public_visual_pack.py `
  --source reports/public_harness_demo `
  --output-dir docs/images `
  --report docs/VISUAL_GUIDE.md
```

| Visual | Primary artifact inputs |
| --- | --- |
| `docs/images/worldquant-harness-overview.svg` | `demo_summary.json`, `eval_summary.json`, quality summary |
| `docs/images/worldquant-harness-architecture.svg` | static public-safe map of agent entrypoints, harness gates, memory, and submit boundary |
| `docs/images/worldquant-harness-architecture.zh-CN.svg` | Chinese static public-safe map for `README.zh-CN.md` |
| `docs/images/harness-artifact-lifecycle.svg` | static map of `candidate_specs`, simulation, review, ready/rejected, memory/profile |
| `docs/images/public-demo-trace.svg` | `candidate_specs.jsonl`, ready/rejected rows, `efficiency_summary.json` |
| `docs/images/memory-feedback-graph.svg` | reject counts, quality recommended directions, evolution result |
| `docs/images/factor-map-snapshot.svg` | field-signature rows and source-family leaderboards |
| `docs/images/quality-review-dashboard.svg` | `quality_review/summary.json`, self-correlation pressure |
| `docs/images/profile-evolution-timeline.svg` | `evolution_result.json` profile candidate actions |
| `docs/images/submit-boundary.svg` | static submit/no-submit boundary |
| `docs/images/release-safety-boundary.svg` | static open-source publication boundary |

The generated guide and SVGs are sanitized for public docs: they should not
contain local absolute paths, Obsidian paths, or credential material.

## Presubmit Outcomes

A candidate can only be considered ready when it passes all active gates:

- base simulation thresholds such as Sharpe, fitness, turnover, and returns
- check-only platform review when available
- self-correlation status and strict local self-correlation cutoff
- similarity to real or virtual ACTIVE inventory
- legal-input validation for fields, operators, and field types

Rejected rows should preserve enough context to explain why work was stopped.
Common reasons include `illegal_field`, `exact_active_duplicate`,
`too_similar_to_virtual_active`, `not_confirmed_ready`, and
`self_correlation_value_above_strict_cutoff`.

## Harness Score

`harness_score` is a workflow-quality score, not a trading performance metric.
It answers: did this harness configuration produce ready, diverse candidates
with low rejection waste and reasonable latency?

The current score is a weighted average of normalized components:

| Component | Weight | Normalization |
| --- | ---: | --- |
| Ready yield | 0.35 | `min(ready_per_100_simulations / 5, 1)` |
| Explicit submit success rate | 0.20 | `promote_submit_success_rate`, omitted when no explicit submit runs are supplied |
| Self-correlation quality | 0.15 | `1 - self_correlation_reject_share` |
| Similarity quality | 0.15 | `1 - too_similar_reject_share` |
| Field diversity | 0.10 | `1 - field_signature_duplicate_ratio` |
| Speed | 0.05 | `1 - min(hypothesis_to_first_ready_seconds, 86400) / 86400` |

Unavailable components are omitted and the remaining weights are renormalized.
This is why the public demo can have a score without any real submit attempts:
the submit-success component is absent rather than treated as zero.

## Public Demo Expectations

`scripts/run_public_harness_demo.py` uses deterministic synthetic fixtures. A
healthy demo run should show:

- `real_submit_attempted: false`
- one ready candidate
- three simulated candidates
- one strict self-correlation rejection
- one illegal-field rejection
- one exact active duplicate rejection
- a child experiment created by the evolution step

Validate a run with:

```powershell
python scripts/validate_public_harness_artifacts.py reports/public_harness_demo
```

Generate an alpha submit-efficiency report with:

```powershell
python scripts/wq_submit_efficiency_report.py `
  --current-run-dirs reports/public_harness_demo/experiments/<exp-id>/presubmit_run `
  --current-name public-demo `
  --output reports/public_harness_demo/efficiency_summary.json `
  --markdown-output reports/public_harness_demo/efficiency_summary.md `
  --events-output reports/public_harness_demo/efficiency_events.jsonl
```

The efficiency report is separate from `harness_score`. It tracks the funnel
from candidates to simulations, ready candidates, real submit attempts, and
active/successful outcomes. Public demo runs have no real submit attempts, so
`submit_efficiency_score` is intentionally blank while presubmit efficiency
metrics are still populated.

## Period Quality Review

The alpha quality review is a time-window report for recent real submissions
and generated `UNSUBMITTED` alphas:

```powershell
python scripts/wq_alpha_quality_review.py `
  --reports reports `
  --window-days 14 `
  --check-policy window_unsubmitted `
  --max-checks 50 `
  --output-dir reports/wq_alpha_quality_review_latest
```

It uses only read-only WQ API calls plus the check-only review endpoint. It does
not submit or delete alphas. The report writes quality tables, a self-correlation
pressure table, recommended next synthesis directions, an Obsidian Markdown note,
and a local research-profile candidate that must be applied explicitly.

`period_quality_score` is a weighted score over submitted quality, generated
quality, correlation quality, and diversity. Missing components, such as a
window with no real submissions, are omitted and the remaining weights are
renormalized.

## Candidate Identity

Efficiency tracking uses a stable `candidate_uid`:

```text
sha256(expression_hash + settings_hash)
```

`expression_hash` is based on the normalized expression. `settings_hash`
normalizes account, region, universe, delay, decay, neutralization, truncation,
`maxTrade`, and `maxPosition`. This makes the same expression under different
simulation settings traceable as separate candidate lifecycles.
