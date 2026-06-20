"""Generate a map-guided WQ submit candidate batch.

The batch is intentionally data-source diverse:
- successful ACTIVE edge patterns, changed enough to avoid exact reuse
- forum-derived operator structures
- previous near-miss repair records
- low-covered fields from the current factor map
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

from quantgpt.alpha_tracker import compute_similarity
from quantgpt.expression_parser import extract_components, normalize_expression
from quantgpt.wq_auto_mining import validate_wq_expression


DEFAULT_ACTIVE_NODES = ROOT / "reports" / "wq_active_alpha_map_pnl_20260610_full" / "active_nodes.jsonl"
DEFAULT_REPAIR_QUEUE = (
    ROOT
    / "reports"
    / "wq_submit_non_active10_20260610"
    / "run_submit_new_lowcorr_01"
    / "cycles"
    / "cycle_001"
    / "repair_queue.jsonl"
)
DEFAULT_FORUM_TOP = ROOT / "reports" / "wq_forum_expression_expansion_20260609" / "forum_expansion_top8_allow.jsonl"
DEFAULT_FORUM_TUNING = ROOT / "reports" / "wq_forum_real_submit_20260609" / "forum_regime_tuning_candidates.jsonl"
DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit10_20260610" / "combined_submit10_candidates.jsonl"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    active_rows = _load_active_rows(Path(args.active_nodes))
    active_norms = {normalize_expression(row["expression"]) for row in active_rows if row.get("expression")}

    records: list[dict[str, Any]] = []
    records.extend(_map_guided_records())
    records.extend(_read_forum_records(Path(args.forum_top), source_suffix="top8"))
    records.extend(_read_forum_records(Path(args.forum_tuning), source_suffix="tuning"))
    records.extend(_read_repair_records(Path(args.repair_queue)))

    out_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    invalid: list[dict[str, Any]] = []
    skipped_exact = 0
    skipped_similarity = 0

    for row in records:
        expression = str(row.get("expression") or "").strip()
        if not expression:
            continue
        settings = _clean_settings(row.get("simulation_settings"))
        dedupe_key = normalize_expression(expression) + "||" + json.dumps(settings, sort_keys=True, separators=(",", ":"))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if normalize_expression(expression) in active_norms:
            skipped_exact += 1
            continue
        try:
            validate_wq_expression(expression)
        except Exception as exc:
            invalid.append({**row, "validation_error": str(exc)})
            continue

        nearest = _nearest(expression, active_rows)
        nearest_score = float(((nearest or {}).get("similarity") or {}).get("overall_similarity") or 0.0)
        if nearest_score > args.max_similarity:
            skipped_similarity += 1
            continue
        fields = _fields(expression)
        out_rows.append(
            {
                "expression": expression,
                "tag": str(row.get("tag") or f"submit10-map-{len(out_rows) + 1:03d}"),
                "source": str(row.get("source") or "generate_wq_submit10_candidates"),
                "source_family": str(row.get("source_family") or "map_guided_submit10"),
                "mutation_strategy": row.get("mutation_strategy") or "factor_map_submit10",
                "rationale": row.get("rationale"),
                "expected_low_corr_reason": row.get("expected_low_corr_reason")
                or "Uses factor-map gaps, forum structure, or low-covered fields rather than direct active reuse.",
                "source_fields": row.get("source_fields") or fields,
                "risk_flags": row.get("risk_flags") or ["map_guided_real_submit_candidate"],
                "simulation_settings": settings,
                "active_similarity": nearest,
                "nearest_active_similarity": nearest_score,
                "candidate_meta": {
                    **(row.get("candidate_meta") or {}),
                    "generator": "generate_wq_submit10_candidates",
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
        "source_records": len(records),
        "skipped_exact_active": skipped_exact,
        "skipped_similarity": skipped_similarity,
        "invalid": len(invalid),
        "max_similarity": args.max_similarity,
        "top": [
            {
                "tag": row["tag"],
                "nearest_active_similarity": row["nearest_active_similarity"],
                "fields": row["source_fields"],
                "settings": row.get("simulation_settings") or {},
            }
            for row in out_rows[:20]
        ],
    }
    summary_path = output.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if invalid:
        invalid_path = output.with_suffix(".invalid.jsonl")
        invalid_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in invalid) + "\n",
            encoding="utf-8",
        )
        summary["invalid_output"] = str(invalid_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate submit10 WQ candidate JSONL")
    parser.add_argument("--active-nodes", default=str(DEFAULT_ACTIVE_NODES))
    parser.add_argument("--repair-queue", default=str(DEFAULT_REPAIR_QUEUE))
    parser.add_argument("--forum-top", default=str(DEFAULT_FORUM_TOP))
    parser.add_argument("--forum-tuning", default=str(DEFAULT_FORUM_TUNING))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=140)
    parser.add_argument("--max-similarity", type=float, default=0.84)
    return parser.parse_args(argv)


def _map_guided_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def add(expr: str, tag: str, family: str, *, settings: list[dict[str, Any]] | None = None, strategy: str = "") -> None:
        variants = settings or [{"decay": 8, "truncation": 0.08}]
        for index, variant in enumerate(variants, start=1):
            suffix = "" if len(variants) == 1 else f"-s{index}"
            records.append(
                {
                    "expression": expr,
                    "tag": f"{tag}{suffix}",
                    "source_family": family,
                    "source": "manual_factor_map_submit10",
                    "mutation_strategy": strategy or family,
                    "simulation_settings": variant,
                    "rationale": "Generated from active/non-active factor map gaps and prior self-correlation near-misses.",
                    "expected_low_corr_reason": (
                        "Changes field family and operator skeleton from crowded ACTIVE expressions while preserving "
                        "a slow-quality or revision anchor."
                    ),
                    "risk_flags": ["real_submit_candidate", "factor_map_guided", "requires_online_simulation"],
                }
            )

    slow_settings = [
        {"decay": 8, "truncation": 0.08, "neutralization": "SUBINDUSTRY"},
        {"decay": 16, "truncation": 0.03, "neutralization": "SUBINDUSTRY"},
        {"decay": 8, "truncation": 0.05, "neutralization": "INDUSTRY"},
    ]
    tight_settings = [
        {"decay": 16, "truncation": 0.01, "neutralization": "SUBINDUSTRY"},
        {"decay": 8, "truncation": 0.03, "neutralization": "INDUSTRY"},
    ]
    market_settings = [
        {"decay": 8, "truncation": 0.08, "neutralization": "MARKET"},
        {"decay": 16, "truncation": 0.03, "neutralization": "SUBINDUSTRY"},
    ]

    add(
        "rank(group_neutralize(0.20 * ts_rank(anl4_adjusted_netincome_ft / enterprise_value, 70) + "
        "0.16 * ts_rank(forward_cash_flow_to_price, 90) + "
        "0.16 * rank(-1 * relative_valuation_rank_derivative) + "
        "0.14 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(ts_corr(vwap, volume, 40)) + 0.10 * rank(volume / adv20) - "
        "0.12 * ts_rank(returns, 80), industry))",
        "submit10-edge-analyst-credit-micro",
        "map_edge_analyst_credit_micro",
        settings=slow_settings,
    )
    add(
        "rank(group_rank(0.22 * ts_rank(ts_backfill(operating_income, 120) / assets, 120) + "
        "0.18 * ts_rank(ts_backfill(forward_sales_to_price, 120), 100) + "
        "0.16 * ts_rank(ts_backfill(anl4_afv4_eps_mean, 120), 100) + "
        "0.14 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(ts_corr(vwap, volume, 40)) - 0.12 * ts_rank(returns, 60), subindustry))",
        "submit10-backfill-sales-revision-credit",
        "map_backfill_group_compare_credit",
        settings=tight_settings,
    )
    add(
        "rank(0.34 * group_rank(ts_rank(forward_ebitda_to_enterprise_value_2, 80), industry) + "
        "0.24 * ts_rank(fundamental_growth_module_score, 80) + "
        "0.18 * rank(-1 * relative_valuation_rank_derivative) + "
        "0.14 * rank(-1 * credit_risk_premium_indicator) + "
        "0.10 * rank(ts_corr(vwap, volume, 40)))",
        "submit10-unknown-growth-value-credit",
        "map_unknown_growth_value_credit",
        settings=slow_settings,
    )
    add(
        "rank(group_rank(0.30 * ts_rank(fcf_yield_times_forward_roe_2, 100) + "
        "0.26 * ts_rank(cash_flow_return_on_invested_capital, 100) + "
        "0.18 * rank(-1 * beta_last_30_days_spy) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.10 * rank(-1 * ts_rank(pcr_oi_60, 60)), industry))",
        "submit10-fcf-roic-beta-credit-pcr",
        "map_unknown_fcf_risk_options",
        settings=market_settings,
    )
    add(
        "rank(0.28 * ts_rank(forward_ebitda_to_enterprise_value_2, 100) + "
        "0.24 * rank(-1 * ts_mean(option_breakeven_720, 20)) + "
        "0.22 * rank(-1 * credit_risk_premium_indicator) + "
        "0.16 * ts_rank(forward_sales_to_price, 100) + "
        "0.10 * rank(ts_corr(close, volume, 30)))",
        "submit10-forward-ebitda-breakeven-credit",
        "map_unknown_option_breakeven_forward",
        settings=slow_settings,
    )
    add(
        "rank(0.36 * group_rank(ts_delta(ts_count_nans(anl4_afv4_eps_mean, 240), 20), subindustry) + "
        "0.24 * group_rank(rank(ts_delta(forward_sales_to_price, 5)), subindustry) + "
        "0.18 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(ts_corr(vwap, volume, 40)) - 0.10 * ts_rank(returns, 80))",
        "submit10-missingness-forward-credit",
        "map_forum_missingness_forward_credit",
        settings=slow_settings,
    )
    add(
        "rank(0.32 * group_zscore(ts_delta(snt1_d1_netearningsrevision, 10), subindustry) + "
        "0.26 * ts_rank(forward_cash_flow_to_price, 120) + "
        "0.20 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * zscore(ts_mean(scl12_sentiment_fast_d1, 5)) - "
        "0.10 * ts_rank(close / vwap, 30))",
        "submit10-sentiment-revision-credit-value",
        "map_forum_sentiment_credit_value",
        settings=slow_settings,
    )
    add(
        "trade_when(ts_rank(volume / adv20, 20) > 0.55, "
        "rank(0.34 * group_zscore(ts_rank(forward_sales_to_price, 100), industry) + "
        "0.24 * ts_rank(fundamental_growth_module_score, 80) + "
        "0.18 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * zscore(ts_delta(snt1_cored1_score, 5)) - "
        "0.10 * ts_rank(open / close, 20)), -1)",
        "submit10-tradewhen-growth-credit-revision",
        "map_forum_trade_when_growth_credit",
        settings=tight_settings,
    )
    add(
        "rank(group_neutralize(0.24 * ts_rank(actual_cashflow_per_share_value_quarterly / close, 120) + "
        "0.22 * ts_rank(forward_book_value_to_price, 100) + "
        "0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(ts_corr(close, volume, 30)) - "
        "0.08 * ts_rank(returns, 80), industry))",
        "submit10-cashflowps-book-certainty-credit",
        "map_cashflowps_certainty_credit",
        settings=slow_settings,
    )
    add(
        "rank(0.30 * group_rank(ts_rank(actual_sales_value_quarterly / assets, 120), subindustry) + "
        "0.22 * group_zscore(ts_delta(anl4_af_eps_value / close, 10), industry) + "
        "0.18 * ts_rank(snt1_d1_analystcoverage, 80) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * rank(-1 * ts_rank(pcr_oi_60, 60)))",
        "submit10-sales-assets-eps-coverage-credit",
        "map_group_sales_eps_credit_pcr",
        settings=slow_settings,
    )
    add(
        "rank(0.30 * ts_rank(forward_cash_flow_to_price, 100) + "
        "0.24 * ts_rank(forward_book_value_to_price, 100) + "
        "0.18 * ts_rank(earnings_momentum_composite_score, 60) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(-1 * cashflow_efficiency_rank_derivative))",
        "submit10-forward-value-momentum-credit",
        "map_forward_value_credit_derivative",
        settings=slow_settings,
    )
    add(
        "rank(0.30 * group_zscore(ts_rank(cashflow_op / enterprise_value, 160), sector) + "
        "0.24 * group_zscore(ts_rank(forward_cash_flow_to_price, 120), industry) + "
        "0.18 * zscore(ts_delta(snt1_d1_netearningsrevision, 5)) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) - "
        "0.12 * ts_rank(high / low, 30))",
        "submit10-value-revision-credit-range",
        "map_forum_value_revision_credit",
        settings=slow_settings,
    )
    add(
        "rank(0.26 * group_rank(ts_rank(actual_eps_value_quarterly / enterprise_value, 120), subindustry) + "
        "0.24 * ts_rank(forward_sales_to_price, 100) + "
        "0.20 * rank(-1 * ts_rank(pcr_oi_60, 60)) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) + "
        "0.14 * ts_rank(coefficient_variation_fy1_eps, 80))",
        "submit10-forward-dispersion-pcr-credit",
        "map_repair_forward_dispersion_credit",
        settings=slow_settings,
    )
    add(
        "rank(0.34 * rank(-1 * multi_factor_static_score_derivative) + "
        "0.24 * group_rank(ts_rank(forward_ebitda_to_enterprise_value_2, 80), industry) + "
        "0.20 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank((high - close) / (high - low) * volume / adv20) + "
        "0.10 * rank(-1 * ts_rank(pcr_oi_60, 60)))",
        "submit10-static-derivative-forward-credit",
        "map_derivative_forward_credit_micro",
        settings=slow_settings,
    )
    add(
        "rank(0.30 * ts_rank(ts_backfill(forward_sales_to_price, 120), 120) + "
        "0.24 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / enterprise_value, 100) + "
        "0.18 * rank(-1 * credit_risk_premium_indicator) + "
        "0.16 * rank(ts_corr(vwap, volume, 40)) + "
        "0.12 * rank(-1 * earnings_certainty_rank_derivative))",
        "submit10-backfill-sales-certainty-credit",
        "map_backfill_sales_certainty_credit",
        settings=tight_settings,
    )
    add(
        "rank(0.32 * group_zscore(ts_rank(fcf_yield_times_forward_roe, 120), industry) + "
        "0.24 * group_zscore(ts_rank(forward_book_value_to_price, 120), subindustry) + "
        "0.20 * rank(-1 * ts_mean(option_breakeven_720, 10)) + "
        "0.14 * rank(-1 * beta_last_30_days_spy) + "
        "0.10 * rank(ts_corr(vwap, volume, 40)))",
        "submit10-fcf-forwardbook-breakeven-beta",
        "map_unknown_fcf_forwardbook_breakeven",
        settings=market_settings,
    )
    return records


def _read_forum_records(path: Path, *, source_suffix: str) -> list[dict[str, Any]]:
    rows = []
    for row in _read_jsonl(path):
        expression = str(row.get("expression") or "").strip()
        if not expression:
            continue
        rows.append(
            {
                **row,
                "source": f"{row.get('source') or path.name}:{source_suffix}",
                "source_family": row.get("source_family") or f"forum_{source_suffix}",
                "risk_flags": list(dict.fromkeys((row.get("risk_flags") or []) + ["forum_expansion", "real_submit_candidate"])),
            }
        )
    return rows


def _read_repair_records(path: Path) -> list[dict[str, Any]]:
    rows = []
    for item in _read_jsonl(path):
        for row in item.get("candidate_records") or []:
            if row.get("expression"):
                rows.append(
                    {
                        **row,
                        "source": f"{path.name}:policy_repair",
                        "risk_flags": list(dict.fromkeys((row.get("risk_flags") or []) + ["policy_repair", "real_submit_candidate"])),
                    }
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
    nearest = None
    normalized = normalize_expression(expression)
    for row in active_rows:
        other = str(row.get("expression") or "")
        if not other:
            continue
        similarity = compute_similarity(expression, other)
        item = {
            "alpha_id": row.get("alpha_id"),
            "expression": other,
            "status": row.get("status"),
            "similarity": similarity,
            "exact": normalized == normalize_expression(other),
        }
        if nearest is None or similarity.get("overall_similarity", 0.0) > nearest["similarity"].get("overall_similarity", 0.0):
            nearest = item
    return nearest


def _fields(expression: str) -> list[str]:
    try:
        return sorted(str(field) for field in extract_components(expression).get("fields", []))
    except Exception:
        return []


def _clean_settings(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("region", "universe", "neutralization"):
        value = raw.get(key)
        if value not in (None, ""):
            out[key] = str(value)
    for key in ("delay", "decay"):
        value = raw.get(key)
        if value in (None, ""):
            continue
        try:
            out[key] = int(value)
        except (TypeError, ValueError):
            pass
    if raw.get("truncation") not in (None, ""):
        try:
            truncation = float(raw["truncation"])
        except (TypeError, ValueError):
            truncation = None
        if truncation is not None and 0 < truncation <= 0.2:
            out["truncation"] = truncation
    for key in ("maxTrade", "maxPosition"):
        value = raw.get(key)
        if value not in (None, ""):
            text = str(value).upper()
            if text in {"ON", "OFF"}:
                out[key] = text
    return out


if __name__ == "__main__":
    raise SystemExit(main())
