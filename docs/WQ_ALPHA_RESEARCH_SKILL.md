# worldquant-harness WQ Alpha Research Iteration

本地 WQ 挖因子迭代现在分成四层：

1. `references/wq_alpha_research/` 保留 `QuantML-Research/wq-alpha-research` 的完整字段 reference。
2. `configs/wq_research_profiles/` 保存可版本化 research profile。
3. harness evolution 在每次 eval 后生成 `profile_evolution`，包含 A/B/C 三个候选 profile。
4. memory maintenance 和 daily-return correlation gate 作为提交前约束。

## Reference Catalog

查看字段库状态：

```powershell
python scripts/wq_research_profile.py catalog-status
```

搜索字段：

```powershell
python scripts/wq_research_profile.py search-fields cashflow --category fundamental --limit 10
```

字段库来自 https://github.com/QuantML-Research/wq-alpha-research 的 `references/`。上游检查时没有发现 `LICENSE` 文件，所以这里保留来源说明，不把 reference 内容硬编码进代码。

## Research Profile

初始化默认 profile：

```powershell
python scripts/wq_research_profile.py init --name default
```

查看状态：

```powershell
python scripts/wq_research_profile.py status
```

对候选 profile 做 diff 并显式 apply：

```powershell
python scripts/wq_research_profile.py diff candidate_b
python scripts/wq_research_profile.py apply candidate_b
```

profile 管理的核心参数包括：

- `similarity_policy.cutoff`
- `family_policy.max_family_count`
- `field_signature_policy.max_field_signature_count`
- `field_signature_policy.blacklist`
- `legal_input_policy.strict`
- `promotion_gate.max_daily_return_correlation`
- `memory_policy.compress_threshold`

## Harness Evolution

`evolve_wq_research_experiment` 仍保留原有 `mine_config_overrides`，同时新增：

- `profile_evolution`
- `recommended_profile_candidate`
- `recommended_research_profile`

child experiment 会记录推荐 profile，但不会真实提交。真实提交仍必须通过显式 submit 命令。

## Memory Maintenance

生成 memory 压缩和吸收到 policy 的候选建议：

```powershell
python scripts/wq_memory_maintenance.py path\to\memory.jsonl --output reports\memory_report.json --markdown-output reports\memory_report.md
```

这个命令默认只报告，不修改原始 memory。

## Daily-Return Correlation Gate

`worldquant_harness.wq_pnl_analysis` 提供：

- `daily_return_series_from_pnl`
- `aligned_daily_return_correlation`
- `max_active_daily_return_correlation`

如果 candidate row 上有 `active_daily_return_corr_max` 或 `active_daily_return_corr_gate=reject`，`presubmit_acceptance_gate` 会拒绝超过默认 `0.70` 的候选。`0.50` 以上作为 warn 阈值保存在 gate 详情里。

## Knowledge Update

从 harness eval summary 生成可审查知识片段：

```powershell
python scripts/wq_evolve_knowledge.py --eval-summary path\to\eval_summary.json
```

写入文档需要显式传 `--apply`。

## Iteration Rule

后续迭代遵守 train/validation/test 隔离：profile 和 candidate 只根据本地 sandbox、harness eval、validation 结果更新；不要把最终 test 或真实提交结果反向喂给训练期搜索参数。
