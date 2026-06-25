"""Generate second-order repair candidates for the strong e5 near-pass family."""

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


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "e5_second_order_repair_candidates.jsonl"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output = Path(args.output)
    rows: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _records():
        key = row["expression"] + "||" + json.dumps(row.get("simulation_settings") or {}, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        try:
            validate_wq_expression(row["expression"])
        except Exception as exc:
            invalid.append({**row, "validation_error": str(exc)})
            continue
        rows.append(row)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows) + "\n", encoding="utf-8")
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
    parser = argparse.ArgumentParser(description="Generate e5 second-order repair candidates")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args(argv)


def _records() -> list[dict[str, Any]]:
    d8 = {"neutralization": "SUBINDUSTRY", "decay": 8, "truncation": 0.05}
    d10 = {"neutralization": "SUBINDUSTRY", "decay": 10, "truncation": 0.05}
    d16 = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.03}
    tight = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.02}
    ind = {"neutralization": "INDUSTRY", "decay": 8, "truncation": 0.05}
    maxpos = {"neutralization": "SUBINDUSTRY", "decay": 8, "truncation": 0.03, "maxPosition": "ON"}

    rows: list[dict[str, Any]] = []

    def add(tag: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
        rows.append(
            {
                "tag": tag,
                "source_family": "e5_second_order_repair",
                "source": "generate_wq_submit5_more_e5_repair_candidates",
                "expression": expr,
                "simulation_settings": settings,
                "mutation_strategy": "strong_e5_nearpass_with_orthogonal_overlay",
                "rationale": rationale,
                "risk_flags": ["real_submit_candidate", "requires_online_simulation"],
            }
        )

    add(
        "e5-pcr-overlay-ret90-d8",
        "rank(group_neutralize(0.12 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + "
        "0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 90) + "
        "0.12 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * rank(-1 * ts_decay_linear(close / vwap, 5)) + "
        "0.08 * rank(-1 * ts_rank(pcr_oi_60, 60)) - "
        "0.10 * ts_rank(high / low, 40) - "
        "0.12 * ts_rank(returns, 90), industry))",
        d8,
        "PCR replaces the IV overlay as the low-correlation options perturbation.",
    )
    add(
        "e5-pcr-overlay-tight-d16",
        "rank(group_neutralize(0.12 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + "
        "0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        "0.12 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        "0.08 * rank(-1 * ts_rank(pcr_oi_60, 80)) - "
        "0.10 * ts_rank(high / low, 40) - "
        "0.12 * ts_rank(returns, 100), industry))",
        tight,
        "Longer windows and tight truncation target the 0.71-0.72 self-corr band.",
    )
    add(
        "e5-spycorr-maxpos-t003",
        "rank(group_neutralize(0.12 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + "
        "0.20 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 90) + "
        "0.12 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.14 * rank(-1 * ts_decay_linear(close / vwap, 5)) + "
        "0.08 * rank(-1 * correlation_last_30_days_spy) - "
        "0.10 * ts_rank(high / low, 40) - "
        "0.12 * ts_rank(returns, 80), industry))",
        maxpos,
        "Retests the high-metric SPY-corr overlay with lower truncation and maxPosition enabled.",
    )
    add(
        "e5-sntcore-overlay-ret90-d8",
        "rank(group_neutralize(0.12 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        "0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        "0.12 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        "0.08 * ts_rank(snt1_cored1_score, 60) - "
        "0.10 * ts_rank(high / low, 40) - "
        "0.12 * ts_rank(returns, 90), industry))",
        d8,
        "Forum sentiment is a small overlay rather than the main signal.",
    )
    add(
        "e5-revision-overlay-d10",
        "rank(group_neutralize(0.12 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        "0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        "0.12 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        "0.08 * zscore(ts_delta(snt1_d1_netearningsrevision, 5)) - "
        "0.10 * ts_rank(high / low, 40) - "
        "0.12 * ts_rank(returns, 90), industry))",
        d10,
        "Uses revision delta as a different forum perturbation from the accepted IV overlay.",
    )
    add(
        "e5-missingness-lite-d8",
        "rank(group_neutralize(0.12 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        "0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        "0.12 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        "0.08 * group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 45), industry) - "
        "0.10 * ts_rank(high / low, 40) - "
        "0.12 * ts_rank(returns, 90), industry))",
        d8,
        "Adds a tiny missingness leg from the new ACTIVE family.",
    )
    add(
        "e5-openclose-volumepressure-d8",
        "rank(group_neutralize(0.12 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        "0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        "0.12 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        "0.04 * rank((open - close) / (high - low)) + "
        "0.04 * rank((high - close) / (high - low) * volume / adv20) - "
        "0.10 * ts_rank(high / low, 40) - "
        "0.12 * ts_rank(returns, 90), industry))",
        d8,
        "Splits the perturbation into open-close and high-close pressure.",
    )
    add(
        "e5-beta-pcr-mix-ind",
        "rank(group_neutralize(0.12 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        "0.17 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        "0.12 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        "0.04 * rank(-1 * beta_last_30_days_spy) + "
        "0.04 * rank(-1 * ts_rank(pcr_oi_60, 80)) - "
        "0.10 * ts_rank(high / low, 40) - "
        "0.12 * ts_rank(returns, 100), industry))",
        ind,
        "Uses two small orthogonal risk/option overlays and changes outer setting.",
    )
    add(
        "e5-cfps-vwap-pcr-ret100-d16",
        "rank(group_neutralize(0.12 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        "0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / vwap, 120) + "
        "0.12 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        "0.08 * rank(-1 * ts_rank(pcr_oi_60, 80)) - "
        "0.10 * ts_rank(high / low, 45) - "
        "0.12 * ts_rank(returns, 100), industry))",
        d16,
        "Combines the vwap denominator near-pass with PCR and longer windows.",
    )
    add(
        "e5-cfps-grouprank-snt-tight",
        "rank(group_neutralize(0.12 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        "0.17 * group_rank(ts_rank(actual_cashflow_per_share_value_quarterly / close, 110), subindustry) + "
        "0.12 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        "0.08 * ts_rank(snt1_cored1_score, 80) - "
        "0.10 * ts_rank(high / low, 45) - "
        "0.12 * ts_rank(returns, 100), industry))",
        tight,
        "Group-ranks the cashflow leg to change the trajectory while keeping the strong shell.",
    )

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
