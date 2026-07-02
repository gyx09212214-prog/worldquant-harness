"""Build submission-level WQ experience from local run artifacts."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_io import read_jsonl as _read_jsonl
from .artifact_io import utc_now as _now
from .artifact_io import write_json, write_text
from .artifact_io import write_jsonl as _write_jsonl
from .expression_parser import normalize_expression
from .record_utils import dedupe_rows_by_key as _dedupe_rows_by_key
from .record_utils import nested as _nested
from .record_utils import safe_float as _safe_float
from .source_utils import source_run_id_from_cycle_path as _source_run_id
from .wq_expression_utils import expression_components as _components
from .wq_failure_taxonomy import LOW_COVERAGE_PREFIXES, failed_check_names, failure_kind_from_check_names
from .wq_history_experience import canonical_failure_kind

SCHEMA_VERSION = 1

SUBMISSION_ARTIFACT_NAMES = {
    "simulation_results.jsonl",
    "review_queue.jsonl",
    "submit_results.jsonl",
    "submit_existing_results.jsonl",
    "submitted_accumulator.jsonl",
    "presubmit_ready_sequential.jsonl",
    "presubmit_rejected.jsonl",
}

SUCCESS_STATUSES = {"ACTIVE", "SUBMITTED"}
EVENT_FIELDS = {
    "news_open_gap",
    "news_max_dn_ret",
    "news_mins_10_chg",
    "news_mov_vol",
    "scl12_buzz_fast_d1",
    "scl12_sentiment_fast_d1",
}
PRICE_VOLUME_FIELDS = {"adv20", "close", "high", "low", "open", "volume", "vwap"}
GROUP_FIELDS = {"industry", "sector", "subindustry", "market"}


@dataclass(frozen=True)
class WQSubmissionExperienceConfig:
    reports_dir: Path
    output_dir: Path
    local_file_limit: int = 0
    record_limit: int = 0
    min_field_evidence: int = 3
    near_pass_sharpe: float = 0.60
    near_pass_fitness: float = 0.80


def build_submission_experience(config: WQSubmissionExperienceConfig) -> dict[str, Any]:
    """Collect local submission artifacts and render rule/memory outputs."""

    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = _artifact_paths(Path(config.reports_dir), limit=config.local_file_limit)
    records = _load_records(artifacts, limit=config.record_limit)
    summary = _summarize_records(records, config=config)
    memory = _build_memory_rows(records)
    rules = _build_rules(summary, records, config=config)

    files = {
        "records": str(output_dir / "submission_experience_records.jsonl"),
        "memory": str(output_dir / "submission_experience_memory.jsonl"),
        "rules": str(output_dir / "experience_rules.json"),
        "summary": str(output_dir / "summary.json"),
        "markdown": str(output_dir / "submission_experience.md"),
    }
    _write_jsonl(Path(files["records"]), records)
    _write_jsonl(Path(files["memory"]), memory)
    write_json(Path(files["rules"]), rules)

    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "created_at": _now(),
        "mode": "wq_submission_experience",
        "reports_dir": str(Path(config.reports_dir)),
        "output_dir": str(output_dir),
        "artifact_count": len(artifacts),
        "record_count": len(records),
        "memory_count": len(memory),
        "rule_count": _rule_count(rules),
        "summary": summary,
        "files": files,
    }
    write_json(Path(files["summary"]), result)
    write_text(Path(files["markdown"]), render_submission_experience_markdown(result, rules))
    return result


def render_submission_experience_markdown(summary: dict[str, Any], rules: dict[str, Any]) -> str:
    """Render a human review report from the generated experience summary."""

    core = summary.get("summary") or {}
    lines = [
        "# WQ Submission Experience",
        "",
        f"- Records: {summary.get('record_count')}",
        f"- Memory rows: {summary.get('memory_count')}",
        f"- Rules: {summary.get('rule_count')}",
        f"- Artifacts scanned: {summary.get('artifact_count')}",
        "",
        "## Outcomes",
        "",
    ]
    for key, value in (core.get("outcome_counts") or {}).items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Failure Kinds", ""])
    for key, value in (core.get("failure_kind_counts") or {}).items():
        lines.append(f"- {key}: {value}")

    lines.extend(["", "## Top Field Experience", ""])
    field_rows = (core.get("field_stats") or [])[:12]
    if field_rows:
        for row in field_rows:
            lines.append(
                f"- {row['field']}: n={row['count']}, success={row['success_count']}, "
                f"near={row['near_pass_count']}, failures={row['failure_kinds']}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Near Passes", ""])
    near_passes = core.get("near_passes") or []
    if near_passes:
        for row in near_passes[:10]:
            lines.append(
                f"- `{row['tag']}` {row['alpha_id'] or ''}: sharpe={row['sharpe']} "
                f"fitness={row['fitness']} turnover={row['turnover']} failures={','.join(row['failed_checks'])}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Generated Rules", ""])
    for row in rules.get("field_rules") or []:
        lines.append(f"- `{row['field']}`: {row['action']} - {row['reason']}")
    for row in rules.get("structure_rules") or []:
        lines.append(f"- `{row['rule_id']}`: {row['action']} - {row['reason']}")
    for row in rules.get("repair_rules") or []:
        lines.append(f"- `{row['rule_id']}`: {row['action']} - {row['reason']}")
    return "\n".join(lines).rstrip() + "\n"


def _artifact_paths(reports_dir: Path, *, limit: int) -> list[Path]:
    paths = [
        path
        for path in sorted(reports_dir.rglob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
        if path.name in SUBMISSION_ARTIFACT_NAMES
    ]
    if limit > 0:
        return paths[:limit]
    return paths


def _load_records(paths: list[Path], *, limit: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        source_type = path.name.removesuffix(".jsonl")
        for row_index, row in enumerate(_read_jsonl(path)):
            record = _normalize_record(row, source_file=path, source_type=source_type, row_index=row_index)
            if record:
                records.append(record)
                if limit > 0 and len(records) >= limit:
                    return records
    return _dedupe_records(records)


def _normalize_record(
    row: dict[str, Any],
    *,
    source_file: Path,
    source_type: str,
    row_index: int,
) -> dict[str, Any] | None:
    expression = str(row.get("expression") or "").strip()
    if not expression:
        return None
    components = _components(expression)
    failed_checks = _failed_check_names(row)
    sc_result = _check_result(row, "SELF_CORRELATION")
    prod_result = _check_result(row, "PROD_CORRELATION")
    failure_kind = canonical_failure_kind(row, api_status=str(row.get("api_check_status") or ""), platform_status=str(row.get("platform_status") or row.get("status") or ""), sc_result=sc_result, prod_result=prod_result, sc_value=_safe_float(row.get("sc_value")))
    if not failure_kind and failed_checks:
        failure_kind = _failure_from_checks(failed_checks)
    outcome = _record_outcome(row, failure_kind=failure_kind, source_type=source_type)
    sharpe = _safe_float(row.get("sharpe") or _nested(row, ("result", "is_metrics", "sharpe")))
    fitness = _safe_float(row.get("fitness") or _nested(row, ("result", "is_metrics", "fitness")))
    turnover = _safe_float(row.get("turnover") or _nested(row, ("result", "is_metrics", "turnover")))
    returns = _safe_float(row.get("returns") or _nested(row, ("result", "is_metrics", "returns")))
    return {
        "schema_version": SCHEMA_VERSION,
        "record_id": _record_id(source_file, row_index, expression),
        "source_file": str(source_file),
        "source_type": source_type,
        "source_run_id": _source_run_id(source_file),
        "row_index": row_index,
        "outcome": outcome,
        "failure_kind": failure_kind or "none",
        "alpha_id": row.get("alpha_id"),
        "tag": row.get("tag"),
        "source_family": row.get("source_family") or _nested(row, ("candidate_meta", "source_family")),
        "mutation_strategy": row.get("mutation_strategy"),
        "expression": expression,
        "expression_normalized": _safe_normalize(expression),
        "expression_hash": _hash(expression),
        "field_signature": "|".join(sorted(components["fields"])),
        "fields": sorted(components["fields"]),
        "operators": sorted(components["operators"]),
        "sharpe": sharpe,
        "fitness": fitness,
        "returns": returns,
        "turnover": turnover,
        "submit_eligible": bool(row.get("submit_eligible")),
        "submitted": bool(row.get("submitted")),
        "platform_status": str(row.get("platform_status") or row.get("status") or ""),
        "failed_checks": failed_checks,
        "is_near_pass": _is_near_pass(sharpe, fitness, turnover),
        "created_at": _now(),
    }


def _summarize_records(records: list[dict[str, Any]], *, config: WQSubmissionExperienceConfig) -> dict[str, Any]:
    outcome_counts = Counter(str(row.get("outcome") or "unknown") for row in records)
    failure_counts = Counter(str(row.get("failure_kind") or "none") for row in records)
    field_stats = _field_stats(records)
    family_stats = _family_stats(records)
    pattern_stats = _pattern_stats(records)
    near_passes = _unique_near_passes([
        _compact_record(row) for row in sorted(
            records,
            key=lambda item: (
                -(_safe_float(item.get("fitness")) or 0.0),
                -(_safe_float(item.get("sharpe")) or 0.0),
            ),
        )
        if row.get("is_near_pass") and row.get("outcome") != "success"
    ])
    return {
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "failure_kind_counts": dict(sorted(failure_counts.items())),
        "field_stats": field_stats,
        "family_stats": family_stats,
        "pattern_stats": pattern_stats[:30],
        "near_passes": near_passes[:30],
        "config": {
            "min_field_evidence": config.min_field_evidence,
            "near_pass_sharpe": config.near_pass_sharpe,
            "near_pass_fitness": config.near_pass_fitness,
        },
    }


def _field_stats(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        for field in row.get("fields") or []:
            if field in GROUP_FIELDS:
                continue
            buckets[str(field)].append(row)
    stats = []
    for field, rows in buckets.items():
        failures = Counter(str(row.get("failure_kind") or "none") for row in rows if row.get("outcome") != "success")
        checks = Counter(check for row in rows for check in row.get("failed_checks") or [])
        stats.append({
            "field": field,
            "count": len(rows),
            "success_count": sum(1 for row in rows if row.get("outcome") == "success"),
            "near_pass_count": sum(1 for row in rows if row.get("is_near_pass")),
            "avg_sharpe": _mean(row.get("sharpe") for row in rows),
            "avg_fitness": _mean(row.get("fitness") for row in rows),
            "avg_turnover": _mean(row.get("turnover") for row in rows),
            "failure_kinds": dict(sorted(failures.items())),
            "failed_checks": dict(sorted(checks.items())),
        })
    return sorted(stats, key=lambda row: (-row["count"], -row["near_pass_count"], row["field"]))


def _family_stats(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        buckets[str(row.get("source_family") or row.get("mutation_strategy") or "unknown")].append(row)
    return sorted([
        {
            "family": family,
            "count": len(rows),
            "success_count": sum(1 for row in rows if row.get("outcome") == "success"),
            "near_pass_count": sum(1 for row in rows if row.get("is_near_pass")),
            "failure_kinds": dict(sorted(Counter(str(row.get("failure_kind") or "none") for row in rows).items())),
            "avg_fitness": _mean(row.get("fitness") for row in rows),
        }
        for family, rows in buckets.items()
    ], key=lambda row: (-row["count"], -row["near_pass_count"], row["family"]))


def _pattern_stats(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        key = f"fields={row.get('field_signature') or ''}|ops={','.join(row.get('operators') or [])}"
        buckets[key].append(row)
    return sorted([
        {
            "pattern": pattern,
            "count": len(rows),
            "success_count": sum(1 for row in rows if row.get("outcome") == "success"),
            "near_pass_count": sum(1 for row in rows if row.get("is_near_pass")),
            "failure_kinds": dict(sorted(Counter(str(row.get("failure_kind") or "none") for row in rows).items())),
            "example": rows[0].get("expression"),
        }
        for pattern, rows in buckets.items()
    ], key=lambda row: (-row["count"], -row["near_pass_count"], row["pattern"]))


def _build_rules(
    summary: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    config: WQSubmissionExperienceConfig,
) -> dict[str, Any]:
    field_rules = []
    for row in summary.get("field_stats") or []:
        if row["count"] < config.min_field_evidence:
            continue
        checks = row.get("failed_checks") or {}
        failures = row.get("failure_kinds") or {}
        if row["success_count"] == 0 and (checks.get("CONCENTRATED_WEIGHT", 0) >= 2 or failures.get("concentrated_weight", 0) >= 2):
            field_rules.append({
                "field": row["field"],
                "action": "require_broad_overlay_or_block_standalone",
                "reason": "Repeated concentrated-weight failures without successful submitted evidence.",
                "evidence_count": row["count"],
            })
        elif row["success_count"] == 0 and checks.get("HIGH_TURNOVER", 0) >= 2:
            field_rules.append({
                "field": row["field"],
                "action": "require_smoothing",
                "reason": "Repeated high-turnover failures.",
                "evidence_count": row["count"],
            })
        elif row["near_pass_count"] > 0 and row["success_count"] == 0:
            field_rules.append({
                "field": row["field"],
                "action": "prefer_as_secondary_overlay",
                "reason": "Near-pass evidence exists, but standalone submission evidence is insufficient.",
                "evidence_count": row["count"],
            })

    structure_rules = []
    if _count_records(records, lambda row: _is_single_non_price(row) and row.get("outcome") != "success") >= 3:
        structure_rules.append({
            "rule_id": "no_single_non_price_standalone",
            "action": "block_or_blend",
            "reason": "Single non-price standalone signals repeatedly failed; require smoothing and broad overlay.",
        })
    if _count_records(records, lambda row: _has_event_fields(row) and "HIGH_TURNOVER" in (row.get("failed_checks") or [])) >= 2:
        structure_rules.append({
            "rule_id": "event_fields_need_smoothing",
            "action": "require_ts_mean_or_decay",
            "reason": "Event/news/social fields repeatedly produced high turnover without smoothing.",
        })
    if _count_records(records, lambda row: _has_low_coverage_prefix(row) and "CONCENTRATED_WEIGHT" in (row.get("failed_checks") or [])) >= 2:
        structure_rules.append({
            "rule_id": "low_coverage_anchor_needs_dispersion",
            "action": "require_price_volume_or_model_dispersion",
            "reason": "Low-coverage news/options style anchors repeatedly triggered concentrated-weight checks.",
        })

    repair_rules = []
    near_passes = summary.get("near_passes") or []
    if near_passes:
        repair_rules.append({
            "rule_id": "near_pass_repair",
            "action": "repair_before_new_family",
            "reason": "At least one near-pass record should be repaired with concentration relief and small orthogonal overlay.",
            "alpha_ids": [row.get("alpha_id") for row in near_passes[:5] if row.get("alpha_id")],
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "field_rules": field_rules,
        "structure_rules": structure_rules,
        "repair_rules": repair_rules,
    }


def _build_memory_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        if record.get("outcome") == "success":
            continue
        failure = str(record.get("failure_kind") or "unknown")
        if failure in {"none", "platform_alpha"}:
            continue
        severity = "block" if failure in {"self_correlation_fail", "prod_correlation_fail", "high_similarity"} else "penalize"
        rows.append({
            "schema_version": SCHEMA_VERSION,
            "memory_kind": "submission_experience",
            "memory_type": "expression_family" if severity == "penalize" else "expression_exact",
            "severity": severity,
            "failure_kind": failure,
            "alpha_id": record.get("alpha_id"),
            "expression": record.get("expression"),
            "expression_normalized": record.get("expression_normalized"),
            "expression_hash": record.get("expression_hash"),
            "field_signature": record.get("field_signature"),
            "fields": record.get("fields") or [],
            "operators": record.get("operators") or [],
            "source_type": record.get("source_type"),
            "source_file": record.get("source_file"),
            "source_run_id": record.get("source_run_id"),
            "sharpe": record.get("sharpe"),
            "fitness": record.get("fitness"),
            "turnover": record.get("turnover"),
            "weak_score": _weak_score(record),
            "repair_hints": _repair_hints(record),
            "retrieval_text": (
                f"{failure}: {record.get('tag') or record.get('source_family') or ''}; "
                f"sharpe={record.get('sharpe')} fitness={record.get('fitness')} "
                f"turnover={record.get('turnover')} failed={','.join(record.get('failed_checks') or [])}"
            ),
            "created_at": _now(),
        })
    return _dedupe_memory(rows)


def _repair_hints(record: dict[str, Any]) -> list[str]:
    checks = set(record.get("failed_checks") or [])
    hints = []
    if "CONCENTRATED_WEIGHT" in checks:
        hints.append("add_broad_price_volume_or_model_dispersion")
    if "HIGH_TURNOVER" in checks:
        hints.append("increase_decay_or_add_ts_mean")
    if "LOW_SHARPE" in checks or "LOW_FITNESS" in checks:
        hints.append("use_as_overlay_not_main_anchor")
    if record.get("is_near_pass"):
        hints.append("repair_near_pass_before_fresh_budget")
    return hints


def _weak_score(record: dict[str, Any]) -> float:
    score = 0.0
    score += max(_safe_float(record.get("fitness")) or 0.0, 0.0)
    score += 0.25 * max(_safe_float(record.get("sharpe")) or 0.0, 0.0)
    if record.get("is_near_pass"):
        score += 0.5
    return round(score, 6)


def _record_outcome(row: dict[str, Any], *, failure_kind: str | None, source_type: str) -> str:
    status = str(row.get("platform_status") or row.get("status") or "").upper()
    if bool(row.get("submitted")) or status in SUCCESS_STATUSES or source_type == "submitted_accumulator":
        return "success"
    if source_type == "presubmit_ready_sequential" or bool(row.get("submit_eligible")):
        return "pre_submit_pass"
    if failure_kind and failure_kind != "none":
        return "fail"
    return "candidate"


def _is_near_pass(sharpe: float | None, fitness: float | None, turnover: float | None) -> bool:
    if turnover is not None and not (0.01 <= turnover <= 0.7):
        return False
    return (sharpe is not None and sharpe >= 0.60) or (fitness is not None and fitness >= 0.80)


def _compact_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "alpha_id": row.get("alpha_id"),
        "tag": row.get("tag"),
        "source_family": row.get("source_family"),
        "expression": row.get("expression"),
        "sharpe": row.get("sharpe"),
        "fitness": row.get("fitness"),
        "returns": row.get("returns"),
        "turnover": row.get("turnover"),
        "failed_checks": row.get("failed_checks") or [],
        "repair_hints": _repair_hints(row),
    }


def _unique_near_passes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    seen = set()
    for row in rows:
        key = row.get("alpha_id") or _hash(str(row.get("expression") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _failed_check_names(row: dict[str, Any]) -> list[str]:
    return failed_check_names(row)


def _failure_from_checks(checks: list[str]) -> str:
    return failure_kind_from_check_names(checks) or "platform_check_fail"


def _check_result(row: dict[str, Any], name: str) -> str | None:
    target = name.upper()
    for check in row.get("is_checks") or _nested(row, ("result", "is_metrics", "checks")) or []:
        if isinstance(check, dict) and str(check.get("name") or "").upper() == target:
            return str(check.get("result") or "").upper()
    return None


def _is_single_non_price(row: dict[str, Any]) -> bool:
    fields = [field for field in row.get("fields") or [] if field not in GROUP_FIELDS and field != "returns"]
    return len(fields) == 1 and fields[0] not in PRICE_VOLUME_FIELDS


def _has_event_fields(row: dict[str, Any]) -> bool:
    return bool(set(row.get("fields") or []) & EVENT_FIELDS)


def _has_low_coverage_prefix(row: dict[str, Any]) -> bool:
    return any(str(field).startswith(LOW_COVERAGE_PREFIXES) for field in row.get("fields") or [])


def _count_records(records: list[dict[str, Any]], predicate) -> int:
    return sum(1 for row in records if predicate(row))


def _rule_count(rules: dict[str, Any]) -> int:
    return sum(len(rules.get(key) or []) for key in ("field_rules", "structure_rules", "repair_rules"))


def _mean(values) -> float | None:
    clean = [_safe_float(value) for value in values]
    nums = [value for value in clean if value is not None]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 6)


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _dedupe_rows_by_key(
        records,
        lambda row: (row.get("alpha_id"), row.get("expression_hash"), row.get("source_type"), row.get("source_run_id")),
    )


def _dedupe_memory(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _dedupe_rows_by_key(
        rows,
        lambda row: (row.get("memory_kind"), row.get("failure_kind"), row.get("expression_hash"), row.get("field_signature")),
    )


def _safe_normalize(expression: str) -> str | None:
    try:
        return normalize_expression(expression)
    except Exception:
        return None


def _record_id(source_file: Path, row_index: int, expression: str) -> str:
    return _hash(f"{source_file}|{row_index}|{expression}")[:24]


def _hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()
