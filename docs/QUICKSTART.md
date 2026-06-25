# Quick Start

This guide starts with the public harness demo because it is deterministic and
does not require WQ BRAIN, DeepSeek, Wind, or private credentials.

## 1. Public Harness Demo

```powershell
git clone https://github.com/gyx09212214-prog/worldquant-harness.git
cd worldquant-harness
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"

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
pip install -e .
python -m worldquant_harness --transport http
```

The server starts at `http://localhost:8003`.

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

## 5. More Examples

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
