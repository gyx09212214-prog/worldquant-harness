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
    block_rows: list[dict[str, Any]] = []
    for block_file in args.block_files:
        block_rows.extend(read_jsonl(Path(block_file)))
    blocklist = build_blocklist(block_rows, min_correlation=args.block_min_correlation)
    exclude_domains = {str(domain).strip() for domain in args.exclude_domains if str(domain).strip()}
    active_ids = {str(row.get("alpha_id") or row.get("id") or "") for row in active_rows}
    active_exprs = [expression_of(row) for row in active_rows if expression_of(row)]
    active_keys = {canonical(expr) for expr in active_exprs}

    raw_candidates: list[dict[str, Any]] = []
    for row in platform_rows:
        candidate = candidate_from_platform(
            row,
            active_ids,
            active_keys,
            active_exprs,
            blocklist=blocklist,
            max_active_similarity_cutoff=args.max_active_similarity,
            block_field_jaccard=args.block_field_jaccard,
            exclude_domains=exclude_domains,
            max_returns_references=args.max_returns_references,
            max_field_count=args.max_field_count,
        )
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
        "blocklist": {
            "files": [str(path) for path in args.block_files],
            "source_rows": len(block_rows),
            "alpha_ids": len(blocklist["alpha_ids"]),
            "anchor_ids": len(blocklist["anchor_ids"]),
            "self_correlation_signatures": len(blocklist["self_correlation_signatures"]),
            "min_correlation": args.block_min_correlation,
            "field_jaccard": args.block_field_jaccard,
        },
        "max_active_similarity": args.max_active_similarity,
        "exclude_domains": sorted(exclude_domains),
        "max_returns_references": args.max_returns_references,
        "max_field_count": args.max_field_count,
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
    parser.add_argument("--block-files", nargs="*", default=[])
    parser.add_argument("--block-min-correlation", type=float, default=0.70)
    parser.add_argument("--block-field-jaccard", type=float, default=0.72)
    parser.add_argument("--max-active-similarity", type=float, default=0.93)
    parser.add_argument("--exclude-domains", nargs="*", default=[])
    parser.add_argument("--max-returns-references", type=int, default=-1)
    parser.add_argument("--max-field-count", type=int, default=0)
    return parser.parse_args(argv)


def candidate_from_platform(
    row: dict[str, Any],
    active_ids: set[str],
    active_keys: set[str],
    active_exprs: list[str],
    *,
    blocklist: dict[str, Any] | None = None,
    max_active_similarity_cutoff: float = 0.93,
    block_field_jaccard: float = 0.72,
    exclude_domains: set[str] | None = None,
    max_returns_references: int = -1,
    max_field_count: int = 0,
) -> dict[str, Any] | None:
    blocklist = blocklist or empty_blocklist()
    exclude_domains = exclude_domains or set()
    alpha_id = str(row.get("alpha_id") or row.get("id") or "")
    if not alpha_id or alpha_id in active_ids:
        return None
    if alpha_id in blocklist["alpha_ids"] or alpha_id in blocklist["anchor_ids"]:
        return None
    if str(row.get("status") or "").upper() != "UNSUBMITTED":
        return None

    expr = expression_of(row)
    if not expr:
        return None
    expr_key = canonical(expr)
    if expr_key in active_keys:
        return None
    if max_returns_references >= 0 and returns_reference_count(expr) > max_returns_references:
        return None
    expr_fields = fields(expr)
    if max_field_count > 0 and len(expr_fields) > max_field_count:
        return None
    domain = classify_domain(expr)
    if domain in exclude_domains:
        return None
    if blocklist_expression_match(expr, domain, blocklist, field_jaccard_cutoff=block_field_jaccard):
        return None

    sharpe = safe_float(row.get("sharpe"))
    fitness = safe_float(row.get("fitness"))
    turnover = safe_float(row.get("turnover"))
    returns = safe_float(row.get("returns"), default=0.0)
    if sharpe < 1.25 or fitness < 1.0 or turnover < 0.01 or turnover > 0.7:
        return None
    if has_blocking_platform_fail(row):
        return None

    max_sim, nearest_id = max_active_similarity(expr, active_exprs)
    if max_sim >= max_active_similarity_cutoff:
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


def empty_blocklist() -> dict[str, Any]:
    return {
        "alpha_ids": set(),
        "anchor_ids": set(),
        "expression_keys": set(),
        "self_correlation_signatures": [],
    }


def build_blocklist(rows: list[dict[str, Any]], *, min_correlation: float = 0.70) -> dict[str, Any]:
    blocklist = empty_blocklist()
    for row in rows:
        for alpha_id in ids_from_row(row):
            blocklist["alpha_ids"].add(alpha_id)

        correlated_records = self_correlated_records(row, min_correlation=min_correlation)
        for record in correlated_records:
            anchor_id = self_correlated_record_id(record)
            if anchor_id:
                blocklist["anchor_ids"].add(anchor_id)

        expr = expression_of(row)
        if not expr:
            continue
        blocklist["expression_keys"].add(canonical(expr))
        if correlated_records or is_self_correlation_failure(row, min_correlation=min_correlation):
            blocklist["self_correlation_signatures"].append({
                "domain": classify_domain(expr),
                "fields": tuple(sorted(fields(expr))),
                "operators": tuple(sorted(operators(expr))),
                "expression_key": canonical(expr),
            })
    return blocklist


def ids_from_row(row: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("alpha_id", "id", "platform_alpha_id"):
        value = row.get(key)
        if value:
            ids.add(str(value))
    meta = row.get("candidate_meta")
    if isinstance(meta, dict):
        for key in ("alpha_id", "id", "platform_alpha_id"):
            value = meta.get(key)
            if value:
                ids.add(str(value))
    parent_ids = row.get("parent_alpha_ids")
    if isinstance(parent_ids, list):
        ids.update(str(value) for value in parent_ids if value)
    return ids


def blocklist_expression_match(
    expr: str,
    domain: str,
    blocklist: dict[str, Any],
    *,
    field_jaccard_cutoff: float,
) -> bool:
    expr_key = canonical(expr)
    if expr_key in blocklist["expression_keys"]:
        return True
    expr_fields = fields(expr)
    expr_operators = operators(expr)
    for signature in blocklist["self_correlation_signatures"]:
        blocked_fields = set(signature.get("fields") or [])
        if not blocked_fields:
            continue
        if expr_fields == blocked_fields:
            return True
        if str(signature.get("domain") or "") != domain:
            continue
        if len(expr_fields) < 3 or len(blocked_fields) < 3:
            continue
        field_overlap = jaccard(expr_fields, blocked_fields)
        if field_overlap < field_jaccard_cutoff:
            continue
        blocked_operators = set(signature.get("operators") or [])
        operator_overlap = jaccard(expr_operators, blocked_operators)
        if operator_overlap >= 0.20 or field_overlap >= 0.90:
            return True
    return False


def is_self_correlation_failure(row: dict[str, Any], *, min_correlation: float) -> bool:
    if self_correlated_records(row, min_correlation=min_correlation):
        return True
    status = str(row.get("final_status") or row.get("status") or "").upper()
    if "SC_FAIL" in status or "SELF_CORRELATION" in status:
        return True
    for key in ("failure_kind", "presubmit_reject_reason", "submit_reject_reason", "detail", "reason"):
        if "SELF_CORRELATION" in str(row.get(key) or "").upper():
            return True
    review = row.get("review_checks") if isinstance(row.get("review_checks"), dict) else {}
    failed = {str(item).upper() for item in review.get("failed") or []}
    if "SELF_CORRELATION" in failed:
        return True
    sc_result = str(row.get("sc_result") or "").upper()
    sc_value = safe_float(row.get("sc_value"), default=-999.0)
    return sc_result in {"FAIL", "FAILED"} or sc_value >= min_correlation


def self_correlated_records(row: dict[str, Any], *, min_correlation: float) -> list[Any]:
    records: list[Any] = []
    for payload in self_correlation_payloads(row):
        payload_records = payload.get("records")
        if not isinstance(payload_records, list):
            continue
        schema = payload.get("schema") if isinstance(payload.get("schema"), dict) else {}
        for record in payload_records:
            correlation = self_correlated_record_correlation(record, schema)
            if correlation >= min_correlation:
                records.append(record)
    return records


def self_correlation_payloads(value: Any) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    if isinstance(value, dict):
        direct = value.get("selfCorrelated")
        if isinstance(direct, dict):
            payloads.append(direct)
        for child in value.values():
            payloads.extend(self_correlation_payloads(child))
    elif isinstance(value, list):
        for item in value:
            payloads.extend(self_correlation_payloads(item))
    return payloads


def self_correlated_record_id(record: Any) -> str | None:
    if isinstance(record, dict):
        for key in ("id", "alpha_id", "alphaId"):
            value = record.get(key)
            if value:
                return str(value)
        return None
    if isinstance(record, list):
        for item in record:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def self_correlated_record_correlation(record: Any, schema: dict[str, Any]) -> float:
    if isinstance(record, dict):
        for key in ("correlation", "value", "score"):
            value = safe_float(record.get(key), default=-999.0)
            if -1.0 <= value <= 1.0:
                return abs(value)
        return -999.0

    if not isinstance(record, list):
        return -999.0

    names = schema_property_names(schema)
    for index, name in enumerate(names):
        if index >= len(record):
            continue
        if "correlation" in name or name in {"value", "score"}:
            value = safe_float(record[index], default=-999.0)
            if -1.0 <= value <= 1.0:
                return abs(value)

    numeric_values = []
    for item in record[1:]:
        value = safe_float(item, default=-999.0)
        if -1.0 <= value <= 1.0:
            numeric_values.append(abs(value))
    return max(numeric_values) if numeric_values else -999.0


def schema_property_names(schema: dict[str, Any]) -> list[str]:
    properties = schema.get("properties")
    if not isinstance(properties, list):
        return []
    names: list[str] = []
    for item in properties:
        if isinstance(item, dict):
            names.append(str(item.get("name") or item.get("title") or "").lower())
        else:
            names.append(str(item).lower())
    return names


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


def returns_reference_count(expr: str) -> int:
    return len(re.findall(r"\breturns\b", expr, flags=re.IGNORECASE))


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
