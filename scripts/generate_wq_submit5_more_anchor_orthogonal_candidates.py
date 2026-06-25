"""Generate anchor-orthogonal candidates for the submit-5-more continuation.

This batch starts from the near-threshold families that already passed IS
metrics, then dilutes the known self-correlation anchors with lower-covered
forum/map axes: relationship customer return, sales value, sentiment/revision,
coverage/missingness, and market-correlation controls.
"""

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


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "anchor_orthogonal_candidates.jsonl"


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
    parser = argparse.ArgumentParser(description="Generate anchor-orthogonal WQ candidates")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=36)
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
            "source": "generate_wq_submit5_more_anchor_orthogonal_candidates",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "anchor_orthogonal_forum_map_blend",
            "rationale": rationale,
            "risk_flags": [
                "real_submit_candidate",
                "requires_online_simulation",
                "factor_map_guided",
                "forum_axis_overlay",
            ],
        }
    )


def _records() -> list[dict[str, Any]]:
    settings = [
        {"neutralization": "INDUSTRY", "decay": 8, "truncation": 0.05},
        {"neutralization": "INDUSTRY", "decay": 10, "truncation": 0.03},
        {"neutralization": "SECTOR", "decay": 8, "truncation": 0.05},
        {"neutralization": "SECTOR", "decay": 10, "truncation": 0.03},
        {"neutralization": "SUBINDUSTRY", "decay": 12, "truncation": 0.03},
        {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.02},
    ]

    opt_ind = (
        "group_rank(ts_backfill(0.18*ts_rank(change_in_eps_surprise,60)+"
        "0.14*ts_rank(actual_eps_value_quarterly/open,80)-"
        "0.12*ts_rank(pcr_oi_20,80)+"
        "0.12*rank(ts_mean((implied_volatility_call_60-implied_volatility_put_60)/"
        "(implied_volatility_call_60+implied_volatility_put_60),12))-"
        "0.12*ts_rank(returns,40),60),industry)"
    )
    opt_sector = opt_ind.replace(",industry)", ",sector)")
    opt_sub = opt_ind.replace(",industry)", ",subindustry)")

    specs: list[tuple[str, str, str, list[dict[str, Any]], str]] = [
        (
            "ortho-short-opt-rel-sales",
            "options_relationship_sales",
            f"rank(0.30*{opt_sector}+0.18*ts_rank(rel_ret_cust,120)+0.16*ts_rank(forward_sales_to_price,100)+0.12*ts_rank(actual_sales_value_quarterly/enterprise_value,120)+0.10*rank(-1*credit_risk_premium_indicator)+0.08*ts_rank(-ts_delta(vwap,12)/vwap,50)+0.06*rank(-1*correlation_last_30_days_spy)-0.10*ts_rank(returns,100))",
            [settings[3], settings[2]],
            "Dilutes the near-threshold options shell with relationship/customer sales and market-correlation controls.",
        ),
        (
            "ortho-short-opt-sent-coverage",
            "options_sentiment_coverage",
            f"rank(0.28*{opt_ind}+0.18*zscore(ts_mean(scl12_sentiment_fast_d1,10))+0.14*group_zscore(ts_delta(snt1_d1_netearningsrevision,5),subindustry)+0.14*ts_rank(snt1_d1_analystcoverage,80)+0.12*ts_rank(forward_book_value_to_price,120)+0.08*ts_rank(-ts_delta(vwap,12)/vwap,50)-0.10*ts_rank(returns,100))",
            [settings[1], settings[4]],
            "Uses forum sentiment, revision, and coverage axes to move away from the pw7xWejv micro path.",
        ),
        (
            "ortho-short-opt-cashflow",
            "options_cashflow_credit",
            f"rank(0.28*{opt_ind}+0.16*group_rank(ts_rank(cashflow_op/cap,100),industry)+0.16*ts_rank(forward_cash_flow_to_price,120)+0.12*rank(-1*earnings_certainty_rank_derivative)+0.10*rank(-1*credit_risk_premium_indicator)+0.08*ts_rank(snt1_cored1_score,80)+0.08*rank(-1*ts_rank(close/vwap,40))-0.12*ts_rank(returns,100))",
            [settings[1], settings[4]],
            "Adds cashflow and credit payload to the options shell while keeping the shared options leg below one-third.",
        ),
        (
            "ortho-short-sales-iv-pcr",
            "sales_iv_pcr_boosted",
            "rank(0.22*ts_rank(actual_sales_value_quarterly/enterprise_value,120)+0.18*ts_rank(forward_sales_to_price,100)+0.16*group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,60),subindustry)+0.12*rank(-1*ts_rank(pcr_oi_60,90))+0.10*ts_rank(rel_ret_cust,120)+0.10*ts_rank(-ts_delta(vwap,12)/vwap,50)+0.08*rank(-1*beta_last_30_days_spy)-0.12*ts_rank(returns,100))",
            [settings[2], settings[3]],
            "Lifts the near-pass sales-IV-PCR forum candidate with customer-return and vwap trajectory legs.",
        ),
        (
            "ortho-short-rel-main",
            "relationship_customer_main",
            f"rank(0.24*ts_rank(rel_ret_cust,120)+0.18*ts_rank(forward_sales_to_price,100)+0.14*ts_rank(actual_sales_value_quarterly/enterprise_value,120)+0.14*{opt_sector}+0.10*rank(-1*credit_risk_premium_indicator)+0.08*rank(-1*ts_rank(pcr_oi_60,90))+0.06*rank(volume/adv20)-0.12*ts_rank(returns,100))",
            [settings[2], settings[3]],
            "Makes relationship customer return the main low-covered axis, supported by a small options payload.",
        ),
        (
            "ortho-short-cert-rel-sales",
            "certainty_dividend_relationship",
            "rank(0.18*rank(-1*earnings_certainty_rank_derivative)+0.14*ts_rank(dividends_to_gross_profit,90)+0.14*ts_rank(rel_ret_cust,120)+0.12*ts_rank(actual_sales_value_quarterly/enterprise_value,120)+0.10*ts_rank(forward_sales_to_price,100)+0.10*rank(ts_corr(close,volume,45))+0.08*ts_rank(-ts_delta(vwap,12)/vwap,50)+0.08*rank(-1*correlation_last_30_days_spy)-0.12*ts_rank(returns,100))",
            [settings[0], settings[1]],
            "Repairs the high-IS certainty/dividend candidate by replacing most micro weight with relationship/sales fields.",
        ),
        (
            "ortho-short-missdiv-forum",
            "missing_dividend_forum_overlay",
            "rank(0.16*ts_rank(ts_count_nans(actual_sales_value_quarterly,240),45)+0.14*ts_rank(dividends_to_gross_profit,90)+0.14*zscore(ts_mean(scl12_sentiment_fast_d1,10))+0.12*group_zscore(ts_delta(snt1_d1_netearningsrevision,5),subindustry)+0.12*ts_rank(forward_sales_to_price,100)+0.10*rank(ts_corr(vwap,volume,60))+0.10*rank(-1*ts_rank(pcr_oi_60,90))-0.12*ts_rank(returns,80))",
            [settings[4], settings[5]],
            "Keeps missingness/dividend as minority support and adds forum revision/sentiment to avoid the new active anchor.",
        ),
        (
            "ortho-short-cf-salesiv",
            "cashflow_sales_iv",
            "rank(0.16*group_rank(ts_rank(cashflow_op/cap,100),industry)+0.14*ts_rank(forward_cash_flow_to_price,120)+0.14*ts_rank(actual_sales_value_quarterly/enterprise_value,120)+0.12*group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,60),subindustry)+0.10*rank(-1*ts_rank(pcr_oi_60,90))+0.10*rank(-1*credit_risk_premium_indicator)+0.08*ts_rank(rel_ret_cust,120)-0.14*ts_rank(returns,110))",
            [settings[1], settings[4]],
            "Moves the crowded cashflow forum split toward sales-IV and customer-return fields.",
        ),
        (
            "ortho-short-coverage-value",
            "coverage_value_options",
            f"rank(0.16*ts_rank(snt1_d1_analystcoverage,80)+0.16*ts_rank(forward_book_value_to_price,120)+0.14*group_rank(ts_rank(actual_sales_value_quarterly/assets,120),subindustry)+0.14*{opt_sub}+0.10*rank(-1*credit_risk_premium_indicator)+0.08*rank(-1*correlation_last_30_days_spy)+0.08*rank(ts_corr(vwap,volume,50))-0.12*ts_rank(returns,100))",
            [settings[4], settings[5]],
            "Uses coverage/value as the primary map gap with a small options support leg.",
        ),
        (
            "ortho-short-sent-salesiv",
            "sentiment_sales_iv",
            "rank(0.18*zscore(ts_mean(scl12_sentiment_fast_d1,10))+0.16*group_zscore(ts_delta(snt1_d1_netearningsrevision,5),subindustry)+0.16*ts_rank(actual_sales_value_quarterly/enterprise_value,120)+0.14*ts_rank(forward_sales_to_price,100)+0.12*group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,60),subindustry)+0.10*rank(-1*ts_rank(pcr_oi_60,90))+0.08*rank(-1*beta_last_30_days_spy)-0.12*ts_rank(returns,100))",
            [settings[2], settings[3]],
            "Balances weak but low-crowded sentiment/revision with the stronger sales-IV-PCR forum structure.",
        ),
        (
            "ortho-short-opt-rel-pcr60",
            "options_relationship_pcr60",
            f"rank(0.26*{opt_sector}+0.20*ts_rank(rel_ret_cust,120)+0.16*ts_rank(actual_sales_value_quarterly/enterprise_value,120)+0.12*ts_rank(forward_sales_to_price,100)+0.10*rank(-1*ts_rank(pcr_oi_60,90))+0.08*rank(-1*beta_last_30_days_spy)+0.08*rank(-1*ts_rank(close/vwap,40))-0.12*ts_rank(returns,100))",
            [settings[2], settings[3]],
            "Further reduces the near-threshold options weight and shifts PCR horizon from 20 to 60.",
        ),
        (
            "ortho-short-cert-cf-sent",
            "certainty_cashflow_sentiment",
            "rank(0.16*rank(-1*earnings_certainty_rank_derivative)+0.14*ts_rank(dividends_to_gross_profit,90)+0.14*group_rank(ts_rank(cashflow_op/cap,100),industry)+0.12*ts_rank(forward_cash_flow_to_price,120)+0.12*zscore(ts_mean(scl12_sentiment_fast_d1,10))+0.10*rank(-1*credit_risk_premium_indicator)+0.08*rank(ts_corr(close,volume,50))-0.12*ts_rank(returns,100))",
            [settings[0], settings[4]],
            "Uses the strong certainty/dividend signal as a compact leg rather than the old full micro shell.",
        ),
    ]

    rows: list[dict[str, Any]] = []
    for tag, family, expr, setting_list, rationale in specs:
        for index, setting in enumerate(setting_list, start=1):
            row_tag = tag if index == 1 else f"{tag}-v{index}"
            _add(rows, row_tag, family, expr, setting, rationale)
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
