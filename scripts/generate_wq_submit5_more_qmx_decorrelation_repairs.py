"""Generate self-correlation repairs for the qMX coverage-backfill hit."""

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


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "qmx_decorrelation_repair_candidates.jsonl"


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
    summary = {"ok": True, "output": str(output), "written": len(rows), "invalid": len(invalid), "tags": [r["tag"] for r in rows]}
    output.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if invalid:
        output.with_suffix(".invalid.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in invalid) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate qMX self-correlation repair candidates")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=24)
    return parser.parse_args(argv)


def _add(rows: list[dict[str, Any]], tag: str, family: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": family,
            "source": "generate_wq_submit5_more_qmx_decorrelation_repairs",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "qmx_backfill_selfcorr_repair",
            "rationale": rationale,
            "risk_flags": [
                "real_submit_candidate",
                "requires_online_simulation",
                "self_correlation_repair",
                "platform_high_is_repair",
            ],
        }
    )


def _records() -> list[dict[str, Any]]:
    i12 = {"neutralization": "INDUSTRY", "decay": 12, "truncation": 0.01, "maxPosition": "ON"}
    i16 = {"neutralization": "INDUSTRY", "decay": 16, "truncation": 0.01, "maxPosition": "ON"}
    s12 = {"neutralization": "SECTOR", "decay": 12, "truncation": 0.01, "maxPosition": "ON"}
    sub16 = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.01, "maxPosition": "ON"}

    rows: list[dict[str, Any]] = []
    iv120 = (
        "rank(ts_mean((ts_backfill(implied_volatility_call_120,120)-ts_backfill(implied_volatility_put_120,120))/"
        "(ts_backfill(implied_volatility_call_120,120)+ts_backfill(implied_volatility_put_120,120)),5))"
    )
    eps = "group_rank(ts_rank(ts_backfill(actual_eps_value_quarterly,120)/close,90),industry)"
    anl = "ts_rank(ts_backfill(anl4_af_eps_value,120),80)"
    eps_chg = "ts_rank(ts_backfill(change_in_eps_surprise,120),80)"
    f_sales = "ts_rank(ts_backfill(forward_sales_to_price,120),100)"
    pcr60 = "rank(-1*ts_rank(ts_backfill(pcr_oi_60,120),80))"

    _add(
        rows,
        "qmx-decor-cashflow-credit-i12",
        "qmx_cashflow_credit_decor",
        f"rank(0.10*{eps}+0.08*{anl}+0.07*{eps_chg}+0.07*{iv120}+0.08*rank(ts_mean(ts_rank(vwap/close,20),3))+0.08*{f_sales}+0.07*{pcr60}+0.14*ts_rank(ts_backfill(cashflow_op,120)/cap,80)+0.10*rank(-1*credit_risk_premium_indicator)+0.08*rank(-1*cashflow_efficiency_rank_derivative)+0.07*rank(-1*correlation_last_30_days_spy)-0.14*ts_rank(returns,100))",
        i12,
        "Lower the qMX EPS/IV block and add cashflow/credit legs to reduce correlation to YPN9QR0M.",
    )
    _add(
        rows,
        "qmx-decor-cashflow-credit-sub16",
        "qmx_cashflow_credit_decor",
        f"rank(0.10*{eps}+0.08*{anl}+0.07*{eps_chg}+0.07*{iv120}+0.08*rank(ts_mean(ts_rank(vwap/close,20),3))+0.08*{f_sales}+0.07*{pcr60}+0.14*group_rank(ts_rank(ts_backfill(cashflow_op,120)/cap,80),subindustry)+0.10*rank(-1*credit_risk_premium_indicator)+0.08*rank(-1*cashflow_efficiency_rank_derivative)+0.07*rank(ts_corr(vwap,volume,60))-0.14*ts_rank(returns,100))",
        sub16,
        "Subindustry variant of the cashflow/credit decorrelation repair.",
    )
    _add(
        rows,
        "qmx-decor-rel-coverage-s12",
        "qmx_relationship_coverage_decor",
        f"rank(0.10*{eps}+0.08*{anl}+0.06*{eps_chg}+0.07*{iv120}+0.08*{pcr60}+0.14*ts_rank(rel_ret_cust,120)+0.12*ts_rank(snt1_d1_analystcoverage,80)+0.10*ts_rank(ts_backfill(forward_book_value_to_price,120),100)+0.08*rank(-1*credit_risk_premium_indicator)+0.07*rank(ts_corr(vwap,volume,60))-0.14*ts_rank(returns,100))",
        s12,
        "Replace part of the qMX sales/EPS block with relationship and coverage/value axes.",
    )
    _add(
        rows,
        "qmx-decor-rel-coverage-i16",
        "qmx_relationship_coverage_decor",
        f"rank(0.09*{eps}+0.08*{anl}+0.06*{eps_chg}+0.06*{iv120}+0.08*{pcr60}+0.16*ts_rank(rel_ret_cust,140)+0.12*ts_rank(snt1_d1_analystcoverage,90)+0.10*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.08*rank(-1*correlation_last_30_days_spy)+0.07*rank(-1*ts_rank(close/vwap,45))-0.14*ts_rank(returns,120))",
        i16,
        "Longer-window relationship/coverage variant to lower active overlap.",
    )
    _add(
        rows,
        "qmx-decor-sent-revision-i12",
        "qmx_sentiment_revision_decor",
        f"rank(0.09*{eps}+0.07*{anl}+0.06*{eps_chg}+0.08*{iv120}+0.08*{f_sales}+0.07*{pcr60}+0.14*zscore(ts_mean(scl12_sentiment_fast_d1,10))+0.12*group_zscore(ts_delta(snt1_d1_netearningsrevision,5),subindustry)+0.09*rank(-1*beta_last_30_days_spy)+0.08*rank(ts_corr(close,volume,60))-0.14*ts_rank(returns,100))",
        i12,
        "Use forum sentiment/revision as the main decorrelation payload while retaining a smaller qMX shell.",
    )
    _add(
        rows,
        "qmx-decor-sent-revision-sub16",
        "qmx_sentiment_revision_decor",
        f"rank(0.08*{eps}+0.07*{anl}+0.06*{eps_chg}+0.07*{iv120}+0.08*{f_sales}+0.08*{pcr60}+0.15*zscore(ts_mean(scl12_sentiment_fast_d1,12))+0.12*group_zscore(ts_delta(snt1_d1_netearningsrevision,7),subindustry)+0.09*rank(-1*correlation_last_30_days_spy)+0.08*rank(-1*ts_rank(close/vwap,45))-0.14*ts_rank(returns,120))",
        sub16,
        "Subindustry, slower sentiment/revision version.",
    )
    _add(
        rows,
        "qmx-decor-no-eps-cf-iv-s12",
        "qmx_no_eps_cashflow_iv",
        f"rank(0.12*{iv120}+0.10*{f_sales}+0.10*{pcr60}+0.18*group_rank(ts_rank(ts_backfill(cashflow_op,120)/cap,100),industry)+0.12*rank(-1*credit_risk_premium_indicator)+0.10*rank(-1*cashflow_efficiency_rank_derivative)+0.10*rank(ts_corr(vwap,volume,60))+0.08*rank(volume/adv20)-0.14*ts_rank(returns,100))",
        s12,
        "Remove EPS estimate legs entirely and test whether IV plus cashflow retains enough strength with lower self-corr.",
    )
    _add(
        rows,
        "qmx-decor-no-iv-rel-eps-i12",
        "qmx_no_iv_relationship_eps",
        f"rank(0.12*{eps}+0.10*{anl}+0.08*{eps_chg}+0.12*{f_sales}+0.18*ts_rank(rel_ret_cust,120)+0.12*ts_rank(snt1_d1_analystcoverage,80)+0.10*rank(-1*credit_risk_premium_indicator)+0.08*rank(ts_corr(vwap,volume,60))-0.14*ts_rank(returns,100))",
        i12,
        "Remove the IV spread leg and let relationship/coverage carry the decorrelation.",
    )
    _add(
        rows,
        "qmx-decor-cashflow-sales-i16",
        "qmx_cashflow_sales_decor",
        f"rank(0.09*{eps}+0.07*{anl}+0.06*{eps_chg}+0.07*{iv120}+0.10*{f_sales}+0.08*{pcr60}+0.14*group_rank(ts_rank(ts_backfill(cashflow_op,120)/enterprise_value,100),industry)+0.12*ts_rank(ts_backfill(forward_cash_flow_to_price,120),120)+0.08*rank(-1*credit_risk_premium_indicator)+0.08*rank(-1*ts_rank(close/vwap,45))-0.14*ts_rank(returns,110))",
        i16,
        "Cashflow/sales bridge version with lower qMX factor loadings.",
    )
    _add(
        rows,
        "qmx-decor-cashflow-sales-sub16",
        "qmx_cashflow_sales_decor",
        f"rank(0.08*{eps}+0.07*{anl}+0.06*{eps_chg}+0.07*{iv120}+0.10*{f_sales}+0.08*{pcr60}+0.14*group_rank(ts_rank(ts_backfill(cashflow_op,120)/enterprise_value,100),subindustry)+0.12*ts_rank(ts_backfill(forward_cash_flow_to_price,120),120)+0.08*rank(-1*credit_risk_premium_indicator)+0.08*rank(ts_corr(close,volume,60))-0.14*ts_rank(returns,110))",
        sub16,
        "Subindustry cashflow/sales bridge version.",
    )
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
