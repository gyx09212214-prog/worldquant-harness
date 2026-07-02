"""Shared constants for the WQ agent workflow."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


CONFIRMED_READY = "confirmed_ready"


SUBMIT_PROBE_NEEDED = "submit_probe_needed"


NEAR_MISS_REPAIR = "near_miss_repair"


HARD_FAIL = "hard_fail"


ACTIVE_OR_SUBMITTED = "active_or_submitted"


INFRA_TIMEOUT = "infra_timeout"


BLOCKED_REPAIR_SOURCE_FAMILIES = {
    "repair_metric_threshold_settings",
    "repair_metric_threshold_smoothing",
    "repair_concentration_generic",
}


BLOCKED_REPAIR_MUTATION_STRATEGIES = {
    "metric_near_miss_decay_truncation_retest",
    "metric_near_miss_max_position_retest",
    "metric_near_miss_smooth_group_neutralize",
    "smooth_group_neutralize",
}


SUCCESS_FAMILY_SEEDS = [
    {
        "expression": "rank(ts_rank(ebit / enterprise_value, 60) - ts_rank(returns, 20))",
        "tag": "legacy-value-reversal-ebit-ev",
        "source_family": "legacy_fundamental_reversal",
    },
    {
        "expression": "rank(ts_mean(ts_rank(vwap / close, 20), 3) - ts_rank(returns, 20))",
        "tag": "legacy-vwap-close-reversal",
        "source_family": "legacy_price_volume_reversal",
    },
    {
        "expression": "rank((high - close) / (high - low) * volume / ts_mean(volume, 20))",
        "tag": "legacy-intraday-volume-pressure",
        "source_family": "legacy_price_volume_reversal",
    },
    {
        "expression": (
            "rank(0.50 * rank(-ts_delta(close, 3) / close) + "
            "0.30 * rank(ts_mean((implied_volatility_call_120 - implied_volatility_put_120) / "
            "(implied_volatility_call_120 + implied_volatility_put_120), 5)) + "
            "0.20 * rank(-1 * ts_rank(cash_burn_rate, 60)))"
        ),
        "tag": "legacy-option-reversal-cashburn",
        "source_family": "legacy_option_reversal",
    },
]
