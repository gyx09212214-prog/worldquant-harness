"""Shared harness for static WQ candidate-generation scripts."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .wq_auto_mining import validate_wq_expression


def run_static_candidate_generator(
    argv: list[str] | None,
    *,
    records_func: Callable[[], Iterable[dict[str, Any]]],
    default_output: Path,
    default_limit: int | None,
    description: str,
    limit_valid_count: bool = False,
    add_candidate_rank: bool = True,
) -> int:
    args = _parse_static_args(argv, default_output=default_output, default_limit=default_limit, description=description)
    summary = write_static_candidate_artifacts(
        records_func(),
        output=Path(args.output),
        limit=None if getattr(args, "limit", None) is None else int(args.limit),
        limit_valid_count=limit_valid_count,
        add_candidate_rank=add_candidate_rank,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


def write_static_candidate_artifacts(
    records: Iterable[dict[str, Any]],
    *,
    output: Path,
    limit: int | None,
    limit_valid_count: bool = False,
    add_candidate_rank: bool = True,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()
    source_rows = list(records)
    iterable = source_rows if limit_valid_count or limit is None else source_rows[:limit]
    for source_row in iterable:
        row = dict(source_row)
        key = _candidate_key(row)
        if key in seen:
            continue
        seen.add(key)
        try:
            validate_wq_expression(row["expression"])
        except Exception as exc:
            invalid.append({**row, "validation_error": str(exc)})
            continue
        if add_candidate_rank:
            row["candidate_rank"] = len(rows) + 1
        rows.append(row)
        if limit_valid_count and limit is not None and len(rows) >= limit:
            break

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows) + "\n",
        encoding="utf-8",
    )
    summary = {
        "ok": True,
        "output": str(output),
        "written": len(rows),
        "invalid": len(invalid),
        "tags": [row["tag"] for row in rows],
    }
    output.with_suffix(".summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    if invalid:
        output.with_suffix(".invalid.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in invalid) + "\n",
            encoding="utf-8",
        )
    return summary


def _parse_static_args(
    argv: list[str] | None,
    *,
    default_output: Path,
    default_limit: int | None,
    description: str,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--output", default=str(default_output))
    if default_limit is not None:
        parser.add_argument("--limit", type=int, default=default_limit)
    return parser.parse_args(argv)


def _candidate_key(row: dict[str, Any]) -> str:
    return str(row.get("expression") or "") + "||" + json.dumps(row.get("simulation_settings") or {}, sort_keys=True)


def lln_proxy_expression() -> str:
    return (
        "rank(0.28*ts_rank(actual_sales_value_quarterly/cap,60)+"
        "0.24*ts_rank(actual_eps_value_quarterly/close,60)+"
        "0.24*ts_rank(change_in_eps_surprise,60)+"
        "0.16*rank(ts_mean(implied_volatility_call_90-implied_volatility_put_90,5))-"
        "0.12*ts_rank(returns,20))"
    )
