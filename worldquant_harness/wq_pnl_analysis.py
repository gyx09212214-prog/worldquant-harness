"""Analyze WQ daily PnL curves into yearly stability metrics."""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .record_utils import safe_float as _safe_float
from .report_utils import format_number as _format_number
from .report_utils import markdown_cell as _md

TRADING_DAYS_PER_YEAR = 252
DEFAULT_BOOK_SIZE = 20_000_000.0


def analyze_probe_directory(
    probe_dir: Path,
    *,
    submitted_rows: Iterable[dict[str, Any]] = (),
    default_book_size: float = DEFAULT_BOOK_SIZE,
) -> dict[str, Any]:
    """Analyze all ``*_summary.json`` files produced by probe_wq_alpha_detail.py."""
    tags = {
        str(row.get("alpha_id") or ""): str(row.get("tag") or "")
        for row in submitted_rows
        if row.get("alpha_id")
    }
    alpha_reports: list[dict[str, Any]] = []
    generated_summary_names = {"summary.json", "pnl_analysis_summary.json"}
    for summary_path in sorted(probe_dir.glob("*_summary.json")):
        if summary_path.name in generated_summary_names:
            continue
        summary = _read_json(summary_path)
        alpha_id = str(summary.get("alpha_id") or summary_path.name.removesuffix("_summary.json"))
        probe = _read_json(probe_dir / f"{alpha_id}_probe.json")
        alpha_reports.append(
            analyze_alpha_probe_summary(
                summary,
                probe=probe,
                tag=tags.get(alpha_id, ""),
                default_book_size=default_book_size,
            )
        )
    return build_pnl_analysis_report(alpha_reports, probe_dir=probe_dir)


def analyze_alpha_probe_summary(
    summary: dict[str, Any],
    *,
    probe: dict[str, Any] | None = None,
    tag: str = "",
    default_book_size: float = DEFAULT_BOOK_SIZE,
) -> dict[str, Any]:
    """Return yearly and stability metrics for one alpha probe summary."""
    alpha_id = str(summary.get("alpha_id") or "")
    alpha_detail = _alpha_detail_payload(probe or {})
    is_metrics = alpha_detail.get("is") if isinstance(alpha_detail.get("is"), dict) else {}
    book_size = _safe_float(is_metrics.get("bookSize")) or default_book_size
    curve = summary.get("pnl_curve") or []
    yearly = yearly_metrics_from_daily_pnl(curve, book_size=book_size)
    stability = stability_metrics(yearly)
    return {
        "alpha_id": alpha_id,
        "tag": tag,
        "status": alpha_detail.get("status"),
        "stage": alpha_detail.get("stage"),
        "expression": ((alpha_detail.get("regular") or {}).get("code") if isinstance(alpha_detail.get("regular"), dict) else None),
        "book_size": book_size,
        "overall": {
            "sharpe": _safe_float(is_metrics.get("sharpe")),
            "fitness": _safe_float(is_metrics.get("fitness")),
            "returns": _safe_float(is_metrics.get("returns")),
            "turnover": _safe_float(is_metrics.get("turnover")),
            "drawdown": _safe_float(is_metrics.get("drawdown")),
            "margin": _safe_float(is_metrics.get("margin")),
            "pnl": _safe_float(is_metrics.get("pnl")),
            "self_correlation": _safe_float(is_metrics.get("selfCorrelation")),
            "prod_correlation": _safe_float(is_metrics.get("prodCorrelation")),
        },
        "pnl_curve_found": bool(summary.get("pnl_curve_found")),
        "pnl_points": int(_safe_float(summary.get("pnl_points")) or len(curve)),
        "pnl_curve_path": summary.get("pnl_curve_path") or "",
        "yearly": yearly,
        "stability": stability,
        "warnings": stability_warnings(yearly, stability),
    }


def yearly_metrics_from_daily_pnl(curve: Iterable[dict[str, Any]], *, book_size: float = DEFAULT_BOOK_SIZE) -> list[dict[str, Any]]:
    """Aggregate normalized daily PnL into yearly return, Sharpe, and drawdown."""
    by_year: dict[int, list[float]] = {}
    for row in curve:
        day = _parse_date(row.get("date"))
        pnl = _safe_float(row.get("pnl"))
        if day is None or pnl is None:
            continue
        daily_return = pnl / book_size if book_size else 0.0
        by_year.setdefault(day.year, []).append(daily_return)

    years: list[dict[str, Any]] = []
    for year in sorted(by_year):
        returns = by_year[year]
        if not returns:
            continue
        pnl_sum = sum(returns) * book_size
        annual_return = sum(returns)
        mean_ret = statistics.mean(returns)
        std_ret = statistics.stdev(returns) if len(returns) >= 2 else 0.0
        sharpe = (mean_ret / std_ret * math.sqrt(TRADING_DAYS_PER_YEAR)) if std_ret > 0 else None
        cumulative = _cumulative_sum(returns)
        years.append({
            "year": year,
            "days": len(returns),
            "pnl": round(pnl_sum, 2),
            "return": round(annual_return, 6),
            "sharpe": round(sharpe, 4) if sharpe is not None else None,
            "hit_rate": round(sum(1 for value in returns if value > 0) / len(returns), 4),
            "max_drawdown": round(_max_drawdown_from_cumulative(cumulative), 6),
            "mean_daily_return": round(mean_ret, 8),
            "daily_vol": round(std_ret, 8),
        })
    return years


def stability_metrics(yearly: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize yearly metrics into review-friendly stability scores."""
    if not yearly:
        return {
            "years": 0,
            "positive_year_ratio": None,
            "min_year_sharpe": None,
            "worst_year_return": None,
            "recent_2y_sharpe": None,
            "pnl_concentration": None,
            "temporal_stability_score": 0.0,
        }

    returns = [_safe_float(row.get("return")) or 0.0 for row in yearly]
    sharpes = [_safe_float(row.get("sharpe")) for row in yearly if _safe_float(row.get("sharpe")) is not None]
    pnl_abs = [abs(_safe_float(row.get("pnl")) or 0.0) for row in yearly]
    recent = yearly[-2:]
    recent_sharpes = [_safe_float(row.get("sharpe")) for row in recent if _safe_float(row.get("sharpe")) is not None]
    positive_ratio = sum(1 for value in returns if value > 0) / len(returns)
    pnl_concentration = max(pnl_abs) / sum(pnl_abs) if sum(pnl_abs) > 0 else None
    min_sharpe = min(sharpes) if sharpes else None
    score = _temporal_stability_score(
        positive_year_ratio=positive_ratio,
        min_year_sharpe=min_sharpe,
        worst_year_return=min(returns),
        recent_2y_sharpe=statistics.mean(recent_sharpes) if recent_sharpes else None,
        pnl_concentration=pnl_concentration,
    )
    return {
        "years": len(yearly),
        "positive_year_ratio": round(positive_ratio, 4),
        "min_year_sharpe": round(min_sharpe, 4) if min_sharpe is not None else None,
        "worst_year_return": round(min(returns), 6),
        "recent_2y_sharpe": round(statistics.mean(recent_sharpes), 4) if recent_sharpes else None,
        "pnl_concentration": round(pnl_concentration, 4) if pnl_concentration is not None else None,
        "temporal_stability_score": round(score, 4),
    }


def stability_warnings(yearly: list[dict[str, Any]], stability: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not yearly:
        return ["missing_pnl_curve"]
    if (stability.get("years") or 0) < 3:
        warnings.append("short_pnl_history")
    if _safe_float(stability.get("positive_year_ratio")) is not None and float(stability["positive_year_ratio"]) < 0.8:
        warnings.append("low_positive_year_ratio")
    if _safe_float(stability.get("min_year_sharpe")) is not None and float(stability["min_year_sharpe"]) < 0:
        warnings.append("negative_year_sharpe")
    if _safe_float(stability.get("worst_year_return")) is not None and float(stability["worst_year_return"]) < -0.02:
        warnings.append("bad_worst_year_return")
    if _safe_float(stability.get("pnl_concentration")) is not None and float(stability["pnl_concentration"]) > 0.45:
        warnings.append("single_year_pnl_concentration")
    return warnings


def build_pnl_analysis_report(alpha_reports: list[dict[str, Any]], *, probe_dir: Path | None = None) -> dict[str, Any]:
    found = [row for row in alpha_reports if row.get("pnl_curve_found") and row.get("yearly")]
    return {
        "ok": True,
        "probe_dir": str(probe_dir) if probe_dir else "",
        "alpha_count": len(alpha_reports),
        "pnl_found_count": len(found),
        "alpha_reports": alpha_reports,
        "portfolio_yearly": portfolio_yearly_metrics(alpha_reports),
        "markdown": render_pnl_analysis_markdown(alpha_reports, probe_dir=probe_dir),
    }


def portfolio_yearly_metrics(alpha_reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Equal-weight yearly aggregation across alphas with available PnL curves."""
    by_year: dict[int, list[float]] = {}
    for report in alpha_reports:
        for row in report.get("yearly") or []:
            year = int(row["year"])
            value = _safe_float(row.get("return"))
            if value is not None:
                by_year.setdefault(year, []).append(value)
    rows: list[dict[str, Any]] = []
    for year in sorted(by_year):
        values = by_year[year]
        rows.append({
            "year": year,
            "alpha_count": len(values),
            "equal_weight_return": round(statistics.mean(values), 6),
            "positive_alpha_ratio": round(sum(1 for value in values if value > 0) / len(values), 4),
            "min_alpha_return": round(min(values), 6),
            "max_alpha_return": round(max(values), 6),
        })
    return rows


def render_pnl_analysis_markdown(alpha_reports: list[dict[str, Any]], *, probe_dir: Path | None = None) -> str:
    lines = [
        "---",
        "tags:",
        "  - worldquant",
        "  - pnl-analysis",
        "  - factor-map",
        f"created: {date.today().isoformat()}",
        "---",
        "",
        "# WQ Alpha 年度 PnL 稳定性分析",
        "",
        "## Alpha Summary",
        "",
        "| Alpha | Tag | PnL | Years | Positive Years | Min Year Sharpe | Worst Year Ret | Recent 2Y Sharpe | PnL Concentration | Stability | Warnings |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for report in alpha_reports:
        stability = report.get("stability") or {}
        lines.append(
            f"| `{_md(report.get('alpha_id'))}` | `{_md(report.get('tag'))}` | "
            f"{bool(report.get('pnl_curve_found'))} | "
            f"{_fmt(stability.get('years'))} | "
            f"{_fmt_pct(stability.get('positive_year_ratio'))} | "
            f"{_fmt(stability.get('min_year_sharpe'))} | "
            f"{_fmt_pct(stability.get('worst_year_return'))} | "
            f"{_fmt(stability.get('recent_2y_sharpe'))} | "
            f"{_fmt_pct(stability.get('pnl_concentration'))} | "
            f"{_fmt(stability.get('temporal_stability_score'))} | "
            f"{', '.join(report.get('warnings') or [])} |"
        )

    lines.extend(["", "## Yearly Metrics", ""])
    for report in alpha_reports:
        lines.extend([
            f"### `{_md(report.get('alpha_id'))}`",
            "",
            "| Year | Days | PnL | Return | Sharpe | Hit Rate | Max DD |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ])
        yearly = report.get("yearly") or []
        if not yearly:
            lines.append("| - | - | - | - | - | - | - |")
        for row in yearly:
            lines.append(
                f"| {row.get('year')} | {row.get('days')} | {_fmt(row.get('pnl'))} | "
                f"{_fmt_pct(row.get('return'))} | {_fmt(row.get('sharpe'))} | "
                f"{_fmt(row.get('hit_rate'))} | {_fmt_pct(row.get('max_drawdown'))} |"
            )
        lines.append("")

    portfolio = portfolio_yearly_metrics(alpha_reports)
    lines.extend([
        "## Equal-Weight Portfolio View",
        "",
        "| Year | Alpha Count | EW Return | Positive Alpha Ratio | Min Ret | Max Ret |",
        "|---:|---:|---:|---:|---:|---:|",
    ])
    for row in portfolio:
        lines.append(
            f"| {row['year']} | {row['alpha_count']} | {_fmt_pct(row['equal_weight_return'])} | "
            f"{_fmt(row['positive_alpha_ratio'])} | {_fmt_pct(row['min_alpha_return'])} | {_fmt_pct(row['max_alpha_return'])} |"
        )
    if probe_dir:
        lines.extend(["", f"Probe directory: `{probe_dir}`"])
    return "\n".join(lines).rstrip() + "\n"


def write_pnl_analysis_artifacts(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "pnl_analysis_summary.json"
    alpha_path = output_dir / "pnl_alpha_metrics.jsonl"
    yearly_path = output_dir / "pnl_yearly_metrics.jsonl"
    markdown_path = output_dir / "pnl_analysis.md"
    summary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with alpha_path.open("w", encoding="utf-8") as fh:
        for row in report.get("alpha_reports") or []:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    with yearly_path.open("w", encoding="utf-8") as fh:
        for report_row in report.get("alpha_reports") or []:
            for year_row in report_row.get("yearly") or []:
                fh.write(json.dumps({
                    "alpha_id": report_row.get("alpha_id"),
                    "tag": report_row.get("tag"),
                    **year_row,
                }, ensure_ascii=False, default=str) + "\n")
    markdown_path.write_text(str(report.get("markdown") or ""), encoding="utf-8")
    return {
        "summary": str(summary_path),
        "alpha_metrics": str(alpha_path),
        "yearly_metrics": str(yearly_path),
        "markdown": str(markdown_path),
    }


def daily_return_series_from_pnl(
    curve: Iterable[dict[str, Any]],
    *,
    book_size: float = DEFAULT_BOOK_SIZE,
    cumulative: bool = False,
) -> dict[date, float]:
    """Convert a WQ PnL curve into date-indexed daily returns."""

    points: list[tuple[date, float]] = []
    for row in curve:
        day = _parse_date(row.get("date"))
        if day is None:
            continue
        direct_return = _safe_float(row.get("daily_return"))
        if direct_return is None:
            direct_return = _safe_float(row.get("return"))
        pnl = _safe_float(row.get("pnl"))
        if direct_return is not None:
            value = direct_return
        elif pnl is not None:
            value = pnl / book_size if book_size else 0.0
        else:
            continue
        points.append((day, value))

    if not cumulative:
        return {day: value for day, value in sorted(points)}

    series: dict[date, float] = {}
    previous: float | None = None
    for day, cumulative_value in sorted(points):
        if previous is not None:
            series[day] = cumulative_value - previous
        previous = cumulative_value
    return series


def aligned_daily_return_correlation(
    left_curve: Iterable[dict[str, Any]],
    right_curve: Iterable[dict[str, Any]],
    *,
    book_size: float = DEFAULT_BOOK_SIZE,
    cumulative: bool = False,
    min_overlap: int = 20,
    warn_abs_correlation: float = 0.50,
    reject_abs_correlation: float = 0.70,
) -> dict[str, Any]:
    """Correlate two PnL curves on overlapping daily returns."""

    left = daily_return_series_from_pnl(left_curve, book_size=book_size, cumulative=cumulative)
    right = daily_return_series_from_pnl(right_curve, book_size=book_size, cumulative=cumulative)
    common = sorted(set(left) & set(right))
    if len(common) < min_overlap:
        return {
            "ok": False,
            "gate": "insufficient_overlap",
            "overlap_days": len(common),
            "correlation": None,
            "abs_correlation": None,
        }
    left_values = [left[day] for day in common]
    right_values = [right[day] for day in common]
    corr = _pearson(left_values, right_values)
    if corr is None:
        return {
            "ok": False,
            "gate": "zero_variance",
            "overlap_days": len(common),
            "correlation": None,
            "abs_correlation": None,
        }
    abs_corr = abs(corr)
    gate = "pass"
    ok = True
    if abs_corr >= reject_abs_correlation:
        gate = "reject"
        ok = False
    elif abs_corr >= warn_abs_correlation:
        gate = "warn"
    return {
        "ok": ok,
        "gate": gate,
        "overlap_days": len(common),
        "correlation": round(corr, 6),
        "abs_correlation": round(abs_corr, 6),
        "warn_abs_correlation": warn_abs_correlation,
        "reject_abs_correlation": reject_abs_correlation,
    }


def max_active_daily_return_correlation(
    candidate_curve: Iterable[dict[str, Any]],
    active_curves: dict[str, Iterable[dict[str, Any]] | dict[str, Any]],
    *,
    book_size: float = DEFAULT_BOOK_SIZE,
    cumulative: bool = False,
    min_overlap: int = 20,
    warn_abs_correlation: float = 0.50,
    reject_abs_correlation: float = 0.70,
) -> dict[str, Any]:
    """Return the strongest daily-return correlation against active curves."""

    best: dict[str, Any] | None = None
    for alpha_id, payload in active_curves.items():
        curve = _curve_from_payload(payload)
        result = aligned_daily_return_correlation(
            candidate_curve,
            curve,
            book_size=book_size,
            cumulative=cumulative,
            min_overlap=min_overlap,
            warn_abs_correlation=warn_abs_correlation,
            reject_abs_correlation=reject_abs_correlation,
        )
        result["alpha_id"] = alpha_id
        current = _safe_float(result.get("abs_correlation"))
        best_value = _safe_float((best or {}).get("abs_correlation"))
        if current is not None and (best_value is None or current > best_value):
            best = result
    if best is None:
        return {
            "ok": False,
            "gate": "missing_active_curves",
            "alpha_id": None,
            "overlap_days": 0,
            "correlation": None,
            "abs_correlation": None,
        }
    return best


def _temporal_stability_score(
    *,
    positive_year_ratio: float,
    min_year_sharpe: float | None,
    worst_year_return: float,
    recent_2y_sharpe: float | None,
    pnl_concentration: float | None,
) -> float:
    score = 50.0
    score += 25.0 * positive_year_ratio
    if min_year_sharpe is not None:
        score += max(-20.0, min(20.0, min_year_sharpe * 5.0))
    score += max(-15.0, min(15.0, worst_year_return * 250.0))
    if recent_2y_sharpe is not None:
        score += max(-10.0, min(10.0, recent_2y_sharpe * 2.5))
    if pnl_concentration is not None:
        score -= max(0.0, pnl_concentration - 0.35) * 60.0
    return max(0.0, min(100.0, score))


def _curve_from_payload(payload: Iterable[dict[str, Any]] | dict[str, Any]) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        curve = payload.get("pnl_curve") or payload.get("curve") or []
        return curve if isinstance(curve, list) else []
    return payload


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = statistics.mean(left)
    right_mean = statistics.mean(right)
    left_centered = [value - left_mean for value in left]
    right_centered = [value - right_mean for value in right]
    left_var = sum(value * value for value in left_centered)
    right_var = sum(value * value for value in right_centered)
    if left_var <= 0.0 or right_var <= 0.0:
        return None
    cov = sum(a * b for a, b in zip(left_centered, right_centered))
    return cov / math.sqrt(left_var * right_var)


def _alpha_detail_payload(probe: dict[str, Any]) -> dict[str, Any]:
    for endpoint in probe.get("endpoints") or []:
        if isinstance(endpoint, dict) and endpoint.get("path", "").count("/") == 2 and endpoint.get("ok"):
            data = endpoint.get("data")
            if isinstance(data, dict):
                return data
    return {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text[:8], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _cumulative_sum(values: Iterable[float]) -> list[float]:
    out: list[float] = []
    total = 0.0
    for value in values:
        total += value
        out.append(total)
    return out


def _max_drawdown_from_cumulative(values: list[float]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    max_dd = 0.0
    for value in values:
        peak = max(peak, value)
        max_dd = min(max_dd, value - peak)
    return abs(max_dd)


def _fmt(value: Any) -> str:
    return _format_number(value, coerce=_safe_float, large_commas=True)


def _fmt_pct(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return ""
    return f"{number * 100:.2f}%"
