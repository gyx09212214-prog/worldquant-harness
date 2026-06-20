"""Generate near self-correlation residual repairs for submit-5-more continuation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.wq_auto_mining import validate_wq_expression


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "near_sc_residual_candidates.jsonl"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output = Path(args.output)
    rows: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in _records()[: args.limit]:
        key = row["expression"] + "||" + json.dumps(row.get("simulation_settings") or {}, sort_keys=True)
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
    output.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if invalid:
        output.with_suffix(".invalid.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in invalid) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate near self-correlation residual candidates")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=10)
    return parser.parse_args(argv)


def _add(rows: list[dict[str, Any]], tag: str, family: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": family,
            "source": "generate_wq_submit5_more_near_sc_residual_candidates",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "near_self_correlation_residual_repair",
            "rationale": rationale,
            "risk_flags": [
                "real_submit_candidate",
                "requires_online_simulation",
                "near_self_correlation_threshold",
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


def _qmx_shallow(scale: float = 1.0) -> str:
    return (
        f"{0.030 * scale:.3f}*group_rank(ts_rank(ts_backfill(actual_eps_value_quarterly,120)/close,90),industry)+"
        f"{0.025 * scale:.3f}*ts_rank(ts_backfill(anl4_af_eps_value,120),80)+"
        f"{0.025 * scale:.3f}*ts_rank(ts_backfill(change_in_eps_surprise,120),80)+"
        f"{0.030 * scale:.3f}*rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,120))+"
        f"{0.025 * scale:.3f}*rank(volume/adv20)+"
        f"{0.030 * scale:.3f}*group_rank(ts_rank(ts_backfill(cashflow_op,120)/cap,80),subindustry)+"
        f"{0.025 * scale:.3f}*rank(-1*credit_risk_premium_indicator)+"
        f"{0.020 * scale:.3f}*rank(ts_corr(vwap,volume,60))-"
        f"{0.035 * scale:.3f}*ts_rank(returns,100)"
    )


def _omyo_shallow(scale: float = 1.0) -> str:
    return (
        f"{0.060 * scale:.3f}*group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,60),subindustry)+"
        f"{0.055 * scale:.3f}*rank((open-close)/open)+"
        f"{0.035 * scale:.3f}*rank(ts_corr(vwap,volume,40))"
    )


def _d5n_shallow(scale: float = 1.0) -> str:
    return (
        f"{0.055 * scale:.3f}*group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,60),subindustry)+"
        f"{0.040 * scale:.3f}*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+"
        f"{0.035 * scale:.3f}*rank(-1*credit_risk_premium_indicator)+"
        f"{0.030 * scale:.3f}*rank(volume/adv20)-"
        f"{0.035 * scale:.3f}*ts_rank(returns,120)"
    )


def _records() -> list[dict[str, Any]]:
    i14 = _settings("INDUSTRY", 14, 0.01)
    i16 = _settings("INDUSTRY", 16, 0.01)
    sec14 = _settings("SECTOR", 14, 0.015)
    sub14 = _settings("SUBINDUSTRY", 14, 0.01)
    sub16 = _settings("SUBINDUSTRY", 16, 0.01)
    rows: list[dict[str, Any]] = []

    missing_grouped = "group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly,240),60),industry)"
    book_grouped = "group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),industry)"
    rel_grouped = "group_rank(ts_rank(rel_ret_cust,160),subindustry)"
    open_grouped = "group_rank(rank((open-close)/open),subindustry)"
    ivspread_sub = "group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,60),subindustry)"
    sent = "zscore(ts_mean(scl12_sentiment_fast_d1,12))"
    revision = "group_zscore(ts_delta(snt1_d1_netearningsrevision,7),subindustry)"
    coverage = "ts_rank(snt1_d1_analystcoverage,100)"

    _add(
        rows,
        "near-sc-missing-sales-rel-i16",
        "missingness_sales_relationship",
        f"rank(0.08*{_iv_ratio(18)}+0.07*{_cf()}+0.17*{missing_grouped}+0.12*{book_grouped}+0.12*ts_rank(ts_backfill(forward_sales_to_price,120),120)+0.11*ts_rank(rel_ret_cust,160)+0.10*{sent}+0.08*{coverage}-0.10*ts_rank(returns,180))",
        i16,
        "Dilute the 9qReWYQd missingness hit away from ZYodJEk1 by replacing credit/vwap legs with sales, relationship, sentiment, and coverage.",
    )
    _add(
        rows,
        "near-sc-missing-qmx-resid-i16",
        "missingness_qmx_residual",
        f"rank(0.06*{_iv_ratio(20)}+0.05*{_cf(80,240,130)}+0.17*{missing_grouped}+0.13*{book_grouped}+0.12*ts_rank(ts_backfill(forward_sales_to_price,120),120)+0.10*{sent}+0.08*{revision}-0.10*ts_rank(returns,180)-0.05*rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,120))-0.04*rank(ts_corr(vwap,volume,60)))",
        i16,
        "Explicitly subtract a shallow ZYodJEk1 proxy while keeping the concentration-safe grouped missingness legs.",
    )
    _add(
        rows,
        "near-sc-missing-d5n-resid-sub16",
        "missingness_d5n_residual",
        f"rank(0.16*group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly,240),60),subindustry)+0.12*group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),subindustry)+0.12*ts_rank(ts_backfill(forward_cash_flow_to_price,120),120)+0.10*ts_rank(rel_ret_cust,160)+0.10*{coverage}+0.08*{sent}-0.10*ts_rank(returns,180)-0.05*{ivspread_sub}-0.04*rank(-1*credit_risk_premium_indicator))",
        sub16,
        "Subtract the value/IV/credit d5n-style anchor that was just above threshold for 9qReWYQd.",
    )
    _add(
        rows,
        "near-sc-missing-sent-coverage-sec14",
        "missingness_sentiment_coverage",
        f"rank(0.07*{_iv_ratio(20)}+0.05*{_cf(80,240,130)}+0.15*group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly,240),60),sector)+0.11*group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),sector)+0.14*{sent}+0.10*{revision}+0.10*{coverage}+0.09*rank(-1*beta_last_30_days_spy)-0.10*ts_rank(returns,180))",
        sec14,
        "A sector version that leans into the lower-covered forum sentiment/revision/coverage axes.",
    )
    _add(
        rows,
        "near-sc-missing-no-credit-i14",
        "missingness_no_credit",
        f"rank(0.07*{_iv_ratio(22)}+0.05*rank(ts_rank(ts_backfill(forward_cash_flow_to_price,120),120)-ts_rank(returns,80))+0.18*{missing_grouped}+0.13*{book_grouped}+0.13*ts_rank(ts_backfill(actual_sales_value_quarterly,120)/enterprise_value,120)+0.10*ts_rank(rel_ret_cust,160)+0.09*rank(-1*correlation_last_30_days_spy)-0.10*ts_rank(returns,180))",
        i14,
        "Remove the credit-risk and vwap-volume legs that overlap heavily with ZYodJEk1.",
    )
    _add(
        rows,
        "near-sc-open-rel-sales-sub16",
        "openclose_relationship_sales",
        f"rank(0.08*{_iv_ratio(20)}+0.07*{_cf(70,220,120)}+0.07*{open_grouped}+0.14*{rel_grouped}+0.13*ts_rank(ts_backfill(forward_sales_to_price,120),120)+0.10*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.10*{sent}+0.08*rank(-1*beta_last_30_days_spy)-0.10*ts_rank(returns,180))",
        sub16,
        "Keep the A13pvnYe open-close idea but make relationship and sales the dominant payload instead of omYo-like IV/open-close/vwap.",
    )
    _add(
        rows,
        "near-sc-open-omyo-resid-sub16",
        "openclose_omyo_residual",
        f"rank(0.05*{_iv_ratio(22)}+0.05*{_cf(80,240,130)}+0.05*{open_grouped}+0.16*{rel_grouped}+0.13*ts_rank(ts_backfill(forward_sales_to_price,120),120)+0.10*{sent}+0.08*{revision}-0.10*ts_rank(returns,180)-0.05*{ivspread_sub}-0.04*rank((open-close)/open)-0.03*rank(ts_corr(vwap,volume,40)))",
        sub16,
        "Subtract a shallow omYo proxy, the main self-correlation anchor for A13pvnYe.",
    )
    _add(
        rows,
        "near-sc-open-d5n-resid-sec14",
        "openclose_d5n_residual",
        f"rank(0.05*{_iv_ratio(22)}+0.05*{_cf(80,240,130)}+0.06*group_rank(rank((open-close)/open),sector)+0.15*ts_rank(rel_ret_cust,160)+0.13*ts_rank(ts_backfill(forward_sales_to_price,120),120)+0.11*{coverage}+0.09*rank(-1*correlation_last_30_days_spy)-0.10*ts_rank(returns,180)-0.05*{ivspread_sub}-0.04*rank(-1*credit_risk_premium_indicator))",
        sec14,
        "Sector open-close residual against d5n-style IV/book/credit overlap.",
    )
    _add(
        rows,
        "near-sc-open-sent-rev-i16",
        "openclose_sentiment_revision",
        f"rank(0.07*{_iv_ratio(22)}+0.05*{_cf(80,240,130)}+0.06*group_rank(rank((open-close)/open),industry)+0.12*ts_rank(rel_ret_cust,160)+0.12*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.14*{sent}+0.10*{revision}+0.09*rank(-1*beta_last_30_days_spy)-0.10*ts_rank(returns,180))",
        i16,
        "Use open-close only as a small timing leg, with sentiment/revision as the decorrelating driver.",
    )
    _add(
        rows,
        "near-sc-rel-main-book-sub14",
        "relationship_main_value",
        f"rank(0.06*{_iv_ratio(24)}+0.05*{_cf(90,260,140)}+0.16*{rel_grouped}+0.14*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.12*ts_rank(ts_backfill(forward_sales_to_price,120),120)+0.11*{coverage}+0.10*{sent}+0.07*{open_grouped}-0.10*ts_rank(returns,180))",
        sub14,
        "Make relationship/value/coverage the main axis and retain only a small open-close/IV timing support.",
    )

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
