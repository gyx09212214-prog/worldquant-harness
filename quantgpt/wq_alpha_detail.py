"""Utilities for read-only WQ alpha detail probes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DATE_KEYS = {"date", "day", "timestamp", "time", "datetime", "x"}
PNL_KEYS = {
    "pnl",
    "dailyPnl",
    "daily_pnl",
    "profit",
    "returns",
    "return",
    "value",
    "y",
}
SERIES_KEYS = {
    "pnl",
    "pnlCurve",
    "pnl_curve",
    "pnlChart",
    "pnl_chart",
    "dailyPnl",
    "daily_pnl",
    "chart",
    "series",
    "values",
    "records",
    "data",
    "points",
}
SERIES_PATH_TOKENS = ("pnl", "chart", "performance", "curve")
NON_SERIES_PATH_TOKENS = ("checks", "check", "settings")


def extract_pnl_curve(payload: Any) -> list[dict[str, Any]]:
    """Return a normalized PnL-like curve from a raw endpoint payload, if present."""
    candidates: list[tuple[str, list[dict[str, Any]]]] = []
    _walk_for_series(payload, path="$", candidates=candidates)
    if not candidates:
        return []
    candidates.sort(key=lambda item: (-len(item[1]), item[0]))
    return candidates[0][1]


def summarize_alpha_probe(probe: dict[str, Any]) -> dict[str, Any]:
    endpoints = probe.get("endpoints") or []
    pnl_by_endpoint: dict[str, list[dict[str, Any]]] = {}
    for endpoint in endpoints:
        if not isinstance(endpoint, dict) or not endpoint.get("ok"):
            continue
        curve = extract_pnl_curve(endpoint.get("data"))
        if curve:
            pnl_by_endpoint[str(endpoint.get("path") or "")] = curve
    best_path = ""
    best_curve: list[dict[str, Any]] = []
    if pnl_by_endpoint:
        best_path, best_curve = sorted(pnl_by_endpoint.items(), key=lambda item: (-len(item[1]), item[0]))[0]
    return {
        "ok": bool(probe.get("ok")),
        "alpha_id": probe.get("alpha_id"),
        "read_only": probe.get("read_only") is True,
        "endpoint_count": len(endpoints),
        "successful_endpoints": sum(1 for endpoint in endpoints if isinstance(endpoint, dict) and endpoint.get("ok")),
        "status_codes": {
            str(endpoint.get("path") or ""): endpoint.get("status_code")
            for endpoint in endpoints
            if isinstance(endpoint, dict)
        },
        "pnl_curve_found": bool(best_curve),
        "pnl_curve_path": best_path,
        "pnl_points": len(best_curve),
        "pnl_curve": best_curve,
    }


def write_probe_outputs(output_dir: Path, alpha_id: str, probe: dict[str, Any], summary: dict[str, Any]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_id = _safe_filename(alpha_id)
    files = {
        "probe": output_dir / f"{safe_id}_probe.json",
        "summary": output_dir / f"{safe_id}_summary.json",
    }
    files["probe"].write_text(json.dumps(probe, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    files["summary"].write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if summary.get("pnl_curve"):
        pnl_path = output_dir / f"{safe_id}_pnl_curve.jsonl"
        with pnl_path.open("w", encoding="utf-8") as fh:
            for row in summary["pnl_curve"]:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        files["pnl_curve"] = pnl_path
    return {key: str(path) for key, path in files.items()}


def render_probe_markdown(summaries: list[dict[str, Any]], *, output_dir: Path | None = None) -> str:
    lines = [
        "# WQ Alpha 只读详情探测",
        "",
        "此报告只来自 GET-only endpoint probe，不包含 submit/delete 行为。",
        "",
        "| Alpha | OK | Success Endpoints | PnL Found | PnL Points | PnL Path |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for summary in summaries:
        lines.append(
            f"| `{_md(summary.get('alpha_id'))}` | {summary.get('ok')} | "
            f"{summary.get('successful_endpoints')}/{summary.get('endpoint_count')} | "
            f"{summary.get('pnl_curve_found')} | {summary.get('pnl_points')} | "
            f"`{_md(summary.get('pnl_curve_path') or '')}` |"
        )
    if output_dir:
        lines.extend(["", f"输出目录：`{output_dir}`"])
    return "\n".join(lines).rstrip() + "\n"


def _walk_for_series(payload: Any, *, path: str, candidates: list[tuple[str, list[dict[str, Any]]]]) -> None:
    if isinstance(payload, list):
        curve = _normalize_list_series(payload, source_path=path)
        if curve and _looks_like_curve_path(path, curve):
            candidates.append((path, curve))
        for index, item in enumerate(payload[:50]):
            _walk_for_series(item, path=f"{path}[{index}]", candidates=candidates)
        return
    if not isinstance(payload, dict):
        return
    for key, value in payload.items():
        next_path = f"{path}.{key}"
        if key in SERIES_KEYS:
            curve = _normalize_series_value(value, source_path=next_path)
            if curve and _looks_like_curve_path(next_path, curve, explicit_series_key=True):
                candidates.append((next_path, curve))
        if isinstance(value, (dict, list)):
            _walk_for_series(value, path=next_path, candidates=candidates)


def _normalize_series_value(value: Any, *, source_path: str) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return _normalize_list_series(value, source_path=source_path)
    if isinstance(value, dict):
        for key in ("values", "data", "records", "points", "series"):
            if isinstance(value.get(key), list):
                return _normalize_list_series(value[key], source_path=f"{source_path}.{key}")
        if all(isinstance(item, (int, float)) for item in value.values()):
            rows = [{"date": key, "pnl": val, "source_path": source_path} for key, val in value.items()]
            return rows if len(rows) >= 2 else []
    return []


def _normalize_list_series(values: list[Any], *, source_path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(values):
        row = _normalize_point(item, index=index, source_path=source_path)
        if row:
            rows.append(row)
    if len(rows) < 2:
        return []
    return rows


def _looks_like_curve_path(path: str, rows: list[dict[str, Any]], *, explicit_series_key: bool = False) -> bool:
    lower_path = path.lower()
    if any(token in lower_path for token in NON_SERIES_PATH_TOKENS):
        return False
    has_date = any(row.get("date") is not None for row in rows)
    has_curve_token = any(token in lower_path for token in SERIES_PATH_TOKENS)
    if explicit_series_key and has_curve_token:
        return True
    return has_date and (explicit_series_key or has_curve_token)


def _normalize_point(item: Any, *, index: int, source_path: str) -> dict[str, Any] | None:
    if isinstance(item, (int, float)):
        return {"index": index, "pnl": float(item), "source_path": source_path}
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        pnl = _safe_float(item[1])
        if pnl is None:
            return None
        return {"date": item[0], "pnl": pnl, "source_path": source_path}
    if not isinstance(item, dict):
        return None
    date = _first_present(*(item.get(key) for key in DATE_KEYS))
    pnl_key = next((key for key in PNL_KEYS if _safe_float(item.get(key)) is not None), "")
    if not pnl_key:
        return None
    return {
        "date": date,
        "index": index if date is None else None,
        "pnl": _safe_float(item.get(pnl_key)),
        "pnl_key": pnl_key,
        "source_path": source_path,
    }


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in str(value))


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")
