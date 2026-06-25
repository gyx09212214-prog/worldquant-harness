"""Generate hybrid WQ candidates for the submit-5-more continuation.

The records here are intentionally narrow. They combine the latest successful
forum-overlay group-rank trajectory with the newer missingness/dividend/micro
trajectory, while avoiding pure parameter-only mutations of already ACTIVE
alphas.
"""

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


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "hybrid_forum_missingness_candidates.jsonl"


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
    parser = argparse.ArgumentParser(description="Generate submit5-more hybrid forum/missingness candidates")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args(argv)


def _records() -> list[dict[str, Any]]:
    d8 = {"neutralization": "SUBINDUSTRY", "decay": 8, "truncation": 0.05}
    d12 = {"neutralization": "SUBINDUSTRY", "decay": 12, "truncation": 0.03}
    d16 = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.03}
    tight = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.02}
    ind = {"neutralization": "INDUSTRY", "decay": 12, "truncation": 0.03}

    rows: list[dict[str, Any]] = []

    def add(tag: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
        rows.append(
            {
                "tag": tag,
                "source_family": "hybrid_forum_missingness_submit5_more",
                "source": "generate_wq_submit5_more_hybrid_candidates",
                "expression": expr,
                "simulation_settings": settings,
                "mutation_strategy": "forum_overlay_group_rank_x_missingness_dividend_micro",
                "rationale": rationale,
                "risk_flags": ["real_submit_candidate", "requires_online_simulation"],
            }
        )

    add(
        "hyb-miss-div-forum-cvwap-r90-d16",
        "rank(group_neutralize(0.16 * group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 45), industry) + "
        "0.15 * ts_rank(dividends_to_gross_profit, 100) + "
        "0.12 * ts_rank(snt1_cored1_score, 60) + "
        "0.10 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * rank(-1 * ts_rank(close / vwap, 30)) + "
        "0.12 * rank(ts_corr(vwap, volume, 50)) + "
        "0.09 * rank((high - close) / (high - low) * volume / adv20) - "
        "0.16 * ts_rank(returns, 90), industry))",
        d16,
        "Moves the forum-overlay skeleton onto the newer missingness/dividend anchor.",
    )
    add(
        "hyb-miss-div-forum-pcr-r90-d8",
        "rank(group_neutralize(0.15 * group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 45), industry) + "
        "0.15 * ts_rank(dividends_to_gross_profit, 90) + "
        "0.12 * ts_rank(snt1_cored1_score, 60) + "
        "0.10 * rank(-1 * ts_rank(pcr_oi_60, 60)) + "
        "0.10 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * rank(ts_corr(vwap, volume, 60)) + "
        "0.10 * rank(-1 * ts_rank(close / vwap, 30)) - "
        "0.16 * ts_rank(returns, 90), industry))",
        d8,
        "Uses the previously successful PCR overlay as the orthogonal perturbation.",
    )
    add(
        "hyb-group-sales-missing-div-closevwap-d12",
        "rank(group_neutralize(0.15 * group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 45), subindustry) + "
        "0.14 * group_rank(ts_rank(actual_sales_value_quarterly / enterprise_value, 120), industry) + "
        "0.14 * ts_rank(dividends_to_gross_profit, 100) + "
        "0.12 * ts_rank(snt1_cored1_score, 60) + "
        "0.10 * rank(-1 * credit_risk_premium_indicator) + "
        "0.10 * rank(-1 * ts_rank(close / vwap, 30)) + "
        "0.10 * rank(ts_corr(vwap, volume, 50)) - "
        "0.15 * ts_rank(returns, 90), industry))",
        d12,
        "Adds a sales/value bridge without relying on the saturated cashflow-per-share shell.",
    )
    add(
        "hyb-cfop-lite-miss-div-forum-d16",
        "rank(group_neutralize(0.13 * group_rank(ts_rank(cashflow_op / cap, 120), industry) + "
        "0.14 * group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 45), subindustry) + "
        "0.13 * ts_rank(dividends_to_gross_profit, 100) + "
        "0.12 * ts_rank(snt1_cored1_score, 60) + "
        "0.10 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * rank(-1 * ts_rank(close / vwap, 30)) + "
        "0.10 * rank(ts_corr(vwap, volume, 50)) - "
        "0.15 * ts_rank(returns, 100), industry))",
        d16,
        "Keeps cashflow_op only as a smaller anchor while missingness/dividend changes the trajectory.",
    )
    add(
        "hyb-revision-miss-div-micro-d12",
        "rank(group_neutralize(0.15 * group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 45), industry) + "
        "0.14 * ts_rank(dividends_to_gross_profit, 100) + "
        "0.12 * zscore(ts_delta(snt1_d1_netearningsrevision, 5)) + "
        "0.10 * ts_rank(snt1_cored1_score, 60) + "
        "0.10 * rank(-1 * ts_rank(close / vwap, 30)) + "
        "0.11 * rank(ts_corr(vwap, volume, 50)) + "
        "0.10 * rank((high - close) / (high - low) * volume / adv20) - "
        "0.16 * ts_rank(returns, 90), industry))",
        d12,
        "Uses forum revision as a small perturbation rather than a main signal.",
    )
    add(
        "hyb-div-quality-sentiment-tight",
        "rank(group_neutralize(0.16 * ts_rank(dividends_to_gross_profit, 120) + "
        "0.14 * group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 50), industry) + "
        "0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.12 * ts_rank(snt1_cored1_score, 80) + "
        "0.10 * rank(-1 * ts_rank(close / vwap, 40)) + "
        "0.10 * rank(ts_corr(close, volume, 60)) + "
        "0.10 * rank(volume / adv20) - "
        "0.16 * ts_rank(returns, 100), industry))",
        tight,
        "Longer windows and tight truncation aim to reduce overlap with the newly ACTIVE missingness alpha.",
    )
    add(
        "hyb-sales-eps-miss-forum-ind",
        "rank(group_neutralize(0.14 * group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 45), industry) + "
        "0.14 * ts_rank(actual_eps_value_quarterly / enterprise_value, 120) + "
        "0.12 * ts_rank(dividends_to_gross_profit, 100) + "
        "0.12 * ts_rank(snt1_cored1_score, 60) + "
        "0.10 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * rank(-1 * ts_rank(close / vwap, 30)) + "
        "0.10 * rank(ts_corr(vwap, volume, 50)) - "
        "0.16 * ts_rank(returns, 90), subindustry))",
        ind,
        "Tests EPS value as a non-cashflow fundamental bridge with industry setting.",
    )
    add(
        "hyb-miss-div-ivtiny-cvwap-d8",
        "rank(group_neutralize(0.15 * group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 45), industry) + "
        "0.15 * ts_rank(dividends_to_gross_profit, 100) + "
        "0.12 * ts_rank(snt1_cored1_score, 60) + "
        "0.08 * rank(ts_mean((implied_volatility_call_60 - implied_volatility_put_60) / (implied_volatility_call_60 + implied_volatility_put_60), 20)) + "
        "0.10 * rank(-1 * ts_rank(close / vwap, 30)) + "
        "0.11 * rank(ts_corr(vwap, volume, 50)) + "
        "0.09 * rank(volume / adv20) - "
        "0.16 * ts_rank(returns, 90), industry))",
        d8,
        "Keeps IV as a tiny overlay; previous pure IV was concentrated, but small IV overlays can decorrelate.",
    )
    add(
        "hyb-miss-div-openclose-r100-d16",
        "rank(group_neutralize(0.16 * group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 50), industry) + "
        "0.15 * ts_rank(dividends_to_gross_profit, 120) + "
        "0.12 * ts_rank(snt1_cored1_score, 60) + "
        "0.10 * rank((open - close) / (high - low)) + "
        "0.10 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * rank(ts_corr(vwap, volume, 60)) + "
        "0.09 * rank(-1 * ts_rank(close / vwap, 40)) - "
        "0.16 * ts_rank(returns, 100), industry))",
        d16,
        "Uses open-close pressure and longer windows to avoid direct overlap with the accepted formula.",
    )
    add(
        "hyb-miss-div-spycorr-sent-d12",
        "rank(group_neutralize(0.15 * group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 45), subindustry) + "
        "0.15 * ts_rank(dividends_to_gross_profit, 100) + "
        "0.12 * ts_rank(snt1_cored1_score, 60) + "
        "0.10 * rank(-1 * correlation_last_30_days_spy) + "
        "0.10 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.10 * rank(ts_corr(vwap, volume, 50)) + "
        "0.10 * rank(-1 * ts_rank(close / vwap, 30)) - "
        "0.16 * ts_rank(returns, 90), industry))",
        d12,
        "Market-correlation overlay is intended to decorrelate from the cashflow/value active island.",
    )

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
