"""Family-aware A/B hybrid candidate generation for WQ workflows.

This module only creates candidate expressions. It never calls WQ BRAIN and
never submits alphas; the existing workflow remains responsible for validation,
simulation, review, and submission.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from .expression_parser import normalize_expression
from .wq_expression_utils import expression_component_lists
from .wq_expression_utils import strip_outer_rank as _strip_outer_rank
from .wq_field_groups import (
    ANALYST_FIELDS,
    FUNDAMENTAL_FIELDS,
    LIQUIDITY_FIELDS,
    MODEL_DERIVATIVE_FIELDS,
    OPTION_FIELDS,
    PRICE_FIELDS,
)

SENTIMENT_TERMS = {"news", "sentiment", "buzz", "scl", "snt"}
RISK_TERMS = {"beta", "volatility", "vol", "risk", "std"}


USA3000_PAIR_TARGETS = [
    ("options_positioning", "price_reversal"),
    ("model_derivative", "liquidity_microstructure"),
    ("price_reversal", "options_positioning"),
    ("liquidity_microstructure", "model_derivative"),
    ("options_positioning", "liquidity_microstructure"),
    ("liquidity_microstructure", "options_positioning"),
    ("model_derivative", "price_reversal"),
    ("price_reversal", "model_derivative"),
    ("options_positioning", "fundamental_quality"),
    ("analyst_revision", "liquidity_microstructure"),
    ("analyst_revision", "price_reversal"),
    ("liquidity_microstructure", "analyst_revision"),
    ("liquidity_microstructure", "fundamental_quality"),
    ("fundamental_quality", "liquidity_microstructure"),
    ("fundamental_quality", "price_reversal"),
    ("sentiment_news", "price_reversal"),
    ("risk_defensive", "fundamental_quality"),
    ("unknown", "liquidity_microstructure"),
]

DEFAULT_TEMPLATE_PRIORITY = [
    "usa3000_subindustry_residual_blend",
    "liquidity_weighted_residual",
    "usa3000_industry_liquidity_blend",
    "sector_neutral_modifier",
    "liquidity_gate",
    "regime_modifier",
    "weak_cross_domain_overlay",
]

PAIR_TEMPLATE_PRIORITIES = {
    ("options_positioning", "price_reversal"): [
        "regime_modifier",
        "sector_neutral_modifier",
        "liquidity_weighted_residual",
        "weak_cross_domain_overlay",
        "usa3000_industry_liquidity_blend",
        "usa3000_subindustry_residual_blend",
        "liquidity_gate",
    ],
    ("price_reversal", "options_positioning"): [
        "regime_modifier",
        "sector_neutral_modifier",
        "weak_cross_domain_overlay",
        "liquidity_weighted_residual",
        "usa3000_industry_liquidity_blend",
        "usa3000_subindustry_residual_blend",
        "liquidity_gate",
    ],
    ("model_derivative", "liquidity_microstructure"): [
        "sector_neutral_modifier",
        "usa3000_industry_liquidity_blend",
        "usa3000_subindustry_residual_blend",
        "liquidity_weighted_residual",
        "regime_modifier",
        "weak_cross_domain_overlay",
        "liquidity_gate",
    ],
    ("liquidity_microstructure", "model_derivative"): [
        "sector_neutral_modifier",
        "usa3000_subindustry_residual_blend",
        "usa3000_industry_liquidity_blend",
        "liquidity_weighted_residual",
        "regime_modifier",
        "weak_cross_domain_overlay",
        "liquidity_gate",
    ],
}

DOMAIN_PRIOR = {
    "fundamental_quality": 1.18,
    "analyst_revision": 1.12,
    "options_positioning": 1.05,
    "liquidity_microstructure": 1.00,
    "model_derivative": 0.98,
    "price_reversal": 0.92,
    "risk_defensive": 0.88,
    "sentiment_news": 0.82,
    "unknown": 0.55,
}


@dataclass(frozen=True)
class EvolutionSeed:
    seed_id: str
    expression: str
    domain: str
    family_hash: str
    score: float
    source: str
    tag: str
    alpha_id: str | None
    fields: tuple[str, ...]
    operators: tuple[str, ...]
    row: dict[str, Any]


@dataclass(frozen=True)
class EvolutionPair:
    pair_id: str
    a: EvolutionSeed
    b: EvolutionSeed
    tag_pair: str


def generate_evolutionary_candidates(
    *,
    active_rows: list[dict[str, Any]] | None = None,
    candidate_rows: list[dict[str, Any]] | None = None,
    field_opportunity_rows: list[dict[str, Any]] | None = None,
    repair_rows: list[dict[str, Any]] | None = None,
    target_count: int = 20,
    region: str = "USA",
    universe: str = "TOP3000",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Generate deterministic A/B hybrid candidates from existing workflow memory."""

    if target_count <= 0:
        return [], {"ok": True, "generated": 0, "skipped": True, "reason": "target_count <= 0"}

    seeds = build_seed_pool(
        active_rows=active_rows or [],
        candidate_rows=candidate_rows or [],
        field_opportunity_rows=field_opportunity_rows or [],
        repair_rows=repair_rows or [],
        region=region,
        universe=universe,
    )
    if len(seeds) < 2:
        return [], {
            "ok": True,
            "generated": 0,
            "skipped": True,
            "reason": "not enough distinct seeds",
            "seed_count": len(seeds),
        }

    pairs = sample_diverse_pairs(seeds, n_pairs=max(target_count, len(USA3000_PAIR_TARGETS)))
    candidates = candidates_from_pairs(pairs, target_count=target_count, region=region, universe=universe)
    return candidates, {
        "ok": True,
        "generated": len(candidates),
        "seed_count": len(seeds),
        "pair_count": len(pairs),
        "domain_counts": _domain_counts(seeds),
        "target_count": target_count,
        "region": region,
        "universe": universe,
    }


def build_seed_pool(
    *,
    active_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    field_opportunity_rows: list[dict[str, Any]],
    repair_rows: list[dict[str, Any]],
    region: str,
    universe: str,
) -> list[EvolutionSeed]:
    rows: list[tuple[str, dict[str, Any]]] = []
    rows.extend(("active_inventory", row) for row in active_rows)
    rows.extend(("candidate_memory", row) for row in candidate_rows)
    rows.extend(("community_field_opportunity", row) for row in field_opportunity_rows)
    for repair in repair_rows:
        for expr in repair.get("candidate_expressions") or repair.get("repair_expressions") or []:
            rows.append(("repair_memory", {**repair, "expression": expr}))

    seeds: list[EvolutionSeed] = []
    seen_families: set[str] = set()
    for index, (source_kind, row) in enumerate(rows, start=1):
        expression = _clean_expression(str(row.get("expression") or ""))
        if not expression or ";" in expression:
            continue
        fields, operators = _components(expression)
        if not fields:
            continue
        domain = classify_domain(fields, expression)
        family = family_hash(expression, domain=domain)
        if family in seen_families:
            continue
        seen_families.add(family)
        seeds.append(
            EvolutionSeed(
                seed_id=f"E{index:04d}_{short_hash(source_kind + family, 6)}",
                expression=expression,
                domain=domain,
                family_hash=family,
                score=seed_score(row, domain=domain, source_kind=source_kind, region=region, universe=universe),
                source=source_kind,
                tag=str(row.get("tag") or row.get("alpha_id") or source_kind),
                alpha_id=str(row.get("alpha_id")) if row.get("alpha_id") else None,
                fields=tuple(fields),
                operators=tuple(operators),
                row=row,
            )
        )
    seeds.sort(key=lambda seed: (-seed.score, seed.domain, seed.family_hash))
    return seeds


def sample_diverse_pairs(seeds: list[EvolutionSeed], *, n_pairs: int) -> list[EvolutionPair]:
    by_domain: dict[str, list[EvolutionSeed]] = {}
    for seed in seeds:
        by_domain.setdefault(seed.domain, []).append(seed)
    for domain in by_domain:
        by_domain[domain].sort(key=lambda seed: (-seed.score, seed.family_hash))

    pairs: list[EvolutionPair] = []
    used_pair_hashes: set[str] = set()
    family_usage: dict[str, int] = {}
    targets = _expanded_pair_targets(seeds)

    def pick(domain: str, excluded: set[str]) -> EvolutionSeed | None:
        candidates = [
            seed for seed in by_domain.get(domain, [])
            if seed.family_hash not in excluded
        ]
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda seed: (
                family_usage.get(seed.family_hash, 0),
                _is_active_seed(seed),
                -seed.score,
                seed.family_hash,
            ),
        )[0]

    attempts = 0
    target_index = 0
    while len(pairs) < n_pairs and attempts < max(200, n_pairs * 20):
        attempts += 1
        a_domain, b_domain = targets[target_index % len(targets)]
        target_index += 1
        a = pick(a_domain, excluded=set())
        b = pick(b_domain, excluded={a.family_hash} if a else set())
        if a is None or b is None or a.family_hash == b.family_hash:
            continue
        pair_hash = short_hash(f"{a.family_hash}::{b.family_hash}", 12)
        if pair_hash in used_pair_hashes:
            continue
        used_pair_hashes.add(pair_hash)
        family_usage[a.family_hash] = family_usage.get(a.family_hash, 0) + 1
        family_usage[b.family_hash] = family_usage.get(b.family_hash, 0) + 1
        pairs.append(
            EvolutionPair(
                pair_id=f"P{len(pairs) + 1:04d}_{pair_hash}",
                a=a,
                b=b,
                tag_pair=f"{a.domain}+{b.domain}",
            )
        )
    return pairs


def candidates_from_pairs(
    pairs: list[EvolutionPair],
    *,
    target_count: int,
    region: str,
    universe: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    pair_templates = [(pair, _hybrid_templates(pair)) for pair in pairs]
    max_template_count = max((len(templates) for _, templates in pair_templates), default=0)
    for template_index in range(max_template_count):
        for pair, templates in pair_templates:
            if template_index >= len(templates):
                continue
            mode, expression, rationale = templates[template_index]
            key = normalize_expression(expression)
            if key in seen:
                continue
            seen.add(key)
            family = f"evolutionary_{pair.a.domain}_{pair.b.domain}"
            fields = sorted(set(pair.a.fields) | set(pair.b.fields) | _fields_for_template(mode))
            row = {
                "expression": expression,
                "tag": f"evo-{pair.a.domain}-{pair.b.domain}-{mode}-{short_hash(expression, 6)}",
                "source_family": family,
                "source": "evolutionary_alpha_generator",
                "rationale": rationale,
                "expected_low_corr_reason": (
                    "Family-aware A/B hybrid: A preserves the primary signal while B is a small "
                    "cross-domain modifier, gate, or neutralization helper."
                ),
                "source_fields": fields,
                "mutation_strategy": f"evolutionary_{mode}",
                "parent_alpha_ids": [item for item in (pair.a.alpha_id, pair.b.alpha_id) if item],
                "risk_flags": _risk_flags(pair, mode, region=region, universe=universe),
                "candidate_meta": {
                    "evolutionary": True,
                    "pair_id": pair.pair_id,
                    "tag_pair": pair.tag_pair,
                    "parent_a": _seed_meta(pair.a),
                    "parent_b": _seed_meta(pair.b),
                    "family_hash": family_hash(expression, domain=family),
                    "usa3000_bias": region.upper() == "USA" and universe.upper() == "TOP3000",
                    "template_mode": mode,
                },
            }
            rows.append(row)
            if len(rows) >= target_count:
                return rows
    return rows


def classify_domain(fields: list[str], expression: str) -> str:
    field_set = set(fields)
    text = expression.lower()
    if field_set & OPTION_FIELDS:
        return "options_positioning"
    if field_set & ANALYST_FIELDS or "analyst" in text or "earnings" in text:
        return "analyst_revision"
    if field_set & MODEL_DERIVATIVE_FIELDS:
        return "model_derivative"
    if field_set & FUNDAMENTAL_FIELDS:
        return "fundamental_quality"
    if field_set & LIQUIDITY_FIELDS:
        return "liquidity_microstructure"
    if any(term in text for term in SENTIMENT_TERMS):
        return "sentiment_news"
    if any(term in text for term in RISK_TERMS):
        return "risk_defensive"
    if field_set & PRICE_FIELDS:
        return "price_reversal"
    return "unknown"


def family_hash(expression: str, *, domain: str) -> str:
    text = normalize_expression(expression)
    text = re.sub(r"\d+\.\d+|\d+", "N", text)
    return short_hash(f"{domain}:{text}", 12)


def short_hash(text: str, length: int = 10) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:length]


def seed_score(row: dict[str, Any], *, domain: str, source_kind: str, region: str, universe: str) -> float:
    sharpe = _to_float(row.get("sharpe"))
    fitness = _to_float(row.get("fitness"))
    turnover = _to_float(row.get("turnover"))
    score = DOMAIN_PRIOR.get(domain, 0.50)
    if sharpe is not None:
        score += min(max(sharpe, -1.0), 3.0) * 0.22
    if fitness is not None:
        score += min(max(fitness, -1.0), 2.0) * 0.35
    if turnover is not None:
        if 0.04 <= turnover <= 0.35:
            score += 0.18
        elif turnover > 0.55:
            score -= 0.18
    status = str(row.get("status") or "").upper()
    if status in {"ACTIVE", "SUBMITTED"}:
        score += 0.25
    if source_kind == "community_field_opportunity":
        score += 0.08
    if source_kind == "repair_memory":
        score -= 0.05
    if region.upper() == "USA" and universe.upper() == "TOP3000":
        if domain in {"fundamental_quality", "analyst_revision", "liquidity_microstructure"}:
            score += 0.12
        if domain == "unknown":
            score -= 0.10
    return round(score, 4)


def _hybrid_templates(pair: EvolutionPair) -> list[tuple[str, str, str]]:
    primary, overlay = _primary_overlay_seeds(pair)
    a = _stabilized_seed_expression(primary)
    b = _stabilized_seed_expression(overlay)
    primary_group = "industry" if primary.domain in {"fundamental_quality", "analyst_revision"} else "subindustry"
    templates = [
        (
            "usa3000_subindustry_residual_blend",
            f"rank(0.46 * group_neutralize(rank({a}), subindustry) + 0.34 * group_rank({b}, industry) + "
            "0.12 * rank(ts_corr(vwap, volume, 60)) - 0.08 * ts_rank(returns, 90))",
            "Subindustry residual primary signal plus industry-ranked overlay and slow flow term for broad US coverage.",
        ),
        (
            "liquidity_weighted_residual",
            f"rank(group_neutralize(0.50 * rank({a}) + 0.30 * rank({b}) + "
            "0.20 * rank(ts_corr(vwap, volume, 40)), industry) * (0.80 + 0.20 * rank(volume / adv20)))",
            "Industry residualized blend with a mild liquidity weight to avoid concentrated USA3000 tails.",
        ),
        (
            "usa3000_industry_liquidity_blend",
            f"rank(0.50 * group_rank({a}, {primary_group}) + 0.25 * rank({b}) + "
            "0.15 * rank(volume / adv20) - 0.10 * ts_rank(returns, 45))",
            "USA TOP3000 blend: group-relative primary signal, cross-domain overlay, liquidity breadth, and reversal guard.",
        ),
        (
            "sector_neutral_modifier",
            f"group_neutralize(0.58 * rank({a}) + 0.27 * rank({b}) + 0.15 * rank(volume / adv20), sector)",
            "Neutralize sector exposure and avoid a dominant active-parent multiplicative shape.",
        ),
        (
            "liquidity_gate",
            f"trade_when(rank(volume / adv20) > 0.50, rank(0.55 * rank({a}) + 0.30 * rank({b}) - 0.15 * ts_rank(returns, 60)), -1)",
            "Gate exposure through relative liquidity, which is useful in broad TOP3000 universes.",
        ),
        (
            "regime_modifier",
            f"rank(0.52 * group_rank({a}, {primary_group}) + 0.28 * ts_rank({b}, 50) + "
            "0.10 * rank(volume / adv20) - 0.10 * ts_rank(returns, 60))",
            "Use B as a slow regime overlay while preserving A as the main signal carrier.",
        ),
        (
            "weak_cross_domain_overlay",
            f"rank(0.58 * rank({a}) + 0.32 * rank({b}) - 0.10 * ts_rank(returns, 45))",
            "Heterogeneous overlay with a lower primary weight to reduce self-correlation.",
        ),
    ]
    return _prioritize_templates(pair, templates)


def _prioritize_templates(
    pair: EvolutionPair,
    templates: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    by_mode = {template[0]: template for template in templates}
    priority = PAIR_TEMPLATE_PRIORITIES.get((pair.a.domain, pair.b.domain), DEFAULT_TEMPLATE_PRIORITY)
    ordered = [by_mode[mode] for mode in priority if mode in by_mode]
    ordered.extend(template for template in templates if template[0] not in set(priority))
    return ordered


def _primary_overlay_seeds(pair: EvolutionPair) -> tuple[EvolutionSeed, EvolutionSeed]:
    """Keep real ACTIVE alphas as overlays when a non-active seed is available."""

    if _is_active_seed(pair.a) and not _is_active_seed(pair.b):
        return pair.b, pair.a
    return pair.a, pair.b


def _stabilized_seed_expression(seed: EvolutionSeed) -> str:
    expression = _strip_outer_rank(seed.expression)
    if seed.domain == "options_positioning":
        return f"ts_backfill({expression}, 60)"
    return expression


def _is_active_seed(seed: EvolutionSeed) -> bool:
    status = str(seed.row.get("status") or "").upper()
    return seed.source == "active_inventory" or status in {"ACTIVE", "SUBMITTED"}


def _expanded_pair_targets(seeds: list[EvolutionSeed]) -> list[tuple[str, str]]:
    domains = sorted({seed.domain for seed in seeds})
    targets = [pair for pair in USA3000_PAIR_TARGETS if pair[0] in domains and pair[1] in domains]
    if targets:
        return targets
    fallback: list[tuple[str, str]] = []
    for left in domains:
        for right in domains:
            if left != right:
                fallback.append((left, right))
    return fallback or [("unknown", "unknown")]


def _fields_for_template(mode: str) -> set[str]:
    fields = {"returns"}
    if mode in {
        "usa3000_industry_liquidity_blend",
        "sector_neutral_modifier",
        "liquidity_gate",
        "regime_modifier",
        "liquidity_weighted_residual",
    }:
        fields.update({"adv20", "volume"})
    if mode in {"usa3000_industry_liquidity_blend", "regime_modifier", "usa3000_subindustry_residual_blend"}:
        fields.add("industry")
    if mode == "usa3000_subindustry_residual_blend":
        fields.update({"subindustry", "vwap", "volume"})
    if mode == "liquidity_weighted_residual":
        fields.update({"industry", "vwap"})
    if mode == "sector_neutral_modifier":
        fields.add("sector")
    return fields


def _risk_flags(pair: EvolutionPair, mode: str, *, region: str, universe: str) -> list[str]:
    flags = ["requires fresh self-correlation check"]
    if pair.a.domain == pair.b.domain:
        flags.append("same_domain_pair")
    if mode == "liquidity_gate":
        flags.append("trade_when_turnover_check")
    if _is_active_seed(pair.a) or _is_active_seed(pair.b):
        flags.append("active_parent_used_as_overlay_when_possible")
    if region.upper() == "USA" and universe.upper() == "TOP3000":
        flags.append("usa_top3000_liquidity_and_group_exposure")
    return flags


def _seed_meta(seed: EvolutionSeed) -> dict[str, Any]:
    return {
        "seed_id": seed.seed_id,
        "alpha_id": seed.alpha_id,
        "tag": seed.tag,
        "domain": seed.domain,
        "family_hash": seed.family_hash,
        "score": seed.score,
        "source": seed.source,
        "fields": list(seed.fields),
        "operators": list(seed.operators),
    }


def _domain_counts(seeds: list[EvolutionSeed]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for seed in seeds:
        counts[seed.domain] = counts.get(seed.domain, 0) + 1
    return dict(sorted(counts.items()))


def _components(expression: str) -> tuple[list[str], list[str]]:
    components = expression_component_lists(expression)
    return components["fields"], components["operators"]


def _clean_expression(expression: str) -> str:
    return " ".join(expression.strip().split())


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
