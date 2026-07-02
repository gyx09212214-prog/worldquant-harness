"""Aggregate WQ mining/submission efficiency metrics from local run artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.report_utils import matching_reason_count as _matching_reason_count
from worldquant_harness.wq_efficiency import annotate_candidate_identity, lifecycle_event

SELF_CORRELATION_REASONS = {
    "self_correlation",
    "self_correlation_fail",
    "self_correlation_high",
    "self_correlation_not_pass",
    "self_correlation_value_above_strict_cutoff",
}
TOO_SIMILAR_REASONS = {
    "too_similar_to_real_or_virtual_active",
    "too_similar_to_inventory",
    "high_similarity",
    "exact_active_duplicate",
    "exact_inventory_duplicate",
    "duplicate_or_active_expression",
}
SUCCESS_STATUSES = {"ACTIVE", "SUBMITTED"}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    current_run_dirs = tuple(_resolve_many(args.current_run_dirs))
    if not current_run_dirs:
        current_run_dirs = tuple(_dirs_from_roots(_resolve_many(_root_args(args))))
    current = _collect_group(
        name=args.current_name,
        run_dirs=current_run_dirs,
        miner_summaries=tuple(_resolve_many(args.current_miner_summaries)),
    )
    baseline = _collect_group(
        name=args.baseline_name,
        run_dirs=tuple(_baseline_dirs(args)),
        miner_summaries=tuple(_resolve_many(args.baseline_miner_summaries)),
        exclude_dirs=tuple(_resolve_many(args.current_run_dirs)),
    )
    report = {
        "ok": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "current": current,
        "baseline": baseline,
        "delta": _delta(current["metrics"], baseline["metrics"]),
    }
    output = _resolve(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    if args.markdown_output:
        markdown_output = _resolve(args.markdown_output)
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
        markdown_output.write_text(_render_markdown_report(report), encoding="utf-8")
    if args.events_output:
        events_output = _resolve(args.events_output)
        events_output.parent.mkdir(parents=True, exist_ok=True)
        _write_jsonl(events_output, [*current.get("events", []), *baseline.get("events", [])])
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize WQ live mining/submission efficiency")
    parser.add_argument("--current-run-dirs", nargs="*", default=[])
    parser.add_argument("--run-roots", nargs="*", default=[])
    parser.add_argument("--experiment-root", nargs="*", default=[])
    parser.add_argument("--agent-run-root", nargs="*", default=[])
    parser.add_argument("--current-miner-summaries", nargs="*", default=[])
    parser.add_argument("--current-name", default="current")
    parser.add_argument("--baseline-run-dirs", nargs="*", default=[])
    parser.add_argument("--baseline-roots", nargs="*", default=[])
    parser.add_argument("--baseline-miner-summaries", nargs="*", default=[])
    parser.add_argument("--baseline-name", default="baseline")
    parser.add_argument("--exclude-path-contains", nargs="*", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown-output")
    parser.add_argument("--events-output")
    return parser.parse_args(argv)


def _collect_group(
    *,
    name: str,
    run_dirs: tuple[Path, ...],
    miner_summaries: tuple[Path, ...],
    exclude_dirs: tuple[Path, ...] = (),
) -> dict[str, Any]:
    excluded = {_norm(path) for path in exclude_dirs}
    dirs = [_resolve(path) for path in run_dirs if _resolve(path).exists() and _norm(_resolve(path)) not in excluded]
    files = _collect_files(dirs)
    default_settings = _group_default_settings(files)
    simulation_rows = _annotate_rows(_dedupe_rows(
        _load_all_jsonl(files["simulation_results"]),
        ("simulation_id", "alpha_id", "candidate_key", "expression"),
    ), default_settings)
    check_rows = _annotate_rows(_dedupe_rows(
        _load_all_jsonl(files["check_results"]),
        ("alpha_id", "candidate_key", "api_check_status", "sc_value", "detail"),
    ), default_settings)
    submit_rows = _annotate_rows(_dedupe_rows(
        _load_submit_rows(files),
        ("alpha_id", "created_at", "final_status", "platform_status", "status", "detail"),
    ), default_settings)
    review_rows = _annotate_rows(_dedupe_rows(
        _load_all_jsonl(files["review_queue"]),
        ("simulation_id", "alpha_id", "candidate_rank", "tag", "expression", "triage_bucket"),
    ), default_settings)
    ready_rows = _annotate_rows(_ready_rows(files, check_rows), default_settings)
    rejected_rows = _annotate_rows(_dedupe_rows(
        _load_all_jsonl(files["presubmit_rejected"]),
        ("alpha_id", "expression", "presubmit_reject_reason", "reject_reason", "failure_kind"),
    ), default_settings)
    candidate_rows = _annotate_rows(_dedupe_rows(
        _load_all_jsonl(files["candidates"]),
        ("candidate_spec_id", "candidate_key", "tag", "expression"),
    ), default_settings)
    miner_rows = [_read_json(path) for path in miner_summaries if path.is_file()]
    miner_rows += [_read_json(path) for path in files["miner_summaries"] if path.is_file()]
    reject_counts = _rejection_counts(rejected_rows, check_rows, submit_rows, review_rows, miner_rows, files["loop_status"])
    submit_attempt_count = len(submit_rows)
    submit_success_rows = [row for row in submit_rows if _is_submit_success(row)]
    sim_count = _simulation_count(simulation_rows, files["summaries"])
    signature_summary = _field_signature_summary(candidate_rows or simulation_rows or ready_rows or rejected_rows)
    lifecycle = _build_lifecycle(
        candidate_rows=candidate_rows,
        simulation_rows=simulation_rows,
        review_rows=review_rows,
        ready_rows=ready_rows,
        rejected_rows=rejected_rows,
        submit_rows=submit_rows,
        run_name=name,
    )
    metrics = {
        "candidate_count": lifecycle["candidate_count"],
        "simulation_count": sim_count,
        "ready_count": len(_dedupe_ready(ready_rows)),
        "ready_per_100_simulations": _ratio(len(_dedupe_ready(ready_rows)) * 100.0, sim_count),
        "submitted_per_100_simulations": _ratio(submit_attempt_count * 100.0, sim_count),
        "active_per_100_simulations": _ratio(len(submit_success_rows) * 100.0, sim_count),
        "active_per_ready": _ratio(len(submit_success_rows), len(_dedupe_ready(ready_rows))),
        "active_per_submit_attempt": _ratio(len(submit_success_rows), submit_attempt_count),
        "simulations_per_active": _ratio(sim_count, len(submit_success_rows)),
        "total_rejection_count": sum(reject_counts.values()),
        "self_correlation_reject_count": _matching_reason_count(reject_counts, SELF_CORRELATION_REASONS),
        "self_correlation_reject_share": _ratio(
            _matching_reason_count(reject_counts, SELF_CORRELATION_REASONS),
            sum(reject_counts.values()),
        ),
        "too_similar_reject_count": _matching_reason_count(reject_counts, TOO_SIMILAR_REASONS),
        "too_similar_reject_share": _ratio(
            _matching_reason_count(reject_counts, TOO_SIMILAR_REASONS),
            sum(reject_counts.values()),
        ),
        "concentrated_weight_reject_count": reject_counts.get("CONCENTRATED_WEIGHT", 0),
        "concentrated_weight_reject_share": _ratio(
            reject_counts.get("CONCENTRATED_WEIGHT", 0),
            sum(reject_counts.values()),
        ),
        "duplicate_field_signature_count": signature_summary["duplicate_field_signature_count"],
        "field_signature_unique_count": signature_summary["field_signature_unique_count"],
        "field_signature_duplicate_ratio": _ratio(
            signature_summary["duplicate_field_signature_count"],
            signature_summary["field_signature_total_rows"],
        ),
        "setting_mismatch_count": _setting_mismatch_count(simulation_rows),
        "max_position_override_count": _setting_mismatch_count(simulation_rows, key="maxPosition"),
        "hypothesis_to_first_ready_seconds": _seconds_between(_group_start_time(files, dirs), _first_ready_time(ready_rows, submit_success_rows)),
        "real_submit_attempt_count": submit_attempt_count,
        "real_submit_success_count": len(submit_success_rows),
        "promote_submit_success_rate": _ratio(len(submit_success_rows), submit_attempt_count),
    }
    metrics["submit_efficiency_score"] = _submit_efficiency_score(metrics)
    return {
        "name": name,
        "run_dirs": [str(path) for path in dirs],
        "metrics": metrics,
        "funnel": lifecycle["funnel"],
        "leaderboards": lifecycle["leaderboards"],
        "events": lifecycle["events"],
        "reject_counts": dict(sorted(reject_counts.items())),
        "field_signature": signature_summary,
        "active_alpha_ids": sorted({str(row.get("alpha_id")) for row in submit_success_rows if row.get("alpha_id")}),
        "source_file_counts": {key: len(value) for key, value in files.items()},
    }


def _collect_files(run_dirs: list[Path]) -> dict[str, list[Path]]:
    files: dict[str, list[Path]] = {
        "simulation_results": [],
        "check_results": [],
        "submit_results": [],
        "single_submit_results": [],
        "review_queue": [],
        "presubmit_ready": [],
        "presubmit_rejected": [],
        "candidates": [],
        "summaries": [],
        "loop_status": [],
        "miner_summaries": [],
        "lifecycle_events": [],
        "manifests": [],
    }
    names = {
        "simulation_results.jsonl": "simulation_results",
        "check_results.jsonl": "check_results",
        "submit_results.jsonl": "submit_results",
        "submit_existing_results.jsonl": "submit_results",
        "submitted_accumulator.jsonl": "submit_results",
        "submit_result.json": "single_submit_results",
        "review_queue.jsonl": "review_queue",
        "presubmit_ready_sequential.jsonl": "presubmit_ready",
        "post_patch_ready_sequential.jsonl": "presubmit_ready",
        "presubmit_rejected.jsonl": "presubmit_rejected",
        "candidates.jsonl": "candidates",
        "candidate_specs.jsonl": "candidates",
        "candidate_pool.jsonl": "candidates",
        "summary.json": "summaries",
        "loop_status.json": "loop_status",
        "wq_research_miner_summary.json": "miner_summaries",
        "alpha_lifecycle_events.jsonl": "lifecycle_events",
        "manifest.json": "manifests",
    }
    for run_dir in run_dirs:
        sibling_specs = run_dir.parent / "candidate_specs.jsonl"
        if run_dir.name == "presubmit_run" and sibling_specs.is_file():
            files["candidates"].append(sibling_specs)
        for path in run_dir.rglob("*"):
            if path.is_file() and path.name in names:
                files[names[path.name]].append(path)
    return {key: _dedupe_paths(value) for key, value in files.items()}


def _baseline_dirs(args: argparse.Namespace) -> list[Path]:
    explicit = _resolve_many(args.baseline_run_dirs)
    found = _dirs_from_roots(_resolve_many(args.baseline_roots))
    return _filter_dirs([*explicit, *found], args.exclude_path_contains)


def _root_args(args: argparse.Namespace) -> list[str]:
    return [*args.run_roots, *args.experiment_root, *args.agent_run_root]


def _dirs_from_roots(roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    markers = {
        "simulation_results.jsonl",
        "check_results.jsonl",
        "submit_results.jsonl",
        "submit_existing_results.jsonl",
        "submitted_accumulator.jsonl",
        "submit_result.json",
        "presubmit_ready_sequential.jsonl",
        "alpha_lifecycle_events.jsonl",
        "manifest.json",
    }
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.name in markers:
                found.append(path.parent)
    return _dedupe_paths(found)


def _filter_dirs(paths: list[Path], exclude_path_contains: list[str]) -> list[Path]:
    blocked_text = tuple(str(value).lower() for value in exclude_path_contains)
    dirs = []
    for path in paths:
        text = str(path).lower()
        if any(token in text for token in blocked_text):
            continue
        dirs.append(path)
    return _dedupe_paths(dirs)


def _ready_rows(files: dict[str, list[Path]], check_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = _load_all_jsonl(files["presubmit_ready"])
    for row in check_rows:
        if _is_ready_check(row):
            rows.append(row)
    return rows


def _load_submit_rows(files: dict[str, list[Path]]) -> list[dict[str, Any]]:
    rows = _load_all_jsonl(files["submit_results"])
    for path in files["single_submit_results"]:
        row = _read_json(path)
        if row:
            payload = row.get("submit_result") if isinstance(row.get("submit_result"), dict) else row
            if "alpha_id" not in payload and row.get("alpha_id"):
                payload["alpha_id"] = row.get("alpha_id")
            rows.append(payload)
    return rows


def _simulation_count(simulation_rows: list[dict[str, Any]], summary_files: list[Path]) -> int:
    count = len(simulation_rows)
    if count > 0:
        return count
    summary_count = 0
    for path in summary_files:
        summary = _read_json(path)
        summary_count += int(
            _first_number(
                summary.get("simulated"),
                summary.get("total_simulations"),
                _nested(summary, "simulation", "simulated"),
                _nested(summary, "presubmit_loop", "total_simulations"),
                _nested(summary, "run_submit_loop", "total_simulations"),
                0,
            )
            or 0
        )
    return summary_count


def _rejection_counts(
    rejected_rows: list[dict[str, Any]],
    check_rows: list[dict[str, Any]],
    submit_rows: list[dict[str, Any]],
    review_rows: list[dict[str, Any]],
    miner_summaries: list[dict[str, Any]],
    loop_status_files: list[Path],
) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rejected_rows:
        counts[_row_reason(row)] += 1
    for row in check_rows:
        status = str(row.get("api_check_status") or "")
        if status and status != "api_check_readable":
            counts[status] += 1
    for row in submit_rows:
        if not _is_submit_success(row):
            reason = _row_reason(row)
            if reason == "unknown":
                reason = str(row.get("final_status") or row.get("platform_status") or "submit_rejected")
            counts[reason] += 1
    for row in review_rows:
        failed = row.get("failed_platform_checks") or []
        if failed:
            for check in failed:
                name = str((check or {}).get("name") or "PLATFORM_CHECK_FAIL").upper()
                counts[name] += 1
            continue
        triage = str(row.get("triage_bucket") or "")
        if triage in {"hard_fail", "near_miss_repair"}:
            reason = _row_reason(row)
            if reason != "unknown":
                counts[reason] += 1
    for summary in miner_summaries:
        for reason, count in ((summary.get("counts") or {}).get("screen_reject_reason") or {}).items():
            counts[str(reason)] += int(count or 0)
    for path in loop_status_files:
        loop_status = _read_json(path)
        for cycle in loop_status.get("cycles") or []:
            skip = cycle.get("candidate_skip") if isinstance(cycle, dict) else {}
            if not isinstance(skip, dict):
                skip = {}
            for reason, count in (skip.get("skip_reasons") or {}).items():
                counts[str(reason)] += int(count or 0)
    return counts


def _annotate_rows(rows: list[dict[str, Any]], default_settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [annotate_candidate_identity(row, default_settings) for row in rows]


def _group_default_settings(files: dict[str, list[Path]]) -> dict[str, Any]:
    for path in files.get("manifests", []):
        manifest = _read_json(path)
        config = manifest.get("config") if isinstance(manifest.get("config"), dict) else {}
        if config:
            return {
                "account": config.get("account"),
                "region": config.get("region"),
                "universe": config.get("universe"),
                "delay": config.get("delay"),
                "decay": config.get("decay"),
                "neutralization": config.get("neutralization"),
                "truncation": config.get("truncation"),
            }
    return {}


def _build_lifecycle(
    *,
    candidate_rows: list[dict[str, Any]],
    simulation_rows: list[dict[str, Any]],
    review_rows: list[dict[str, Any]],
    ready_rows: list[dict[str, Any]],
    rejected_rows: list[dict[str, Any]],
    submit_rows: list[dict[str, Any]],
    run_name: str,
) -> dict[str, Any]:
    records: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []

    for row in candidate_rows:
        _merge_lifecycle_record(records, row, candidate=True)
        events.append(lifecycle_event("candidate_created", row, run_id=run_name))
    for row in simulation_rows:
        _merge_lifecycle_record(records, row, simulated=True)
        events.append(lifecycle_event("simulation_finished", row, run_id=run_name))
    for row in review_rows:
        _merge_lifecycle_record(records, row, reviewed=True)
        events.append(lifecycle_event("review_finished", row, run_id=run_name))
    for row in ready_rows:
        _merge_lifecycle_record(records, row, ready=True)
        events.append(lifecycle_event("candidate_ready", row, run_id=run_name))
    for row in rejected_rows:
        _merge_lifecycle_record(records, row, rejected=True, reason=_row_reason(row))
        events.append(lifecycle_event("candidate_rejected", row, run_id=run_name))
    for row in submit_rows:
        success = _is_submit_success(row)
        _merge_lifecycle_record(records, row, submitted=True, active=success, reason=None if success else _row_reason(row))
        events.append(lifecycle_event("submit_attempted", row, run_id=run_name))
        events.append(lifecycle_event("submit_succeeded" if success else "submit_failed", row, run_id=run_name))

    values = list(records.values())
    funnel = {
        "candidates": sum(1 for row in values if row.get("candidate")),
        "simulated": sum(1 for row in values if row.get("simulated")),
        "reviewed": sum(1 for row in values if row.get("reviewed")),
        "ready": sum(1 for row in values if row.get("ready")),
        "rejected": sum(1 for row in values if row.get("rejected")),
        "submitted": sum(1 for row in values if row.get("submitted")),
        "active": sum(1 for row in values if row.get("active")),
    }
    return {
        "candidate_count": len(values),
        "funnel": funnel,
        "leaderboards": {
            "source_family": _leaderboard(values, "source_family"),
            "field_signature": _leaderboard(values, "field_signature"),
            "settings": _leaderboard(values, "settings_label"),
        },
        "events": _dedupe_events(events),
    }


def _merge_lifecycle_record(
    records: dict[str, dict[str, Any]],
    row: dict[str, Any],
    *,
    candidate: bool = False,
    simulated: bool = False,
    reviewed: bool = False,
    ready: bool = False,
    rejected: bool = False,
    submitted: bool = False,
    active: bool = False,
    reason: str | None = None,
) -> None:
    uid = str(row.get("candidate_uid") or row.get("alpha_id") or row.get("expression") or "")
    if not uid:
        return
    record = records.setdefault(
        uid,
        {
            "candidate_uid": uid,
            "expression": row.get("expression"),
            "alpha_id": row.get("alpha_id"),
            "source_family": row.get("source_family") or "unknown",
            "field_signature": row.get("field_signature") or "unknown",
            "settings_hash": row.get("settings_hash"),
            "settings_label": _settings_label(row.get("efficiency_settings") or {}),
            "reject_reasons": [],
        },
    )
    for key, value in (
        ("candidate", candidate),
        ("simulated", simulated),
        ("reviewed", reviewed),
        ("ready", ready),
        ("rejected", rejected),
        ("submitted", submitted),
        ("active", active),
    ):
        if value:
            record[key] = True
    for key in ("expression", "alpha_id", "source_family", "field_signature", "settings_hash"):
        if row.get(key) and not record.get(key):
            record[key] = row.get(key)
    if reason and reason != "unknown" and reason not in record["reject_reasons"]:
        record["reject_reasons"].append(reason)


def _leaderboard(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        label = str(row.get(key) or "unknown")
        grouped.setdefault(label, []).append(row)
    rows = []
    for label, items in grouped.items():
        simulated = sum(1 for item in items if item.get("simulated"))
        ready = sum(1 for item in items if item.get("ready"))
        submitted = sum(1 for item in items if item.get("submitted"))
        active = sum(1 for item in items if item.get("active"))
        rows.append({
            key: label,
            "candidate_count": len(items),
            "simulation_count": simulated,
            "ready_count": ready,
            "rejected_count": sum(1 for item in items if item.get("rejected")),
            "submit_attempt_count": submitted,
            "active_count": active,
            "ready_per_100_simulations": _ratio(ready * 100.0, simulated),
            "active_per_100_simulations": _ratio(active * 100.0, simulated),
            "active_per_ready": _ratio(active, ready),
        })
    rows.sort(
        key=lambda row: (
            _safe_float(row.get("active_per_100_simulations")) or 0.0,
            _safe_float(row.get("ready_per_100_simulations")) or 0.0,
            row.get("simulation_count") or 0,
        ),
        reverse=True,
    )
    return rows[:20]


def _settings_label(settings: dict[str, Any]) -> str:
    if not settings:
        return "unknown"
    keys = ("region", "universe", "delay", "decay", "neutralization", "truncation", "maxTrade", "maxPosition")
    return "|".join(f"{key}={settings.get(key)}" for key in keys if settings.get(key) is not None)


def _dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    seen = set()
    for event in events:
        key = json.dumps(
            {
                "event_type": event.get("event_type"),
                "candidate_uid": event.get("candidate_uid"),
                "alpha_id": event.get("alpha_id"),
                "reason": event.get("reason"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


def _submit_efficiency_score(metrics: dict[str, Any]) -> float | None:
    if not metrics.get("real_submit_attempt_count"):
        return None
    active_yield = min((_safe_float(metrics.get("active_per_100_simulations")) or 0.0) / 5.0, 1.0)
    active_ready = _safe_float(metrics.get("active_per_ready")) or 0.0
    self_quality = 1.0 - min(_safe_float(metrics.get("self_correlation_reject_share")) or 0.0, 1.0)
    similar_quality = 1.0 - min(_safe_float(metrics.get("too_similar_reject_share")) or 0.0, 1.0)
    diversity = 1.0 - min(_safe_float(metrics.get("field_signature_duplicate_ratio")) or 0.0, 1.0)
    speed_seconds = _safe_float(metrics.get("hypothesis_to_first_ready_seconds"))
    speed = None if speed_seconds is None else max(0.0, 1.0 - min(speed_seconds, 86400.0) / 86400.0)
    weighted = [
        (active_yield, 0.35),
        (active_ready, 0.20),
        (self_quality, 0.15),
        (similar_quality, 0.15),
        (diversity, 0.10),
        (speed, 0.05),
    ]
    available = [(value, weight) for value, weight in weighted if value is not None]
    if not available:
        return None
    total_weight = sum(weight for _, weight in available)
    return round(sum(value * weight for value, weight in available) / total_weight, 6)


def _field_signature_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    signatures = []
    for row in rows:
        signature = str(row.get("field_signature") or "")
        expression = str(row.get("expression") or "")
        if not signature and expression:
            signature = str(annotate_candidate_identity(row).get("field_signature") or "")
        if signature:
            signatures.append(signature)
    counts = Counter(signatures)
    duplicate_count = sum(max(0, count - 1) for count in counts.values())
    return {
        "field_signature_total_rows": len(signatures),
        "field_signature_unique_count": len(counts),
        "duplicate_field_signature_count": duplicate_count,
        "top_duplicate_field_signatures": [
            {"field_signature": signature, "count": count, "duplicate_count": count - 1}
            for signature, count in counts.most_common(10)
            if count > 1
        ],
    }


def _setting_mismatch_count(rows: list[dict[str, Any]], *, key: str | None = None) -> int:
    total = 0
    for row in rows:
        mismatches = row.get("simulation_setting_mismatches") or []
        if key is None:
            total += len(mismatches)
            continue
        for mismatch in mismatches:
            if str((mismatch or {}).get("key") or "") == key:
                total += 1
    return total


def _is_ready_check(row: dict[str, Any]) -> bool:
    if row.get("failed_platform_checks"):
        return False
    if str(row.get("api_check_status") or "") != "api_check_readable":
        return False
    if str(row.get("sc_result") or "").upper() != "PASS":
        return False
    if str(row.get("prod_corr_result") or "").upper() == "FAIL":
        return False
    return True


def _is_submit_success(row: dict[str, Any]) -> bool:
    status = str(row.get("final_status") or row.get("platform_status") or row.get("status") or "").upper()
    return bool(row.get("ok")) and status in SUCCESS_STATUSES


def _row_reason(row: dict[str, Any]) -> str:
    return str(
        row.get("presubmit_reject_reason")
        or row.get("candidate_skip_reason")
        or row.get("reject_reason")
        or row.get("api_check_status")
        or row.get("failure_kind")
        or row.get("final_status")
        or "unknown"
    )


def _dedupe_ready(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    seen = set()
    for row in rows:
        key = str(row.get("alpha_id") or row.get("candidate_key") or row.get("expression") or len(out))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _dedupe_rows(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    out = []
    seen = set()
    for row in rows:
        key_parts = []
        for field in fields:
            value = row.get(field)
            if value not in (None, ""):
                key_parts.append(f"{field}={value}")
        if not key_parts:
            key_parts.append(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
        key = "|".join(key_parts)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _group_start_time(files: dict[str, list[Path]], run_dirs: list[Path]) -> datetime | None:
    values: list[datetime] = []
    for file_list in files.values():
        for path in file_list:
            parsed = _parse_time_from_path(path)
            if parsed:
                values.append(parsed)
    for run_dir in run_dirs:
        parsed = _parse_time_from_path(run_dir)
        if parsed:
            values.append(parsed)
    return min(values) if values else None


def _first_ready_time(ready_rows: list[dict[str, Any]], submit_success_rows: list[dict[str, Any]]) -> datetime | None:
    values = [_parse_time(str(row.get("created_at") or "")) for row in [*ready_rows, *submit_success_rows]]
    values = [value for value in values if value is not None]
    return min(values) if values else None


def _seconds_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def _delta(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "ready_per_100_simulations",
        "self_correlation_reject_share",
        "too_similar_reject_share",
        "concentrated_weight_reject_share",
        "duplicate_field_signature_count",
        "setting_mismatch_count",
        "max_position_override_count",
        "promote_submit_success_rate",
        "real_submit_success_count",
        "real_submit_attempt_count",
    ]
    out = {}
    for key in keys:
        cur = _safe_float(current.get(key))
        base = _safe_float(baseline.get(key))
        out[key] = {
            "current": current.get(key),
            "baseline": baseline.get(key),
            "absolute_delta": None if cur is None or base is None else round(cur - base, 6),
            "relative_delta": None if cur is None or base in (None, 0.0) else round((cur - base) / base, 6),
        }
    return out


def _load_all_jsonl(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        rows.extend(_read_jsonl(path))
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    text = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
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


def _render_markdown_report(report: dict[str, Any]) -> str:
    current = report.get("current") or {}
    baseline = report.get("baseline") or {}
    lines = [
        "# WQ Alpha Submit Efficiency",
        "",
        f"- Created at: {report.get('created_at')}",
        f"- Current group: {current.get('name')}",
        f"- Baseline group: {baseline.get('name')}",
        "",
        "## Current Funnel",
        "",
        _funnel_table(current.get("funnel") or {}),
        "",
        "## Current Metrics",
        "",
        _metrics_table(current.get("metrics") or {}),
        "",
        "## Reject Counts",
        "",
        _counter_table(current.get("reject_counts") or {}),
        "",
        "## Source Family Leaderboard",
        "",
        _leaderboard_table((current.get("leaderboards") or {}).get("source_family") or [], "source_family"),
        "",
        "## Field Signature Leaderboard",
        "",
        _leaderboard_table((current.get("leaderboards") or {}).get("field_signature") or [], "field_signature"),
        "",
        "## Settings Leaderboard",
        "",
        _leaderboard_table((current.get("leaderboards") or {}).get("settings") or [], "settings_label", display_label="settings"),
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _funnel_table(funnel: dict[str, Any]) -> str:
    keys = ("candidates", "simulated", "reviewed", "ready", "rejected", "submitted", "active")
    lines = ["| Stage | Count |", "| --- | ---: |"]
    for key in keys:
        lines.append(f"| {key} | {funnel.get(key, 0)} |")
    return "\n".join(lines)


def _metrics_table(metrics: dict[str, Any]) -> str:
    keys = (
        "candidate_count",
        "simulation_count",
        "ready_count",
        "ready_per_100_simulations",
        "real_submit_attempt_count",
        "real_submit_success_count",
        "active_per_100_simulations",
        "active_per_ready",
        "active_per_submit_attempt",
        "simulations_per_active",
        "self_correlation_reject_share",
        "too_similar_reject_share",
        "submit_efficiency_score",
    )
    lines = ["| Metric | Value |", "| --- | ---: |"]
    for key in keys:
        lines.append(f"| {key} | {_format_value(metrics.get(key))} |")
    return "\n".join(lines)


def _counter_table(counts: dict[str, Any]) -> str:
    if not counts:
        return "_No reject counts._"
    lines = ["| Reason | Count |", "| --- | ---: |"]
    for key, value in sorted(counts.items(), key=lambda item: int(item[1] or 0), reverse=True)[:30]:
        lines.append(f"| {key} | {value} |")
    return "\n".join(lines)


def _leaderboard_table(rows: list[dict[str, Any]], label_key: str, *, display_label: str | None = None) -> str:
    if not rows:
        return "_No leaderboard rows._"
    header = display_label or label_key
    lines = [
        f"| {header} | candidates | sims | ready | rejected | submit | active | ready/100 sim | active/100 sim | active/ready |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows[:15]:
        lines.append(
            f"| {_md_escape(row.get(label_key))} | {row.get('candidate_count')} | {row.get('simulation_count')} | "
            f"{row.get('ready_count')} | {row.get('rejected_count')} | {row.get('submit_attempt_count')} | "
            f"{row.get('active_count')} | {_format_value(row.get('ready_per_100_simulations'))} | "
            f"{_format_value(row.get('active_per_100_simulations'))} | {_format_value(row.get('active_per_ready'))} |"
        )
    return "\n".join(lines)


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _md_escape(value: Any) -> str:
    return str(value).replace("|", "\\|")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_many(values: list[str]) -> list[Path]:
    return [_resolve(value) for value in values if value]


def _resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    out = []
    seen = set()
    for path in paths:
        key = _norm(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _norm(path: Path) -> str:
    try:
        return str(path.resolve()).lower()
    except OSError:
        return str(path).lower()


def _parse_time_from_path(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def _parse_time(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _ratio(numerator: float | int, denominator: float | int) -> float | None:
    try:
        den = float(denominator)
        if den == 0.0:
            return None
        return round(float(numerator) / den, 6)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_number(*values: Any) -> float | None:
    for value in values:
        parsed = _safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


if __name__ == "__main__":
    raise SystemExit(main())
