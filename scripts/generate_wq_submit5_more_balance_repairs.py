"""Generate balanced concentration/self-correlation repairs for the submit-5-more continuation."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_candidate_generation import run_static_candidate_generator

DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "balance_repair_candidates.jsonl"


def main(argv: list[str] | None = None) -> int:
    return run_static_candidate_generator(
        argv,
        records_func=_records,
        default_output=DEFAULT_OUTPUT,
        default_limit=12,
        description='Generate balanced WQ repair candidates',
        limit_valid_count=False,
    )


def _add(rows: list[dict[str, Any]], tag: str, family: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": family,
            "source": "generate_wq_submit5_more_balance_repairs",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "balanced_concentration_self_corr_repair",
            "rationale": rationale,
            "risk_flags": [
                "real_submit_candidate",
                "requires_online_simulation",
                "near_threshold_repair",
            ],
        }
    )


def _settings(neut: str, decay: int, trunc: float) -> dict[str, Any]:
    return {"neutralization": neut, "decay": decay, "truncation": trunc, "maxPosition": "ON"}


def _iv_ratio(window: int = 14) -> str:
    return (
        "rank(ts_mean((ts_backfill(implied_volatility_call_120,120)-ts_backfill(implied_volatility_put_120,120))/"
        f"(ts_backfill(implied_volatility_call_120,120)+ts_backfill(implied_volatility_put_120,120)),{window}))"
    )


def _cf(ret_short: int = 60, ret_long: int = 200, window: int = 110) -> str:
    return (
        "rank(ts_rank(ts_backfill(cashflow_op,120)/cap,"
        f"{window})-ts_rank(returns,{ret_short})+0.20*rank(-1*ts_rank(returns,{ret_long})))"
    )


def _records() -> list[dict[str, Any]]:
    i14 = _settings("INDUSTRY", 14, 0.01)
    i16 = _settings("INDUSTRY", 16, 0.01)
    i20 = _settings("INDUSTRY", 20, 0.01)
    sec16 = _settings("SECTOR", 16, 0.012)
    sub16 = _settings("SUBINDUSTRY", 16, 0.01)
    sub20 = _settings("SUBINDUSTRY", 20, 0.01)
    rows: list[dict[str, Any]] = []

    miss_i = "group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly,240),60),industry)"
    miss_sub = "group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly,240),60),subindustry)"
    book_i = "group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),industry)"
    book_sub = "group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),subindustry)"
    sales_i = "group_rank(ts_rank(ts_backfill(actual_sales_value_quarterly,120)/enterprise_value,120),industry)"
    sales_sub = "group_rank(ts_rank(ts_backfill(actual_sales_value_quarterly,120)/enterprise_value,120),subindustry)"
    rel_i = "group_rank(ts_rank(rel_ret_cust,160),industry)"
    rel_sub = "group_rank(ts_rank(rel_ret_cust,160),subindustry)"
    open_sub = "group_rank(rank((open-close)/open),subindustry)"
    corr_i = "group_rank(rank(-1*correlation_last_30_days_spy),industry)"

    _add(
        rows,
        "balance-missing-sales-grank-i16",
        "n1od_concentration_repair",
        f"rank(0.07*{_iv_ratio(22)}+0.05*rank(ts_rank(ts_backfill(forward_cash_flow_to_price,120),120)-ts_rank(returns,80))+0.17*{miss_i}+0.13*{book_i}+0.13*{sales_i}+0.10*{rel_i}+0.08*{corr_i}-0.10*ts_rank(returns,180))",
        i16,
        "Group-rank every raw payload from N1OdNAYw to keep its IS signal while repairing concentrated weight.",
    )
    _add(
        rows,
        "balance-missing-sales-grank-sub16",
        "n1od_concentration_repair",
        f"rank(0.07*{_iv_ratio(22)}+0.05*rank(ts_rank(ts_backfill(forward_cash_flow_to_price,120),120)-ts_rank(returns,80))+0.17*{miss_sub}+0.13*{book_sub}+0.13*{sales_sub}+0.10*{rel_sub}+0.08*rank(-1*correlation_last_30_days_spy)-0.10*ts_rank(returns,180))",
        sub16,
        "Subindustry version of the N1OdNAYw concentration repair.",
    )
    _add(
        rows,
        "balance-missing-lowiv-grank-i20",
        "n1od_concentration_repair",
        f"rank(0.05*{_iv_ratio(26)}+0.05*rank(ts_rank(ts_backfill(forward_cash_flow_to_price,120),140)-ts_rank(returns,90))+0.18*{miss_i}+0.14*{book_i}+0.13*{sales_i}+0.10*{rel_i}+0.08*rank(-1*beta_last_30_days_spy)-0.10*ts_rank(returns,200))",
        i20,
        "Further lower the IV/cashflow overlap that caused ZYod/d5n self-correlation risk.",
    )
    _add(
        rows,
        "balance-missing-open-support-sub16",
        "n1od_open_support",
        f"rank(0.06*{_iv_ratio(24)}+0.05*rank(ts_rank(ts_backfill(forward_cash_flow_to_price,120),120)-ts_rank(returns,80))+0.15*{miss_sub}+0.12*{book_sub}+0.12*{sales_sub}+0.10*{rel_sub}+0.08*{open_sub}+0.07*rank(-1*correlation_last_30_days_spy)-0.10*ts_rank(returns,180))",
        sub16,
        "Add a small grouped open-close support leg without returning to the omYo-heavy open-close profile.",
    )
    _add(
        rows,
        "balance-missing-book-cash-i14",
        "n1od_cashflow_value",
        f"rank(0.06*{_iv_ratio(24)}+0.07*rank(ts_rank(ts_backfill(forward_cash_flow_to_price,120),120)-ts_rank(returns,80))+0.16*{miss_i}+0.15*{book_i}+0.10*group_rank(ts_rank(ts_backfill(forward_sales_to_price,120),120),industry)+0.09*{rel_i}+0.08*{corr_i}-0.10*ts_rank(returns,180))",
        i14,
        "Swap the more concentrated actual-sales leg for forward cashflow/sales value while keeping grouped missingness.",
    )

    _add(
        rows,
        "balance-d5n-lowiv-missing-sec16",
        "6xe_self_corr_repair",
        f"rank(0.10*{_iv_ratio(16)}+0.10*{_cf(60,200,110)}+0.12*group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),sector)+0.12*group_rank(ts_rank(ts_backfill(forward_sales_to_price,120),120),sector)+0.11*ts_rank(rel_ret_cust,140)+0.10*group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly,240),60),sector)+0.06*rank(ts_corr(vwap,volume,90))-0.12*ts_rank(returns,160))",
        sec16,
        "Start from 6XE6n99G and lower the IV/book overlap with d5n, replacing it with grouped missingness.",
    )
    _add(
        rows,
        "balance-d5n-open-support-sec16",
        "6xe_self_corr_repair",
        f"rank(0.10*{_iv_ratio(16)}+0.11*{_cf(60,200,110)}+0.11*group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),sector)+0.12*group_rank(ts_rank(ts_backfill(forward_sales_to_price,120),120),sector)+0.10*ts_rank(rel_ret_cust,140)+0.08*group_rank(rank((open-close)/open),sector)+0.06*rank(ts_corr(vwap,volume,90))-0.12*ts_rank(returns,160))",
        sec16,
        "Use a small sector-grouped open-close leg to move the 6XE branch below the d5n self-corr threshold.",
    )
    _add(
        rows,
        "balance-d5n-nobook-rel-sec16",
        "6xe_self_corr_repair",
        f"rank(0.11*{_iv_ratio(18)}+0.10*{_cf(70,220,120)}+0.06*group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),sector)+0.15*group_rank(ts_rank(ts_backfill(forward_sales_to_price,120),120),sector)+0.14*ts_rank(rel_ret_cust,140)+0.09*ts_rank(snt1_d1_analystcoverage,100)+0.06*rank(ts_corr(vwap,volume,90))-0.12*ts_rank(returns,170))",
        sec16,
        "Cut the book leg that likely drives d5n overlap and compensate with sales, relationship, and coverage.",
    )
    _add(
        rows,
        "balance-d5n-sub-grank-sub20",
        "6xe_self_corr_repair",
        f"rank(0.10*{_iv_ratio(18)}+0.10*{_cf(70,220,120)}+0.10*{book_sub}+0.13*group_rank(ts_rank(ts_backfill(forward_sales_to_price,120),120),subindustry)+0.13*{rel_sub}+0.08*rank(ts_corr(vwap,volume,90))+0.08*rank(-1*correlation_last_30_days_spy)-0.12*ts_rank(returns,170))",
        sub20,
        "Subindustry grouped version of the 6XE branch, slower decay for lower turnover and lower self-corr.",
    )
    _add(
        rows,
        "balance-d5n-cash-sales-i16",
        "6xe_self_corr_repair",
        f"rank(0.09*{_iv_ratio(20)}+0.08*rank(ts_rank(ts_backfill(forward_cash_flow_to_price,120),120)-ts_rank(returns,80))+0.10*{book_i}+0.14*group_rank(ts_rank(ts_backfill(forward_sales_to_price,120),120),industry)+0.12*{rel_i}+0.08*ts_rank(snt1_d1_analystcoverage,100)+0.08*rank(ts_corr(vwap,volume,90))-0.12*ts_rank(returns,180))",
        i16,
        "Industry cashflow/sales variant designed to stay close to 6XE metrics while reducing d5n overlap.",
    )

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
