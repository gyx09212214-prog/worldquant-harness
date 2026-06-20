"""Review weak ACTIVE/SUBMITTED WorldQuant alphas without model calls or submit."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .expression_parser import extract_components, normalize_expression
from .wq_auto_mining import load_dotenv
from .wq_brain_client import SUBMIT_THRESHOLDS, get_client, is_configured
from .wq_brain_service import run_check_submissions, run_list_alphas, submit_threshold_checks


ROOT = Path(__file__).resolve().parents[1]

ACTIVE_STATUSES = {"ACTIVE", "SUBMITTED"}
WEAK_MEMORY_KIND = "weak_active_constraint"


@dataclass(frozen=True)
class WQActiveWeakReviewConfig:
    output_dir: Path
    platform_file: Path | None = None
    account: str = "primary"
    platform_sync_limit: int = 2000
    max_checks: int = 30
    check_chunk_size: int = 25
    weak_score_cutoff: float = 4.0
    bottom_quantile: float = 0.30


def run_active_weak_review(
    config: WQActiveWeakReviewConfig,
    *,
    dependencies: dict[str, Any] | None = None,
) -> dict:
    """Build a weak ACTIVE/SUBMITTED review packet using only local logic and WQ read APIs."""

    load_dotenv(ROOT)
    dependencies = dependencies or {}
    output_dir = _resolve_path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    platform_rows = _load_platform_rows(config, dependencies=dependencies)
    platform_output = output_dir / "platform_alphas.jsonl"
    _write_jsonl(platform_output, platform_rows)

    active_rows = _active_rows(platform_rows)
    initial_scored = score_active_rows(active_rows, checks_by_id={}, config=config)
    initial_weak = select_weak_rows(initial_scored, config=config)

    checks_by_id: dict[str, dict] = {}
    if config.max_checks > 0 and initial_weak:
        check_ids = [
            str(row.get("alpha_id") or "")
            for row in sorted(initial_weak, key=_weak_sort_key)
            if row.get("alpha_id")
        ][: config.max_checks]
        checks_by_id = _check_alpha_ids(check_ids, config=config, dependencies=dependencies)

    scored_rows = score_active_rows(active_rows, checks_by_id=checks_by_id, config=config)
    weak_rows = select_weak_rows(scored_rows, config=config)
    strong_anchors = select_strong_anchors(scored_rows, weak_rows)
    weak_memory = build_weak_active_memory(weak_rows)

    weak_output = output_dir / "weak_active_review.jsonl"
    memory_output = output_dir / "weak_active_memory.jsonl"
    strong_output = output_dir / "strong_active_anchors.jsonl"
    summary_output = output_dir / "weak_active_summary.json"
    report_output = output_dir / "weak_active_report.md"

    _write_jsonl(weak_output, weak_rows)
    _write_jsonl(memory_output, weak_memory)
    _write_jsonl(strong_output, strong_anchors)

    cause_counts = Counter(row.get("primary_weakness") for row in weak_rows)
    reason_counts = Counter(reason for row in weak_rows for reason in row.get("weak_reasons") or [])
    summary = {
        "ok": True,
        "mode": "wq-active-weak-review",
        "no_external_llm": True,
        "submit_guard": "read-only platform list and optional check-only review; never submits",
        "account": config.account,
        "platform_source": str(config.platform_file) if config.platform_file else "worldquant_platform",
        "platform_rows": len(platform_rows),
        "active_count": len(active_rows),
        "weak_count": len(weak_rows),
        "strong_anchor_count": len(strong_anchors),
        "checked_count": len(checks_by_id),
        "weak_score_cutoff": config.weak_score_cutoff,
        "bottom_quantile": config.bottom_quantile,
        "counts": {
            "primary_weakness": dict(sorted(cause_counts.items())),
            "weak_reason": dict(sorted(reason_counts.items())),
        },
        "files": {
            "platform_alphas": str(platform_output),
            "weak_active_review": str(weak_output),
            "weak_active_memory": str(memory_output),
            "strong_active_anchors": str(strong_output),
            "summary": str(summary_output),
            "report": str(report_output),
        },
        "top_weak": [
            {
                "alpha_id": row.get("alpha_id"),
                "status": row.get("status"),
                "primary_weakness": row.get("primary_weakness"),
                "weak_score": row.get("weak_score"),
                "quality_percentile": row.get("quality_percentile"),
                "sharpe": row.get("sharpe"),
                "fitness": row.get("fitness"),
                "returns": row.get("returns"),
                "turnover": row.get("turnover"),
                "weak_reasons": row.get("weak_reasons"),
            }
            for row in sorted(weak_rows, key=_weak_sort_key)[:20]
        ],
    }
    _write_json(summary_output, summary)
    report_output.write_text(render_weak_active_report(summary, weak_rows, strong_anchors), encoding="utf-8")
    return summary


def score_active_rows(
    rows: list[dict],
    *,
    checks_by_id: dict[str, dict],
    config: WQActiveWeakReviewConfig,
) -> list[dict]:
    active_count = len(rows)
    field_counts: Counter[str] = Counter()
    operator_counts: Counter[str] = Counter()
    signature_counts: Counter[str] = Counter()

    for row in rows:
        expression = str(row.get("expression") or "")
        components = _components_for(expression)
        field_counts.update(components["fields"])
        operator_counts.update(components["operators"])
        signature = _field_signature_from_components(components)
        if signature:
            signature_counts[signature] += 1

    scored: list[dict] = []
    for row in rows:
        alpha_id = str(row.get("alpha_id") or "")
        merged = _merge_check_result(row, checks_by_id.get(alpha_id))
        components = _components_for(str(merged.get("expression") or ""))
        scored.append(_score_one_active_row(
            merged,
            components=components,
            active_count=active_count,
            field_counts=field_counts,
            operator_counts=operator_counts,
            signature_counts=signature_counts,
        ))

    ranked = sorted(scored, key=_quality_sort_key)
    denominator = max(1, len(ranked) - 1)
    bottom_count = max(1, math.ceil(len(ranked) * max(0.0, min(config.bottom_quantile, 1.0)))) if ranked else 0
    bottom_ids = {str(row.get("alpha_id") or "") for row in ranked[:bottom_count]}
    for rank, row in enumerate(ranked):
        alpha_id = str(row.get("alpha_id") or "")
        row["quality_rank"] = rank + 1
        row["quality_percentile"] = round(rank / denominator, 4) if ranked else None
        row["relative_weak"] = alpha_id in bottom_ids
        if row["relative_weak"] and "relative_laggard" not in row["weak_reasons"]:
            row["weak_reasons"].append("relative_laggard")
            if row["weak_score"] < config.weak_score_cutoff:
                row["weak_score"] = round(row["weak_score"] + 1.0, 4)
                row["weak_score_components"].append({
                    "reason": "relative_laggard",
                    "points": 1.0,
                    "detail": f"bottom {config.bottom_quantile:.0%} within ACTIVE/SUBMITTED cohort",
                })
        row["primary_weakness"] = _primary_weakness(row["weak_reasons"])
        row["lesson"] = _lesson_for_weakness(row["primary_weakness"])
        row["repair_hints"] = _repair_hints(row)
    return scored


def select_weak_rows(rows: list[dict], *, config: WQActiveWeakReviewConfig) -> list[dict]:
    selected = [
        row
        for row in rows
        if row.get("weak_score", 0.0) >= config.weak_score_cutoff or row.get("relative_weak") is True
    ]
    selected.sort(key=_weak_sort_key)
    return selected


def select_strong_anchors(rows: list[dict], weak_rows: list[dict]) -> list[dict]:
    weak_ids = {str(row.get("alpha_id") or "") for row in weak_rows if row.get("alpha_id")}
    candidates = [
        row
        for row in rows
        if str(row.get("alpha_id") or "") not in weak_ids
        and submit_threshold_checks({
            "sharpe": row.get("sharpe"),
            "fitness": row.get("fitness"),
            "turnover": row.get("turnover"),
        })["eligible"]
    ]
    candidates.sort(key=_quality_sort_key, reverse=True)
    limit = max(5, math.ceil(len(rows) * 0.25)) if rows else 0
    return candidates[:limit]


def build_weak_active_memory(rows: list[dict]) -> list[dict]:
    memory: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        expression = str(row.get("expression") or "")
        if not expression:
            continue
        components = _components_for(expression)
        failure_kind = str(row.get("primary_weakness") or "active_metric_mixed")
        key = (failure_kind, normalize_expression(expression))
        if key in seen:
            continue
        seen.add(key)
        memory.append({
            "memory_kind": WEAK_MEMORY_KIND,
            "severity": "penalize",
            "failure_kind": failure_kind,
            "lesson": row.get("lesson") or _lesson_for_weakness(failure_kind),
            "repair_hints": row.get("repair_hints") or [],
            "alpha_id": row.get("alpha_id"),
            "status": row.get("status"),
            "expression": expression,
            "expression_normalized": normalize_expression(expression),
            "fields": sorted(components["fields"]),
            "operators": sorted(components["operators"]),
            "field_signature": _field_signature_from_components(components),
            "sharpe": row.get("sharpe"),
            "fitness": row.get("fitness"),
            "returns": row.get("returns"),
            "turnover": row.get("turnover"),
            "weak_score": row.get("weak_score"),
            "quality_percentile": row.get("quality_percentile"),
            "weak_reasons": row.get("weak_reasons") or [],
            "evidence": {
                "source": "active_weak_review",
                "alpha_id": row.get("alpha_id"),
                "status": row.get("status"),
                "weak_score_components": row.get("weak_score_components") or [],
                "sc_result": row.get("sc_result"),
                "sc_value": row.get("sc_value"),
                "prod_corr_result": row.get("prod_corr_result"),
                "prod_corr_value": row.get("prod_corr_value"),
            },
        })
    return memory


def render_weak_active_report(summary: dict, weak_rows: list[dict], strong_anchors: list[dict]) -> str:
    lines = [
        "# WQ Active Weak Factor Review",
        "",
        f"- Updated: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- Platform rows: {summary.get('platform_rows')}",
        f"- ACTIVE/SUBMITTED rows: {summary.get('active_count')}",
        f"- Weak rows: {summary.get('weak_count')}",
        f"- Check-only rows: {summary.get('checked_count')}",
        f"- No external LLM: {summary.get('no_external_llm')}",
        "",
        "## Weak Cause Counts",
    ]
    counts = (summary.get("counts") or {}).get("primary_weakness") or {}
    if counts:
        for name, count in counts.items():
            lines.append(f"- {name}: {count}")
    else:
        lines.append("- none")

    lines.extend(["", "## Top Weak Active Alphas"])
    for row in sorted(weak_rows, key=_weak_sort_key)[:25]:
        lines.append(
            "- "
            f"{row.get('alpha_id') or 'unknown'} "
            f"status={row.get('status')} "
            f"weak={row.get('weak_score')} "
            f"pct={row.get('quality_percentile')} "
            f"cause={row.get('primary_weakness')} "
            f"metrics=S{row.get('sharpe')}/F{row.get('fitness')}/R{row.get('returns')}/T{row.get('turnover')} "
            f"reasons={','.join(row.get('weak_reasons') or [])}"
        )
        expression = str(row.get("expression") or "")
        if expression:
            lines.append(f"  - expr: {_truncate(expression, 220)}")
        hints = row.get("repair_hints") or []
        if hints:
            lines.append(f"  - repair: {_truncate('; '.join(hints), 220)}")

    lines.extend(["", "## Strong Anchors"])
    for row in strong_anchors[:15]:
        lines.append(
            "- "
            f"{row.get('alpha_id') or 'unknown'} "
            f"metrics=S{row.get('sharpe')}/F{row.get('fitness')}/R{row.get('returns')}/T{row.get('turnover')} "
            f"pct={row.get('quality_percentile')}"
        )

    lines.extend(["", "## Files"])
    for key, value in (summary.get("files") or {}).items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines) + "\n"


def _load_platform_rows(config: WQActiveWeakReviewConfig, *, dependencies: dict[str, Any]) -> list[dict]:
    if config.platform_file:
        return _read_rows(_resolve_path(config.platform_file))
    fetcher = dependencies.get("list_alphas")
    if fetcher:
        return list(fetcher(config))
    if not is_configured(config.account):
        raise RuntimeError(f"WQ BRAIN credentials are not configured (account={config.account})")

    client = get_client(config.account)
    try:
        if not client.authenticate():
            raise RuntimeError(f"WQ BRAIN authentication failed (account={config.account})")
        rows: list[dict] = []
        page_size = 100
        for offset in range(0, max(1, config.platform_sync_limit), page_size):
            result = run_list_alphas(client, limit=page_size, offset=offset)
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "platform alpha list failed")
            page = result.get("alphas") or []
            rows.extend(page)
            if len(page) < page_size or len(rows) >= config.platform_sync_limit:
                break
        return rows[: config.platform_sync_limit]
    finally:
        client.close()


def _check_alpha_ids(
    alpha_ids: list[str],
    *,
    config: WQActiveWeakReviewConfig,
    dependencies: dict[str, Any],
) -> dict[str, dict]:
    ids = [alpha_id for alpha_id in alpha_ids if alpha_id]
    if not ids:
        return {}
    checker: Callable[[list[str], WQActiveWeakReviewConfig], Any] | None = dependencies.get("check_submissions")
    if checker:
        result = checker(ids, config)
        if isinstance(result, dict) and "alphas" in result:
            return {str(key): value for key, value in (result.get("alphas") or {}).items() if isinstance(value, dict)}
        if isinstance(result, dict):
            return {str(key): value for key, value in result.items() if isinstance(value, dict)}
        return {}

    if not is_configured(config.account):
        raise RuntimeError(f"WQ BRAIN credentials are not configured (account={config.account})")
    client = get_client(config.account)
    try:
        if not client.authenticate():
            raise RuntimeError(f"WQ BRAIN authentication failed (account={config.account})")
        out: dict[str, dict] = {}
        for chunk in _chunks(ids, max(1, config.check_chunk_size)):
            result = run_check_submissions(client, chunk)
            out.update(result.get("alphas") or {})
        return out
    finally:
        client.close()


def _score_one_active_row(
    row: dict,
    *,
    components: dict[str, set[str]],
    active_count: int,
    field_counts: Counter[str],
    operator_counts: Counter[str],
    signature_counts: Counter[str],
) -> dict:
    weak_score = 0.0
    reasons: list[str] = []
    components_out: list[dict] = []

    def add(reason: str, points: float, detail: str) -> None:
        nonlocal weak_score
        if reason not in reasons:
            reasons.append(reason)
        weak_score += points
        components_out.append({"reason": reason, "points": points, "detail": detail})

    sharpe = _safe_float(row.get("sharpe"))
    fitness = _safe_float(row.get("fitness"))
    returns = _safe_float(row.get("returns"))
    turnover = _safe_float(row.get("turnover"))

    if sharpe is None or fitness is None:
        add("metric_missing", 1.5, "missing sharpe or fitness")
    if sharpe is not None and sharpe < SUBMIT_THRESHOLDS["sharpe"]:
        add("low_sharpe", 2.0, f"sharpe {sharpe} < {SUBMIT_THRESHOLDS['sharpe']}")
    elif sharpe is not None and sharpe < SUBMIT_THRESHOLDS["sharpe"] + 0.10:
        add("near_low_sharpe", 1.0, f"sharpe {sharpe} is close to the floor")

    if fitness is not None and fitness < SUBMIT_THRESHOLDS["fitness"]:
        add("low_fitness", 3.0, f"fitness {fitness} < {SUBMIT_THRESHOLDS['fitness']}")
    elif fitness is not None and fitness < SUBMIT_THRESHOLDS["fitness"] + 0.10:
        add("near_low_fitness", 1.0, f"fitness {fitness} is close to the floor")

    if returns is not None and returns < 0:
        add("negative_returns", 3.0, f"returns {returns} < 0")
    elif returns is not None and returns < 0.03:
        add("low_returns", 1.5, f"returns {returns} is small")

    if turnover is None:
        add("turnover_missing", 1.0, "missing turnover")
    elif turnover < SUBMIT_THRESHOLDS["turnover_min"] or turnover > SUBMIT_THRESHOLDS["turnover_max"]:
        add(
            "turnover_outside_submit_band",
            2.5,
            f"turnover {turnover} outside {SUBMIT_THRESHOLDS['turnover_min']}..{SUBMIT_THRESHOLDS['turnover_max']}",
        )
    elif turnover < 0.03 or turnover > 0.55:
        add("turnover_drag", 1.0, f"turnover {turnover} is near an inefficient edge")

    sc_value = _safe_float(row.get("sc_value"))
    prod_corr_value = _safe_float(row.get("prod_corr_value"))
    if str(row.get("sc_result") or "").upper() in {"FAIL", "WARNING"} or (sc_value is not None and sc_value >= 0.65):
        add("correlation_risk", 2.0, f"self-correlation result/value {row.get('sc_result')}/{sc_value}")
    if str(row.get("prod_corr_result") or "").upper() in {"FAIL", "WARNING"} or (
        prod_corr_value is not None and prod_corr_value >= 0.65
    ):
        add("correlation_risk", 2.0, f"prod-correlation result/value {row.get('prod_corr_result')}/{prod_corr_value}")

    signature = _field_signature_from_components(components)
    signature_count = signature_counts.get(signature, 0) if signature else 0
    signature_limit = 2 if active_count < 20 else max(3, math.ceil(active_count * 0.05))
    if signature and signature_count >= signature_limit:
        add("crowded_field_signature", 1.25, f"field signature appears {signature_count} times")

    crowded_fields = sorted(
        field
        for field in components["fields"]
        if active_count >= 5 and field_counts.get(field, 0) >= max(3, math.ceil(active_count * 0.20))
    )
    if len(crowded_fields) >= 2:
        add("crowded_fields", 0.75, f"common fields: {','.join(crowded_fields[:6])}")

    return {
        **row,
        "status": str(row.get("status") or "").upper(),
        "sharpe": sharpe,
        "fitness": fitness,
        "returns": returns,
        "turnover": turnover,
        "fields": sorted(components["fields"]),
        "operators": sorted(components["operators"]),
        "field_signature": signature,
        "field_signature_count": signature_count,
        "operator_signature": "|".join(sorted(components["operators"])),
        "weak_score": round(weak_score, 4),
        "weak_score_components": components_out,
        "weak_reasons": reasons,
        "primary_weakness": _primary_weakness(reasons),
        "field_crowding": {
            field: field_counts.get(field, 0)
            for field in sorted(components["fields"])
            if field_counts.get(field, 0) > 1
        },
        "operator_crowding": {
            op: operator_counts.get(op, 0)
            for op in sorted(components["operators"])
            if operator_counts.get(op, 0) > 1
        },
    }


def _merge_check_result(row: dict, check: dict | None) -> dict:
    if not check:
        return dict(row)
    merged = dict(row)
    for key in (
        "sharpe",
        "fitness",
        "returns",
        "turnover",
        "grade",
        "sc_result",
        "sc_value",
        "sc_limit",
        "prod_corr_result",
        "prod_corr_value",
        "prod_corr_limit",
        "review_failure_kind",
        "review_checks",
    ):
        if check.get(key) is not None:
            merged[key] = check.get(key)
    merged["check_only_review"] = check
    return merged


def _primary_weakness(reasons: list[str]) -> str:
    reason_set = set(reasons)
    if "correlation_risk" in reason_set:
        return "active_correlation_risk"
    if "negative_returns" in reason_set or "low_returns" in reason_set:
        return "active_low_returns"
    if "low_fitness" in reason_set or "near_low_fitness" in reason_set:
        return "active_low_fitness"
    if "turnover_outside_submit_band" in reason_set or "turnover_drag" in reason_set:
        return "active_turnover_drag"
    if "crowded_field_signature" in reason_set or "crowded_fields" in reason_set:
        return "active_crowded_family"
    return "active_metric_mixed"


def _lesson_for_weakness(kind: str) -> str:
    return {
        "active_correlation_risk": "Keep the idea as evidence, but require a field-family or operator-family change before reuse.",
        "active_low_returns": "Do not reuse as a standalone return signal; treat it as a small overlay or invert only with fresh evidence.",
        "active_low_fitness": "Avoid spending budget on the same standalone structure; pair it with a stronger orthogonal leg.",
        "active_turnover_drag": "Repair with smoothing or a different horizon before testing related variants.",
        "active_crowded_family": "The field signature is crowded in the active book; diversify fields or operator structure.",
        "active_metric_mixed": "Use only as weak evidence and demand a materially different construction.",
    }.get(kind, "Use only as weak evidence and demand a materially different construction.")


def _repair_hints(row: dict) -> list[str]:
    reasons = set(row.get("weak_reasons") or [])
    hints: list[str] = []
    if "correlation_risk" in reasons or "crowded_field_signature" in reasons or "crowded_fields" in reasons:
        hints.append("change field family or add a small orthogonal options/microstructure overlay")
    if "negative_returns" in reasons or "low_returns" in reasons:
        hints.append("test inversion or use only as a low-weight contrarian overlay")
    if "low_fitness" in reasons or "near_low_fitness" in reasons:
        hints.append("blend with a stronger quality/value leg before simulation")
    if "turnover_outside_submit_band" in reasons or "turnover_drag" in reasons:
        hints.append("adjust horizon with smoothing or slower rolling rank")
    if not hints:
        hints.append("require material field/operator change before retesting")
    return hints


def _quality_sort_key(row: dict) -> tuple:
    turnover = _safe_float(row.get("turnover"))
    turnover_penalty = 0.0
    if turnover is None:
        turnover_penalty = 1.0
    elif turnover < SUBMIT_THRESHOLDS["turnover_min"]:
        turnover_penalty = SUBMIT_THRESHOLDS["turnover_min"] - turnover
    elif turnover > SUBMIT_THRESHOLDS["turnover_max"]:
        turnover_penalty = turnover - SUBMIT_THRESHOLDS["turnover_max"]
    return (
        _safe_float(row.get("fitness")) if _safe_float(row.get("fitness")) is not None else -999.0,
        _safe_float(row.get("sharpe")) if _safe_float(row.get("sharpe")) is not None else -999.0,
        _safe_float(row.get("returns")) if _safe_float(row.get("returns")) is not None else -999.0,
        -turnover_penalty,
    )


def _weak_sort_key(row: dict) -> tuple:
    return (
        -(_safe_float(row.get("weak_score")) or 0.0),
        _safe_float(row.get("quality_percentile")) if _safe_float(row.get("quality_percentile")) is not None else 1.0,
        str(row.get("alpha_id") or ""),
    )


def _active_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if str(row.get("status") or "").upper() in ACTIVE_STATUSES]


def _components_for(expression: str) -> dict[str, set[str]]:
    try:
        parts = extract_components(expression or "")
    except Exception:
        return {"fields": set(), "operators": set()}
    return {
        "fields": {str(item) for item in parts.get("fields", set())},
        "operators": {str(item) for item in parts.get("operators", set())},
    }


def _field_signature_from_components(components: dict[str, set[str]]) -> str:
    return "|".join(sorted(components.get("fields") or []))


def _read_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
        return _rows_from_payload(payload)

    rows: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rows.extend(_rows_from_payload(json.loads(line)))
    return rows


def _rows_from_payload(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("alphas", "active", "rows", "results"):
            if isinstance(payload.get(key), list):
                return [row for row in payload[key] if isinstance(row, dict)]
        return [payload]
    return []


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
    path.write_text(text + ("\n" if text else ""), encoding="utf-8")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _chunks(values: list[str], size: int):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truncate(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _resolve_path(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path
