"""Durable field-diversity gates for live WorldQuant submissions.

The gate deliberately uses only candidate metadata and the expression's field
set, so it can reject crowded candidates before a simulation or submit POST.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .artifact_io import read_json, utc_now, write_json
from .expression_parser import normalize_expression
from .wq_expression_utils import expression_fields

POLICY_VERSION = "wq-field-diversity-v1"

GROUPING_FIELDS = {
    "country",
    "exchange",
    "industry",
    "market",
    "sector",
    "subindustry",
}
MARKET_HELPER_FIELDS = {
    "cap",
    "close",
    "high",
    "low",
    "open",
    "returns",
    "volume",
    "vwap",
}


@dataclass(frozen=True)
class DiversityPolicy:
    """Hard limits for one accepted-goal inventory."""

    policy_version: str = POLICY_VERSION
    max_new_weighted_jaccard: float = 0.60
    max_legacy_weighted_jaccard: float = 0.75
    max_core_signature_count: int = 2
    max_research_field_count: int = 3
    max_market_helper_field_count: int = 10
    max_family_count: int = 4
    min_final_family_count: int = 8
    research_field_weight: float = 1.0
    market_helper_weight: float = 0.5
    grouping_field_weight: float = 0.25

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def field_role(field: str) -> str:
    value = str(field or "").strip().lower()
    if value in GROUPING_FIELDS:
        return "grouping"
    if value in MARKET_HELPER_FIELDS or re.fullmatch(r"adv\d+", value):
        return "market_helper"
    return "research"


def field_weight(field: str, policy: DiversityPolicy | None = None) -> float:
    policy = policy or DiversityPolicy()
    role = field_role(field)
    if role == "grouping":
        return policy.grouping_field_weight
    if role == "market_helper":
        return policy.market_helper_weight
    return policy.research_field_weight


def weighted_field_jaccard(
    left: Iterable[str],
    right: Iterable[str],
    *,
    policy: DiversityPolicy | None = None,
) -> float:
    """Return weighted intersection / weighted union for two field sets."""

    policy = policy or DiversityPolicy()
    left_set = {str(value).strip().lower() for value in left if str(value).strip()}
    right_set = {str(value).strip().lower() for value in right if str(value).strip()}
    union = left_set | right_set
    if not union:
        return 0.0
    numerator = sum(field_weight(field, policy) for field in left_set & right_set)
    denominator = sum(field_weight(field, policy) for field in union)
    return round(numerator / denominator, 6) if denominator else 0.0


def core_signature(row: dict[str, Any]) -> str:
    """Build a stable strategy-core signature, ignoring terminal rank bands.

    Candidate builders may provide ``diversity_core_signature`` explicitly.
    Existing partition builders already identify a common seed Alpha, which is
    the strongest available declaration that sibling masks share one core.
    The final fallback canonicalizes the full formula.
    """

    explicit = str(row.get("diversity_core_signature") or "").strip()
    if explicit:
        return normalize_expression(explicit)
    design = row.get("partition_design") if isinstance(row.get("partition_design"), dict) else {}
    seed_alpha_id = str(design.get("seed_alpha_id") or "").strip()
    if seed_alpha_id:
        return f"seed-alpha:{seed_alpha_id}"
    expression = normalize_expression(str(row.get("expression") or ""))
    return _strip_terminal_simple_rank_interval(expression)


def infer_family(row: dict[str, Any], fields: Iterable[str] | None = None) -> str:
    explicit = str(row.get("diversity_family") or "").strip().lower()
    if explicit:
        return explicit
    values = sorted({str(value).lower() for value in (fields or expression_fields(str(row.get("expression") or "")))})
    research = [value for value in values if field_role(value) == "research"]
    def has(*needles: str) -> bool:
        return any(needle in field for field in research for needle in needles)

    # These are deliberately broad, auditable data domains.  Falling back to
    # arbitrary field prefixes would make the minimum-family rule too easy to
    # game (for example, treating every vendor prefix as a separate family).
    if has("anl", "analyst", "estimate", "forecast", "guidance", "consensus", "recommend", "revision"):
        return "analyst"
    if has("option", "implied_volatility", "pcr_", "put_", "call_"):
        return "options"
    if has("sentiment", "news", "social", "buzz", "media", "snt", "scl12", "nws"):
        return "sentiment"
    if has("beta", "risk", "correlation", "volatility", "systematic", "distress"):
        return "risk"
    if has("rp_", "event", "earnings", "announcement", "surprise"):
        return "event"
    if has("institution", "ownership", "short_interest", "insider"):
        return "institutional"
    if has("macro", "economic", "yield_curve", "interest_rate", "fx_"):
        return "macro"
    if has("actual_"):
        return "reported_actuals"
    if has("cashflow", "cash_flow", "cfo", "free_cash", "operating_cash"):
        return "cashflow"
    if has("value", "valuation", "bookvalue", "enterprise", "price_to", "forward_"):
        return "valuation"
    if has("asset", "debt", "equity", "liabilit", "inventory", "book_leverage", "cash"):
        return "balance_sheet"
    if has("profit", "margin", "roe", "roa", "revenue", "sales", "ebit", "income", "dividend"):
        return "profitability"
    if has("composite", "multi_factor", "module_score", "model"):
        return "model_factor"
    if has("pv13", "page_rank", "network", "custret"):
        return "network_alternative"
    if has("price_ratio", "momentum", "abnormal", "relative_strength"):
        return "price_technical"
    if has("fn_"):
        return "fundamental_detail"
    if research:
        return "other_research"
    return "market_technical"


def build_diversity_record(
    row: dict[str, Any],
    *,
    alpha_id: str | None = None,
    candidate_uid: str | None = None,
    accepted_at: str | None = None,
) -> dict[str, Any]:
    expression = str(row.get("expression") or "")
    fields = sorted(set(expression_fields(expression)))
    roles = {field: field_role(field) for field in fields}
    return {
        "alpha_id": str(alpha_id or row.get("alpha_id") or "").strip() or None,
        "candidate_uid": str(candidate_uid or row.get("candidate_uid") or "").strip() or None,
        "tag": row.get("tag"),
        "source_family": row.get("source_family"),
        "diversity_family": infer_family(row, fields),
        "expression": expression,
        "fields": fields,
        "field_roles": roles,
        "field_signature": "|".join(fields),
        "core_signature": core_signature(row),
        "accepted_at": accepted_at or row.get("accepted_at") or utc_now(),
        "sc_value": row.get("sc_value"),
        "sharpe": row.get("sharpe"),
        "fitness": row.get("fitness"),
        "turnover": row.get("turnover"),
    }


def evaluate_diversity(
    candidate: dict[str, Any],
    accepted: list[dict[str, Any]],
    legacy: list[dict[str, Any]],
    *,
    policy: DiversityPolicy | None = None,
) -> dict[str, Any]:
    policy = policy or DiversityPolicy()
    record = build_diversity_record(candidate)
    fields = record["fields"]
    reasons: list[str] = []
    if not fields:
        reasons.append("diversity_fields_missing")

    signature_count = sum(record["field_signature"] == row.get("field_signature") for row in accepted)
    if signature_count:
        reasons.append("duplicate_new_field_signature")

    core_count = sum(record["core_signature"] == row.get("core_signature") for row in accepted)
    if core_count >= policy.max_core_signature_count:
        reasons.append("core_signature_capacity_reached")

    research_counts = Counter(
        field
        for row in accepted
        for field in row.get("fields") or []
        if field_role(field) == "research"
    )
    market_counts = Counter(
        field
        for row in accepted
        for field in row.get("fields") or []
        if field_role(field) == "market_helper"
    )
    research_capacity_fields = sorted(
        field for field in fields
        if field_role(field) == "research" and research_counts[field] >= policy.max_research_field_count
    )
    market_capacity_fields = sorted(
        field for field in fields
        if field_role(field) == "market_helper" and market_counts[field] >= policy.max_market_helper_field_count
    )
    if research_capacity_fields:
        reasons.append("research_field_capacity_reached")
    if market_capacity_fields:
        reasons.append("market_helper_field_capacity_reached")

    family_counts = Counter(str(row.get("diversity_family") or "") for row in accepted)
    family_count = family_counts[record["diversity_family"]]
    if family_count >= policy.max_family_count:
        reasons.append("diversity_family_capacity_reached")

    new_overlaps = _overlaps(record, accepted, policy)
    legacy_overlaps = _overlaps(record, legacy, policy)
    max_new = max((item["weighted_jaccard"] for item in new_overlaps), default=0.0)
    max_legacy = max((item["weighted_jaccard"] for item in legacy_overlaps), default=0.0)
    if max_new > policy.max_new_weighted_jaccard:
        reasons.append("new_inventory_weighted_jaccard_exceeded")
    if max_legacy > policy.max_legacy_weighted_jaccard:
        reasons.append("legacy_inventory_weighted_jaccard_exceeded")

    return {
        "ok": not reasons,
        "policy_version": policy.policy_version,
        "reasons": reasons,
        "record": record,
        "counts_before": {
            "accepted": len(accepted),
            "legacy": len(legacy),
            "field_signature": signature_count,
            "core_signature": core_count,
            "diversity_family": family_count,
            "research_fields": {field: research_counts[field] for field in fields if field_role(field) == "research"},
            "market_helper_fields": {field: market_counts[field] for field in fields if field_role(field) == "market_helper"},
        },
        "capacity_fields": {
            "research": research_capacity_fields,
            "market_helper": market_capacity_fields,
        },
        "max_new_weighted_jaccard": max_new,
        "max_legacy_weighted_jaccard": max_legacy,
        "nearest_new": new_overlaps[:3],
        "nearest_legacy": legacy_overlaps[:3],
    }


def load_diversity_ledger(path: str) -> dict[str, Any]:
    payload = read_json(path)
    accepted = payload.get("accepted") if isinstance(payload.get("accepted"), list) else []
    return {
        "schema_version": 1,
        "policy_version": str(payload.get("policy_version") or POLICY_VERSION),
        "accepted": [row for row in accepted if isinstance(row, dict)],
        "updated_at": payload.get("updated_at"),
    }


def save_diversity_ledger(
    path: str,
    accepted: list[dict[str, Any]],
    *,
    policy: DiversityPolicy | None = None,
) -> None:
    policy = policy or DiversityPolicy()
    write_json(
        path,
        {
            "schema_version": 1,
            "policy_version": policy.policy_version,
            "updated_at": utc_now(),
            "policy": policy.to_dict(),
            "accepted_count": len(accepted),
            "accepted": accepted,
        },
    )


def merge_diversity_records(
    existing: list[dict[str, Any]],
    incoming: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Idempotently merge records, preferring alpha id then candidate id."""

    output = list(existing)
    keys = {_record_key(row) for row in output if _record_key(row)}
    for row in incoming:
        key = _record_key(row)
        if not key or key in keys:
            continue
        output.append(row)
        keys.add(key)
    return output


def final_diversity_summary(
    accepted: list[dict[str, Any]],
    *,
    policy: DiversityPolicy | None = None,
) -> dict[str, Any]:
    policy = policy or DiversityPolicy()
    family_counts = Counter(str(row.get("diversity_family") or "") for row in accepted)
    field_counts = Counter(field for row in accepted for field in row.get("fields") or [])
    signatures = [str(row.get("field_signature") or "") for row in accepted]
    pairwise = [
        weighted_field_jaccard(left.get("fields") or [], right.get("fields") or [], policy=policy)
        for index, left in enumerate(accepted)
        for right in accepted[index + 1:]
    ]
    return {
        "accepted_count": len(accepted),
        "family_count": len([family for family in family_counts if family]),
        "family_minimum_met": len([family for family in family_counts if family]) >= policy.min_final_family_count,
        "family_counts": dict(sorted(family_counts.items())),
        "field_counts": dict(sorted(field_counts.items())),
        "field_signature_unique_count": len(set(signatures)),
        "field_signature_duplicate_count": len(signatures) - len(set(signatures)),
        "pairwise_weighted_jaccard_max": max(pairwise, default=0.0),
        "pairwise_weighted_jaccard_mean": round(sum(pairwise) / len(pairwise), 6) if pairwise else 0.0,
    }


def _overlaps(
    record: dict[str, Any],
    inventory: list[dict[str, Any]],
    policy: DiversityPolicy,
) -> list[dict[str, Any]]:
    rows = [
        {
            "alpha_id": other.get("alpha_id"),
            "candidate_uid": other.get("candidate_uid"),
            "tag": other.get("tag"),
            "field_signature": other.get("field_signature"),
            "weighted_jaccard": weighted_field_jaccard(record.get("fields") or [], other.get("fields") or [], policy=policy),
        }
        for other in inventory
    ]
    return sorted(rows, key=lambda item: (-item["weighted_jaccard"], str(item.get("alpha_id") or item.get("tag") or "")))


def _record_key(row: dict[str, Any]) -> str:
    alpha_id = str(row.get("alpha_id") or "").strip()
    if alpha_id:
        return f"alpha:{alpha_id}"
    candidate_uid = str(row.get("candidate_uid") or "").strip()
    return f"candidate:{candidate_uid}" if candidate_uid else ""


def _strip_terminal_simple_rank_interval(expression: str) -> str:
    # Handles the common ``core * ((rank(field)>lo)*(rank(field)<hi))`` form.
    match = re.match(
        r"^(?P<core>.+)\*\(\(rank\((?P<field>[a-z_][a-z0-9_]*)\)(?:>|>=)-?[0-9.]+\)"
        r"\*\(rank\((?P=field)\)(?:<|<=)-?[0-9.]+\)\)\)?$",
        expression,
    )
    if not match:
        return expression
    return match.group("core").strip()
