# Security Policy

## Supported Versions

Security fixes are handled on the default branch. Public releases should be cut from a
commit that passes the release checklist in `docs/OPEN_SOURCE_RELEASE_CHECKLIST.md`.

## Reporting A Vulnerability

Please do not open a public issue containing credentials, private alpha expressions,
platform exports, or exploit details.

Use a private GitHub security advisory when available. If that is not available, open a
minimal issue that says a security report exists and avoid sensitive details.

Include:

- affected commit or version
- reproduction steps using synthetic data when possible
- impact
- whether credentials, platform records, or private research artifacts may be exposed

## Secret Handling

The repository is designed so local secrets stay outside Git:

- `.env` and `.env.*` are ignored except `.env.example`
- `.secrets/` is ignored
- `data/`, `reports/`, `logs/`, `references/`, and local database files are ignored
- raw WQ BRAIN submit/check ledgers are private by default

Before publishing or opening a PR, run:

```bash
git status --short
git check-ignore -v .env .secrets data reports logs references local.db
```

## Real Submission Boundary

Public demo, sandbox, presubmit, and check-only flows must not submit to external platforms.
Submit-capable commands must require explicit user intent and user-provided credentials.

See `DISCLAIMER.md` and `docs/SECURITY_AND_LIMITATIONS.md` for the full boundary.
