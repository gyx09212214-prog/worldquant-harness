"""Build reusable idea memory from WQ Community triage output.

The script is intentionally deterministic and local-only. It reads
triage_records.jsonl produced by scripts/triage_wq_community.py and writes
theme clusters, source indexes, theme combinations, candidate recipes, and a
compact markdown report for downstream WQ factor mining.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ThemeRule:
    theme_id: str
    label: str
    logic: str
    candidate_policy: str
    keywords: tuple[str, ...]
    field_keywords: tuple[str, ...] = ()
    operator_keywords: tuple[str, ...] = ()
    risk_keywords: tuple[str, ...] = ()
    recipes: tuple[str, ...] = ()


THEMES: tuple[ThemeRule, ...] = (
    ThemeRule(
        theme_id="robustness_turnover",
        label="Robustness / fitness / turnover rules",
        logic="Treat stability, turnover, live/OOS behavior, and overfitting comments as candidate gating rules.",
        candidate_policy="Use for filtering and budget allocation; do not generate standalone expressions from this theme.",
        keywords=(
            "fitness",
            "turnover",
            "stable",
            "stability",
            "robust",
            "robustness",
            "overfit",
            "overfitting",
            "live",
            "out of sample",
            "oos",
            "drawdown",
            "margin",
            "low turnover",
            "high turnover",
            "稳健",
            "换手",
            "过拟合",
        ),
        risk_keywords=("low_turnover", "high_turnover"),
        recipes=(
            "prefer slow anchor + smoothing",
            "decay_linear(rank(signal), 3-8)",
            "stop high-turnover or low-turnover families early",
        ),
    ),
    ThemeRule(
        theme_id="correlation_similarity",
        label="Correlation / similarity avoidance",
        logic="Correlation failures need field-family or operator-family changes; simple window tweaks are usually not enough.",
        candidate_policy="Use as a hard constraint. Graylist direct forum templates unless a new orthogonal field family is introduced.",
        keywords=(
            "correlation",
            "self correlation",
            "prod correlation",
            "similarity",
            "similar",
            "diversify",
            "orthogonal",
            "decorrel",
            "redundant",
            "duplicate",
            "corr",
            "相关",
            "相似",
            "去相关",
        ),
        risk_keywords=("correlation_risk",),
        recipes=(
            "require field-family change",
            "require operator-family change",
            "keep ledger similarity cutoff strict",
        ),
    ),
    ThemeRule(
        theme_id="fundamental_value_quality",
        label="Fundamental value / quality anchors",
        logic="Use cash-flow, sales, profits, EPS, forward value, and quality fields as slow anchors, then add lower-correlation overlays.",
        candidate_policy="Slow anchor weight 0.45-0.65, overlay 0.20-0.35, return reversal 0.15-0.25.",
        keywords=(
            "fundamental",
            "value",
            "quality",
            "cash flow",
            "cashflow",
            "sales",
            "revenue",
            "earnings",
            "eps",
            "income",
            "profit",
            "assets",
            "book",
            "enterprise value",
            "valuation",
            "估值",
            "基本面",
            "质量",
            "现金流",
            "利润",
            "收入",
        ),
        field_keywords=(
            "cashflow",
            "cash_flow",
            "sales",
            "revenue",
            "income",
            "eps",
            "assets",
            "enterprise_value",
            "equity",
            "cap",
            "book",
            "forward",
        ),
        recipes=(
            "ts_rank(cashflow_op / enterprise_value, 80)",
            "ts_rank(actual_sales_value_quarterly / assets, 60)",
            "group_rank(value_ratio, industry)",
        ),
    ),
    ThemeRule(
        theme_id="missingness_coverage",
        label="Missingness / coverage signals",
        logic="Treat missingness, backfill behavior, and coverage changes as information-flow or liquidity-regime proxies.",
        candidate_policy="Run small probes first. Stop the family if operators, fields, or coverage checks fail repeatedly.",
        keywords=(
            "missing",
            "nan",
            "nans",
            "null",
            "coverage",
            "covered",
            "sparse",
            "backfill",
            "availability",
            "absence",
            "ghost signal",
            "缺失",
            "覆盖",
            "空值",
        ),
        operator_keywords=("ts_count_nans", "is_nan", "if_else"),
        risk_keywords=("platform_limit",),
        recipes=(
            "ts_delta(ts_count_nans(FIELD, 240), 20)",
            "group_rank(..., subindustry)",
            "blend with -ts_rank(returns, 120) and ts_corr(vwap, volume, 40)",
        ),
    ),
    ThemeRule(
        theme_id="sentiment_news_revision",
        label="Sentiment / news / analyst revision overlays",
        logic="Use news, sentiment, buzz, and revision fields as information-change overlays instead of standalone fast templates.",
        candidate_policy="Smooth sentiment/revision first, then blend with a slow anchor and return reversal.",
        keywords=(
            "sentiment",
            "news",
            "buzz",
            "revision",
            "analyst",
            "rating",
            "recommendation",
            "estimate",
            "earnings revision",
            "情绪",
            "新闻",
            "分析师",
            "修正",
            "预期",
        ),
        field_keywords=("scl12", "snt", "sentiment", "buzz", "revision", "analyst", "recommendation", "estimate"),
        recipes=(
            "ts_rank(scl12_sentiment_fast_d1, 20)",
            "ts_rank(snt1_d1_netearningsrevision, 60)",
            "blend with value anchor and return reversal",
        ),
    ),
    ThemeRule(
        theme_id="internal_group_compare",
        label="Internal industry/group comparison",
        logic="Use group_rank, group_zscore, and group_neutralize inside the expression to reduce sector structure before platform neutralization.",
        candidate_policy="Use mainly on slow anchors or micro overlays; avoid excessive double neutralization.",
        keywords=(
            "industry",
            "sector",
            "subindustry",
            "group rank",
            "group_rank",
            "group zscore",
            "group_zscore",
            "group neutralize",
            "group_neutralize",
            "neutralize",
            "neutralization",
            "行业",
            "中性化",
            "组内",
        ),
        operator_keywords=("group_rank", "group_zscore", "group_neutralize", "group_mean"),
        recipes=(
            "group_rank(ts_rank(FIELD_RATIO, 60), industry)",
            "group_zscore(MICRO_SHOCK, industry)",
            "test MARKET/INDUSTRY before SUBINDUSTRY",
        ),
    ),
    ThemeRule(
        theme_id="regime_trade_when",
        label="Conditional regime / trade_when",
        logic="Use trade_when or explicit regime conditions to control holding days and reduce noisy daily rebalancing.",
        candidate_policy="Monitor long/short count, turnover, and subuniverse sharply. Stop when coverage collapses.",
        keywords=(
            "trade_when",
            "regime",
            "conditional",
            "condition",
            "market regime",
            "rebalance",
            "trigger",
            "hold",
            "持仓",
            "状态",
            "条件",
            "再平衡",
        ),
        operator_keywords=("trade_when", "where"),
        recipes=(
            "trade_when(volume > adv20, rank(BASE_SIGNAL), -1)",
            "trade_when(volatility_condition, rank(BASE_SIGNAL), -1)",
        ),
    ),
    ThemeRule(
        theme_id="field_update_event",
        label="Field update / event cycle",
        logic="Convert fundamental, analyst, and sentiment field changes, stability, and update frequency into event signals.",
        candidate_policy="For quarterly or slow fields prefer 22/63/252 windows and avoid dense window sweeps.",
        keywords=(
            "update",
            "event",
            "announcement",
            "change",
            "delta",
            "surprise",
            "quarterly",
            "report",
            "release",
            "更新",
            "事件",
            "公告",
            "变化",
            "财报",
            "季度",
        ),
        operator_keywords=("ts_delta", "ts_av_diff"),
        recipes=(
            "rank(ts_delta(FIELD, 5))",
            "rank(ts_std(FIELD, 22 or 63))",
            "rank(ts_delta(ts_mean(FIELD, 3), 5))",
        ),
    ),
    ThemeRule(
        theme_id="intraday_volume_shock",
        label="Intraday amplitude / volume shock overlays",
        logic="Use high-low range, volume/adv20, and short-term returns to capture disagreement and liquidity shocks.",
        candidate_policy="Use only as an overlay unless it proves low correlation and stable turnover.",
        keywords=(
            "intraday",
            "amplitude",
            "range",
            "high-low",
            "volume shock",
            "volume",
            "adv20",
            "liquidity",
            "vwap",
            "日内",
            "振幅",
            "量能",
            "成交量",
            "流动性",
        ),
        field_keywords=("high", "low", "volume", "adv20", "vwap"),
        recipes=(
            "rank((high - low) / close)",
            "rank(volume / adv20)",
            "blend range shock with slow value anchor",
        ),
    ),
)


COURSE_PATTERNS = (
    "course",
    "homework",
    "assignment",
    "iqc",
    "learn",
    "作业",
    "课程",
    "问卷",
    "零基础",
    "专辑",
)


RECIPE_ROWS: tuple[dict[str, Any], ...] = (
    {
        "recipe_id": "missingness_probe_market",
        "source_theme": "missingness_coverage",
        "max_initial_sims": 6,
        "neutralization": "MARKET",
        "fields": [
            "implied_volatility_mean_30",
            "anl4_af_eps_value",
            "actual_sales_value_quarterly",
            "scl12_sentiment_fast_d1",
            "snt1_d1_netearningsrevision",
        ],
        "template": "rank(0.50 * group_rank(ts_delta(ts_count_nans(FIELD, 240), 20), subindustry) + 0.30 * rank(-1 * ts_rank(returns, 120)) + 0.20 * rank(ts_corr(vwap, volume, 40)))",
        "stop_if": ["operator_rejected", "coverage_or_subuniverse_fail_on_first_3"],
    },
    {
        "recipe_id": "industry_rank_value_anchor",
        "source_theme": "internal_group_compare",
        "max_initial_sims": 12,
        "neutralization": "INDUSTRY_then_MARKET",
        "fields": [
            "cashflow_op / enterprise_value",
            "actual_sales_value_quarterly / assets",
            "anl4_af_eps_value / close",
            "forward_cash_flow_to_price",
        ],
        "template": "rank(0.55 * group_rank(ts_rank(FIELD_RATIO, 80), industry) + 0.25 * rank(ts_corr(vwap, volume, 40)) - 0.20 * ts_rank(returns, 40))",
        "stop_if": ["similarity_to_active_or_rejected_ge_0.70"],
    },
    {
        "recipe_id": "update_event_subindustry",
        "source_theme": "field_update_event",
        "max_initial_sims": 12,
        "neutralization": "SUBINDUSTRY",
        "fields": [
            "anl4_af_eps_value",
            "actual_sales_value_quarterly",
            "cashflow_op",
            "forward_sales_to_price",
            "scl12_sentiment_fast_d1",
        ],
        "template": "rank(0.45 * rank(ts_delta(FIELD, 5)) + 0.35 * rank(ts_std(FIELD, 22)) - 0.20 * ts_rank(returns, 20))",
        "stop_if": ["low_turnover_repeated", "field_unsupported"],
    },
    {
        "recipe_id": "trade_when_slow_anchor",
        "source_theme": "regime_trade_when",
        "max_initial_sims": 8,
        "neutralization": "MARKET",
        "fields": [
            "cashflow_op / cap",
            "anl4_af_eps_value / close",
            "actual_sales_value_quarterly / enterprise_value",
        ],
        "template": "trade_when(volume > adv20, rank(0.55 * ts_rank(FIELD_RATIO, 80) + 0.25 * rank(ts_corr(vwap, volume, 40)) - 0.20 * ts_rank(returns, 30)), -1)",
        "stop_if": ["coverage_density_low", "turnover_outside_0.01_0.70"],
    },
    {
        "recipe_id": "sentiment_revision_overlay",
        "source_theme": "sentiment_news_revision",
        "max_initial_sims": 10,
        "neutralization": "MARKET",
        "fields": ["scl12_sentiment_fast_d1", "snt1_d1_netearningsrevision", "snt1_cored1_score"],
        "template": "rank(0.50 * VALUE_ANCHOR + 0.30 * ts_rank(SENTIMENT_FIELD, 20) - 0.20 * ts_rank(returns, 40))",
        "stop_if": ["high_turnover_repeated"],
    },
)


def main() -> int:
    args = _parse_args()
    triage_dir = Path(args.triage_dir)
    records_path = triage_dir / "triage_records.jsonl"
    if not records_path.is_file():
        raise FileNotFoundError(records_path)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    records = _read_jsonl(records_path)
    created_at = datetime.now().isoformat(timespec="seconds")
    analyzed = _analyze_records(records)
    _write_outputs(
        records=records,
        analyzed=analyzed,
        output_dir=output_dir,
        triage_dir=triage_dir,
        created_at=created_at,
        source_label=args.source_label,
        top_sources=args.top_sources,
    )
    print(json.dumps(analyzed["manifest"], ensure_ascii=False, indent=2))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build deterministic WQ forum idea memory from triage records")
    parser.add_argument("--triage-dir", required=True, help="Directory containing triage_records.jsonl")
    parser.add_argument("--output-dir", required=True, help="Output directory for idea memory artifacts")
    parser.add_argument("--source-label", default="", help="Optional label shown in the markdown report")
    parser.add_argument("--top-sources", type=int, default=6, help="Representative sources per theme")
    return parser.parse_args()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no} is not valid JSONL") from exc
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _analyze_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    clusters: dict[str, list[dict[str, Any]]] = {rule.theme_id: [] for rule in THEMES}
    source_index: list[dict[str, Any]] = []
    combinations: Counter[tuple[str, str]] = Counter()
    unmatched = 0

    for record_index, record in enumerate(records):
        text = _record_text(record)
        fields = _as_list(record.get("wq_fields"))
        operators = _as_list(record.get("operators"))
        risk_flags = _as_list(record.get("risk_flags"))
        themes = _match_themes(text, fields, operators, risk_flags)
        non_course = not _is_course_like(record)
        if not themes:
            unmatched += 1
        for left, right in itertools.combinations(sorted(themes), 2):
            combinations[(left, right)] += 1
        source_entry = {
            "record_index": record_index,
            "themes": themes,
            "non_course": non_course,
            "relevance_score": record.get("relevance_score"),
            "source_type": record.get("source_type"),
            "post_id": record.get("post_id"),
            "comment_id": record.get("comment_id"),
            "title": _clean(record.get("title")),
            "url": record.get("url"),
            "value_type": record.get("value_type"),
            "hypothesis": _clean(record.get("hypothesis")),
            "wq_fields": fields,
            "operators": operators,
            "windows": _as_list(record.get("windows")),
            "risk_flags": risk_flags,
            "excerpt": _truncate(_clean(record.get("excerpt")), 600),
            "created_at": record.get("created_at"),
        }
        source_index.append(source_entry)
        for theme_id in themes:
            clusters[theme_id].append(source_entry)

    theme_stats: dict[str, dict[str, Any]] = {}
    for rule in THEMES:
        entries = clusters[rule.theme_id]
        theme_stats[rule.theme_id] = _theme_stats(entries)

    combo_rows = [
        {"themes": list(pair), "shared_records": count}
        for pair, count in combinations.most_common()
    ]
    manifest = {
        "schema_version": 1,
        "records_scanned": len(records),
        "matched_records": sum(1 for entry in source_index if entry["themes"]),
        "multi_label_memberships": sum(len(entry["themes"]) for entry in source_index),
        "unmatched_records": unmatched,
        "themes": {
            theme_id: {"count": stats["count"], "non_course": stats["non_course"]}
            for theme_id, stats in theme_stats.items()
        },
    }
    return {
        "clusters": clusters,
        "source_index": source_index,
        "theme_stats": theme_stats,
        "combinations": combo_rows,
        "manifest": manifest,
    }


def _record_text(record: dict[str, Any]) -> str:
    parts = [
        record.get("title"),
        record.get("excerpt"),
        record.get("hypothesis"),
        record.get("value_type"),
        " ".join(_as_list(record.get("wq_fields"))),
        " ".join(_as_list(record.get("operators"))),
        " ".join(_as_list(record.get("risk_flags"))),
    ]
    return " ".join(str(part) for part in parts if part).lower()


def _match_themes(text: str, fields: list[str], operators: list[str], risk_flags: list[str]) -> list[str]:
    field_text = " ".join(fields).lower()
    operator_text = " ".join(operators).lower()
    risk_text = " ".join(risk_flags).lower()
    matched: list[str] = []
    for rule in THEMES:
        score = 0
        score += _keyword_score(text, rule.keywords)
        score += _keyword_score(field_text, rule.field_keywords)
        score += _keyword_score(operator_text, rule.operator_keywords)
        score += _keyword_score(risk_text, rule.risk_keywords)
        if rule.theme_id == "intraday_volume_shock":
            if {"high", "low"}.issubset(set(fields)) and ("volume" in fields or "adv20" in fields):
                score += 2
        if rule.theme_id == "field_update_event":
            slow_or_event_field = any(
                token in field_text
                for token in ("anl", "actual_", "forward", "scl12", "snt", "eps", "cashflow", "sales")
            )
            if slow_or_event_field and any(op in operators for op in ("ts_delta", "ts_std", "ts_std_dev")):
                score += 1
        if rule.theme_id == "fundamental_value_quality":
            ratio_hint = "/" in text and any(token in field_text for token in ("cap", "assets", "enterprise_value", "close"))
            if ratio_hint:
                score += 1
        if score >= 1:
            matched.append(rule.theme_id)
    return matched


def _keyword_score(text: str, keywords: tuple[str, ...]) -> int:
    if not keywords:
        return 0
    score = 0
    for keyword in keywords:
        if not keyword:
            continue
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(keyword.lower())}(?![A-Za-z0-9_])", text):
            score += 1
    return score


def _is_course_like(record: dict[str, Any]) -> bool:
    text = f"{record.get('title', '')} {record.get('excerpt', '')}".lower()
    return any(pattern in text for pattern in COURSE_PATTERNS)


def _theme_stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
    field_counter: Counter[str] = Counter()
    operator_counter: Counter[str] = Counter()
    risk_counter: Counter[str] = Counter()
    value_counter: Counter[str] = Counter()
    for entry in entries:
        field_counter.update(str(field) for field in entry.get("wq_fields", []) if field)
        operator_counter.update(str(operator) for operator in entry.get("operators", []) if operator)
        risk_counter.update(str(flag) for flag in entry.get("risk_flags", []) if flag)
        value = entry.get("value_type")
        if value:
            value_counter[str(value)] += 1
    return {
        "count": len(entries),
        "non_course": sum(1 for entry in entries if entry.get("non_course")),
        "course_or_noisy": sum(1 for entry in entries if not entry.get("non_course")),
        "top_fields": field_counter.most_common(12),
        "top_operators": operator_counter.most_common(12),
        "top_risks": risk_counter.most_common(12),
        "value_types": value_counter.most_common(8),
    }


def _write_outputs(
    *,
    records: list[dict[str, Any]],
    analyzed: dict[str, Any],
    output_dir: Path,
    triage_dir: Path,
    created_at: str,
    source_label: str,
    top_sources: int,
) -> None:
    clusters_path = output_dir / "forum_idea_clusters_strict.jsonl"
    source_path = output_dir / "forum_idea_source_index_strict.jsonl"
    combinations_path = output_dir / "forum_idea_theme_combinations.jsonl"
    recipes_path = output_dir / "forum_candidate_recipes.jsonl"
    rules_path = output_dir / "forum_pattern_rules.jsonl"
    report_path = output_dir / "forum_idea_memory_strict.md"
    manifest_path = output_dir / "strict_manifest.json"

    _write_jsonl(
        clusters_path,
        [
            {
                "theme_id": rule.theme_id,
                "label": rule.label,
                "logic": rule.logic,
                "candidate_policy": rule.candidate_policy,
                "recipes": list(rule.recipes),
                "stats": analyzed["theme_stats"][rule.theme_id],
                "sources": analyzed["clusters"][rule.theme_id],
            }
            for rule in THEMES
        ],
    )
    _write_jsonl(source_path, analyzed["source_index"])
    _write_jsonl(combinations_path, analyzed["combinations"])
    _write_jsonl(recipes_path, _recipes_with_evidence(analyzed["theme_stats"]))
    _write_jsonl(rules_path, _pattern_rules())

    manifest = dict(analyzed["manifest"])
    manifest.update(
        {
            "created_at": created_at,
            "source_triage_dir": str(triage_dir),
            "source_label": source_label,
            "no_external_llm": True,
            "files": {
                "strict_clusters": str(clusters_path),
                "theme_combinations": str(combinations_path),
                "candidate_recipes": str(recipes_path),
                "strict_source_index": str(source_path),
                "pattern_rules": str(rules_path),
                "strict_report": str(report_path),
            },
        }
    )
    analyzed["manifest"] = manifest
    _write_json(manifest_path, manifest)
    report_path.write_text(
        _markdown_report(
            records=records,
            analyzed=analyzed,
            created_at=created_at,
            triage_dir=triage_dir,
            source_label=source_label,
            top_sources=top_sources,
        ),
        encoding="utf-8",
    )


def _recipes_with_evidence(theme_stats: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for recipe in RECIPE_ROWS:
        row = dict(recipe)
        stats = theme_stats.get(str(recipe["source_theme"]), {})
        row["evidence_records"] = stats.get("count", 0)
        row["non_course_sources"] = stats.get("non_course", 0)
        rows.append(row)
    return rows


def _pattern_rules() -> list[dict[str, Any]]:
    return [
        {
            "rule_id": "forum_direct_formula_graylist",
            "action": "penalize",
            "logic": "Direct forum formula snippets are similarity-prone. Only use after changing field and operator families.",
        },
        {
            "rule_id": "price_volume_template_saturation",
            "action": "overlay_only",
            "logic": "Standalone price-volume, close/vwap, volatility, and volume-shock templates are crowded.",
        },
        {
            "rule_id": "similarity_hard_gate",
            "action": "reject_or_mutate",
            "logic": "Reject or heavily mutate candidates with ledger similarity >= 0.70 before simulation.",
        },
        {
            "rule_id": "missingness_probe_budget",
            "action": "small_batch_first",
            "logic": "Test missingness operators and fields in small batches before allocating broad simulation budget.",
        },
        {
            "rule_id": "trade_when_count_monitoring",
            "action": "monitor",
            "logic": "Any trade_when candidate must pass long/short count, turnover, and subuniverse stability checks.",
        },
    ]


def _markdown_report(
    *,
    records: list[dict[str, Any]],
    analyzed: dict[str, Any],
    created_at: str,
    triage_dir: Path,
    source_label: str,
    top_sources: int,
) -> str:
    lines: list[str] = []
    title = "WQ Forum Idea Memory - Strict"
    if source_label:
        title += f" ({source_label})"
    lines.extend(
        [
            f"# {title}",
            "",
            f"- Created: {created_at}",
            f"- Source: `{triage_dir}`",
            f"- Records scanned: {len(records)}",
            f"- Matched records: {analyzed['manifest']['matched_records']}",
            f"- Multi-label memberships: {analyzed['manifest']['multi_label_memberships']}",
            f"- Unmatched records: {analyzed['manifest']['unmatched_records']}",
            f"- No external LLM: true",
            "",
            "## Theme Map",
        ]
    )
    for rule in THEMES:
        stats = analyzed["theme_stats"][rule.theme_id]
        lines.extend(
            [
                f"### {rule.label} (`{rule.theme_id}`)",
                f"- Count: {stats['count']}; non-course: {stats['non_course']}; course/noisy: {stats['course_or_noisy']}",
                f"- Logic: {rule.logic}",
                f"- Candidate policy: {rule.candidate_policy}",
                f"- Top fields: {_format_counter(stats['top_fields'])}",
                f"- Top operators: {_format_counter(stats['top_operators'])}",
                f"- Risks: {_format_counter(stats['top_risks'])}",
                "- Recipes:",
            ]
        )
        for recipe in rule.recipes:
            lines.append(f"  - `{recipe}`")
        lines.append("- Representative sources:")
        for source in _representative_sources(analyzed["clusters"][rule.theme_id], top_sources):
            label = "non-course" if source.get("non_course") else "course/noisy"
            score = source.get("relevance_score")
            title = source.get("title") or "(untitled)"
            lines.append(f"  - [{score}] {title} ({label})")
            if source.get("url"):
                lines.append(f"    URL: {source['url']}")
        lines.append("")

    lines.extend(["## Theme Combinations", ""])
    for row in analyzed["combinations"][:20]:
        left, right = row["themes"]
        lines.append(f"- `{left}` + `{right}`: {row['shared_records']} shared records")
    lines.extend(["", "## Candidate Recipes", ""])
    for recipe in _recipes_with_evidence(analyzed["theme_stats"]):
        lines.append(
            f"- `{recipe['recipe_id']}`: theme `{recipe['source_theme']}`, max sims {recipe['max_initial_sims']}, "
            f"neutralization `{recipe['neutralization']}`, evidence {recipe['evidence_records']} records / "
            f"{recipe['non_course_sources']} non-course."
        )
    lines.extend(
        [
            "",
            "## Workflow Notes",
            "",
            "- Keep forum direct snippets out of the primary candidate stream unless field and operator families are changed.",
            "- Treat robustness/turnover/correlation themes as gating rules, not expression sources.",
            "- Prefer combinations: field update + sentiment, fundamental anchor + group comparison, missingness + slow anchor.",
            "- Run find-only/check-only first; this report does not submit any alpha.",
        ]
    )
    return "\n".join(lines) + "\n"


def _representative_sources(entries: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    def key(entry: dict[str, Any]) -> tuple[int, int, str]:
        return (
            1 if entry.get("non_course") else 0,
            int(entry.get("relevance_score") or 0),
            str(entry.get("title") or ""),
        )

    seen: set[tuple[str, str]] = set()
    selected: list[dict[str, Any]] = []
    for entry in sorted(entries, key=key, reverse=True):
        source_key = (str(entry.get("post_id") or ""), str(entry.get("comment_id") or ""))
        if source_key in seen:
            continue
        seen.add(source_key)
        selected.append(entry)
        if len(selected) >= limit:
            break
    return selected


def _format_counter(items: list[tuple[str, int]]) -> str:
    if not items:
        return "(none)"
    return ", ".join(f"{key}({value})" for key, value in items[:8])


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None and str(item) != ""]
    return [str(value)]


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


if __name__ == "__main__":
    raise SystemExit(main())
