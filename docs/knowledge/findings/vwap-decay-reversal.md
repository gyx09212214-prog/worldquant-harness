# VWAP Decay Reversal — Strong Signal Family

## 纯 reversal 基线
- `-1 * rank(ts_decay_linear(close / vwap, 10))` → Fitness 1.07, Sharpe 1.69 (SUBMITTED, SC PASS)
- `-1 * rank(ts_decay_linear(close / vwap, 5))` → Fitness 1.06, Sharpe 1.84 (SUBMITTED, SC PASS)

## 复合因子（reversal + fundamental）
模板：`-1 * rank(ts_decay_linear(close/vwap, W)) + rank(RATIO)`

最优组合：
- `+ rank(sales/assets)` MARKET d=0 → Ft=1.47, Sh=1.93 (SC=0.74 PASS)
- `+ rank(revenue/enterprise_value)` IND d=0 → Ft=1.27, Sh=1.88 (SC=0.62 PASS)
- `+ rank(sales/assets)` IND d=2 → Ft=1.25, Sh=1.86 (SC=0.68 PASS)

## 关键规律
- MARKET 中性化对 s/a 复合因子最优（Ft=1.47 vs IND d=2 的 1.25）
- 叠加基本面比率显著提升 Fitness（1.07 → 1.47），因为增加了与 reversal 正交的信息
- 同族不同 decay 的 SC 差异小（d=2 SC=0.68 vs d=3 SC=0.77），微调参数无法根本突破 SC
- sales/ev 与 revenue/ev 在 WQ 上 SC=1.0（完全相关），虽然会计定义不同
