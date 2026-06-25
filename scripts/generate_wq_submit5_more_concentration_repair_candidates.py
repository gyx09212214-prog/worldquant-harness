"""Generate concentration-repair candidates from the best anchor-orthogonal hits."""

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


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "concentration_repair_candidates.jsonl"


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
    else:
        invalid_path = output.with_suffix(".invalid.jsonl")
        if invalid_path.exists():
            invalid_path.unlink()
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate WQ concentration repair candidates")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=20)
    return parser.parse_args(argv)


def _row(tag: str, family: str, expr: str, settings: dict[str, Any], rationale: str) -> dict[str, Any]:
    return {
        "tag": tag,
        "source_family": family,
        "source": "generate_wq_submit5_more_concentration_repair_candidates",
        "expression": expr,
        "simulation_settings": settings,
        "mutation_strategy": "concentration_repair_after_anchor_orthogonal_hit",
        "rationale": rationale,
        "risk_flags": [
            "real_submit_candidate",
            "requires_online_simulation",
            "concentrated_weight_repair",
            "factor_map_guided",
        ],
    }


def _records() -> list[dict[str, Any]]:
    tight_i = {"neutralization": "INDUSTRY", "decay": 12, "truncation": 0.01}
    tight_s = {"neutralization": "SECTOR", "decay": 12, "truncation": 0.01}
    tight_sub = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.01}
    slow_i = {"neutralization": "INDUSTRY", "decay": 18, "truncation": 0.01}
    slow_s = {"neutralization": "SECTOR", "decay": 18, "truncation": 0.01}

    opt_s = (
        "group_rank(ts_backfill(0.16*ts_rank(change_in_eps_surprise,70)+"
        "0.12*ts_rank(actual_eps_value_quarterly/open,90)-"
        "0.10*ts_rank(pcr_oi_20,90)+"
        "0.10*rank(ts_mean((implied_volatility_call_60-implied_volatility_put_60)/"
        "(implied_volatility_call_60+implied_volatility_put_60),14))-"
        "0.12*ts_rank(returns,50),70),sector)"
    )
    opt_i = opt_s.replace(",sector)", ",industry)")
    opt_sub = opt_s.replace(",sector)", ",subindustry)")

    rows: list[dict[str, Any]] = []
    specs: list[tuple[str, str, str, dict[str, Any], str]] = [
        (
            "conc-repair-rel-sales-grank-slow",
            "options_relationship_sales_concentration_repair",
            f"rank(group_neutralize(0.26*{opt_s}+0.16*group_rank(ts_rank(rel_ret_cust,120),industry)+0.14*group_rank(ts_rank(forward_sales_to_price,100),sector)+0.10*group_rank(ts_rank(actual_sales_value_quarterly/enterprise_value,120),industry)+0.09*rank(-1*credit_risk_premium_indicator)+0.09*rank(-1*correlation_last_30_days_spy)+0.08*ts_rank(-ts_delta(vwap,12)/vwap,50)+0.08*rank(ts_corr(vwap,volume,60))-0.10*ts_rank(returns,110),sector))",
            tight_s,
            "First hit repair: group-rank the relationship and sales legs, then sector-neutralize and tighten truncation.",
        ),
        (
            "conc-repair-rel-sales-grank-ind",
            "options_relationship_sales_concentration_repair",
            f"rank(group_neutralize(0.26*{opt_i}+0.16*group_rank(ts_rank(rel_ret_cust,120),industry)+0.14*group_rank(ts_rank(forward_sales_to_price,100),industry)+0.10*group_rank(ts_rank(actual_sales_value_quarterly/enterprise_value,120),subindustry)+0.09*rank(-1*credit_risk_premium_indicator)+0.08*rank(-1*beta_last_30_days_spy)+0.08*rank(-1*ts_rank(pcr_oi_60,90))+0.08*rank(-1*ts_rank(close/vwap,40))-0.10*ts_rank(returns,110),industry))",
            tight_i,
            "Industry variant of the same concentration repair with PCR60 and close/vwap dispersion.",
        ),
        (
            "conc-repair-rel-sales-sub",
            "options_relationship_sales_concentration_repair",
            f"rank(group_neutralize(0.24*{opt_sub}+0.16*group_rank(ts_rank(rel_ret_cust,140),subindustry)+0.14*group_rank(ts_rank(forward_sales_to_price,120),industry)+0.12*rank(-1*credit_risk_premium_indicator)+0.10*rank(-1*correlation_last_30_days_spy)+0.10*rank(ts_corr(close,volume,60))+0.08*ts_rank(-ts_delta(vwap,15)/vwap,60)-0.10*ts_rank(returns,120),subindustry))",
            tight_sub,
            "Subindustry slower-window variant intended to reduce single-name concentration further.",
        ),
        (
            "conc-repair-rel-sales-no-ev",
            "options_relationship_sales_concentration_repair",
            f"rank(group_neutralize(0.28*{opt_s}+0.18*group_rank(ts_rank(rel_ret_cust,120),industry)+0.16*group_rank(ts_rank(forward_sales_to_price,100),sector)+0.10*ts_rank(snt1_d1_analystcoverage,80)+0.10*rank(-1*credit_risk_premium_indicator)+0.08*rank(-1*correlation_last_30_days_spy)+0.08*rank(ts_corr(vwap,volume,60))-0.10*ts_rank(returns,100),sector))",
            slow_s,
            "Drops the sales/EV leg from the concentrated pair and replaces it with analyst coverage.",
        ),
        (
            "conc-repair-rel-sales-sent",
            "options_relationship_sales_concentration_repair",
            f"rank(group_neutralize(0.24*{opt_i}+0.16*group_rank(ts_rank(rel_ret_cust,120),industry)+0.14*ts_rank(forward_sales_to_price,100)+0.12*zscore(ts_mean(scl12_sentiment_fast_d1,10))+0.10*group_zscore(ts_delta(snt1_d1_netearningsrevision,5),subindustry)+0.08*rank(-1*credit_risk_premium_indicator)+0.08*rank(-1*beta_last_30_days_spy)-0.10*ts_rank(returns,110),industry))",
            slow_i,
            "Adds sentiment/revision to disperse the rel-sales hit without returning to the old options anchor.",
        ),
        (
            "conc-repair-sales-iv-rel",
            "sales_iv_pcr_concentration_repair",
            "rank(group_neutralize(0.20*group_rank(ts_rank(actual_sales_value_quarterly/enterprise_value,120),sector)+0.16*group_rank(ts_rank(forward_sales_to_price,100),industry)+0.14*group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,70),subindustry)+0.12*rank(-1*ts_rank(pcr_oi_60,100))+0.10*group_rank(ts_rank(rel_ret_cust,120),industry)+0.10*ts_rank(-ts_delta(vwap,12)/vwap,50)+0.08*rank(-1*beta_last_30_days_spy)-0.12*ts_rank(returns,110),sector))",
            tight_s,
            "Concentration repair for the sales-IV-PCR near-pass using group-ranked sales and IV legs.",
        ),
        (
            "conc-repair-cert-rel",
            "certainty_relationship_concentration_repair",
            "rank(group_neutralize(0.14*rank(-1*earnings_certainty_rank_derivative)+0.12*group_rank(ts_rank(dividends_to_gross_profit,90),industry)+0.14*group_rank(ts_rank(rel_ret_cust,120),sector)+0.12*group_rank(ts_rank(forward_sales_to_price,100),industry)+0.10*rank(ts_corr(close,volume,60))+0.10*ts_rank(-ts_delta(vwap,15)/vwap,60)+0.08*rank(-1*correlation_last_30_days_spy)-0.12*ts_rank(returns,120),industry))",
            tight_i,
            "Compact certainty/dividend repair with relationship and micro dispersion.",
        ),
        (
            "conc-repair-cf-rel-sales",
            "cashflow_relationship_concentration_repair",
            "rank(group_neutralize(0.14*group_rank(ts_rank(cashflow_op/cap,120),industry)+0.12*group_rank(ts_rank(forward_cash_flow_to_price,140),subindustry)+0.12*group_rank(ts_rank(rel_ret_cust,120),sector)+0.12*group_rank(ts_rank(actual_sales_value_quarterly/enterprise_value,120),industry)+0.10*rank(-1*credit_risk_premium_indicator)+0.10*rank(-1*earnings_certainty_rank_derivative)+0.08*rank(-1*ts_rank(close/vwap,45))-0.12*ts_rank(returns,120),industry))",
            tight_i,
            "Cashflow/relationship repair that avoids the older full cashflow split expression.",
        ),
    ]

    for tag, family, expr, settings, rationale in specs:
        rows.append(_row(tag, family, expr, settings, rationale))
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
