"""Generate self-correlation threshold repairs for near-pass WQ candidates."""

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


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "sc_threshold_repair_candidates.jsonl"


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
    parser = argparse.ArgumentParser(description="Generate WQ self-correlation threshold repairs")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=32)
    return parser.parse_args(argv)


def _add(rows: list[dict[str, Any]], tag: str, family: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": family,
            "source": "generate_wq_submit5_more_sc_threshold_repairs",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "self_correlation_threshold_repair",
            "rationale": rationale,
            "risk_flags": [
                "real_submit_candidate",
                "requires_online_simulation",
                "near_self_correlation_threshold",
            ],
        }
    )


def _opt_inner(
    *,
    pcr_field: str = "pcr_oi_20",
    pcr_window: int = 80,
    iv_days: int = 60,
    iv_window: int = 12,
    ret_window: int = 45,
    eps_window: int = 100,
) -> str:
    return (
        "ts_backfill("
        "0.25*ts_rank(change_in_eps_surprise,90)+"
        f"0.20*ts_rank(actual_eps_value_quarterly/open,{eps_window})-"
        f"0.18*ts_rank({pcr_field},{pcr_window})+"
        f"0.22*rank(ts_mean((implied_volatility_call_{iv_days}-implied_volatility_put_{iv_days})/"
        f"(implied_volatility_call_{iv_days}+implied_volatility_put_{iv_days}),{iv_window}))+"
        "0.12*rank(volume/adv20)-"
        f"0.18*ts_rank(returns,{ret_window}),80)"
    )


def _opt_core(group: str = "industry", **kwargs: Any) -> str:
    return f"group_rank({_opt_inner(**kwargs)},{group})"


def _vwap_delta(window: int = 14, rank_window: int = 55) -> str:
    return f"ts_rank(-ts_delta(vwap,{window})/vwap,{rank_window})"


def _range_pressure() -> str:
    return "rank((high-close)/(high-low)*rank(volume/ts_mean(volume,20)))"


def _close_vwap(window: int = 16) -> str:
    return f"rank(-ts_decay_linear(close/vwap,{window}))"


def _iv120_raw() -> str:
    return "group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,120),subindustry)"


def _iv120_ratio(window: int = 8) -> str:
    return (
        "rank(ts_mean((ts_backfill(implied_volatility_call_120,120)-ts_backfill(implied_volatility_put_120,120))/"
        f"(ts_backfill(implied_volatility_call_120,120)+ts_backfill(implied_volatility_put_120,120)),{window}))"
    )


def _cf_op(window: int = 100) -> str:
    return f"rank(ts_rank(ts_backfill(cashflow_op,120)/cap,{window})-ts_rank(returns,50))"


def _records() -> list[dict[str, Any]]:
    i8 = {"neutralization": "INDUSTRY", "decay": 8, "truncation": 0.04, "maxPosition": "ON"}
    i10 = {"neutralization": "INDUSTRY", "decay": 10, "truncation": 0.03, "maxPosition": "ON"}
    i12 = {"neutralization": "INDUSTRY", "decay": 12, "truncation": 0.03, "maxPosition": "ON"}
    s8 = {"neutralization": "SECTOR", "decay": 8, "truncation": 0.04, "maxPosition": "ON"}
    s12 = {"neutralization": "SECTOR", "decay": 12, "truncation": 0.03, "maxPosition": "ON"}
    sub12 = {"neutralization": "SUBINDUSTRY", "decay": 12, "truncation": 0.02, "maxPosition": "ON"}
    sub16 = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.02, "maxPosition": "ON"}

    rows: list[dict[str, Any]] = []

    _add(
        rows,
        "opt-sc-core-heavy-i10",
        "options_eps_core_heavy",
        f"rank(0.58*{_opt_core()}+0.18*{_vwap_delta()}+0.08*rank(volume/adv20)+0.08*rank(ts_corr(vwap,volume,80))-0.10*ts_rank(returns,120))",
        i10,
        "Lower the micro-reversal payload that collided with pw7xWejv and let the options/earnings core dominate.",
    )
    _add(
        rows,
        "opt-sc-core-heavy-sec8",
        "options_eps_core_heavy",
        f"rank(0.58*{_opt_core('sector')}+0.18*{_vwap_delta()}+0.08*rank(volume/adv20)+0.08*rank(ts_corr(vwap,volume,80))-0.10*ts_rank(returns,120))",
        s8,
        "Sector bucket variant of the core-heavy options repair.",
    )
    _add(
        rows,
        "opt-sc-no-range-i12",
        "options_eps_no_range",
        f"rank(0.55*{_opt_core(pcr_window=100, ret_window=60)}+0.16*{_vwap_delta(16,60)}+0.12*{_iv120_ratio(8)}+0.09*rank(volume/adv20)-0.10*ts_rank(returns,140))",
        i12,
        "Remove the range-pressure leg entirely and substitute a slower IV ratio.",
    )
    _add(
        rows,
        "opt-sc-no-range-sec12",
        "options_eps_no_range",
        f"rank(0.54*{_opt_core('sector', pcr_window=100, ret_window=60)}+0.16*{_vwap_delta(16,60)}+0.12*{_iv120_ratio(8)}+0.09*rank(volume/adv20)-0.10*ts_rank(returns,140))",
        s12,
        "Sector version of the no-range options repair.",
    )
    _add(
        rows,
        "opt-sc-analyst-coverage-i8",
        "options_eps_analyst_coverage",
        f"rank(0.46*{_opt_core(pcr_field='pcr_oi_60', pcr_window=100, iv_days=90, iv_window=10, ret_window=55)}+0.16*{_vwap_delta(14,55)}+0.14*ts_rank(snt1_d1_analystcoverage,90)+0.10*rank(ts_corr(vwap,volume,70))-0.10*ts_rank(returns,120))",
        i8,
        "Use analyst-coverage as the orthogonal payload instead of more close/vwap pressure.",
    )
    _add(
        rows,
        "opt-sc-analyst-coverage-sub12",
        "options_eps_analyst_coverage",
        f"rank(0.44*{_opt_core('subindustry', pcr_field='pcr_oi_60', pcr_window=100, iv_days=90, iv_window=10, ret_window=55)}+0.16*{_vwap_delta(14,55)}+0.14*ts_rank(snt1_d1_analystcoverage,90)+0.10*rank(ts_corr(vwap,volume,70))-0.10*ts_rank(returns,120))",
        sub12,
        "Subindustry analyst-coverage decorrelation variant.",
    )
    _add(
        rows,
        "opt-sc-book-sales-i10",
        "options_book_sales_decor",
        f"rank(0.44*{_opt_core(pcr_field='pcr_oi_60', pcr_window=90, iv_days=90, ret_window=60)}+0.14*ts_rank(ts_backfill(forward_book_value_to_price,120),100)+0.12*ts_rank(ts_backfill(forward_sales_to_price,120),100)+0.14*{_vwap_delta(14,55)}-0.10*ts_rank(returns,120))",
        i10,
        "Bridge the options base to book/sales value fields that are underrepresented in the two new ACTIVE alphas.",
    )
    _add(
        rows,
        "opt-sc-book-sales-sec12",
        "options_book_sales_decor",
        f"rank(0.42*{_opt_core('sector', pcr_field='pcr_oi_60', pcr_window=90, iv_days=90, ret_window=60)}+0.14*ts_rank(ts_backfill(forward_book_value_to_price,120),100)+0.12*ts_rank(ts_backfill(forward_sales_to_price,120),100)+0.14*{_vwap_delta(14,55)}-0.10*ts_rank(returns,120))",
        s12,
        "Sector book/sales bridge for the options near-pass cluster.",
    )
    _add(
        rows,
        "opt-sc-range-tiny-i8",
        "options_tiny_range",
        f"rank(0.54*{_opt_core(pcr_window=100, ret_window=55)}+0.18*{_vwap_delta(14,55)}+0.05*{_range_pressure()}+0.08*rank(ts_corr(vwap,volume,80))-0.10*ts_rank(returns,120))",
        i8,
        "Keep only a tiny range-pressure term, below the old direct-micro repair weight.",
    )
    _add(
        rows,
        "opt-sc-closevwap-tiny-i12",
        "options_tiny_closevwap",
        f"rank(0.54*{_opt_core(pcr_window=100, ret_window=55)}+0.18*{_vwap_delta(14,55)}+0.05*{_close_vwap(18)}+0.08*rank(ts_corr(vwap,volume,80))-0.10*ts_rank(returns,120))",
        i12,
        "Keep a tiny close/vwap term while shifting most weight into options/earnings.",
    )

    _add(
        rows,
        "ivcf-sc-rel-credit-sub12",
        "iv_cashflow_rel_credit_decor",
        f"rank(0.28*{_iv120_raw()}+0.18*{_cf_op(100)}+0.13*ts_rank(rel_ret_cust,140)+0.10*rank(-1*correlation_last_30_days_spy)+0.10*rank(volume/adv20)+0.08*rank(ts_corr(vwap,volume,80))-0.12*ts_rank(returns,120))",
        sub12,
        "Lower the IV/cashflow mixed weight and add relationship/credit legs to move away from LLnYjZQv and ZYodJEk1.",
    )
    _add(
        rows,
        "ivcf-sc-rel-credit-i12",
        "iv_cashflow_rel_credit_decor",
        f"rank(0.28*group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,120),industry)+0.18*{_cf_op(100)}+0.13*ts_rank(rel_ret_cust,140)+0.10*rank(-1*correlation_last_30_days_spy)+0.10*rank(volume/adv20)+0.08*rank(ts_corr(vwap,volume,80))-0.12*ts_rank(returns,120))",
        i12,
        "Industry version of the relationship/credit mixed IV repair.",
    )
    _add(
        rows,
        "ivcf-sc-ratio-value-sub16",
        "iv_cashflow_ratio_value_decor",
        f"rank(0.24*{_iv120_ratio(10)}+0.20*{_cf_op(120)}+0.14*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.10*rank(-1*credit_risk_premium_indicator)+0.10*rank(volume/adv20)+0.08*rank(ts_corr(vwap,volume,80))-0.12*ts_rank(returns,120))",
        sub16,
        "Use normalized IV ratio plus book value instead of the raw IV spread that drove the old correlation.",
    )
    _add(
        rows,
        "ivcf-sc-ratio-value-i12",
        "iv_cashflow_ratio_value_decor",
        f"rank(0.24*{_iv120_ratio(10)}+0.20*{_cf_op(120)}+0.14*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.10*rank(-1*credit_risk_premium_indicator)+0.10*rank(volume/adv20)+0.08*rank(ts_corr(vwap,volume,80))-0.12*ts_rank(returns,120))",
        i12,
        "Industry setting for the normalized IV/value repair.",
    )
    _add(
        rows,
        "ivcf-sc-sent-revision-sub12",
        "iv_cashflow_sentiment_decor",
        f"rank(0.24*{_iv120_raw()}+0.18*{_cf_op(100)}+0.14*zscore(ts_mean(scl12_sentiment_fast_d1,12))+0.10*group_zscore(ts_delta(snt1_d1_netearningsrevision,7),subindustry)+0.10*rank(volume/adv20)+0.08*rank(ts_corr(vwap,volume,80))-0.12*ts_rank(returns,120))",
        sub12,
        "Forum sentiment/revision payload to push the IV/cashflow hit outside the existing active map.",
    )
    _add(
        rows,
        "ivcf-sc-sales-rel-i10",
        "iv_cashflow_sales_relationship",
        f"rank(0.24*group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,120),industry)+0.18*{_cf_op(100)}+0.12*ts_rank(ts_backfill(forward_sales_to_price,120),120)+0.12*ts_rank(rel_ret_cust,120)+0.10*rank(volume/adv20)+0.08*rank(ts_corr(vwap,volume,80))-0.12*ts_rank(returns,120))",
        i10,
        "Blend IV/cashflow with sales value and customer relationship instead of stronger reversal terms.",
    )

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
