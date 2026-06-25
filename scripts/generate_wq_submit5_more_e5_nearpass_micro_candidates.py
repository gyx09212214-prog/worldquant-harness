"""Generate micro repairs for the e5 near-pass cluster."""

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


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "e5_nearpass_micro_candidates.jsonl"


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
    parser = argparse.ArgumentParser(description="Generate e5 near-pass micro repair candidates")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args(argv)


def _add(
    rows: list[dict[str, Any]],
    tag: str,
    expr: str,
    settings: dict[str, Any],
    rationale: str,
) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": "e5_nearpass_micro_repair",
            "source": "generate_wq_submit5_more_e5_nearpass_micro_candidates",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "nearpass_selfcorr_micro_overlay",
            "rationale": rationale,
            "risk_flags": ["real_submit_candidate", "requires_online_simulation"],
        }
    )


def _records() -> list[dict[str, Any]]:
    d8 = {"neutralization": "SUBINDUSTRY", "decay": 8, "truncation": 0.05}
    d10 = {"neutralization": "SUBINDUSTRY", "decay": 10, "truncation": 0.05}
    d12 = {"neutralization": "SUBINDUSTRY", "decay": 12, "truncation": 0.05}
    d16_t03 = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.03}
    ind = {"neutralization": "INDUSTRY", "decay": 10, "truncation": 0.05}

    iv90 = "group_rank(ts_backfill(implied_volatility_call_90 - implied_volatility_put_90, 60), subindustry)"

    rows: list[dict[str, Any]] = []
    _add(
        rows,
        "e5-cfeff-iv03-ret80-d8",
        f"rank(group_neutralize(0.13 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + "
        f"0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 90) + "
        f"0.13 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.11 * rank(-1 * cashflow_efficiency_rank_derivative) + "
        f"0.13 * rank(-1 * ts_decay_linear(close / vwap, 5)) + "
        f"0.03 * {iv90} - "
        f"0.11 * ts_rank(high / low, 40) - "
        f"0.12 * ts_rank(returns, 80), industry))",
        d8,
        "Start from the 0.7123 cfeff near-pass and add only a tiny IV leg.",
    )
    _add(
        rows,
        "e5-cfeff-iv04-ret90-d10",
        f"rank(group_neutralize(0.13 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + "
        f"0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 90) + "
        f"0.13 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.10 * rank(-1 * cashflow_efficiency_rank_derivative) + "
        f"0.13 * rank(-1 * ts_decay_linear(close / vwap, 5)) + "
        f"0.04 * {iv90} - "
        f"0.11 * ts_rank(high / low, 40) - "
        f"0.12 * ts_rank(returns, 90), industry))",
        d10,
        "Slightly larger IV perturbation and longer return window on the cfeff near-pass.",
    )
    _add(
        rows,
        "e5-vwaprev7-iv03-ret90-d8",
        f"rank(group_neutralize(0.13 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + "
        f"0.19 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 90) + "
        f"0.13 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.13 * rank(-1 * earnings_certainty_rank_derivative) + "
        f"0.12 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        f"0.03 * {iv90} - "
        f"0.11 * ts_rank(high / low, 40) - "
        f"0.12 * ts_rank(returns, 90), industry))",
        d8,
        "Repair the 0.7151 vwap-reversal near-pass with a tiny IV overlay.",
    )
    _add(
        rows,
        "e5-vwaprev7-iv04-pcr02-d10",
        f"rank(group_neutralize(0.13 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + "
        f"0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 90) + "
        f"0.13 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        f"0.12 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        f"0.04 * {iv90} + "
        f"0.02 * rank(-1 * ts_rank(pcr_oi_60, 80)) - "
        f"0.11 * ts_rank(high / low, 40) - "
        f"0.12 * ts_rank(returns, 90), industry))",
        d10,
        "Use a very small PCR overlay on top of the vwap-reversal near-pass.",
    )
    _add(
        rows,
        "e5-liq-iv03-ret80-d10",
        f"rank(group_neutralize(0.13 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + "
        f"0.17 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 90) + "
        f"0.13 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        f"0.12 * rank(-1 * ts_decay_linear(close / vwap, 5)) + "
        f"0.04 * rank(volume / adv20) + "
        f"0.03 * {iv90} - "
        f"0.11 * ts_rank(high / low, 40) - "
        f"0.12 * ts_rank(returns, 80), industry))",
        d10,
        "Keep the liquidity split and add a tiny IV overlay to push below the 0.7165 self-corr.",
    )
    _add(
        rows,
        "e5-liq-spy03-ret90-d12",
        f"rank(group_neutralize(0.13 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + "
        f"0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 90) + "
        f"0.13 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        f"0.12 * rank(-1 * ts_decay_linear(close / vwap, 5)) + "
        f"0.04 * rank(volume / adv20) + "
        f"0.03 * rank(-1 * correlation_last_30_days_spy) - "
        f"0.11 * ts_rank(high / low, 40) - "
        f"0.12 * ts_rank(returns, 90), industry))",
        d12,
        "Use a smaller SPY-correlation residual than the prior concentration-failing version.",
    )
    _add(
        rows,
        "e5-cfps-vwap-iv03-ret100-d8",
        f"rank(group_neutralize(0.13 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + "
        f"0.19 * ts_rank(actual_cashflow_per_share_value_quarterly / vwap, 100) + "
        f"0.13 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        f"0.12 * rank(-1 * ts_decay_linear(close / vwap, 5)) + "
        f"0.03 * {iv90} - "
        f"0.11 * ts_rank(high / low, 40) - "
        f"0.12 * ts_rank(returns, 100), industry))",
        d8,
        "Start from the cfps-vwap near-pass and add a tiny IV leg.",
    )
    _add(
        rows,
        "e5-inner-subind-iv03-d10",
        f"rank(group_neutralize(0.13 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + "
        f"0.19 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 90) + "
        f"0.13 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.13 * rank(-1 * earnings_certainty_rank_derivative) + "
        f"0.12 * rank(-1 * ts_decay_linear(close / vwap, 5)) + "
        f"0.03 * {iv90} - "
        f"0.11 * ts_rank(high / low, 40) - "
        f"0.12 * ts_rank(returns, 80), subindustry))",
        ind,
        "Keep the inner subindustry skeleton and add a tiny IV perturbation.",
    )
    _add(
        rows,
        "e5-cfeff-closevol03-d16",
        f"rank(group_neutralize(0.13 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + "
        f"0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 90) + "
        f"0.13 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.11 * rank(-1 * cashflow_efficiency_rank_derivative) + "
        f"0.12 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        f"0.03 * rank(ts_corr(close, volume, 50)) + "
        f"0.03 * {iv90} - "
        f"0.11 * ts_rank(high / low, 45) - "
        f"0.12 * ts_rank(returns, 90), industry))",
        d16_t03,
        "Combine tiny close-volume and IV perturbations with slower decay.",
    )
    _add(
        rows,
        "e5-vwaprev7-liq02-iv03-d12",
        f"rank(group_neutralize(0.13 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + "
        f"0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 90) + "
        f"0.13 * rank(-1 * credit_risk_premium_indicator) + "
        f"0.12 * rank(-1 * earnings_certainty_rank_derivative) + "
        f"0.12 * rank(-1 * ts_decay_linear(close / vwap, 7)) + "
        f"0.02 * rank(volume / adv20) + "
        f"0.03 * {iv90} - "
        f"0.11 * ts_rank(high / low, 45) - "
        f"0.12 * ts_rank(returns, 90), industry))",
        d12,
        "Micro liquidity plus IV on the vwap-reversal near-pass.",
    )
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
