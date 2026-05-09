# SC Saturation Rule

## 规则
同一算子家族（`ts_decay_linear` 或 `ts_av_diff`）+ `rank(Y)` 模板的因子，在累积 3-5 个 ACTIVE 后，后续变体全部 SC > 0.7。调参（窗口/基底/基本面/中性化）无法突破。

## 两个已知算子家族

### 家族 1: ts_decay_linear（3 个 ACTIVE）
- ACTIVE: 78aAQjoL (MARKET), 2raboRxb (MARKET), xAP0o9NJ (IND)
- SC 饱和后: 换窗口 (w=5~30) SC 0.81~0.94, 换基底 (c/o, W%R, ret) SC 0.75~0.91

### 家族 2: ts_av_diff（5 个 ACTIVE）
- ACTIVE: O0baermq (MARKET), e7dR8YpE (MARKET), e7dR8Mnl (MARKET), npZVOkMx (IND), 1YaEn36k (IND)
- SC 饱和后: 换窗口/中性化 SC 0.825~0.989

## SC 差异化机制
- **算子结构不同** → SC 低：ts_decay_linear 与 ts_av_diff 互相独立
- **中性化不同** → SC 中等差异：MARKET 与 IND 可各有一个 PASS，但 SECTOR/SUBINDUSTRY 无效
- **参数不同** → SC 几乎不变：换窗口/基本面/decay 对 SC 影响 <0.1

## 应用
1. 每个新算子家族预期可贡献 3-5 个 ACTIVE alpha
2. 同一家族内，MARKET + IND 两种中性化各有机会
3. 必须发现第三个算子家族才能继续扩展 ACTIVE 数量
4. 候选第三家族：ts_corr, ts_rank (需解决换手率), ts_sum(log(x),N), group_rank 等
5. 条件结构 where()/trade_when() 和 sign_power() 在 free tier 不可用，排除
