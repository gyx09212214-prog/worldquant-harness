from quantgpt.wq_auto_mining import validate_wq_expression
from quantgpt.wq_evolutionary_generator import (
    build_seed_pool,
    classify_domain,
    generate_evolutionary_candidates,
)


def test_classify_domain_prefers_usa3000_relevant_groups():
    assert classify_domain(["cashflow_op", "assets"], "rank(cashflow_op / assets)") == "fundamental_quality"
    assert classify_domain(["anl4_af_eps_value", "close"], "rank(anl4_af_eps_value / close)") == "analyst_revision"
    assert classify_domain(["implied_volatility_call_90", "implied_volatility_put_90"], "rank(x)") == "options_positioning"
    assert classify_domain(["volume", "adv20"], "rank(volume / adv20)") == "liquidity_microstructure"


def test_build_seed_pool_family_hash_collapses_window_only_variants():
    seeds = build_seed_pool(
        active_rows=[],
        candidate_rows=[
            {"expression": "rank(ts_rank(cashflow_op / assets, 80))", "tag": "cf80"},
            {"expression": "rank(ts_rank(cashflow_op / assets, 120))", "tag": "cf120"},
            {"expression": "rank(volume / adv20)", "tag": "liq"},
        ],
        field_opportunity_rows=[],
        repair_rows=[],
        region="USA",
        universe="TOP3000",
    )

    assert len(seeds) == 2
    assert {seed.domain for seed in seeds} == {"fundamental_quality", "liquidity_microstructure"}


def test_generate_evolutionary_candidates_uses_ab_hybrids_and_valid_fast_expr():
    candidates, summary = generate_evolutionary_candidates(
        active_rows=[
            {
                "alpha_id": "active_cf",
                "status": "ACTIVE",
                "expression": "rank(ts_rank(cashflow_op / assets, 120) - ts_rank(returns, 30))",
                "sharpe": 1.8,
                "fitness": 1.2,
                "turnover": 0.18,
            }
        ],
        candidate_rows=[
            {
                "expression": "rank(volume / adv20)",
                "tag": "liq",
                "source_family": "liquidity",
            },
            {
                "expression": "rank(anl4_af_eps_value / close)",
                "tag": "analyst",
                "source_family": "analyst",
            },
        ],
        field_opportunity_rows=[],
        repair_rows=[],
        target_count=4,
        region="USA",
        universe="TOP3000",
    )

    assert summary["ok"] is True
    assert candidates
    assert all(row["candidate_meta"]["evolutionary"] is True for row in candidates)
    assert any(row["candidate_meta"]["usa3000_bias"] is True for row in candidates)
    assert any("volume / adv20" in row["expression"] for row in candidates)
    for row in candidates:
        validate_wq_expression(row["expression"])


def test_generate_evolutionary_candidates_prioritizes_recent_usa3000_success_patterns():
    candidates, summary = generate_evolutionary_candidates(
        active_rows=[],
        candidate_rows=[
            {
                "expression": (
                    "rank((implied_volatility_call_90 - implied_volatility_put_90) / "
                    "(implied_volatility_call_90 + implied_volatility_put_90))"
                ),
                "tag": "option-skew",
            },
            {
                "expression": "rank(-ts_delta(close, 5) / close)",
                "tag": "price-reversal",
            },
            {
                "expression": "rank(-composite_factor_score_derivative)",
                "tag": "model-derivative",
            },
            {
                "expression": "rank((high - close) / (high - low) * volume / adv20)",
                "tag": "liquidity-pressure",
            },
            {
                "expression": "rank(ts_rank(cashflow_op / assets, 80))",
                "tag": "fundamental-quality",
            },
        ],
        field_opportunity_rows=[],
        repair_rows=[],
        target_count=4,
        region="USA",
        universe="TOP3000",
    )

    assert summary["ok"] is True
    assert candidates[0]["tag"].startswith("evo-options_positioning-price_reversal-regime_modifier")
    assert candidates[1]["tag"].startswith("evo-model_derivative-liquidity_microstructure-sector_neutral_modifier")
    for row in candidates:
        validate_wq_expression(row["expression"])
