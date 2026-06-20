"""Generate direct-weight micro repairs for the options/earnings base near-pass."""

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


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "options_direct_micro_candidates.jsonl"


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
    parser = argparse.ArgumentParser(description="Generate options direct micro candidates")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args(argv)


def _add(rows: list[dict[str, Any]], tag: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": "options_direct_micro_repair",
            "source": "generate_wq_submit5_more_options_direct_micro_candidates",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "direct_weight_micro_repair",
            "rationale": rationale,
            "risk_flags": ["real_submit_candidate", "requires_online_simulation"],
        }
    )


def _inner(pcr_coeff: float = -0.20, pcr_window: int = 60, iv_coeff: float = 0.20, ret_window: int = 30) -> str:
    return (
        "ts_backfill("
        "0.25 * ts_rank(change_in_eps_surprise, 60) + "
        "0.20 * ts_rank(actual_eps_value_quarterly / open, 80) "
        f"{pcr_coeff:+.2f} * ts_rank(pcr_oi_20, {pcr_window}) + "
        f"{iv_coeff:.2f} * rank(ts_mean((implied_volatility_call_60 - implied_volatility_put_60) / "
        "(implied_volatility_call_60 + implied_volatility_put_60), 10)) + "
        "0.15 * rank(volume / adv20) - "
        f"0.20 * ts_rank(returns, {ret_window}), 60)"
    )


def _core(group: str = "industry", **kwargs: Any) -> str:
    return f"group_rank({_inner(**kwargs)}, {group})"


def _vwap_delta(delta_window: int = 10, rank_window: int = 40) -> str:
    return f"ts_rank(-ts_delta(vwap, {delta_window}) / vwap, {rank_window})"


def _range_pressure() -> str:
    return "rank((high - close) / (high - low) * rank(volume / ts_mean(volume, 20)))"


def _close_vwap(window: int = 10) -> str:
    return f"rank(-ts_decay_linear(close / vwap, {window}))"


def _records() -> list[dict[str, Any]]:
    d8_ind_t05 = {"neutralization": "INDUSTRY", "decay": 8, "truncation": 0.05}
    d8_ind_t03 = {"neutralization": "INDUSTRY", "decay": 8, "truncation": 0.03}
    d8_ind_t02 = {"neutralization": "INDUSTRY", "decay": 8, "truncation": 0.02}
    d8_sec_t05 = {"neutralization": "SECTOR", "decay": 8, "truncation": 0.05}
    rows: list[dict[str, Any]] = []

    _add(
        rows,
        "odm-base-range04-d8",
        f"rank(0.46 * {_core()} + 0.32 * {_vwap_delta()} + "
        f"0.10 * rank(volume / adv20) + 0.04 * {_range_pressure()} - 0.08 * ts_rank(returns, 90))",
        d8_ind_t05,
        "Directly add a tiny range-pressure leg to the 0.7067 base without outer nested mixing.",
    )
    _add(
        rows,
        "odm-base-range06-d8",
        f"rank(0.45 * {_core()} + 0.31 * {_vwap_delta()} + "
        f"0.10 * rank(volume / adv20) + 0.06 * {_range_pressure()} - 0.08 * ts_rank(returns, 90))",
        d8_ind_t05,
        "Slightly stronger direct range-pressure overlay.",
    )
    _add(
        rows,
        "odm-base-vwap06-d8",
        f"rank(0.46 * {_core()} + 0.30 * {_vwap_delta()} + "
        f"0.10 * rank(volume / adv20) + 0.06 * {_close_vwap(10)} - 0.08 * ts_rank(returns, 90))",
        d8_ind_t05,
        "Direct close/vwap overlay, avoiding the qMg outer-rank form that raised self-corr.",
    )
    _add(
        rows,
        "odm-base-vwap08-ret120",
        f"rank(0.45 * {_core()} + 0.30 * {_vwap_delta()} + "
        f"0.09 * rank(volume / adv20) + 0.08 * {_close_vwap(12)} - 0.08 * ts_rank(returns, 120))",
        d8_ind_t05,
        "Slightly stronger close/vwap overlay and longer outer returns penalty.",
    )
    _add(
        rows,
        "odm-base-vwapdelta12",
        f"rank(0.47 * {_core()} + 0.31 * {_vwap_delta(12, 50)} + "
        f"0.12 * rank(volume / adv20) + 0.02 * {_range_pressure()} - 0.08 * ts_rank(returns, 90))",
        d8_ind_t05,
        "Only shift the vwap-delta trajectory and add a tiny range-pressure term.",
    )
    _add(
        rows,
        "odm-pcr18-iv18-range04",
        f"rank(0.46 * {_core(pcr_coeff=-0.18, iv_coeff=0.18)} + 0.32 * {_vwap_delta()} + "
        f"0.10 * rank(volume / adv20) + 0.04 * {_range_pressure()} - 0.08 * ts_rank(returns, 90))",
        d8_ind_t05,
        "Slightly reduce PCR and IV intensity while adding a small range-pressure leg.",
    )
    _add(
        rows,
        "odm-pcr18-iv22-vwap06",
        f"rank(0.46 * {_core(pcr_coeff=-0.18, iv_coeff=0.22)} + 0.30 * {_vwap_delta()} + "
        f"0.10 * rank(volume / adv20) + 0.06 * {_close_vwap(12)} - 0.08 * ts_rank(returns, 90))",
        d8_ind_t05,
        "Tilt the options spread up but lower PCR and add direct close/vwap overlay.",
    )
    _add(
        rows,
        "odm-sector-range04",
        f"rank(0.46 * {_core('sector')} + 0.32 * {_vwap_delta()} + "
        f"0.10 * rank(volume / adv20) + 0.04 * {_range_pressure()} - 0.08 * ts_rank(returns, 90))",
        d8_sec_t05,
        "Sector bucket plus tiny direct range-pressure overlay.",
    )
    _add(
        rows,
        "odm-sector-vwap06",
        f"rank(0.46 * {_core('sector')} + 0.30 * {_vwap_delta()} + "
        f"0.10 * rank(volume / adv20) + 0.06 * {_close_vwap(12)} - 0.08 * ts_rank(returns, 90))",
        d8_sec_t05,
        "Sector bucket plus direct close/vwap overlay.",
    )
    _add(
        rows,
        "odm-base-range04-t003",
        f"rank(0.46 * {_core()} + 0.32 * {_vwap_delta()} + "
        f"0.10 * rank(volume / adv20) + 0.04 * {_range_pressure()} - 0.08 * ts_rank(returns, 90))",
        d8_ind_t03,
        "Same direct range-pressure expression with truncation 0.03.",
    )
    _add(
        rows,
        "odm-base-vwap06-t002",
        f"rank(0.46 * {_core()} + 0.30 * {_vwap_delta()} + "
        f"0.10 * rank(volume / adv20) + 0.06 * {_close_vwap(10)} - 0.08 * ts_rank(returns, 90))",
        d8_ind_t02,
        "Direct close/vwap expression with tight truncation.",
    )

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
