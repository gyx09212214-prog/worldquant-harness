"""Generate focused WQ submit candidates from the latest live-submit lessons."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.alpha_tracker import compute_similarity
from worldquant_harness.expression_parser import extract_components, normalize_expression
from worldquant_harness.wq_auto_mining import validate_wq_expression


DEFAULT_ACTIVE_NODES = ROOT / "reports" / "wq_active_alpha_map_pnl_20260610_full" / "active_nodes.jsonl"
DEFAULT_SUBMIT_ROOT = ROOT / "reports" / "wq_submit10_20260610"
DEFAULT_OUTPUT = DEFAULT_SUBMIT_ROOT / "focused_success_cross_candidates.jsonl"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    active_rows = _load_active_rows(Path(args.active_nodes))
    active_rows.extend(_load_live_active_rows(Path(args.submit_root)))
    active_norms = {normalize_expression(row["expression"]) for row in active_rows if row.get("expression")}

    out_rows: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in _records():
        expression = row["expression"].strip()
        settings = row.get("simulation_settings") or {}
        dedupe_key = normalize_expression(expression) + "||" + json.dumps(settings, sort_keys=True, separators=(",", ":"))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if normalize_expression(expression) in active_norms:
            continue
        try:
            validate_wq_expression(expression)
        except Exception as exc:
            invalid.append({**row, "validation_error": str(exc)})
            continue
        nearest = _nearest(expression, active_rows)
        nearest_score = float(((nearest or {}).get("similarity") or {}).get("overall_similarity") or 0.0)
        if nearest_score > args.max_similarity:
            continue
        fields = _fields(expression)
        out_rows.append(
            {
                **row,
                "source": "generate_wq_focused_submit_candidates",
                "source_fields": fields,
                "active_similarity": nearest,
                "nearest_active_similarity": nearest_score,
                "risk_flags": ["real_submit_candidate", "focused_success_cross", "requires_online_simulation"],
                "candidate_meta": {
                    "generator": "generate_wq_focused_submit_candidates",
                    "field_signature": "|".join(fields),
                },
            }
        )
        if len(out_rows) >= args.limit:
            break

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in out_rows) + "\n", encoding="utf-8")
    summary = {
        "ok": True,
        "output": str(output),
        "written": len(out_rows),
        "invalid": len(invalid),
        "max_similarity": args.max_similarity,
        "top": [
            {
                "tag": row["tag"],
                "nearest_active_similarity": row["nearest_active_similarity"],
                "settings": row.get("simulation_settings") or {},
                "fields": row.get("source_fields") or [],
            }
            for row in out_rows[:20]
        ],
    }
    summary_path = output.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if invalid:
        invalid_path = output.with_suffix(".invalid.jsonl")
        invalid_path.write_text("\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in invalid) + "\n", encoding="utf-8")
        summary["invalid_output"] = str(invalid_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate focused WQ candidates for submit10 continuation")
    parser.add_argument("--active-nodes", default=str(DEFAULT_ACTIVE_NODES))
    parser.add_argument("--submit-root", default=str(DEFAULT_SUBMIT_ROOT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--max-similarity", type=float, default=0.86)
    return parser.parse_args(argv)


def _records() -> list[dict[str, Any]]:
    d8 = {"neutralization": "SUBINDUSTRY", "decay": 8, "truncation": 0.08}
    d16 = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.03}
    ind = {"neutralization": "INDUSTRY", "decay": 8, "truncation": 0.05}
    tight = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.01}

    rows: list[dict[str, Any]] = []

    def add(expr: str, tag: str, family: str, settings: dict[str, Any], rationale: str) -> None:
        rows.append(
            {
                "expression": expr,
                "tag": tag,
                "source_family": family,
                "mutation_strategy": "live_success_cross_plus_forum_structure",
                "rationale": rationale,
                "expected_low_corr_reason": (
                    "Crosses the two live-success anchors with different field families, windows, "
                    "or a small forum-style revision/microstructure leg instead of reusing either ACTIVE expression."
                ),
                "simulation_settings": settings,
            }
        )

    add(
        "rank(group_neutralize(0.22 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        "0.20 * ts_rank(forward_book_value_to_price, 120) + "
        "0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(ts_corr(close, volume, 30)) - "
        "0.12 * ts_rank(returns, 80), industry))",
        "focused-netincome-book-certainty-credit-micro-d16",
        "focused_success_cross_value_certainty",
        d16,
        "Replaces the submitted edge alpha's forward cash-flow leg with forward book and certainty.",
    )
    add(
        "rank(group_neutralize(0.24 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        "0.20 * ts_rank(forward_cash_flow_to_price, 90) + "
        "0.18 * rank(-1 * relative_valuation_rank_derivative) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(volume / adv20) - "
        "0.10 * ts_rank(returns, 70), industry))",
        "focused-cashflowps-forwardcf-relative-credit-volume-d8",
        "focused_success_cross_cashflow_relative",
        d8,
        "Crosses the cashflow-per-share success with the relative-valuation leg from the analyst edge success.",
    )
    add(
        "rank(group_neutralize(0.26 * group_zscore(ts_rank(cashflow_op / enterprise_value, 140), sector) + "
        "0.22 * ts_rank(forward_book_value_to_price, 110) + "
        "0.16 * rank(-1 * cashflow_efficiency_rank_derivative) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.10 * rank(ts_corr(vwap, volume, 40)) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "focused-cfop-ev-book-efficiency-credit-micro-d8",
        "focused_forum_cashflow_quality_credit",
        d8,
        "Uses the forum cash-flow/value theme but anchors it to the live-success credit and microstructure overlay.",
    )
    add(
        "rank(group_neutralize(0.24 * ts_rank(actual_eps_value_quarterly / enterprise_value, 120) + "
        "0.22 * ts_rank(forward_book_value_to_price, 100) + "
        "0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(ts_corr(close, volume, 30)) - "
        "0.08 * ts_rank(high / low, 30), industry))",
        "focused-eps-book-certainty-credit-range-d8",
        "focused_eps_certainty_credit",
        d8,
        "Tests EPS value as a less-used substitute for cashflow-per-share while keeping the proven certainty/credit shell.",
    )
    add(
        "rank(group_neutralize(0.24 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 70) + "
        "0.20 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 120) + "
        "0.18 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.12 * rank(ts_corr(vwap, volume, 40)) - "
        "0.12 * ts_rank(returns, 80), industry))",
        "focused-netincome-cashflowps-certainty-credit-d16",
        "focused_double_cash_generation_credit",
        d16,
        "Combines the two submitted fundamental anchors while dropping forward value fields to reduce direct overlap.",
    )
    add(
        "rank(group_neutralize(0.24 * ts_rank(forward_cash_flow_to_price, 100) + "
        "0.22 * ts_rank(forward_book_value_to_price, 100) + "
        "0.18 * zscore(ts_delta(snt1_d1_netearningsrevision, 5)) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.10 * rank(ts_corr(close, volume, 30)) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "focused-forward-value-revision-credit-micro-d16",
        "focused_forum_revision_credit_micro",
        d16,
        "Keeps forum analyst-revision information as a modest overlay instead of making sentiment the main driver.",
    )
    add(
        "rank(group_neutralize(0.24 * ts_rank(actual_cashflow_per_share_value_quarterly / vwap, 120) + "
        "0.20 * ts_rank(forward_book_value_to_price, 120) + "
        "0.18 * rank(-1 * credit_risk_premium_indicator) + "
        "0.16 * rank(-1 * beta_last_30_days_spy) + "
        "0.12 * rank(ts_corr(close, volume, 30)) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "focused-cashflowps-book-credit-beta-micro-d8",
        "focused_risk_adjusted_cashflow_book",
        d8,
        "Adds a risk-control beta leg to the submitted cashflow/book shell.",
    )
    add(
        "rank(group_neutralize(0.26 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        "0.20 * group_zscore(ts_rank(cashflow_op / cap, 120), subindustry) + "
        "0.18 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * cashflow_efficiency_rank_derivative) + "
        "0.12 * rank(volume / adv20) - "
        "0.10 * ts_rank(close / vwap, 30), industry))",
        "focused-netincome-cfop-cap-efficiency-volume-d16",
        "focused_forum_cfop_micro_cross",
        d16,
        "Uses forum cfop/cap and intraday dislocation ideas with the successful net-income/credit anchor.",
    )
    add(
        "rank(group_neutralize(0.24 * ts_rank(ts_backfill(actual_cashflow_per_share_value_quarterly, 120) / close, 120) + "
        "0.22 * ts_rank(ts_backfill(forward_book_value_to_price, 120), 100) + "
        "0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(ts_corr(vwap, volume, 40)) - "
        "0.08 * ts_rank(returns, 80), industry))",
        "focused-backfill-cashflowps-book-certainty-credit-tight",
        "focused_backfill_cashflow_book",
        tight,
        "Applies the forum backfill trick to the proven cashflow/book/certainty factor family.",
    )
    add(
        "rank(group_neutralize(0.24 * ts_rank(forward_cash_flow_to_price, 80) + "
        "0.20 * rank(-1 * relative_valuation_rank_derivative) + "
        "0.18 * rank(-1 * credit_risk_premium_indicator) + "
        "0.16 * rank(ts_corr(vwap, volume, 40)) + "
        "0.12 * rank(volume / adv20) - "
        "0.10 * ts_rank(high / low, 30), industry))",
        "focused-forwardcf-relative-credit-micro-range-d8",
        "focused_relative_value_micro_range",
        d8,
        "Drops the analyst net-income leg from the first success to test a lighter forward-value/micro map node.",
    )
    add(
        "rank(group_neutralize(0.24 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 120) + "
        "0.20 * group_rank(ts_rank(forward_cash_flow_to_price, 100), industry) + "
        "0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * zscore(ts_delta(snt1_d1_netearningsrevision, 5)) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "focused-cashflowps-forwardcf-certainty-revision-d16",
        "focused_cashflow_revision_blend",
        d16,
        "Blends the successful cashflow/certainty shell with a controlled forum revision term.",
    )
    add(
        "rank(group_neutralize(0.26 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + "
        "0.22 * ts_rank(forward_cash_flow_to_price, 100) + "
        "0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.10 * rank(-1 * ts_rank(pcr_oi_60, 60)) - "
        "0.08 * ts_rank(returns, 80), industry))",
        "focused-netincome-forwardcf-certainty-credit-pcr-d16",
        "focused_options_overlay_credit",
        d16,
        "Uses options PCR as a small orthogonal overlay on the stronger net-income/forward-cash-flow family.",
    )
    add(
        "rank(group_neutralize(0.24 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 120) + "
        "0.20 * ts_rank(forward_book_value_to_price, 100) + "
        "0.18 * rank(-1 * cashflow_efficiency_rank_derivative) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank((high - close) / (high - low) * volume / adv20) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "focused-cashflowps-book-efficiency-intraday-d8",
        "focused_intraday_cashflow_book",
        d8,
        "Replaces the close-volume correlation in the cashflow success with the forum intraday pressure operator.",
    )
    add(
        "rank(group_neutralize(0.22 * ts_rank(cashflow_op / enterprise_value, 120) + "
        "0.22 * ts_rank(forward_cash_flow_to_price, 100) + "
        "0.18 * rank(-1 * relative_valuation_rank_derivative) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(ts_corr(vwap, volume, 40)) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "focused-cfop-forwardcf-relative-credit-micro-d16",
        "focused_cashflow_value_relative",
        d16,
        "Builds a cash-flow value expression around fields not identical to either submitted expression.",
    )
    add(
        "rank(group_neutralize(0.22 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        "0.20 * ts_rank(fcf_yield_times_forward_roe, 120) + "
        "0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(ts_corr(close, volume, 30)) - "
        "0.12 * ts_rank(returns, 80), industry))",
        "focused-cashflowps-fcfroe-certainty-credit-d8",
        "focused_fcfroe_cashflow_certainty",
        d8,
        "Tests a forward-ROE cash-flow field as an underused substitute inside the proven shell.",
    )
    add(
        "rank(group_neutralize(0.24 * group_zscore(ts_rank(cashflow_op / cap, 120), industry) + "
        "0.22 * ts_rank(forward_book_value_to_price, 100) + "
        "0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.10 * rank(ts_argmax(volume, 30)) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "focused-cfop-cap-book-certainty-volume-recency-d16",
        "focused_forum_volume_recency_credit",
        d16,
        "Adds a small forum-style volume recency term to a cash-flow/book/certainty factor.",
    )
    add(
        "rank(group_neutralize(0.24 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 70) + "
        "0.20 * ts_rank(forward_book_value_to_price, 100) + "
        "0.18 * rank(-1 * cashflow_efficiency_rank_derivative) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(close / vwap) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "focused-netincome-book-efficiency-close-vwap-d8",
        "focused_close_vwap_efficiency",
        d8,
        "Uses close/vwap directly as a microstructure residual rather than correlation or adv scaling.",
    )
    add(
        "rank(group_neutralize(0.24 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 120) + "
        "0.20 * ts_rank(forward_book_value_to_price, 100) + "
        "0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(-1 * ts_rank(close / vwap, 30)) - "
        "0.10 * ts_rank(high / low, 30), industry))",
        "focused-cashflowps-book-certainty-vwap-reversal-ind",
        "focused_vwap_reversal_cashflow_book",
        ind,
        "Retests the submitted cashflow/book shell with a distinct industry setting and vwap-reversal overlay.",
    )
    add(
        "trade_when(volume > adv20, rank(group_neutralize(0.24 * ts_rank(cashflow_op / enterprise_value, 120) + "
        "0.22 * ts_rank(forward_cash_flow_to_price, 100) + "
        "0.18 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * relative_valuation_rank_derivative) - "
        "0.12 * ts_rank(returns, 80), industry)), -1)",
        "focused-tradewhen-cfop-forwardcf-credit-relative-tight",
        "focused_trade_when_cashflow_value",
        tight,
        "Keeps forum trade_when as a liquidity gate around a slow cash-flow/value expression.",
    )
    add(
        "rank(group_neutralize(0.24 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 120) + "
        "0.20 * ts_rank(forward_cash_flow_to_price, 90) + "
        "0.18 * rank(-1 * credit_risk_premium_indicator) + "
        "0.16 * rank(-1 * correlation_last_30_days_spy) + "
        "0.12 * rank(ts_corr(vwap, volume, 40)) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "focused-cashflowps-forwardcf-credit-spycorr-d8",
        "focused_market_corr_overlay",
        d8,
        "Uses the repair queue's low market-correlation idea inside the stronger cash-flow shell.",
    )
    add(
        "rank(group_neutralize(0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 80) + "
        "0.16 * ts_rank(forward_cash_flow_to_price, 140) + "
        "0.16 * rank(-1 * relative_valuation_rank_derivative) + "
        "0.14 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * ts_rank(pcr_oi_60, 60)) + "
        "0.12 * rank((high - close) / (high - low) * volume / adv20) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "repair-highmetric-cashflowps-forwardcf-pcr-intraday-d8",
        "repair_high_metric_cashflow_forwardcf",
        d8,
        "Reduces the high-metric candidate's two strongest value legs and substitutes PCR plus intraday pressure.",
    )
    add(
        "rank(group_neutralize(0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        "0.16 * ts_rank(forward_cash_flow_to_price, 90) + "
        "0.16 * zscore(ts_delta(snt1_d1_netearningsrevision, 5)) + "
        "0.14 * rank(-1 * relative_valuation_rank_derivative) + "
        "0.14 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(-1 * correlation_last_30_days_spy) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "repair-highmetric-cashflowps-forwardcf-revision-spycorr-d16",
        "repair_high_metric_revision_spycorr",
        d16,
        "Uses analyst revision and market-correlation repair legs to lower self-correlation while preserving value signal.",
    )
    add(
        "rank(group_neutralize(0.18 * group_rank(ts_rank(actual_cashflow_per_share_value_quarterly / close, 100), subindustry) + "
        "0.16 * group_rank(ts_rank(forward_cash_flow_to_price, 120), industry) + "
        "0.16 * rank(-1 * relative_valuation_rank_derivative) + "
        "0.14 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(volume / adv20) + "
        "0.12 * rank(-1 * ts_rank(close / vwap, 30)) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "repair-highmetric-group-ranked-vwap-reversal-d8",
        "repair_high_metric_group_rank_vwap",
        d8,
        "Changes the high-metric candidate into within-group rank space and adds vwap reversal.",
    )
    add(
        "trade_when(ts_rank(abs(close / vwap), 20) > 0.55, "
        "rank(group_neutralize(0.20 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        "0.18 * ts_rank(forward_cash_flow_to_price, 90) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * relative_valuation_rank_derivative) - "
        "0.12 * ts_rank(returns, 80), industry)), -1)",
        "repair-highmetric-vwap-regime-gated-tight",
        "repair_high_metric_regime_gate",
        tight,
        "Uses the forum regime gate to make the high-metric skeleton trade in a narrower state space.",
    )
    add(
        "rank(group_neutralize(0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / vwap, 100) + "
        "0.16 * ts_rank(forward_cash_flow_to_price, 120) + "
        "0.16 * rank(-1 * relative_valuation_rank_derivative) + "
        "0.14 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * beta_last_30_days_spy) + "
        "0.12 * rank(ts_corr(close, volume, 50)) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "repair-highmetric-beta-volume-corr-d8",
        "repair_high_metric_beta_volume",
        d8,
        "Substitutes vwap denominator, beta control, and a longer volume-correlation window.",
    )
    add(
        "rank(group_neutralize(0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 60) + "
        "0.16 * ts_rank(forward_cash_flow_to_price, 160) + "
        "0.16 * rank(-1 * relative_valuation_rank_derivative) + "
        "0.14 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * cashflow_efficiency_rank_derivative) + "
        "0.12 * rank(ts_argmax(volume, 30)) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "repair-highmetric-window-shift-efficiency-recency-d16",
        "repair_high_metric_window_shift",
        d16,
        "Shifts value windows apart and uses efficiency plus volume recency as orthogonal legs.",
    )
    add(
        "rank(group_neutralize(0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        "0.16 * ts_rank(forward_cash_flow_to_price, 90) + "
        "0.16 * rank(-1 * relative_valuation_rank_derivative) + "
        "0.14 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * zscore(ts_delta(anl4_af_eps_value / close, 10)) + "
        "0.12 * rank(ts_corr(vwap, volume, 60)) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "repair-highmetric-eps-revision-volume60-d16",
        "repair_high_metric_eps_revision",
        d16,
        "Uses an EPS revision leg rather than pure sentiment to reduce overlap with existing cash-flow factors.",
    )
    add(
        "rank(group_neutralize(0.20 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 120) + "
        "0.18 * ts_rank(forward_cash_flow_to_price, 90) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * relative_valuation_rank_derivative) + "
        "0.12 * rank(-1 * ts_rank(implied_volatility_mean_30, 80)) + "
        "0.10 * rank(volume / adv20) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "repair-highmetric-iv-overlay-volume-ind",
        "repair_high_metric_iv_overlay",
        ind,
        "Adds an implied-volatility overlay and changes the neutralization setting from the high-correlation run.",
    )
    add(
        "rank(group_neutralize(0.20 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 70) + "
        "0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 120) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.12 * rank(-1 * ts_rank(pcr_oi_60, 60)) + "
        "0.10 * rank(ts_corr(vwap, volume, 60)) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "repair-nearpass-netincome-cashflowps-pcr-vol60-d16",
        "repair_nearpass_netincome_cashflowps",
        d16,
        "Moves the near-pass netincome/cashflowps alpha away from existing factors with PCR and longer volume correlation.",
    )
    add(
        "rank(group_neutralize(0.20 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 90) + "
        "0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / vwap, 100) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.12 * zscore(ts_delta(snt1_d1_netearningsrevision, 5)) + "
        "0.10 * rank(volume / adv20) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "repair-nearpass-netincome-cashflowps-revision-volume-d16",
        "repair_nearpass_revision_volume",
        d16,
        "Uses a small analyst-revision forum leg and vwap denominator to nudge the near-pass alpha below the self-corr cutoff.",
    )
    add(
        "rank(group_neutralize(0.20 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 70) + "
        "0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 120) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.12 * rank((high - close) / (high - low) * volume / adv20) - "
        "0.10 * ts_rank(high / low, 30) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "repair-nearpass-netincome-cashflowps-intraday-range-d8",
        "repair_nearpass_intraday_range",
        d8,
        "Replaces the volume-correlation leg with intraday pressure and range penalty.",
    )
    add(
        "rank(group_neutralize(0.20 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 70) + "
        "0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 120) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.12 * rank(-1 * beta_last_30_days_spy) + "
        "0.10 * rank(-1 * ts_rank(close / vwap, 30)) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "repair-nearpass-netincome-cashflowps-beta-vwap-d8",
        "repair_nearpass_beta_vwap",
        d8,
        "Adds a beta and vwap-reversal overlay to reduce overlap with the submitted net-income and cashflow factors.",
    )
    add(
        "rank(group_neutralize(0.20 * group_rank(ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80), industry) + "
        "0.18 * group_rank(ts_rank(actual_cashflow_per_share_value_quarterly / close, 120), subindustry) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.12 * rank(ts_corr(close, volume, 50)) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "repair-nearpass-group-rank-volume50-ind",
        "repair_nearpass_group_rank",
        ind,
        "Moves the near-pass expression into group-rank space and changes neutralization setting.",
    )
    add(
        "rank(group_neutralize(0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 80) + "
        "0.16 * ts_rank(forward_cash_flow_to_price, 140) + "
        "0.16 * rank(-1 * relative_valuation_rank_derivative) + "
        "0.14 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * ts_rank(pcr_oi_60, 60)) + "
        "0.12 * rank((high - close) / (high - low) * volume / adv20) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "repair-concentration-highmetric-pcr-intraday-maxpos-t003",
        "repair_concentration_highmetric",
        {"neutralization": "SUBINDUSTRY", "decay": 8, "truncation": 0.03, "maxPosition": "ON"},
        "Retests the strong high-metric repair with lower truncation and maxPosition enabled.",
    )
    add(
        "rank(group_neutralize(0.18 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 80) + "
        "0.16 * ts_rank(forward_cash_flow_to_price, 140) + "
        "0.16 * rank(-1 * relative_valuation_rank_derivative) + "
        "0.14 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * ts_rank(pcr_oi_60, 60)) + "
        "0.12 * rank((high - close) / (high - low) * volume / adv20) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "repair-concentration-highmetric-pcr-intraday-maxpos-t001",
        "repair_concentration_highmetric",
        {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.01, "maxPosition": "ON"},
        "Retests the concentrated but strong candidate with tighter truncation and slower decay.",
    )
    add(
        "rank(group_neutralize(0.18 * group_rank(ts_rank(actual_cashflow_per_share_value_quarterly / close, 80), subindustry) + "
        "0.16 * group_rank(ts_rank(forward_cash_flow_to_price, 140), industry) + "
        "0.16 * rank(-1 * relative_valuation_rank_derivative) + "
        "0.14 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * ts_rank(pcr_oi_60, 60)) + "
        "0.12 * rank((high - close) / (high - low) * volume / adv20) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "repair-concentration-highmetric-group-rank-t003",
        "repair_concentration_group_rank",
        {"neutralization": "SUBINDUSTRY", "decay": 8, "truncation": 0.03, "maxPosition": "ON"},
        "Adds group-rank smoothing to reduce position concentration while keeping the successful repair legs.",
    )
    add(
        "rank(group_neutralize(0.19 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 70) + "
        "0.17 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 120) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.12 * rank((high - close) / (high - low) * volume / adv20) + "
        "0.07 * rank(-1 * ts_rank(pcr_oi_60, 60)) - "
        "0.08 * ts_rank(high / low, 30) - "
        "0.07 * ts_rank(returns, 80), industry))",
        "repair-nearpass-intraday-range-pcr-lite-d8",
        "repair_nearpass_intraday_pcr_lite",
        d8,
        "Adds a small PCR leg to the self-corr 0.739 near-pass intraday/range expression.",
    )
    add(
        "rank(group_neutralize(0.19 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 70) + "
        "0.17 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 120) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.12 * rank((high - close) / (high - low) * volume / adv20) + "
        "0.07 * rank(-1 * beta_last_30_days_spy) - "
        "0.08 * ts_rank(high / low, 30) - "
        "0.07 * ts_rank(returns, 80), industry))",
        "repair-nearpass-intraday-range-beta-lite-d8",
        "repair_nearpass_intraday_beta_lite",
        d8,
        "Uses a light beta leg to lower self-corr without weakening the near-pass alpha too much.",
    )
    add(
        "rank(group_neutralize(0.19 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 80) + "
        "0.17 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 100) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.12 * rank(ts_corr(vwap, volume, 60)) + "
        "0.07 * rank((high - close) / (high - low) * volume / adv20) - "
        "0.08 * ts_rank(high / low, 30) - "
        "0.07 * ts_rank(returns, 80), industry))",
        "repair-nearpass-volcorr60-intraday-mix-d16",
        "repair_nearpass_volcorr_intraday",
        d16,
        "Mixes longer volume correlation with intraday pressure and shifts windows from the self-corr 0.739 run.",
    )
    add(
        "rank(group_neutralize(0.19 * group_rank(ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 70), industry) + "
        "0.17 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 120) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.12 * rank((high - close) / (high - low) * volume / adv20) - "
        "0.08 * ts_rank(high / low, 30) - "
        "0.07 * ts_rank(returns, 80), industry))",
        "repair-nearpass-netincome-grouprank-intraday-d8",
        "repair_nearpass_netincome_grouprank",
        d8,
        "Changes only the net-income leg into group-rank space to reduce overlap while preserving the rest.",
    )

    return rows


def _load_active_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    for row in _read_jsonl(path):
        expression = str(row.get("expression") or "").strip()
        if not expression:
            continue
        rows.append(
            {
                "alpha_id": (row.get("alpha_ids") or [None])[0],
                "status": "ACTIVE",
                "expression": expression,
                "metrics": row.get("metrics") or {},
            }
        )
    return rows


def _load_live_active_rows(root: Path) -> list[dict[str, Any]]:
    rows = []
    if not root.is_dir():
        return rows
    for path in root.rglob("submit_results.jsonl"):
        for row in _read_jsonl(path):
            if row.get("ok") and str(row.get("final_status") or "").upper() == "ACTIVE" and row.get("expression"):
                rows.append(
                    {
                        "alpha_id": row.get("alpha_id"),
                        "status": "ACTIVE",
                        "expression": row.get("expression"),
                        "metrics": {
                            "sharpe": row.get("sharpe"),
                            "fitness": row.get("fitness"),
                            "turnover": row.get("turnover"),
                            "sc_value": row.get("sc_value"),
                        },
                    }
                )
    return rows


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _nearest(expression: str, active_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    best = None
    for active in active_rows:
        similarity = compute_similarity(expression, active["expression"])
        score = float(similarity.get("overall_similarity") or 0.0)
        if best is None or score > float(best["similarity"].get("overall_similarity") or 0.0):
            best = {
                "alpha_id": active.get("alpha_id"),
                "status": active.get("status"),
                "metrics": active.get("metrics") or {},
                "similarity": similarity,
            }
    return best


def _fields(expression: str) -> list[str]:
    try:
        components = extract_components(expression)
    except Exception:
        return []
    return sorted(set(components.get("fields") or []))


if __name__ == "__main__":
    raise SystemExit(main())
