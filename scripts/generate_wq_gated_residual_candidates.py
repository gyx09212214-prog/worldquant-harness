"""Generate low-crowding gated/residual WQ submit candidates.

This batch deliberately moves away from the crowded cash-flow/options shells
that dominated the latest self-correlation failures.  It uses relationship,
dividend, coverage/update, PCR, beta, and event-gated structures as primary
paths, while keeping familiar value/reversal legs small.
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

from worldquant_harness.alpha_tracker import compute_similarity
from worldquant_harness.expression_parser import extract_components, normalize_expression
from worldquant_harness.wq_auto_mining import validate_wq_expression


DEFAULT_ACTIVE_NODES = ROOT / "reports" / "wq_active_alpha_map_pnl_20260610_full" / "active_nodes.jsonl"
DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit10_20260610" / "gated_residual_candidates.jsonl"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    active_rows = _load_active_rows(Path(args.active_nodes))
    active_norms = {normalize_expression(row["expression"]) for row in active_rows if row.get("expression")}

    rows: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    skipped_exact = 0
    skipped_similarity = 0
    seen: set[str] = set()

    for record in _records():
        for idx, settings in enumerate(record.pop("settings"), start=1):
            row = {**record, "simulation_settings": settings}
            if idx > 1:
                row["tag"] = f"{row['tag']}-v{idx}"
            expression = row["expression"].strip()
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
            rows.append(
                {
                    **row,
                    "source": "generate_wq_gated_residual_candidates",
                    "source_fields": fields,
                    "active_similarity": nearest,
                    "nearest_active_similarity": nearest_score,
                    "risk_flags": [
                        "real_submit_candidate",
                        "low_crowding_structure_jump",
                        "requires_online_simulation",
                    ],
                    "candidate_meta": {
                        "generator": "generate_wq_gated_residual_candidates",
                        "field_signature": "|".join(fields),
                    },
                }
            )
            if len(rows) >= args.limit:
                break
        if len(rows) >= args.limit:
            break

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows) + "\n", encoding="utf-8")
    summary = {
        "ok": True,
        "output": str(output),
        "written": len(rows),
        "invalid": len(invalid),
        "skipped_exact_active": skipped_exact,
        "skipped_similarity": skipped_similarity,
        "max_similarity": args.max_similarity,
        "top": [
            {
                "tag": row["tag"],
                "nearest_active_similarity": row["nearest_active_similarity"],
                "family": row["source_family"],
                "settings": row["simulation_settings"],
                "fields": row["source_fields"],
            }
            for row in rows[:20]
        ],
    }
    output.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if invalid:
        output.with_suffix(".invalid.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in invalid) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate gated residual WQ candidates")
    parser.add_argument("--active-nodes", default=str(DEFAULT_ACTIVE_NODES))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--max-similarity", type=float, default=0.88)
    return parser.parse_args(argv)


def _records() -> list[dict[str, Any]]:
    d8 = {"neutralization": "SUBINDUSTRY", "decay": 8, "truncation": 0.08}
    d16 = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.03}
    ind = {"neutralization": "INDUSTRY", "decay": 8, "truncation": 0.05}
    sector = {"neutralization": "SECTOR", "decay": 8, "truncation": 0.05}
    tight = {"neutralization": "SUBINDUSTRY", "decay": 16, "truncation": 0.01}
    market = {"neutralization": "MARKET", "decay": 8, "truncation": 0.08}

    rows: list[dict[str, Any]] = []

    def add(expr: str, tag: str, family: str, settings: list[dict[str, Any]], rationale: str) -> None:
        rows.append(
            {
                "expression": expr,
                "tag": tag,
                "source_family": family,
                "mutation_strategy": "gated_residual_structure_jump",
                "rationale": rationale,
                "expected_low_corr_reason": (
                    "Primary fields/operators are underrepresented in the active map; value, PCR, "
                    "or reversal legs are deliberately kept small."
                ),
                "settings": settings,
            }
        )

    add(
        "rank(group_neutralize(0.34 * ts_rank(rel_ret_cust, 120) + "
        "0.26 * ts_rank(rel_ret_supp, 120) + "
        "0.18 * rank(-1 * ts_rank(returns, 120)) + "
        "0.12 * rank(volume / adv20) + "
        "0.10 * rank(-1 * correlation_last_30_days_spy), industry))",
        "gated-rel-cust-supp-reversal",
        "relationship_return_residual",
        [ind, sector],
        "Uses supply-chain relationship return fields as the primary axis.",
    )
    add(
        "rank(0.32 * group_rank(ts_rank(rel_momentum, 80), industry) + "
        "0.24 * group_rank(ts_rank(rel_volume, 80), subindustry) + "
        "0.20 * rank(-1 * ts_rank(returns, 100)) + "
        "0.14 * rank(-1 * beta_last_30_days_spy) + "
        "0.10 * rank(-1 * ts_rank(pcr_oi_10, 60)))",
        "gated-rel-momentum-volume-pcr",
        "relationship_flow_options_overlay",
        [d8, ind],
        "Tests relationship momentum and volume with only a small PCR overlay.",
    )
    add(
        "rank(group_neutralize(0.30 * group_zscore(ts_delta(rel_num_cust, 20), industry) - "
        "0.24 * group_zscore(ts_delta(rel_num_supp, 20), industry) + "
        "0.20 * ts_rank(forward_sales_to_price, 100) + "
        "0.14 * rank(-1 * ts_rank(returns, 120)) + "
        "0.12 * rank(volume / adv20), sector))",
        "gated-rel-count-sales-residual",
        "relationship_count_update",
        [sector, d16],
        "Transforms relationship-count updates into a sector-neutral residual.",
    )
    add(
        "rank(0.30 * rank(-1 * ts_rank(short_interest, 80)) + "
        "0.24 * rank(-1 * ts_rank(short_ratio, 80)) + "
        "0.22 * ts_rank(institutional_ownership, 120) + "
        "0.14 * rank(-1 * beta_last_30_days_spy) - "
        "0.10 * ts_rank(returns, 60))",
        "gated-short-ownership-defensive",
        "short_ownership_defensive",
        [market, sector],
        "Probes short/ownership data as a new field family; remote support is tested online.",
    )
    add(
        "trade_when(ts_rank(volume / adv20, 20) > 0.55, "
        "rank(0.34 * rank(-1 * ts_rank(short_sale_cost, 80)) + "
        "0.24 * ts_rank(rel_ret_cust, 100) + "
        "0.18 * rank(-1 * ts_rank(returns, 120)) + "
        "0.14 * rank(ts_corr(vwap, volume, 60)) + "
        "0.10 * rank(-1 * ts_rank(pcr_oi_10, 60))), -1)",
        "gated-shortcost-rel-liquidity",
        "short_cost_relationship_gate",
        [tight, ind],
        "Uses trade_when only as a liquidity gate around short-cost and relationship payloads.",
    )
    add(
        "rank(group_neutralize(0.34 * ts_rank(five_year_dividend_growth_rate_2, 120) + "
        "0.26 * ts_rank(dividends_to_gross_profit, 100) + "
        "0.18 * group_rank(ts_rank(actual_dividend_value_quarterly / open, 80), industry) + "
        "0.12 * rank(-1 * beta_last_90_days_spy) - "
        "0.10 * ts_rank(returns, 80), sector))",
        "gated-dividend-growth-quality",
        "dividend_growth_residual",
        [sector, d16],
        "Moves to dividend growth/quality, a sparse active-map field family.",
    )
    add(
        "rank(0.36 * group_rank(ts_rank(anl4_afv4_div_median / close, 100), industry) + "
        "0.24 * ts_rank(five_year_dividend_growth_rate_2, 120) + "
        "0.18 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(-1 * ts_rank(pcr_oi_10, 60)) - "
        "0.10 * ts_rank(returns, 80))",
        "gated-dividend-analyst-credit",
        "dividend_analyst_credit",
        [ind, d8],
        "Blends analyst dividend estimates with a credit and PCR overlay.",
    )
    add(
        "rank(0.34 * group_zscore(ts_delta(snt1_d1_analystcoverage, 10), industry) + "
        "0.26 * group_zscore(ts_delta(anl4_afv4_eps_mean, 10), subindustry) + "
        "0.18 * ts_rank(forward_book_value_to_price, 100) + "
        "0.12 * rank(-1 * correlation_last_30_days_spy) - "
        "0.10 * ts_rank(returns, 80))",
        "gated-coverage-eps-update-book",
        "coverage_update_value",
        [d16, ind],
        "Uses coverage and EPS estimate update events instead of static EPS/value.",
    )
    add(
        "rank(0.36 * zscore(days_from_last_change(ts_count_nans(actual_sales_value_quarterly, 240))) + "
        "0.24 * group_zscore(ts_delta(snt1_d1_netearningsrevision, 5), subindustry) + "
        "0.20 * ts_rank(forward_sales_to_price, 100) + "
        "0.10 * rank(-1 * ts_rank(pcr_oi_10, 60)) - "
        "0.10 * ts_rank(close / vwap, 30))",
        "gated-missingness-recency-revision",
        "missingness_recency_revision",
        [d8, tight],
        "Turns missingness into event recency to avoid the crowded count-delta template.",
    )
    add(
        "trade_when(abs(ts_delta(snt1_d1_netearningsrevision, 1)) > 0, "
        "rank(0.34 * ts_rank(actual_sales_value_quarterly / enterprise_value, 100) + "
        "0.26 * zscore(ts_mean(scl12_sentiment_fast_d1, 10)) + "
        "0.20 * group_zscore(forward_book_value_to_price, industry) + "
        "0.10 * rank(-1 * beta_last_30_days_spy) - "
        "0.10 * ts_rank(volume / adv20, 30)), -1)",
        "gated-revision-event-sales-sentiment",
        "revision_event_value_gate",
        [tight, ind],
        "Uses revision events as the gate while keeping sentiment as a payload overlay.",
    )
    add(
        "rank(group_neutralize(0.32 * ts_rank(capex / assets, 120) + "
        "0.24 * rank(-1 * beta_last_90_days_spy) + "
        "0.18 * rank(-1 * correlation_last_30_days_spy) + "
        "0.16 * ts_rank(free_cash_flow / assets, 120) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "gated-capex-fcf-lowbeta",
        "capex_lowbeta_quality",
        [ind, d16],
        "Tests capex/free-cash-flow quality with low beta and low market-correlation controls.",
    )
    add(
        "rank(group_neutralize(0.30 * rank(-1 * ts_rank(debt_lt / assets, 120)) + "
        "0.24 * ts_rank(gross_profit / assets, 120) + "
        "0.18 * rank(-1 * beta_last_30_days_spy) + "
        "0.16 * rank(-1 * credit_risk_premium_indicator) - "
        "0.12 * ts_rank(returns, 80), sector))",
        "gated-balance-lowdebt-profit",
        "balance_sheet_lowdebt_profit",
        [sector, d16],
        "Uses balance-sheet quality rather than cash-flow-per-cap or EPS.",
    )
    add(
        "rank(humpdecay(group_neutralize(0.32 * ts_rank(forward_sales_to_price, 120) + "
        "0.24 * group_zscore(ts_delta(snt1_d1_analystcoverage, 10), industry) + "
        "0.18 * rank(-1 * ts_rank(pcr_oi_10, 80)) + "
        "0.16 * rank(-1 * correlation_last_30_days_spy) - "
        "0.10 * ts_rank(returns, 90), industry), 0.01))",
        "gated-hump-sales-coverage-pcr",
        "humpdecay_sales_coverage",
        [d8, d16],
        "Tests humpdecay as a path-shaping operator around sales/coverage/PCR.",
    )
    add(
        "trade_when(ts_rank(pcr_oi_10, 60) < 0.45, "
        "rank(0.32 * ts_rank(forward_sales_to_price, 120) + "
        "0.24 * group_rank(ts_rank(rel_ret_cust, 90), industry) + "
        "0.18 * rank(-1 * credit_risk_premium_indicator) + "
        "0.16 * rank(-1 * ts_rank(returns, 120)) + "
        "0.10 * rank(volume / adv20)), -1)",
        "gated-pcr-regime-sales-rel",
        "pcr_regime_relationship_sales",
        [tight, ind],
        "Uses PCR as a regime filter rather than a primary alpha leg.",
    )
    add(
        "rank(group_neutralize(0.28 * rank(-1 * ts_rank(implied_volatility_mean_30, 80)) + "
        "0.26 * ts_rank(forward_sales_to_price, 100) + "
        "0.20 * group_zscore(ts_delta(snt1_d1_analystcoverage, 10), subindustry) + "
        "0.16 * rank(-1 * beta_last_30_days_spy) - "
        "0.10 * ts_rank(returns, 80), industry))",
        "gated-ivmean-sales-coverage",
        "ivmean_coverage_value",
        [ind, d16],
        "Avoids the crowded call/put spread by using IV mean as a small risk leg.",
    )
    add(
        "rank(0.30 * ts_rank(fifty_to_two_hundred_day_price_ratio, 120) + "
        "0.26 * ts_rank(five_year_dividend_growth_rate_2, 120) + "
        "0.18 * rank(-1 * ts_rank(pcr_oi_10, 60)) + "
        "0.14 * rank(-1 * correlation_last_30_days_spy) - "
        "0.12 * ts_rank(returns, 80))",
        "gated-trend-dividend-pcr",
        "trend_dividend_options_overlay",
        [sector, market],
        "Combines a sparse dividend field with longer trend and PCR regime information.",
    )
    add(
        "rank(0.32 * group_rank(ts_rank(rel_ret_supp, 120), industry) + "
        "0.24 * rank(ts_mean(implied_volatility_call_120 - implied_volatility_put_120, 10)) + "
        "0.20 * rank(-1 * ts_rank(pcr_oi_10, 80)) + "
        "0.14 * rank(-1 * beta_last_30_days_spy) - "
        "0.10 * ts_rank(returns, 90))",
        "gated-rel-supplier-iv120-pcr",
        "relationship_options_120",
        [d8, ind],
        "Uses relationship return as the main leg and a different IV tenor as the overlay.",
    )
    add(
        "rank(0.34 * group_rank(rank(last_diff_value(anl4_afv4_eps_mean, 0)), subindustry) + "
        "0.26 * group_rank(rank(last_diff_value(forward_sales_to_price, 0)), industry) + "
        "0.18 * rank(-1 * credit_risk_premium_indicator) + "
        "0.12 * rank(-1 * ts_rank(pcr_oi_10, 60)) - "
        "0.10 * ts_rank(returns, 80))",
        "gated-lastdiff-eps-sales",
        "lastdiff_update_credit",
        [d16, ind],
        "Uses last_diff_value update payloads to alter the path away from static value shells.",
    )
    add(
        "trade_when(ts_rank(abs(close / vwap), 20) > 0.60, "
        "rank(0.30 * group_zscore(ts_rank(rel_ret_cust, 120), industry) + "
        "0.24 * ts_rank(five_year_dividend_growth_rate_2, 120) + "
        "0.18 * rank(-1 * beta_last_30_days_spy) + "
        "0.16 * rank(-1 * ts_rank(pcr_oi_10, 60)) - "
        "0.12 * ts_rank(returns, 100)), -1)",
        "gated-vwap-regime-rel-dividend",
        "vwap_regime_relationship_dividend",
        [tight, sector],
        "Uses vwap dislocation only as a gate around relationship/dividend payloads.",
    )

    return rows


def _load_active_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.is_file():
        return rows
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not raw.strip().startswith("{"):
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        expression = str(item.get("expression") or "").strip()
        if expression:
            rows.append({"expression": expression, "alpha_id": (item.get("alpha_ids") or [None])[0], "metrics": item.get("metrics") or {}})
    return rows


def _nearest(expression: str, active_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = -1.0
    for row in active_rows:
        try:
            sim = compute_similarity(expression, row["expression"])
        except Exception:
            continue
        score = float(sim.get("overall_similarity") or 0.0)
        if score > best_score:
            best_score = score
            best = {
                "alpha_id": row.get("alpha_id"),
                "expression": row.get("expression"),
                "metrics": row.get("metrics"),
                "similarity": sim,
            }
    return best


def _fields(expression: str) -> list[str]:
    try:
        components = extract_components(expression)
    except Exception:
        return []
    return sorted(str(field) for field in components.get("fields", []))


if __name__ == "__main__":
    raise SystemExit(main())
