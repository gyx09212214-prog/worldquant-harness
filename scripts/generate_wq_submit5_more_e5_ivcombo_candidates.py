"""Generate IV-combo repairs around the e5 ACTIVE overlay path."""

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


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "e5_ivcombo_repair_candidates.jsonl"


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
    parser = argparse.ArgumentParser(description="Generate e5 IV-combo repair candidates")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args(argv)


def _records() -> list[dict[str, Any]]:
    d8 = {"neutralization": "SUBINDUSTRY", "decay": 8, "truncation": 0.05}
    d10 = {"neutralization": "SUBINDUSTRY", "decay": 10, "truncation": 0.05}
    d16 = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.03}
    tight = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.02}
    ind = {"neutralization": "INDUSTRY", "decay": 8, "truncation": 0.05}

    rows: list[dict[str, Any]] = []

    def add(tag: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
        rows.append(
            {
                "tag": tag,
                "source_family": "e5_ivcombo_repair",
                "source": "generate_wq_submit5_more_e5_ivcombo_candidates",
                "expression": expr,
                "simulation_settings": settings,
                "mutation_strategy": "active_e5_iv_overlay_plus_orthogonal_micro_overlay",
                "rationale": rationale,
                "risk_flags": ["real_submit_candidate", "requires_online_simulation"],
            }
        )

    iv90 = "group_rank(ts_backfill(implied_volatility_call_90 - implied_volatility_put_90, 60), subindustry)"
    iv60 = "group_rank(ts_backfill(implied_volatility_call_60 - implied_volatility_put_60, 60), subindustry)"

    add(
        "e5-iv90-pcr-ret100-d8",
        f"rank(group_neutralize(0.11 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        f"0.17 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        f"0.12 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        f"0.09 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        f"0.06 * {iv90} + "
        f"0.04 * rank(-1 * ts_rank(pcr_oi_60, 80)) - "
        f"0.09 * ts_rank(high / low, 45) - "
        f"0.12 * ts_rank(returns, 100), industry))",
        d8,
        "Keeps IV for concentration repair while PCR and longer windows move away from O09WZrLg.",
    )
    add(
        "e5-iv90-sntcore-ret100-d8",
        f"rank(group_neutralize(0.11 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        f"0.17 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        f"0.12 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        f"0.09 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        f"0.06 * {iv90} + "
        f"0.04 * ts_rank(snt1_cored1_score, 80) - "
        f"0.09 * ts_rank(high / low, 45) - "
        f"0.12 * ts_rank(returns, 100), industry))",
        d8,
        "Forum score is a small trajectory perturbation on the IV-pass shell.",
    )
    add(
        "e5-iv90-revision-ret100-d10",
        f"rank(group_neutralize(0.11 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        f"0.17 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        f"0.12 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        f"0.09 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        f"0.06 * {iv90} + "
        f"0.04 * zscore(ts_delta(snt1_d1_netearningsrevision, 5)) - "
        f"0.09 * ts_rank(high / low, 45) - "
        f"0.12 * ts_rank(returns, 100), industry))",
        d10,
        "Revision delta adds a second forum perturbation without changing the strong shell too much.",
    )
    add(
        "e5-iv60-pcr-openclose-d8",
        f"rank(group_neutralize(0.11 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        f"0.17 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        f"0.12 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        f"0.09 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        f"0.05 * {iv60} + "
        f"0.04 * rank(-1 * ts_rank(pcr_oi_60, 80)) + "
        f"0.03 * rank((open - close) / (high - low)) - "
        f"0.09 * ts_rank(high / low, 45) - "
        f"0.12 * ts_rank(returns, 100), industry))",
        d8,
        "Switches IV window and adds open-close pressure for extra decorrelation.",
    )
    add(
        "e5-iv90-cfps-vwap-pcr-tight",
        f"rank(group_neutralize(0.11 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        f"0.17 * ts_rank(actual_cashflow_per_share_value_quarterly / vwap, 120) + "
        f"0.12 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        f"0.09 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        f"0.06 * {iv90} + "
        f"0.04 * rank(-1 * ts_rank(pcr_oi_60, 80)) - "
        f"0.09 * ts_rank(high / low, 45) - "
        f"0.12 * ts_rank(returns, 100), industry))",
        tight,
        "Vwap denominator and tight truncation are intended to move self-corr below O09WZrLg.",
    )
    add(
        "e5-iv90-cfps-grouprank-snt-d16",
        f"rank(group_neutralize(0.11 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        f"0.16 * group_rank(ts_rank(actual_cashflow_per_share_value_quarterly / close, 110), subindustry) + "
        f"0.12 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        f"0.09 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        f"0.06 * {iv90} + "
        f"0.04 * ts_rank(snt1_cored1_score, 80) - "
        f"0.09 * ts_rank(high / low, 45) - "
        f"0.12 * ts_rank(returns, 100), industry))",
        d16,
        "Group-ranks the cashflow leg to change holding trajectory while retaining IV concentration repair.",
    )
    add(
        "e5-iv90-spycorr-pcr-ind",
        f"rank(group_neutralize(0.11 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        f"0.17 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        f"0.12 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        f"0.09 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        f"0.05 * {iv90} + "
        f"0.03 * rank(-1 * correlation_last_30_days_spy) + "
        f"0.03 * rank(-1 * ts_rank(pcr_oi_60, 80)) - "
        f"0.09 * ts_rank(high / low, 45) - "
        f"0.12 * ts_rank(returns, 100), industry))",
        ind,
        "Small SPY-corr plus PCR overlays with an industry setting for self-corr reduction.",
    )
    add(
        "e5-iv90-missingness-pcr-d8",
        f"rank(group_neutralize(0.11 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        f"0.17 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        f"0.12 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        f"0.09 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        f"0.05 * {iv90} + "
        f"0.03 * group_rank(ts_rank(ts_count_nans(actual_sales_value_quarterly, 240), 45), industry) + "
        f"0.03 * rank(-1 * ts_rank(pcr_oi_60, 80)) - "
        f"0.09 * ts_rank(high / low, 45) - "
        f"0.12 * ts_rank(returns, 100), industry))",
        d8,
        "Adds a tiny missingness perturbation from the newer active cluster.",
    )

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
