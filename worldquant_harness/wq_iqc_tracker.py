"""IQC Stage 2 submission tracker.

The tracker is intentionally artifact-first: workflow runs remain the source of
truth for real submissions, while the WQ ledger and latest active inventory are
used to enrich records and keep duplicate discovery blocked.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .expression_parser import extract_components, normalize_expression

DEFAULT_ROUND_ID = "iqc_stage2_after_RRrQo83z_20260604"
DEFAULT_ROUND_START_AFTER_ALPHA = "RRrQo83z"
DEFAULT_RUN_ROOT = Path("reports/wq_agent_runs")
DEFAULT_JSONL_OUTPUT = Path("reports/wq_iqc_stage2_submission_tracker.jsonl")
DEFAULT_MARKDOWN_OUTPUT = Path("reports/wq_iqc_stage2_submission_tracker.md")
DEFAULT_DB_PATH = Path("worldquant_harness.db")
MPX_SIGNATURE_FIELDS = {
    "multi_factor_acceleration_score_derivative",
    "fifty_to_two_hundred_day_price_ratio",
    "returns",
    "volume",
    "vwap",
}


@dataclass(frozen=True)
class TrackerConfig:
    run_root: Path = DEFAULT_RUN_ROOT
    db_path: Path = DEFAULT_DB_PATH
    jsonl_output: Path = DEFAULT_JSONL_OUTPUT
    markdown_output: Path = DEFAULT_MARKDOWN_OUTPUT
    round_id: str = DEFAULT_ROUND_ID
    round_start_after_alpha: str = DEFAULT_ROUND_START_AFTER_ALPHA
    target_count: int = 10


def build_tracker(config: TrackerConfig) -> dict[str, Any]:
    run_records = collect_submit_run_records(config.run_root)
    active_rows = load_latest_active_inventory(config.run_root)
    ledger_rows = load_ledger_active_rows(config.db_path)
    records = merge_records(run_records, active_rows, ledger_rows)
    mark_round_records(records, config.round_start_after_alpha)
    novelty = build_novelty_audit(records, active_rows)
    records.sort(key=lambda row: (row.get("submitted_at") or "", row.get("alpha_id") or ""))
    counted = [row for row in records if row.get("counted_for_round")]
    summary = {
        "round_id": config.round_id,
        "target_count": config.target_count,
        "round_start_after_alpha": config.round_start_after_alpha,
        "new_active_count": len(counted),
        "remaining": max(config.target_count - len(counted), 0),
        "latest_counted_alpha": counted[-1]["alpha_id"] if counted else None,
        "records": records,
        "novelty": novelty,
    }
    return summary


def write_tracker_outputs(summary: dict[str, Any], config: TrackerConfig) -> None:
    _write_jsonl(config.jsonl_output, summary["records"])
    _write_markdown(config.markdown_output, summary)


def collect_submit_run_records(run_root: Path) -> list[dict[str, Any]]:
    if not run_root.exists():
        return []
    records: list[dict[str, Any]] = []
    run_dirs = sorted((p for p in run_root.iterdir() if p.is_dir()), key=lambda p: (p.stat().st_mtime, p.name))
    for run_index, run_dir in enumerate(run_dirs):
        summary = _read_json(run_dir / "summary.json")
        submission = summary.get("submission") if isinstance(summary.get("submission"), dict) else {}
        result = submission.get("result") if isinstance(submission.get("result"), dict) else {}
        result_rows = result.get("results") if isinstance(result.get("results"), dict) else {}
        if not result_rows:
            continue
        review_by_alpha = _review_rows_by_alpha(run_dir / "review_queue.jsonl")
        for alpha_id, submit_entry in result_rows.items():
            if not _submission_entry_succeeded(submit_entry):
                continue
            source = review_by_alpha.get(str(alpha_id), {})
            record = build_record(
                alpha_id=str(alpha_id),
                source=source,
                submit_entry=submit_entry,
                run_id=run_dir.name,
                submit_run_dir=run_dir,
                submitted_at=_mtime_iso(run_dir / "summary.json"),
                record_source="submit_run",
            )
            record["run_index"] = run_index
            records.append(record)
    return records


def load_latest_active_inventory(run_root: Path) -> list[dict[str, Any]]:
    candidates = sorted(
        run_root.glob("*/active_inventory.json") if run_root.exists() else [],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        payload = _read_json(path)
        rows = payload.get("active") if isinstance(payload.get("active"), list) else []
        if rows:
            return [row for row in rows if str(row.get("status") or row.get("platform_status") or "").upper() in {"ACTIVE", "SUBMITTED"}]
    return []


def load_ledger_active_rows(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            select
                alpha_id, expression, source_tag, source_family, source_run_id,
                lifecycle_status, platform_status, region, universe, delay, decay,
                neutralization, truncation, sharpe, fitness, returns, turnover,
                self_correlation_result, self_correlation_value,
                prod_correlation_result, prod_correlation_value, updated_at
            from wq_alpha_experiments
            where lifecycle_status in ('active', 'submitted')
               or upper(coalesce(platform_status, '')) in ('ACTIVE', 'SUBMITTED')
            """
        ).fetchall()
        return [dict(row) for row in rows if row["alpha_id"]]
    except Exception:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def merge_records(
    submit_records: list[dict[str, Any]],
    active_rows: list[dict[str, Any]],
    ledger_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    active_by_alpha = {str(row.get("alpha_id") or ""): row for row in active_rows if row.get("alpha_id")}
    ledger_by_alpha = {str(row.get("alpha_id") or ""): row for row in ledger_rows if row.get("alpha_id")}
    merged: dict[str, dict[str, Any]] = {}
    for record in submit_records:
        alpha_id = str(record.get("alpha_id") or "")
        if not alpha_id:
            continue
        enriched = enrich_record(record, active_by_alpha.get(alpha_id), ledger_by_alpha.get(alpha_id))
        previous = merged.get(alpha_id)
        if previous is None or str(enriched.get("submitted_at") or "") >= str(previous.get("submitted_at") or ""):
            merged[alpha_id] = enriched
    for alpha_id, row in active_by_alpha.items():
        if alpha_id in merged:
            continue
        merged[alpha_id] = enrich_record(
            build_record(
                alpha_id=alpha_id,
                source=row,
                submit_entry={},
                run_id=str(row.get("source_run_id") or row.get("run_id") or ""),
                submit_run_dir=None,
                submitted_at=str(row.get("dateCreated") or row.get("updated_at") or ""),
                record_source="active_inventory",
            ),
            row,
            ledger_by_alpha.get(alpha_id),
        )
    return list(merged.values())


def build_record(
    *,
    alpha_id: str,
    source: dict[str, Any],
    submit_entry: dict[str, Any],
    run_id: str,
    submit_run_dir: Path | None,
    submitted_at: str,
    record_source: str,
) -> dict[str, Any]:
    result = source.get("result") if isinstance(source.get("result"), dict) else {}
    settings = result.get("settings") if isinstance(result.get("settings"), dict) else {}
    expression = str(source.get("expression") or result.get("expression") or "")
    fields = source.get("source_fields")
    if not isinstance(fields, list):
        fields = sorted(str(field) for field in extract_components(expression).get("fields", []))
    review_checks = submit_entry.get("review_checks") if isinstance(submit_entry.get("review_checks"), dict) else {}
    self_corr = _first_dict(source.get("self_correlation"), review_checks.get("self_correlation"))
    prod_corr = _first_dict(source.get("prod_correlation"), review_checks.get("prod_correlation"))
    return {
        "round_id": DEFAULT_ROUND_ID,
        "alpha_id": alpha_id,
        "platform_status": str(submit_entry.get("platform_status") or submit_entry.get("final_status") or source.get("platform_status") or source.get("status") or "").upper() or None,
        "counted_for_round": False,
        "submitted_at": submitted_at,
        "tag": source.get("tag") or source.get("source_tag"),
        "expression": expression,
        "expression_normalized": normalize_expression(expression) if expression else "",
        "source_fields": fields,
        "field_signature": field_signature(expression),
        "source_family": source.get("source_family") or (source.get("candidate_meta") or {}).get("source_family"),
        "region": settings.get("region") or source.get("region"),
        "universe": settings.get("universe") or source.get("universe"),
        "delay": settings.get("delay") if settings.get("delay") is not None else source.get("delay"),
        "decay": settings.get("decay") if settings.get("decay") is not None else source.get("decay"),
        "neutralization": settings.get("neutralization") or source.get("neutralization"),
        "truncation": settings.get("truncation") if settings.get("truncation") is not None else source.get("truncation"),
        "sharpe": _first(source.get("sharpe"), result.get("wq_brain", {}).get("wq_sharpe") if isinstance(result.get("wq_brain"), dict) else None),
        "fitness": _first(source.get("fitness"), result.get("wq_brain", {}).get("wq_fitness") if isinstance(result.get("wq_brain"), dict) else None),
        "turnover": _first(source.get("turnover"), result.get("wq_brain", {}).get("wq_turnover") if isinstance(result.get("wq_brain"), dict) else None),
        "returns": _first(source.get("returns"), result.get("wq_brain", {}).get("wq_returns") if isinstance(result.get("wq_brain"), dict) else None),
        "self_correlation": self_corr,
        "prod_correlation": prod_corr,
        "run_id": run_id,
        "submit_run_dir": str(submit_run_dir) if submit_run_dir else None,
        "record_source": record_source,
    }


def enrich_record(record: dict[str, Any], active_row: dict[str, Any] | None, ledger_row: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(record)
    if active_row:
        out["platform_status"] = str(active_row.get("status") or active_row.get("platform_status") or out.get("platform_status") or "").upper()
        for key in ("expression", "tag", "region", "universe", "delay", "decay", "neutralization", "truncation"):
            if out.get(key) in (None, "") and active_row.get(key) not in (None, ""):
                out[key] = active_row.get(key)
    if ledger_row:
        out["ledger_lifecycle_status"] = ledger_row.get("lifecycle_status")
        out["ledger_platform_status"] = ledger_row.get("platform_status")
        for key in ("expression", "source_family", "region", "universe", "delay", "decay", "neutralization", "truncation", "sharpe", "fitness", "returns", "turnover"):
            if out.get(key) in (None, "") and ledger_row.get(key) not in (None, ""):
                out[key] = ledger_row.get(key)
        if not out.get("tag"):
            out["tag"] = ledger_row.get("source_tag")
        if not out.get("self_correlation") and ledger_row.get("self_correlation_result"):
            out["self_correlation"] = {
                "name": "SELF_CORRELATION",
                "result": ledger_row.get("self_correlation_result"),
                "value": ledger_row.get("self_correlation_value"),
            }
        if not out.get("prod_correlation") and ledger_row.get("prod_correlation_result"):
            out["prod_correlation"] = {
                "name": "PROD_CORRELATION",
                "result": ledger_row.get("prod_correlation_result"),
                "value": ledger_row.get("prod_correlation_value"),
            }
    expression = str(out.get("expression") or "")
    out["expression_normalized"] = normalize_expression(expression) if expression else ""
    if not out.get("source_fields"):
        out["source_fields"] = sorted(str(field) for field in extract_components(expression).get("fields", []))
    out["field_signature"] = field_signature(expression)
    return out


def mark_round_records(records: list[dict[str, Any]], round_start_after_alpha: str) -> None:
    anchor_ts = ""
    anchor_index = -1
    for index, record in enumerate(records):
        if record.get("alpha_id") == round_start_after_alpha:
            ts = str(record.get("submitted_at") or "")
            if ts >= anchor_ts:
                anchor_ts = ts
                anchor_index = index
    for index, record in enumerate(records):
        active = str(record.get("platform_status") or "").upper() == "ACTIVE"
        submitted_at = str(record.get("submitted_at") or "")
        after_anchor = False
        if anchor_index >= 0:
            after_anchor = submitted_at > anchor_ts or (submitted_at == anchor_ts and index > anchor_index)
        record["counted_for_round"] = active and after_anchor


def build_novelty_audit(records: list[dict[str, Any]], active_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = _dedupe_rows_for_audit(records + active_rows)
    expressions = [str(row.get("expression") or "") for row in rows if row.get("expression")]
    field_counts: Counter[str] = Counter()
    operator_counts: Counter[str] = Counter()
    signature_counts: Counter[str] = Counter()
    source_family_counts: Counter[str] = Counter()
    for row in rows:
        expression = str(row.get("expression") or "")
        if not expression:
            continue
        components = extract_components(expression)
        fields = sorted(str(field) for field in components.get("fields", []))
        operators = sorted(str(op) for op in components.get("operators", []))
        field_counts.update(fields)
        operator_counts.update(operators)
        signature = field_signature(expression)
        if signature:
            signature_counts[signature] += 1
        family = row.get("source_family")
        if family:
            source_family_counts[str(family)] += 1
    mpx_matches = [
        row.get("alpha_id")
        for row in rows
        if MPX_SIGNATURE_FIELDS.issubset(set(str(field) for field in (row.get("source_fields") or extract_components(str(row.get("expression") or "")).get("fields", []))))
    ]
    return {
        "active_expression_count": len(expressions),
        "top_fields": dict(field_counts.most_common(20)),
        "top_operators": dict(operator_counts.most_common(15)),
        "top_field_signatures": dict(signature_counts.most_common(10)),
        "top_source_families": dict(source_family_counts.most_common(10)),
        "strict_ledger_similarity_cutoff": 0.62,
        "strict_field_signature_capacity": 1,
        "strict_source_family_capacity": 1,
        "mpx_signature_fields": sorted(MPX_SIGNATURE_FIELDS),
        "mpx_signature_active_alpha_ids": [alpha_id for alpha_id in mpx_matches if alpha_id],
    }


def _dedupe_rows_for_audit(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        expression = str(row.get("expression") or "")
        if not expression:
            continue
        key = str(row.get("alpha_id") or "") or normalize_expression(expression)
        previous = deduped.get(key)
        if previous is None:
            deduped[key] = row
            continue
        if not previous.get("source_fields") and row.get("source_fields"):
            deduped[key] = row
    return list(deduped.values())


def field_signature(expression: str) -> str:
    fields = sorted(str(field) for field in extract_components(expression or "").get("fields", []))
    return "|".join(fields)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh the IQC Stage 2 submission tracker")
    parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--jsonl-output", default=str(DEFAULT_JSONL_OUTPUT))
    parser.add_argument("--markdown-output", default=str(DEFAULT_MARKDOWN_OUTPUT))
    parser.add_argument("--round-id", default=DEFAULT_ROUND_ID)
    parser.add_argument("--round-start-after-alpha", default=DEFAULT_ROUND_START_AFTER_ALPHA)
    parser.add_argument("--target-count", type=int, default=10)
    args = parser.parse_args(argv)
    config = TrackerConfig(
        run_root=Path(args.run_root),
        db_path=Path(args.db_path),
        jsonl_output=Path(args.jsonl_output),
        markdown_output=Path(args.markdown_output),
        round_id=args.round_id,
        round_start_after_alpha=args.round_start_after_alpha,
        target_count=args.target_count,
    )
    summary = build_tracker(config)
    write_tracker_outputs(summary, config)
    print(json.dumps({k: v for k, v in summary.items() if k != "records"}, ensure_ascii=False, indent=2, default=str))
    return 0


def _review_rows_by_alpha(path: Path) -> dict[str, dict[str, Any]]:
    rows = {}
    for row in _read_jsonl(path):
        alpha_id = str(row.get("alpha_id") or "")
        if alpha_id:
            rows[alpha_id] = row
    return rows


def _submission_entry_succeeded(entry: dict[str, Any]) -> bool:
    if bool(entry.get("ok")):
        return True
    status = str(entry.get("final_status") or entry.get("platform_status") or entry.get("status") or "").upper()
    return status in {"ACTIVE", "SUBMITTED"}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(row, ensure_ascii=False, default=str, sort_keys=True) for row in rows)
    path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")


def _write_markdown(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = summary.get("records") or []
    counted = [row for row in records if row.get("counted_for_round")]
    novelty = summary.get("novelty") or {}
    lines = [
        "# WQ IQC Stage 2 Submission Tracker",
        "",
        f"- Round: {summary.get('round_id')}",
        f"- Target: {summary.get('new_active_count')}/{summary.get('target_count')}",
        f"- Remaining: {summary.get('remaining')}",
        f"- Anchor alpha: {summary.get('round_start_after_alpha')}",
        "",
        "## Counted Active Submissions",
        "",
        "| # | alpha_id | tag | sharpe | fitness | turnover | neutralization | fields |",
        "|---:|---|---|---:|---:|---:|---|---|",
    ]
    for index, row in enumerate(counted, start=1):
        fields = ",".join((row.get("source_fields") or [])[:8])
        lines.append(
            f"| {index} | {row.get('alpha_id')} | {_md(row.get('tag'))} | {_fmt(row.get('sharpe'))} | "
            f"{_fmt(row.get('fitness'))} | {_fmt(row.get('turnover'))} | {_md(row.get('neutralization'))} | {_md(fields)} |"
        )
    if not counted:
        lines.append("|  |  |  |  |  |  |  |  |")
    lines.extend([
        "",
        "## Novelty Baseline",
        "",
        f"- Strict ledger similarity cutoff: {novelty.get('strict_ledger_similarity_cutoff')}",
        f"- Field signature capacity: {novelty.get('strict_field_signature_capacity')}",
        f"- Source family capacity: {novelty.get('strict_source_family_capacity')}",
        f"- MPx signature active alpha IDs: {', '.join(novelty.get('mpx_signature_active_alpha_ids') or [])}",
        f"- Top fields: {_md(', '.join(f'{k}:{v}' for k, v in list((novelty.get('top_fields') or {}).items())[:12]))}",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def _mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _first_dict(*values: Any) -> dict[str, Any] | None:
    for value in values:
        if isinstance(value, dict) and value:
            return value
    return None


def _fmt(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.4g}"
    except Exception:
        return str(value)


def _md(value: Any) -> str:
    return str(value or "").replace("|", "/")


if __name__ == "__main__":
    raise SystemExit(main())
