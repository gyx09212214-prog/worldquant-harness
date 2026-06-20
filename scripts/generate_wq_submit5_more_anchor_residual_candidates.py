"""Generate anchor-residual WQ repairs against known active self-corr anchors."""

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


DEFAULT_OUTPUT = ROOT / "reports" / "wq_submit5_more_20260611" / "anchor_residual_candidates.jsonl"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output = Path(args.output)
    rows: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _records()[: args.limit]:
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
    parser = argparse.ArgumentParser(description="Generate anchor residual WQ candidates")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=32)
    return parser.parse_args(argv)


def _add(rows: list[dict[str, Any]], tag: str, family: str, expr: str, settings: dict[str, Any], rationale: str) -> None:
    rows.append(
        {
            "tag": tag,
            "source_family": family,
            "source": "generate_wq_submit5_more_anchor_residual_candidates",
            "expression": expr,
            "simulation_settings": settings,
            "mutation_strategy": "active_anchor_residualization",
            "rationale": rationale,
            "risk_flags": [
                "real_submit_candidate",
                "requires_online_simulation",
                "anchor_residual_repair",
            ],
        }
    )


def _settings(neut: str, decay: int, trunc: float) -> dict[str, Any]:
    return {"neutralization": neut, "decay": decay, "truncation": trunc, "maxPosition": "ON"}


def _opt_core(group: str = "industry", *, pcr_window: int = 80, ret_window: int = 45) -> str:
    return (
        "group_rank(ts_backfill("
        "0.25*ts_rank(change_in_eps_surprise,90)+"
        "0.20*ts_rank(actual_eps_value_quarterly/open,100)-"
        f"0.18*ts_rank(pcr_oi_20,{pcr_window})+"
        "0.22*rank(ts_mean((implied_volatility_call_60-implied_volatility_put_60)/"
        "(implied_volatility_call_60+implied_volatility_put_60),12))+"
        "0.10*rank(volume/adv20)-"
        f"0.18*ts_rank(returns,{ret_window}),80),"
        f"{group})"
    )


def _o0ox_proxy() -> str:
    return (
        "rank(0.20*ts_rank(ts_backfill(actual_eps_value_quarterly,252)/vwap,80)+"
        "0.14*ts_rank(ts_backfill(earnings_momentum_composite_score,252),60)+"
        "0.30*rank(ts_mean((ts_backfill(implied_volatility_call_90,120)-ts_backfill(implied_volatility_put_90,120))/"
        "(ts_backfill(implied_volatility_call_90,120)+ts_backfill(implied_volatility_put_90,120)),5))+"
        "0.18*rank(volume/adv20)+"
        "0.12*rank(-1*ts_rank(ts_backfill(pcr_oi_10,120),60))-"
        "0.12*ts_rank(returns,30))"
    )


def _o0ox_shallow(rescale: float = 1.0) -> str:
    return (
        f"{0.06 * rescale:.3f}*ts_rank(ts_backfill(actual_eps_value_quarterly,252)/vwap,80)+"
        f"{0.05 * rescale:.3f}*ts_rank(ts_backfill(earnings_momentum_composite_score,252),60)+"
        f"{0.09 * rescale:.3f}*rank(ts_mean((ts_backfill(implied_volatility_call_90,120)-ts_backfill(implied_volatility_put_90,120))/"
        "(ts_backfill(implied_volatility_call_90,120)+ts_backfill(implied_volatility_put_90,120)),5))+"
        f"{0.06 * rescale:.3f}*rank(volume/adv20)+"
        f"{0.05 * rescale:.3f}*rank(-1*ts_rank(ts_backfill(pcr_oi_10,120),60))-"
        f"{0.04 * rescale:.3f}*ts_rank(returns,30)"
    )


def _lln_shallow(rescale: float = 1.0) -> str:
    return (
        f"{0.05 * rescale:.3f}*ts_rank(actual_sales_value_quarterly/cap,60)+"
        f"{0.04 * rescale:.3f}*ts_rank(actual_eps_value_quarterly/close,60)+"
        f"{0.04 * rescale:.3f}*ts_rank(change_in_eps_surprise,60)+"
        f"{0.04 * rescale:.3f}*rank(ts_mean(implied_volatility_call_90-implied_volatility_put_90,5))-"
        f"{0.03 * rescale:.3f}*ts_rank(returns,20)"
    )


def _lln_proxy() -> str:
    return (
        "rank(0.28*ts_rank(actual_sales_value_quarterly/cap,60)+"
        "0.24*ts_rank(actual_eps_value_quarterly/close,60)+"
        "0.24*ts_rank(change_in_eps_surprise,60)+"
        "0.16*rank(ts_mean(implied_volatility_call_90-implied_volatility_put_90,5))-"
        "0.12*ts_rank(returns,20))"
    )


def _omyo_proxy() -> str:
    return (
        "rank(0.38*group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,60),subindustry)+"
        "0.42*rank((open-close)/open)+"
        "0.20*rank(ts_corr(vwap,volume,40)))"
    )


def _vwap_delta(window: int = 14, rank_window: int = 55) -> str:
    return f"ts_rank(-ts_delta(vwap,{window})/vwap,{rank_window})"


def _ivcf_core(group: str = "subindustry") -> str:
    return (
        f"0.32*group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,120),{group})+"
        "0.22*rank(0.70*rank(ts_rank(ts_backfill(cashflow_op,120)/cap,80)-ts_rank(returns,30))+"
        "0.30*rank(-1*ts_rank(returns,120)))+"
        "0.12*rank(volume/adv20)+0.08*rank(ts_corr(vwap,volume,60))-0.12*ts_rank(returns,80)"
    )


def _records() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    i10 = _settings("INDUSTRY", 10, 0.03)
    i12 = _settings("INDUSTRY", 12, 0.025)
    s10 = _settings("SECTOR", 10, 0.03)
    sub12 = _settings("SUBINDUSTRY", 12, 0.02)
    sub16 = _settings("SUBINDUSTRY", 16, 0.02)

    _add(
        rows,
        "res-opt-o0ox-light-i10",
        "options_o0ox_residual",
        f"rank(0.60*{_opt_core()}+0.18*{_vwap_delta()}+0.08*rank(ts_corr(vwap,volume,80))-0.10*ts_rank(returns,120)-{_o0ox_shallow(1.0)})",
        i10,
        "Residualize the strong options repair against the visible O0oxYmXd EPS/IV/PCR/volume proxy.",
    )
    _add(
        rows,
        "res-opt-o0ox-med-i12",
        "options_o0ox_residual",
        f"rank(0.62*{_opt_core(pcr_window=100, ret_window=55)}+0.16*{_vwap_delta(16,60)}+0.08*rank(ts_corr(vwap,volume,80))-0.10*ts_rank(returns,140)-{_o0ox_shallow(1.35)})",
        i12,
        "Stronger O0ox residual, slower windows and tighter truncation.",
    )
    _add(
        rows,
        "res-opt-o0ox-sector-light",
        "options_o0ox_residual",
        f"rank(0.58*{_opt_core('sector')}+0.17*{_vwap_delta()}+0.08*rank(ts_corr(vwap,volume,80))-0.10*ts_rank(returns,120)-{_o0ox_shallow(1.0)})",
        s10,
        "Sector variant with light O0ox residualization.",
    )
    _add(
        rows,
        "res-opt-o0ox-lln-i10",
        "options_o0ox_lln_residual",
        f"rank(0.62*{_opt_core(pcr_window=100, ret_window=55)}+0.16*{_vwap_delta(16,60)}+0.08*rank(ts_corr(vwap,volume,80))-0.10*ts_rank(returns,140)-{_o0ox_shallow(1.0)}-{_lln_shallow(0.8)})",
        i10,
        "Residualize both O0ox and LLn to reduce the options/EPS active cluster overlap.",
    )
    _add(
        rows,
        "res-opt-o0ox-zy-small-sub12",
        "options_o0ox_qmx_residual",
        f"rank(0.52*{_opt_core('subindustry', pcr_window=100, ret_window=55)}+0.14*{_vwap_delta(16,60)}+0.10*ts_rank(ts_backfill(forward_sales_to_price,120),100)+0.08*rank(ts_corr(vwap,volume,80))-0.10*ts_rank(returns,140)-{_o0ox_shallow(1.0)})",
        sub12,
        "Subindustry version shifts some payload to forward sales after subtracting O0ox.",
    )
    _add(
        rows,
        "res-opt-o0ox-book-i12",
        "options_o0ox_value_residual",
        f"rank(0.48*{_opt_core(pcr_window=100, ret_window=55)}+0.14*{_vwap_delta(16,60)}+0.14*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.10*ts_rank(ts_backfill(forward_sales_to_price,120),100)-0.10*ts_rank(returns,140)-{_o0ox_shallow(1.0)})",
        i12,
        "Replace some O0ox-like options exposure with book/sales value after residualization.",
    )

    _add(
        rows,
        "res-simple-opt-o0ox-i10",
        "simple_options_o0ox_residual",
        "rank(0.22*ts_rank(change_in_eps_surprise,90)+0.18*ts_rank(actual_eps_value_quarterly/open,100)-0.15*ts_rank(pcr_oi_20,100)+0.18*rank(ts_mean((implied_volatility_call_60-implied_volatility_put_60)/(implied_volatility_call_60+implied_volatility_put_60),12))+0.14*ts_rank(-ts_delta(vwap,14)/vwap,55)+0.08*rank(ts_corr(vwap,volume,80))-0.12*ts_rank(returns,120)-0.08*rank(ts_mean((ts_backfill(implied_volatility_call_90,120)-ts_backfill(implied_volatility_put_90,120))/(ts_backfill(implied_volatility_call_90,120)+ts_backfill(implied_volatility_put_90,120)),5))-0.05*rank(volume/adv20)-0.05*rank(-1*ts_rank(ts_backfill(pcr_oi_10,120),60)))",
        i10,
        "A shallow direct version of the 0.7344 options repair, subtracting the largest O0ox legs.",
    )
    _add(
        rows,
        "res-simple-opt-book-i12",
        "simple_options_value_o0ox_residual",
        "rank(0.18*ts_rank(change_in_eps_surprise,100)+0.12*ts_rank(actual_eps_value_quarterly/open,120)+0.14*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.12*ts_rank(ts_backfill(forward_sales_to_price,120),100)-0.12*ts_rank(pcr_oi_20,100)+0.16*rank(ts_mean((implied_volatility_call_60-implied_volatility_put_60)/(implied_volatility_call_60+implied_volatility_put_60),12))+0.12*ts_rank(-ts_delta(vwap,16)/vwap,60)+0.08*rank(ts_corr(vwap,volume,80))-0.12*ts_rank(returns,140)-0.08*rank(ts_mean((ts_backfill(implied_volatility_call_90,120)-ts_backfill(implied_volatility_put_90,120))/(ts_backfill(implied_volatility_call_90,120)+ts_backfill(implied_volatility_put_90,120)),5))-0.05*rank(volume/adv20))",
        i12,
        "Shift shallow options residual toward book/sales value to reduce O0ox overlap.",
    )
    _add(
        rows,
        "res-simple-opt-revmag-sales-i10",
        "simple_revision_sales_o0ox_residual",
        "rank(0.18*ts_rank(ts_backfill(earnings_revision_magnitude,120),100)+0.16*ts_rank(ts_backfill(actual_sales_value_quarterly,120)/enterprise_value,120)+0.14*ts_rank(ts_backfill(forward_sales_to_price,120),100)+0.14*rank(ts_mean((implied_volatility_call_60-implied_volatility_put_60)/(implied_volatility_call_60+implied_volatility_put_60),12))+0.12*ts_rank(-ts_delta(vwap,14)/vwap,55)+0.08*rank(-1*correlation_last_30_days_spy)+0.08*rank(ts_corr(vwap,volume,80))-0.12*ts_rank(returns,120)-0.08*rank(ts_mean((ts_backfill(implied_volatility_call_90,120)-ts_backfill(implied_volatility_put_90,120))/(ts_backfill(implied_volatility_call_90,120)+ts_backfill(implied_volatility_put_90,120)),5))-0.05*rank(volume/adv20))",
        i10,
        "Avoid actual EPS and change surprise; use revision magnitude plus sales while subtracting IV90/volume.",
    )
    _add(
        rows,
        "res-simple-opt-sector-revmag",
        "simple_revision_sales_o0ox_residual",
        "rank(0.18*ts_rank(ts_backfill(earnings_revision_magnitude,120),100)+0.16*ts_rank(ts_backfill(actual_sales_value_quarterly,120)/enterprise_value,120)+0.14*ts_rank(ts_backfill(forward_sales_to_price,120),100)+0.14*rank(ts_mean((implied_volatility_call_60-implied_volatility_put_60)/(implied_volatility_call_60+implied_volatility_put_60),12))+0.12*ts_rank(-ts_delta(vwap,14)/vwap,55)+0.08*rank(-1*correlation_last_30_days_spy)+0.08*rank(ts_corr(vwap,volume,80))-0.12*ts_rank(returns,120)-0.08*rank(ts_mean((ts_backfill(implied_volatility_call_90,120)-ts_backfill(implied_volatility_put_90,120))/(ts_backfill(implied_volatility_call_90,120)+ts_backfill(implied_volatility_put_90,120)),5))-0.05*rank(volume/adv20))",
        s10,
        "Sector setting for the shallow revision/sales residual.",
    )

    _add(
        rows,
        "res-ivcf-lln-light-sub12",
        "iv_cashflow_lln_residual",
        f"rank({_ivcf_core('subindustry')}-0.12*{_lln_proxy()})",
        sub12,
        "The RRrzMJqo family missed at 0.7709; subtract the LLn sales/EPS/IV90 proxy.",
    )
    _add(
        rows,
        "res-ivcf-lln-omyo-sub16",
        "iv_cashflow_lln_omyo_residual",
        f"rank({_ivcf_core('subindustry')}-0.10*{_lln_proxy()}-0.10*{_omyo_proxy()})",
        sub16,
        "Residualize both LLn and omYo raw-IV/open-close proxies.",
    )
    _add(
        rows,
        "res-ivcf-omyo-i12",
        "iv_cashflow_omyo_residual",
        f"rank({_ivcf_core('industry')}-0.14*{_omyo_proxy()}+0.08*rank(-1*correlation_last_30_days_spy))",
        i12,
        "Industry IV/cashflow candidate with omYo residual and market-correlation overlay.",
    )
    _add(
        rows,
        "res-ivcf-value-lln-i10",
        "iv_cashflow_value_lln_residual",
        f"rank(0.24*group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,120),industry)+0.18*rank(ts_rank(ts_backfill(cashflow_op,120)/cap,100)-ts_rank(returns,50))+0.12*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.10*rank(-1*credit_risk_premium_indicator)+0.08*rank(ts_corr(vwap,volume,80))-0.12*ts_rank(returns,120)-0.10*{_lln_proxy()})",
        i10,
        "Lower raw IV/cashflow weights and add book/credit before subtracting LLn.",
    )
    _add(
        rows,
        "res-ivcf-rel-omyo-sub12",
        "iv_cashflow_relationship_omyo_residual",
        f"rank(0.24*group_rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,120),subindustry)+0.18*rank(ts_rank(ts_backfill(cashflow_op,120)/cap,100)-ts_rank(returns,50))+0.12*ts_rank(rel_ret_cust,140)+0.10*rank(-1*correlation_last_30_days_spy)+0.08*rank(ts_corr(vwap,volume,80))-0.12*ts_rank(returns,120)-0.10*{_omyo_proxy()})",
        sub12,
        "Relationship/market overlay plus omYo residual for the IV/cashflow branch.",
    )
    _add(
        rows,
        "res-qmx-o0ox-book-sales-sub16",
        "qmx_o0ox_value_residual",
        f"rank(0.10*group_rank(ts_rank(ts_backfill(actual_eps_value_quarterly,120)/close,90),industry)+0.08*ts_rank(ts_backfill(anl4_af_eps_value,120),80)+0.07*ts_rank(ts_backfill(change_in_eps_surprise,120),80)+0.08*rank(ts_backfill(implied_volatility_call_120-implied_volatility_put_120,120))+0.12*ts_rank(ts_backfill(forward_book_value_to_price,120),120)+0.12*ts_rank(ts_backfill(forward_sales_to_price,120),120)+0.10*group_rank(ts_rank(ts_backfill(cashflow_op,120)/cap,80),subindustry)+0.08*rank(-1*credit_risk_premium_indicator)-0.14*ts_rank(returns,100)-{_o0ox_shallow(1.0)})",
        sub16,
        "Try one more qMX/value bridge, explicitly subtracting the O0ox options/value active proxy.",
    )

    return rows


if __name__ == "__main__":
    raise SystemExit(main())
