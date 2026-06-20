"""Build a diversified existing-alpha candidate list for submit-5-more.

The input pool is platform `/users/self/alphas` data already synced to reports.
This script only writes a JSONL candidate file; submission is handled by
submit_wq_existing_until_target.py.
"""

from __future__ import annotations

import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLATFORM = ROOT / "reports" / "wq_active_alpha_map_refresh_20260611_submit5" / "platform_alphas.jsonl"
DEFAULT_ACTIVE = ROOT / "reports" / "wq_active_alpha_map_refresh_20260611_submit5" / "selected_alpha_inventory.jsonl"
DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "existing_diverse_candidates.jsonl"

COMMON_IDENTIFIERS = {
    "rank",
    "ts_rank",
    "group_rank",
    "group_neutralize",
    "ts_backfill",
    "ts_corr",
    "ts_delta",
    "ts_decay_linear",
    "ts_mean",
    "ts_std_dev",
    "power",
    "bucket",
    "range",
    "densify",
    "zscore",
    "normalize",
    "winsorize",
    "industry",
    "subindustry",
    "sector",
    "market",
    "true",
    "false",
}

BLOCKING_CHECKS = {
    "LOW_SHARPE",
    "LOW_FITNESS",
    "LOW_TURNOVER",
    "HIGH_TURNOVER",
    "CONCENTRATED_WEIGHT",
    "LOW_SUB_UNIVERSE_SHARPE",
    "LOW_SUB_UNIVERSE_FITNESS",
}

DOMAIN_PENALTY = {
    "raw_iv": 0.75,
    "options_vol_pcr": 0.35,
    "cashflow_cap_crowded": 0.42,
    "analyst_revision": 0.35,
    "cashflow_noncap": 0.25,
    "model_derivative": 0.08,
    "fundamental_quality": 0.05,
    "dividend_reversal": 0.02,
    "missingness_coverage": 0.12,
    "intraday_micro": 0.10,
    "risk_credit": 0.18,
    "other": 0.16,
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    platform_rows = read_jsonl(Path(args.platform_file))
    active_rows = read_jsonl(Path(args.active_file))
    active_ids = {str(row.get("alpha_id") or row.get("id") or "") for row in active_rows}
    active_exprs = [expression_of(row) for row in active_rows if expression_of(row)]
    active_keys = {canonical(expr) for expr in active_exprs}

    raw_candidates: list[dict[str, Any]] = []
    for row in platform_rows:
        candidate = candidate_from_platform(row, active_ids, active_keys, active_exprs)
        if candidate is not None:
            raw_candidates.append(candidate)

    raw_candidates.sort(
        key=lambda row: (
            -float(row["score"]),
            float(row["max_active_similarity"]),
            -float(row["fitness"]),
            -float(row["sharpe"]),
        )
    )

    selected = select_diverse(raw_candidates, limit=args.limit, per_domain=args.per_domain)
    for index, row in enumerate(selected, start=1):
        row["rank"] = index

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in selected) + "\n",
        encoding="utf-8",
    )
    summary = {
        "ok": True,
        "platform_rows": len(platform_rows),
        "active_rows": len(active_rows),
        "eligible_before_diversity": len(raw_candidates),
        "written": len(selected),
        "output": str(output),
        "domain_counts": counts(row["domain"] for row in selected),
        "top": [
            {
                "rank": row["rank"],
                "alpha_id": row["alpha_id"],
                "domain": row["domain"],
                "score": round(float(row["score"]), 4),
                "fitness": row["fitness"],
                "sharpe": row["sharpe"],
                "max_active_similarity": round(float(row["max_active_similarity"]), 4),
                "tag": row.get("tag"),
            }
            for row in selected[:20]
        ],
    }
    output.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate diversified existing WQ alpha submit candidates")
    parser.add_argument("--platform-file", default=str(DEFAULT_PLATFORM))
    parser.add_argument("--active-file", default=str(DEFAULT_ACTIVE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--per-domain", type=int, default=14)
    return parser.parse_args(argv)


def candidate_from_platform(
    row: dict[str, Any],
    active_ids: set[str],
    active_keys: set[str],
    active_exprs: list[str],
) -> dict[str, Any] | None:
    alpha_id = str(row.get("alpha_id") or row.get("id") or "")
    if not alpha_id or alpha_id in active_ids:
        return None
    if str(row.get("status") or "").upper() != "UNSUBMITTED":
        return None

    expr = expression_of(row)
    if not expr:
        return None
    expr_key = canonical(expr)
    if expr_key in active_keys:
        return None

    sharpe = safe_float(row.get("sharpe"))
    fitness = safe_float(row.get("fitness"))
    turnover = safe_float(row.get("turnover"))
    returns = safe_float(row.get("returns"), default=0.0)
    if sharpe < 1.25 or fitness < 1.0 or turnover < 0.01 or turnover > 0.7:
        return None
    if has_blocking_platform_fail(row):
        return None

    domain = classify_domain(expr)
    max_sim, nearest_id = max_active_similarity(expr, active_exprs)
    if max_sim >= 0.93:
        return None

    score = (
        1.25 * fitness
        + 0.32 * sharpe
        + 1.2 * returns
        - 1.15 * max(0.0, max_sim - 0.68)
        - DOMAIN_PENALTY.get(domain, 0.16)
        + low_use_bonus(expr, domain)
    )

    review = row.get("review_checks") if isinstance(row.get("review_checks"), dict) else {}
    pending = review.get("pending") if isinstance(review.get("pending"), list) else []
    failed = review.get("failed") if isinstance(review.get("failed"), list) else []
    return {
        "alpha_id": alpha_id,
        "expression": expr,
        "domain": domain,
        "score": round(score, 6),
        "sharpe": sharpe,
        "fitness": fitness,
        "returns": returns,
        "turnover": turnover,
        "dateCreated": row.get("dateCreated"),
        "stage": row.get("stage"),
        "max_active_similarity": round(max_sim, 6),
        "nearest_active_expression_index": nearest_id,
        "review_pending": pending,
        "review_failed": failed,
        "settings": row.get("settings") or {},
        "tag": f"existing-diverse-{domain}-{alpha_id}",
    }


def has_blocking_platform_fail(row: dict[str, Any]) -> bool:
    review = row.get("review_checks") if isinstance(row.get("review_checks"), dict) else {}
    failed = {str(item).upper() for item in review.get("failed") or []}
    if failed & {"SELF_CORRELATION", "PROD_CORRELATION"}:
        return True
    checks = row.get("checks")
    if isinstance(checks, list):
        for item in checks:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").upper() in BLOCKING_CHECKS and str(item.get("result") or "").upper() == "FAIL":
                return True
    return False


def select_diverse(rows: list[dict[str, Any]], *, limit: int, per_domain: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    domain_counts: dict[str, int] = {}
    selected_exprs: list[str] = []
    seen_exprs: set[str] = set()
    for row in rows:
        domain = str(row["domain"])
        if domain_counts.get(domain, 0) >= per_domain:
            continue
        key = canonical(row["expression"])
        if key in seen_exprs:
            continue
        if selected_exprs and max(similarity(row["expression"], expr) for expr in selected_exprs) >= 0.9:
            continue
        selected.append(row)
        selected_exprs.append(row["expression"])
        seen_exprs.add(key)
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def classify_domain(expr: str) -> str:
    text = expr.lower()
    if is_raw_iv(text):
        return "raw_iv"
    if "implied_volatility" in text or "pcr_oi" in text:
        return "options_vol_pcr"
    if "dividend" in text:
        return "dividend_reversal"
    if "ts_count_nans" in text:
        return "missingness_coverage"
    if any(token in text for token in ("multi_factor_", "composite_factor", "growth_potential_rank_derivative")):
        return "model_derivative"
    if any(token in text for token in ("cashflow_op / cap", "cashflow_op/cap", "cashflow / cap", "cashflow_fin / cap")):
        return "cashflow_cap_crowded"
    if any(token in text for token in ("actual_cashflow_per_share", "forward_cash_flow", "anl4_af_cfps")):
        return "cashflow_noncap"
    if any(token in text for token in ("operating_income", "capex", "assets", "debt", "ebit", "equity")):
        return "fundamental_quality"
    if any(token in text for token in ("earnings_revision", "earnings_momentum", "anl4_", "actual_eps", "change_in_eps")):
        return "analyst_revision"
    if any(token in text for token in ("credit_risk", "distress", "cash_burn")):
        return "risk_credit"
    if any(token in text for token in ("high - close", "vwap / close", "close / vwap", "ts_corr")):
        return "intraday_micro"
    return "other"


def is_raw_iv(text: str) -> bool:
    stripped = re.sub(r"\s+", "", text)
    raw_patterns = {
        "implied_volatility_call_90-implied_volatility_put_90",
        "implied_volatility_call_120-implied_volatility_put_120",
    }
    if stripped in raw_patterns:
        return True
    return "iv_difference=" in stripped and "bucket(rank(cap)" in stripped and "group_neutralize(iv_difference" in stripped


def low_use_bonus(expr: str, domain: str) -> float:
    text = expr.lower()
    bonus = 0.0
    for token in (
        "actual_dividend_value_quarterly",
        "five_year_dividend_growth_rate_2",
        "cash_burn_rate",
        "debt / assets",
        "ebit / enterprise_value",
        "equity / cap",
        "growth_potential_rank_derivative",
        "multi_factor_static_score_derivative",
        "analyst_revision_rank_derivative",
    ):
        if token in text:
            bonus += 0.05
    if domain in {"dividend_reversal", "model_derivative", "fundamental_quality", "intraday_micro"}:
        bonus += 0.04
    return min(bonus, 0.18)


def max_active_similarity(expr: str, active_exprs: list[str]) -> tuple[float, int | None]:
    best = 0.0
    best_index: int | None = None
    for index, active_expr in enumerate(active_exprs, start=1):
        sim = similarity(expr, active_expr)
        if sim > best:
            best = sim
            best_index = index
    return best, best_index


def similarity(left: str, right: str) -> float:
    left_tokens = fields(left)
    right_tokens = fields(right)
    field_sim = jaccard(left_tokens, right_tokens)
    op_sim = jaccard(operators(left), operators(right))
    text_sim = SequenceMatcher(None, canonical(left), canonical(right)).ratio()
    return 0.48 * text_sim + 0.37 * field_sim + 0.15 * op_sim


def fields(expr: str) -> set[str]:
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr))
    return {token for token in tokens if token not in COMMON_IDENTIFIERS}


def operators(expr: str) -> set[str]:
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr))
    return {token for token in tokens if token in COMMON_IDENTIFIERS}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    return len(left & right) / len(left | right)


def expression_of(row: dict[str, Any]) -> str:
    regular = row.get("regular")
    if isinstance(regular, dict) and regular.get("code"):
        return str(regular["code"]).strip()
    return str(row.get("expression") or "").strip()


def canonical(expr: str) -> str:
    return re.sub(r"\s+", "", expr).lower()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def safe_float(value: Any, *, default: float = -999.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def counts(values: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        key = str(value)
        result[key] = result.get(key, 0) + 1
    return dict(sorted(result.items(), key=lambda item: (-item[1], item[0])))


if __name__ == "__main__":
    raise SystemExit(main())
