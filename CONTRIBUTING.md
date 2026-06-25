# Contributing to worldquant-harness

感谢你对 worldquant-harness 的关注！以下是参与贡献的指南。

## Getting Started

```bash
git clone https://github.com/gyx09212214-prog/worldquant-harness.git
cd worldquant-harness
make setup      # creates venv, installs deps, copies .env
make test       # run tests
make lint       # ruff + pyright
```

No paid API keys required for basic development — the expression engine and backtest work without DeepSeek.

## Development Workflow

1. Fork the repo and create a feature branch from `main`
2. Make your changes
3. Run `make lint && make test` to verify
4. Submit a PR with a clear description

## Code Style

- **Python**: [Ruff](https://docs.astral.sh/ruff/) for linting, [Pyright](https://github.com/microsoft/pyright) for type checking (basic mode)
- **TypeScript**: Strict mode enabled, Vite + React 18
- Commit messages: `feat:`, `fix:`, `chore:`, `docs:` prefixes (Chinese or English body)

## Project Structure

```
worldquant_harness/                  # Python backend
├── api_server.py          # FastAPI app + routes
├── expression_parser.py   # Factor expression engine (50+ operators)
├── backtest.py            # Group backtest engine
├── market_data.py         # baostock + akshare data pipeline
├── anti_overfit.py        # Statistical anti-overfit detection
├── rolling_validator.py   # Walk-forward validation
├── models.py              # SQLAlchemy ORM (SQLite/PostgreSQL)
├── routes/                # API route modules
└── mcp_server.py          # MCP tools for AI agents

frontend/                  # React TypeScript SPA
├── src/components/        # UI components
├── src/api/               # HTTP client layer
├── src/hooks/             # Custom React hooks
└── src/contexts/          # Auth + theme context
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed design documentation.

## What to Contribute

### Good first issues

- Add missing tests for auth, market data edge cases
- Improve error messages (standardize language)
- Add new operators to `expression_parser.py`

### Medium

- Add new data source adapters (Tushare, Yahoo Finance, etc.)
- Improve transaction cost model in `backtest.py`
- Add code splitting / lazy loading to frontend

### Advanced

- OHLC data integrity validation pipeline
- New anti-overfit detection methods
- Multi-factor portfolio optimization improvements

## Testing

```bash
make test                  # all tests
.venv/bin/pytest tests/test_expression_parser.py -v   # specific file
```

Tests use SQLite in-memory database. No external services needed.

## Frontend Development

```bash
cd frontend
npm ci
npm run dev                # starts Vite dev server on :5173
```

The dev server proxies `/api` to `localhost:8003`. Run the backend with `make dev` in a separate terminal.

## Pull Request Guidelines

- Keep PRs focused — one feature or fix per PR
- Include tests for new functionality
- Update documentation if you change public APIs
- CI must pass (lint + tests + frontend build)
- Do not include private platform exports, raw submit/check ledgers, credentials,
  browser cookies, local databases, or full research-history artifacts
- Use synthetic fixtures for tests and public examples unless a real artifact has
  been intentionally sanitized and approved for release

## Security And Publication Boundary

Before opening a PR, review:

- [DISCLAIMER.md](DISCLAIMER.md)
- [SECURITY.md](SECURITY.md)
- [docs/SECURITY_AND_LIMITATIONS.md](docs/SECURITY_AND_LIMITATIONS.md)
- [docs/OPEN_SOURCE_RELEASE_CHECKLIST.md](docs/OPEN_SOURCE_RELEASE_CHECKLIST.md)

Run:

```bash
git status --short
git check-ignore -v .env .secrets data reports logs references local.db
```

If your change touches WQ BRAIN, WQ Community, credentials, submission commands,
or report publication, state clearly whether it is read-only, no-submit,
check-only, or submit-capable.

## Reporting Issues

Use the GitHub issue templates:
- **Bug Report**: Steps to reproduce, expected vs actual behavior
- **Feature Request**: Motivation and proposed solution
- **Factor Research**: Factor hypotheses and backtesting results

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

You also agree that you have the right to submit your contribution and that it
does not knowingly include confidential credentials, private platform data, or
third-party material that cannot be redistributed under the project license.
