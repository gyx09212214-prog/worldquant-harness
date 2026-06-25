"""Period quality review for WorldQuant alpha submissions and generated candidates."""

from __future__ import annotations

import copy
import csv
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .expression_parser import extract_components, normalize_expression
from .wq_efficiency import annotate_candidate_identity
from .wq_evolutionary_generator import classify_domain
from .wq_history_experience import (
    api_check_record_from_result,
    client_check_alpha_submission,
    discover_local_history_files,
    fetch_platform_alphas,
    normalize_event,
    normalize_platform_alpha,
    platform_source_row,
    source_type_for_path,
)
from .wq_research_profile import load_profile, save_profile

SCHEMA_VERSION = 1
SUCCESS_STATUSES = {"ACTIVE", "SUBMITTED"}
GENERATED_LIFECYCLES = {
    "candidate",
    "weak",
    "invalid",
    "api_check_failed",
    "correlation_pending",
    "pre_submit_pass",
    "skipped_similar",
    "self_corr_fail",
    "prod_corr_fail",
}
SELF_CORRELATION_FAILURES = {"self_correlation", "self_correlation_fail", "self_corr_fail"}
PROD_CORRELATION_FAILURES = {"prod_correlation", "prod_correlation_fail", "prod_corr_fail"}
HIGH_SIMILARITY_FAILURES = {"high_similarity", "too_similar_to_real_or_virtual_active", "skipped_similar"}
INVALID_FIELD_TOKENS = ("not_a_real", "illegal", "unknown_field")
LOCAL_EXTRA_PATTERNS = (
    "alpha_lifecycle_events.jsonl",
    "candidate_pool.jsonl",
    "candidate_specs.jsonl",
    "candidates.jsonl",
)


@dataclass(frozen=True)
class WQAlphaQualityReviewConfig:
    reports_dir: Path
    output_dir: Path
    account: str = "primary"
    since: str | None = None
    until: str | None = None
    window_days: int = 14
    platform_enabled: bool = True
    check_policy: str = "window_unsubmitted"
    max_checks: int = 50
    check_polls: int = 2
    check_interval: int = 5
    platform_limit: int = 0
    local_file_limit: int = 0
    obsidian_output: Path | None = None
    profile_dir: Path | None = None
    write_profile_candidate: bool = True


def build_alpha_quality_review(
    config: WQAlphaQualityReviewConfig,
    *,
    client_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Build a period review from local artifacts, optional WQ read-only API, and existing maps."""

    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    since, until = _period_bounds(config)

    local_events = _collect_local_events(Path(config.reports_dir), limit=config.local_file_limit)
    platform = _collect_platform_events(config, since=since, until=until, client_factory=client_factory)
    all_events = [*local_events, *(platform.get("events") or [])]
    records = _dedupe_records([
        record
        for event in all_events
        if (record := _quality_record(event)) is not None and _record_in_period(record, since=since, until=until)
    ])
    map_context = _load_map_context(Path(config.reports_dir))
    metrics = _quality_metrics(records)
    pressure = _self_correlation_pressure(records, map_context)
    directions = _recommended_directions(records, pressure, map_context)

    files = {
        "quality_alpha_events": str(output_dir / "quality_alpha_events.jsonl"),
        "submitted_quality": str(output_dir / "submitted_quality.csv"),
        "unsubmitted_quality": str(output_dir / "unsubmitted_quality.csv"),
        "self_correlation_pressure": str(output_dir / "self_correlation_pressure.csv"),
        "recommended_directions": str(output_dir / "recommended_directions.json"),
        "summary": str(output_dir / "summary.json"),
        "markdown": str(output_dir / "quality_review.md"),
    }
    profile_result = {"ok": True, "skipped": True, "reason": "profile candidate disabled"}
    if config.write_profile_candidate:
        profile_result = _write_profile_candidate(config, metrics=metrics, pressure=pressure, directions=directions, files=files)
        if profile_result.get("path"):
            files["profile_candidate"] = str(profile_result["path"])

    submitted_rows = [row for row in records if row["cohort"] == "submitted"]
    generated_rows = [row for row in records if row["cohort"] == "generated"]
    report = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "created_at": _now(),
        "mode": "wq_alpha_quality_review",
        "period": {
            "since": since.isoformat(timespec="seconds"),
            "until": until.isoformat(timespec="seconds"),
            "window_days": config.window_days,
        },
        "reports_dir": str(Path(config.reports_dir)),
        "output_dir": str(output_dir),
        "platform": platform.get("summary", {}),
        "counts": {
            "local_events": len(local_events),
            "platform_events": len(platform.get("events") or []),
            "quality_records": len(records),
            "submitted_records": len(submitted_rows),
            "generated_records": len(generated_rows),
        },
        "metrics": metrics,
        "self_correlation_pressure": pressure,
        "recommended_directions": directions,
        "profile_candidate": profile_result,
        "map_context": map_context.get("summary", {}),
        "files": files,
    }

    _write_jsonl(Path(files["quality_alpha_events"]), records)
    _write_csv(Path(files["submitted_quality"]), submitted_rows)
    _write_csv(Path(files["unsubmitted_quality"]), generated_rows)
    _write_csv(Path(files["self_correlation_pressure"]), pressure)
    _write_json(Path(files["recommended_directions"]), {"directions": directions})
    _write_json(Path(files["summary"]), report)
    markdown = render_alpha_quality_review_markdown(report)
    _write_text(Path(files["markdown"]), markdown)
    if config.obsidian_output:
        _write_text(Path(config.obsidian_output), markdown)
        report["files"]["obsidian"] = str(config.obsidian_output)
        _write_json(Path(files["summary"]), report)
    return report


def render_alpha_quality_review_markdown(report: dict[str, Any]) -> str:
    metrics = report.get("metrics") or {}
    period = report.get("period") or {}
    counts = report.get("counts") or {}
    pressure = report.get("self_correlation_pressure") or []
    directions = report.get("recommended_directions") or []
    generated_at = report.get("created_at") or _now()
    lines = [
        "---",
        "tags:",
        "  - worldquant",
        "  - alpha-quality-review",
        "  - worldquant_harness",
        f"generated_at: {generated_at}",
        "---",
        "",
        "# WorldQuant Alpha 提交质量复盘",
        "",
        "## 复盘口径",
        "",
        f"- 窗口：`{period.get('since')}` 到 `{period.get('until')}`。",
        f"- 样本：submitted={counts.get('submitted_records', 0)}，generated/unsubmitted={counts.get('generated_records', 0)}。",
        "- 平台动作：只读列表和 check-only；不调用 submit/delete。",
        "",
        "## 核心指标",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key in (
        "period_quality_score",
        "submitted_quality_score",
        "generated_quality_score",
        "correlation_quality_score",
        "diversity_score",
        "submitted_metric_pass_rate",
        "generated_metric_pass_rate",
        "generated_ready_rate",
        "generated_self_correlation_fail_share",
        "near_pass_share",
        "field_signature_duplicate_ratio",
    ):
        lines.append(f"| `{key}` | {_fmt(metrics.get(key))} |")

    lines.extend([
        "",
        "## 主要制约",
        "",
        "| Group | Count | SELF Fail | SELF Share | Ready | Active | Median SC | Avg Fitness | Map Crowded |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for row in pressure[:12]:
        lines.append(
            f"| `{_md(row.get('group_type'))}:{_md(row.get('group_key'))}` | {row.get('count')} | "
            f"{row.get('self_correlation_fail_count')} | {_fmt(row.get('self_correlation_fail_share'))} | "
            f"{row.get('ready_count')} | {row.get('active_count')} | {_fmt(row.get('median_sc_value'))} | "
            f"{_fmt(row.get('avg_fitness'))} | {_fmt(row.get('map_crowded_score'))} |"
        )

    lines.extend(["", "## 下一阶段推荐合成方向", ""])
    if directions:
        for index, row in enumerate(directions, start=1):
            lines.extend([
                f"### {index}. {row.get('title')}",
                "",
                f"- 理由：{row.get('rationale')}",
                f"- Seed fields：{', '.join(row.get('seed_fields') or []) or 'n/a'}",
                f"- Avoid fields：{', '.join(row.get('avoid_fields') or []) or 'n/a'}",
                f"- Operator bias：{', '.join(row.get('operator_biases') or []) or 'n/a'}",
                f"- 预算：{row.get('budget_hint')}",
                f"- 生成 brief：{row.get('candidate_generation_brief')}",
                "",
            ])
    else:
        lines.append("- 暂无足够样本生成方向建议。")

    files = report.get("files") or {}
    lines.extend([
        "## 输出文件",
        "",
        f"- Records：`{files.get('quality_alpha_events')}`",
        f"- SELF pressure：`{files.get('self_correlation_pressure')}`",
        f"- Profile candidate：`{files.get('profile_candidate', '')}`",
    ])
    return "\n".join(lines).rstrip() + "\n"


def _collect_local_events(reports_dir: Path, *, limit: int) -> list[dict[str, Any]]:
    files = discover_local_history_files(reports_dir)
    seen = {path.resolve() for path in files}
    for pattern in LOCAL_EXTRA_PATTERNS:
        for path in sorted(reports_dir.rglob(pattern), key=lambda item: item.stat().st_mtime, reverse=True):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(path)
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    if limit > 0:
        files = files[:limit]

    events: list[dict[str, Any]] = []
    for path in files:
        source_type = "lifecycle_event" if path.name == "alpha_lifecycle_events.jsonl" else source_type_for_path(path)
        file_time = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        for index, row in enumerate(_read_jsonl(path)):
            event = normalize_event(row, source_type=source_type, source_file=str(path), row_index=index)
            if row.get("event_type"):
                event["event_type"] = row.get("event_type")
            if row.get("candidate_uid"):
                event["candidate_uid"] = row.get("candidate_uid")
            if row.get("reason"):
                event["reason"] = row.get("reason")
            if isinstance(row.get("metrics"), dict):
                metrics = row["metrics"]
                for key in ("sharpe", "fitness", "returns", "turnover", "sc_value", "prod_corr_value"):
                    if event.get(key) in (None, "") and metrics.get(key) not in (None, ""):
                        event[key] = metrics.get(key)
            if isinstance(row.get("efficiency_settings"), dict):
                event["settings"] = row["efficiency_settings"]
            event["event_time"] = row.get("event_time") or row.get("created_at") or event.get("created_at") or file_time
            events.append(event)
    return events


def _collect_platform_events(
    config: WQAlphaQualityReviewConfig,
    *,
    since: datetime,
    until: datetime,
    client_factory: Callable[[str], Any] | None,
) -> dict[str, Any]:
    if not config.platform_enabled:
        return {"events": [], "summary": {"enabled": False, "skipped": True, "reason": "platform disabled"}}

    if client_factory is None:
        from .wq_brain_client import get_client, is_configured

        if not is_configured(config.account):
            return {
                "events": [],
                "summary": {
                    "enabled": True,
                    "skipped": True,
                    "reason": f"WQ credentials not configured for {config.account}",
                },
            }
        client_factory = get_client

    client = client_factory(config.account)
    try:
        if hasattr(client, "authenticate") and not client.authenticate():
            return {"events": [], "summary": {"enabled": True, "skipped": True, "reason": "authentication failed"}}

        raw_alphas = fetch_platform_alphas(client, limit=max(0, config.platform_limit))
        platform_events = []
        for row in raw_alphas:
            event = normalize_platform_alpha(row, source_file="platform:/users/self/alphas")
            event["event_time"] = _platform_event_time(row)
            event["dateCreated"] = row.get("dateCreated")
            event["dateSubmitted"] = row.get("dateSubmitted")
            platform_events.append(event)

        check_targets = _check_targets(raw_alphas, platform_events, config=config, since=since, until=until)
        check_records = []
        for source_row in check_targets:
            result = client_check_alpha_submission(
                client,
                str(source_row.get("alpha_id")),
                max_polls=max(1, config.check_polls),
                interval=max(0, config.check_interval),
            )
            check_records.append(api_check_record_from_result(source_row, result))

        check_events = []
        for row in check_records:
            event = normalize_event(row, source_type="api_check", source_file="platform:/alphas/check")
            event["event_time"] = row.get("created_at") or _now()
            check_events.append(event)

        return {
            "events": [*platform_events, *check_events],
            "summary": {
                "enabled": True,
                "skipped": False,
                "alpha_count": len(raw_alphas),
                "check_count": len(check_records),
                "check_policy": config.check_policy,
                "max_checks": config.max_checks,
            },
        }
    finally:
        if hasattr(client, "close"):
            client.close()


def _check_targets(
    raw_alphas: list[dict[str, Any]],
    platform_events: list[dict[str, Any]],
    *,
    config: WQAlphaQualityReviewConfig,
    since: datetime,
    until: datetime,
) -> list[dict[str, Any]]:
    if config.check_policy == "none":
        return []
    source_by_id = {
        str(source.get("alpha_id") or ""): source
        for source in (platform_source_row(row) for row in raw_alphas)
        if source.get("alpha_id")
    }
    targets: list[dict[str, Any]] = []
    for event in platform_events:
        status = str(event.get("platform_status") or "").upper()
        if status != "UNSUBMITTED":
            continue
        if config.check_policy == "window_unsubmitted" and not _record_in_period(
            {"event_time": event.get("event_time") or event.get("created_at")},
            since=since,
            until=until,
        ):
            continue
        source = source_by_id.get(str(event.get("alpha_id") or ""))
        if source:
            targets.append(source)
    if config.max_checks > 0:
        return targets[: config.max_checks]
    return targets


def _quality_record(event: dict[str, Any]) -> dict[str, Any] | None:
    expression = str(event.get("expression") or "").strip()
    alpha_id = str(event.get("alpha_id") or "").strip()
    if not expression and not alpha_id:
        return None
    annotated = annotate_candidate_identity({"expression": expression, **event}, event.get("settings") or {})
    components = _components(expression)
    platform_status = str(event.get("platform_status") or event.get("status") or "").upper()
    lifecycle = str(event.get("lifecycle_status") or "").lower()
    failure_kind = _failure_kind(event)
    sc_value = _first_float(event.get("sc_value"), event.get("self_correlation_value"), _nested(event, "metrics", "sc_value"))
    prod_value = _first_float(
        event.get("prod_corr_value"),
        event.get("prod_correlation_value"),
        _nested(event, "metrics", "prod_corr_value"),
    )
    sharpe = _first_float(event.get("sharpe"), _nested(event, "metrics", "sharpe"))
    fitness = _first_float(event.get("fitness"), _nested(event, "metrics", "fitness"))
    returns = _first_float(event.get("returns"), _nested(event, "metrics", "returns"))
    turnover = _first_float(event.get("turnover"), _nested(event, "metrics", "turnover"))
    cohort = _cohort(platform_status=platform_status, lifecycle=lifecycle, event=event)
    quality_bucket = _quality_bucket(
        platform_status=platform_status,
        lifecycle=lifecycle,
        failure_kind=failure_kind,
        event=event,
    )
    fields = sorted(components["fields"])
    domain = str(event.get("domain") or classify_domain(fields, expression))
    event_time = _first_text(event.get("event_time"), event.get("created_at"), event.get("dateSubmitted"), event.get("dateCreated"))
    return {
        "schema_version": SCHEMA_VERSION,
        "event_time": event_time,
        "cohort": cohort,
        "quality_bucket": quality_bucket,
        "alpha_id": alpha_id,
        "candidate_uid": annotated.get("candidate_uid"),
        "expression": expression,
        "expression_normalized": normalize_expression(expression) if expression else "",
        "expression_hash": annotated.get("expression_hash"),
        "settings_hash": annotated.get("settings_hash"),
        "settings": annotated.get("efficiency_settings"),
        "platform_status": platform_status,
        "lifecycle_status": lifecycle,
        "api_check_status": event.get("api_check_status"),
        "failure_kind": failure_kind,
        "source_type": event.get("source_type"),
        "source_file": event.get("source_file"),
        "source_run_id": event.get("source_run_id"),
        "tag": event.get("tag"),
        "source_family": event.get("source_family") or event.get("tag") or "unknown",
        "field_signature": annotated.get("field_signature") or "|".join(fields),
        "fields": fields,
        "operators": sorted(components["operators"]),
        "domain": domain,
        "sharpe": sharpe,
        "fitness": fitness,
        "returns": returns,
        "turnover": turnover,
        "metric_pass": _metric_pass(sharpe=sharpe, fitness=fitness, turnover=turnover),
        "near_pass": _near_pass(sharpe=sharpe, fitness=fitness, turnover=turnover),
        "sc_result": event.get("sc_result"),
        "sc_value": sc_value,
        "sc_limit": _first_float(event.get("sc_limit")),
        "prod_corr_result": event.get("prod_corr_result"),
        "prod_corr_value": prod_value,
        "prod_corr_limit": _first_float(event.get("prod_corr_limit")),
        "nearest_similarity": _first_float(event.get("nearest_similarity")),
        "created_at": event.get("created_at"),
        "dateCreated": event.get("dateCreated"),
        "dateSubmitted": event.get("dateSubmitted"),
    }


def _quality_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    submitted = [row for row in records if row["cohort"] == "submitted"]
    generated = [row for row in records if row["cohort"] == "generated"]
    submitted_pass = sum(1 for row in submitted if row.get("metric_pass"))
    generated_pass = sum(1 for row in generated if row.get("metric_pass"))
    generated_ready = sum(1 for row in generated if row.get("quality_bucket") == "ready")
    generated_self = sum(1 for row in generated if row.get("failure_kind") == "self_correlation_fail")
    generated_prod = sum(1 for row in generated if row.get("failure_kind") == "prod_correlation_fail")
    near_pass = sum(1 for row in generated if row.get("near_pass"))
    signatures = [str(row.get("field_signature") or "") for row in records if row.get("field_signature")]
    duplicate_ratio = _duplicate_ratio(signatures)

    submitted_score = None
    if submitted:
        submitted_score = _weighted([
            (_ratio(submitted_pass, len(submitted)), 0.45),
            (_score_avg(submitted, "fitness", target=1.5), 0.25),
            (_score_avg(submitted, "sharpe", target=2.0), 0.20),
            (_turnover_quality(submitted), 0.10),
        ])
    generated_score = None
    if generated:
        generated_score = _weighted([
            (_ratio(generated_pass, len(generated)), 0.35),
            (_ratio(generated_ready, len(generated)), 0.30),
            (_ratio(near_pass, len(generated)), 0.20),
            (1.0 - min(_ratio(generated_self, len(generated)) or 0.0, 1.0), 0.15),
        ])
    correlation_score = None
    if generated:
        correlation_score = round(1.0 - min(_ratio(generated_self + generated_prod, len(generated)) or 0.0, 1.0), 6)
    diversity_score = round(1.0 - min(duplicate_ratio or 0.0, 1.0), 6) if records else None
    period_score = _weighted([
        (submitted_score, 0.30),
        (generated_score, 0.30),
        (correlation_score, 0.25),
        (diversity_score, 0.15),
    ])

    failure_counts = Counter(str(row.get("failure_kind") or "none") for row in records)
    bucket_counts = Counter(str(row.get("quality_bucket") or "unknown") for row in records)
    return {
        "record_count": len(records),
        "submitted_count": len(submitted),
        "generated_count": len(generated),
        "active_count": sum(1 for row in submitted if row.get("platform_status") == "ACTIVE"),
        "unsubmitted_count": sum(1 for row in records if row.get("platform_status") == "UNSUBMITTED"),
        "submitted_metric_pass_rate": _ratio(submitted_pass, len(submitted)),
        "generated_metric_pass_rate": _ratio(generated_pass, len(generated)),
        "generated_ready_rate": _ratio(generated_ready, len(generated)),
        "generated_self_correlation_fail_share": _ratio(generated_self, len(generated)),
        "generated_prod_correlation_fail_share": _ratio(generated_prod, len(generated)),
        "near_pass_count": near_pass,
        "near_pass_share": _ratio(near_pass, len(generated)),
        "field_signature_unique_count": len(set(signatures)),
        "field_signature_duplicate_ratio": duplicate_ratio,
        "avg_submitted_sharpe": _mean(row.get("sharpe") for row in submitted),
        "avg_submitted_fitness": _mean(row.get("fitness") for row in submitted),
        "avg_generated_sharpe": _mean(row.get("sharpe") for row in generated),
        "avg_generated_fitness": _mean(row.get("fitness") for row in generated),
        "failure_kind_counts": dict(sorted(failure_counts.items())),
        "quality_bucket_counts": dict(sorted(bucket_counts.items())),
        "submitted_quality_score": submitted_score,
        "generated_quality_score": generated_score,
        "correlation_quality_score": correlation_score,
        "diversity_score": diversity_score,
        "period_quality_score": period_score,
    }


def _self_correlation_pressure(records: list[dict[str, Any]], map_context: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        for group_type, group_key in (
            ("field_signature", row.get("field_signature")),
            ("source_family", row.get("source_family")),
            ("domain", row.get("domain")),
        ):
            if group_key:
                grouped[(group_type, str(group_key))].append(row)

    domain_map = map_context.get("domains") or {}
    field_map = map_context.get("fields") or {}
    rows: list[dict[str, Any]] = []
    for (group_type, group_key), items in grouped.items():
        self_count = sum(1 for row in items if row.get("failure_kind") == "self_correlation_fail")
        sc_values = [_first_float(row.get("sc_value")) for row in items]
        sc_values = [value for value in sc_values if value is not None]
        map_row = domain_map.get(group_key) if group_type == "domain" else {}
        if group_type == "field_signature":
            fields = [field for field in group_key.split("|") if field]
            crowded = [_first_float((field_map.get(field) or {}).get("crowded_score")) for field in fields]
            active_counts = [_first_float((field_map.get(field) or {}).get("active_or_submitted_count")) for field in fields]
            map_crowded_score = _mean(value for value in crowded if value is not None)
            map_active = int(sum(value for value in active_counts if value is not None))
        else:
            map_crowded_score = _first_float(map_row.get("crowded_score")) if map_row else None
            map_active = _int_or_none(map_row.get("active_or_submitted_count")) if map_row else None
        rows.append({
            "group_type": group_type,
            "group_key": group_key,
            "count": len(items),
            "generated_count": sum(1 for row in items if row.get("cohort") == "generated"),
            "submitted_count": sum(1 for row in items if row.get("cohort") == "submitted"),
            "ready_count": sum(1 for row in items if row.get("quality_bucket") == "ready"),
            "active_count": sum(1 for row in items if row.get("platform_status") == "ACTIVE"),
            "near_pass_count": sum(1 for row in items if row.get("near_pass")),
            "self_correlation_fail_count": self_count,
            "self_correlation_fail_share": _ratio(self_count, len(items)),
            "high_similarity_count": sum(1 for row in items if row.get("failure_kind") == "high_similarity"),
            "median_sc_value": round(statistics.median(sc_values), 6) if sc_values else None,
            "avg_fitness": _mean(row.get("fitness") for row in items),
            "map_crowded_score": map_crowded_score,
            "map_active_or_submitted_count": map_active,
        })
    rows.sort(
        key=lambda row: (
            row["self_correlation_fail_count"],
            _first_float(row["self_correlation_fail_share"]) or 0.0,
            row["near_pass_count"],
            row["count"],
        ),
        reverse=True,
    )
    return rows


def _recommended_directions(
    records: list[dict[str, Any]],
    pressure: list[dict[str, Any]],
    map_context: dict[str, Any],
) -> list[dict[str, Any]]:
    top_signature = next((row for row in pressure if row["group_type"] == "field_signature" and row["self_correlation_fail_count"] > 0), None)
    avoid_fields = _avoid_fields(top_signature)
    opportunity_domains = sorted(
        (map_context.get("domains") or {}).values(),
        key=lambda row: (
            _first_float(row.get("opportunity_score")) or 0.0,
            -(_first_float(row.get("self_corr_fail_count")) or 0.0),
        ),
        reverse=True,
    )
    low_pressure_fields = _low_pressure_fields(records, avoid_fields=avoid_fields)
    near_pass_rows = [
        row for row in records
        if row.get("near_pass") and row.get("failure_kind") == "self_correlation_fail"
    ]

    directions: list[dict[str, Any]] = []
    if top_signature:
        directions.append({
            "title": "Self-correlation 高压 family 的跨域 overlay 修复",
            "rationale": (
                f"`{top_signature['group_key']}` 出现 {top_signature['self_correlation_fail_count']} 次 SELF 失败，"
                "继续只调窗口或 decay 的边际收益较低。"
            ),
            "seed_fields": low_pressure_fields[:6],
            "avoid_fields": avoid_fields,
            "operator_biases": ["group_rank", "ts_rank", "trade_when", "rank"],
            "expected_blocker": "self_correlation",
            "budget_hint": "先做 20-40 个小批量候选，要求 field signature 与高压组无完全重合。",
            "candidate_generation_brief": "保留高压 family 的经济含义，但主信号换到低重合字段，并只用原字段作弱 overlay。",
        })

    for domain_row in opportunity_domains[:2]:
        domain = str(domain_row.get("domain") or "")
        if not domain:
            continue
        fields = _top_values_to_list(domain_row.get("top_fields")) or low_pressure_fields[:5]
        directions.append({
            "title": f"低覆盖机会域扩展：{domain}",
            "rationale": (
                f"地图中 `{domain}` opportunity_score={_fmt(domain_row.get('opportunity_score'))}，"
                f"active/submitted={domain_row.get('active_or_submitted_count', 0)}。"
            ),
            "seed_fields": [field for field in fields if field not in avoid_fields][:6],
            "avoid_fields": avoid_fields,
            "operator_biases": ["ts_mean", "ts_delta", "rank", "group_zscore"],
            "expected_blocker": "low_signal_or_concentration",
            "budget_hint": "每个 domain 10-20 个候选，优先用宽覆盖字段做主腿。",
            "candidate_generation_brief": "从地图低覆盖域挑 seed，用 price/volume 或 model dispersion 做稳定化，不复制 active family 结构。",
        })

    if near_pass_rows:
        best = sorted(near_pass_rows, key=lambda row: -(_first_float(row.get("fitness")) or 0.0))[0]
        directions.append({
            "title": "Near-pass alpha 的结构性去相关修复",
            "rationale": (
                f"`{best.get('alpha_id') or best.get('candidate_uid')}` fitness={_fmt(best.get('fitness'))} "
                f"但 SELF={_fmt(best.get('sc_value'))}，应换字段/算子 family。"
            ),
            "seed_fields": [field for field in best.get("fields") or [] if field not in avoid_fields][:4] + low_pressure_fields[:3],
            "avoid_fields": avoid_fields,
            "operator_biases": ["trade_when", "group_zscore", "ts_av_diff", "winsorize"],
            "expected_blocker": "self_correlation",
            "budget_hint": "围绕 near-pass 样本做 10-15 个 repair，禁止仅改 window/decay/truncation。",
            "candidate_generation_brief": "复用 near-pass 的 alpha 方向，但至少替换一个主字段和一个主算子，控制 daily return similarity。",
        })

    if not directions:
        directions.append({
            "title": "低重合字段系统网格探索",
            "rationale": "当前窗口样本不足或没有明显 SELF 高压组，先扩大低重合字段覆盖。",
            "seed_fields": low_pressure_fields[:8],
            "avoid_fields": avoid_fields,
            "operator_biases": ["rank", "ts_rank", "ts_mean", "group_rank"],
            "expected_blocker": "unknown",
            "budget_hint": "40 个以内本地候选，先跑 presubmit-sequential，不真实 submit。",
            "candidate_generation_brief": "按 field signature 做去重网格，目标是提高 generated metric pass 和 ready yield。",
        })
    return directions[:5]


def _write_profile_candidate(
    config: WQAlphaQualityReviewConfig,
    *,
    metrics: dict[str, Any],
    pressure: list[dict[str, Any]],
    directions: list[dict[str, Any]],
    files: dict[str, str],
) -> dict[str, Any]:
    try:
        profile = copy.deepcopy(load_profile(profile_dir=config.profile_dir))
    except Exception as exc:
        return {"ok": False, "skipped": True, "reason": f"failed to load active profile: {exc}"}

    candidate_name = f"quality_review_{date.today():%Y%m%d}"
    profile["candidate_key"] = candidate_name
    profile["candidate_label"] = "period_alpha_quality_review"
    profile["profile_version"] = int(profile.get("profile_version") or 0) + 1
    profile["updated_at"] = _now()
    profile["quality_review"] = {
        "source": str(config.output_dir),
        "period_quality_score": metrics.get("period_quality_score"),
        "generated_self_correlation_fail_share": metrics.get("generated_self_correlation_fail_share"),
        "direction_count": len(directions),
        "apply_guard": "candidate only; review before applying to active profile",
    }
    biases = list(profile.get("priority_biases") or [])
    for value in ("period_quality_review", "self_correlation_pressure_avoidance", "map_guided_low_overlap_direction"):
        if value not in biases:
            biases.append(value)
    profile["priority_biases"] = biases

    signature_policy = profile.setdefault("field_signature_policy", {})
    blacklist = [str(value) for value in signature_policy.get("blacklist") or [] if value]
    for row in pressure:
        if row.get("group_type") == "field_signature" and row.get("self_correlation_fail_count", 0) > 0:
            signature = str(row.get("group_key") or "")
            if signature and signature not in blacklist:
                blacklist.append(signature)
        if len(blacklist) >= 12:
            break
    signature_policy["blacklist"] = blacklist
    signature_policy["max_field_signature_count"] = max(1, int(signature_policy.get("max_field_signature_count") or 4) - 1)

    similarity_policy = profile.setdefault("similarity_policy", {})
    current_cutoff = _first_float(similarity_policy.get("cutoff")) or 0.72
    if (_first_float(metrics.get("generated_self_correlation_fail_share")) or 0.0) >= 0.25:
        similarity_policy["cutoff"] = round(max(0.55, current_cutoff - 0.03), 3)

    mine_defaults = profile.setdefault("mine_defaults", {})
    mine_defaults["no_real_submit"] = True
    weak_files = [str(value) for value in mine_defaults.get("weak_memory_files") or [] if value]
    if files["quality_alpha_events"] not in weak_files:
        weak_files.append(files["quality_alpha_events"])
    mine_defaults["weak_memory_files"] = weak_files
    mine_defaults["direction_briefs"] = [row.get("candidate_generation_brief") for row in directions if row.get("candidate_generation_brief")]

    result = save_profile(candidate_name, profile, profile_dir=config.profile_dir, as_candidate=True)
    return {"ok": True, "skipped": False, "candidate": candidate_name, "path": result.get("path")}


def _load_map_context(reports_dir: Path) -> dict[str, Any]:
    domain_files = _latest_files(reports_dir, {"domain_summary.csv", "active_domain_summary.csv"}, limit=4)
    field_files = _latest_files(reports_dir, {"field_summary.csv", "active_field_summary.csv"}, limit=4)
    domains: dict[str, dict[str, Any]] = {}
    fields: dict[str, dict[str, Any]] = {}
    for path in domain_files:
        for row in _read_csv(path):
            key = str(row.get("domain") or "")
            if key and key not in domains:
                row["source_file"] = str(path)
                domains[key] = row
    for path in field_files:
        for row in _read_csv(path):
            key = str(row.get("field") or "")
            if key and key not in fields:
                row["source_file"] = str(path)
                fields[key] = row
    return {
        "summary": {
            "domain_files": [str(path) for path in domain_files],
            "field_files": [str(path) for path in field_files],
            "domains": len(domains),
            "fields": len(fields),
        },
        "domains": domains,
        "fields": fields,
    }


def _period_bounds(config: WQAlphaQualityReviewConfig) -> tuple[datetime, datetime]:
    until = _parse_time(config.until, end=True) if config.until else datetime.now(timezone.utc)
    since = _parse_time(config.since, end=False) if config.since else until - timedelta(days=max(1, config.window_days))
    return since, until


def _record_in_period(record: dict[str, Any], *, since: datetime, until: datetime) -> bool:
    dt = _parse_time(record.get("event_time") or record.get("created_at"), end=False)
    if dt is None:
        return True
    return since <= dt < until


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for row in records:
        key = str(row.get("alpha_id") or row.get("candidate_uid") or row.get("expression_hash") or "")
        if not key:
            continue
        current = by_key.get(key)
        if current is None or _record_rank(row) >= _record_rank(current):
            if current is not None:
                row["evidence_count"] = int(current.get("evidence_count") or 1) + 1
            else:
                row["evidence_count"] = int(row.get("evidence_count") or 1)
            by_key[key] = row
        elif current is not None:
            current["evidence_count"] = int(current.get("evidence_count") or 1) + 1
    return sorted(by_key.values(), key=lambda row: (str(row.get("event_time") or ""), str(row.get("alpha_id") or "")))


def _record_rank(row: dict[str, Any]) -> tuple[int, str]:
    bucket_rank = {
        "active": 9,
        "submitted": 8,
        "blocked_self_correlation": 7,
        "blocked_prod_correlation": 7,
        "high_similarity": 6,
        "ready": 5,
        "pending_correlation": 4,
        "weak_metrics": 2,
        "generated_candidate": 1,
    }
    return bucket_rank.get(str(row.get("quality_bucket") or ""), 0), str(row.get("event_time") or "")


def _cohort(*, platform_status: str, lifecycle: str, event: dict[str, Any]) -> str:
    if platform_status in SUCCESS_STATUSES or lifecycle in {"active", "submitted"} or bool(event.get("submitted")):
        return "submitted"
    return "generated"


def _quality_bucket(*, platform_status: str, lifecycle: str, failure_kind: str, event: dict[str, Any]) -> str:
    event_type = str(event.get("event_type") or "")
    if platform_status == "ACTIVE" or lifecycle == "active":
        return "active"
    if platform_status == "SUBMITTED" or lifecycle == "submitted":
        return "submitted"
    if failure_kind == "self_correlation_fail" or lifecycle == "self_corr_fail":
        return "blocked_self_correlation"
    if failure_kind == "prod_correlation_fail" or lifecycle == "prod_corr_fail":
        return "blocked_prod_correlation"
    if failure_kind == "high_similarity" or lifecycle == "skipped_similar":
        return "high_similarity"
    if lifecycle == "correlation_pending" or str(event.get("api_check_status") or "") == "api_check_pending":
        return "pending_correlation"
    if event_type == "candidate_ready" or lifecycle == "pre_submit_pass" or bool(event.get("submit_eligible")):
        return "ready"
    if failure_kind and failure_kind != "none":
        return "weak_metrics"
    return "generated_candidate"


def _failure_kind(event: dict[str, Any]) -> str:
    raw = str(event.get("failure_kind") or event.get("review_failure_kind") or "").lower()
    api_status = str(event.get("api_check_status") or "").lower()
    lifecycle = str(event.get("lifecycle_status") or "").lower()
    reason = str(event.get("reason") or event.get("presubmit_reject_reason") or event.get("status") or "").lower()
    if raw in SELF_CORRELATION_FAILURES or api_status == "self_correlation_fail" or lifecycle == "self_corr_fail":
        return "self_correlation_fail"
    if raw in PROD_CORRELATION_FAILURES or api_status == "prod_correlation_fail" or lifecycle == "prod_corr_fail":
        return "prod_correlation_fail"
    if raw in HIGH_SIMILARITY_FAILURES or lifecycle == "skipped_similar" or "too_similar" in reason:
        return "high_similarity"
    if "self_correlation" in reason:
        return "self_correlation_fail"
    if "prod_correlation" in reason:
        return "prod_correlation_fail"
    if raw:
        return raw
    return "none"


def _components(expression: str) -> dict[str, set[str]]:
    if not expression:
        return {"fields": set(), "operators": set()}
    try:
        components = extract_components(expression)
    except Exception:
        return {"fields": set(), "operators": set()}
    return {
        "fields": {str(value) for value in components.get("fields") or []},
        "operators": {str(value) for value in components.get("operators") or []},
    }


def _metric_pass(*, sharpe: float | None, fitness: float | None, turnover: float | None) -> bool:
    return (
        sharpe is not None
        and fitness is not None
        and turnover is not None
        and sharpe >= 1.25
        and fitness >= 1.0
        and 0.01 <= turnover <= 0.70
    )


def _near_pass(*, sharpe: float | None, fitness: float | None, turnover: float | None) -> bool:
    turnover_ok = turnover is None or 0.005 <= turnover <= 0.90
    return (
        sharpe is not None
        and fitness is not None
        and sharpe >= 0.60
        and fitness >= 0.80
        and turnover_ok
    )


def _avoid_fields(row: dict[str, Any] | None) -> list[str]:
    if not row:
        return []
    return [field for field in str(row.get("group_key") or "").split("|") if field]


def _low_pressure_fields(records: list[dict[str, Any]], *, avoid_fields: list[str]) -> list[str]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        for field in row.get("fields") or []:
            if field not in avoid_fields and not _invalid_seed_field(str(field)):
                buckets[str(field)].append(row)
    scored = []
    for field, rows in buckets.items():
        self_count = sum(1 for row in rows if row.get("failure_kind") == "self_correlation_fail")
        active_count = sum(1 for row in rows if row.get("cohort") == "submitted")
        near_count = sum(1 for row in rows if row.get("near_pass"))
        scored.append((near_count + active_count - self_count * 2, len(rows), field))
    scored.sort(reverse=True)
    return [field for _, _, field in scored[:12]]


def _invalid_seed_field(field: str) -> bool:
    text = field.lower()
    return not text or any(token in text for token in INVALID_FIELD_TOKENS)


def _top_values_to_list(value: Any) -> list[str]:
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, dict) and item.get("value"):
                out.append(str(item["value"]))
            elif item:
                out.append(str(item))
        return out
    if isinstance(value, str):
        return [part.strip(" `") for part in value.split(",") if part.strip()]
    return []


def _platform_event_time(row: dict[str, Any]) -> str | None:
    status = str(row.get("status") or "").upper()
    if status in SUCCESS_STATUSES and row.get("dateSubmitted"):
        return str(row.get("dateSubmitted"))
    return _first_text(row.get("dateCreated"), row.get("created_at"), row.get("dateSubmitted"))


def _latest_files(root: Path, names: set[str], *, limit: int) -> list[Path]:
    if not root.exists():
        return []
    files = [path for path in root.rglob("*") if path.is_file() and path.name in names]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return files[:limit]


def _parse_time(value: Any, *, end: bool) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            if len(text) == 10 and text[4] == "-" and text[7] == "-":
                dt = datetime.fromisoformat(text)
                if end:
                    dt += timedelta(days=1)
            else:
                dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        dt = dt.replace(tzinfo=local_tz)
    return dt.astimezone(timezone.utc)


def _weighted(values: list[tuple[float | None, float]]) -> float | None:
    available = [(value, weight) for value, weight in values if value is not None]
    if not available:
        return None
    total = sum(weight for _, weight in available)
    return round(sum(float(value) * weight for value, weight in available) / total, 6)


def _score_avg(rows: list[dict[str, Any]], key: str, *, target: float) -> float | None:
    avg = _mean(row.get(key) for row in rows)
    if avg is None:
        return None
    return round(max(0.0, min(avg / target, 1.0)), 6)


def _turnover_quality(rows: list[dict[str, Any]]) -> float | None:
    values = [_first_float(row.get("turnover")) for row in rows]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return round(sum(1 for value in values if 0.01 <= value <= 0.70) / len(values), 6)


def _duplicate_ratio(values: list[str]) -> float | None:
    if not values:
        return None
    return round((len(values) - len(set(values))) / len(values), 6)


def _ratio(numerator: float | int, denominator: float | int) -> float | None:
    if not denominator:
        return None
    return round(float(numerator) / float(denominator), 6)


def _mean(values) -> float | None:
    cleaned = [_first_float(value) for value in values]
    cleaned = [value for value in cleaned if value is not None]
    return round(statistics.mean(cleaned), 6) if cleaned else None


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is not None and str(value).strip() != "":
            return str(value)
    return None


def _first_float(*values: Any) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _read_csv(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    except OSError:
        return []


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
    path.write_text(text + ("\n" if text else ""), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys or ["empty"], extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fmt(value: Any) -> str:
    number = _first_float(value)
    if number is None:
        return ""
    return f"{number:.4f}".rstrip("0").rstrip(".")


def _md(value: Any) -> str:
    return str(value or "").replace("|", "\\|")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
