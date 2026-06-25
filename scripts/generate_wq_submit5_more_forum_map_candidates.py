"""Generate forum/map-guided candidates for the submit-5-more continuation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_auto_mining import validate_wq_expression


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "forum_map_structural_candidates.jsonl"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output = Path(args.output)
    rows: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in _records():
        key = row["expression"] + "||" + json.dumps(row["simulation_settings"], sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        try:
            validate_wq_expression(row["expression"])
        except Exception as exc:
            invalid.append({**row, "validation_error": str(exc)})
            continue
        row["candidate_rank"] = len(rows) + 1
        rows.append(row)
        if len(rows) >= args.limit:
            break

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows) + "\n",
        encoding="utf-8",
    )
    summary = {
        "ok": True,
        "output": str(output),
        "written": len(rows),
        "invalid": len(invalid),
        "tags": [row["tag"] for row in rows],
    }
    output.with_suffix(".summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    if invalid:
        output.with_suffix(".invalid.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in invalid) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate forum/map structural WQ candidates")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=40)
    return parser.parse_args(argv)


def _add(
    rows: list[dict[str, Any]],
    tag: str,
    family: str,
    expr: str,
    settings: dict[str, Any],
    rationale: str,
) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": family,
            "source": "generate_wq_submit5_more_forum_map_candidates",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "forum_map_structural_decorrelation",
            "rationale": rationale,
            "risk_flags": [
                "real_submit_candidate",
                "requires_online_simulation",
                "forum_map_guided",
            ],
        }
    )


def _records() -> list[dict[str, Any]]:
    d8 = {"neutralization": "SUBINDUSTRY", "decay": 8, "truncation": 0.05}
    d10_t01 = {"neutralization": "SUBINDUSTRY", "decay": 10, "truncation": 0.01}
    d12_t02 = {"neutralization": "SUBINDUSTRY", "decay": 12, "truncation": 0.02}
    d16 = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.03}
    d16_t01 = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.01}
    d20 = {"neutralization": "SUBINDUSTRY", "decay": 20, "truncation": 0.03}
    ind = {"neutralization": "INDUSTRY", "decay": 8, "truncation": 0.05}
    sector = {"neutralization": "SECTOR", "decay": 8, "truncation": 0.05}

    rows: list[dict[str, Any]] = []

    _add(
        rows,
        "fm-sent-event-sales-book-gate",
        "forum_sentiment_event_gate",
        "trade_when(abs(ts_delta(snt1_d1_netearningsrevision, 1)) > 0, "
        "rank(0.42 * ts_rank(actual_sales_value_quarterly / enterprise_value, 100) + "
        "0.30 * zscore(ts_mean(scl12_sentiment_fast_d1, 10)) + "
        "0.18 * group_zscore(forward_book_value_to_price, industry) - "
        "0.10 * ts_rank(volume / adv20, 30)), -1)",
        d8,
        "Directly test the lowest-similarity forum expansion; returns is absent and saturated price fields are small.",
    )
    _add(
        rows,
        "fm-sent-event-sales-book-gate-d16",
        "forum_sentiment_event_gate",
        "trade_when(abs(ts_delta(snt1_d1_netearningsrevision, 1)) > 0, "
        "rank(0.42 * ts_rank(actual_sales_value_quarterly / enterprise_value, 100) + "
        "0.30 * zscore(ts_mean(scl12_sentiment_fast_d1, 10)) + "
        "0.18 * group_zscore(forward_book_value_to_price, industry) - "
        "0.10 * ts_rank(volume / adv20, 30)), -1)",
        d16,
        "Slower version of the low-similarity event-gated forum candidate.",
    )
    _add(
        rows,
        "fm-missing-iv-revision-spread",
        "forum_missingness_coverage_spread",
        "rank(0.42 * group_zscore(ts_rank(ts_count_nans(implied_volatility_mean_30, 180), 90), sector) - "
        "0.28 * group_zscore(ts_rank(ts_count_nans(snt1_d1_netearningsrevision, 180), 90), industry) + "
        "0.18 * zscore(ts_rank(forward_book_value_to_price, 120)) + "
        "0.12 * zscore(ts_rank(volume / adv20, 30)))",
        d16,
        "Forum missingness coverage-spread candidate with verified IV/revision fields.",
    )
    _add(
        rows,
        "fm-update-decay-sentiment",
        "forum_update_event_sentiment",
        "rank(0.36 * group_zscore(ts_decay_linear(ts_delta(anl4_af_eps_value, 5), 12), subindustry) + "
        "0.28 * group_zscore(ts_rank(abs(ts_delta(scl12_sentiment_fast_d1, 1)), 20), industry) + "
        "0.22 * ts_rank(forward_book_value_to_price, 80) - "
        "0.14 * ts_rank(close / vwap, 30))",
        d8,
        "Uses update-event intensity instead of the crowded slow analyst rank shell.",
    )
    _add(
        rows,
        "fm-group-sales-eps-coverage",
        "forum_internal_group_sales_coverage",
        "rank(group_neutralize(0.40 * zscore(ts_rank(actual_sales_value_quarterly / assets, 120)) + "
        "0.30 * group_zscore(ts_delta(anl4_af_eps_value / close, 10), subindustry) + "
        "0.20 * ts_rank(snt1_d1_analystcoverage, 80) - "
        "0.10 * rank(abs(close / vwap)), industry))",
        d8,
        "Group-comparison forum structure using analyst coverage as a separate axis.",
    )
    _add(
        rows,
        "fm-missing-recency-revision",
        "forum_missingness_recency_revision",
        "rank(0.40 * zscore(days_from_last_change(ts_count_nans(actual_sales_value_quarterly, 240))) + "
        "0.32 * group_zscore(ts_delta(snt1_d1_netearningsrevision, 3), subindustry) + "
        "0.18 * ts_rank(forward_sales_to_price, 100) - "
        "0.10 * ts_rank(close / vwap, 20))",
        d16,
        "Missingness recency plus revision delta, intentionally away from the submitted dividend/micro mix.",
    )
    _add(
        rows,
        "fm-update-regime-sales-core",
        "forum_update_regime_sales",
        "trade_when(ts_rank(volume / adv20, 20) > 0.55, "
        "rank(0.44 * group_zscore(ts_delta(forward_sales_to_price, 10), industry) + "
        "0.30 * zscore(ts_std(actual_sales_value_quarterly, 63)) + "
        "0.16 * ts_rank(snt1_cored1_score, 40) - "
        "0.10 * ts_rank(open / close, 20)), -1)",
        d8,
        "Liquidity-gated sales/update event candidate from the forum expansion set.",
    )
    _add(
        rows,
        "fm-value-quality-revision",
        "forum_value_quality_revision",
        "rank(0.36 * group_zscore(ts_rank(cashflow_op / enterprise_value, 180), sector) + "
        "0.28 * group_zscore(ts_rank(forward_cash_flow_to_price, 100), industry) + "
        "0.22 * zscore(ts_delta(snt1_d1_netearningsrevision, 5)) - "
        "0.14 * ts_rank(high / low, 20))",
        sector,
        "Value-quality forum candidate; included because prior cashflow overlay had good IS but high self-corr.",
    )
    _add(
        rows,
        "fm-a130-sent-delta-coverage",
        "a130_forum_structure_shift",
        "rank(group_rank(0.13 * ts_rank(ts_backfill(earnings_revision_magnitude, 120), 70) + "
        "0.13 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / enterprise_value, 100) + "
        "0.12 * ts_rank(ts_backfill(forward_sales_to_price, 120), 100) + "
        "0.11 * group_zscore(ts_delta(snt1_d1_netearningsrevision, 5), subindustry) + "
        "0.10 * zscore(ts_mean(scl12_sentiment_fast_d1, 10)) + "
        "0.08 * ts_rank(snt1_d1_analystcoverage, 80) + "
        "0.08 * rank(ts_corr(close, volume, 40)) - "
        "0.18 * ts_rank(returns, 70), subindustry))",
        d12_t02,
        "Nearpass a130 repair that reduces EPS/momentum weight and adds forum sentiment/coverage axes.",
    )
    _add(
        rows,
        "fm-a130-pcr-spy-update",
        "a130_options_market_update",
        "rank(group_rank(0.13 * ts_rank(ts_backfill(earnings_revision_magnitude, 120), 80) + "
        "0.14 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / enterprise_value, 100) + "
        "0.13 * ts_rank(ts_backfill(forward_sales_to_price, 120), 100) + "
        "0.10 * rank(-1 * ts_backfill(earnings_certainty_rank_derivative, 120)) + "
        "0.08 * rank(-1 * ts_rank(pcr_oi_60, 90)) + "
        "0.07 * rank(-1 * correlation_last_30_days_spy) + "
        "0.07 * zscore(ts_delta(snt1_d1_netearningsrevision, 5)) + "
        "0.07 * rank(ts_corr(vwap, volume, 50)) - "
        "0.19 * ts_rank(returns, 80), subindustry))",
        d16_t01,
        "Takes the 0.7016 a130 edge and changes both overlay and smoothing.",
    )
    _add(
        rows,
        "fm-p0-revision-missing-micro",
        "p0_missingness_forum_micro",
        "rank(0.38 * group_rank(ts_backfill(0.17 * ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 40) + "
        "0.16 * group_zscore(ts_delta(snt1_d1_netearningsrevision, 3), subindustry) + "
        "0.13 * zscore(ts_mean(scl12_sentiment_fast_d1, 10)) + "
        "0.14 * rank(volume / adv20) + "
        "0.12 * rank(ts_corr(vwap, volume, 40)) - "
        "0.18 * ts_rank(returns, 40), 60), industry) + "
        "0.26 * ts_rank(-ts_delta(vwap, 10) / vwap, 40) + "
        "0.20 * rank((high - close) / (high - low) * rank(volume / ts_mean(volume, 20))) + "
        "0.16 * rank(-ts_decay_linear(close / vwap, 10)))",
        d16,
        "Uses the successful p0 micro shell but replaces dividend with forum revision/sentiment axes.",
    )
    _add(
        rows,
        "fm-p0-sales-forward-micro",
        "p0_sales_forward_micro",
        "rank(0.36 * group_rank(ts_backfill(0.17 * ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 40) + "
        "0.16 * ts_rank(forward_sales_to_price, 100) + "
        "0.13 * ts_rank(snt1_cored1_score, 60) + "
        "0.14 * rank(volume / adv20) + "
        "0.12 * rank(ts_corr(vwap, volume, 40)) - "
        "0.18 * ts_rank(returns, 50), 60), industry) + "
        "0.26 * ts_rank(-ts_delta(vwap, 10) / vwap, 40) + "
        "0.20 * rank((high - close) / (high - low) * rank(volume / ts_mean(volume, 20))) + "
        "0.18 * rank(-ts_decay_linear(close / vwap, 10)))",
        d20,
        "Moves the p0 shell toward forward-sales and core sentiment to reduce overlap with pw7xWejv.",
    )
    _add(
        rows,
        "fm-core-group-rank-sales-pcr",
        "forum_group_rank_sales_pcr",
        "rank(group_neutralize(0.16 * group_rank(ts_rank(actual_sales_value_quarterly / enterprise_value, 120), industry) + "
        "0.14 * group_rank(ts_rank(forward_sales_to_price, 120), subindustry) + "
        "0.12 * ts_rank(snt1_cored1_score, 70) + "
        "0.10 * rank(-1 * credit_risk_premium_indicator) + "
        "0.08 * rank(-1 * ts_rank(pcr_oi_60, 90)) + "
        "0.08 * rank(-1 * ts_rank(close / vwap, 40)) + "
        "0.08 * rank(ts_corr(close, volume, 50)) - "
        "0.16 * ts_rank(returns, 100), industry))",
        d8,
        "Forum group-rank success shell shifted away from cashflow_op/cap toward sales and PCR.",
    )
    _add(
        rows,
        "fm-core-group-rank-coverage-value",
        "forum_group_rank_coverage_value",
        "rank(group_neutralize(0.16 * group_rank(ts_rank(forward_book_value_to_price, 120), industry) + "
        "0.14 * group_rank(ts_rank(actual_sales_value_quarterly / assets, 120), subindustry) + "
        "0.12 * ts_rank(snt1_d1_analystcoverage, 80) + "
        "0.10 * rank(-1 * credit_risk_premium_indicator) + "
        "0.08 * rank(-1 * correlation_last_30_days_spy) + "
        "0.08 * rank(ts_corr(vwap, volume, 50)) - "
        "0.16 * ts_rank(returns, 100), industry))",
        ind,
        "Coverage/value group comparison with market-correlation residual.",
    )
    _add(
        rows,
        "fm-relcust-sales-credit",
        "relationship_customer_sales_probe",
        "rank(group_neutralize(0.24 * ts_rank(rel_ret_cust, 120) + "
        "0.20 * ts_rank(forward_sales_to_price, 100) + "
        "0.16 * ts_rank(actual_sales_value_quarterly / enterprise_value, 100) + "
        "0.12 * rank(-1 * credit_risk_premium_indicator) + "
        "0.10 * rank(-1 * correlation_last_30_days_spy) + "
        "0.08 * rank(volume / adv20) - "
        "0.10 * ts_rank(returns, 80), industry))",
        ind,
        "Small relationship probe using only rel_ret_cust, which appears in platform history.",
    )
    _add(
        rows,
        "fm-relcust-pcr-regime",
        "relationship_customer_pcr_regime",
        "trade_when(ts_rank(volume / adv20, 20) > 0.55, "
        "rank(0.24 * ts_rank(rel_ret_cust, 120) + "
        "0.20 * ts_rank(forward_sales_to_price, 100) + "
        "0.16 * rank(-1 * ts_rank(pcr_oi_10, 60)) + "
        "0.14 * rank(-1 * credit_risk_premium_indicator) + "
        "0.10 * rank(ts_corr(vwap, volume, 60)) - "
        "0.12 * ts_rank(returns, 90)), -1)",
        d16,
        "Relationship/PCR probe gated by liquidity, avoiding unsupported supplier/short fields.",
    )
    _add(
        rows,
        "fm-ivmean-coverage-value",
        "ivmean_coverage_value",
        "rank(group_neutralize(0.24 * ts_rank(implied_volatility_mean_30, 90) + "
        "0.20 * ts_rank(forward_sales_to_price, 100) + "
        "0.16 * ts_rank(snt1_d1_analystcoverage, 80) + "
        "0.14 * rank(-1 * beta_last_30_days_spy) + "
        "0.10 * rank(ts_corr(vwap, volume, 50)) - "
        "0.16 * ts_rank(returns, 90), industry))",
        d8,
        "IV mean plus coverage/value axis; uses fields present in platform history.",
    )
    _add(
        rows,
        "fm-missing-coverage-backfill",
        "missingness_backfill_coverage",
        "rank(group_neutralize(0.24 * ts_rank(ts_backfill(forward_sales_to_price, 120), 100) + "
        "0.18 * zscore(days_from_last_change(ts_count_nans(actual_sales_value_quarterly, 240))) + "
        "0.16 * ts_rank(snt1_d1_analystcoverage, 80) + "
        "0.14 * rank(-1 * credit_risk_premium_indicator) + "
        "0.10 * rank(ts_corr(close, volume, 40)) - "
        "0.18 * ts_rank(returns, 90), industry))",
        d16,
        "Coverage and missingness backfill structure that does not use dividend or cashflow_op anchors.",
    )
    _add(
        rows,
        "fm-sales-sentiment-no-returns",
        "sales_sentiment_no_returns",
        "rank(group_neutralize(0.26 * ts_rank(actual_sales_value_quarterly / enterprise_value, 120) + "
        "0.22 * ts_rank(forward_sales_to_price, 100) + "
        "0.18 * zscore(ts_mean(scl12_sentiment_fast_d1, 10)) + "
        "0.14 * group_zscore(ts_delta(snt1_d1_netearningsrevision, 5), subindustry) + "
        "0.10 * rank(-1 * beta_last_30_days_spy) + "
        "0.10 * rank(ts_corr(close, volume, 50)), industry))",
        d12_t02,
        "A no-returns sales/sentiment candidate to reduce overlap with active reversal anchors.",
    )
    _add(
        rows,
        "fm-book-revision-pcr-no-cashflow",
        "book_revision_pcr_no_cashflow",
        "rank(group_neutralize(0.26 * ts_rank(forward_book_value_to_price, 120) + "
        "0.22 * zscore(ts_delta(snt1_d1_netearningsrevision, 5)) + "
        "0.18 * ts_rank(scl12_sentiment_fast_d1, 40) + "
        "0.14 * rank(-1 * ts_rank(pcr_oi_60, 90)) + "
        "0.10 * rank(-1 * correlation_last_30_days_spy) + "
        "0.10 * rank(ts_corr(vwap, volume, 50)), industry))",
        d16_t01,
        "No cashflow/opincome fields; intended as a genuinely separate book/revision/PCR map node.",
    )
    _add(
        rows,
        "fm-sales-iv-pcr-sector",
        "sales_iv_pcr_sector",
        "rank(group_neutralize(0.24 * ts_rank(actual_sales_value_quarterly / enterprise_value, 120) + "
        "0.20 * ts_rank(forward_sales_to_price, 100) + "
        "0.18 * group_rank(ts_backfill(implied_volatility_call_120 - implied_volatility_put_120, 60), subindustry) + "
        "0.14 * rank(-1 * ts_rank(pcr_oi_60, 90)) + "
        "0.10 * rank(-1 * beta_last_30_days_spy) - "
        "0.14 * ts_rank(returns, 100), sector))",
        sector,
        "Sales plus option-skew/PCR with sector neutralization instead of the crowded IV90 cashflow shell.",
    )

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
