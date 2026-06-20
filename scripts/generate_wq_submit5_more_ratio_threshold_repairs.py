"""Generate near-threshold repairs for the IV-ratio/book/credit branch."""

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


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "ratio_threshold_repair_candidates.jsonl"


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
    parser = argparse.ArgumentParser(description="Generate IV-ratio threshold repair candidates")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=24)
    return parser.parse_args(argv)


def _add(rows: list[dict[str, Any]], tag: str, family: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": family,
            "source": "generate_wq_submit5_more_ratio_threshold_repairs",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "near_self_correlation_threshold_repair",
            "rationale": rationale,
            "risk_flags": [
                "real_submit_candidate",
                "requires_online_simulation",
                "ypaoen2m_threshold_repair",
            ],
        }
    )


def _settings(neut: str, decay: int, trunc: float) -> dict[str, Any]:
    return {"neutralization": neut, "decay": decay, "truncation": trunc, "maxPosition": "ON"}


def _iv_ratio(window: int = 10) -> str:
    return (
        "rank(ts_mean((ts_backfill(implied_volatility_call_120,120)-ts_backfill(implied_volatility_put_120,120))/"
        f"(ts_backfill(implied_volatility_call_120,120)+ts_backfill(implied_volatility_put_120,120)),{window}))"
    )


def _cf_mix(ret_short: int = 40, ret_long: int = 160, window: int = 90) -> str:
    return (
        "rank(ts_rank(ts_backfill(cashflow_op,120)/cap,"
        f"{window})-ts_rank(returns,{ret_short})+0.25*rank(-1*ts_rank(returns,{ret_long})))"
    )


def _lln_proxy() -> str:
    return (
        "rank(0.28*ts_rank(actual_sales_value_quarterly/cap,60)+"
        "0.24*ts_rank(actual_eps_value_quarterly/close,60)+"
        "0.24*ts_rank(change_in_eps_surprise,60)+"
        "0.16*rank(ts_mean(implied_volatility_call_90-implied_volatility_put_90,5))-"
        "0.12*ts_rank(returns,20))"
    )


def _records() -> list[dict[str, Any]]:
    i16_tight = _settings("INDUSTRY", 16, 0.01)
    i20 = _settings("INDUSTRY", 20, 0.01)
    s16 = _settings("SECTOR", 16, 0.012)
    sub16_tight = _settings("SUBINDUSTRY", 16, 0.01)
    sub20 = _settings("SUBINDUSTRY", 20, 0.01)
    rows: list[dict[str, Any]] = []

    _add(
        rows,
        "ratio-thresh-book-credit-analyst-i16",
        "ratio_book_credit_analyst",
        f"rank(0.18*{_iv_ratio(10)}+0.15*{_cf_mix()}+0.16*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.12*rank(-1*credit_risk_premium_indicator)+0.10*ts_rank(snt1_d1_analystcoverage,90)+0.08*rank(-1*correlation_last_30_days_spy)-0.12*ts_rank(returns,140))",
        i16_tight,
        "Push the 0.7315 YPAoeN2M branch below threshold by cutting IV/cashflow and adding analyst coverage.",
    )
    _add(
        rows,
        "ratio-thresh-book-sales-rel-sec16",
        "ratio_book_sales_relationship",
        f"rank(0.16*{_iv_ratio(12)}+0.14*{_cf_mix(50,180,100)}+0.14*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.12*ts_rank(ts_backfill(forward_sales_to_price,120),120)+0.10*ts_rank(rel_ret_cust,140)+0.08*rank(ts_corr(vwap,volume,90))-0.12*ts_rank(returns,160))",
        s16,
        "Sector version with relationship and sales payload to reduce overlap with 58vd1EZJ and ZYodJEk1.",
    )
    _add(
        rows,
        "ratio-thresh-rel-analyst-sub16",
        "ratio_relationship_analyst",
        f"rank(0.16*{_iv_ratio(12)}+0.13*{_cf_mix(50,180,100)}+0.13*ts_rank(rel_ret_cust,140)+0.12*ts_rank(snt1_d1_analystcoverage,90)+0.10*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.08*rank(-1*correlation_last_30_days_spy)-0.12*ts_rank(returns,160))",
        sub16_tight,
        "Blend relationship and analyst coverage as underused map regions while preserving a smaller IV-ratio core.",
    )
    _add(
        rows,
        "ratio-thresh-credit-spy-i20",
        "ratio_credit_market",
        f"rank(0.16*{_iv_ratio(14)}+0.14*{_cf_mix(60,200,100)}+0.14*rank(-1*credit_risk_premium_indicator)+0.12*rank(-1*correlation_last_30_days_spy)+0.10*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.08*rank(ts_corr(vwap,volume,100))-0.12*ts_rank(returns,180))",
        i20,
        "Slow industry variant with credit and market-correlation payload to move away from the qMX/LLn cluster.",
    )
    _add(
        rows,
        "ratio-conc-repair-book-volume-sub20",
        "ratio_concentration_repair",
        f"rank(0.18*{_iv_ratio(10)}+0.14*{_cf_mix()}+0.14*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.12*rank(volume/adv20)+0.10*rank(ts_corr(vwap,volume,100))+0.08*rank(-1*correlation_last_30_days_spy)-0.12*ts_rank(returns,160))",
        sub20,
        "Repair le0PnwnA concentrated weight by lowering truncation and adding volume/correlation dispersion.",
    )
    _add(
        rows,
        "ratio-conc-repair-credit-sector",
        "ratio_concentration_repair",
        f"rank(0.16*{_iv_ratio(14)}+0.13*{_cf_mix(60,200,110)}+0.14*rank(-1*credit_risk_premium_indicator)+0.12*ts_rank(ts_backfill(forward_sales_to_price,120),120)+0.10*ts_rank(snt1_d1_analystcoverage,100)+0.08*rank(ts_corr(vwap,volume,100))-0.12*ts_rank(returns,180))",
        s16,
        "A sector concentration repair that shifts payload from IV ratio to credit, sales and analyst coverage.",
    )
    _add(
        rows,
        "ratio-thresh-missingness-book-i16",
        "ratio_missingness_book",
        f"rank(0.15*{_iv_ratio(12)}+0.13*{_cf_mix(50,180,100)}+0.14*ts_rank(ts_count_nans(actual_sales_value_quarterly,240),60)+0.12*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.10*rank(-1*credit_risk_premium_indicator)+0.08*rank(-1*correlation_last_30_days_spy)-0.12*ts_rank(returns,160))",
        i16_tight,
        "Use reporting missingness as a non-core payload; avoid an explicit LLn proxy to reduce overlap.",
    )
    _add(
        rows,
        "ratio-thresh-openclose-rel-sub16",
        "ratio_openclose_relationship",
        f"rank(0.15*{_iv_ratio(14)}+0.13*{_cf_mix(60,200,110)}+0.14*rank((open-close)/open)+0.12*ts_rank(rel_ret_cust,160)+0.10*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.08*rank(-1*correlation_last_30_days_spy)-0.12*ts_rank(returns,180))",
        sub16_tight,
        "Add open-close and relationship payload to avoid the four-anchor collision seen in YPAoeN2M.",
    )
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
