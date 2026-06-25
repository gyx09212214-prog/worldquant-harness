# Security And Limitations

worldquant-harness is a research system. It is not an investment adviser, trading system,
or guarantee of future performance.

It is not affiliated with, endorsed by, sponsored by, or officially connected to
WorldQuant, WorldQuant BRAIN, or any external data/trading platform.

![Submit boundary](images/submit-boundary.svg)

## Submit Boundary

No-submit paths:

- `scripts/run_public_harness_demo.py`
- `scripts/wq_research_sandbox.py new/mine/gate/eval/evolve/report`
- `scripts/wq_research_miner.py generate`
- `scripts/wq_agent_workflow.py presubmit-sequential`
- `scripts/wq_daily_mining.py`
- check-only status and validation commands

Submit-capable paths:

- `scripts/wq_agent_workflow.py submit --alpha-ids ...`
- `scripts/wq_agent_workflow.py run-submit --target-submissions ...`
- MCP/API submit tools such as `wq_brain_submit`, `wq_brain_batch_submit`,
  `wq_brain_submit_by_ids`, and `wq_brain_finalize_submissions`

Treat a sandbox `promote_candidate` decision as "ready for review", not as
permission to submit automatically.

## Credentials

Credentials must stay in environment variables or ignored local files:

- `.env` and `.env.*` are ignored by git
- `.secrets/` is ignored by git
- public demo and local sandbox fixtures do not require WQ BRAIN, DeepSeek, Wind,
  or RQDatac credentials

Do not commit real API keys, WQ credentials, browser cookies, platform session
states, or private research reports.

For vulnerability reporting and PR secret checks, see the root
[`SECURITY.md`](../SECURITY.md).

## External Platform Data

Some workflows can query WorldQuant BRAIN or WQ Community when credentials are
configured. Public documentation and examples should avoid bundling private
platform outputs unless they are intentionally sanitized and permitted to share.

![Open-source release boundary](images/release-safety-boundary.svg)

Treat real platform screenshots, full field-discovery registries, complete
submission ledgers, and raw check/submit artifacts as private by default.

Local reference catalogs copied from third-party repositories must keep source
attribution. If an upstream repository has no license, treat the files as local
research references rather than redistributable public assets.

## Research Limits

- Public harness demo fixtures validate workflow mechanics, not investment
  performance.
- Historical factor screenshots and metrics are examples only; they do not
  imply future results.
- Local A-share backtests depend on free public data sources and daily-frequency
  assumptions.
- LLM-generated candidates can be wrong, redundant, overfit, or invalid; the
  harness is designed to surface those failures rather than hide them.
- Real platform policies, available data fields, and submission checks can
  change over time.
