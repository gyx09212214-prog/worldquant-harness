from worldquant_harness.wq_pnl_analysis import (
    aligned_daily_return_correlation,
    max_active_daily_return_correlation,
    stability_metrics,
    stability_warnings,
    yearly_metrics_from_daily_pnl,
)


def test_yearly_metrics_from_daily_pnl_computes_years_and_sharpe():
    curve = [
        {"date": "2020-01-02", "pnl": 100.0},
        {"date": "2020-01-03", "pnl": -50.0},
        {"date": "2020-01-06", "pnl": 150.0},
        {"date": "2021-01-04", "pnl": 200.0},
        {"date": "2021-01-05", "pnl": 100.0},
    ]

    yearly = yearly_metrics_from_daily_pnl(curve, book_size=10_000.0)

    assert [row["year"] for row in yearly] == [2020, 2021]
    assert yearly[0]["pnl"] == 200.0
    assert yearly[0]["return"] == 0.02
    assert yearly[0]["sharpe"] is not None
    assert yearly[1]["pnl"] == 300.0
    assert yearly[1]["sharpe"] is not None


def test_stability_metrics_flags_concentrated_negative_years():
    yearly = [
        {"year": 2019, "return": 0.10, "pnl": 1000.0, "sharpe": 2.0},
        {"year": 2020, "return": -0.03, "pnl": -300.0, "sharpe": -0.5},
        {"year": 2021, "return": 0.01, "pnl": 100.0, "sharpe": 0.2},
    ]

    stability = stability_metrics(yearly)
    warnings = stability_warnings(yearly, stability)

    assert stability["years"] == 3
    assert stability["positive_year_ratio"] == 0.6667
    assert "low_positive_year_ratio" in warnings
    assert "negative_year_sharpe" in warnings
    assert "bad_worst_year_return" in warnings
    assert "single_year_pnl_concentration" in warnings


def test_aligned_daily_return_correlation_rejects_high_overlap_correlation():
    left = [{"date": f"2024-01-{day:02d}", "pnl": float(day)} for day in range(1, 22)]
    right = [{"date": f"2024-01-{day:02d}", "pnl": float(day * 2)} for day in range(1, 22)]

    result = aligned_daily_return_correlation(left, right, book_size=1.0, min_overlap=20)

    assert result["gate"] == "reject"
    assert result["ok"] is False
    assert result["abs_correlation"] == 1.0


def test_max_active_daily_return_correlation_selects_strongest_active_curve():
    candidate = [{"date": f"2024-02-{day:02d}", "daily_return": float(day)} for day in range(1, 22)]
    active_curves = {
        "weak": [{"date": f"2024-02-{day:02d}", "daily_return": float(22 - day)} for day in range(1, 22)],
        "strong": [{"date": f"2024-02-{day:02d}", "daily_return": float(day * 3)} for day in range(1, 22)],
    }

    result = max_active_daily_return_correlation(candidate, active_curves, min_overlap=20)

    assert result["alpha_id"] == "weak"
    assert result["abs_correlation"] == 1.0
