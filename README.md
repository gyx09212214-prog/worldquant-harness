<div align="center">

# worldquant-harness

**A WorldQuant-oriented Agent Research Harness — sandbox, gate, evaluate, remember, evolve**

LLM Agent 生成候选 → Harness 记录、过滤、评分、沉淀 → 人工选择后显式提交

[![CI](https://github.com/gyx09212214-prog/worldquant-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/gyx09212214-prog/worldquant-harness/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React_18-TypeScript-61DAFB?logo=react&logoColor=white)](https://react.dev)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[Quick Start](docs/QUICKSTART.md) ·
[Visual Guide](docs/VISUAL_GUIDE.md) ·
[Public Harness Demo](docs/PUBLIC_HARNESS_DEMO.md) ·
[Harness Contract](docs/AGENT_HARNESS_CONTRACT.md) ·
[Agent Roles](docs/AGENT_ROLES.md) ·
[Architecture](docs/ARCHITECTURE.md) ·
[API Docs](docs/API_DOC.md) ·
[MCP Guide](docs/MCP_GUIDE.md) ·
[WQ Workflow](docs/WQ_WORKFLOW.md) ·
[Safety](docs/SECURITY_AND_LIMITATIONS.md) ·
[Disclaimer](DISCLAIMER.md) ·
[Security](SECURITY.md) ·
[Audit](docs/OPEN_SOURCE_AUDIT.md) ·
[Release Checklist](docs/OPEN_SOURCE_RELEASE_CHECKLIST.md) ·
[Contributing](CONTRIBUTING.md) ·
[Code of Conduct](CODE_OF_CONDUCT.md)

<img src="docs/images/worldquant-harness-overview.svg" width="920" alt="worldquant-harness agent research harness overview" />

Start with the [Visual Guide](docs/VISUAL_GUIDE.md) to inspect the public demo trace, memory feedback graph, factor map, quality review, and profile evolution timeline.

</div>

---

## Visual Overview

These visuals are public-safe. They use synthetic demo artifacts or sanitized boundary diagrams.

| Harness Loop | Artifact Lifecycle |
|:--:|:--:|
| ![worldquant-harness overview](docs/images/worldquant-harness-overview.svg) | ![artifact lifecycle](docs/images/harness-artifact-lifecycle.svg) |

| Public Demo Trace | Memory Feedback |
|:--:|:--:|
| ![public demo trace](docs/images/public-demo-trace.svg) | ![memory feedback graph](docs/images/memory-feedback-graph.svg) |

| Submit Boundary | Release Boundary |
|:--:|:--:|
| ![submit boundary](docs/images/submit-boundary.svg) | ![release safety boundary](docs/images/release-safety-boundary.svg) |

## What Is worldquant-harness

worldquant-harness is not an alpha generator. It is a research harness that turns an LLM agent's alpha ideas into auditable, replayable, gated research artifacts.

The LLM proposes. The harness records, validates, rejects, scores, remembers, and evolves. Real WQ BRAIN submission remains a separate, explicit command path for selected alpha IDs after human review.

worldquant-harness 不是单个因子生成器。它是 Agent 研究约束层。

Agent 提出想法。Harness 记录、校验、拒绝、评分、沉淀、进化。真实提交只走显式命令。

This project is not affiliated with or endorsed by WorldQuant or WorldQuant BRAIN. See [Disclaimer](DISCLAIMER.md) and [Security Policy](SECURITY.md) before connecting credentials or publishing artifacts.

本项目不是 WorldQuant 官方项目。接入凭证或发布产物前，请先看 [Disclaimer](DISCLAIMER.md) 与 [Security Policy](SECURITY.md)。

The Python module and CLI entrypoint are `worldquant_harness`:

```bash
python -m worldquant_harness --transport http
```

The fastest way to understand the project is the public visual pack:

| Visual | What it explains |
|:--|:--|
| [Overview](docs/images/worldquant-harness-overview.svg) | Human goal → agent → harness → memory → profile → explicit review |
| [Artifact lifecycle](docs/images/harness-artifact-lifecycle.svg) | Candidate specs → simulations → review queue → ready/rejected → memory/profile |
| [Trace funnel](docs/images/public-demo-trace.svg) | `candidate_uid` lifecycle from generated candidate to ready/rejected |
| [Memory graph](docs/images/memory-feedback-graph.svg) | How self-correlation and other blockers become reusable memory |
| [Quality dashboard](docs/images/quality-review-dashboard.svg) | How generated and submitted alpha quality are reviewed |
| [Submit boundary](docs/images/submit-boundary.svg) | Which commands are no-submit, check-only, and real submit |
| [Release boundary](docs/images/release-safety-boundary.svg) | What is publishable, private, and review-required |

## Why Harness, Not Just Alpha Generation

Most AI quant tools stop at "prompt → expression → backtest". That is not enough for an autonomous research agent. The hard part is not producing one expression; it is keeping thousands of candidate decisions traceable, constrained, comparable, and reusable.

多数 AI 回测工具停在“提示词 → 表达式 → 回测”。这不够。

难点不是生成一个表达式。难点是让上千个候选有来源、有状态、有拒绝原因、有复盘入口。

| Common AI backtest tool | worldquant-harness |
|:--|:--|
| Prompt produces one factor | Agent produces a candidate batch |
| Result is ad hoc | Every candidate has lifecycle artifacts |
| Failures are forgotten | Failures become memory and constraints |
| Submission boundary is unclear | Sandbox and presubmit never submit |
| Iteration depends on chat history | Profile evolution is versioned |
| Hard to audit | JSONL/YAML/Markdown artifacts are replayable |

## Agent Contract

The agent can explore, but it works inside a contract. The harness owns the state.

Agent 可以探索。Harness 管状态。

The contract is now executable. `harness_contracts.py` defines `HarnessRun`, `HarnessStep`, `HarnessEvent`, `ArtifactRef`, `DecisionGate`, `MemoryDelta`, and `ProfilePatch`. `harness_runner.py` writes these files from the public no-submit eval path.

契约可执行。`harness_contracts.py` 定义 run、step、event、artifact、decision、memory delta、profile patch。`harness_runner.py` 从公开 no-submit eval 写出这些产物。

| Agent action | Harness control |
|:--|:--|
| Generate candidates | Stable `candidate_uid`, source tags, field/operator extraction |
| Run experiments | Sandbox artifacts, no-submit default |
| Interpret results | Structured review queue and rejection reasons |
| Learn from failures | Memory records, blocked signatures, field-family stats |
| Plan next iteration | Profile evolution and explicit child experiment |
| Submit | Human-selected alpha IDs only |

See [Agent Harness Contract](docs/AGENT_HARNESS_CONTRACT.md) and [Agent Roles](docs/AGENT_ROLES.md) for the JSON/JSONL files and role boundaries.

## Reproducible Public Demo

The public demo is the open-source contract. It uses synthetic fixtures and fake platform adapters, so it requires no WQ BRAIN, DeepSeek, Wind, or private credentials.

公开 demo 是项目的复现实例。它使用合成数据和 fake adapter。它不需要平台账号。

```text
candidate_specs
  -> presubmit simulation
  -> review_queue
  -> presubmit_ready / presubmit_rejected
  -> alpha_lifecycle_events
  -> harness score
  -> profile candidate
  -> child experiment
```

Key artifacts:

| Artifact | Purpose |
|:--|:--|
| `candidate_specs.jsonl` | Candidate source and design intent |
| `simulation_results.jsonl` | Simulated outcomes from guarded adapters |
| `review_queue.jsonl` | Candidates ready for gate review |
| `presubmit_ready_sequential.jsonl` | Accepted candidates |
| `presubmit_rejected.jsonl` | Rejection reasons and blocker memory |
| `alpha_lifecycle_events.jsonl` | Append-only candidate lifecycle |
| `eval_summary.json` | Harness score and gate decision |
| `evolution_result.json` | Next-generation profile candidate |

Run it:

```bash
git clone https://github.com/gyx09212214-prog/worldquant-harness.git
cd worldquant-harness
pip install -e ".[dev]"
python scripts/run_public_harness_demo.py --output-root reports/public_harness_demo
python scripts/validate_public_harness_artifacts.py reports/public_harness_demo
python scripts/run_public_harness_eval.py --output-root reports/public_harness_eval
```

---

## Agent Harness Loop

> **This is worldquant-harness' defining capability.**
>
> The agent searches. The harness records source, rejects duplicates and illegal inputs, runs the presubmit gate, computes the harness score, and writes the next profile.
>
> Agent 搜索。Harness 记录来源。Harness 拒绝重复和非法输入。Harness 评分。Harness 写入下一轮 profile。

### Research Cycle

```
                    ┌─────────────────────────────┐
                    │  Research Notes & Knowledge  │
                    │  (Rules / Findings / Fails)  │
                    └──────────┬──────────────────┘
                               │ read
                               ▼
┌──────────┐    ┌──────────────────────────┐    ┌──────────────────┐
│  Phase 0 │───▶│  Phase 1: Factor Design  │───▶│  Phase 2: Batch  │
│  Context │    │  Hypothesis → Expression │    │  Backtest (10-20 │
│  Loading │    │  1-3 candidates per idea │    │  concurrent)     │
└──────────┘    └──────────────────────────┘    └────────┬─────────┘
                                                         │
                               ┌─────────────────────────┘
                               ▼
                ┌──────────────────────────────────────────┐
                │  Phase 3: Four-Step Analysis             │
                │                                          │
                │  ① Fact Collection (metrics vs baseline) │
                │  ② Independent Judgment (Agent)          │
                │  ③ Cross-Review (DeepSeek Reasoner)      │
                │  ④ Consensus or Divergence Resolution    │
                └──────────────────┬───────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
          ┌─────────────────┐          ┌──────────────────┐
          │  Phase 4: Update │          │  Phase 5: Stop?  │
          │  Notes + Knowledge│         │  Converged /     │
          │  Base             │◀────────│  Time / Rounds   │
          └─────────────────┘          └──────────────────┘
                    │                         │ no
                    │                         └──▶ back to Phase 1
                    ▼
          ┌──────────────────┐
          │  Phase 6: Report │
          │  Ready/rejected  │
          │  artifacts + KB  │
          └──────────────────┘
```

### Key Mechanisms

<table>
<tr>
<td width="50%">

**Dual-LLM Cross-Review**

每个结论性判断（采用/不采用/关闭方向）必须经过第二个 LLM 独立评审。把事实数据和第一个模型的推理链一起发给 DeepSeek Reasoner，要求独立评估推理是否合理、是否有遗漏角度。

共识 → 直接输出。分歧 → 呈现双方证据，采用更保守结论。

这解决了单模型因子研究的核心问题：**confirmation bias**。

</td>
<td width="50%">

**Persistent Knowledge Base**

```
research_notes/knowledge/
├── rules/       ← 已验证的稳定规则（必须遵守）
├── findings/    ← 经验发现（参考）
└── failures/    ← 已证伪路径（禁止重复）
```

知识库跨会话积累。第 10 次研究会话可以直接利用前 9 次的所有发现，避免重复实验，遵守已验证规则，绕开已证伪路径。

这不是 chat history——是**结构化的研究资产**。

</td>
</tr>
<tr>
<td>

**Batch Concurrent Evaluation**

单次提交 10-20 个因子表达式，并发回测 + 三波重试。结果按 fitness 降序排列。hs300 fitness < 0.1 时自动跳过 csi500 验证，节省算力。

```python
from scripts.factor_miner import batch_evaluate
results = batch_evaluate(
    server, expressions, params,
    max_concurrent=10
)
```

</td>
<td>

**Research Discipline (Enforced)**

不是建议，是硬性规则：
- 每次实验只改一个变量
- 提交前检查是否已做过（笔记 + 知识库）
- 分析结论标注"仅为假设"
- 失败实验同样记录原因
- 表达式 > 4 层嵌套需额外论证
- 简单清晰 > 复杂精巧

</td>
</tr>
</table>

> 当前公开复现入口是 [Public Harness Demo](docs/PUBLIC_HARNESS_DEMO.md)。标准 agent contract 回归入口是 [Agent Harness Contract](docs/AGENT_HARNESS_CONTRACT.md) 中的 `run_public_harness_eval.py`。完整 WQ 运行边界见 [WorldQuant Workflow](docs/WQ_WORKFLOW.md)。

---

## Historical Validation Examples

The public demo is the reproducible contract. The examples below are historical validation samples from real platform runs. They show that the harness output can reach submit-quality candidates, but they are not required to run the open-source demo.

公开 demo 是复现入口。下面是历史验证样本。它们说明 harness 能产出可提交候选。运行开源 demo 不需要这些截图。

| Factor | Expression | WQ Sharpe | WQ Fitness | WQ Returns | IS Tests | Status |
|:-------|:-----------|:---------:|:----------:|:----------:|:--------:|:------:|
| **Debt-Momentum Composite** | `-1 * rank(ts_av_diff(close, 10)) + rank(debt / enterprise_value)` | **1.77** | **1.26** | **20.18%** | **ALL PASS** | **Submitted** |
| **VWAP Decay Reversal** | `-1 * rank(ts_decay_linear(close / vwap, 10))` | **1.69** | **1.07** | **18.63%** | **ALL PASS** | **Submitted** |
| **Returns-Volume Momentum** | `-1 * rank(ts_decay_linear(returns * volume / adv20, 5))` | **1.60** | **1.03** | **24.15%** | **ALL PASS** | **Submitted** |

<p align="center">
  <img src="example_factor/1-1.png" width="49%" alt="WQ BRAIN PnL - Debt-Momentum Composite" />
  <img src="example_factor/1-2.png" width="49%" alt="WQ BRAIN IS Summary - Debt-Momentum Composite" />
</p>
<p align="center">
  <sub>Debt-Momentum Composite - Sharpe 1.77, Fitness 1.26, Returns 20.18%, IS all pass</sub>
</p>

<p align="center">
  <img src="example_factor/2-1.png" width="49%" alt="WQ BRAIN PnL - VWAP Decay Reversal" />
  <img src="example_factor/2-2.png" width="49%" alt="WQ BRAIN IS Summary - VWAP Decay Reversal" />
</p>
<p align="center">
  <sub>VWAP Decay Reversal - Sharpe 1.69, Fitness 1.07, Returns 18.63%, IS all pass</sub>
</p>

<p align="center">
  <img src="example_factor/3-1.png" width="49%" alt="WQ BRAIN PnL - Returns-Volume Momentum" />
  <img src="example_factor/3-2.png" width="49%" alt="WQ BRAIN IS Summary - Returns-Volume Momentum" />
</p>
<p align="center">
  <sub>Returns-Volume Momentum - Sharpe 1.60, Fitness 1.03, Returns 24.15%, IS all pass</sub>
</p>

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                  worldquant-harness Research Engine                │
├─────────────┬──────────────────────────────┬───────────────────────┤
│             │         Core Engine          │                       │
│  Agent      │  ┌──────────────────────┐   │   Data Layer          │
│  Interface  │  │  Expression Parser   │   │  ┌─────────────────┐  │
│             │  │  50+ operators       │   │  │ baostock (free)  │  │
│ MCP Tools   │  │  WQ BRAIN compatible │   │  │ akshare (free)   │  │
│ REST API    │  └──────────┬───────────┘   │  │ PolarDB (opt)    │  │
│ Web UI      │  ┌──────────▼───────────┐   │  │ Parquet cache    │  │
│ (monitor)   │  │  Backtest Engine     │   │  └─────────────────┘  │
│             │  │  Rank-based grouping │   │                       │
│             │  │  WQ BRAIN aligned    │   │   AI Layer            │
│             │  └──────────┬───────────┘   │  ┌─────────────────┐  │
│             │  ┌──────────▼───────────┐   │  │ DeepSeek LLM    │  │
│             │  │  Validation Suite    │   │  │ Factor design   │  │
│             │  │  Anti-overfit (4x)   │   │  │ Cross-review    │  │
│             │  │  Walk-forward        │   │  │ Mutation engine │  │
│             │  │  WQ BRAIN simulation │   │  └─────────────────┘  │
│             │  └──────────────────────┘   │                       │
│             │                             │   Storage             │
│             │  Evolution Engine           │  ┌─────────────────┐  │
│             │  Trajectory → Meta-Evo →    │  │ SQLite (default)│  │
│             │  Mutation / Crossover       │  │ PostgreSQL (opt)│  │
│             │                             │  └─────────────────┘  │
├─────────────┴──────────────────────────────┴───────────────────────┤
│  Agent Orchestrator: Claude Code skill loop / Claude Desktop MCP   │
└────────────────────────────────────────────────────────────────────┘
```

### Tech Stack

| Layer | Technology |
|:------|:-----------|
| Agent | Claude Code (skill loop) / Claude Desktop (MCP) |
| Backend | Python 3.10+, FastAPI, uvicorn, SQLAlchemy 2.0 async |
| Database | SQLite (default, zero-config) / PostgreSQL (optional) |
| AI/LLM | DeepSeek (factor generation + cross-review) |
| Market Data | baostock + akshare (free) → Parquet cache |
| Frontend | React 18 + TypeScript + Tailwind CSS 4 (monitoring dashboard) |
| MCP | FastMCP (stdio / SSE / streamable-http) |
| Report | QuantStats HTML |

---

## Key Engineering Decisions

### 1. Expression Parser — The Core Differentiator

自研的表达式解析器（`expression_parser.py`, 870+ lines）是整个系统的核心：

- **50+ 算子**：截面（`rank`, `zscore`）、时序（`ts_corr`, `decay_linear`）、非线性（`sign_power`）、条件（`where`, `trade_when`）、技术指标（`rsi`, `macd`, `atr`）
- **双模式**：`mode="wq"` 仅允许 WQ BRAIN 兼容算子（提交前校验），`mode="local"` 开放全部算子
- **语义正确的截面/时序分离**：`rank()` 按 `trade_date` 分组（截面），`ts_mean()` 按 `stock_code` 分组（时序），自动处理分组逻辑
- **安全约束**：递归深度限制、窗口上限、表达式长度限制，防止恶意输入

### 2. Three-Layer Anti-Overfit System

| Layer | Module | Method |
|:------|:-------|:-------|
| Statistical Tests | `anti_overfit.py` | IC 稳定性 + 子样本压力测试（牛/熊/震荡）+ 安慰剂检验 + 半衰期估计 |
| Walk-Forward | `rolling_validator.py` | 滚动 train/valid/test 窗口，评估样本外 IC 衰减 |
| WQ Simulation | `wq_simulate.py` | Dollar-neutral 多空模拟，对齐 BRAIN 的 Sharpe/Turnover/Fitness 计算 |
| **WQ BRAIN API** | `wq_brain_client.py` | **直连 BRAIN 平台 — 真实模拟 + check-only 检测 + 显式提交** |

### 3. Evolutionary Factor Iteration

受 QuantaAlpha 启发的三阶段自动搜索：

```
TrajectoryAnalyzer → MetaEvolutionSelector → Strategy Execution
 (质量指标评估)       (EXPLOIT/EXPLORE/        (MutationEngine ×8 方向
                      RECOMBINE/SIMPLIFY)       / CrossoverEngine)
```

8 种定向突变：时间窗口变异、算子替换、复杂度调整、截面变换叠加等。5 维评分驱动迭代方向。

### 4. Agent-First Access Model

| Mode | Role | Use Case |
|:-----|:-----|:---------|
| **MCP (primary)** | Agent toolkit | Claude Code / Claude Desktop 通过 MCP 调用所有研究工具，驱动自治研究循环 |
| **REST API** | Programmatic access | 批量回测、外部系统集成、CI/CD 因子验证 |
| **Web UI** | Monitoring dashboard | 任务监控、报告查看、因子库管理 |

<details>
<summary><b>MCP Tools (21 个)</b></summary>

| Tool | Description |
|:-----|:------------|
| `list_operators` | 全部算子文档 |
| `list_universes` | 股票池和基准 |
| `validate_expression` | 语法校验 |
| `wq_harness_new` | 创建 no-submit harness run |
| `wq_harness_run_presubmit` | 运行 no-submit public demo/eval wrapper |
| `wq_harness_evaluate` | 评估 sandbox experiment |
| `wq_harness_evolve` | 生成下一轮 profile/experiment candidate |
| `wq_harness_history_ingest` | 沉淀本地历史经验，默认不连平台 |
| `wq_harness_memory_maintain` | 生成 memory maintenance 和 memory delta |
| `wq_harness_status` | 读取 persisted harness 状态 |
| `run_backtest` | 完整回测 |
| `score_factor` | 评分（0–100, A/B/C/D） |
| `diagnose_factor` | 失败模式诊断 + 改进建议 |
| `run_anti_overfit` | 4 项反过拟合检验 |
| `run_rolling_validation` | Walk-forward 验证 |
| `wq_brain_submit` | 显式单因子模拟/提交路径 |
| `wq_brain_batch_submit` | 显式批量参数扫描路径 |
| `wq_brain_submit_by_ids` | 按选定 alpha ID 显式提交 |
| `wq_brain_list_alphas` | 查询平台 alpha |
| `wq_brain_check_alphas` | check-only 状态检查 |
| `wq_brain_finalize_submissions` | 最终提交确认 |

</details>

---

## Competitive Landscape

| Capability | JoinQuant | Backtrader | ChatGPT + Backtest | **worldquant-harness** |
|:-----------|:------:|:------:|:------:|:------:|
| Research mode | Human writes code | Human writes code | Human prompts, tool executes | **Agent autonomously researches** |
| Factor discovery | Manual | Manual | One-shot LLM | **Multi-round evolution + knowledge base** |
| Anti-bias | Researcher judgment | None | None | **Dual-LLM mandatory cross-review** |
| Knowledge accumulation | Personal notes | None | Lost between sessions | **Structured KB across sessions** |
| WQ BRAIN integration | -- | -- | -- | **Operator-aligned + guarded explicit submission** |
| Anti-overfit | -- | -- | -- | **4 statistical tests + walk-forward** |
| MCP / AI Agent | -- | -- | -- | **21 tools, contract-driven harness orchestration** |
| Live trading | Yes | Limited | -- | -- |
| Intraday data | Yes | Yes | -- | Daily only |

---

## Quick Start

### Option 1: Public Harness Demo (No Credentials)

Run the deterministic harness demo first. It does not require WQ BRAIN, DeepSeek, Wind, or private data.

```bash
git clone https://github.com/gyx09212214-prog/worldquant-harness.git && cd worldquant-harness
pip install -e ".[dev]"
python scripts/run_public_harness_demo.py --output-root reports/public_harness_demo
python scripts/validate_public_harness_artifacts.py reports/public_harness_demo
python scripts/run_public_harness_eval.py --output-root reports/public_harness_eval
python scripts/wq_submit_efficiency_report.py \
  --run-roots reports/public_harness_demo \
  --current-name public-demo \
  --output reports/public_harness_demo/efficiency_summary.json \
  --markdown-output reports/public_harness_demo/efficiency_summary.md \
  --events-output reports/public_harness_demo/efficiency_events.jsonl
python scripts/wq_alpha_quality_review.py \
  --reports reports/public_harness_demo \
  --no-platform \
  --no-profile-candidate \
  --output-dir reports/public_harness_demo/quality_review
python scripts/build_public_visual_pack.py \
  --source reports/public_harness_demo \
  --output-dir docs/images \
  --report docs/VISUAL_GUIDE.md
```

The demo creates a sandbox experiment, runs `presubmit-sequential` with fake platform adapters, applies the gate, computes a harness score, and creates the next-generation child experiment. The eval command wraps the same path and writes `harness_run.json`, `agent_trace.jsonl`, `eval_cases.jsonl`, `memory_delta.jsonl`, and `profile_patch.json`. It never calls a real submit endpoint.

### Option 2: Agent Mode

```bash
git clone https://github.com/gyx09212214-prog/worldquant-harness.git && cd worldquant-harness
make setup   # creates venv, installs deps, generates .env
make run     # starts server at http://localhost:8003
```

Add MCP configuration to Claude Code or Claude Desktop:

```json
{
  "mcpServers": {
    "worldquant-harness": {
      "command": "python",
      "args": ["-m", "worldquant_harness"]
    }
  }
}
```

Then let the Agent work: *"为一个新的因子方向创建 sandbox，生成候选，运行 presubmit gate，并输出 ready/rejected artifacts"*

### Option 3: Expression Mode (No LLM Required)

```bash
# Direct expression backtest via API
curl -X POST http://localhost:8003/api/v1/auto_backtest \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <token>" \
  -d '{"expression": "rank(close / ts_mean(close, 20))", "universe": "hs300"}'
```

### Windows Quick Start

Windows 用户可手动执行：

```powershell
# 1. Clone
git clone https://github.com/gyx09212214-prog/worldquant-harness.git
cd worldquant-harness

# 2. Environment
python -m venv .venv
.venv\Scripts\activate
pip install -e .
# Optional PostgreSQL
# pip install -e ".[postgresql]"

# 3. Frontend
cd frontend && npm install && npm run build && cd ..

# 4. Server
python -m worldquant_harness --transport http
# Open http://localhost:8003
```

> **注意：**
> - 推荐 Python 3.11 或 3.12
> - 端口占用：`netstat -ano | findstr :8003`，再 `taskkill /PID <pid> /F`
> - WSL2 可用

**Zero config by default**: the public harness demo uses synthetic fixtures; local expression backtests use SQLite plus baostock/akshare free data. See [full Quick Start guide](docs/QUICKSTART.md) for details.

<details>
<summary><b>Optional: DeepSeek API (for factor generation & cross-review)</b></summary>

```bash
# Edit .env, add your DeepSeek API key (~$0.001 per query)
DEEPSEEK_API_KEY=your-deepseek-api-key
```

</details>

<details>
<summary><b>Optional: PostgreSQL (for production)</b></summary>

```bash
pip install -e ".[postgresql]"
# Edit .env:
DATABASE_URL=postgresql+asyncpg://worldquant_harness:password@localhost:5432/worldquant_harness
alembic upgrade head
```

</details>

<details>
<summary><b>Expression Examples</b></summary>

**Local backtest** — works out of the box with baostock/akshare data:

```python
# 20-day momentum
rank(close / ts_mean(close, 20))

# Low volatility
rank(-1 * ts_std(close/ts_shift(close,1)-1, 20))

# Decay-weighted correlation
decay_linear(rank(ts_corr(vwap, volume, 10)), 5)

# Momentum + profitability composite
-1 * rank(ts_av_diff(close, 10)) + rank(roe)
```

**WQ BRAIN remote** — requires WQ BRAIN credentials and explicit submit/check commands. Keep public examples generic unless a platform output has been sanitized and approved for release:

```python
# Example only; run through presubmit/check-only before any explicit submit.
rank(ts_decay_linear(rank(close / vwap), 10))
```

</details>

---

## Project Structure

```
worldquant-harness/
├── worldquant_harness/                    # Backend (Python)
│   ├── expression_parser.py     # Factor expression parser (50+ ops, WQ compatible)
│   ├── backtest.py              # Rank-based group backtest engine
│   ├── market_data.py           # baostock/akshare → Parquet cache
│   ├── api_server.py            # FastAPI REST API + SSE
│   ├── mcp_server.py            # FastMCP server (21 tools, including harness contract tools)
│   ├── harness_contracts.py     # Stable agent/harness JSON and JSONL contracts
│   ├── harness_runner.py        # No-submit public eval, history, memory, status wrappers
│   ├── iteration.py             # 3-phase evolutionary iteration
│   ├── mutation_engine.py       # 8 directed mutation strategies
│   ├── crossover_engine.py      # High-score factor crossover
│   ├── meta_evolution.py        # Adaptive strategy selector
│   ├── trajectory_analyzer.py   # Trajectory quality metrics
│   ├── anti_overfit.py          # 4 statistical anti-overfit tests
│   ├── rolling_validator.py     # Walk-forward validation
│   ├── wq_simulate.py           # WQ BRAIN dollar-neutral simulator
│   ├── wq_brain_client.py       # WQ BRAIN API integration
│   ├── neutralize.py            # Industry & cap neutralization
│   ├── daily_summary.py         # LLM-powered daily market report
│   └── routes/                  # API route modules
├── frontend/                    # React 18 + TypeScript + Tailwind CSS 4
│   └── src/components/          # Monitoring dashboard
├── scripts/
│   └── factor_miner.py          # Batch factor evaluation toolkit
├── tests/                       # parser, backtest, WQ workflow, harness, and API tests
├── example_factor/              # Optional validation examples; review before public release
└── docs/                        # Architecture, API, MCP, Mining guides
```

---

## Limitations

- **Daily frequency only** — no intraday backtesting
- **A-share market only** — China mainland equities
- **Agent quality depends on LLM** — better models produce better factors
- **Public harness demo uses synthetic fixtures** — it validates workflow mechanics, not investment performance
- **Real platform submission is guarded** — sandbox and presubmit flows do not submit; live submission requires explicit commands and credentials

## Responsible Use

- Use your own credentials for external services and follow their terms, policies, rate limits, and data restrictions.
- Treat `.env`, `.secrets/`, local databases, raw platform exports, submit/check ledgers, and full research reports as private.
- Do not present generated factors, screenshots, or backtests as guaranteed returns.
- Review [Open Source Audit](docs/OPEN_SOURCE_AUDIT.md) and [Release Checklist](docs/OPEN_SOURCE_RELEASE_CHECKLIST.md) before publishing a fork or release.

---

## License

[MIT](LICENSE). Copyright and attribution details are recorded in [NOTICE](NOTICE).

This public release is maintained as `worldquant-harness`. Derivative works should
retain the copyright notice and comply with the MIT License terms.
See [NOTICE](NOTICE), [DISCLAIMER](DISCLAIMER.md), [SECURITY](SECURITY.md), and [CODE_OF_CONDUCT](CODE_OF_CONDUCT.md) for details.

<sub>*Past factor performance does not guarantee future returns. This project does not constitute investment advice.*</sub>
