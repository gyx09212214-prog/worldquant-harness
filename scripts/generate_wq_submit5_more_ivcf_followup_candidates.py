"""Generate follow-up IV/cashflow residual repairs after the 58vd1EZJ ACTIVE hit."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_candidate_generation import lln_proxy_expression as _lln_proxy
from worldquant_harness.wq_candidate_generation import run_static_candidate_generator

DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "ivcf_followup_candidates.jsonl"


def main(argv: list[str] | None = None) -> int:
    return run_static_candidate_generator(
        argv,
        records_func=_records,
        default_output=DEFAULT_OUTPUT,
        default_limit=24,
        description='Generate IV/cashflow follow-up candidates',
        limit_valid_count=False,
    )


def _add(rows: list[dict[str, Any]], tag: str, family: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": family,
            "source": "generate_wq_submit5_more_ivcf_followup_candidates",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "post_active_ivcf_residual_followup",
            "rationale": rationale,
            "risk_flags": ["real_submit_candidate", "requires_online_simulation", "post_58vd1EZJ_followup"],
        }
    )


def _settings(neut: str, decay: int, trunc: float) -> dict[str, Any]:
    return {"neutralization": neut, "decay": decay, "truncation": trunc, "maxPosition": "ON"}


def _iv(group: str = "subindustry", backfill: int = 120) -> str:
    return f"group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,{backfill}),{group})"


def _iv_ratio(window: int = 10) -> str:
    return (
        "rank(ts_mean((ts_backfill(implied_volatility_call_120,120)-ts_backfill(implied_volatility_put_120,120))/"
        f"(ts_backfill(implied_volatility_call_120,120)+ts_backfill(implied_volatility_put_120,120)),{window}))"
    )


def _cf_mix(ret_short: int = 30, ret_long: int = 120) -> str:
    return (
        "rank(ts_rank(ts_backfill(cashflow_op,120)/cap,80)-"
        f"ts_rank(returns,{ret_short})+0.30*rank(-1*ts_rank(returns,{ret_long})))"
    )


def _omyo_proxy() -> str:
    return (
        "rank(0.38*group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,60),subindustry)+"
        "0.42*rank((open-close)/open)+"
        "0.20*rank(ts_corr(vwap,volume,40)))"
    )


def _records() -> list[dict[str, Any]]:
    sub12 = _settings("SUBINDUSTRY", 12, 0.02)
    sub16 = _settings("SUBINDUSTRY", 16, 0.015)
    i12 = _settings("INDUSTRY", 12, 0.02)
    i16 = _settings("INDUSTRY", 16, 0.015)
    s12 = _settings("SECTOR", 12, 0.02)
    rows: list[dict[str, Any]] = []

    _add(
        rows,
        "ivcf-follow-lite-lln-sub16",
        "ivcf_lln_omyo_lite",
        f"rank(0.30*{_iv()}+0.22*{_cf_mix()}+0.10*rank(volume/adv20)+0.08*rank(ts_corr(vwap,volume,60))-0.12*ts_rank(returns,90)-0.08*{_lln_proxy()})",
        sub16,
        "Start from 58vd1EZJ but keep only the LLn residual and slower/tighter settings.",
    )
    _add(
        rows,
        "ivcf-follow-book-sub12",
        "ivcf_lln_book",
        f"rank(0.26*{_iv()}+0.20*{_cf_mix()}+0.10*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.08*rank(ts_corr(vwap,volume,80))-0.12*ts_rank(returns,100)-0.06*{_lln_proxy()})",
        sub12,
        "Add book value and a lighter LLn residual to separate from 58vd1EZJ.",
    )
    _add(
        rows,
        "ivcf-follow-credit-i12",
        "ivcf_lln_credit",
        f"rank(0.26*{_iv('industry')}+0.20*{_cf_mix()}+0.10*rank(-1*credit_risk_premium_indicator)+0.08*rank(ts_corr(vwap,volume,80))-0.12*ts_rank(returns,100)-0.06*{_lln_proxy()})",
        i12,
        "Industry variant with credit-risk payload to reduce overlap with the new active.",
    )
    _add(
        rows,
        "ivcf-follow-ratio-sub12",
        "ivcf_ratio_lln",
        f"rank(0.26*{_iv_ratio(10)}+0.22*{_cf_mix()}+0.10*rank(volume/adv20)+0.08*rank(-1*correlation_last_30_days_spy)-0.12*ts_rank(returns,100)-0.06*{_lln_proxy()})",
        sub12,
        "Replace raw IV120 with normalized IV ratio to reduce concentration while keeping LLn residual.",
    )
    _add(
        rows,
        "ivcf-follow-ratio-book-i16",
        "ivcf_ratio_book",
        f"rank(0.22*{_iv_ratio(10)}+0.20*{_cf_mix(40,140)}+0.12*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.08*rank(-1*credit_risk_premium_indicator)-0.12*ts_rank(returns,120)-0.06*{_lln_proxy()})",
        i16,
        "Normalized IV plus book/credit bridge, aimed at avoiding both concentration and new active self-corr.",
    )
    _add(
        rows,
        "ivcf-follow-omyo-sub12",
        "ivcf_omyo_concentration_repair",
        f"rank(0.28*{_iv()}+0.20*{_cf_mix()}+0.10*rank(volume/adv20)+0.08*rank(ts_corr(vwap,volume,60))-0.12*ts_rank(returns,90)-0.08*{_omyo_proxy()})",
        sub12,
        "Repair the high-IS XgKAxZem concentration with a compact omYo residual.",
    )
    _add(
        rows,
        "ivcf-follow-omyo-i16",
        "ivcf_omyo_concentration_repair",
        f"rank(0.26*{_iv('industry')}+0.20*{_cf_mix(40,140)}+0.10*rank(volume/adv20)+0.08*rank(-1*correlation_last_30_days_spy)-0.12*ts_rank(returns,120)-0.08*{_omyo_proxy()})",
        i16,
        "Slower industry version of the XgKAxZem concentration repair.",
    )
    _add(
        rows,
        "ivcf-follow-sector-book",
        "ivcf_sector_book",
        f"rank(0.24*{_iv('industry')}+0.20*{_cf_mix(40,140)}+0.12*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.08*rank(ts_corr(vwap,volume,80))-0.12*ts_rank(returns,120)-0.06*{_lln_proxy()})",
        s12,
        "Sector setting with book and market-correlation payload.",
    )
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
