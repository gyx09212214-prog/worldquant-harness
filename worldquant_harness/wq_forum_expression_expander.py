"""Expand forum-derived WQ research themes into screened candidate expressions.

This module is local-only: it reads forum idea memory and prior WQ artifacts,
then writes a candidate JSONL for the existing presubmit workflow.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .wq_forum_submission_optimizer import load_submission_policy
from .wq_research_miner import WQResearchMinerConfig, screen_candidate_drafts


@dataclass(frozen=True)
class WQForumExpressionExpanderConfig:
    forum_memory_dirs: tuple[Path, ...] = field(default_factory=tuple)
    direction_score_files: tuple[Path, ...] = field(default_factory=tuple)
    active_inventory_files: tuple[Path, ...] = field(default_factory=tuple)
    platform_files: tuple[Path, ...] = field(default_factory=tuple)
    rejected_files: tuple[Path, ...] = field(default_factory=tuple)
    submission_policy_file: Path | None = None
    output_dir: Path = Path("reports/wq_forum_expression_expansion")
    obsidian_output: Path | None = None
    max_candidates: int = 40
    similarity_cutoff: float = 0.62
    max_family_count: int = 4
    max_field_signature_count: int = 2
    title: str = "worldquant-harness forum expression expansion"


def build_forum_expression_expansion(config: WQForumExpressionExpanderConfig) -> dict[str, Any]:
    recipes = _load_forum_recipes(config.forum_memory_dirs)
    directions = _load_direction_scores(config.direction_score_files)
    active_rows = _load_inventory_rows(config.active_inventory_files)
    platform_rows = _load_rows(config.platform_files)
    rejected_rows = _load_rows(config.rejected_files)
    platform_active = [
        row for row in platform_rows
        if str(row.get("status") or "").upper() in {"ACTIVE", "SUBMITTED"}
    ]
    comparison_rows = _dedupe_by_expression([*active_rows, *platform_active])
    policy = load_submission_policy(config.submission_policy_file)

    drafts = _forum_recipe_drafts(recipes)
    drafts.extend(_forum_theme_hybrid_drafts(directions))
    drafts.extend(_forum_structure_shift_drafts(recipes, directions))
    drafts = _dedupe_by_expression(drafts)

    miner_config = WQResearchMinerConfig(
        output=config.output_dir / "forum_expansion_candidates.jsonl",
        max_candidates=config.max_candidates,
        similarity_cutoff=config.similarity_cutoff,
        max_family_count=config.max_family_count,
        max_field_signature_count=config.max_field_signature_count,
    )
    selected, rejected = screen_candidate_drafts(
        drafts,
        comparison_rows,
        config=miner_config,
        blocked_rows=rejected_rows,
        submission_policy=policy,
    )
    selected = sorted(selected, key=_selected_candidate_sort_key)[: max(0, config.max_candidates)]
    for index, row in enumerate(selected, start=1):
        row["candidate_rank"] = index
        row["source"] = "wq_forum_expression_expander"
        row["llm_provider"] = "none"
        row["no_external_llm"] = True

    plan = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "forum_recipes": len(recipes),
            "direction_scores": len(directions),
            "drafts": len(drafts),
            "selected": len(selected),
            "screened_out": len(rejected),
            "selected_families": dict(sorted(Counter(row.get("source_family") for row in selected).items())),
            "selected_themes": dict(sorted(Counter(row.get("forum_theme_id") for row in selected).items())),
            "rejected_reasons": dict(sorted(Counter(row.get("reject_reason") for row in rejected).items())),
            "policy_actions": dict(sorted(Counter(
                row.get("forum_policy_action") for row in selected if row.get("forum_policy_action")
            ).items())),
        },
        "inputs": {
            "forum_memory_dirs": [str(path) for path in config.forum_memory_dirs],
            "direction_score_files": [str(path) for path in config.direction_score_files],
            "active_inventory_files": [str(path) for path in config.active_inventory_files],
            "platform_files": [str(path) for path in config.platform_files],
            "rejected_files": [str(path) for path in config.rejected_files],
            "submission_policy_file": str(config.submission_policy_file) if config.submission_policy_file else "",
        },
        "candidates": selected,
        "rejected": rejected,
    }
    plan["markdown"] = render_forum_expression_expansion_markdown(plan, config=config)
    write_forum_expression_expansion_artifacts(plan, output_dir=config.output_dir, obsidian_output=config.obsidian_output)
    return plan


def write_forum_expression_expansion_artifacts(
    plan: dict[str, Any],
    *,
    output_dir: Path,
    obsidian_output: Path | None = None,
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "summary": str(_write_json(output_dir / "summary.json", _summary(plan))),
        "candidates": str(_write_jsonl(output_dir / "forum_expansion_candidates.jsonl", plan["candidates"])),
        "rejected": str(_write_jsonl(output_dir / "screened_out.jsonl", plan["rejected"])),
        "markdown": str(_write_text(output_dir / "forum_expression_expansion.md", plan["markdown"])),
    }
    if obsidian_output:
        obsidian_output.parent.mkdir(parents=True, exist_ok=True)
        files["obsidian"] = str(_write_text(obsidian_output, plan["markdown"]))
    plan["files"] = files
    return files


def render_forum_expression_expansion_markdown(
    plan: dict[str, Any],
    *,
    config: WQForumExpressionExpanderConfig,
) -> str:
    summary = plan.get("summary") or {}
    lines = [
        "---",
        "tags:",
        "  - worldquant_harness",
        "  - worldquant",
        "  - forum-expression-expansion",
        f"generated_at: {plan.get('generated_at')}",
        "---",
        "",
        f"# {config.title}",
        "",
        "## Summary",
        "",
        f"- Forum recipes: {summary.get('forum_recipes')}",
        f"- Direction scores: {summary.get('direction_scores')}",
        f"- Draft expressions: {summary.get('drafts')}",
        f"- Selected candidates: {summary.get('selected')}",
        f"- Screened out: {summary.get('screened_out')}",
        f"- Selected themes: {_format_counter(summary.get('selected_themes'))}",
        f"- Rejected reasons: {_format_counter(summary.get('rejected_reasons'))}",
        "",
        "## Selected Candidates",
        "",
        "| Rank | Theme | Family | Policy | Similarity | Expression |",
        "|---:|---|---|---|---:|---|",
    ]
    for row in plan.get("candidates", [])[:30]:
        lines.append(
            f"| {row.get('candidate_rank')} | {_md(row.get('forum_theme_id'))} | "
            f"{_md(row.get('source_family'))} | {_md(row.get('forum_policy_action'))} | "
            f"{_md(row.get('nearest_similarity'))} | "
            f"`{_md(row.get('expression'))}` |"
        )
    lines.extend([
        "",
        "## Expansion Logic",
        "",
        "- Treat forum direct snippets as blocked references; use field-family and operator-family changes.",
        "- Prefer missingness, update-event, group-compare, intraday shock overlay, and sentiment-revision blends.",
        "- Avoid continuing the concentrated EPS/options path unless a genuinely different distribution family is introduced.",
        "- If many drafts are screened out as too similar, shift both field family and operator skeleton before spending WQ simulation budget.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def _forum_recipe_drafts(recipes: list[dict]) -> list[dict]:
    drafts: list[dict] = []
    by_id = {str(row.get("recipe_id") or ""): row for row in recipes}

    missing = by_id.get("missingness_probe_market")
    if missing:
        for field in _recipe_fields(missing)[:5]:
            drafts.append(_draft(
                f"rank(0.46 * group_rank(ts_delta(ts_count_nans({field}, 240), 20), subindustry) + "
                "0.28 * rank(ts_corr(vwap, volume, 40)) + "
                "0.26 * rank(-1 * ts_rank(returns, 120)))",
                tag=f"forum-missingness-{_slug(field)}",
                family="forum_expansion_missingness_probe",
                theme="missingness_coverage",
                recipe="missingness_probe_market",
                rationale="Forum missingness/coverage recipe expanded with microstructure and long reversal overlays.",
            ))

    industry = by_id.get("industry_rank_value_anchor")
    if industry:
        for field in _recipe_fields(industry)[:5]:
            drafts.append(_draft(
                f"rank(0.48 * group_rank(ts_rank({field}, 100), industry) + "
                "0.22 * rank(ts_corr(vwap, volume, 40)) + "
                "0.18 * ts_rank(snt1_d1_netearningsrevision, 80) - "
                "0.12 * ts_rank(returns, 60))",
                tag=f"forum-group-value-{_slug(field)}",
                family="forum_expansion_internal_group_value",
                theme="internal_group_compare",
                recipe="industry_rank_value_anchor",
                rationale="Use forum group-comparison idea on slow value anchors with a revision overlay.",
            ))

    update = by_id.get("update_event_subindustry")
    if update:
        for field in _recipe_fields(update)[:5]:
            drafts.append(_draft(
                f"rank(0.38 * group_rank(rank(ts_delta({field}, 5)), subindustry) + "
                f"0.27 * group_rank(rank(ts_std({field}, 22)), subindustry) + "
                "0.20 * rank(ts_corr(vwap, volume, 40)) - "
                "0.15 * ts_rank(returns, 40))",
                tag=f"forum-update-event-{_slug(field)}",
                family="forum_expansion_update_event_subindustry",
                theme="field_update_event",
                recipe="update_event_subindustry",
                rationale="Convert forum update-event guidance into subindustry-relative field-change signals.",
            ))

    trade_when = by_id.get("trade_when_slow_anchor")
    if trade_when:
        for field in _recipe_fields(trade_when)[:4]:
            drafts.append(_draft(
                f"trade_when(volume > adv20, rank(0.52 * ts_rank({field}, 100) + "
                "0.24 * rank(ts_corr(vwap, volume, 40)) + "
                "0.14 * ts_rank(snt1_d1_netearningsrevision, 60) - "
                "0.10 * ts_rank(returns, 60)), -1)",
                tag=f"forum-tradewhen-anchor-{_slug(field)}",
                family="forum_expansion_regime_slow_anchor",
                theme="regime_trade_when",
                recipe="trade_when_slow_anchor",
                rationale="Use forum trade_when regime control only around a slow anchor, not as a standalone template.",
            ))

    sentiment = by_id.get("sentiment_revision_overlay")
    if sentiment:
        anchors = [
            "forward_cash_flow_to_price",
            "forward_book_value_to_price",
            "cashflow_op / enterprise_value",
            "actual_sales_value_quarterly / enterprise_value",
        ]
        for sentiment_field in _recipe_fields(sentiment)[:3]:
            for anchor in anchors[:3]:
                drafts.append(_draft(
                    f"rank(0.48 * ts_rank({anchor}, 100) + "
                    f"0.28 * ts_rank({sentiment_field}, 40) + "
                    "0.14 * rank(ts_corr(vwap, volume, 40)) - "
                    "0.10 * ts_rank(returns, 60))",
                    tag=f"forum-sentrev-{_slug(sentiment_field)}-{_slug(anchor)}",
                    family="forum_expansion_sentiment_revision_anchor",
                    theme="sentiment_news_revision",
                    recipe="sentiment_revision_overlay",
                    rationale="Blend forum sentiment/revision overlays with slow value anchors to avoid direct fast templates.",
                ))
    return drafts


def _forum_theme_hybrid_drafts(directions: list[dict]) -> list[dict]:
    preferred = {str(row.get("theme_id") or "") for row in directions if str(row.get("action") or "") == "prefer"}
    drafts = []
    if "intraday_volume_shock" in preferred or not directions:
        shock = "rank(ts_rank(((high - low) / close) * (volume / adv20), 20))"
        drafts.extend([
            _draft(
                f"rank(0.44 * ts_rank(forward_cash_flow_to_price, 120) + "
                f"0.24 * {shock} + 0.20 * ts_rank(snt1_d1_netearningsrevision, 80) - "
                "0.12 * ts_rank(returns, 60))",
                tag="forum-hybrid-intraday-forward-revision",
                family="forum_expansion_intraday_forward_revision",
                theme="intraday_volume_shock",
                recipe="theme_hybrid",
                rationale="Use intraday volume shock as an overlay on forward value and revision, per forum playbook.",
            ),
            _draft(
                f"rank(0.42 * group_rank(ts_rank(cashflow_op / enterprise_value, 100), industry) + "
                f"0.24 * {shock} + 0.20 * rank(ts_corr(vwap, volume, 40)) - "
                "0.14 * ts_rank(returns, 80))",
                tag="forum-hybrid-intraday-cashflow-group",
                family="forum_expansion_intraday_cashflow_group",
                theme="intraday_volume_shock",
                recipe="theme_hybrid",
                rationale="Combine forum intraday shock with group-relative cashflow value instead of standalone microstructure.",
            ),
        ])
    drafts.extend([
        _draft(
            "rank(0.34 * group_rank(ts_delta(ts_count_nans(snt1_d1_netearningsrevision, 240), 20), subindustry) + "
            "0.28 * group_rank(rank(ts_delta(forward_sales_to_price, 5)), subindustry) + "
            "0.23 * ts_rank(scl12_sentiment_fast_d1, 40) - 0.15 * ts_rank(returns, 80))",
            tag="forum-hybrid-missingness-update-sentiment",
            family="forum_expansion_missingness_update_sentiment",
            theme="missingness_coverage",
            recipe="theme_combo_missing_update_sentiment",
            rationale="Combine forum missingness, update-event, and sentiment-revision themes.",
        ),
        _draft(
            "rank(0.40 * group_rank(ts_rank(actual_sales_value_quarterly / assets, 100), subindustry) + "
            "0.25 * group_rank(rank(ts_delta(anl4_af_eps_value, 5)), subindustry) + "
            "0.20 * rank(ts_corr(vwap, volume, 40)) - 0.15 * ts_rank(returns, 80))",
            tag="forum-hybrid-group-sales-update",
            family="forum_expansion_group_update_value",
            theme="internal_group_compare",
            recipe="theme_combo_group_update",
            rationale="Forum theme combination field_update_event + internal_group_compare on less-used sales/assets anchor.",
        ),
    ])
    return drafts


def _forum_structure_shift_drafts(recipes: list[dict], directions: list[dict]) -> list[dict]:
    """Create forum-derived drafts that deliberately change the operator skeleton."""

    if not recipes and not directions:
        return []
    return [
        _draft(
            "rank(0.42 * group_zscore(ts_rank(ts_count_nans(implied_volatility_mean_30, 180), 90), sector) - "
            "0.28 * group_zscore(ts_rank(ts_count_nans(snt1_d1_netearningsrevision, 180), 90), industry) + "
            "0.18 * zscore(ts_rank(forward_book_value_to_price, 120)) + "
            "0.12 * zscore(ts_rank(volume / adv20, 30)))",
            tag="forum-shift-missingness-coverage-spread",
            family="forum_expansion_missingness_structure_shift",
            theme="missingness_coverage",
            recipe="structure_shift_missingness",
            rationale="Turn the forum missingness idea into a coverage-spread signal instead of the active ts_delta template.",
        ),
        _draft(
            "rank(0.40 * zscore(days_from_last_change(ts_count_nans(actual_sales_value_quarterly, 240))) + "
            "0.32 * group_zscore(ts_delta(snt1_d1_netearningsrevision, 3), subindustry) + "
            "0.18 * ts_rank(forward_sales_to_price, 100) - "
            "0.10 * ts_rank(close / vwap, 20))",
            tag="forum-shift-missingness-recency-revision",
            family="forum_expansion_missingness_structure_shift",
            theme="missingness_coverage",
            recipe="structure_shift_missingness",
            rationale="Use missingness event recency plus revision change, avoiding the crowded coverage-count delta expression.",
        ),
        _draft(
            "rank(0.36 * group_zscore(ts_decay_linear(ts_delta(anl4_af_eps_value, 5), 12), subindustry) + "
            "0.28 * group_zscore(ts_rank(abs(ts_delta(scl12_sentiment_fast_d1, 1)), 20), industry) + "
            "0.22 * ts_rank(forward_book_value_to_price, 80) - "
            "0.14 * ts_rank(close / vwap, 30))",
            tag="forum-shift-update-decay-sentiment",
            family="forum_expansion_update_event_structure_shift",
            theme="field_update_event",
            recipe="structure_shift_update_event",
            rationale="Translate forum update-event guidance into decayed EPS change plus sentiment event intensity.",
        ),
        _draft(
            "trade_when(ts_rank(volume / adv20, 20) > 0.55, "
            "rank(0.44 * group_zscore(ts_delta(forward_sales_to_price, 10), industry) + "
            "0.30 * zscore(ts_std(actual_sales_value_quarterly, 63)) + "
            "0.16 * ts_rank(snt1_cored1_score, 40) - "
            "0.10 * ts_rank(open / close, 20)), -1)",
            tag="forum-shift-update-regime-sales",
            family="forum_expansion_update_event_structure_shift",
            theme="field_update_event",
            recipe="structure_shift_update_event",
            rationale="Gate sales and forward-sales update events by liquidity regime without copying the standard trade_when template.",
        ),
        _draft(
            "rank(group_neutralize(0.40 * zscore(ts_rank(actual_sales_value_quarterly / assets, 120)) + "
            "0.30 * group_zscore(ts_delta(anl4_af_eps_value / close, 10), subindustry) + "
            "0.20 * ts_rank(snt1_d1_analystcoverage, 80) - "
            "0.10 * rank(abs(close / vwap)), industry))",
            tag="forum-shift-group-neutralized-sales-eps",
            family="forum_expansion_internal_group_structure_shift",
            theme="internal_group_compare",
            recipe="structure_shift_group_compare",
            rationale="Keep the forum group-comparison idea but use group neutralization and analyst coverage as a separate axis.",
        ),
        _draft(
            "rank(0.36 * group_zscore(ts_rank(cashflow_op / enterprise_value, 180), sector) + "
            "0.28 * group_zscore(ts_rank(forward_cash_flow_to_price, 100), industry) + "
            "0.22 * zscore(ts_delta(snt1_d1_netearningsrevision, 5)) - "
            "0.14 * ts_rank(high / low, 20))",
            tag="forum-shift-value-quality-revision",
            family="forum_expansion_value_quality_structure_shift",
            theme="fundamental_value_quality",
            recipe="structure_shift_value_quality",
            rationale="Use forum value anchors with group-zscore and revision change instead of another value-plus-volume clone.",
        ),
        _draft(
            "rank(0.34 * zscore(ts_mean(scl12_sentiment_fast_d1, 5)) + "
            "0.30 * group_zscore(ts_delta(snt1_d1_netearningsrevision, 10), subindustry) + "
            "0.24 * ts_rank(forward_cash_flow_to_price, 120) - "
            "0.12 * rank(ts_rank(close / vwap, 20)))",
            tag="forum-shift-sentiment-term-split",
            family="forum_expansion_sentiment_structure_shift",
            theme="sentiment_news_revision",
            recipe="structure_shift_sentiment",
            rationale="Split fast sentiment and slower revision terms around a value anchor, per the forum overlay theme.",
        ),
        _draft(
            "trade_when(abs(ts_delta(snt1_d1_netearningsrevision, 1)) > 0, "
            "rank(0.42 * ts_rank(actual_sales_value_quarterly / enterprise_value, 100) + "
            "0.30 * zscore(ts_mean(scl12_sentiment_fast_d1, 10)) + "
            "0.18 * group_zscore(forward_book_value_to_price, industry) - "
            "0.10 * ts_rank(volume / adv20, 30)), -1)",
            tag="forum-shift-sentiment-event-gated-value",
            family="forum_expansion_sentiment_structure_shift",
            theme="sentiment_news_revision",
            recipe="structure_shift_sentiment",
            rationale="Use revision events as the gate and value/sentiment as payload, avoiding a standalone sentiment template.",
        ),
        _draft(
            "rank(0.36 * zscore(ts_argmax(high / low, 20)) + "
            "0.30 * group_zscore(ts_rank(forward_sales_to_price, 100), industry) + "
            "0.22 * ts_delta(snt1_d1_netearningsrevision, 5) - "
            "0.12 * ts_rank(volume / adv20, 60))",
            tag="forum-shift-intraday-shock-forward-sales",
            family="forum_expansion_intraday_structure_shift",
            theme="intraday_volume_shock",
            recipe="structure_shift_intraday",
            rationale="Convert intraday shock into a timing layer around forward sales and revision instead of a pure liquidity alpha.",
        ),
        _draft(
            "trade_when(ts_rank(abs(close / vwap), 20) > 0.60, "
            "rank(0.46 * group_zscore(ts_rank(cashflow_op / cap, 120), subindustry) + "
            "0.28 * ts_rank(snt1_cored1_score, 60) + "
            "0.16 * zscore(ts_delta(forward_sales_to_price, 5)) - "
            "0.10 * ts_rank(returns, 100)), -1)",
            tag="forum-shift-regime-vwap-dislocation",
            family="forum_expansion_regime_structure_shift",
            theme="regime_trade_when",
            recipe="structure_shift_regime",
            rationale="Use forum trade_when guidance as a vwap-dislocation regime around slow cashflow and revision payloads.",
        ),
    ]


def _draft(
    expression: str,
    *,
    tag: str,
    family: str,
    theme: str,
    recipe: str,
    rationale: str,
) -> dict[str, Any]:
    return {
        "expression": expression,
        "tag": tag,
        "source": "wq_forum_expression_expander",
        "source_family": family,
        "mutation_strategy": recipe,
        "forum_theme_id": theme,
        "forum_recipe_id": recipe,
        "rationale": rationale,
        "expected_low_corr_reason": "Forum idea expanded with orthogonal field/operator overlays and screened against active inventory.",
        "risk_flags": ["forum_expansion", "not_forum_direct_template"],
        "llm_provider": "none",
        "no_external_llm": True,
    }


def _load_forum_recipes(memory_dirs: tuple[Path, ...]) -> list[dict]:
    rows = []
    for directory in memory_dirs:
        path = directory / "forum_candidate_recipes.jsonl"
        rows.extend(_load_jsonl(path))
    return rows


def _load_direction_scores(paths: tuple[Path, ...]) -> list[dict]:
    rows = []
    for path in paths:
        rows.extend(_load_jsonl(path))
    return rows


def _load_rows(paths: tuple[Path, ...]) -> list[dict]:
    rows = []
    for path in paths:
        if not path.exists():
            continue
        if path.suffix.lower() == ".json":
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError:
                continue
            if isinstance(data, list):
                rows.extend(row for row in data if isinstance(row, dict))
            elif isinstance(data, dict):
                for key in ("rows", "records", "active", "real_active", "virtual_active", "ready"):
                    value = data.get(key)
                    if isinstance(value, list):
                        rows.extend(row for row in value if isinstance(row, dict))
                if not rows and data.get("expression"):
                    rows.append(data)
            continue
        rows.extend(_load_jsonl(path))
    return rows


def _load_inventory_rows(paths: tuple[Path, ...]) -> list[dict]:
    return _load_rows(paths)


def _load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _dedupe_by_expression(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for row in rows:
        expression = str(row.get("expression") or "").strip()
        if not expression:
            continue
        key = expression.lower().replace(" ", "")
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _selected_candidate_sort_key(row: dict) -> tuple:
    policy_rank = 1 if str(row.get("forum_policy_action") or "") == "penalize" else 0
    similarity = _safe_float(row.get("nearest_similarity")) or 0.0
    priority = _safe_float(row.get("research_priority_score")) or 0.0
    return (
        policy_rank,
        similarity,
        -priority,
        str(row.get("source_family") or ""),
        str(row.get("tag") or ""),
    )


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _recipe_fields(recipe: dict) -> list[str]:
    return [str(value) for value in recipe.get("fields") or [] if str(value).strip()]


def _slug(value: str) -> str:
    text = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-")[:48] or "field"


def _summary(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": plan.get("ok"),
        "generated_at": plan.get("generated_at"),
        "summary": plan.get("summary"),
        "inputs": plan.get("inputs"),
        "files": plan.get("files", {}),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _format_counter(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return ""
    return ", ".join(f"{key}={count}" for key, count in sorted(value.items()))
