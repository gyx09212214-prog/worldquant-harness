"""Generate concentration repairs for the strong IV-ratio/missingness near misses."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_candidate_generation import run_static_candidate_generator

DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "ratio_concentration_repair_candidates.jsonl"


def main(argv: list[str] | None = None) -> int:
    return run_static_candidate_generator(
        argv,
        records_func=_records,
        default_output=DEFAULT_OUTPUT,
        default_limit=24,
        description='Generate ratio concentration repair candidates',
        limit_valid_count=False,
    )


def _add(rows: list[dict[str, Any]], tag: str, family: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": family,
            "source": "generate_wq_submit5_more_ratio_concentration_repairs",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "concentrated_weight_repair",
            "rationale": rationale,
            "risk_flags": [
                "real_submit_candidate",
                "requires_online_simulation",
                "ratio_missingness_concentration_repair",
            ],
        }
    )


def _settings(neut: str, decay: int, trunc: float) -> dict[str, Any]:
    return {"neutralization": neut, "decay": decay, "truncation": trunc, "maxPosition": "ON"}


def _iv_ratio(window: int = 12) -> str:
    return (
        "rank(ts_mean((ts_backfill(implied_volatility_call_120,120)-ts_backfill(implied_volatility_put_120,120))/"
        f"(ts_backfill(implied_volatility_call_120,120)+ts_backfill(implied_volatility_put_120,120)),{window}))"
    )


def _cf(ret_short: int = 50, ret_long: int = 180, window: int = 100) -> str:
    return (
        "rank(ts_rank(ts_backfill(cashflow_op,120)/cap,"
        f"{window})-ts_rank(returns,{ret_short})+0.25*rank(-1*ts_rank(returns,{ret_long})))"
    )


def _base_missingness() -> str:
    return (
        f"0.15*{_iv_ratio(12)}+"
        f"0.13*{_cf()}+"
        "0.14*ts_rank(ts_count_nans(actual_sales_value_quarterly,240),60)+"
        "0.12*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+"
        "0.10*rank(-1*credit_risk_premium_indicator)+"
        "0.08*rank(-1*correlation_last_30_days_spy)-"
        "0.12*ts_rank(returns,160)"
    )


def _base_openclose() -> str:
    return (
        f"0.15*{_iv_ratio(14)}+"
        f"0.13*{_cf(60,200,110)}+"
        "0.14*rank((open-close)/open)+"
        "0.12*ts_rank(rel_ret_cust,160)+"
        "0.10*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+"
        "0.08*rank(-1*correlation_last_30_days_spy)-"
        "0.12*ts_rank(returns,180)"
    )


def _base_book_volume() -> str:
    return (
        f"0.18*{_iv_ratio(10)}+"
        f"0.14*{_cf(40,160,90)}+"
        "0.14*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+"
        "0.12*rank(volume/adv20)+"
        "0.10*rank(ts_corr(vwap,volume,100))+"
        "0.08*rank(-1*correlation_last_30_days_spy)-"
        "0.12*ts_rank(returns,160)"
    )


def _records() -> list[dict[str, Any]]:
    i12 = _settings("INDUSTRY", 12, 0.01)
    i16 = _settings("INDUSTRY", 16, 0.01)
    sub12 = _settings("SUBINDUSTRY", 12, 0.01)
    sub16 = _settings("SUBINDUSTRY", 16, 0.01)
    sec12 = _settings("SECTOR", 12, 0.01)
    rows: list[dict[str, Any]] = []

    missing = _base_missingness()
    openclose = _base_openclose()
    book_volume = _base_book_volume()

    _add(
        rows,
        "ratio-conc-missing-neutral-sub",
        "missingness_concentration_repair",
        f"rank(ts_decay_linear(group_neutralize({missing}, subindustry), 5))",
        sub12,
        "Smooth and subindustry-neutralize the high-IS JjdA7aqW missingness branch.",
    )
    _add(
        rows,
        "ratio-conc-missing-grank-ind",
        "missingness_concentration_repair",
        f"rank(group_rank(ts_decay_linear(group_neutralize({missing}, industry), 7), industry))",
        i12,
        "Use industry group-rank after smoothing to reduce peak stock weights.",
    )
    _add(
        rows,
        "ratio-conc-missing-winsor-ind",
        "missingness_concentration_repair",
        f"rank(winsorize(group_neutralize({missing}, industry), 0.02))",
        i16,
        "Winsorize the neutralized missingness branch while preserving its strong IS profile.",
    )
    _add(
        rows,
        "ratio-conc-openclose-neutral-sub",
        "openclose_concentration_repair",
        f"rank(ts_decay_linear(group_neutralize({openclose}, subindustry), 5))",
        sub12,
        "Smooth and neutralize the high-turnover rKWq5va9 open-close branch.",
    )
    _add(
        rows,
        "ratio-conc-openclose-grank-sec",
        "openclose_concentration_repair",
        f"rank(group_rank(ts_decay_linear(group_neutralize({openclose}, sector), 7), sector))",
        sec12,
        "Sector group-rank version of the open-close/relationship branch.",
    )
    _add(
        rows,
        "ratio-conc-book-volume-neutral-sub",
        "book_volume_concentration_repair",
        f"rank(ts_decay_linear(group_neutralize({book_volume}, subindustry), 5))",
        sub12,
        "Smooth the bl9mjVbm book-volume branch that had good IS but concentrated weight.",
    )
    _add(
        rows,
        "ratio-conc-missing-granked-legs",
        "missingness_concentration_repair",
        f"rank(0.13*{_iv_ratio(12)}+0.12*{_cf()}+0.12*group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly,240),60),industry)+0.11*group_rank(ts_rank(ts_backfill(forward_book_value_to_price,120),120),industry)+0.10*rank(-1*credit_risk_premium_indicator)+0.10*rank(ts_corr(vwap,volume,100))-0.12*ts_rank(returns,160))",
        i16,
        "Replace the two most concentrated legs with group-ranked component legs.",
    )
    _add(
        rows,
        "ratio-conc-openclose-granked-legs",
        "openclose_concentration_repair",
        f"rank(0.13*{_iv_ratio(14)}+0.12*{_cf(60,200,110)}+0.12*group_rank(rank((open-close)/open),subindustry)+0.10*group_rank(ts_rank(rel_ret_cust,160),subindustry)+0.10*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.10*rank(ts_corr(vwap,volume,100))-0.12*ts_rank(returns,180))",
        sub16,
        "Group-rank the open-close and relationship legs to reduce single-name peaks.",
    )
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
