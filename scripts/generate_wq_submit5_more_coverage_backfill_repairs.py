"""Generate coverage/backfill repairs for high-IS platform candidates."""

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


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "coverage_backfill_repair_candidates.jsonl"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output = Path(args.output)
    rows: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []

    for row in _records()[: args.limit]:
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
    parser = argparse.ArgumentParser(description="Generate WQ coverage/backfill repair candidates")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=24)
    return parser.parse_args(argv)


def _add(rows: list[dict[str, Any]], tag: str, family: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": family,
            "source": "generate_wq_submit5_more_coverage_backfill_repairs",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "coverage_backfill_repair_for_concentrated_high_is",
            "rationale": rationale,
            "risk_flags": [
                "real_submit_candidate",
                "requires_online_simulation",
                "coverage_backfill_repair",
                "platform_high_is_repair",
            ],
        }
    )


def _records() -> list[dict[str, Any]]:
    i12 = {"neutralization": "INDUSTRY", "decay": 12, "truncation": 0.01, "maxPosition": "ON"}
    i16 = {"neutralization": "INDUSTRY", "decay": 16, "truncation": 0.01, "maxPosition": "ON"}
    s12 = {"neutralization": "SECTOR", "decay": 12, "truncation": 0.01, "maxPosition": "ON"}
    sub16 = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.01, "maxPosition": "ON"}
    sub12 = {"neutralization": "SUBINDUSTRY", "decay": 12, "truncation": 0.01, "maxPosition": "ON"}

    rows: list[dict[str, Any]] = []

    qmx_bf = (
        "rank(0.16*group_rank(ts_rank(ts_backfill(actual_eps_value_quarterly,120)/close,90),industry)+"
        "0.13*ts_rank(ts_backfill(anl4_af_eps_value,120),80)+"
        "0.11*ts_rank(ts_backfill(change_in_eps_surprise,120),80)+"
        "0.12*rank(ts_mean((ts_backfill(implied_volatility_call_120,120)-ts_backfill(implied_volatility_put_120,120))/(ts_backfill(implied_volatility_call_120,120)+ts_backfill(implied_volatility_put_120,120)),5))+"
        "0.10*rank(ts_mean(ts_rank(vwap/close,20),3))+0.10*rank(volume/adv20)+"
        "0.10*ts_rank(ts_backfill(forward_sales_to_price,120),100)+"
        "0.08*rank(-1*ts_rank(ts_backfill(pcr_oi_60,120),60))-0.16*ts_rank(returns,90))"
    )
    _add(rows, "bf-qmx-iv-eps-sales-i12", "qmx_options_eps_backfill", qmx_bf, i12, "Backfill every sparse qMX field to repair concentrated weight while keeping the high-IS structure.")
    _add(rows, "bf-qmx-iv-eps-sales-sub16", "qmx_options_eps_backfill", qmx_bf, sub16, "Subindustry neutralization variant of the qMX coverage repair.")

    qmx_group = (
        "rank(group_neutralize(0.15*group_rank(ts_rank(ts_backfill(actual_eps_value_quarterly,120)/close,90),industry)+"
        "0.12*group_rank(ts_rank(ts_backfill(anl4_af_eps_value,120),80),industry)+"
        "0.10*group_rank(ts_rank(ts_backfill(change_in_eps_surprise,120),80),subindustry)+"
        "0.12*rank(ts_mean((ts_backfill(implied_volatility_call_120,120)-ts_backfill(implied_volatility_put_120,120))/(ts_backfill(implied_volatility_call_120,120)+ts_backfill(implied_volatility_put_120,120)),5))+"
        "0.10*rank(volume/adv20)+0.10*group_rank(ts_rank(ts_backfill(forward_sales_to_price,120),100),industry)+"
        "0.08*rank(-1*ts_rank(ts_backfill(pcr_oi_60,120),80))-0.14*ts_rank(returns,100),industry))"
    )
    _add(rows, "bf-qmx-grouped-i16", "qmx_options_eps_grouped_backfill", qmx_group, i16, "Group-rank sparse qMX legs before neutralization to reduce single-name spikes.")
    _add(rows, "bf-qmx-grouped-sub16", "qmx_options_eps_grouped_backfill", qmx_group, sub16, "Subindustry setting for the grouped qMX backfill repair.")

    omyl_bf = (
        "rank(0.15*ts_rank(ts_backfill(actual_eps_value_quarterly,120)/close,90)+"
        "0.11*ts_rank(ts_backfill(anl4_af_eps_value,120),80)+"
        "0.10*ts_rank(ts_backfill(change_in_eps_surprise,120),80)+"
        "0.13*rank(ts_mean((ts_backfill(implied_volatility_call_90,120)-ts_backfill(implied_volatility_put_90,120))/(ts_backfill(implied_volatility_call_90,120)+ts_backfill(implied_volatility_put_90,120)),5))+"
        "0.11*rank(volume/ts_mean(volume,20))+0.10*rank(ts_corr(vwap,volume,60))+"
        "0.10*ts_rank(ts_backfill(forward_book_value_to_price,120),100)+"
        "0.08*rank(-1*ts_rank(ts_backfill(pcr_oi_10,120),80))-0.16*ts_rank(returns,90))"
    )
    _add(rows, "bf-omyl-eps-iv-book-i12", "omyl_options_book_backfill", omyl_bf, i12, "Backfill the omYL options/book expression that had high IS but concentrated weight.")
    _add(rows, "bf-omyl-eps-iv-book-sub16", "omyl_options_book_backfill", omyl_bf, sub16, "Subindustry variant of the omYL coverage repair.")

    np_bf = (
        "rank(0.14*group_rank(ts_rank(ts_backfill(actual_eps_value_quarterly,120)/vwap,100),subindustry)+"
        "0.14*group_rank(ts_rank(ts_backfill(actual_sales_value_quarterly,120)/enterprise_value,100),subindustry)+"
        "0.12*group_rank(ts_rank(ts_backfill(forward_sales_to_price,120),100),subindustry)+"
        "0.12*group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),100),subindustry)+"
        "0.12*rank(ts_mean((ts_backfill(implied_volatility_call_90,120)-ts_backfill(implied_volatility_put_90,120))/(ts_backfill(implied_volatility_call_90,120)+ts_backfill(implied_volatility_put_90,120)),10))+"
        "0.09*rank(-1*ts_rank(ts_backfill(pcr_oi_10,120),80))+0.08*rank(ts_corr(vwap,volume,40))-0.15*ts_rank(returns,60))"
    )
    _add(rows, "bf-np-value-options-sub12", "np_value_options_backfill", np_bf, sub12, "Backfill the low-active-sim value/options platform candidate that already passed sub-universe.")
    _add(rows, "bf-np-value-options-i16", "np_value_options_backfill", np_bf, i16, "Industry variant of the value/options backfill repair.")

    cf_credit = (
        "rank(group_neutralize(0.26*ts_rank(ts_backfill(cashflow_op,120)/cap,80)+"
        "0.16*rank(-1*correlation_last_30_days_spy)+0.14*rank(-1*ts_decay_linear(close/vwap,5))+"
        "0.12*rank(-1*cashflow_efficiency_rank_derivative)+0.10*rank(volume/adv20)-0.16*ts_rank(returns,70),industry))"
    )
    _add(rows, "bf-cashflow-credit-i12", "cashflow_credit_backfill", cf_credit, i12, "Backfill the cashflow/credit platform expression that had strong IS but concentrated weight.")
    _add(rows, "bf-cashflow-credit-sub16", "cashflow_credit_backfill", cf_credit, sub16, "Subindustry version of cashflow/credit backfill repair.")

    cf_value = (
        "rank(group_neutralize(0.16*group_rank(ts_rank(ts_backfill(actual_cashflow_per_share_value_quarterly,120)/close,90),subindustry)+"
        "0.14*group_rank(ts_rank(ts_backfill(forward_cash_flow_to_price,140),120),industry)+"
        "0.14*rank(-1*relative_valuation_rank_derivative)+0.12*rank(-1*credit_risk_premium_indicator)+"
        "0.12*rank(-1*ts_rank(ts_backfill(pcr_oi_60,120),70))+0.10*rank((high-close)/(high-low)*volume/adv20)-0.10*ts_rank(returns,90),industry))"
    )
    _add(rows, "bf-cashflow-value-pcr-i12", "cashflow_value_pcr_backfill", cf_value, i12, "Backfill the high-IS cashflow/value/PCR expression and keep group dispersion.")
    _add(rows, "bf-cashflow-value-pcr-s12", "cashflow_value_pcr_backfill", cf_value, s12, "Sector neutralization variant of the cashflow/value/PCR repair.")

    iv_cashflow = (
        "rank(0.36*group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,120),subindustry)+"
        "0.24*rank(0.70*rank(ts_rank(ts_backfill(cashflow_op,120)/cap,80)-ts_rank(returns,30))+0.30*rank(-1*ts_rank(returns,120)))+"
        "0.14*rank(volume/adv20)+0.10*rank(ts_corr(vwap,volume,60))-0.12*ts_rank(returns,80))"
    )
    _add(rows, "bf-iv-cashflow-mixed-sub12", "iv_cashflow_mixed_backfill", iv_cashflow, sub12, "Lower and backfill the strong IV/cashflow mixed platform expression.")
    _add(rows, "bf-iv-cashflow-mixed-i16", "iv_cashflow_mixed_backfill", iv_cashflow, i16, "Industry slower variant of the IV/cashflow mixed repair.")

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
