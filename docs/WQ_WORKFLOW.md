# WorldQuant Workflow

This document is the canonical runbook for the current WQ mining flow. The goal
is to keep daily operation clear: know which command finds candidates, which
command only checks, and which command can submit.

For a no-credential reproducible example, start with
[PUBLIC_HARNESS_DEMO.md](PUBLIC_HARNESS_DEMO.md). For artifact fields and the
`harness_score` formula, see
[HARNESS_ARTIFACTS_AND_SCORE.md](HARNESS_ARTIFACTS_AND_SCORE.md). For submit
and credential boundaries, see
[SECURITY_AND_LIMITATIONS.md](SECURITY_AND_LIMITATIONS.md).

## Canonical Paths

| Purpose | Command | Submit behavior |
| --- | --- | --- |
| Build a sequential pre-submit shortlist | `python scripts/wq_agent_workflow.py presubmit-sequential ...` | Never submits |
| Submit explicitly selected alpha IDs | `python scripts/wq_agent_workflow.py submit --alpha-ids ...` | Submits only those IDs |
| Run an authorized submit loop | `python scripts/wq_agent_workflow.py run-submit --target-submissions N ...` | Submits only when target is explicit |
| Generate local research-memory candidates | `python scripts/wq_research_miner.py generate ...` | Never submits, never simulates |
| Run a gated research sandbox | `python scripts/wq_research_sandbox.py new/mine/gate ...` | Never submits |
| Evaluate/evolve a research sandbox | `python scripts/wq_research_sandbox.py eval/evolve/report ...` | Never submits |
| Daily find/check mining | `python scripts/wq_daily_mining.py --config configs/wq_daily_mining.example.json` | Never submits |
| Check latest find-only status | `python scripts/wq_status.py --kind find-only` | Read-only |
| Check latest legacy loop status | `python scripts/wq_status.py --kind loop` | Read-only |

`logs/*_latest.json` is only a pointer. The authoritative state is the
`status.json`, `loop_status.json`, or `summary.json` inside the run directory.
Use `scripts/wq_status.py` or the PowerShell wrappers to avoid reading stale
latest files directly.

## Recommended Daily Flow

### Research sandbox flow

For new factor directions, prefer the local research sandbox. It wraps the
existing research miner and `presubmit-sequential` workflow with an experiment
record, candidate specs, a critic report, and a fixed gate decision.

```powershell
python scripts/wq_research_sandbox.py new `
  --topic "cashflow options decorrelation" `
  --hypothesis "Cash-flow quality plus a small options-skew overlay may reduce self-correlation"

python scripts/wq_research_sandbox.py mine `
  --experiment reports/wq_research_experiments/<exp-id> `
  --run-dirs reports/wq_agent_runs/<prior-run> `
  --legal-inputs configs/wq_legal_inputs.default.json `
  --target-ready 3 `
  --max-total-simulations 120 `
  --cycle-candidate-count 20

python scripts/wq_research_sandbox.py gate `
  --experiment reports/wq_research_experiments/<exp-id>

python scripts/wq_research_sandbox.py eval `
  --experiment reports/wq_research_experiments/<exp-id> `
  --submit-run-dirs reports/wq_agent_runs/<explicit-submit-run>

python scripts/wq_research_sandbox.py evolve `
  --experiment reports/wq_research_experiments/<exp-id> `
  --eval-dir reports/wq_research_experiments/<exp-id>/evaluations/<eval-id>
```

The sandbox writes `experiment.yaml`, `candidate_specs.jsonl`,
`experience_memory.jsonl`, `presubmit_run/`, `critic_report.yaml`, and
`decision.yaml` under `reports/wq_research_experiments/<exp-id>/`. The files are
local research artifacts; even a `promote_candidate` decision means "ready for
human review / explicit submit selection", not automatic submission.

The harness evaluation writes `evaluations/<eval-id>/eval_records.csv`,
`eval_summary.json`, `summary_by_field_signature.csv`,
`summary_by_reject_reason.csv`, `gate_report.json`, and `run_report.md`. The
core metrics are ready candidates per 100 simulations, self-correlation reject
share, too-similar reject share, duplicate field signatures, hypothesis-to-ready
latency, illegal input reject share, and promote-to-real-submit success rate.
The last metric is calculated only from explicitly supplied submit run
directories.

### Alpha submit-efficiency tracking

Use the submit-efficiency report when comparing research profiles, field
families, or real submit batches:

```powershell
python scripts/wq_submit_efficiency_report.py `
  --experiment-root reports/wq_research_experiments `
  --agent-run-root reports/wq_agent_runs `
  --current-name all-local-runs `
  --output reports/wq_efficiency/summary.json `
  --markdown-output reports/wq_efficiency/summary.md `
  --events-output reports/wq_efficiency/events.jsonl
```

The report writes a candidate → simulated → reviewed → ready → submitted →
active funnel, source-family / field-signature / settings leaderboards, reject
reason counts, and stable `candidate_uid` lifecycle events. `submit_efficiency_score`
is calculated only when real submit attempts are present; no-submit presubmit
runs still report ready yield and rejection waste.

### Period alpha quality review

Use the quality review when you want a weekly/biweekly read on whether recent
submissions and generated `UNSUBMITTED` alphas are improving:

```powershell
python scripts/wq_alpha_quality_review.py `
  --reports reports `
  --window-days 14 `
  --check-policy window_unsubmitted `
  --max-checks 50 `
  --output-dir reports/wq_alpha_quality_review_latest
```

The review combines local run artifacts, existing factor-map outputs, the WQ
`/users/self/alphas` list, and limited check-only results for recent
`UNSUBMITTED` alphas. It writes submitted/unsubmitted quality tables,
self-correlation pressure by field signature/source family/domain, an Obsidian
Markdown review, and a local research profile candidate. The profile candidate
is not applied automatically.

Primary metrics are `submitted_quality_score`, `generated_quality_score`,
`correlation_quality_score`, `diversity_score`, and `period_quality_score`.
When no real submitted alpha exists in the window, the submitted component is
omitted and the remaining score weights are renormalized. A high
self-correlation pressure group should usually trigger a new field/operator
family or cross-domain overlay, not another small window/decay/truncation tweak.

### Concentrated-weight lesson

Recent live runs showed that `CONCENTRATED_WEIGHT` is usually not fixed by
rerunning the same expression with lower truncation, added decay, or another
`group_rank`/`group_neutralize` wrapper. In many failures WQ still had hundreds
or more long/short instruments, but the portfolio weight distribution was too
peaked. Treat these as source-design failures, not parameter-tuning failures.

The research miner now blocks candidates with `concentration_sparse_group_risk`
when they combine multiple sparse legs such as `enterprise_value`,
`cashflow_op`, dividend fields, or `pcr_*` fields with group transforms. Keep at
most one sparse main leg, avoid sparse denominators before group operations, and
require a broad price-volume or model-field dispersion leg such as `volume`,
`vwap`, `cap`, `forward_sales_to_price`, or related high-coverage model fields.
Recent internal runs also showed that a PCR-only sparse leg without explicit
price-volume dispersion can still trigger concentrated-weight failure; treat
`pcr_*` as a low-coverage leg that needs `volume`/`vwap`/`adv*`/price dispersion,
not only model overlays.

### Near-ready self-correlation lesson

Internal near-ready runs found useful but not submit-ready families where
headline metrics passed local thresholds but self-correlation remained too high.
Treat those as structure problems rather than parameter-tuning problems; the
next repair must change field family or operator family before any explicit
submit.

Short-window news/credit, social buzz, and news open-gap reactions have also
shown weak metrics, high turnover, or concentrated-weight failures in internal
runs. Use them only after adding slow windows and broad liquidity controls.

### Operational flow

1. Sync platform inventory and build a pre-submit shortlist:

   ```powershell
   python scripts/wq_agent_workflow.py presubmit-sequential `
     --output-dir reports/wq_agent_runs/<run_id> `
     --candidate-files <candidate_file.jsonl> `
     --legal-inputs configs/wq_legal_inputs.default.json `
     --target-ready 4 `
     --max-total-simulations 40 `
     --cycle-candidate-count 4
   ```

2. When a run stalls, generate a local research-memory candidate file from
   previous ready/rejected artifacts, then feed it back into presubmit:

   ```powershell
   python scripts/wq_research_miner.py generate `
     --output reports/wq_agent_runs/<candidate_file>.jsonl `
     --run-dirs reports/wq_agent_runs/<prior_run_id> reports/wq_agent_runs/<another_prior_run_id> `
     --ready-files reports/wq_agent_runs/<run_id>/presubmit_ready_sequential.jsonl `
     --rejected-files reports/wq_agent_runs/<run_id>/presubmit_rejected.jsonl `
     --active-inventory-files reports/wq_agent_runs/<run_id>/active_inventory.json `
     --legal-inputs configs/wq_legal_inputs.default.json `
     --similarity-cutoff 0.72 `
     --max-family-count 8 `
     --max-field-signature-count 4 `
     --max-candidates 200
   ```

   This planner is local-only by default (`--llm-provider none`). It distills
   ready/rejected rows into experience memory, screens exact duplicates and high
   similarity candidates, mines prior run directories, and writes a diversified
   candidate JSONL for the existing `presubmit-sequential` evaluator. The
   planner cutoff may be looser than the final presubmit cutoff; the strict
   `presubmit-sequential` virtual similarity gate remains authoritative.

3. Review `presubmit_ready_sequential.jsonl`. A row is ready only when:
   - base WQ submit thresholds pass,
   - check-only review is readable,
   - self-correlation is `PASS` and below `0.7`,
   - no failed platform checks are present,
   - similarity to real or virtual ACTIVE inventory is within the configured cutoff.

4. Before any real submit, recheck selected alpha IDs:

   ```powershell
   python scripts/check_wq_submissions.py `
     --ids <alpha_id_1> <alpha_id_2> `
     --output reports/wq_daily/pre_submit_recheck.jsonl `
     --summary-output reports/wq_daily/pre_submit_recheck_summary.json `
     --account primary `
     --chunk-size 1
   ```

5. Submit only explicit IDs:

   ```powershell
   python scripts/wq_agent_workflow.py submit `
     --output-dir reports/wq_agent_runs/<submit_run_id> `
     --alpha-ids <alpha_id_1> <alpha_id_2> `
     --submit-count 0
   ```

## Community Refresh

For daily Community mining, prefer a Playwright storage state instead of
copying browser cookies by hand. This keeps the refresh local-only and avoids
external model/API dependencies.

1. Install the optional browser dependency once:

   ```powershell
   python -m pip install -e ".[community]"
   python -m playwright install chromium
   ```

2. Refresh the login state when needed:

   ```powershell
   python scripts/wq_community_login_state.py
   ```

   A browser opens. Log in to WQ Community, then press Enter in the terminal.
   The session is saved to `.secrets/wq_community_state.json`, which is ignored
   by git.

3. Run the daily refresh:

   ```powershell
   python scripts/wq_community_daily_refresh.py `
     --output-root <local_tmp> `
     --run-prefix worldquant_community_daily `
     --max-pages 20 `
     --max-posts 500 `
     --comments-max-pages 5 `
     --max-comments-per-post 500
   ```

   The script loads `.secrets/wq_community_state.json`, derives a Community
   Cookie header in memory, exports posts/comments, and runs triage. If the
   stored session has expired and live export returns 401, it falls back to the
   newest local `<local_tmp>/worldquant_community*` cache and records the reason in
   `daily_refresh_manifest.json`.

4. Build reusable forum idea memory and community skill memory from the triage output:

   ```powershell
   python scripts/build_wq_forum_idea_memory.py `
     --triage-dir <local_tmp>\worldquant_community_daily_<date>\triage `
     --output-dir reports\wq_forum_research_<date>\idea_memory `
     --source-label daily `
     --top-sources 6

   python scripts/build_wq_community_skill_memory.py `
     --triage-dir <local_tmp>\worldquant_community_daily_<date>\triage `
     --forum-memory-dirs reports\wq_forum_research_<date>\idea_memory `
     --output-dir <local_tmp>\worldquant_community_daily_<date>\skill_memory `
     --source-label daily
   ```

   For a wider historical sample, increase `--max-pages` and `--max-posts` in
   the refresh command first, then run the same builders against that longer
   triage directory. The memory builders are deterministic and local-only: they
   write theme clusters, candidate recipes, pattern rules, and community skills,
   but they do not call external LLM APIs, simulate, or submit. Community skill
   memory now includes both the backward-compatible `community::*` routes and
   finer `community_failure::*` failure-action routes, such as metric near-pass
   overlay repair, correlation family-shift repair, template clone blocking,
   low-coverage concentration repair, turnover/density repair, pending-check
   gating, and duplicate blocking.

5. Convert community/forum memory into a conservative submission policy:

   ```powershell
   python scripts/build_wq_submission_plan.py `
     --forum-memory-dirs reports\wq_forum_research_<date>\idea_memory `
     --community-skill-memory-file <local_tmp>\worldquant_community_daily_<date>\skill_memory\community_skill_memory.jsonl `
     --output-dir reports\wq_submission_plan_<date> `
     --no-obsidian
   ```

   Community skills are used as gates, risk flags, and repair routes. Do not
   pass `community_skill_memory.jsonl` directly as a large candidate file.
The preferred presubmit path is still `presubmit-sequential`; pass the
generated `submission_policy.json` and the triage directory as context.

Each workflow run now writes detailed iteration audit artifacts by default:
`iteration_audit.jsonl`, `iteration_audit_summary.json`, and
`iteration_audit.md`. These files explain what was tweaked, the resulting
metrics/check status, concrete failure causes, and next actions. The Markdown
report and default JSONL withhold full alpha expressions and use expression
hashes, field signatures, operators, and metrics instead. Use
`--audit-include-expressions` only for local debugging when full expression
traceability is required; use `--no-iteration-audit` to disable this layer.

6. Optional Windows daily task:

   ```powershell
   schtasks /Create /F /SC DAILY /ST 07:30 /TN worldquant_harness_WQ_Community_Daily /TR "powershell -NoProfile -ExecutionPolicy Bypass -Command cd <repo_root>; python scripts\wq_community_daily_refresh.py --output-root <local_tmp> --run-prefix worldquant_community_daily"
   ```

## Legacy Scripts

These scripts remain available for compatibility, but they are no longer the
preferred daily entrypoints.

| Script | Current role |
| --- | --- |
| `scripts/wq_find_only.py` | Low-level worker for find-only simulations; never submits |
| `scripts/start_wq_find_only_job.py` | Background wrapper around `wq_find_only.py` |
| `scripts/run_wq_loop.py` | Legacy sequential loop; can submit only with `--auto-submit` |
| `scripts/start_wq_loop_job.py` | Background wrapper around the legacy loop |
| `scripts/wq_auto_mine.py` | Legacy autonomous mining path |

Prefer `wq_agent_workflow.py presubmit-sequential` for new pre-submit work.
Use `wq_daily_mining.py` for scheduled find/check collection.

## Status Files

All new run artifacts should include:

- `schema_version`
- `canonical_entrypoint`
- `submit_guard`
- `authoritative_status_file`
- `legacy_entrypoint` when the command is a compatibility wrapper

Status readers should resolve the authoritative status file first. If the
latest pointer says `RUNNING` but the run status says `STOPPED`, the run status
wins.

## Ledger Defaults

Use ledger blocking by default unless deliberately re-testing an old path.
Current failure memory is valuable for avoiding repeat work: high similarity,
self-correlation failures, and weak metric families should usually be blocked
before spending another WQ simulation.
