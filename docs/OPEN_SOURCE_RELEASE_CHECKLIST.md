# Open Source Release Checklist

Use this checklist before pushing a public GitHub release.

Target public repository for this release:

- `https://github.com/gyx09212214-prog/worldquant-harness`

## Required Gates

- `python -m ruff check worldquant_harness tests`
- `pytest tests/ -x -q --cov=worldquant_harness --cov-report=term-missing --cov-fail-under=33`
- `python scripts/run_public_harness_demo.py --output-root .test_tmp/public_harness_demo_ci --run-id public-harness-demo-ci`
- `python scripts/validate_public_harness_artifacts.py .test_tmp/public_harness_demo_ci`
- `npm --prefix frontend audit --audit-level=low`
- `npm --prefix frontend run build`

`pyright worldquant_harness/` is currently advisory. The repository has known type debt in
SQLAlchemy, pandas, and artifact-heavy workflow code; do not make pyright a hard
release gate until that debt is intentionally reduced.

## Current Verification Snapshot

Last verified: 2026-06-25.

- Public docs/new package legacy-name scan: no hits in publishable paths.
- Public docs/example alpha-id and secret scan: 0 hits.
- Markdown/SVG image link scan: 0 missing links.
- Unused image scan: 0 unused images after removing the old `star-history.png`.
- `git ls-files .env .secrets data reports logs references "*.db"`: no tracked private runtime files.
- `python -m ruff check worldquant_harness tests`: passed.
- `pytest tests/ -x -q --cov=worldquant_harness --cov-report=term-missing --cov-fail-under=33`: 619 passed, 2 skipped, total coverage 65.02%.
- `python scripts/run_public_harness_demo.py --output-root .test_tmp/public_harness_demo_ci --run-id public-harness-demo-ci`: passed, no real submit attempted.
- `python scripts/validate_public_harness_artifacts.py .test_tmp/public_harness_demo_ci`: passed with `manifest_contains_absolute_paths` warning in ignored local output.
- `npm --prefix frontend audit --audit-level=low`: 0 vulnerabilities.
- `npm --prefix frontend run build`: passed; Vite reports a large main chunk as a follow-up optimization.

Run the full required gates again immediately before pushing.

## Publishable By Default

- Core source under `worldquant_harness/`
- Tests under `tests/`
- Public docs under `docs/`
- Public harness demo scripts:
  - `scripts/run_public_harness_demo.py`
  - `scripts/validate_public_harness_artifacts.py`
  - `scripts/build_public_visual_pack.py`
- Sanitized visual assets under `docs/images/`
- `.env.example`, `LICENSE`, `NOTICE`, `DISCLAIMER.md`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`

## Keep Local By Default

- `.env`, `.env.*`, `.secrets/`
- `*.db`, including `worldquant_harness.db` and legacy local database files
- `data/`, `logs/`, `reports/`
- `references/`
- Raw platform exports, check results, submit ledgers, and full submission
  history artifacts
- `scripts/local_seed_expressions.json`
- `scripts/wq_loop_submit_candidates*.jsonl`
- Full field-discovery registries such as `configs/wq_legal_inputs.default.json`
- Local seed files such as `scripts/local_seed_expressions.json`
- Private submit batches such as `scripts/wq_loop_submit_candidates*.jsonl`

## Requires Explicit Review

- `example_factor/` platform screenshots and metrics
- Any doc containing real alpha IDs, real platform metrics, or exact historical
  submitted expressions
- Any third-party reference copied from a repository without a clear license
- Any generated artifact containing absolute local paths
- Any real alpha ID, exact submitted expression, or platform output planned for
  README/docs/example publication

## Suggested Commit Groups

Keep the release reviewable by splitting the current worktree into focused commits:

1. `chore: rename project to worldquant-harness`
   - legacy package deletion and `worldquant_harness/` package addition
   - package metadata, imports, Docker/Makefile/CI/frontend name changes
   - no new research behavior unless required for the rename

2. `feat: add public agent harness contract and demo`
   - `worldquant_harness/harness_contracts.py`
   - `worldquant_harness/harness_runner.py`
   - public demo/eval/validation scripts
   - harness contract tests and public artifact tests

3. `docs: prepare open-source release materials`
   - README, visual guide, release checklist, audit, disclaimer, security,
     contributing, code of conduct, NOTICE updates
   - sanitized SVG assets under `docs/images/`
   - explicit no-submit and release-boundary documentation

4. `fix: enforce live submit self-correlation cutoff`
   - `scripts/wq_live_submit_candidates.py`
   - `tests/test_wq_live_submit_candidates.py`

Do not commit ignored local state such as `.env`, `.secrets/`, `data/`,
`reports/`, `references/`, local databases, local candidate batches, or
generated run outputs.

## Final Publish Checks

- Confirm the GitHub owner/repository is correct in badges, clone commands, and frontend copy.
- Confirm the remote named `origin` points to the new public repository, not the old repository.
- Confirm any retained copyright notices are intentional and legally correct.
- Confirm `example_factor/` screenshots and metrics are intentionally public.
- Confirm real submit-capable commands are documented as credentialed and explicit.

## Protocol And Policy Files

- `LICENSE`: MIT software license.
- `NOTICE`: authorship, non-affiliation, and publication boundary.
- `DISCLAIMER.md`: no-investment-advice and external-platform boundary.
- `SECURITY.md`: vulnerability reporting and secret-handling policy.
- `CODE_OF_CONDUCT.md`: project participation rules.
- `CONTRIBUTING.md`: contribution terms and PR publication checklist.

## Release Positioning

![Open-source release boundary](images/release-safety-boundary.svg)

Lead with the deterministic public harness demo. It proves the engineering
workflow: candidate lifecycle, no-submit boundary, presubmit gate, harness
score, memory feedback, and profile evolution.

Avoid positioning the public repo as a collection of live platform alphas.
Private platform performance can be mentioned only after sanitization and an
explicit decision that the data is permitted to share.
