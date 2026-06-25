# Open Source Audit

Last updated: 2026-06-25

This audit records the checks needed before publishing `worldquant-harness` as a public
GitHub repository.

Target repository: `https://github.com/gyx09212214-prog/worldquant-harness`

## Current Decision

The repository is suitable for an open-source MVP after release cleanup. The public story
should be the harness loop: candidate lifecycle, no-submit public demo, presubmit gates,
memory feedback, profile evolution, and explicit submit boundary.

Do not publish the private research corpus as the product.

![Open-source release boundary](images/release-safety-boundary.svg)

## Files Safe To Publish

- `worldquant_harness/`
- `tests/`
- `docs/` after reviewing generated screenshots and metrics
- `scripts/run_public_harness_demo.py`
- `scripts/run_public_harness_eval.py`
- `scripts/validate_public_harness_artifacts.py`
- `scripts/build_public_visual_pack.py`
- `.env.example`
- `README.md`
- `LICENSE`
- `NOTICE`
- `DISCLAIMER.md`
- `SECURITY.md`
- `CODE_OF_CONDUCT.md`
- `CONTRIBUTING.md`

## Files Private By Default

- `.env`, `.env.*`, `.secrets/`
- `*.db`
- `data/`, `reports/`, `logs/`, `references/`
- raw WQ BRAIN platform exports
- raw submit/check ledgers
- complete submission history
- real browser cookies or authorization headers
- full field-discovery registries
- local candidate seed files and private submit batches

## Audit Findings

1. Local secret and runtime paths are ignored by `.gitignore`.
2. `git ls-files` shows no tracked `.env`, local database, `data/`, `reports/`, `logs/`, or
   `references/` entries.
3. The repository already uses the MIT License.
4. The release boundary needed stronger docs, so the repository now includes root-level
   disclaimer, security policy, and code-of-conduct files.
5. The old unreferenced `docs/images/star-history.png` asset was removed because it
   contained stale project branding.
6. `scripts/wq_live_submit_candidates.py` now enforces the local self-correlation
   cutoff even when the platform check row says `PASS`.
7. Some docs and example screenshots still require human review before a public push,
   especially anything under `example_factor/` or generated from real platform runs.

## Release Gate Commands

```bash
git status --short
git ls-files .env .secrets data reports logs references "*.db"
python -m ruff check worldquant_harness tests
pytest -q
python scripts/run_public_harness_demo.py --output-root .test_tmp/public_harness_demo_ci --run-id public-harness-demo-ci
python scripts/validate_public_harness_artifacts.py .test_tmp/public_harness_demo_ci
npm --prefix frontend audit --audit-level=low
npm --prefix frontend run build
```

## Human Review Questions

- Are all included screenshots and metrics intentionally public?
- Are any real alpha IDs or exact submitted expressions present in public docs?
- Are copied third-party references backed by a clear license?
- Does the README lead with harness mechanics rather than live performance?
- Are submit-capable commands clearly marked as explicit, credentialed actions?
