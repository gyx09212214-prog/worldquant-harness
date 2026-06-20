"""Generate options/earnings near-pass repair candidates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.wq_auto_mining import validate_wq_expression


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "options_nearpass_candidates.jsonl"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output = Path(args.output)
    rows: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _records():
        key = row["expression"] + "||" + json.dumps(row.get("simulation_settings") or {}, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        try:
            validate_wq_expression(row["expression"])
        except Exception as exc:
            invalid.append({**row, "validation_error": str(exc)})
            continue
        row["candidate_rank"] = len(rows) + 1
        rows.append(row)

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
    output.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if invalid:
        output.with_suffix(".invalid.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in invalid) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate options near-pass candidates")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args(argv)


def _add(
    rows: list[dict[str, Any]],
    tag: str,
    expr: str,
    settings: dict[str, Any],
    rationale: str,
) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": "options_earnings_nearpass_repair",
            "source": "generate_wq_submit5_more_options_nearpass_candidates",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "nearpass_settings_and_micro_weight_repair",
            "rationale": rationale,
            "risk_flags": ["real_submit_candidate", "requires_online_simulation"],
        }
    )


def _base_inner(group: str = "industry", pcr_window: int = 60, ret_window: int = 30) -> str:
    return (
        "rank(0.48 * group_rank(ts_backfill("
        "0.25 * ts_rank(change_in_eps_surprise, 60) + "
        "0.20 * ts_rank(actual_eps_value_quarterly / open, 80) - "
        f"0.20 * ts_rank(pcr_oi_20, {pcr_window}) + "
        "0.20 * rank(ts_mean((implied_volatility_call_60 - implied_volatility_put_60) / "
        "(implied_volatility_call_60 + implied_volatility_put_60), 10)) + "
        "0.15 * rank(volume / adv20) - "
        f"0.20 * ts_rank(returns, {ret_window}), 60), {group}) + "
        "0.32 * ts_rank(-ts_delta(vwap, 10) / vwap, 40) + "
        "0.12 * rank(volume / adv20) - "
        "0.08 * ts_rank(returns, 90))"
    )


def _range_pressure() -> str:
    return "rank((high - close) / (high - low) * rank(volume / ts_mean(volume, 20)))"


def _close_vwap(window: int = 10) -> str:
    return f"rank(-ts_decay_linear(close / vwap, {window}))"


def _records() -> list[dict[str, Any]]:
    d8_ind_t03 = {"neutralization": "INDUSTRY", "decay": 8, "truncation": 0.03}
    d10_ind_t03 = {"neutralization": "INDUSTRY", "decay": 10, "truncation": 0.03}
    d12_ind_t03 = {"neutralization": "INDUSTRY", "decay": 12, "truncation": 0.03}
    d10_ind_t02 = {"neutralization": "INDUSTRY", "decay": 10, "truncation": 0.02}
    d8_sec_t05 = {"neutralization": "SECTOR", "decay": 8, "truncation": 0.05}
    d10_sec_t03 = {"neutralization": "SECTOR", "decay": 10, "truncation": 0.03}

    base_ind = _base_inner("industry")
    base_sector = _base_inner("sector")
    rows: list[dict[str, Any]] = []

    _add(
        rows,
        "opt-base-ind-d10-t003",
        base_ind,
        d10_ind_t03,
        "Same 0.7067 near-pass expression with slower decay.",
    )
    _add(
        rows,
        "opt-base-ind-d10-t002",
        base_ind,
        d10_ind_t02,
        "Same 0.7067 near-pass expression with tighter truncation.",
    )
    _add(
        rows,
        "opt-base-ind-d12-t003",
        base_ind,
        d12_ind_t03,
        "Same near-pass with decay 12 to alter holding path.",
    )
    _add(
        rows,
        "opt-base-pcr80-ret40",
        _base_inner("industry", pcr_window=80, ret_window=40),
        d10_ind_t03,
        "Only lengthen PCR and short-return windows around the 0.7067 near-pass.",
    )
    _add(
        rows,
        "opt-sector-d10-t003",
        base_sector,
        d10_sec_t03,
        "Sector group variant of the 0.7079 near-pass with slower decay and tighter truncation.",
    )
    _add(
        rows,
        "opt-sector-d8-t005",
        base_sector,
        d8_sec_t05,
        "Recheck sector neutralization path from the 0.7079 near-pass family.",
    )

    _add(
        rows,
        "qmg-range24-vwap16",
        f"rank(0.60 * {base_ind} + 0.24 * {_range_pressure()} + 0.16 * {_close_vwap(10)})",
        d8_ind_t03,
        "Increase the close/vwap overlay from the 0.7174 near-pass while keeping range pressure.",
    )
    _add(
        rows,
        "qmg-range22-vwap18",
        f"rank(0.60 * {base_ind} + 0.22 * {_range_pressure()} + 0.18 * {_close_vwap(12)})",
        d8_ind_t03,
        "Push further toward the close/vwap overlay, which was the best prior decorrelator.",
    )
    _add(
        rows,
        "qmg-range26-vwap14-d10",
        f"rank(0.60 * {base_ind} + 0.26 * {_range_pressure()} + 0.14 * {_close_vwap(12)})",
        d10_ind_t03,
        "Use slower decay with a balanced range and close/vwap overlay.",
    )
    _add(
        rows,
        "qmg-range20-vwap16-corr04",
        f"rank(0.60 * {base_ind} + 0.20 * {_range_pressure()} + "
        f"0.16 * {_close_vwap(10)} + 0.04 * rank(ts_decay_linear(ts_corr(close, volume, 20), 5)))",
        d8_ind_t03,
        "Add only a small close-volume correlation term after the close/vwap overlay.",
    )
    _add(
        rows,
        "qmg-pcr80-range22-vwap18",
        f"rank(0.60 * {_base_inner('industry', pcr_window=80, ret_window=40)} + "
        f"0.22 * {_range_pressure()} + 0.18 * {_close_vwap(12)})",
        d10_ind_t03,
        "Combine the tiny base-window shift with the strongest qMg close/vwap overlay.",
    )
    _add(
        rows,
        "qmg-sector-range22-vwap18",
        f"rank(0.60 * {base_sector} + 0.22 * {_range_pressure()} + 0.18 * {_close_vwap(12)})",
        d10_sec_t03,
        "Sector group plus stronger close/vwap overlay.",
    )

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
