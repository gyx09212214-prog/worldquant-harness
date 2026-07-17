<div align="center">

# worldquant-harness

**面向 WorldQuant 风格 alpha 研究智能体的约束框架。**

智能体生成候选 -> 框架记录、门控、评估、记忆、演化 -> 人工显式选择可提交对象。

[![CI](https://github.com/gyx09212214-prog/worldquant-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/gyx09212214-prog/worldquant-harness/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React_18-TypeScript-61DAFB?logo=react&logoColor=white)](https://react.dev)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[英文版](README.md) ·
[快速开始](docs/QUICKSTART.md) ·
[可视化指南](docs/VISUAL_GUIDE.md) ·
[公开演示](docs/PUBLIC_HARNESS_DEMO.md) ·
[研究契约](docs/AGENT_HARNESS_CONTRACT.md) ·
[智能体角色](docs/AGENT_ROLES.md) ·
[架构](docs/ARCHITECTURE.md) ·
[接口](docs/API_DOC.md) ·
[MCP 指南](docs/MCP_GUIDE.md) ·
[WQ 工作流](docs/WQ_WORKFLOW.md) ·
[安全说明](docs/SECURITY_AND_LIMITATIONS.md)

<img src="docs/images/worldquant-harness-overview.svg" width="920" alt="worldquant-harness 概览" />

</div>

---

## 项目定位

worldquant-harness 不是一次性 alpha 生成器。它是智能体因子研究的执行层和记忆层。

智能体可以提出想法、运行批量实验、查看结果。框架负责生命周期：候选标识、沙盒执行、无提交门控、复盘队列、拒绝原因、历史记忆、画像演化、真实 WQ BRAIN 操作前的显式边界。

本项目不隶属于 WorldQuant 或 WorldQuant BRAIN，也未获其背书。接入凭证或发布产物前，请先阅读 [免责声明](DISCLAIMER.md)、[安全政策](SECURITY.md)、[使用边界](docs/SECURITY_AND_LIMITATIONS.md)。

## 为什么需要约束框架

很多 AI 量化流程停在想法 -> 表达式 -> 回测。真正困难的部分常被留在系统外：可追踪性、失败记忆、重复控制、平台边界、可复现复盘。

worldquant-harness 把因子挖掘视为受控研究循环：

| 问题 | 处理方式 |
|:--|:--|
| 批量候选难以审计 | 每个候选有稳定 ID 和生命周期产物 |
| 失败想法反复出现 | 失败进入结构化记忆和下一轮约束 |
| 提交边界不清晰 | 公开演示、沙盒、预提交、只检查、真实提交分离 |
| 智能体上下文易丢失 | 笔记、事件、复盘队列、画像补丁持久化 |
| 开源发布可能泄露私有研究 | 演示产物和视觉材料使用合成或脱敏内容 |

## Skill 体系和效果对比

这次主要更新不是再加一个 prompt 模板，而是把 WQ 论坛经验、本地失败记录、repair queue 和手写候选说明整理成可执行的 skill 体系。智能体在生成或提交前，先要经过这些 skill 的分类、阻塞和修复路线约束。

<p align="center">
  <img src="docs/images/skill-effect-comparison.svg" width="920" alt="Skill 体系效果对比" />
</p>

| 旧流程问题 | 新 skill 体系 |
|:--|:--|
| ready 和 near-pass 记录容易直接变成下一轮候选池 | 可以显式忽略旧 ready；已有 ACTIVE 只作为相关性和拥挤度边界 |
| 失败原因维度较粗，常停在 near pass、self-corr、platform fail | 拆成 clone blocker、family shift、concentration repair、metric overlay、pending-check gate、duplicate block 等动作桶 |
| 某个 family 变成 ACTIVE 后，近似变体仍可能继续消耗提交额度 | 对 0.97+ clone family 直接阻塞，要求换 field family、operator skeleton 或 source family |
| platform PASS 和严格 numeric related-record cutoff 容易混在一起 | 单独记录 `platform_result` 和 numeric related-record value |
| 每次微调难以横向比较 | 记录每次改了什么、结果如何、具体 blocker、下一步 repair 动作 |

本地记录里最适合公开表达的对比是：

| 流程 | 候选来源 | 脱敏结果 | 经验 |
|:--|:--|:--|:--|
| 代表性的 near-miss 阻塞提交复盘 | 旧 near-pass / ready 风格 family | 7 次提交尝试，0 ACTIVE | 指标接近，但 self-correlation、sub-universe margin、weight distribution 仍然阻塞 |
| fresh skill-routed forum/experience run | 论坛 recipe memory + failure taxonomy + 手写 JSONL，忽略旧 ready | 5/5 ACTIVE，其中 3 个 strict submit，2 个 platform-PASS relaxed submit | skill 路由帮助放弃 clone family，并切换到 EPS/dividend、披露缺失、value-quality 等结构差异更大的方向 |

这只是流程有效性信号，不是投资收益承诺。README 不公开 alpha 表达式、原始平台导出或未脱敏论坛内容。

<p align="center">
  <img src="docs/images/submit5-case-study.svg" width="920" alt="脱敏 submit5 active 案例" />
</p>

<p align="center">
  <img src="docs/images/forum-to-skill-memory.svg" width="920" alt="论坛经验蒸馏为 skill memory" />
</p>

候选记录也被明确约束：一个可审计候选最好带有 `expression`、`simulation_settings`、`source_family`、`field_signature`、`tag`、`rationale`，以及 run、forum 或 repair 来源依据。

## 架构

<p align="center">
  <img src="docs/images/worldquant-harness-architecture.zh-CN.svg" width="960" alt="worldquant-harness 系统架构" />
</p>

| 层级 | 职责 |
|:--|:--|
| 智能体入口 | 将研究目标转成候选批次，可通过 MCP 工具、CLI 脚本、REST 接口进入 |
| 框架控制面 | 分配稳定候选标识，运行沙盒评估，执行预提交门控，生成复盘队列 |
| 记忆与演化 | 将生命周期事件、拒绝原因、参考上下文、框架评分转成下一轮约束 |
| 提交边界 | 公开演示和沙盒默认不提交；真实 WQ BRAIN 提交需要凭证和显式命令 |

默认公开路径不会提交。真实 WQ BRAIN 操作需要显式凭证和显式提交命令。

WQ Agent Core v2 现将候选、模拟、检查、提交和 ACTIVE 状态记录为幂等数据库事件。数据库事件是事实源，JSONL 保留为可读兼容导出；中断提交会先与平台状态对账，记忆按 owner/account/region/universe/delay 隔离，autopilot 分支预算与剪枝状态会持久化。详见 [WQ Agent Core v2](docs/WQ_AGENT_CORE_V2.md)。

## 公开演示

公开演示是可复现契约。它使用合成测试夹具和受保护适配器，不需要 WQ BRAIN、DeepSeek、Wind 或私有行情数据。

```bash
git clone https://github.com/gyx09212214-prog/worldquant-harness.git
cd worldquant-harness
pip install -e ".[dev]"
python scripts/run_public_harness_demo.py --output-root reports/public_harness_demo
python scripts/validate_public_harness_artifacts.py reports/public_harness_demo
python scripts/run_public_harness_eval.py --output-root reports/public_harness_eval
```

演示会写出完整的无提交研究包：

| 产物 | 用途 |
|:--|:--|
| `candidate_specs.jsonl` | 候选来源、标签、设计意图 |
| `simulation_results.jsonl` | 受保护适配器的结果 |
| `review_queue.jsonl` | 等待门控复盘的候选 |
| `presubmit_ready_sequential.jsonl` | 通过的候选 |
| `presubmit_rejected.jsonl` | 拒绝原因和阻塞记忆 |
| `alpha_lifecycle_events.jsonl` | 追加式生命周期轨迹 |
| `agent_events.jsonl` | 可恢复 live run 的 v2 规范事件导出 |
| `eval_summary.json` | 框架评分和门控决策 |
| `evolution_result.json` | 下一轮画像候选 |

## Skill 分类和迭代记录

2026-07 之后的核心变化是把松散的论坛评论和提交失败拆成可复用 skill。每个失败都应该回答两个问题：为什么失败，下一步允许怎么修。

<p align="center">
  <img src="docs/images/failure-taxonomy-map.svg" width="920" alt="细粒度失败分类和修复路线" />
</p>

| Skill 层 | 控制内容 |
|:--|:--|
| `community::*` 兼容入口 | 保留旧的 near-pass、template、operator、submit-gate 路线，同时把它们转到更细的 skill |
| `community_failure::*` 失败动作 | 拆分 metric overlay repair、correlation family shift、direct-template clone blocker、concentration/coverage repair、turnover/density repair、pending-check gate、duplicate block、platform/unit probe |
| `near_sc_cutoff_settings_repair` | 只在强 parent 接近 self-correlation cutoff 时做 settings grid；如果相似性是结构性的就停止调参 |
| `top5_high_score_low_corr_submit` | 提交/检查目标按 WQ score、eligibility、correlation risk 排序，而不是只看 Sharpe |
| 候选 provenance 字段 | 要求 `source_family`、`field_signature`、`tag`、`rationale` 和来源证据，方便下一轮审计 |

<p align="center">
  <img src="docs/images/fresh-submit-loop.svg" width="920" alt="带 ACTIVE 相关性边界的 fresh submit loop" />
</p>

新的 iteration audit 默认写出 `iteration_audit.jsonl`、`iteration_audit_summary.json` 和 `iteration_audit.md`。默认报告不暴露完整表达式，而是使用表达式 hash、字段签名、算子、指标、失败类型和下一步动作，方便公开说明方法论，也方便本地继续排查提交体系是否有效。

代码结构也围绕这个流程做了收敛：artifact I/O 和 record utilities 复用到 WQ workflow，repair template 按失败类型拆分，候选/repair 去重的 first-wins key 语义进入通用 helper。详见 [WQ Agent Core v2](docs/WQ_AGENT_CORE_V2.md)、[Alpha-GPT Harness](docs/ALPHA_GPT_HARNESS.md)、[Alpha Search Memory](docs/WQ_ALPHA_SEARCH_MEMORY.md)、[WQ Workflow](docs/WQ_WORKFLOW.md) 和 [冗余模块审计](docs/WQ_REDUNDANCY_MODULE_AUDIT.md)。

## 可视化材料

可视化材料由 public demo 生成图和脱敏的 skill/effect 静态图组成。它用于解释约束框架，不披露私有研究。

| 视图 | 内容 |
|:--|:--|
| [概览](docs/images/worldquant-harness-overview.svg) | 人工目标 -> 智能体 -> 约束框架 -> 记忆 -> 复盘 |
| [架构](docs/images/worldquant-harness-architecture.zh-CN.svg) | 智能体入口、框架控制面、记忆反馈、提交边界 |
| [Skill 效果对比](docs/images/skill-effect-comparison.svg) | 代表性 near-miss 阻塞批次和后续 skill-routed 5/5 active 案例对比 |
| [论坛到 skill memory](docs/images/forum-to-skill-memory.svg) | 论坛笔记、本地提交历史、repair 记录如何蒸馏成 typed skills |
| [失败分类](docs/images/failure-taxonomy-map.svg) | self-correlation、concentration、weak metrics、platform mismatch 和修复路线 |
| [Fresh submit loop](docs/images/fresh-submit-loop.svg) | 忽略旧 ready，重新生成候选，已有 ACTIVE 只作为相关性边界 |
| [Submit5 案例](docs/images/submit5-case-study.svg) | 不公开表达式和原始平台导出的 5 ACTIVE 脱敏案例 |
| [产物生命周期](docs/images/harness-artifact-lifecycle.svg) | 候选规格、模拟结果、复盘队列、记忆 |
| [公开演示轨迹](docs/images/public-demo-trace.svg) | 候选在就绪和拒绝状态间流转 |
| [记忆反馈](docs/images/memory-feedback-graph.svg) | 阻塞项如何变成下一轮约束 |
| [质量面板](docs/images/quality-review-dashboard.svg) | 已提交和已生成 alpha 的质量复盘 |
| [提交边界](docs/images/submit-boundary.svg) | 无提交、只检查、真实提交的分离 |
| [策略展示](docs/images/strategy-display-alpha-set.svg) | 已选 ACTIVE 验证指标，不公开 alpha 表达式 |
| [发布边界](docs/images/release-safety-boundary.svg) | 公开、私有、需复核产物 |

## 策略展示验证

公开演示证明框架契约。下面是用于策略展示的已选 ACTIVE 验证记录。它们说明受控研究可以产出可提交候选。精确 alpha 表达式和平台代码面板不公开。

<p align="center">
  <img src="docs/images/strategy-display-alpha-set.svg" width="920" alt="脱敏策略展示 alpha 指标" />
</p>

| Alpha 标识 | 状态 | WQ 夏普 | WQ 适应度 | WQ 收益 | 换手率 | 回撤 | 中性化 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| `3qz93wP6` | ACTIVE | **2.38** | 1.60 | **18.94%** | 41.79% | 6.49% | MARKET |
| `3q7Rew3e` | ACTIVE | 2.08 | 1.68 | 10.32% | 15.87% | 4.47% | SUBINDUSTRY |
| `YPN9QR0M` | ACTIVE | 2.06 | 1.72 | 8.70% | 10.93% | 3.36% | SUBINDUSTRY |
| `akd1QGp1` | ACTIVE | 1.81 | **1.78** | 12.08% | 11.87% | 5.93% | INDUSTRY |

<p align="center">
  <sub>历史验证指标。Alpha 表达式不公开。</sub>
</p>

历史表现不代表未来收益。这些验证记录不是运行开源演示的必要条件，也不构成投资建议。

## 智能体契约

智能体可以探索，但必须在契约内工作。框架管理状态，生成可复盘产物。

| 智能体动作 | 框架控制 |
|:--|:--|
| 生成候选 | 稳定候选 ID、来源标签、字段和算子提取 |
| 运行实验 | 沙盒产物，无提交默认值 |
| 解读结果 | 结构化复盘队列和拒绝原因 |
| 从失败中学习 | 记忆记录、阻塞签名、字段族统计 |
| 规划下一批 | 画像演化和显式子实验 |
| 提交 | 只允许人工选定 alpha ID |

可执行契约由 `HarnessRun`、`HarnessStep`、`HarnessEvent`、`ArtifactRef`、`DecisionGate`、`MemoryDelta`、`ProfilePatch` 表达。见 [研究契约](docs/AGENT_HARNESS_CONTRACT.md) 和 [智能体角色](docs/AGENT_ROLES.md)。

[确定性 WQ Agent Benchmark](docs/WQ_AGENT_BENCHMARK.md) 使用固定 seed、等配额预算和 canonical trace hash，在九个合成场景上比较 benchmark 内部的三种 scripted reference policy：`no_memory`、`raw_history` 与 `scoped_memory_v2`。Fake-only gateway 会在 transport 前拒绝提交动作；该结果不是对整个仓库真实 HTTP POST 的统计。

## 核心能力

| 模块 | 能力 |
|:--|:--|
| 编排 | 公开无提交评估、三变体确定性 Benchmark、沙盒实验、预提交门控、生命周期轨迹 |
| 记忆 | 历史导入、阻塞签名、字段族统计、画像演化 |
| 智能体接入 | MCP 工具、CLI 脚本、REST API、监控 UI |
| 复盘 | 质量复盘面板、提交效率报告、就绪/拒绝队列 |
| WQ 边界 | 只检查、显式凭证化提交命令 |
| 本地研究 | 本地解析器、回测、反过拟合检查、滚动验证 |

<details>
<summary><b>MCP 工具面</b></summary>

MCP 服务向智能体工作流暴露约束框架和研究操作，包括公开运行、预提交评估、历史导入、记忆维护、状态读取、本地回测、因子评分、诊断、反过拟合检查、滚动验证、显式 WQ BRAIN 检查和提交命令。

</details>

## 安装与启动

默认安装现在是 WQ-only：只包含 WQ BRAIN 模拟、check-only、显式提交和本地
SQLite 事件台账，不安装 A 股数据、本地回测、前端、HTTP/MCP 服务或 LLM 客户端。

```bash
pip install -e .
```

需要其他能力时再显式安装：

```bash
pip install -e ".[llm]"             # 模型生成候选
pip install -e ".[server]"          # HTTP 和 MCP 服务
pip install -e ".[migrations]"      # Alembic 数据库迁移
pip install -e ".[local-backtest]"  # 本地/A 股回测栈
pip install -e ".[all,dev]"         # 完整开发环境
```

启动可选 HTTP 服务：

```bash
pip install -e ".[server,local-backtest,llm]"
python -m worldquant_harness --transport http
```

在 Claude Code 或 Claude Desktop 中使用 MCP：

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

完整本地安装、Windows 说明、可选 PostgreSQL、可选 DeepSeek 配置、API 示例见 [快速开始](docs/QUICKSTART.md)。

## 项目结构

```text
worldquant-harness/
├── worldquant_harness/          # 后端、解析器、研究契约、MCP、API
├── frontend/                    # React 监控面板
├── scripts/                     # 公开演示、视觉材料、复盘、WQ 工作流
├── tests/                       # 解析器、回测、工作流、API、框架测试
├── example_factor/              # 脱敏历史验证截图
└── docs/                        # 架构、接口、MCP、工作流、安全文档
```

## 负责任使用

- 使用自己的外部服务凭证，遵守服务条款、政策、频率限制、数据限制。
- 保护 `.env`、`.secrets/`、本地数据库、原始平台导出、提交/检查台账、完整研究报告。
- 不发布 alpha 表达式、私有平台导出、未脱敏截图。
- 不把生成因子、截图、回测结果表述为确定收益。
- 发布分支或版本前，阅读 [开源审计](docs/OPEN_SOURCE_AUDIT.md) 和 [发布清单](docs/OPEN_SOURCE_RELEASE_CHECKLIST.md)。

## 许可证

[MIT](LICENSE)。版权和归因记录见 [NOTICE](NOTICE)。

本公开版本以 `worldquant-harness` 维护。衍生作品应保留版权声明，遵守 MIT 许可证条款。

更多信息见 [NOTICE](NOTICE)、[免责声明](DISCLAIMER.md)、[安全政策](SECURITY.md)、[行为准则](CODE_OF_CONDUCT.md)。
