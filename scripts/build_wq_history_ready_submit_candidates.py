"""Build a direct-submit candidate file from historical low-self-corr checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.expression_parser import extract_components, normalize_expression


DEFAULT_REPORT_ROOT = ROOT / "reports"
DEFAULT_ACTIVE_NODES = ROOT / "reports" / "wq_active_alpha_map_pnl_20260610_full" / "active_nodes.jsonl"
DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit10_20260610" / "history_ready_lowcorr_submit_candidates.jsonl"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    global _CURRENT_ARGS
    _CURRENT_ARGS = args
    active_ids, active_norms = _active_inventory(Path(args.active_nodes))
    excluded_domains = {str(item) for item in args.exclude_domain}
    excluded_ids = {str(item) for item in args.exclude_alpha_id}
    rows = []
    seen_ids: set[str] = set()
    seen_exprs: set[str] = set()

    for path in Path(args.report_root).rglob("*.jsonl"):
        if _skip_path(path):
            continue
        for row in _read_jsonl(path):
            candidate = _candidate_from_row(row, path)
            if not candidate:
                continue
            alpha_id = str(candidate["alpha_id"])
            expression = str(candidate.get("expression") or "")
            norm = normalize_expression(expression)
            if alpha_id in active_ids or alpha_id in excluded_ids or alpha_id in seen_ids:
                continue
            if str(candidate.get("domain") or "") in excluded_domains:
                continue
            if norm in active_norms or norm in seen_exprs:
                continue
            seen_ids.add(alpha_id)
            seen_exprs.add(norm)
            rows.append(candidate)

    rows.sort(key=_sort_key)
    rows = rows[: max(0, args.limit)]

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows) + "\n", encoding="utf-8")
    summary = {
        "ok": True,
        "output": str(output),
        "written": len(rows),
        "top": [
            {
                "alpha_id": row["alpha_id"],
                "score": row["score"],
                "sharpe": row["sharpe"],
                "fitness": row["fitness"],
                "turnover": row["turnover"],
                "sc_value": row["sc_value"],
                "domain": row["domain"],
                "tag": row.get("tag"),
                "source_file": row.get("source_file"),
            }
            for row in rows[:30]
        ],
    }
    output.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build historical ready direct-submit candidates")
    parser.add_argument("--report-root", default=str(DEFAULT_REPORT_ROOT))
    parser.add_argument("--active-nodes", default=str(DEFAULT_ACTIVE_NODES))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--max-sc", type=float, default=0.705)
    parser.add_argument("--min-sharpe", type=float, default=1.25)
    parser.add_argument("--min-fitness", type=float, default=1.0)
    parser.add_argument("--exclude-domain", action="append", default=[])
    parser.add_argument("--exclude-alpha-id", action="append", default=[])
    return parser.parse_args(argv)


def _candidate_from_row(row: dict[str, Any], path: Path) -> dict[str, Any] | None:
    alpha_id = str(row.get("alpha_id") or "")
    expression = str(row.get("expression") or row.get("candidate", {}).get("expression") or "").strip()
    if not alpha_id or not expression:
        return None
    sc = _to_float(row.get("sc_value"))
    if sc is None and isinstance(row.get("precheck"), dict):
        sc = _to_float(row["precheck"].get("sc_value"))
    if sc is None:
        return None
    sharpe = _to_float(row.get("sharpe"))
    fitness = _to_float(row.get("fitness"))
    turnover = _to_float(row.get("turnover"))
    metrics = row.get("candidate_metrics") if isinstance(row.get("candidate_metrics"), dict) else {}
    sharpe = sharpe if sharpe is not None else _to_float(metrics.get("sharpe"))
    fitness = fitness if fitness is not None else _to_float(metrics.get("fitness"))
    turnover = turnover if turnover is not None else _to_float(metrics.get("turnover"))
    if sharpe is None or fitness is None:
        return None
    args = _CURRENT_ARGS
    if sc > args.max_sc or sharpe < args.min_sharpe or fitness < args.min_fitness:
        return None
    status = str(row.get("final_status") or row.get("api_check_status") or row.get("status") or "").upper()
    if status in {"ACTIVE", "ALREADY_SUBMITTED"}:
        return None
    fields = _fields(expression)
    domain = _domain(fields, expression)
    score = round(sharpe * 0.55 + fitness * 0.75 + max(0.0, 0.705 - sc) * 3.0, 4)
    return {
        "alpha_id": alpha_id,
        "rank": None,
        "domain": domain,
        "expression": expression,
        "sharpe": sharpe,
        "fitness": fitness,
        "turnover": turnover,
        "sc_value": sc,
        "score": score,
        "tag": row.get("tag"),
        "source_file": str(path),
        "source_status": status,
        "source_fields": fields,
    }


def _sort_key(row: dict[str, Any]) -> tuple:
    return (
        -float(row.get("score") or 0),
        float(row.get("sc_value") or 1),
        -float(row.get("fitness") or 0),
        -float(row.get("sharpe") or 0),
        float(row.get("turnover") or 999),
        str(row.get("alpha_id") or ""),
    )


def _active_inventory(path: Path) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    norms: set[str] = set()
    for row in _read_jsonl(path):
        for alpha_id in row.get("alpha_ids") or []:
            ids.add(str(alpha_id))
        expression = str(row.get("expression") or "").strip()
        if expression:
            norms.add(normalize_expression(expression))
    return ids, norms


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _skip_path(path: Path) -> bool:
    text = str(path).replace("\\", "/")
    return "/node_modules/" in text or "/.git/" in text


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fields(expression: str) -> list[str]:
    try:
        components = extract_components(expression)
    except Exception:
        return []
    return sorted(str(field) for field in components.get("fields", []))


def _domain(fields: list[str], expression: str) -> str:
    field_set = set(fields)
    text = expression.lower()
    if any("implied_volatility" in field or field.startswith("pcr_") for field in field_set):
        return "options_positioning"
    if field_set & {"earnings_momentum_composite_score", "change_in_eps_surprise"} or "anl4_" in text:
        return "analyst_revision"
    if any(field.endswith("_derivative") for field in field_set):
        return "model_derivative"
    if field_set & {"cashflow_op", "cashflow", "cashflow_fin", "forward_sales_to_price", "equity", "cap", "assets"}:
        return "fundamental_value"
    if field_set & {"volume", "adv20", "vwap"}:
        return "liquidity_microstructure"
    return "other"


_CURRENT_ARGS: argparse.Namespace


if __name__ == "__main__":
    raise SystemExit(main())
