# Quick Start

This guide starts with the public harness demo because it is deterministic and
does not require WQ BRAIN, DeepSeek, Wind, or private credentials.

Use Python 3.10 or newer (3.12 recommended). The web UI additionally requires
Node.js 20 or newer and npm 10 or newer. Docker is an alternative when you do
not want to install the Python and Node.js toolchains locally.

The default Python installation is WQ-only. It contains the WQ BRAIN client,
simulation/check/submit workflows, environment loading, and the local SQLite
event ledger. It does not install the A-share data stack, local backtester,
HTTP/MCP server, frontend, or an LLM client.

```powershell
python -m pip install -e .
```

Install optional features only when needed:

```powershell
python -m pip install -e ".[llm]"            # model-generated candidates
python -m pip install -e ".[server]"         # HTTP and MCP services
python -m pip install -e ".[migrations]"     # Alembic schema migrations
python -m pip install -e ".[local-backtest]" # pandas/A-share local backtests
python -m pip install -e ".[all,dev]"        # full development and CI environment
```

## 1. Public Harness Demo

```powershell
git clone https://github.com/gyx09212214-prog/worldquant-harness.git
cd worldquant-harness
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env

python scripts/run_public_harness_demo.py --output-root reports/public_harness_demo
python scripts/validate_public_harness_artifacts.py reports/public_harness_demo
python scripts/wq_submit_efficiency_report.py `
  --run-roots reports/public_harness_demo `
  --current-name public-demo `
  --output reports/public_harness_demo/efficiency_summary.json `
  --markdown-output reports/public_harness_demo/efficiency_summary.md `
  --events-output reports/public_harness_demo/efficiency_events.jsonl
python scripts/wq_alpha_quality_review.py `
  --reports reports/public_harness_demo `
  --no-platform `
  --no-profile-candidate `
  --output-dir reports/public_harness_demo/quality_review
python scripts/build_public_visual_pack.py `
  --source reports/public_harness_demo `
  --output-dir docs/images `
  --report docs/VISUAL_GUIDE.md
```

The demo creates a guarded sandbox experiment, runs `presubmit-sequential` with
fake platform/simulation/check adapters, applies the sandbox gate, evaluates the
harness score, and creates a child experiment for the next generation. It never
calls a real submit endpoint.

Expected high-level result:

- `real_submit_attempted: false`
- one ready candidate
- duplicate, illegal-input, near-miss, and strict self-correlation rejection examples
- `eval_summary.json`, `run_report.md`, and `evolution_result.json`
- `efficiency_summary.md` with the candidate → simulation → ready funnel
- `quality_review.md` with generated-alpha quality and self-correlation pressure
- `docs/VISUAL_GUIDE.md` and `docs/images/*.svg` with the public visual onboarding pack

See [PUBLIC_HARNESS_DEMO.md](PUBLIC_HARNESS_DEMO.md) and
[HARNESS_ARTIFACTS_AND_SCORE.md](HARNESS_ARTIFACTS_AND_SCORE.md) for the output
contract.

## 2. Local Server And MCP Tools

For local expression backtests and MCP access:

```powershell
python -m pip install -e ".[server,local-backtest,llm]"
npm --prefix frontend ci
npm --prefix frontend run build
python -m worldquant_harness --transport http
```

The server starts at `http://localhost:8003`. Verify the fresh installation in
another terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8003/api/v1/health
```

For a containerized clean start:

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose ps
```

The default container configuration stores SQLite state in the named volume
`worldquant-harness-state`. Set `DATABASE_URL` in `.env` only when you want an
external PostgreSQL database.

For Claude Code or Claude Desktop, add an MCP server that runs the Python module
`worldquant_harness` in stdio mode:

```json
{
  "mcpServers": {
    "worldquant-harness": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "worldquant_harness"],
      "cwd": "/absolute/path/to/worldquant-harness"
    }
  }
}
```

Example agent request:

```text
为一个新的因子方向创建 sandbox，生成候选，运行 presubmit gate，并输出 ready/rejected artifacts。
```

## 3. Expression Mode

Expression-only mode does not require an LLM.

```powershell
curl -X POST http://localhost:8003/api/v1/auto_backtest `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer <token>" `
  -d '{"expression": "rank(close / ts_mean(close, 20))", "universe": "hs300"}'
```

Or enter a factor expression directly in the web UI at
`http://localhost:8003`.

## 4. Optional Credentials

DeepSeek is only needed for model-generated candidates and cross-review:

```text
DEEPSEEK_API_KEY=your-deepseek-api-key
```

WQ BRAIN credentials are only needed for real platform simulation/check/submit
commands. Sandbox, public demo, and `presubmit-sequential` are guarded paths; a
real submit requires an explicit submit command and selected IDs. See
[WQ_WORKFLOW.md](WQ_WORKFLOW.md) and
[SECURITY_AND_LIMITATIONS.md](SECURITY_AND_LIMITATIONS.md).

## 5. Recreate Or Resume On Another Computer

A Git clone recreates the source tree, tests, migration definitions, frontend
lockfile, and public demo. It deliberately does not contain credentials or live
research state. On the second computer, clone the required branch or commit and
repeat sections 1 and 2.

The following paths stay outside Git and must never be pushed to the public
repository: `.env`, `.secrets/`, `*.db`, `data/`, `logs/`, `reports/`, private
candidate batches, platform exports, and submit/check ledgers.

- For a clean environment, create a new `.env` from `.env.example`; the server
  creates a new SQLite database automatically.
- To resume the exact local state, stop both copies of the service and transfer
  the SQLite database and any required private artifacts through an encrypted
  channel. Recreate `.env` with the current `WORLDQUANT_HARNESS_*` variable
  names instead of committing or blindly copying legacy settings.
- When only legacy JSONL artifacts are available, run
  `python scripts/wq_agent_backfill.py --report reports/wq_agent_backfill_report.json`
  first. Review the dry-run report before adding `--apply`.

The Python project currently declares compatible version ranges in
`pyproject.toml`, not a fully pinned cross-platform lock. For the most
repeatable clean setup, use Python 3.12 and the Docker path above; record the Git
commit used for every research run.

## 6. More Examples

Local backtest examples:

```python
# 20-day momentum
rank(close / ts_mean(close, 20))

# Volume anomaly
rank(volume / ts_mean(volume, 10))

# Low-volatility tilt
rank(-1 * ts_std(close / ts_shift(close, 1) - 1, 20))

# Value factor, when fundamental data is available
rank(-1 * pe)
```

WQ-compatible expression examples, requiring credentials and explicit platform
commands for remote checks:

```python
# Example only; run through presubmit/check-only before any explicit submit.
rank(ts_decay_linear(rank(close / vwap), 10))
```
