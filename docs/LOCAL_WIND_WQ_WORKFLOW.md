# Local Wind + WQ BRAIN Workflow

This local workflow uses Wind Oracle data to pre-screen A-share expressions and
WorldQuant BRAIN to simulate and submit only expressions that use BRAIN-available
fields.

## 1. Environment

```powershell
conda create -n worldquant-harness-py311 python=3.11 -y
conda activate worldquant-harness-py311
pip install -e ".[dev]"
pip install cx_Oracle
```

Create `.env` from `.env.example` and set:

```text
AUTH_DISABLED=true
DATABASE_URL=
WORLDQUANT_HARNESS_DATA_SOURCE=wind,baostock
WORLDQUANT_HARNESS_USE_WIND=1
WORLDQUANT_HARNESS_WIND_CONFIG_PATH=<path-to-local-wind-mcp-config.py>
WQ_BRAIN_EMAIL=...
WQ_BRAIN_PASSWORD=<your-wq-brain-password>
```

The Wind adapter reads `CONN_PARAMS` and `_ORA_CLIENT_DIR` from the local MCP
config path by default. You can also use explicit `WORLDQUANT_HARNESS_WIND_DB_*`
variables.

## 2. Local pre-screen

Start the API server:

```powershell
python -m worldquant_harness --transport http --port 8003
```

Run a small local check first:

```powershell
python -m worldquant_harness --prefetch small_scale
```

Then use MCP or HTTP tasks with expressions such as:

```text
rank(ts_delta(close, 20) / ts_std(returns, 20))
rank(turnover_rate) + rank(cash_flow / market_cap)
rank(vwap / ts_mean(vwap, 20))
```

Wind-backed local columns include `open`, `high`, `low`, `close`, `volume`,
`amount`, `vwap`, `pct_change`, `market_cap`, `float_market_cap`, `turnover_rate`,
`shares`, `pe`, `pb`, `ps`, `net_income`, `cash_flow`, and `revenue`.

Run the bundled seed sweep:

```powershell
python scripts/run_local_seed_mining.py --universe small_scale
python scripts/run_local_seed_mining.py --universe hs300 --max-concurrent 4 --timeout 1800
```

Seed expressions live in `scripts/local_seed_expressions.json`. Results are
written to `reports/local_seed_mining_*.json`.

## 3. Discover WQ fields

Before submitting to BRAIN, discover the fields available to the current account:

```powershell
python scripts/wq_discover_fields.py --regions USA CHN --universes TOP3000 --limit 50
```

The output JSON under `reports/` is the source of truth for WQ-compatible field
names. Local Wind field names are only for pre-screening unless they match a
BRAIN field.

## 4. WQ simulation and submit

Use `wq_brain_simulate` or the WQ API routes after discovery. `auto_submit=true`
is gated by Sharpe, Fitness and Turnover thresholds from `SUBMIT_THRESHOLDS`.
If any check fails, the alpha is simulated but not submitted.
