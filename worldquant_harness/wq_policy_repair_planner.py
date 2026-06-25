"""Policy-aware deterministic repair planning for WQ presubmit misses.

The planner consumes reviewed presubmit rows and emits local-only repair
candidates. It is intentionally conservative: self-correlation repairs change
field families, while concentration repairs smooth and diversify weights.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .expression_parser import extract_components, normalize_expression
from .wq_auto_mining import validate_wq_expression
from .wq_forum_submission_optimizer import annotate_candidate_with_policy, load_submission_policy

SPARSE_CONCENTRATION_FIELDS = {
    "actual_dividend_value_quarterly",
    "actual_cashflow_per_share_value_quarterly",
    "cashflow",
    "cashflow_fin",
    "cashflow_op",
    "dividends_to_gross_profit",
    "enterprise_value",
}
SPARSE_CONCENTRATION_PREFIXES = ("pcr_",)
GROUP_DISTRIBUTION_OPERATORS = {
    "group_neutralize",
    "group_rank",
    "group_zscore",
}
GROUP_FIELDS = {"industry", "sector", "subindustry", "market"}
BLOCKED_SETTINGS_ONLY_REPAIR_FAMILIES = {
    "repair_metric_threshold_settings",
    "repair_metric_threshold_smoothing",
    "repair_concentration_generic",
}
BLOCKED_SETTINGS_ONLY_REPAIR_STRATEGIES = {
    "metric_near_miss_decay_truncation_retest",
    "metric_near_miss_max_position_retest",
    "metric_near_miss_smooth_group_neutralize",
    "smooth_group_neutralize",
}
BROAD_DISPERSION_FIELDS = {
    "adv20",
    "cap",
    "close",
    "high",
    "low",
    "open",
    "volume",
    "vwap",
    "forward_book_value_to_price",
    "forward_cash_flow_to_price",
    "forward_sales_to_price",
    "coefficient_variation_fy1_eps",
    "credit_risk_premium_indicator",
    "earnings_certainty_rank_derivative",
    "relative_valuation_rank_derivative",
}
PRICE_VOLUME_DISPERSION_FIELDS = {"adv20", "close", "high", "low", "open", "volume", "vwap"}


@dataclass(frozen=True)
class PolicyRepairPlannerConfig:
    review_paths: tuple[Path, ...] = field(default_factory=tuple)
    output_dir: Path | None = None
    submission_policy_file: Path | None = None
    obsidian_output: Path | None = None
    max_candidates: int = 40
    max_repairs_per_row: int = 4
    title: str = "worldquant-harness presubmit repair plan"


def build_policy_repair_plan(config: PolicyRepairPlannerConfig) -> dict[str, Any]:
    review_rows = _load_rows(config.review_paths)
    policy = load_submission_policy(config.submission_policy_file)
    repair_records = build_policy_repair_records(
        review_rows,
        submission_policy=policy,
        max_repairs_per_row=config.max_repairs_per_row,
    )
    candidates = build_policy_repair_candidates(repair_records, submission_policy=policy)
    candidates = sorted(candidates, key=_candidate_sort_key)[: max(0, config.max_candidates)]
    for index, row in enumerate(candidates, start=1):
        row["candidate_rank"] = index
    guard_summary = _repair_guard_summary(repair_records)
    markdown = render_policy_repair_markdown(
        review_rows=review_rows,
        repair_records=repair_records,
        candidates=candidates,
        config=config,
    )
    plan = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "review_rows": len(review_rows),
            "repair_records": len(repair_records),
            "candidates": len(candidates),
            "review_buckets": dict(sorted(Counter(row.get("triage_bucket") for row in review_rows).items())),
            "repair_kinds": dict(sorted(Counter(row.get("failure_kind") for row in repair_records).items())),
            "candidate_policy_actions": dict(sorted(Counter(
                row.get("forum_policy_action") for row in candidates if row.get("forum_policy_action")
            ).items())),
            "candidate_guard_blocks": guard_summary["total"],
            "candidate_guard_block_reasons": guard_summary["reasons"],
        },
        "repair_records": repair_records,
        "candidates": candidates,
        "markdown": markdown,
    }
    if config.output_dir or config.obsidian_output:
        write_policy_repair_artifacts(plan, output_dir=config.output_dir, obsidian_output=config.obsidian_output)
    return plan


def build_policy_repair_records(
    review_rows: list[dict],
    *,
    submission_policy: dict[str, Any] | None = None,
    max_repairs_per_row: int = 4,
) -> list[dict]:
    records = []
    for row in review_rows:
        if not _is_repairable_review_row(row):
            continue
        candidates = _repair_candidates_for_row(row)
        candidates = _dedupe_candidates(candidates)[: max(0, max_repairs_per_row)]
        if not candidates:
            continue
        source = str(row.get("expression") or row.get("source_expression") or "")
        candidate_records = [
            annotate_candidate_with_policy(item, submission_policy) if submission_policy else item
            for item in candidates
        ]
        guard_rejected = _guard_rejected_candidates(candidate_records)
        record = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "alpha_id": row.get("alpha_id"),
            "tag": row.get("tag"),
            "source_expression": source,
            "source_fields": sorted(_fields(row)),
            "failure_kind": _failure_kind(row),
            "triage_reason": row.get("triage_reason"),
            "sc_value": _safe_float(row.get("sc_value")),
            "sharpe": _safe_float(row.get("sharpe")),
            "fitness": _safe_float(row.get("fitness")),
            "turnover": _safe_float(row.get("turnover")),
            "repair_objective": _repair_objective(row),
            "candidate_expressions": [item["expression"] for item in candidates],
            "candidate_records": candidate_records,
            "guard_rejected_candidates": guard_rejected,
            "repair_guard_rejected": len(guard_rejected),
            "risk_notes": _risk_notes(row),
            "source_row": row,
        }
        records.append(record)
    return records


def build_policy_repair_candidates(
    repair_records: list[dict],
    *,
    submission_policy: dict[str, Any] | None = None,
) -> list[dict]:
    out = []
    seen = set()
    blocked_no_settings_keys = {
        normalize_expression(str(record.get("source_expression") or ""))
        for record in repair_records
        if str(record.get("source_expression") or "").strip()
    }
    for record in repair_records:
        for item in record.get("candidate_records") or []:
            expression = str(item.get("expression") or "")
            if not expression:
                continue
            key = _candidate_dedupe_key(item)
            settings = _clean_simulation_settings(item.get("simulation_settings") or item.get("settings_override"))
            if not settings and normalize_expression(expression) in blocked_no_settings_keys:
                continue
            if not _locally_valid(expression):
                continue
            if _is_settings_only_repair(item):
                continue
            if _repair_candidate_concentration_risk(expression):
                continue
            if key in seen:
                continue
            seen.add(key)
            row = annotate_candidate_with_policy(item, submission_policy) if submission_policy else dict(item)
            if row.get("forum_policy_action") == "block":
                continue
            out.append({
                **row,
                "source": "wq_policy_repair_planner",
                "source_alpha_id": record.get("alpha_id"),
                "repair_failure_kind": record.get("failure_kind"),
                "repair_source_tag": record.get("tag"),
                "repair_objective": record.get("repair_objective"),
            })
    return out


def write_policy_repair_artifacts(
    plan: dict[str, Any],
    *,
    output_dir: Path | None = None,
    obsidian_output: Path | None = None,
) -> dict[str, str]:
    files: dict[str, str] = {}
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        files["summary"] = str(_write_json(output_dir / "summary.json", _summary(plan)))
        files["repair_records"] = str(_write_jsonl(output_dir / "repair_records.jsonl", plan["repair_records"]))
        files["candidates"] = str(_write_jsonl(output_dir / "repair_candidates.jsonl", plan["candidates"]))
        files["markdown"] = str(_write_text(output_dir / "repair_plan.md", plan["markdown"]))
    if obsidian_output:
        obsidian_output.parent.mkdir(parents=True, exist_ok=True)
        files["obsidian"] = str(_write_text(obsidian_output, plan["markdown"]))
    plan.setdefault("files", {}).update(files)
    return files


def render_policy_repair_markdown(
    *,
    review_rows: list[dict],
    repair_records: list[dict],
    candidates: list[dict],
    config: PolicyRepairPlannerConfig,
) -> str:
    lines = [
        "---",
        "tags:",
        "  - worldquant_harness",
        "  - worldquant",
        "  - presubmit-repair",
        f"generated_at: {datetime.now(timezone.utc).isoformat()}",
        "---",
        "",
        f"# {config.title}",
        "",
        "## Summary",
        "",
        f"- Reviewed rows: {len(review_rows)}",
        f"- Repair records: {len(repair_records)}",
        f"- Candidate expressions: {len(candidates)}",
        "",
        "## Repair Candidates",
        "",
        "| Rank | Source alpha | Kind | Score | Settings | Expression |",
        "|---:|---|---|---:|---|---|",
    ]
    for row in candidates[:20]:
        lines.append(
            f"| {row.get('candidate_rank', '')} | {_md(row.get('source_alpha_id'))} | "
            f"{_md(row.get('repair_failure_kind'))} | {row.get('repair_priority_score', '')} | "
            f"{_md(row.get('settings_hint'))} | "
            f"`{_md(row.get('expression'))}` |"
        )
    lines.extend([
        "",
        "## Rules Applied",
        "",
        "- Self-correlation repairs replace at least one crowded field family instead of only changing windows.",
        "- Concentrated-weight repairs now block sparse-field/group stacks before simulation.",
        "- Forum-direct templates remain blocked by submission policy unless an orthogonal overlay is present.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def _guard_rejected_candidates(candidates: list[dict]) -> list[dict]:
    rejected = []
    for row in candidates:
        expression = str(row.get("expression") or "")
        risk = _repair_candidate_concentration_risk(expression)
        if not risk:
            continue
        rejected.append({
            **row,
            "repair_guard_action": "block",
            "repair_guard_reason": "concentration_sparse_group_risk",
            "concentration_risk": risk,
        })
    return rejected


def _repair_guard_summary(repair_records: list[dict]) -> dict[str, Any]:
    reasons: Counter[str] = Counter()
    total = 0
    for record in repair_records:
        for row in record.get("guard_rejected_candidates") or []:
            total += 1
            reasons.update((row.get("concentration_risk") or {}).get("reasons") or [])
    return {
        "total": total,
        "reasons": dict(sorted(reasons.items())),
    }


def _repair_candidate_concentration_risk(expression: str) -> dict[str, Any] | None:
    try:
        parts = extract_components(expression or "")
    except Exception:
        return None
    fields = {str(item) for item in parts.get("fields", set())}
    sparse_fields = sorted(field for field in fields if _is_sparse_concentration_field(field))
    if not sparse_fields:
        return None
    operators = {str(item) for item in parts.get("operators", set())}
    group_operator_count = _group_distribution_operator_count(expression, operators)
    pcr_fields = sorted(field for field in sparse_fields if field.startswith("pcr_"))
    denominator_sparse_fields = sorted(
        field for field in sparse_fields if _field_used_as_denominator(expression, field)
    )
    broad_dispersion_fields = sorted(field for field in fields if _is_broad_dispersion_field(field))
    price_volume_dispersion_fields = sorted(field for field in fields if _is_price_volume_dispersion_field(field))

    reasons: list[str] = []
    if len(sparse_fields) > 1 and group_operator_count > 0:
        reasons.append("multiple_sparse_legs_with_group_ops")
    if denominator_sparse_fields and group_operator_count > 0:
        reasons.append("sparse_denominator_group_stack")
    if denominator_sparse_fields and len(sparse_fields) > 1 and group_operator_count > 0:
        reasons.append("sparse_denominator_plus_other_sparse_group_leg")
    if len(sparse_fields) == 1 and group_operator_count > 0 and not broad_dispersion_fields:
        reasons.append("single_sparse_group_without_broad_dispersion_leg")
    if pcr_fields and not price_volume_dispersion_fields:
        reasons.append("pcr_sparse_leg_without_price_volume_dispersion")
    if not reasons:
        return None

    return {
        "reasons": sorted(set(reasons)),
        "sparse_fields": sparse_fields,
        "sparse_field_count": len(sparse_fields),
        "pcr_fields": pcr_fields,
        "denominator_sparse_fields": denominator_sparse_fields,
        "broad_dispersion_fields": broad_dispersion_fields,
        "price_volume_dispersion_fields": price_volume_dispersion_fields,
        "group_operator_count": group_operator_count,
        "lesson": (
            "Do not repair concentrated-weight failures with lower truncation, decay, or stacked group ranks only. "
            "Keep at most one sparse main leg; PCR legs need explicit price-volume dispersion."
        ),
    }


def repair_candidate_concentration_risk(expression: str) -> dict[str, Any] | None:
    """Public guard used by workflow ingestion of historical repair queues."""
    return _repair_candidate_concentration_risk(expression)


def _is_settings_only_repair(row: dict) -> bool:
    family = str(row.get("source_family") or "")
    strategy = str(row.get("mutation_strategy") or "")
    tag = str(row.get("tag") or "").lower()
    if family in BLOCKED_SETTINGS_ONLY_REPAIR_FAMILIES:
        return True
    if strategy in BLOCKED_SETTINGS_ONLY_REPAIR_STRATEGIES:
        return True
    return "metric-retest" in tag or "metric-smooth" in tag or tag.endswith("smooth-industry")


def _is_sparse_concentration_field(field: str) -> bool:
    text = str(field or "")
    if text in SPARSE_CONCENTRATION_FIELDS:
        return True
    if any(text.startswith(prefix) for prefix in SPARSE_CONCENTRATION_PREFIXES):
        return True
    return "dividend" in text


def _group_distribution_operator_count(expression: str, operators: set[str]) -> int:
    text = str(expression or "")
    return max(
        sum(1 for operator in operators if operator in GROUP_DISTRIBUTION_OPERATORS),
        sum(text.count(f"{operator}(") for operator in GROUP_DISTRIBUTION_OPERATORS),
    )


def _field_used_as_denominator(expression: str, field: str) -> bool:
    compact = re.sub(r"\s+", "", str(expression or "").lower())
    escaped_field = re.escape(str(field or "").lower())
    return bool(re.search(rf"/(?:ts_backfill\()?{escaped_field}\b", compact))


def _is_broad_dispersion_field(field: str) -> bool:
    text = str(field or "")
    if text in GROUP_FIELDS or text == "returns" or _is_sparse_concentration_field(text):
        return False
    return text in BROAD_DISPERSION_FIELDS or bool(re.fullmatch(r"adv\d+", text or ""))


def _is_price_volume_dispersion_field(field: str) -> bool:
    text = str(field or "")
    return text in PRICE_VOLUME_DISPERSION_FIELDS or bool(re.fullmatch(r"adv\d+", text or ""))


def _repair_candidates_for_row(row: dict) -> list[dict]:
    fields = _fields(row)
    expression = str(row.get("expression") or row.get("source_expression") or "")
    base_tag = str(row.get("tag") or row.get("alpha_id") or "repair")
    parent = [row.get("alpha_id")] if row.get("alpha_id") else []
    failure = _failure_kind(row)
    out: list[dict] = []
    if failure == "concentrated_weight":
        out.extend(_concentration_repairs(base_tag, parent, source_expression=expression, source_row=row))
    if failure == "self_correlation_fail":
        out.extend(_self_corr_repairs(fields, base_tag, parent))
    if failure == "metric_threshold_near_miss":
        out.extend(_metric_threshold_repairs(fields, base_tag, parent, source_expression=expression, source_row=row))
    if not out and expression:
        out.extend(_generic_repairs(expression, base_tag, parent, failure))
    return out


def _self_corr_repairs(fields: set[str], tag: str, parent: list[Any]) -> list[dict]:
    out: list[dict] = []
    if {
        "equity",
        "cap",
        "forward_sales_to_price",
        "change_in_eps_surprise",
        "snt1_d1_netearningsrevision",
    } <= fields:
        out.extend([
            _candidate(
                "rank(0.32 * ts_rank(forward_book_value_to_price, 140) + "
                "0.22 * ts_rank(coefficient_variation_fy1_eps, 120) + "
                "0.18 * ts_rank(change_in_eps_surprise, 100) + "
                "0.16 * rank(ts_corr(vwap, volume, 120)) - "
                "0.12 * ts_rank(returns, 140))",
                tag=f"repair-{tag}-book-cv-eps-liquidity-no-snt",
                family="repair_self_corr_equity_sales_eps_rebuild",
                strategy="replace_equity_snt_with_forward_book_eps_dispersion",
                parent_alpha_ids=parent,
                rationale="Replace the high-self-correlation equity/SNT core with forward book, EPS dispersion, and a broad price-volume leg.",
            ),
            _candidate(
                "rank(group_neutralize(0.24 * ts_rank(equity / cap, 140) + "
                "0.24 * ts_rank(forward_sales_to_price, 150) + "
                "0.18 * ts_rank(coefficient_variation_fy1_eps, 120) + "
                "0.16 * rank(ts_corr(close, volume, 100)) - "
                "0.14 * ts_rank(returns, 150), sector))",
                tag=f"repair-{tag}-equity-forward-cv-sector-broad",
                family="repair_self_corr_equity_sales_eps_rebuild",
                strategy="slow_equity_forward_sales_with_price_volume_dispersion",
                parent_alpha_ids=parent,
                rationale="Keep one broad equity/value leg but remove the SNT revision leg and use slower sector-neutral dispersion.",
            ),
            _candidate(
                "rank(0.26 * ts_rank(forward_sales_to_price, 160) + "
                "0.22 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.20 * ts_rank(earnings_revision_magnitude, 140) + "
                "0.18 * rank(volume / adv20) - "
                "0.14 * ts_rank(returns, 160))",
                tag=f"repair-{tag}-forward-revision-liquidity-no-equity",
                family="repair_self_corr_equity_sales_eps_rebuild",
                strategy="replace_equity_eps_revision_with_model77_liquidity",
                parent_alpha_ids=parent,
                rationale="Move the signal into model77 revision/certainty plus liquidity dispersion to change the active field signature.",
            ),
        ])
    if "actual_eps_value_quarterly" in fields and (
        fields & {
            "implied_volatility_call_90",
            "implied_volatility_put_90",
            "implied_volatility_call_120",
            "implied_volatility_put_120",
        }
    ):
        out.extend([
            _candidate(
                "rank(0.30 * group_rank(ts_rank(actual_eps_value_quarterly / enterprise_value, 120), subindustry) + "
                "0.24 * ts_rank(coefficient_variation_fy1_eps, 80) + "
                "0.20 * ts_rank(forward_sales_to_price, 100) + "
                "0.14 * rank(ts_corr(vwap, volume, 90)) + "
                "0.12 * rank(-1 * ts_rank(pcr_oi_60, 80)))",
                tag=f"repair-{tag}-eps-ev-pcr60-forward-sales",
                family="repair_self_corr_eps_forward_options_flow",
                strategy="replace_iv90_iv120_micro_with_forward_pcr",
                parent_alpha_ids=parent,
                rationale="Replace the crowded IV90/IV120 and price microstructure legs with forward value and PCR flow.",
            ),
            _candidate(
                "rank(0.34 * ts_rank(actual_cashflow_per_share_value_quarterly / enterprise_value, 100) + "
                "0.24 * ts_rank(forward_book_value_to_price, 100) + "
                "0.18 * ts_rank(snt1_d1_netearningsrevision, 80) + "
                "0.14 * rank(ts_corr(vwap, volume, 90)) + "
                "0.10 * rank(-1 * ts_rank(pcr_vol_10, 80)))",
                tag=f"repair-{tag}-cashflow-forward-revision-pcrvol",
                family="repair_self_corr_cashflow_revision_flow",
                strategy="field_family_replacement",
                parent_alpha_ids=parent,
                rationale="Move the idea into cashflow, forward value, revision, and option-flow families.",
            ),
        ])
    if {
        "cashflow_op",
        "cashflow_efficiency_rank_derivative",
        "enterprise_value",
    } <= fields and (
        fields & {
            "implied_volatility_call_90",
            "implied_volatility_put_90",
            "implied_volatility_call_120",
            "implied_volatility_put_120",
        }
    ):
        iv120_ratio = "((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120))"
        out.extend([
            _candidate(
                "rank(0.36 * ts_rank(forward_cash_flow_to_price, 160) + "
                "0.22 * ts_rank(forward_book_value_to_price, 140) + "
                "0.16 * rank(-1 * cashflow_efficiency_rank_derivative) + "
                "0.14 * rank(ts_corr(vwap, volume, 90)) - "
                "0.12 * ts_rank(returns, 120))",
                tag=f"repair-{tag}-cashflow-forwardbook-broad",
                family="repair_self_corr_cashflow_iv_near_threshold",
                strategy="replace_sparse_denominator_with_forward_broad_flow",
                parent_alpha_ids=parent,
                rationale="Keep cash-flow value exposure but avoid sparse denominator plus group repair risk.",
            ),
            _candidate(
                "rank(group_neutralize(0.30 * ts_rank(forward_cash_flow_to_price, 170) + "
                "0.22 * ts_rank(forward_book_value_to_price, 150) + "
                "0.16 * rank(-1 * cashflow_efficiency_rank_derivative) + "
                "0.14 * rank(ts_corr(vwap, volume, 100)) - "
                "0.12 * ts_rank(returns, 140), industry))",
                tag=f"repair-{tag}-cashflow-forwardbook-industry-broad",
                family="repair_self_corr_cashflow_iv_near_threshold",
                strategy="industry_neutral_forward_cashflow_broad_flow",
                parent_alpha_ids=parent,
                rationale="Use industry-neutral forward cashflow/book value with price-volume dispersion and no sparse denominator stack.",
            ),
            _candidate(
                "rank(0.76 * rank(0.44 * ts_rank(cashflow_op / enterprise_value, 120) + "
                "0.20 * rank(-1 * cashflow_efficiency_rank_derivative) + "
                "0.14 * rank(ts_corr(close, volume, 40)) + "
                "0.12 * ts_rank(forward_cash_flow_to_price, 120) - "
                "0.12 * ts_rank(returns, 60)) + "
                "0.24 * rank(-1 * ts_rank(pcr_oi_60, 90)))",
                tag=f"repair-{tag}-cashflow-core-pcr-overlay",
                family="repair_self_corr_cashflow_iv_near_threshold",
                strategy="replace_iv_overlay_with_pcr_flow",
                parent_alpha_ids=parent,
                rationale="Keep the strong cashflow core while replacing the high-SC IV90 overlay with slower PCR flow.",
            ),
            _candidate(
                f"rank(0.70 * rank(0.42 * ts_rank(cashflow_op / enterprise_value, 120) + "
                f"0.22 * rank(-1 * cashflow_efficiency_rank_derivative) + "
                f"0.14 * rank(ts_corr(vwap, volume, 60)) + "
                f"0.12 * rank(ts_mean({iv120_ratio}, 12)) - "
                f"0.12 * ts_rank(returns, 80)) + "
                f"0.30 * group_rank(ts_rank(forward_book_value_to_price, 120), industry))",
                tag=f"repair-{tag}-cashflow-forwardbook-iv120",
                family="repair_self_corr_cashflow_iv_near_threshold",
                strategy="dilute_iv_overlay_with_forward_book",
                parent_alpha_ids=parent,
                rationale="Dilute the IV overlay with forward book value while keeping the cashflow-efficiency core.",
            ),
            _candidate(
                f"rank(group_neutralize(0.38 * ts_rank(cashflow_op / enterprise_value, 120) + "
                f"0.20 * rank(-1 * cashflow_efficiency_rank_derivative) + "
                f"0.16 * rank(ts_corr(close, volume, 60)) + "
                f"0.14 * rank(ts_mean({iv120_ratio}, 10)) - "
                f"0.12 * ts_rank(returns, 80), industry))",
                tag=f"repair-{tag}-cashflow-iv120-industry-neutral",
                family="repair_self_corr_cashflow_iv_near_threshold",
                strategy="flatten_nested_iv_cashflow_structure",
                parent_alpha_ids=parent,
                rationale="Flatten the nested cashflow/IV structure and group-neutralize it to reduce self-correlation.",
            ),
        ])
    if {"implied_volatility_call_90", "implied_volatility_put_90"} <= fields:
        if {"cashflow_op", "forward_cash_flow_to_price"} <= fields or {
            "credit_risk_premium_indicator",
            "relative_valuation_rank_derivative",
        } <= fields:
            out.extend([
                _candidate(
                    "rank(group_neutralize(0.22 * ts_rank(forward_cash_flow_to_price, 170) + "
                    "0.15 * ts_rank(cashflow_op, 170) + "
                    "0.14 * rank(-1 * relative_valuation_rank_derivative) + "
                    "0.12 * rank(-1 * credit_risk_premium_indicator) + "
                    "0.11 * rank(ts_corr(vwap, volume, 120)) + "
                    "0.10 * rank(-1 * ts_rank(volume / adv20, 100)) - "
                    "0.14 * ts_rank(returns, 170), industry))",
                    tag=f"repair-{tag}-cashflow-credit-broad-noiv",
                    family="repair_self_corr_active_iv90_noiv_cashflow_credit",
                    strategy="remove_iv90_replace_with_cashflow_broad_dispersion",
                    parent_alpha_ids=parent,
                    rationale="Remove IV90/PCR and enterprise-value denominator while keeping one cashflow leg plus broad liquidity dispersion.",
                ),
                _candidate(
                    "rank(0.66 * rank(group_neutralize(0.20 * ts_rank(forward_cash_flow_to_price, 180) + "
                    "0.14 * ts_rank(cashflow_op, 180) + "
                    "0.12 * rank(-1 * credit_risk_premium_indicator) + "
                    "0.10 * rank(-1 * relative_valuation_rank_derivative) - "
                    "0.14 * ts_rank(returns, 180), sector)) + "
                    "0.19 * rank(ts_corr(vwap, volume, 120)) + "
                    "0.15 * rank(-1 * ts_rank(volume / adv20, 100)))",
                    tag=f"repair-{tag}-cashflow-credit-liquidity-noiv",
                    family="repair_self_corr_active_iv90_noiv_cashflow_credit",
                    strategy="remove_iv90_add_liquidity_no_sparse_stack",
                    parent_alpha_ids=parent,
                    rationale="Replace the sparse PCR/EV stack with broad price-volume dispersion around a single cashflow leg.",
                ),
                _candidate(
                    "rank(group_neutralize(0.24 * ts_rank(forward_cash_flow_to_price, 180) + "
                    "0.14 * rank(-1 * relative_valuation_rank_derivative) + "
                    "0.12 * rank(-1 * credit_risk_premium_indicator) + "
                    "0.12 * rank(-1 * ts_rank(pcr_oi_60, 110)) + "
                    "0.12 * rank(ts_corr(vwap, volume, 120)) - "
                    "0.14 * ts_rank(returns, 180), industry))",
                    tag=f"repair-{tag}-cashflow-credit-pcr-only-noiv",
                    family="repair_self_corr_active_iv90_noiv_cashflow_credit",
                    strategy="remove_iv90_use_single_pcr_leg_broad_dispersion",
                    parent_alpha_ids=parent,
                    rationale="Keep PCR decorrelation as the only sparse leg and disperse it with forward cashflow plus price-volume fields.",
                ),
                _candidate(
                    "rank(0.70 * rank(group_neutralize(0.24 * ts_rank(forward_cash_flow_to_price, 180) + "
                    "0.14 * rank(-1 * credit_risk_premium_indicator) + "
                    "0.12 * rank(-1 * relative_valuation_rank_derivative) - "
                    "0.14 * ts_rank(returns, 180), sector)) + "
                    "0.18 * rank(-1 * ts_rank(pcr_vol_10, 100)) + "
                    "0.12 * rank(volume / adv20))",
                    tag=f"repair-{tag}-cashflow-credit-pcrvol-only-noiv",
                    family="repair_self_corr_active_iv90_noiv_cashflow_credit",
                    strategy="remove_iv90_use_single_pcrvol_leg_broad_dispersion",
                    parent_alpha_ids=parent,
                    rationale="Use PCR volume as the only sparse leg, with forward cashflow and liquidity dispersion.",
                ),
                _candidate(
                    "rank(group_neutralize(0.20 * group_rank(ts_rank(forward_cash_flow_to_price, 150), industry) + "
                    "0.16 * group_rank(ts_rank(cashflow_op / enterprise_value, 120), subindustry) + "
                    "0.12 * rank(-1 * relative_valuation_rank_derivative) + "
                    "0.12 * rank(-1 * credit_risk_premium_indicator) + "
                    "0.12 * rank(-1 * ts_rank(pcr_oi_60, 90)) + "
                    "0.10 * rank(ts_corr(vwap, volume, 100)) - "
                    "0.14 * ts_rank(returns, 130), sector))",
                    tag=f"repair-{tag}-cashflow-credit-pcr-noiv",
                    family="repair_self_corr_active_iv90_noiv_cashflow_credit",
                    strategy="remove_iv90_replace_with_pcr_slow_cashflow",
                    parent_alpha_ids=parent,
                    rationale="Remove the crowded IV90 overlay and keep the cash-flow/credit core with slower PCR and volume flow legs.",
                ),
                _candidate(
                    "rank(0.70 * rank(group_neutralize(0.18 * ts_rank(forward_cash_flow_to_price, 160) + "
                    "0.16 * ts_rank(cashflow_op / enterprise_value, 140) + "
                    "0.12 * rank(-1 * credit_risk_premium_indicator) + "
                    "0.10 * rank(-1 * relative_valuation_rank_derivative) - "
                    "0.14 * ts_rank(returns, 150), industry)) + "
                    "0.18 * rank(-1 * ts_rank(pcr_vol_10, 80)) + "
                    "0.12 * rank(volume / adv20))",
                    tag=f"repair-{tag}-cashflow-credit-pcrvol-liquidity",
                    family="repair_self_corr_active_iv90_noiv_cashflow_credit",
                    strategy="remove_iv90_add_pcrvol_liquidity",
                    parent_alpha_ids=parent,
                    rationale="Replace IV90 with PCR volume and liquidity dispersion while slowing the cash-flow credit core.",
                ),
            ])
        if {"actual_sales_value_quarterly", "forward_sales_to_price"} <= fields:
            out.append(_candidate(
                "rank(group_neutralize(0.22 * ts_rank(ts_backfill(actual_sales_value_quarterly, 140) / cap, 170) + "
                "0.20 * ts_rank(forward_sales_to_price, 170) + "
                "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.12 * ts_rank(earnings_revision_magnitude, 150) + "
                "0.12 * rank(ts_corr(vwap, volume, 120)) - "
                "0.16 * ts_rank(returns, 170), industry))",
                tag=f"repair-{tag}-sales-revision-broad-noiv",
                family="repair_self_corr_active_iv90_noiv_sales_revision",
                strategy="remove_iv90_replace_with_sales_cap_broad_dispersion",
                parent_alpha_ids=parent,
                rationale="Remove IV90/PCR and enterprise-value denominator from the sales/revision repair.",
            ))
            out.append(_candidate(
                "rank(group_rank(0.20 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / enterprise_value, 150) + "
                "0.18 * ts_rank(forward_sales_to_price, 150) + "
                "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.12 * ts_rank(earnings_revision_magnitude, 120) + "
                "0.12 * rank(-1 * ts_rank(pcr_oi_60, 90)) - "
                "0.18 * ts_rank(returns, 130), industry))",
                tag=f"repair-{tag}-sales-revision-pcr-noiv",
                family="repair_self_corr_active_iv90_noiv_sales_revision",
                strategy="remove_iv90_replace_with_pcr_sales_revision",
                parent_alpha_ids=parent,
                rationale="Remove IV90 from the sales/revision family and use a slower PCR flow leg for decorrelation.",
            ))
        if "anl4_adjusted_netincome_ft" in fields and "forward_cash_flow_to_price" in fields:
            out.append(_candidate(
                "rank(group_neutralize(0.22 * ts_rank(anl4_adjusted_netincome_ft / cap, 130) + "
                "0.20 * ts_rank(forward_cash_flow_to_price, 170) + "
                "0.14 * rank(-1 * credit_risk_premium_indicator) + "
                "0.12 * rank(-1 * relative_valuation_rank_derivative) + "
                "0.10 * rank(ts_corr(vwap, volume, 120)) + "
                "0.08 * rank(-1 * ts_rank(close / vwap, 100)) - "
                "0.16 * ts_rank(returns, 170), sector))",
                tag=f"repair-{tag}-netincome-forwardcf-broad-noiv",
                family="repair_self_corr_active_iv90_noiv_netincome_forwardcf",
                strategy="remove_iv90_replace_with_netincome_broad_dispersion",
                parent_alpha_ids=parent,
                rationale="Remove IV90/PCR from the net-income repair and disperse with price-volume legs.",
            ))
            out.append(_candidate(
                "rank(group_neutralize(0.20 * ts_rank(anl4_adjusted_netincome_ft / cap, 110) + "
                "0.18 * ts_rank(forward_cash_flow_to_price, 150) + "
                "0.14 * rank(-1 * credit_risk_premium_indicator) + "
                "0.12 * rank(-1 * relative_valuation_rank_derivative) + "
                "0.12 * rank(-1 * ts_rank(pcr_oi_60, 90)) + "
                "0.08 * rank(-1 * ts_rank(close / vwap, 80)) - "
                "0.16 * ts_rank(returns, 130), sector))",
                tag=f"repair-{tag}-netincome-forwardcf-pcr-noiv",
                family="repair_self_corr_active_iv90_noiv_netincome_forwardcf",
                strategy="remove_iv90_replace_with_pcr_netincome_forwardcf",
                parent_alpha_ids=parent,
                rationale="Remove IV90 from the net-income/forward-cash-flow blend and add PCR flow plus slower reversal.",
            ))
    if {"actual_sales_value_quarterly", "change_in_eps_surprise"} <= fields:
        out.extend([
            _candidate(
                "rank(0.34 * ts_rank(forward_sales_to_price, 100) + "
                "0.24 * ts_rank(coefficient_variation_fy1_eps, 80) + "
                "0.22 * ts_rank(snt1_d1_netearningsrevision, 80) + "
                "0.20 * rank(-1 * ts_rank(pcr_oi_60, 60)))",
                tag=f"repair-{tag}-forward-sales-cv-revision-pcr60",
                family="repair_self_corr_forward_revision_flow",
                strategy="replace_sales_eps_micro_core",
                parent_alpha_ids=parent,
                rationale="Replace actual sales/EPS and close-volume crowding with forward, dispersion, revision, and PCR fields.",
            ),
            _candidate(
                "rank(0.42 * group_rank(ts_rank(actual_cashflow_per_share_value_quarterly / enterprise_value, 100), industry) + "
                "0.24 * ts_rank(forward_book_value_to_price, 100) + "
                "0.18 * ts_rank(snt1_d1_netearningsrevision, 80) - "
                "0.16 * ts_rank(returns, 80))",
                tag=f"repair-{tag}-cashflow-forward-book-revision",
                family="repair_self_corr_cashflow_forward_revision",
                strategy="cashflow_value_replacement",
                parent_alpha_ids=parent,
                rationale="Use cashflow per share and forward book instead of the prior sales/EPS microstructure template.",
            ),
        ])
    if {
        "actual_sales_value_quarterly",
        "earnings_momentum_composite_score",
        "enterprise_value",
        "vwap",
        "volume",
    } <= fields:
        out.extend([
            _candidate(
                "rank(0.30 * ts_rank(forward_sales_to_price, 150) + "
                "0.24 * ts_rank(change_in_eps_surprise, 110) + "
                "0.20 * ts_rank(coefficient_variation_fy1_eps, 120) + "
                "0.16 * rank(ts_corr(close, volume, 120)) - "
                "0.12 * ts_rank(returns, 150))",
                tag=f"repair-{tag}-forward-eps-cv-liquidity-no-ev",
                family="repair_self_corr_sales_earnmom_rebuild",
                strategy="replace_sales_ev_earnmom_with_forward_eps_dispersion",
                parent_alpha_ids=parent,
                rationale="Replace the crowded sales/EV/earnings-momentum signature with forward sales, EPS surprise, EPS dispersion, and price-volume flow.",
            ),
            _candidate(
                "rank(group_neutralize(0.24 * ts_rank(ts_backfill(actual_sales_value_quarterly, 140) / cap, 170) + "
                "0.22 * ts_rank(forward_sales_to_price, 170) + "
                "0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.14 * ts_rank(earnings_revision_magnitude, 150) + "
                "0.10 * rank(ts_corr(vwap, volume, 120)) - "
                "0.14 * ts_rank(returns, 170), industry))",
                tag=f"repair-{tag}-sales-cap-revision-broad-no-ev",
                family="repair_self_corr_sales_earnmom_rebuild",
                strategy="replace_ev_denominator_and_earnmom_with_revision_breadth",
                parent_alpha_ids=parent,
                rationale="Keep sales information but normalize by cap and add broad revision/liquidity legs instead of EV and PCR.",
            ),
            _candidate(
                "rank(0.26 * group_rank(ts_rank(actual_cashflow_per_share_value_quarterly / cap, 150), industry) + "
                "0.22 * ts_rank(forward_book_value_to_price, 140) + "
                "0.18 * ts_rank(snt1_d1_netearningsrevision, 100) + "
                "0.16 * ts_rank(coefficient_variation_fy1_eps, 100) + "
                "0.10 * rank(ts_corr(vwap, volume, 120)) - "
                "0.10 * ts_rank(returns, 140))",
                tag=f"repair-{tag}-cashflow-book-broad-no-sparse-stack",
                family="repair_self_corr_cashflow_book_revision",
                strategy="cashflow_forward_broad_rebuild_no_sparse_stack",
                parent_alpha_ids=parent,
                rationale="Move the sales/earnings-momentum template into cashflow/book/revision without EV/PCR sparse stacking.",
            ),
            _candidate(
                "rank(0.28 * group_rank(ts_rank(ts_backfill(forward_sales_to_price, 120), 120), industry) + "
                "0.22 * ts_rank(snt1_d1_netearningsrevision, 100) + "
                "0.18 * ts_rank(coefficient_variation_fy1_eps, 100) + "
                "0.16 * rank(-1 * ts_rank(ts_backfill(pcr_oi_60, 120), 90)) + "
                "0.16 * ts_rank(forward_book_value_to_price, 120))",
                tag=f"repair-{tag}-forward-revision-dispersion-pcr",
                family="repair_self_corr_forward_revision_dispersion",
                strategy="replace_sales_earnmom_micro_with_forward_revision",
                parent_alpha_ids=parent,
                rationale="Replace the high-SC sales/earnings-momentum/vwap-volume core with forward value, revision, dispersion, and PCR flow.",
            ),
            _candidate(
                "rank(0.24 * group_rank(ts_rank(actual_cashflow_per_share_value_quarterly / enterprise_value, 120), subindustry) + "
                "0.20 * group_rank(ts_rank(forward_book_value_to_price, 120), industry) + "
                "0.18 * ts_rank(snt1_d1_netearningsrevision, 100) + "
                "0.16 * rank(-1 * ts_rank(pcr_vol_10, 80)) + "
                "0.12 * ts_rank(coefficient_variation_fy1_eps, 100) - "
                "0.10 * ts_rank(returns, 120))",
                tag=f"repair-{tag}-cashflow-book-revision-pcrvol",
                family="repair_self_corr_cashflow_book_revision",
                strategy="cashflow_forward_rebuild",
                parent_alpha_ids=parent,
                rationale="Move the idea into cashflow, forward book, analyst revision, and option-volume flow with a slow returns control.",
            ),
        ])
    out.append(_candidate(
        "rank(0.38 * ts_rank(forward_sales_to_price, 120) + "
        "0.26 * ts_rank(coefficient_variation_fy1_eps, 100) + "
        "0.20 * rank(ts_corr(vwap, volume, 100)) + "
        "0.12 * rank(volume / adv20) - "
        "0.10 * ts_rank(returns, 120))",
        tag=f"repair-{tag}-minimal-forward-dispersion-liquidity",
        family="repair_self_corr_minimal_orthogonal",
        strategy="minimal_orthogonal_rebuild_no_pcr",
        parent_alpha_ids=parent,
        rationale="Minimal rebuild using low-active forward sales, EPS dispersion, and broad price-volume/liquidity legs without PCR.",
    ))
    return out


def _concentration_repairs(
    tag: str,
    parent: list[Any],
    *,
    source_expression: str = "",
    source_row: dict | None = None,
) -> list[dict]:
    iv_ratio = "((implied_volatility_call_90 - implied_volatility_put_90) / (implied_volatility_call_90 + implied_volatility_put_90))"
    source_row = source_row or {}
    source_family = str(
        source_row.get("source_family")
        or source_row.get("mutation_strategy")
        or (source_row.get("candidate_meta") or {}).get("source_family")
        or ""
    )
    source_settings = _source_simulation_settings(source_row)
    is_second_stage = source_family.startswith("repair_concentration")
    rows: list[dict] = []
    source_fields = _fields(source_row)
    if {"actual_dividend_value_quarterly", "composite_factor_score_derivative"} <= source_fields:
        rows.extend([
            _candidate(
                "rank(group_neutralize("
                "0.18 * ts_rank(actual_dividend_value_quarterly / cap, 170) + "
                "0.16 * rank(-1 * composite_factor_score_derivative) + "
                "0.14 * ts_rank(fifty_to_two_hundred_day_price_ratio, 140) + "
                "0.12 * rank(ts_corr(close, volume, 100)) + "
                "0.10 * rank(-1 * ts_rank(volume / adv20, 90)) + "
                "0.10 * rank(-1 * correlation_last_30_days_spy) - "
                "0.14 * ts_rank(returns, 170), industry))",
                tag=f"repair-{tag}-composite-dividend-broad-maxpos",
                family="repair_concentration_composite_dividend_dispersed",
                strategy="single_dividend_leg_broad_dispersion",
                parent_alpha_ids=parent,
                rationale="Keep one dividend leg and disperse weights with cap, price-volume, and broad relative-risk legs.",
                simulation_settings={"truncation": 0.05, "maxPosition": "ON"},
            ),
            _candidate(
                "rank(group_neutralize("
                "0.18 * ts_rank(dividends_to_gross_profit, 150) + "
                "0.16 * rank(-1 * composite_factor_score_derivative) + "
                "0.14 * ts_rank(fifty_to_two_hundred_day_price_ratio, 140) + "
                "0.12 * rank(ts_corr(close, volume, 100)) + "
                "0.10 * rank(-1 * ts_rank(volume / adv20, 90)) + "
                "0.10 * rank(-1 * correlation_last_30_days_spy) - "
                "0.14 * ts_rank(returns, 170), sector))",
                tag=f"repair-{tag}-dividend-grossprofit-broad-maxpos",
                family="repair_concentration_composite_dividend_relative",
                strategy="single_dividends_grossprofit_leg_broad_dispersion",
                parent_alpha_ids=parent,
                rationale="Use dividends-to-gross-profit as the only dividend leg, with broad price-volume dispersion.",
                simulation_settings={"truncation": 0.05, "maxPosition": "ON"},
            ),
            _candidate(
                "rank(group_rank(ts_decay_linear(group_neutralize("
                "0.16 * group_rank(rank(-1 * composite_factor_score_derivative), subindustry) + "
                "0.14 * group_rank(ts_rank(actual_dividend_value_quarterly / open, 140), industry) + "
                "0.14 * group_rank(ts_rank(dividends_to_gross_profit, 120), sector) + "
                "0.12 * group_rank(ts_rank(fifty_to_two_hundred_day_price_ratio, 120), industry) + "
                "0.10 * group_rank(rank(-1 * correlation_last_30_days_spy), subindustry) + "
                "0.10 * rank(ts_corr(close, volume, 80)) - "
                "0.14 * ts_rank(returns, 160), sector), 8), subindustry))",
                tag=f"repair-{tag}-composite-dividend-dispersed-maxpos",
                family="repair_concentration_composite_dividend_dispersed",
                strategy="composite_dividend_group_rank_maxpos",
                parent_alpha_ids=parent,
                rationale="Disperse the high-metric composite/dividend signal with grouped component ranks, smoothing, and max-position controls.",
                simulation_settings={"truncation": 0.05, "maxPosition": "ON"},
            ),
            _candidate(
                "rank(group_neutralize("
                "0.18 * group_rank(ts_rank(actual_dividend_value_quarterly / open, 160), subindustry) + "
                "0.16 * group_rank(ts_rank(dividends_to_gross_profit, 120), industry) + "
                "0.14 * rank(-1 * ts_rank(composite_factor_score_derivative, 120)) + "
                "0.12 * rank(-1 * correlation_last_30_days_spy) + "
                "0.10 * ts_rank(rel_ret_cust, 120) + "
                "0.10 * rank(-1 * ts_rank(volume / adv20, 60)) - "
                "0.14 * ts_rank(returns, 160), sector))",
                tag=f"repair-{tag}-composite-dividend-relative-dispersed",
                family="repair_concentration_composite_dividend_relative",
                strategy="replace_wick_with_relative_volume_dividend",
                parent_alpha_ids=parent,
                rationale="Replace the concentrated wick leg with relative-return and volume dispersion while preserving the dividend/composite edge.",
                simulation_settings={"truncation": 0.05, "maxPosition": "ON"},
            ),
        ])
    if {"actual_sales_value_quarterly", "forward_sales_to_price", "pcr_oi_60"} <= source_fields:
        rows.extend([
            _candidate(
                "rank(group_neutralize("
                "0.22 * ts_rank(ts_backfill(actual_sales_value_quarterly, 150) / cap, 180) + "
                "0.20 * ts_rank(forward_sales_to_price, 180) + "
                "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.12 * ts_rank(earnings_revision_magnitude, 160) + "
                "0.10 * rank(ts_corr(vwap, volume, 120)) + "
                "0.08 * rank(-1 * ts_rank(volume / adv20, 100)) - "
                "0.14 * ts_rank(returns, 180), industry))",
                tag=f"repair-{tag}-sales-revision-broad-trunc003",
                family="repair_concentration_active_noiv_sales_revision",
                strategy="single_sales_cap_leg_broad_dispersion",
                parent_alpha_ids=parent,
                rationale="Replace the EV/PCR sparse stack with cap-normalized sales and broad price-volume dispersion.",
                simulation_settings={"truncation": 0.03},
            ),
            _candidate(
                "rank(group_rank(ts_decay_linear(group_neutralize("
                "0.18 * group_rank(ts_rank(ts_backfill(actual_sales_value_quarterly, 140) / enterprise_value, 180), subindustry) + "
                "0.17 * group_rank(ts_rank(forward_sales_to_price, 170), industry) + "
                "0.13 * group_rank(rank(-1 * earnings_certainty_rank_derivative), sector) + "
                "0.11 * group_rank(ts_rank(earnings_revision_magnitude, 150), industry) + "
                "0.11 * rank(-1 * ts_rank(pcr_oi_60, 120)) + "
                "0.08 * rank(ts_corr(close, volume, 100)) - "
                "0.13 * ts_rank(returns, 170), sector), 10), subindustry))",
                tag=f"repair-{tag}-sales-revision-dispersed-trunc003",
                family="repair_concentration_active_noiv_sales_revision",
                strategy="sales_revision_component_group_rank_low_truncation",
                parent_alpha_ids=parent,
                rationale="Disperse the no-IV sales/revision repair with slower component ranks, smoothing, and stricter truncation.",
                simulation_settings={"truncation": 0.03},
            ),
            _candidate(
                "rank(group_neutralize("
                "0.18 * group_rank(ts_rank(forward_sales_to_price, 180), subindustry) + "
                "0.16 * group_rank(ts_rank(ts_backfill(actual_sales_value_quarterly, 160) / enterprise_value, 160), industry) + "
                "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.12 * ts_rank(earnings_revision_magnitude, 160) + "
                "0.10 * rank(-1 * ts_rank(pcr_oi_60, 130)) + "
                "0.08 * rank(-1 * ts_rank(volume / adv20, 90)) - "
                "0.12 * ts_rank(returns, 180), sector))",
                tag=f"repair-{tag}-sales-revision-liquidity-dispersed",
                family="repair_concentration_active_noiv_sales_revision",
                strategy="sales_revision_liquidity_dispersed",
                parent_alpha_ids=parent,
                rationale="Add a liquidity-dispersion leg and slower sector neutralization to reduce peak weights.",
                simulation_settings={"truncation": 0.05},
            ),
        ])
    if {"cashflow_op", "forward_cash_flow_to_price", "credit_risk_premium_indicator"} <= source_fields:
        rows.extend([
            _candidate(
                "rank(group_neutralize("
                "0.22 * ts_rank(forward_cash_flow_to_price, 180) + "
                "0.15 * ts_rank(cashflow_op, 180) + "
                "0.14 * rank(-1 * credit_risk_premium_indicator) + "
                "0.12 * rank(-1 * relative_valuation_rank_derivative) + "
                "0.11 * rank(ts_corr(vwap, volume, 120)) + "
                "0.10 * rank(-1 * ts_rank(volume / adv20, 100)) - "
                "0.14 * ts_rank(returns, 180), industry))",
                tag=f"repair-{tag}-cashflow-credit-broad-trunc003",
                family="repair_concentration_active_noiv_cashflow_credit",
                strategy="single_cashflow_leg_broad_dispersion",
                parent_alpha_ids=parent,
                rationale="Keep one cashflow leg and replace the EV/PCR sparse stack with broad liquidity dispersion.",
                simulation_settings={"truncation": 0.03},
            ),
            _candidate(
                "rank(group_rank(ts_decay_linear(group_neutralize("
                "0.20 * group_rank(ts_rank(forward_cash_flow_to_price, 170), industry) + "
                "0.17 * group_rank(ts_rank(cashflow_op / enterprise_value, 150), subindustry) + "
                "0.12 * group_rank(rank(-1 * credit_risk_premium_indicator), sector) + "
                "0.10 * group_rank(rank(-1 * relative_valuation_rank_derivative), industry) + "
                "0.10 * rank(-1 * ts_rank(pcr_oi_60, 120)) + "
                "0.09 * rank(ts_corr(vwap, volume, 120)) - "
                "0.12 * ts_rank(returns, 170), sector), 10), subindustry))",
                tag=f"repair-{tag}-cashflow-credit-dispersed-trunc003",
                family="repair_concentration_active_noiv_cashflow_credit",
                strategy="cashflow_credit_component_group_rank_low_truncation",
                parent_alpha_ids=parent,
                rationale="Keep the no-IV cashflow/credit core but reduce concentration through component group ranks and strict truncation.",
                simulation_settings={"truncation": 0.03},
            ),
            _candidate(
                "rank(0.72 * rank(ts_decay_linear(group_neutralize("
                "0.20 * ts_rank(forward_cash_flow_to_price, 180) + "
                "0.16 * ts_rank(cashflow_op / enterprise_value, 160) + "
                "0.12 * rank(-1 * credit_risk_premium_indicator) + "
                "0.10 * rank(-1 * relative_valuation_rank_derivative) - "
                "0.12 * ts_rank(returns, 180), industry), 8)) + "
                "0.16 * rank(-1 * ts_rank(pcr_oi_60, 130)) + "
                "0.12 * rank(-1 * ts_rank(volume / adv20, 100)))",
                tag=f"repair-{tag}-cashflow-credit-liquidity-dispersed",
                family="repair_concentration_active_noiv_cashflow_credit",
                strategy="cashflow_credit_liquidity_dispersed",
                parent_alpha_ids=parent,
                rationale="Blend the cashflow/credit core with slower PCR and liquidity dispersion to lower peak constituent weights.",
                simulation_settings={"truncation": 0.05},
            ),
        ])
    if {"anl4_adjusted_netincome_ft", "forward_cash_flow_to_price", "credit_risk_premium_indicator"} <= source_fields:
        rows.extend([
            _candidate(
                "rank(group_neutralize("
                "0.22 * ts_rank(anl4_adjusted_netincome_ft / cap, 150) + "
                "0.20 * ts_rank(forward_cash_flow_to_price, 180) + "
                "0.14 * rank(-1 * credit_risk_premium_indicator) + "
                "0.12 * rank(-1 * relative_valuation_rank_derivative) + "
                "0.10 * rank(ts_corr(vwap, volume, 120)) + "
                "0.08 * rank(-1 * ts_rank(close / vwap, 100)) - "
                "0.14 * ts_rank(returns, 180), sector))",
                tag=f"repair-{tag}-netincome-forwardcf-broad-trunc003",
                family="repair_concentration_active_noiv_netincome_forwardcf",
                strategy="netincome_forwardcf_broad_dispersion_no_pcr",
                parent_alpha_ids=parent,
                rationale="Remove PCR from the net-income/forward-cash-flow concentration repair and disperse with price-volume legs.",
                simulation_settings={"truncation": 0.03},
            ),
            _candidate(
                "rank(group_rank(ts_decay_linear(group_neutralize("
                "0.20 * group_rank(ts_rank(anl4_adjusted_netincome_ft / cap, 130), industry) + "
                "0.18 * group_rank(ts_rank(forward_cash_flow_to_price, 170), subindustry) + "
                "0.12 * group_rank(rank(-1 * credit_risk_premium_indicator), sector) + "
                "0.10 * rank(-1 * relative_valuation_rank_derivative) + "
                "0.10 * rank(-1 * ts_rank(pcr_oi_60, 120)) + "
                "0.08 * rank(-1 * ts_rank(close / vwap, 100)) - "
                "0.12 * ts_rank(returns, 170), sector), 10), subindustry))",
                tag=f"repair-{tag}-netincome-forwardcf-dispersed-trunc003",
                family="repair_concentration_active_noiv_netincome_forwardcf",
                strategy="netincome_forwardcf_component_group_rank_low_truncation",
                parent_alpha_ids=parent,
                rationale="Disperse the no-IV net-income/forward-cash-flow repair with grouped component ranks and lower truncation.",
                simulation_settings={"truncation": 0.03},
            ),
            _candidate(
                "rank(group_neutralize("
                "0.20 * group_rank(ts_rank(forward_cash_flow_to_price, 180), industry) + "
                "0.18 * group_rank(ts_rank(anl4_adjusted_netincome_ft / cap, 140), subindustry) + "
                "0.12 * rank(-1 * credit_risk_premium_indicator) + "
                "0.10 * rank(-1 * relative_valuation_rank_derivative) + "
                "0.10 * rank(-1 * ts_rank(pcr_oi_60, 130)) + "
                "0.08 * rank(ts_corr(vwap, volume, 120)) - "
                "0.12 * ts_rank(returns, 180), sector))",
                tag=f"repair-{tag}-netincome-forwardcf-flow-dispersed",
                family="repair_concentration_active_noiv_netincome_forwardcf",
                strategy="netincome_forwardcf_flow_dispersed",
                parent_alpha_ids=parent,
                rationale="Use flow dispersion and slower windows to reduce concentrated weights while preserving the net-income signal.",
                simulation_settings={"truncation": 0.05},
            ),
        ])
    if not is_second_stage:
        rows.extend([
            _candidate(
                f"rank(ts_decay_linear(group_neutralize(0.18 * ts_rank(actual_eps_value_quarterly / vwap, 100) + "
                f"0.16 * ts_rank(earnings_momentum_composite_score, 80) + "
                f"0.16 * rank(ts_mean({iv_ratio}, 10)) + "
                f"0.14 * rank(volume / adv20) + "
                f"0.14 * rank(-1 * ts_rank(pcr_oi_10, 80)) + "
                f"0.22 * ts_rank(forward_sales_to_price, 100), industry), 5))",
                tag=f"repair-{tag}-smooth-diversified-concentration",
                family="repair_concentration_smooth_diversified",
                strategy="smooth_and_diversify_weight",
                parent_alpha_ids=parent,
                rationale="Smooth and diversify the concentrated analyst/options blend while keeping the high Sharpe core.",
            ),
            _candidate(
                f"rank(0.30 * group_rank(ts_rank(actual_eps_value_quarterly / vwap, 100), industry) + "
                f"0.22 * group_rank(ts_rank(earnings_momentum_composite_score, 80), industry) + "
                f"0.18 * rank(ts_mean({iv_ratio}, 10)) + "
                f"0.15 * ts_rank(forward_sales_to_price, 100) + "
                f"0.15 * rank(-1 * ts_rank(pcr_oi_10, 80)))",
                tag=f"repair-{tag}-group-rank-lower-peak-weight",
                family="repair_concentration_group_rank",
                strategy="replace_neutralized_sum_with_group_rank_legs",
                parent_alpha_ids=parent,
                rationale="Use group-ranked component legs to reduce peak stock weights.",
            ),
        ])
    base = _strip_outer_rank(source_expression)
    if base and _is_submit_metric_pass(source_row or {}):
        source_has_truncation = "truncation" in source_settings
        source_has_max_position = source_settings.get("maxPosition") == "ON"
        source_truncation = _safe_float(source_settings.get("truncation")) or 0.08
        low_truncation = min(source_truncation, 0.05)
        max_position_settings = {
            "truncation": low_truncation,
            "maxPosition": "ON",
        }
        if not source_has_max_position:
            rows.append(_candidate(
                source_expression.strip(),
                tag=f"repair-{tag}-retest-maxpos",
                family="repair_concentration_max_position_retest",
                strategy="max_position_low_truncation_retest",
                parent_alpha_ids=parent,
                rationale="Retest the high-metric expression with maxPosition enabled and lower truncation.",
                simulation_settings=max_position_settings,
            ))
            rows.append(_candidate(
                f"rank(group_rank(ts_decay_linear(group_neutralize({base}, subindustry), 10), subindustry))",
                tag=f"repair-{tag}-subindustry-dispersed-maxpos",
                family="repair_concentration_subindustry_dispersed",
                strategy="subindustry_dispersed_max_position",
                parent_alpha_ids=parent,
                rationale="Combine stronger subindustry dispersion with maxPosition enabled.",
                simulation_settings=max_position_settings,
            ))
        if not source_has_truncation and not source_has_max_position:
            rows.extend([
                _candidate(
                    source_expression.strip(),
                    tag=f"repair-{tag}-retest-trunc005",
                    family="repair_concentration_low_truncation_retest",
                    strategy="low_truncation_retest",
                    parent_alpha_ids=parent,
                    rationale="Retest the high-metric expression with lower truncation to reduce peak stock weights.",
                    simulation_settings={"truncation": 0.05},
                ),
                _candidate(
                    source_expression.strip(),
                    tag=f"repair-{tag}-retest-trunc003",
                    family="repair_concentration_low_truncation_retest",
                    strategy="low_truncation_retest_strict",
                    parent_alpha_ids=parent,
                    rationale="Strict lower-truncation retest for concentrated-weight near miss.",
                    simulation_settings={"truncation": 0.03},
                ),
                _candidate(
                    f"rank(group_rank(ts_decay_linear(group_neutralize({base}, subindustry), 10), subindustry))",
                    tag=f"repair-{tag}-subindustry-dispersed-trunc005",
                    family="repair_concentration_subindustry_dispersed",
                    strategy="subindustry_dispersed_low_truncation",
                    parent_alpha_ids=parent,
                    rationale="Use subindustry neutralization, group ranking, smoothing, and lower truncation to reduce peak weights.",
                    simulation_settings={"truncation": 0.05},
                ),
            ])
        elif source_has_truncation and source_truncation > 0.03:
            rows.append(_candidate(
                source_expression.strip(),
                tag=f"repair-{tag}-retest-trunc003",
                family="repair_concentration_low_truncation_retest",
                strategy="low_truncation_retest_strict",
                parent_alpha_ids=parent,
                rationale="Strict lower-truncation retest for a concentrated-weight miss whose previous truncation did not clear peak weights.",
                simulation_settings={"truncation": 0.03},
            ))
        rows.extend([
            _candidate(
                "rank(group_neutralize(0.20 * group_rank(ts_rank(actual_sales_value_quarterly / enterprise_value, 120), sector) + "
                "0.16 * group_rank(ts_rank(forward_sales_to_price, 100), industry) + "
                "0.14 * group_rank(ts_backfill(implied_volatility_call_120 - implied_volatility_put_120, 70), subindustry) + "
                "0.12 * rank(-1 * ts_rank(pcr_oi_60, 100)) + "
                "0.10 * group_rank(ts_rank(rel_ret_cust, 120), industry) + "
                "0.10 * ts_rank(-ts_delta(vwap, 12) / vwap, 50) + "
                "0.08 * rank(-1 * beta_last_30_days_spy) - "
                "0.12 * ts_rank(returns, 110), sector))",
                tag=f"repair-{tag}-orthogonal-sector-dispersed",
                family="repair_concentration_orthogonal_sector",
                strategy="orthogonal_sector_dispersed_rebuild",
                parent_alpha_ids=parent,
                rationale="Rebuild the concentrated value/options blend with sector neutralization and broader orthogonal legs.",
                simulation_settings={"truncation": 0.05, "maxPosition": "ON"},
            ),
            _candidate(
                "rank(group_neutralize(0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.14 * ts_rank(dividends_to_gross_profit, 90) + "
                "0.14 * group_rank(ts_rank(rel_ret_cust, 120), sector) + "
                "0.12 * group_rank(ts_rank(forward_sales_to_price, 100), industry) + "
                "0.10 * rank(ts_corr(close, volume, 60)) + "
                "0.10 * ts_rank(-ts_delta(vwap, 15) / vwap, 60) + "
                "0.08 * rank(-1 * correlation_last_30_days_spy) - "
                "0.12 * ts_rank(returns, 120), industry))",
                tag=f"repair-{tag}-quality-relative-dispersed",
                family="repair_concentration_quality_relative",
                strategy="quality_relative_dispersed_rebuild",
                parent_alpha_ids=parent,
                rationale="Use quality, relative-return, forward-value, and beta-correlation legs to reduce peak constituent weights.",
                simulation_settings={"truncation": 0.05, "maxPosition": "ON"},
            ),
        ])
    return rows


def _metric_threshold_repairs(
    fields: set[str],
    tag: str,
    parent: list[Any],
    *,
    source_expression: str,
    source_row: dict | None = None,
) -> list[dict]:
    base = _strip_outer_rank(source_expression)
    source_row = source_row or {}
    rows: list[dict] = []
    if {"anl4_adjusted_netincome_ft", "cap"} <= fields:
        rows.extend([
            _candidate(
                "rank(0.42 * ts_rank(anl4_adjusted_netincome_ft / cap, 90) + "
                "0.24 * ts_rank(forward_cash_flow_to_price, 140) + "
                "0.16 * rank(ts_corr(vwap, volume, 100)) - "
                "0.18 * ts_rank(returns, 90))",
                tag=f"repair-{tag}-netincome-forwardcf-flow-rebuild",
                family="repair_metric_netincome_value_rebuild",
                strategy="metric_near_miss_rebuild_with_forward_cashflow_flow",
                parent_alpha_ids=parent,
                rationale="Lift Fitness by rebuilding the net-income/cap signal with forward cashflow and price-volume dispersion.",
            ),
            _candidate(
                "rank(0.40 * ts_rank(anl4_adjusted_netincome_ft / cap, 100) + "
                "0.22 * ts_rank(forward_book_value_to_price, 140) + "
                "0.16 * rank(-1 * relative_valuation_rank_derivative) + "
                "0.12 * rank(ts_corr(close, volume, 120)) - "
                "0.16 * ts_rank(returns, 100))",
                tag=f"repair-{tag}-netincome-forwardbook-value-rebuild",
                family="repair_metric_netincome_value_rebuild",
                strategy="metric_near_miss_rebuild_with_forward_book_value",
                parent_alpha_ids=parent,
                rationale="Replace weak short-window overlays with forward book value, valuation derivative, and price-volume dispersion.",
            ),
            _candidate(
                "rank(group_neutralize(0.38 * ts_rank(anl4_adjusted_netincome_ft / cap, 100) + "
                "0.22 * ts_rank(forward_sales_to_price, 150) + "
                "0.16 * ts_rank(coefficient_variation_fy1_eps, 120) + "
                "0.10 * rank(ts_corr(vwap, volume, 120)) - "
                "0.16 * ts_rank(returns, 120), industry))",
                tag=f"repair-{tag}-netincome-forward-sales-industry-rebuild",
                family="repair_metric_netincome_value_rebuild",
                strategy="metric_near_miss_industry_forward_value_rebuild",
                parent_alpha_ids=parent,
                rationale="Use slower forward-value and dispersion legs instead of same-expression settings retests.",
                simulation_settings={"truncation": 0.05},
            ),
        ])
    if {
        "actual_sales_value_quarterly",
        "cap",
        "forward_sales_to_price",
        "earnings_certainty_rank_derivative",
        "earnings_revision_magnitude",
        "vwap",
        "volume",
    } <= fields:
        rows.extend([
            _candidate(
                "rank(0.30 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / cap, 150) + "
                "0.24 * ts_rank(forward_sales_to_price, 150) + "
                "0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.14 * ts_rank(earnings_revision_magnitude, 130) + "
                "0.10 * rank(ts_corr(vwap, volume, 90)) - "
                "0.10 * ts_rank(returns, 130))",
                tag=f"repair-{tag}-sales-cap-revision-core-lift",
                family="repair_metric_sales_cap_revision_tune",
                strategy="metric_near_miss_sales_cap_revision_core_lift",
                parent_alpha_ids=parent,
                rationale="Lift the sales/cap revision near-miss by strengthening the high-coverage sales and forward-value legs without EV/PCR.",
            ),
            _candidate(
                "rank(group_neutralize(0.30 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / cap, 150) + "
                "0.24 * ts_rank(forward_sales_to_price, 150) + "
                "0.16 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.14 * ts_rank(earnings_revision_magnitude, 130) + "
                "0.12 * rank(ts_corr(vwap, volume, 80)) - "
                "0.08 * ts_rank(returns, 120), industry))",
                tag=f"repair-{tag}-sales-cap-revision-industry-lift",
                family="repair_metric_sales_cap_revision_tune",
                strategy="metric_near_miss_sales_cap_revision_industry_lift",
                parent_alpha_ids=parent,
                rationale="Keep industry neutralization but reduce the returns drag and strengthen price-volume breadth.",
            ),
            _candidate(
                "rank(0.28 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / cap, 150) + "
                "0.22 * ts_rank(earnings_momentum_composite_score, 80) + "
                "0.20 * ts_rank(forward_sales_to_price, 150) + "
                "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.10 * rank(ts_corr(vwap, volume, 90)) - "
                "0.10 * ts_rank(returns, 130))",
                tag=f"repair-{tag}-sales-cap-earnmom-lite",
                family="repair_metric_sales_cap_revision_tune",
                strategy="metric_near_miss_sales_cap_revision_light_earnmom",
                parent_alpha_ids=parent,
                rationale="Recover some original sales/earnings-momentum strength while keeping cap normalization and avoiding EV/PCR.",
            ),
            _candidate(
                "rank(0.26 * ts_rank(ts_backfill(actual_sales_value_quarterly, 100) / cap, 130) + "
                "0.22 * ts_rank(forward_sales_to_price, 130) + "
                "0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.12 * ts_rank(earnings_revision_magnitude, 110) + "
                "0.12 * rank(volume / adv20) - "
                "0.10 * ts_rank(returns, 110))",
                tag=f"repair-{tag}-sales-cap-revision-liquidity-lift",
                family="repair_metric_sales_cap_revision_tune",
                strategy="metric_near_miss_sales_cap_revision_liquidity_lift",
                parent_alpha_ids=parent,
                rationale="Use volume breadth instead of extra group operations to lift fitness while preserving high coverage.",
            ),
        ])
    if source_expression:
        rows.extend([
            _candidate(
                source_expression.strip(),
                tag=f"repair-{tag}-metric-retest-decay12-trunc005",
                family="repair_metric_threshold_settings",
                strategy="metric_near_miss_decay_truncation_retest",
                parent_alpha_ids=parent,
                rationale="Retest the near-threshold expression with slower decay and lower truncation.",
                simulation_settings={"decay": 12, "truncation": 0.05},
            ),
            _candidate(
                source_expression.strip(),
                tag=f"repair-{tag}-metric-retest-maxpos-trunc005",
                family="repair_metric_threshold_settings",
                strategy="metric_near_miss_max_position_retest",
                parent_alpha_ids=parent,
                rationale="Retest the near-threshold expression with maxPosition enabled to reduce peak risk.",
                simulation_settings={"maxPosition": "ON", "truncation": 0.05},
            ),
        ])
    if base:
        rows.append(_candidate(
            f"rank(ts_decay_linear(group_neutralize({base}, industry), 5))",
            tag=f"repair-{tag}-metric-smooth-industry",
            family="repair_metric_threshold_smoothing",
            strategy="metric_near_miss_smooth_group_neutralize",
            parent_alpha_ids=parent,
            rationale="Smooth and industry-neutralize the near-threshold expression to improve fitness stability.",
            simulation_settings={"truncation": 0.05},
        ))
    if {
        "actual_sales_value_quarterly",
        "enterprise_value",
        "earnings_momentum_composite_score",
        "vwap",
        "volume",
    } <= fields:
        rows.append(_candidate(
            "rank(0.46 * ts_rank(actual_sales_value_quarterly / enterprise_value, 80) + "
            "0.30 * ts_rank(earnings_momentum_composite_score, 70) + "
            "0.14 * rank(ts_corr(vwap, volume, 60)) - "
            "0.10 * ts_rank(returns, 60))",
            tag=f"repair-{tag}-sales-earnmom-slower-ret60",
            family="repair_metric_sales_momentum_tune",
            strategy="metric_near_miss_slow_turnover_tune",
            parent_alpha_ids=parent,
            rationale="Use slower windows and a smaller returns drag to lift fitness without changing the core payload.",
        ))
    return rows


def _generic_repairs(expression: str, tag: str, parent: list[Any], failure: str) -> list[dict]:
    base = _strip_outer_rank(expression)
    if failure == "concentrated_weight":
        return [_candidate(
            f"rank(ts_decay_linear(group_neutralize({base}, industry), 5))",
            tag=f"repair-{tag}-smooth-industry",
            family="repair_concentration_generic",
            strategy="smooth_group_neutralize",
            parent_alpha_ids=parent,
            rationale="Generic concentration repair: smooth and group neutralize the original signal.",
        )]
    return [_candidate(
        "rank(0.42 * ts_rank(forward_sales_to_price, 120) + 0.24 * ts_rank(snt1_d1_netearningsrevision, 90) + 0.18 * rank(ts_corr(vwap, volume, 100)) + 0.16 * rank(-1 * ts_rank(pcr_oi_60, 90)))",
        tag=f"repair-{tag}-generic-forward-revision-pcr",
        family="repair_self_corr_generic_orthogonal",
        strategy="field_family_replacement",
        parent_alpha_ids=parent,
        rationale="Generic self-correlation repair using forward value, revision, price-volume dispersion, and low-weight option flow.",
    )]


def _candidate(
    expression: str,
    *,
    tag: str,
    family: str,
    strategy: str,
    parent_alpha_ids: list[Any],
    rationale: str,
    simulation_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fields = sorted(extract_components(expression).get("fields", []))
    row = {
        "expression": expression,
        "tag": tag,
        "source_family": family,
        "mutation_strategy": strategy,
        "rationale": rationale,
        "expected_low_corr_reason": "Deterministic repair changed field/operator family after presubmit failure.",
        "parent_alpha_ids": [value for value in parent_alpha_ids if value],
        "source_fields": fields,
        "risk_flags": ["repair_candidate", strategy],
        "repair_priority_score": _expression_priority(expression),
    }
    settings = _clean_simulation_settings(simulation_settings)
    if settings:
        row["simulation_settings"] = settings
        row["settings_hint"] = ", ".join(f"{key}={value}" for key, value in sorted(settings.items()))
        row["risk_flags"].append("settings_variant")
        row["repair_priority_score"] = round(row["repair_priority_score"] + 2.0, 4)
    return row


def _is_repairable_review_row(row: dict) -> bool:
    bucket = str(row.get("triage_bucket") or "")
    if bucket not in {"near_miss_repair", "hard_fail"}:
        return False
    failure = _failure_kind(row)
    if failure == "concentrated_weight":
        return _is_concentration_metric_repairable(row)
    if failure == "metric_threshold_near_miss":
        return _is_metric_threshold_repairable(row)
    if failure == "self_correlation_fail":
        sharpe = _safe_float(row.get("sharpe")) or 0.0
        fitness = _safe_float(row.get("fitness")) or 0.0
        sc_value = _safe_float(row.get("sc_value")) or 1.0
        return sharpe >= 1.25 and fitness >= 1.0 and sc_value <= 0.93
    return False


def _is_submit_metric_pass(row: dict) -> bool:
    sharpe = _safe_float(row.get("sharpe")) or 0.0
    fitness = _safe_float(row.get("fitness")) or 0.0
    turnover = _safe_float(row.get("turnover"))
    turnover_ok = turnover is None or 0.01 <= turnover <= 0.7
    return sharpe >= 1.25 and fitness >= 1.0 and turnover_ok


def _is_concentration_metric_repairable(row: dict) -> bool:
    sharpe = _safe_float(row.get("sharpe")) or 0.0
    fitness = _safe_float(row.get("fitness")) or 0.0
    turnover = _safe_float(row.get("turnover"))
    turnover_ok = turnover is None or 0.01 <= turnover <= 0.7
    return turnover_ok and sharpe >= 1.25 and fitness >= 0.85


def _is_metric_threshold_repairable(row: dict) -> bool:
    sharpe = _safe_float(row.get("sharpe")) or 0.0
    fitness = _safe_float(row.get("fitness")) or 0.0
    turnover = _safe_float(row.get("turnover"))
    turnover_ok = turnover is None or 0.01 <= turnover <= 0.7
    return turnover_ok and sharpe >= 1.15 and fitness >= 0.85


def _clean_simulation_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(settings, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("region", "universe", "neutralization"):
        value = settings.get(key)
        if value not in (None, ""):
            out[key] = str(value)
    for out_key, *input_keys in (
        ("maxTrade", "maxTrade", "max_trade"),
        ("maxPosition", "maxPosition", "max_position"),
    ):
        value = next((settings.get(key) for key in input_keys if settings.get(key) not in (None, "")), None)
        if value not in (None, ""):
            text = str(value).upper()
            if text in {"ON", "OFF"}:
                out[out_key] = text
    for key in ("delay", "decay"):
        value = settings.get(key)
        if value in (None, ""):
            continue
        try:
            out[key] = int(value)
        except (TypeError, ValueError):
            continue
    if settings.get("truncation") not in (None, ""):
        try:
            truncation = float(settings["truncation"])
        except (TypeError, ValueError):
            truncation = None
        if truncation is not None and 0 < truncation <= 0.2:
            out["truncation"] = truncation
    return out


def _source_simulation_settings(row: dict) -> dict[str, Any]:
    for value in (
        row.get("actual_simulation_settings"),
        (row.get("result") or {}).get("settings") if isinstance(row.get("result"), dict) else None,
        row.get("simulation_settings"),
        row.get("effective_simulation_settings"),
    ):
        settings = _clean_simulation_settings(value)
        if settings:
            return settings
    return {}


def _candidate_dedupe_key(row: dict) -> str:
    expression = normalize_expression(str(row.get("expression") or ""))
    settings = _clean_simulation_settings(row.get("simulation_settings") or row.get("settings_override"))
    if not settings:
        return expression
    return f"{expression}||settings={json.dumps(settings, sort_keys=True, separators=(',', ':'))}"


def _failure_kind(row: dict) -> str:
    failed_names = {str(check.get("name") or "").upper() for check in row.get("failed_platform_checks") or []}
    for check in row.get("failed_platform_checks") or []:
        if str(check.get("name") or "").upper() == "CONCENTRATED_WEIGHT":
            return "concentrated_weight"
    if failed_names <= {"LOW_SHARPE", "LOW_FITNESS"} and failed_names:
        return "metric_threshold_near_miss"
    reason = str(row.get("triage_reason") or row.get("api_check_status") or "").lower()
    if "self-correlation" in reason or "self_correlation" in reason:
        return "self_correlation_fail"
    if "platform check" in reason:
        return "platform_check_fail"
    return str(row.get("failure_kind") or "unknown")


def _repair_objective(row: dict) -> str:
    failure = _failure_kind(row)
    if failure == "concentrated_weight":
        return "Reduce concentrated weights by smoothing, diversifying legs, and group-ranking components."
    if failure == "self_correlation_fail":
        return "Reduce self-correlation by replacing crowded field families rather than only changing windows."
    if failure == "metric_threshold_near_miss":
        return "Lift near-threshold Sharpe/Fitness with slower windows, smoother transforms, and settings retests."
    return "Repair presubmit failure with deterministic field-family changes."


def _risk_notes(row: dict) -> list[str]:
    notes = []
    failure = _failure_kind(row)
    if failure == "self_correlation_fail":
        notes.append("Do not reuse IV90/returns/price-volume as the only overlay.")
    if failure == "concentrated_weight":
        notes.append("Consider lower truncation, e.g. 0.05, when running this repair batch.")
    if failure == "metric_threshold_near_miss":
        notes.append("Prefer slow-window and low-truncation retests before changing the core field family.")
    return notes


def _fields(row: dict) -> set[str]:
    values = row.get("source_fields")
    if isinstance(values, list):
        return {str(value) for value in values if value}
    expression = str(row.get("expression") or row.get("source_expression") or "")
    return set(extract_components(expression).get("fields", [])) if expression else set()


def _dedupe_candidates(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for row in rows:
        key = _candidate_dedupe_key(row)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _locally_valid(expression: str) -> bool:
    try:
        validate_wq_expression(expression)
        return True
    except Exception:
        return False


def _candidate_sort_key(row: dict) -> tuple:
    score = _safe_float(row.get("repair_priority_score")) or 0.0
    family = str(row.get("source_family") or "")
    fields = set(row.get("source_fields") or [])
    if family.startswith("repair_concentration_active_noiv"):
        score += 8.0
    if family in {
        "repair_self_corr_equity_sales_eps_rebuild",
        "repair_self_corr_sales_earnmom_rebuild",
    }:
        score += 1.0
    if family in {
        "repair_self_corr_cashflow_book_revision",
        "repair_self_corr_minimal_orthogonal",
    }:
        score -= 2.0
    if family == "repair_metric_sales_cap_revision_tune":
        score -= 5.0
    if family == "repair_metric_netincome_value_rebuild":
        score += 6.0
    if family in {"repair_metric_threshold_settings", "repair_metric_threshold_smoothing"}:
        score -= 8.0
    if family == "repair_metric_sales_momentum_tune":
        score -= 6.0
    if str(row.get("repair_failure_kind") or "") == "concentrated_weight" and (
        fields & {
            "implied_volatility_call_90",
            "implied_volatility_put_90",
            "implied_volatility_call_120",
            "implied_volatility_put_120",
        }
    ):
        score -= 10.0
    if family in {"repair_concentration_max_position_retest", "repair_concentration_low_truncation_retest"}:
        score -= 3.0
    return (
        -score,
        str(row.get("repair_failure_kind") or ""),
        str(row.get("tag") or ""),
    )


def _expression_priority(expression: str) -> float:
    fields = extract_components(expression).get("fields", [])
    crowded_penalty = sum(1 for field in fields if field in {"returns", "close", "volume", "vwap"})
    rare_bonus = sum(1 for field in fields if field in {
        "forward_sales_to_price",
        "forward_book_value_to_price",
        "coefficient_variation_fy1_eps",
        "pcr_oi_60",
        "pcr_vol_10",
        "snt1_d1_netearningsrevision",
        "actual_cashflow_per_share_value_quarterly",
    })
    return round(50.0 + rare_bonus * 4.0 - crowded_penalty * 2.5, 4)


def _strip_outer_rank(expression: str) -> str:
    text = expression.strip()
    if text.startswith("rank(") and text.endswith(")"):
        return text[5:-1].strip()
    return text


def _load_rows(paths: tuple[Path, ...]) -> list[dict]:
    rows = []
    for path in paths:
        if not path or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() == ".json":
            payload = json.loads(text)
            if isinstance(payload, list):
                rows.extend(row for row in payload if isinstance(row, dict))
            elif isinstance(payload, dict):
                for key in ("rows", "records", "review", "ready"):
                    value = payload.get(key)
                    if isinstance(value, list):
                        rows.extend(row for row in value if isinstance(row, dict))
                if not rows:
                    rows.append(payload)
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _summary(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: plan[key] for key in ("ok", "generated_at", "summary", "files") if key in plan}


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")
