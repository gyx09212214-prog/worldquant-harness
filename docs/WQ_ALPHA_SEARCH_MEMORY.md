# WQ Alpha Search Memory

This page documents the first Alpha-GPT style search-memory layer in QuantGPT.

## Purpose

The module turns local WorldQuant run artifacts into a repeatable research loop:

1. Merge simulation, check-only, and submit events into one trajectory ledger per alpha.
2. Score source families by WQ score, platform eligibility, check-readable alphas, active alphas, and repeated failures.
3. Persist reusable skill memory instead of relying on one-off chat context.
4. Generate submit/check queues for high-score, low-correlation alphas, with a default target of five submitted factors.
5. Generate a near-pass repair queue for alphas with strong WQ score but near-cutoff SELF_CORRELATION failures.

This follows the practical thread across Alpha-GPT, RD-Agent-Quant, QuantaAlpha, FactorMiner, and Hubble: keep the agent loop evidence-driven, replayable, and constrained before spending more simulation budget.

## Command

```powershell
python scripts/wq_alpha_search_memory.py `
  --run-dirs reports/wq_agent_runs/submit1_sharpe25_20260629 `
  --output-dir reports/wq_alpha_search_memory_submit1 `
  --target-submit-count 5
```

The command writes:

- `trajectory_ledger.jsonl`: merged lifecycle records keyed by alpha/candidate/expression.
- `skill_memory.jsonl`: reusable repair and family skills.
- `family_scores.json`: family-level priority scores and conversion counts.
- `near_pass_repair_candidates.jsonl`: settings-grid repair candidates.
- `top_submit_targets.jsonl`: check-readable high-score candidates ready for explicit submit.
- `top_check_targets.jsonl`: high-score candidates that should run check-only before submit.
- `alpha_search_report.md`: human-readable funnel and next queue.
- `summary.json`: full machine-readable run summary.

## WQ Score Objective

The objective is no longer Sharpe maximization. The ranking uses:

- Platform `fitness` when available.
- Estimated WQ fitness when missing: `sharpe * sqrt(abs(returns) / max(turnover, 0.125))`.
- Positive returns as a secondary term.
- Turnover inside the platform band `[0.01, 0.70]`.
- Lower SELF/PROD correlation risk.
- Check-readable candidates before unchecked candidates.

The platform minimum gate still uses the repository's `submit_threshold_checks`, because submitting candidates that cannot pass LOW_SHARPE/LOW_FITNESS/turnover checks wastes quota. This is a gate, not the optimization objective.

## Near-Pass Repair Rule

The first implemented repair skill is `near_sc_cutoff_settings_repair`.

It selects parents with:

- WQ score at or above `--min-parent-score` (default `1.0`).
- Turnover inside `[0.01, 0.70]`.
- SELF_CORRELATION fail or equivalent status.
- `sc_value` inside `[0.70, 0.82]`.
- No LOW_SUB_UNIVERSE failure.

It keeps the expression fixed and varies only:

- `neutralization`
- `decay`
- `truncation`

This is intentionally conservative: it isolates platform-correlation effects before asking the generator for a new alpha thesis.

## Recommended Loop

1. Run search memory on the latest mining/submission directory.
2. Submit from `top_submit_targets.jsonl` only when it contains check-readable high-score candidates.
3. Run check-only on `top_check_targets.jsonl` to fill the five-factor target.
4. Simulate `near_pass_repair_candidates.jsonl` when the check queue is short or correlation failures dominate.
5. Re-run search memory so successful and failed outcomes update the skill memory.
