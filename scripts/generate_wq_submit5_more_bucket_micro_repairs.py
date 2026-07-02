"""Generate cap-bucket concentration and micro self-correlation repairs."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_candidate_generation import run_static_candidate_generator

DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "bucket_micro_repair_candidates.jsonl"


def main(argv: list[str] | None = None) -> int:
    return run_static_candidate_generator(
        argv,
        records_func=_records,
        default_output=DEFAULT_OUTPUT,
        default_limit=10,
        description='Generate cap-bucket and micro self-corr repairs',
        limit_valid_count=False,
    )


def _add(rows: list[dict[str, Any]], tag: str, family: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": family,
            "source": "generate_wq_submit5_more_bucket_micro_repairs",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "cap_bucket_or_micro_self_corr_repair",
            "rationale": rationale,
            "risk_flags": [
                "real_submit_candidate",
                "requires_online_simulation",
                "bucket_concentration_repair",
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
    i16 = _settings("INDUSTRY", 16, 0.01)
    i20 = _settings("INDUSTRY", 20, 0.01)
    sec16 = _settings("SECTOR", 16, 0.012)
    sub16 = _settings("SUBINDUSTRY", 16, 0.01)
    rows: list[dict[str, Any]] = []

    cap_bucket = 'bucket(rank(cap),range="0.1,1,0.1")'
    miss_i = "group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly,240),60),industry)"
    book_i = "group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),industry)"
    sales_i = "group_rank(ts_rank(ts_backfill(actual_sales_value_quarterly,120)/enterprise_value,120),industry)"
    rel_i = "group_rank(ts_rank(rel_ret_cust,160),industry)"
    miss_sub = "group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly,240),60),subindustry)"
    book_sub = "group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),subindustry)"
    sales_sub = "group_rank(ts_rank(ts_backfill(actual_sales_value_quarterly,120)/enterprise_value,120),subindustry)"
    rel_sub = "group_rank(ts_rank(rel_ret_cust,160),subindustry)"

    n1_core_i = (
        f"0.06*{_iv_ratio(24)}+"
        "0.07*rank(ts_rank(ts_backfill(forward_cash_flow_to_price,120),120)-ts_rank(returns,80))+"
        f"0.12*{miss_i}+0.12*{book_i}+0.11*{sales_i}+0.09*{rel_i}+"
        "0.07*rank(-1*correlation_last_30_days_spy)-0.10*ts_rank(returns,180)"
    )
    n1_core_sub = (
        f"0.06*{_iv_ratio(24)}+"
        "0.07*rank(ts_rank(ts_backfill(forward_cash_flow_to_price,120),120)-ts_rank(returns,80))+"
        f"0.12*{miss_sub}+0.12*{book_sub}+0.11*{sales_sub}+0.09*{rel_sub}+"
        "0.07*rank(-1*correlation_last_30_days_spy)-0.10*ts_rank(returns,180)"
    )

    _add(
        rows,
        "bucket-missing-cap-grank-i16",
        "missingness_cap_bucket_concentration",
        f"rank(group_rank({n1_core_i},{cap_bucket}))",
        i16,
        "Apply cap-bucket group-rank to the strong N1Od/78d missingness-value core.",
    )
    _add(
        rows,
        "bucket-missing-cap-neutral-i16",
        "missingness_cap_bucket_concentration",
        f"rank(group_neutralize({n1_core_i},{cap_bucket}))",
        i16,
        "Cap-bucket neutralization version of the same strong core.",
    )
    _add(
        rows,
        "bucket-missing-sub-cap-grank",
        "missingness_cap_bucket_concentration",
        f"rank(group_rank({n1_core_sub},{cap_bucket}))",
        sub16,
        "Subindustry neutralization plus cap-bucket group-rank.",
    )
    _add(
        rows,
        "bucket-missing-hump-i20",
        "missingness_hump_concentration",
        f"rank(hump({n1_core_i}))",
        i20,
        "Use WQ hump smoothing as a stronger single-name weight damper.",
    )
    _add(
        rows,
        "bucket-missing-lowweight-i16",
        "missingness_lowweight_concentration",
        f"rank(0.10*{_iv_ratio(18)}+0.09*{_cf(60,200,110)}+0.11*{miss_i}+0.10*{book_i}+0.08*{sales_i}+0.08*{rel_i}+0.05*rank(-1*credit_risk_premium_indicator)+0.05*rank(ts_corr(vwap,volume,100))-0.12*ts_rank(returns,160))",
        i16,
        "Return closer to 9qReWYQd's concentration-pass structure while lowering ZYod overlap.",
    )

    _add(
        rows,
        "micro-d5n-open-tiny-sec16",
        "6xe_micro_self_corr",
        f"rank(0.10*{_iv_ratio(16)}+0.11*{_cf(60,200,110)}+0.10*group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),sector)+0.13*group_rank(ts_rank(ts_backfill(forward_sales_to_price,120),120),sector)+0.12*ts_rank(rel_ret_cust,140)+0.04*group_rank(rank((open-close)/open),sector)+0.03*rank(ts_corr(vwap,volume,90))+0.04*rank(-1*beta_last_30_days_spy)-0.12*ts_rank(returns,160))",
        sec16,
        "Reduce the open/corr terms that pushed JjdAoJAx into omYo self-corr while keeping 6XE's metric profile.",
    )
    _add(
        rows,
        "micro-d5n-pcr-tiny-sec16",
        "6xe_micro_self_corr",
        f"rank(0.10*{_iv_ratio(16)}+0.11*{_cf(60,200,110)}+0.10*group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),sector)+0.13*group_rank(ts_rank(ts_backfill(forward_sales_to_price,120),120),sector)+0.12*ts_rank(rel_ret_cust,140)+0.05*rank(-1*ts_rank(ts_backfill(pcr_oi_60,120),90))+0.03*rank(ts_corr(vwap,volume,90))-0.12*ts_rank(returns,160))",
        sec16,
        "Replace most open-close support with a small PCR horizon shift.",
    )
    _add(
        rows,
        "micro-d5n-rel-heavy-sec16",
        "6xe_micro_self_corr",
        f"rank(0.09*{_iv_ratio(18)}+0.10*{_cf(70,220,120)}+0.09*group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),sector)+0.14*group_rank(ts_rank(ts_backfill(forward_sales_to_price,120),120),sector)+0.15*ts_rank(rel_ret_cust,140)+0.04*rank(ts_corr(vwap,volume,90))+0.04*ts_rank(snt1_d1_analystcoverage,100)-0.12*ts_rank(returns,170))",
        sec16,
        "Move just enough weight from IV/book to relationship/coverage without the large performance loss of the previous nobook variant.",
    )
    _add(
        rows,
        "micro-d5n-cap-grank-sec16",
        "6xe_micro_self_corr",
        f"rank(group_rank(0.10*{_iv_ratio(16)}+0.11*{_cf(60,200,110)}+0.11*group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),sector)+0.12*group_rank(ts_rank(ts_backfill(forward_sales_to_price,120),120),sector)+0.10*ts_rank(rel_ret_cust,140)+0.05*rank(ts_corr(vwap,volume,90))-0.12*ts_rank(returns,160),{cap_bucket}))",
        sec16,
        "Cap-bucket rank the original 6XE-like repair to lower self-corr without changing payload weights much.",
    )
    _add(
        rows,
        "micro-d5n-no-corr-sec16",
        "6xe_micro_self_corr",
        f"rank(0.10*{_iv_ratio(18)}+0.11*{_cf(60,200,110)}+0.10*group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),sector)+0.13*group_rank(ts_rank(ts_backfill(forward_sales_to_price,120),120),sector)+0.12*ts_rank(rel_ret_cust,140)+0.06*group_rank(rank((open-close)/open),sector)+0.04*rank(-1*correlation_last_30_days_spy)-0.12*ts_rank(returns,160))",
        sec16,
        "Remove vwap-volume correlation, the leg shared with several self-corr anchors.",
    )

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
