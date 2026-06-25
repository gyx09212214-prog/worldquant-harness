"""Generate final micro self-correlation repair candidates for submit5-more run."""

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


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "final_micro_sc_repair_candidates.jsonl"


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
    parser = argparse.ArgumentParser(description="Generate final micro self-correlation repairs")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=12)
    return parser.parse_args(argv)


def _add(rows: list[dict[str, Any]], tag: str, family: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": family,
            "source": "generate_wq_submit5_more_final_micro_sc_repairs",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "micro_self_correlation_repair",
            "rationale": rationale,
            "risk_flags": [
                "real_submit_candidate",
                "requires_online_simulation",
                "near_self_corr_threshold",
            ],
        }
    )


def _settings(neut: str, decay: int, trunc: float) -> dict[str, Any]:
    return {"neutralization": neut, "decay": decay, "truncation": trunc, "maxPosition": "ON"}


def _iv_ratio(window: int) -> str:
    return (
        "rank(ts_mean((ts_backfill(implied_volatility_call_120,120)-ts_backfill(implied_volatility_put_120,120))/"
        f"(ts_backfill(implied_volatility_call_120,120)+ts_backfill(implied_volatility_put_120,120)),{window}))"
    )


def _cf(ret_short: int, ret_long: int, window: int, rev_weight: float = 0.16) -> str:
    return (
        "rank(ts_rank(ts_backfill(cashflow_op,120)/cap,"
        f"{window})-ts_rank(returns,{ret_short})+{rev_weight:.2f}*rank(-1*ts_rank(returns,{ret_long})))"
    )


def _fwd_cf(ret_short: int = 80, window: int = 120) -> str:
    return f"rank(ts_rank(ts_backfill(forward_cash_flow_to_price,120),{window})-ts_rank(returns,{ret_short}))"


def _records() -> list[dict[str, Any]]:
    sec16 = _settings("SECTOR", 16, 0.012)
    sec18 = _settings("SECTOR", 18, 0.012)
    ind16 = _settings("INDUSTRY", 16, 0.01)
    rows: list[dict[str, Any]] = []

    cap_bucket = 'bucket(rank(cap),range="0.1,1,0.1")'
    book_s = "group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),sector)"
    sales_s = "group_rank(ts_rank(ts_backfill(forward_sales_to_price,120),120),sector)"
    rel_s = "ts_rank(rel_ret_cust,140)"
    pcr = "rank(-1*ts_rank(ts_backfill(pcr_oi_60,120),90))"
    pcr_l = "rank(-1*ts_rank(ts_backfill(pcr_oi_60,120),120))"
    vcorr = "rank(ts_corr(vwap,volume,90))"
    beta = "rank(-1*beta_last_30_days_spy)"
    spy_corr = "rank(-1*correlation_last_30_days_spy)"
    cov = "ts_rank(snt1_d1_analystcoverage,100)"

    core_a = (
        f"0.09*{_iv_ratio(16)}+0.10*{_cf(65,210,115)}+0.10*{book_s}+0.13*{sales_s}+"
        f"0.11*{rel_s}+0.05*{pcr}+0.02*{vcorr}+0.03*{beta}-0.12*ts_rank(returns,165)"
    )
    _add(
        rows,
        "final-micro-cap-pcr-beta-sec16",
        "6xe_cap_bucket_near_threshold",
        f"rank(group_rank({core_a},{cap_bucket}))",
        sec16,
        "Start from vRmEo8nQ but cut vwap-volume and reversal overlap, adding small PCR and beta legs.",
    )

    core_b = (
        f"0.10*{_iv_ratio(18)}+0.09*{_cf(70,220,120)}+0.10*{book_s}+0.14*{sales_s}+"
        f"0.12*{rel_s}+0.05*{pcr_l}+0.03*{spy_corr}-0.12*ts_rank(returns,170)"
    )
    _add(
        rows,
        "final-micro-cap-no-vcorr-sec16",
        "6xe_cap_bucket_near_threshold",
        f"rank(group_rank({core_b},{cap_bucket}))",
        sec16,
        "Remove the vwap-volume leg entirely while keeping the cap-bucket concentration repair.",
    )

    core_c = (
        f"0.09*{_iv_ratio(18)}+0.09*{_fwd_cf(80,120)}+0.10*{book_s}+0.14*{sales_s}+"
        f"0.12*{rel_s}+0.05*{pcr}+0.04*{beta}-0.11*ts_rank(returns,170)"
    )
    _add(
        rows,
        "final-micro-cap-forwardcf-sec18",
        "6xe_cap_bucket_near_threshold",
        f"rank(group_rank({core_c},{cap_bucket}))",
        sec18,
        "Swap cashflow_op/cap for forward cash-flow-to-price to reduce d5n/RRN overlap.",
    )

    core_d = (
        f"0.09*{_iv_ratio(20)}+0.10*{_cf(75,230,125)}+0.08*{book_s}+0.15*{sales_s}+"
        f"0.13*{rel_s}+0.04*{pcr}+0.03*{cov}+0.03*{spy_corr}-0.11*ts_rank(returns,180)"
    )
    _add(
        rows,
        "final-micro-cap-salesrel-sec16",
        "6xe_cap_bucket_near_threshold",
        f"rank(group_rank({core_d},{cap_bucket}))",
        sec16,
        "Shift a little more payload into sales and relationship legs, but not as far as the weak rel-heavy run.",
    )

    core_e = (
        f"0.08*{_iv_ratio(16)}+0.10*{_cf(65,210,115)}+0.09*{book_s}+0.14*{sales_s}+"
        f"0.12*{rel_s}+0.06*{pcr}+0.03*{cov}+0.02*{beta}-0.11*ts_rank(returns,165)"
    )
    _add(
        rows,
        "final-micro-cap-pcrcov-sec16",
        "6xe_cap_bucket_near_threshold",
        f"rank(group_rank({core_e},{cap_bucket}))",
        sec16,
        "PCR plus analyst coverage variant of the cap-bucket repair.",
    )

    core_f = (
        f"0.10*{_iv_ratio(16)}+0.10*{_cf(70,220,120)}+0.10*{book_s}+0.13*{sales_s}+"
        f"0.11*{rel_s}+0.04*{pcr_l}+0.04*rank(-1*ts_rank(correlation_last_30_days_spy,60))-0.12*ts_rank(returns,170)"
    )
    _add(
        rows,
        "final-micro-cap-spycorr-ts-sec16",
        "6xe_cap_bucket_near_threshold",
        f"rank(group_rank({core_f},{cap_bucket}))",
        sec16,
        "Use a smoothed SPY-correlation leg instead of vwap-volume correlation.",
    )

    core_g = (
        f"0.09*{_iv_ratio(16)}+0.10*{_cf(70,220,120)}+0.09*{book_s}+0.15*{sales_s}+"
        f"0.11*{rel_s}+0.05*{pcr}+0.03*rank(ts_corr(close,volume,120))-0.11*ts_rank(returns,170)"
    )
    _add(
        rows,
        "final-micro-cap-closevol-sec16",
        "6xe_cap_bucket_near_threshold",
        f"rank(group_rank({core_g},{cap_bucket}))",
        sec16,
        "Replace vwap-volume with a longer close-volume correlation to perturb the RRN anchor.",
    )

    core_h = (
        f"0.09*{_iv_ratio(18)}+0.09*{_cf(75,230,125)}+0.10*{book_s}+0.13*{sales_s}+"
        f"0.12*{rel_s}+0.05*{pcr}+0.04*rank(-1*ts_rank(beta_last_30_days_spy,60))-0.11*ts_rank(returns,180)"
    )
    _add(
        rows,
        "final-micro-cap-beta-ts-sec18",
        "6xe_cap_bucket_near_threshold",
        f"rank(group_rank({core_h},{cap_bucket}))",
        sec18,
        "Smoothed beta leg plus slightly slower decay to reduce near-threshold self-correlation.",
    )

    miss_i = "group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly,240),60),industry)"
    book_i = "group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),industry)"
    sales_i = "group_rank(ts_rank(ts_backfill(actual_sales_value_quarterly,120)/enterprise_value,120),industry)"
    rel_i = "group_rank(ts_rank(rel_ret_cust,160),industry)"
    core_i = (
        f"0.08*{_iv_ratio(20)}+0.07*{_cf(70,220,120)}+0.12*{miss_i}+0.09*{book_i}+"
        f"0.10*{sales_i}+0.09*{rel_i}+0.05*{pcr_l}+0.04*{spy_corr}-0.11*ts_rank(returns,170)"
    )
    _add(
        rows,
        "final-missing-cap-pcr-i16",
        "missingness_lowweight_near_threshold",
        f"rank(group_rank({core_i},{cap_bucket}))",
        ind16,
        "Take the E5KjLLWL missingness branch and replace credit/vcorr with PCR/SPY correlation.",
    )

    core_j = (
        f"0.08*{_iv_ratio(22)}+0.08*{_fwd_cf(90,120)}+0.12*{miss_i}+0.09*{book_i}+"
        f"0.10*{sales_i}+0.10*{rel_i}+0.05*{pcr}+0.04*{beta}-0.10*ts_rank(returns,180)"
    )
    _add(
        rows,
        "final-missing-cap-forwardcf-i16",
        "missingness_lowweight_near_threshold",
        f"rank(group_rank({core_j},{cap_bucket}))",
        ind16,
        "Forward cash-flow variant of the concentration-pass missingness branch.",
    )

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
