from scripts.build_wq_strict_diverse_active_ensembles import _ensemble_expression


def test_ensemble_expression_uses_requested_equal_weight() -> None:
    chosen = [
        {"source": {"expression": "rank(close)"}},
        {"source": {"expression": "rank(open)"}},
        {"source": {"expression": "rank(volume)"}},
        {"source": {"expression": "rank(returns)"}},
    ]

    expression = _ensemble_expression(chosen, 0.25)

    assert expression.count("0.25*rank") == 4
    assert expression.startswith("rank(")
