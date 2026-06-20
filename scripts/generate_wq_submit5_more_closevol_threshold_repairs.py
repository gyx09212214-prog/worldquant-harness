"""Generate close-volume near-threshold self-correlation repairs."""

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


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "closevol_threshold_repair_candidates.jsonl"


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
    parser = argparse.ArgumentParser(description="Generate close-volume near-threshold repairs")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=12)
    return parser.parse_args(argv)


def _settings(neut: str, decay: int, trunc: float) -> dict[str, Any]:
    return {"neutralization": neut, "decay": decay, "truncation": trunc, "maxPosition": "ON"}


def _add(rows: list[dict[str, Any]], tag: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": "closevol_near_selfcorr_threshold",
            "source": "generate_wq_submit5_more_closevol_threshold_repairs",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "settings_and_micro_weight_repair",
            "rationale": rationale,
            "risk_flags": ["real_submit_candidate", "requires_online_simulation", "near_self_corr_threshold"],
        }
    )


def _iv(window: int) -> str:
    return (
        "rank(ts_mean((ts_backfill(implied_volatility_call_120,120)-ts_backfill(implied_volatility_put_120,120))/"
        f"(ts_backfill(implied_volatility_call_120,120)+ts_backfill(implied_volatility_put_120,120)),{window}))"
    )


def _cf(ret_short: int, ret_long: int, window: int, rev: float = 0.14) -> str:
    return (
        "rank(ts_rank(ts_backfill(cashflow_op,120)/cap,"
        f"{window})-ts_rank(returns,{ret_short})+{rev:.2f}*rank(-1*ts_rank(returns,{ret_long})))"
    )


def _fwd_cf(ret_short: int, window: int) -> str:
    return f"rank(ts_rank(ts_backfill(forward_cash_flow_to_price,120),{window})-ts_rank(returns,{ret_short}))"


def _records() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cap_bucket = 'bucket(rank(cap),range="0.1,1,0.1")'
    adv_bucket = 'bucket(rank(adv20),range="0.1,1,0.1")'

    book_s = "group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),sector)"
    sales_s = "group_rank(ts_rank(ts_backfill(forward_sales_to_price,120),120),sector)"
    rel = "ts_rank(rel_ret_cust,140)"
    pcr90 = "rank(-1*ts_rank(ts_backfill(pcr_oi_60,120),90))"
    pcr120 = "rank(-1*ts_rank(ts_backfill(pcr_oi_60,120),120))"
    closevol = "rank(ts_corr(close,volume,120))"
    closevol160 = "rank(ts_corr(close,volume,160))"
    cov = "ts_rank(snt1_d1_analystcoverage,100)"
    miss_i = "group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly,240),60),industry)"
    spy_corr = "rank(-1*correlation_last_30_days_spy)"

    base = (
        f"0.09*{_iv(16)}+0.10*{_cf(70,220,120,0.16)}+0.09*{book_s}+0.15*{sales_s}+"
        f"0.11*{rel}+0.05*{pcr90}+0.03*{closevol}-0.11*ts_rank(returns,170)"
    )

    _add(
        rows,
        "closevol-same-subindustry-d16",
        f"rank(group_rank({base},{cap_bucket}))",
        _settings("SUBINDUSTRY", 16, 0.012),
        "Keep the 0.7319-sc expression but change neutralization from sector to subindustry.",
    )
    _add(
        rows,
        "closevol-same-sector-d20-t008",
        f"rank(group_rank({base},{cap_bucket}))",
        _settings("SECTOR", 20, 0.008),
        "Slightly smoother/slimmer settings to reduce the RRNx77nj PnL overlap.",
    )
    _add(
        rows,
        "closevol-same-industry-d18-t008",
        f"rank(group_rank({base},{cap_bucket}))",
        _settings("INDUSTRY", 18, 0.008),
        "Industry neutralization settings variant of the near-threshold expression.",
    )

    less_rrn = (
        f"0.08*{_iv(18)}+0.08*{_cf(75,230,125,0.12)}+0.08*{book_s}+0.16*{sales_s}+"
        f"0.12*{rel}+0.06*{pcr120}+0.02*{closevol160}+0.03*{cov}-0.09*ts_rank(returns,180)"
    )
    _add(
        rows,
        "closevol-less-rrn-salescov-sec18",
        f"rank(group_rank({less_rrn},{cap_bucket}))",
        _settings("SECTOR", 18, 0.01),
        "Lower cashflow/reversal/volume-corr weights and add a small coverage leg.",
    )

    fwd = (
        f"0.08*{_iv(18)}+0.08*{_fwd_cf(85,120)}+0.08*{book_s}+0.16*{sales_s}+"
        f"0.12*{rel}+0.06*{pcr90}+0.02*{closevol160}+0.03*{spy_corr}-0.09*ts_rank(returns,180)"
    )
    _add(
        rows,
        "closevol-forwardcf-spy-sec18",
        f"rank(group_rank({fwd},{cap_bucket}))",
        _settings("SECTOR", 18, 0.01),
        "Replace cashflow_op/cap with forward cash-flow-to-price and add SPY-corr orthogonalization.",
    )

    missing = (
        f"0.08*{_iv(18)}+0.08*{_cf(80,240,130,0.12)}+0.07*{book_s}+0.15*{sales_s}+"
        f"0.11*{rel}+0.05*{pcr120}+0.02*{closevol160}+0.06*{miss_i}-0.09*ts_rank(returns,180)"
    )
    _add(
        rows,
        "closevol-missing-blend-ind18",
        f"rank(group_rank({missing},{cap_bucket}))",
        _settings("INDUSTRY", 18, 0.008),
        "Blend in missingness to move away from the RRNx technical anchor.",
    )

    _add(
        rows,
        "closevol-adv-bucket-sec16",
        f"rank(group_rank({base},{adv_bucket}))",
        _settings("SECTOR", 16, 0.012),
        "Use liquidity bucket instead of cap bucket to perturb weights while keeping concentration control.",
    )
    _add(
        rows,
        "closevol-cap-then-adv-sec16",
        f"rank(group_rank(group_rank({base},{cap_bucket}),{adv_bucket}))",
        _settings("SECTOR", 16, 0.012),
        "Nested cap then liquidity bucket ranking to lower self-corr with minimal payload change.",
    )
    _add(
        rows,
        "closevol-hump-cap-sec20",
        f"rank(group_rank(hump({base}),{cap_bucket}))",
        _settings("SECTOR", 20, 0.008),
        "Hump the core before cap-bucket ranking to damp the RRNx high-turnover anchor.",
    )
    _add(
        rows,
        "closevol-lowturn-cap-ind20",
        f"rank(group_rank({less_rrn},{cap_bucket}))",
        _settings("INDUSTRY", 20, 0.006),
        "Lower truncation plus industry neutralization for the less-RRN payload.",
    )

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
