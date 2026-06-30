"""Triage WorldQuant BRAIN community content into factor-mining inputs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .expression_parser import extract_components, normalize_expression, parse_expression

TEXT_KEYS = ("body_text", "body", "text", "content", "snippet", "summary")
TITLE_KEYS = ("title", "subject", "name")
TIME_KEYS = ("created_at", "timestamp", "time", "date", "updated_at")

WQ_FIELDS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "returns",
    "cap",
    "market_cap",
    "adv20",
    "sales",
    "revenue",
    "assets",
    "enterprise_value",
    "equity",
    "debt",
    "liabilities",
    "cash_flow",
    "free_cash_flow",
    "net_income",
    "book_value",
    "dividends",
    "est_eps",
    "est_revenue",
    "est_ebitda",
    "est_growth",
    "price_target",
    "recommendation",
    "snt_buzz",
    "snt_sentiment",
    "snt_bullish",
    "snt_bearish",
    "scl12_sentiment",
    "scl12_sentiment_fast_d1",
    "snt1_cored1_score",
    "snt1_d1_earningsrevision",
    "snt1_d1_netearningsrevision",
    "snt1_d1_stockrank",
    "pcr_oi",
    "pcr_vol",
    "implied_volatility",
    "option_volume",
    "open_interest",
    "short_interest",
}

WQ_OPERATORS = {
    "rank",
    "zscore",
    "scale",
    "group_rank",
    "group_zscore",
    "group_neutralize",
    "group_mean",
    "abs",
    "sign",
    "log",
    "sqrt",
    "power",
    "sign_power",
    "max",
    "min",
    "ts_mean",
    "ts_std",
    "ts_std_dev",
    "ts_max",
    "ts_min",
    "ts_sum",
    "ts_shift",
    "ts_delta",
    "ts_rank",
    "ts_argmax",
    "ts_argmin",
    "ts_corr",
    "ts_cov",
    "decay_linear",
    "ts_decay_linear",
    "product",
    "ts_av_diff",
    "where",
    "trade_when",
    "vector_neut",
    "pasteurize",
    "bucket",
    "humpdecay",
}

FACTOR_KEYWORDS = {
    "alpha",
    "factor",
    "fastexpr",
    "fast expr",
    "expression",
    "formula",
    "operator",
    "data field",
    "field",
    "reversal",
    "mean reversion",
    "momentum",
    "volume shock",
    "vwap",
    "volatility",
    "quality",
    "value",
    "growth",
    "sentiment",
}

SUBMISSION_KEYWORDS = {
    "sharpe",
    "fitness",
    "turnover",
    "returns",
    "submission",
    "submit",
    "active",
    "neutralization",
    "subindustry",
    "industry",
    "market",
    "decay",
    "truncation",
}

FAILURE_KEYWORDS = {
    "fail",
    "failed",
    "unavailable",
    "not available",
    "unknown",
    "unsupported",
    "unit check",
    "unit",
    "self correlation",
    "prod correlation",
    "sc fail",
    "prod_corr",
    "self_corr",
}


@dataclass(frozen=True)
class CommunityItem:
    source_type: str
    item_id: str
    post_id: str
    comment_id: str | None
    url: str
    title: str
    text: str
    created_at: str | None = None


@dataclass
class CommunityTriageConfig:
    posts_file: Path
    output_dir: Path
    comments_file: Path | None = None
    max_candidates_per_record: int = 5
    min_score: int = 15


def read_jsonl(path: Path | None) -> list[dict]:
    if not path:
        return []
    rows: list[dict] = []
    if not path.is_file():
        raise FileNotFoundError(path)
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


def build_community_items(posts: list[dict], comments: list[dict] | None = None) -> list[CommunityItem]:
    comments = comments or []
    post_meta: dict[str, dict] = {}
    items: list[CommunityItem] = []

    for index, post in enumerate(posts):
        post_id = _first_str(post, "post_id", "id", "thread_id") or f"post-{index}"
        title = _first_str(post, *TITLE_KEYS)
        url = _first_str(post, "url", "link", "permalink")
        text = _first_str(post, *TEXT_KEYS)
        created_at = _first_str(post, *TIME_KEYS)
        post_meta[post_id] = {"title": title, "url": url}
        items.append(
            CommunityItem(
                source_type="post",
                item_id=post_id,
                post_id=post_id,
                comment_id=None,
                url=url,
                title=title,
                text=text,
                created_at=created_at,
            )
        )

    for index, comment in enumerate(comments):
        post_id = _first_str(comment, "post_id", "thread_id", "parent_post_id") or ""
        comment_id = _first_str(comment, "comment_id", "id") or f"comment-{index}"
        meta = post_meta.get(post_id, {})
        items.append(
            CommunityItem(
                source_type="comment",
                item_id=comment_id,
                post_id=post_id,
                comment_id=comment_id,
                url=_first_str(comment, "url", "link", "permalink") or str(meta.get("url", "")),
                title=_first_str(comment, *TITLE_KEYS) or str(meta.get("title", "")),
                text=_first_str(comment, *TEXT_KEYS),
                created_at=_first_str(comment, *TIME_KEYS),
            )
        )

    return items


def triage_item(item: CommunityItem, max_candidates_per_record: int = 5) -> dict:
    text = " ".join(part for part in (item.title, item.text) if part).strip()
    text_lower = text.lower()
    expression_snippets = extract_expression_snippets(text)
    fields, operators, windows = extract_text_components(text, expression_snippets)
    risk_flags = infer_risk_flags(text_lower, expression_snippets)
    candidate_expressions = (
        []
        if "private_code" in risk_flags
        else derive_candidate_expressions(text_lower, fields, operators, max_candidates_per_record)
    )
    score = relevance_score(text_lower, fields, operators, windows, risk_flags, expression_snippets, candidate_expressions)
    value_type = classify_value_type(score, fields, operators, risk_flags, candidate_expressions, text_lower)
    experience_category = classify_experience_category(
        value_type=value_type,
        text_lower=text_lower,
        fields=fields,
        operators=operators,
        risk_flags=risk_flags,
        candidate_expressions=candidate_expressions,
        expression_snippets=expression_snippets,
    )

    return {
        "post_id": item.post_id,
        "comment_id": item.comment_id,
        "source_type": item.source_type,
        "url": item.url,
        "title": item.title,
        "excerpt": make_excerpt(item.text or item.title),
        "relevance_score": score,
        "value_type": value_type,
        "experience_category": experience_category,
        "hypothesis": infer_hypothesis(text_lower, fields, operators, risk_flags),
        "wq_fields": sorted(fields),
        "operators": sorted(operators),
        "windows": windows,
        "risk_flags": risk_flags,
        "candidate_expressions": candidate_expressions,
        "expression_snippet_count": len(expression_snippets),
        "created_at": item.created_at,
    }


def triage_community(config: CommunityTriageConfig) -> dict:
    posts = read_jsonl(config.posts_file)
    comments = read_jsonl(config.comments_file)
    items = build_community_items(posts, comments)
    records = [triage_item(item, config.max_candidates_per_record) for item in items]
    kept = [record for record in records if record["relevance_score"] >= config.min_score or record["value_type"] != "discard"]
    kept.sort(key=lambda row: (-int(row["relevance_score"]), row["value_type"], row["post_id"], row.get("comment_id") or ""))

    config.output_dir.mkdir(parents=True, exist_ok=True)
    records_file = config.output_dir / "triage_records.jsonl"
    candidates_file = config.output_dir / "community_wq_candidates.jsonl"
    report_file = config.output_dir / "community_factor_triage.md"
    manifest_file = config.output_dir / "manifest.json"
    knowledge_dir = config.output_dir / "knowledge_suggestions"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    write_jsonl(records_file, kept)
    candidates = build_candidate_rows(kept)
    write_jsonl(candidates_file, candidates)
    report_file.write_text(render_report(kept, candidates), encoding="utf-8")
    write_knowledge_suggestions(knowledge_dir, kept)

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "posts_file": str(config.posts_file),
        "comments_file": str(config.comments_file) if config.comments_file else None,
        "output_dir": str(config.output_dir),
        "input_posts": len(posts),
        "input_comments": len(comments),
        "triage_records": len(kept),
        "candidate_rows": len(candidates),
        "experience_categories": _count_by_key(kept, "experience_category"),
        "privacy_note": "No credentials are read or written; suspected complete formulas are not emitted verbatim as candidates.",
        "files": {
            "records": str(records_file),
            "candidates": str(candidates_file),
            "report": str(report_file),
            "rules": str(knowledge_dir / "rules.md"),
            "findings": str(knowledge_dir / "findings.md"),
            "failures": str(knowledge_dir / "failures.md"),
        },
    }
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def extract_expression_snippets(text: str) -> list[str]:
    snippets: list[str] = []
    for block in re.findall(r"```(?:\w+)?\s*([\s\S]{1,1200}?)```", text):
        snippets.extend(_expression_like_lines(block))
    snippets.extend(_expression_like_lines("\n".join(re.findall(r"`([^`]{4,500})`", text))))
    snippets.extend(_expression_like_lines(text))

    out: list[str] = []
    seen: set[str] = set()
    for snippet in snippets:
        candidate = _clean_expression_snippet(snippet)
        if not candidate:
            continue
        key = normalize_expression(candidate)
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def extract_text_components(text: str, expression_snippets: list[str] | None = None) -> tuple[set[str], set[str], list[int]]:
    text_lower = text.lower()
    fields = {field for field in WQ_FIELDS if re.search(rf"\b{re.escape(field)}\b", text_lower)}
    operators = {op for op in WQ_OPERATORS if re.search(rf"\b{re.escape(op)}\s*\(", text_lower)}

    for snippet in expression_snippets or []:
        parts = extract_components(snippet)
        fields.update(str(field).lower() for field in parts.get("fields", set()))
        operators.update(str(op).lower() for op in parts.get("operators", set()))

    windows = sorted({
        int(value)
        for value in re.findall(r"(?:window|period|lookback|days|d|n|w)\s*[=:]?\s*(\d{1,3})\b", text_lower)
        + re.findall(r"\bts_\w+\s*\([^)]*,\s*(\d{1,3})\s*\)", text_lower)
        + re.findall(r"\bdecay_linear\s*\([^)]*,\s*(\d{1,3})\s*\)", text_lower)
    })
    return fields, operators, windows


def infer_risk_flags(text_lower: str, expression_snippets: list[str]) -> list[str]:
    flags: list[str] = []
    if expression_snippets:
        flags.append("possible_complete_alpha")
    if any(token in text_lower for token in ("actual code", "full code", "完整代码", "完整alpha", "完整 alpha")):
        flags.append("private_code")
    if any(token in text_lower for token in ("template", "模板", "copy", "clone", "directly use", "照抄", "套用")):
        flags.append("template_clone_risk")
    if any(token in text_lower for token in ("near pass", "almost pass", "close to pass", "接近过线", "差一点", "快过线")):
        flags.append("metric_near_pass")
    if any(token in text_lower for token in ("precheck", "pre-check", "stale", "expired", "rerun check", "重新check", "重新 check")):
        flags.append("stale_precheck_risk")
    if any(token in text_lower for token in ("self correlation", "prod correlation", "sc fail", "self_corr", "prod_corr", "相关性不过", "自相关")):
        flags.append("correlation_risk")
    if any(token in text_lower for token in ("crowded", "popular field", "same field", "common field", "拥挤", "热门字段", "同质")):
        flags.append("field_family_crowding")
    if any(token in text_lower for token in ("unknown field", "unknown operator", "unsupported", "not available")):
        flags.append("unknown_or_unsupported")
        flags.append("operator_availability_risk")
    if "unit check" in text_lower or "unit" in text_lower and "fail" in text_lower:
        flags.append("unit_check")
    if "turnover" in text_lower and any(token in text_lower for token in ("high", "too high", ">70", "above")):
        flags.append("high_turnover")
    if "turnover" in text_lower and any(token in text_lower for token in ("low", "too low", "<1", "below")):
        flags.append("low_turnover")
    if any(token in text_lower for token in ("tier", "gold", "free tier", "unavailable", "pasteurize")):
        flags.append("platform_limit")
        flags.append("operator_availability_risk")
    return sorted(set(flags))


def derive_candidate_expressions(
    text_lower: str,
    fields: set[str],
    operators: set[str],
    max_candidates: int = 5,
) -> list[str]:
    templates: list[str] = []

    if "vwap" in fields and any(token in text_lower for token in ("reversal", "mean reversion", "deviation", "close/vwap", "close / vwap")):
        templates.extend([
            "-1 * rank(ts_decay_linear(close / vwap, 5))",
            "-1 * rank(ts_decay_linear(close / vwap, 10))",
        ])
    if "vwap" in fields and {"sales", "assets"} <= fields:
        templates.append("-1 * rank(ts_decay_linear(close / vwap, 10)) + rank(ts_rank(actual_sales_value_quarterly / assets, 60) - ts_rank(returns, 20))")
    if "vwap" in fields and {"revenue", "enterprise_value"} <= fields:
        templates.append("-1 * rank(ts_decay_linear(close / vwap, 10)) + rank(ts_rank(actual_sales_value_quarterly / enterprise_value, 60) - ts_rank(returns, 20))")
    if any(token in text_lower for token in ("volume shock", "abnormal volume", "volume spike", "liquidity", "turnover")) or "volume" in fields:
        templates.extend([
            "rank(volume / ts_mean(volume, 20))",
            "rank(ts_delta(volume, 5) / ts_mean(volume, 20))",
        ])
    if "ts_corr" in operators or "correlation" in text_lower and {"close", "volume"} <= fields:
        templates.extend([
            "rank(ts_corr(close, volume, 10))",
            "rank(ts_decay_linear(ts_corr(close, volume, 10), 5))",
        ])
    if any(token in text_lower for token in ("momentum", "trend", "breakout")):
        templates.extend([
            "rank(ts_delta(close, 20) / close)",
            "rank(close / ts_mean(close, 20))",
        ])
    if any(token in text_lower for token in ("reversal", "mean reversion", "overreaction")) and "vwap" not in fields:
        templates.extend([
            "-1 * rank(ts_delta(close, 5) / close)",
            "-1 * rank(close / ts_mean(close, 10))",
        ])
    if any(token in text_lower for token in ("low vol", "low volatility", "volatility", "risk")):
        templates.append("-1 * rank(ts_std_dev(returns, 20))")
    if {"sales", "assets"} <= fields:
        templates.append("rank(ts_rank(actual_sales_value_quarterly / assets, 60) - ts_rank(returns, 20))")
    if {"revenue", "enterprise_value"} <= fields:
        templates.append("rank(ts_rank(actual_sales_value_quarterly / enterprise_value, 60) - ts_rank(returns, 20))")
    if "cash_flow" in fields and "market_cap" in fields:
        templates.append("rank(ts_rank(cashflow_op / cap, 60) - ts_rank(returns, 20))")
    if "est_eps" in fields or "estimate" in text_lower or "analyst" in text_lower:
        templates.append("rank(ts_rank(anl4_af_eps_value / close, 60) - ts_rank(returns, 20))")
    if "snt_sentiment" in fields or "sentiment" in text_lower:
        templates.append("rank(ts_delta(scl12_sentiment_fast_d1, 5))")
    if "implied_volatility" in fields:
        templates.append("implied_volatility_call_120 - implied_volatility_put_120")
        templates.append("-1 * rank(implied_volatility_mean_30)")
    if "pcr_vol" in fields:
        templates.append("-1 * rank(pcr_vol_10)")
    if "short_interest" in fields:
        templates.append("-1 * rank(short_interest)")

    return validate_and_dedupe_expressions(templates, max_candidates)


def validate_and_dedupe_expressions(expressions: list[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for expression in expressions:
        expression = " ".join(expression.strip().split())
        if not expression:
            continue
        key = normalize_expression(expression)
        if key in seen:
            continue
        try:
            parse_expression(expression, mode="wq")
        except Exception:
            continue
        seen.add(key)
        out.append(expression)
        if len(out) >= limit:
            break
    return out


def relevance_score(
    text_lower: str,
    fields: set[str],
    operators: set[str],
    windows: list[int],
    risk_flags: list[str],
    expression_snippets: list[str],
    candidate_expressions: list[str],
) -> int:
    score = 0
    if candidate_expressions:
        score += 35
    if expression_snippets:
        score += 20
    score += min(15, len(fields) * 3)
    score += min(15, len(operators) * 3)
    if windows:
        score += 5
    if any(token in text_lower for token in FACTOR_KEYWORDS):
        score += 10
    if any(token in text_lower for token in SUBMISSION_KEYWORDS):
        score += 10
    if any(token in text_lower for token in FAILURE_KEYWORDS):
        score += 10
    if "private_code" in risk_flags:
        score -= 20
    return max(0, min(100, score))


def classify_value_type(
    score: int,
    fields: set[str],
    operators: set[str],
    risk_flags: list[str],
    candidate_expressions: list[str],
    text_lower: str,
) -> str:
    if {"unknown_or_unsupported", "unit_check", "platform_limit"} & set(risk_flags):
        return "failure_case"
    if "correlation_risk" in risk_flags and any(token in text_lower for token in ("fail", "failed", "too high")):
        return "failure_case"
    if candidate_expressions:
        return "candidate_seed"
    if any(token in text_lower for token in SUBMISSION_KEYWORDS | FAILURE_KEYWORDS):
        return "submission_rule"
    if fields:
        return "field_hint"
    if operators:
        return "operator_pattern"
    if score >= 25:
        return "operator_pattern"
    return "discard"


def classify_experience_category(
    *,
    value_type: str,
    text_lower: str,
    fields: set[str],
    operators: set[str],
    risk_flags: list[str],
    candidate_expressions: list[str],
    expression_snippets: list[str],
) -> str:
    risk_set = set(risk_flags)
    if "metric_near_pass" in risk_set or (
        "correlation_risk" in risk_set and any(token in text_lower for token in ("near", "close", "almost", "接近", "差一点"))
    ):
        return "near_pass_repair"
    if "template_clone_risk" in risk_set or (
        expression_snippets and any(token in text_lower for token in ("template", "模板", "example", "例子"))
    ):
        return "alpha_template"
    if risk_set & {
        "high_turnover",
        "low_turnover",
        "unit_check",
        "platform_limit",
        "unknown_or_unsupported",
        "operator_availability_risk",
    }:
        return "operation_attribution"
    if risk_set & {"correlation_risk", "stale_precheck_risk", "field_family_crowding"}:
        return "submission_gate"
    if value_type == "candidate_seed" or candidate_expressions:
        return "alpha_template"
    if value_type in {"submission_rule", "failure_case"}:
        return "submission_gate"
    if fields or operators:
        return "operation_attribution"
    return value_type or "discard"


def infer_hypothesis(text_lower: str, fields: set[str], operators: set[str], risk_flags: list[str]) -> str:
    if "metric_near_pass" in risk_flags:
        return "Near-pass candidates should be repaired before spending fresh simulation budget."
    if "correlation_risk" in risk_flags:
        return "Submission constraint: correlation checks require changing field or operator family, not only windows."
    if "template_clone_risk" in risk_flags:
        return "Template-like forum examples should be transformed structurally before simulation or submission."
    if "platform_limit" in risk_flags or "unknown_or_unsupported" in risk_flags:
        return "Platform constraint: verify operator and field availability before expanding this direction."
    if "vwap" in fields and any(token in text_lower for token in ("reversal", "mean reversion", "deviation")):
        return "VWAP deviation may contain short-horizon reversal information."
    if {"sales", "assets"} <= fields or {"revenue", "enterprise_value"} <= fields:
        return "Fundamental ratios can add information orthogonal to price/volume signals."
    if "ts_corr" in operators or "correlation" in text_lower:
        return "Price-volume correlation can capture participation and flow structure."
    if any(token in text_lower for token in ("momentum", "trend", "breakout")):
        return "Recent price trend may persist over the selected horizon."
    if any(token in text_lower for token in ("volume shock", "abnormal volume", "volume spike")):
        return "Abnormal volume may identify attention or liquidity shocks."
    if any(token in text_lower for token in ("low vol", "low volatility", "volatility")):
        return "Lower recent volatility may carry defensive or risk-adjusted return information."
    return "Potential WQ factor-mining hint; review manually before simulation."


def build_candidate_rows(records: list[dict]) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for record in records:
        if record.get("value_type") != "candidate_seed":
            continue
        for expression in record.get("candidate_expressions", []):
            key = normalize_expression(expression)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "expression": expression,
                "tag": "community-" + _slug(record.get("hypothesis", "seed"))[:48],
                "source_post_id": record.get("post_id"),
                "source_comment_id": record.get("comment_id"),
                "relevance_score": record.get("relevance_score"),
                "experience_category": record.get("experience_category"),
                "risk_flags": record.get("risk_flags") or [],
            })
    return rows


def render_report(records: list[dict], candidates: list[dict]) -> str:
    sections = [
        "# WorldQuant Community Factor Triage",
        "",
        f"- Records kept: {len(records)}",
        f"- Candidate expressions: {len(candidates)}",
        "- Note: candidate expressions are derived templates, not verbatim copies of suspected complete alphas.",
        "",
        "## Experience Categories",
        "",
        *[f"- {key}: {value}" for key, value in sorted(_count_by_key(records, "experience_category").items())],
        "",
    ]

    for title, value_type in [
        ("High-Value Candidate Seeds", "candidate_seed"),
        ("Submission Rules And Failure Cases", "failure_case"),
        ("Submission Hints", "submission_rule"),
        ("Field And Operator Hints", "field_hint"),
        ("Operator Patterns", "operator_pattern"),
    ]:
        rows = [record for record in records if record.get("value_type") == value_type]
        if not rows:
            continue
        sections.append(f"## {title}")
        sections.append("")
        for record in rows[:50]:
            sections.append(f"- [{record.get('relevance_score')}] {record.get('title') or record.get('post_id')}")
            if record.get("url"):
                sections.append(f"  URL: {record['url']}")
            sections.append(f"  Hypothesis: {record.get('hypothesis')}")
            if record.get("wq_fields"):
                sections.append("  Fields: " + ", ".join(record["wq_fields"]))
            if record.get("operators"):
                sections.append("  Operators: " + ", ".join(record["operators"]))
            if record.get("risk_flags"):
                sections.append("  Risk flags: " + ", ".join(record["risk_flags"]))
            if record.get("experience_category"):
                sections.append(f"  Experience category: {record['experience_category']}")
            for expression in record.get("candidate_expressions", [])[:5]:
                sections.append(f"  Candidate: `{expression}`")
            if record.get("excerpt"):
                sections.append(f"  Excerpt: {record['excerpt']}")
        sections.append("")

    return "\n".join(sections).rstrip() + "\n"


def write_knowledge_suggestions(output_dir: Path, records: list[dict]) -> None:
    findings = [record for record in records if record.get("value_type") == "candidate_seed"]
    failures = [record for record in records if record.get("value_type") == "failure_case"]
    rules = [
        record
        for record in records
        if record.get("value_type") == "submission_rule" or "correlation_risk" in record.get("risk_flags", [])
    ]
    (output_dir / "findings.md").write_text(render_knowledge_file("Findings", findings), encoding="utf-8")
    (output_dir / "failures.md").write_text(render_knowledge_file("Failures", failures), encoding="utf-8")
    (output_dir / "rules.md").write_text(render_knowledge_file("Rules", rules), encoding="utf-8")


def render_knowledge_file(title: str, records: list[dict]) -> str:
    lines = [f"# Community-Derived {title}", ""]
    if not records:
        lines.append("_No entries._")
        return "\n".join(lines) + "\n"
    for record in records[:100]:
        lines.append(f"- {record.get('hypothesis')}")
        lines.append(f"  Source: {record.get('post_id')}" + (f" / {record.get('comment_id')}" if record.get("comment_id") else ""))
        if record.get("experience_category"):
            lines.append(f"  Experience category: {record.get('experience_category')}")
        if record.get("risk_flags"):
            lines.append("  Risk flags: " + ", ".join(record["risk_flags"]))
        if record.get("candidate_expressions"):
            lines.append("  Derived candidates: " + "; ".join(record["candidate_expressions"][:3]))
    return "\n".join(lines) + "\n"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _count_by_key(rows: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def make_excerpt(text: str, limit: int = 260) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _expression_like_lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or len(line) > 500:
            continue
        if any(re.search(rf"\b{re.escape(op)}\s*\(", line, re.IGNORECASE) for op in WQ_OPERATORS):
            out.append(line)
    return out


def _clean_expression_snippet(value: str) -> str | None:
    text = value.strip().strip("`").strip()
    text = re.sub(r"^[>\-\*\d\.\)\s]+", "", text)
    text = text.split("#", 1)[0].strip()
    text = text.rstrip(";,.")
    first = _first_operator_index(text)
    if first is None:
        return None
    if first >= 5 and text[first - 5:first].strip() in {"-1 *", "-1*", "- rank", "-"}:
        first = max(0, text.rfind("-", 0, first))
    expr = text[first:].strip()
    if len(expr) > 300:
        return None
    depth = 0
    end = None
    for idx, char in enumerate(expr):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                end = idx + 1
    if end:
        expr = expr[:end]
    if expr.count("(") != expr.count(")"):
        return None
    return expr if "(" in expr and ")" in expr else None


def _first_operator_index(text: str) -> int | None:
    matches = [
        match.start()
        for op in WQ_OPERATORS
        for match in re.finditer(rf"\b{re.escape(op)}\s*\(", text, re.IGNORECASE)
    ]
    return min(matches) if matches else None


def _first_str(row: dict, *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if cleaned:
        return cleaned
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def config_from_paths(
    posts_file: str | Path,
    comments_file: str | Path | None = None,
    output_dir: str | Path | None = None,
    max_candidates_per_record: int = 5,
    min_score: int = 15,
) -> CommunityTriageConfig:
    posts_path = Path(posts_file)
    out = Path(output_dir) if output_dir else Path(r"D:\tmp\worldquant_community_20260513\triage")
    return CommunityTriageConfig(
        posts_file=posts_path,
        comments_file=Path(comments_file) if comments_file else None,
        output_dir=out,
        max_candidates_per_record=max_candidates_per_record,
        min_score=min_score,
    )
