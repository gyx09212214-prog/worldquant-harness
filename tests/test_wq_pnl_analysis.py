from quantgpt.wq_pnl_analysis import stability_metrics, stability_warnings, yearly_metrics_from_daily_pnl


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
