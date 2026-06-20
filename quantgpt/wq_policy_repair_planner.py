"""Policy-aware deterministic repair planning for WQ presubmit misses.

The planner consumes reviewed presubmit rows and emits local-only repair
candidates. It is intentionally conservative: self-correlation repairs change
field families, while concentration repairs smooth and diversify weights.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .expression_parser import extract_components, normalize_expression
from .wq_auto_mining import validate_wq_expression
from .wq_forum_submission_optimizer import annotate_candidate_with_policy, load_submission_policy


@dataclass(frozen=True)
class PolicyRepairPlannerConfig:
    review_paths: tuple[Path, ...] = field(default_factory=tuple)
    output_dir: Path | None = None
    submission_policy_file: Path | None = None
    obsidian_output: Path | None = None
    max_candidates: int = 40
    max_repairs_per_row: int = 4
    title: str = "QuantGPT presubmit repair plan"


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
            "candidate_records": [
                annotate_candidate_with_policy(item, submission_policy) if submission_policy else item
                for item in candidates
            ],
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
            if key in seen:
                continue
            seen.add(key)
            if not _locally_valid(expression):
                continue
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
        "  - quantgpt",
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
        "- Concentrated-weight repairs combine smoothing/diversification with lower-truncation retests when metrics already pass.",
        "- Forum-direct templates remain blocked by submission policy unless an orthogonal overlay is present.",
    ])
    return "\n".join(lines).rstrip() + "\n"


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
    if not out and expression:
        out.extend(_generic_repairs(expression, base_tag, parent, failure))
    return out


def _self_corr_repairs(fields: set[str], tag: str, parent: list[Any]) -> list[dict]:
    out: list[dict] = []
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
                "rank(0.34 * group_rank(ts_rank(actual_eps_value_quarterly / enterprise_value, 120), subindustry) + "
                "0.24 * ts_rank(coefficient_variation_fy1_eps, 80) + "
                "0.22 * rank(-1 * ts_rank(pcr_oi_60, 60)) + "
                "0.20 * ts_rank(forward_sales_to_price, 100))",
                tag=f"repair-{tag}-eps-ev-pcr60-forward-sales",
                family="repair_self_corr_eps_forward_options_flow",
                strategy="replace_iv90_iv120_micro_with_forward_pcr",
                parent_alpha_ids=parent,
                rationale="Replace the crowded IV90/IV120 and price microstructure legs with forward value and PCR flow.",
            ),
            _candidate(
                "rank(0.40 * ts_rank(actual_cashflow_per_share_value_quarterly / enterprise_value, 100) + "
                "0.25 * ts_rank(forward_book_value_to_price, 100) + "
                "0.20 * ts_rank(snt1_d1_netearningsrevision, 80) + "
                "0.15 * rank(-1 * ts_rank(pcr_vol_10, 60)))",
                tag=f"repair-{tag}-cashflow-forward-revision-pcrvol",
                family="repair_self_corr_cashflow_revision_flow",
                strategy="field_family_replacement",
                parent_alpha_ids=parent,
                rationale="Move the idea into cashflow, forward value, revision, and option-flow families.",
            ),
        ])
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
    out.append(_candidate(
        "rank(0.45 * ts_rank(forward_sales_to_price, 100) + "
        "0.30 * ts_rank(coefficient_variation_fy1_eps, 80) + "
        "0.25 * rank(-1 * ts_rank(pcr_oi_60, 60)))",
        tag=f"repair-{tag}-minimal-forward-dispersion-pcr",
        family="repair_self_corr_minimal_orthogonal",
        strategy="minimal_orthogonal_rebuild",
        parent_alpha_ids=parent,
        rationale="Minimal rebuild using low-active forward sales, EPS dispersion, and PCR fields.",
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
        max_position_settings = {
            "truncation": source_settings.get("truncation", 0.05),
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
        "rank(0.55 * ts_rank(forward_sales_to_price, 100) + 0.25 * ts_rank(snt1_d1_netearningsrevision, 80) + 0.20 * rank(-1 * ts_rank(pcr_oi_60, 60)))",
        tag=f"repair-{tag}-generic-forward-revision-pcr",
        family="repair_self_corr_generic_orthogonal",
        strategy="field_family_replacement",
        parent_alpha_ids=parent,
        rationale="Generic self-correlation repair using forward value, revision, and option-flow families.",
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
    if failure == "self_correlation_fail":
        sharpe = _safe_float(row.get("sharpe")) or 0.0
        fitness = _safe_float(row.get("fitness")) or 0.0
        sc_value = _safe_float(row.get("sc_value")) or 1.0
        return sharpe >= 1.25 and fitness >= 1.0 and sc_value <= 0.86
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
        row.get("simulation_settings"),
        row.get("effective_simulation_settings"),
        (row.get("result") or {}).get("settings") if isinstance(row.get("result"), dict) else None,
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
    for check in row.get("failed_platform_checks") or []:
        if str(check.get("name") or "").upper() == "CONCENTRATED_WEIGHT":
            return "concentrated_weight"
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
    return "Repair presubmit failure with deterministic field-family changes."


def _risk_notes(row: dict) -> list[str]:
    notes = []
    failure = _failure_kind(row)
    if failure == "self_correlation_fail":
        notes.append("Do not reuse IV90/returns/price-volume as the only overlay.")
    if failure == "concentrated_weight":
        notes.append("Consider lower truncation, e.g. 0.05, when running this repair batch.")
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
        expression = str(row.get("expression") or "")
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
    return (
        -(_safe_float(row.get("repair_priority_score")) or 0.0),
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
