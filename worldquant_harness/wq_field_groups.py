"""Shared WQ field groups used for candidate family classification."""

from __future__ import annotations

import re

PRICE_FIELDS = {"close", "open", "high", "low", "returns", "vwap"}
LIQUIDITY_FIELDS = {"volume", "adv20", "adv60", "adv120", "turnover"}
GROUP_FIELDS = {"industry", "sector", "subindustry", "market"}
PRICE_VOLUME_DISPERSION_FIELDS = {"adv20", "close", "high", "low", "open", "volume", "vwap"}
GROUP_DISTRIBUTION_OPERATORS = {
    "group_neutralize",
    "group_rank",
    "group_zscore",
}

OPTION_FIELDS = {
    "implied_volatility_call_30",
    "implied_volatility_call_60",
    "implied_volatility_call_90",
    "implied_volatility_call_120",
    "implied_volatility_call_180",
    "implied_volatility_put_30",
    "implied_volatility_put_60",
    "implied_volatility_put_90",
    "implied_volatility_put_120",
    "implied_volatility_put_180",
    "pcr_oi_5",
    "pcr_oi_10",
    "pcr_oi_20",
    "pcr_oi_30",
    "pcr_oi_60",
    "pcr_oi_90",
    "pcr_oi_120",
    "pcr_oi_180",
    "pcr_volume_5",
    "pcr_volume_10",
    "pcr_volume_20",
    "pcr_volume_30",
    "pcr_volume_60",
    "pcr_volume_90",
    "pcr_volume_120",
    "pcr_volume_180",
}

PLATFORM_DERIVATIVE_FIELDS = {
    "analyst_revision_rank_derivative",
    "cashflow_efficiency_rank_derivative",
    "composite_factor_score_derivative",
    "earnings_certainty_rank_derivative",
    "growth_potential_rank_derivative",
    "multi_factor_acceleration_score_derivative",
    "relative_valuation_rank_derivative",
}
MODEL_DERIVATIVE_FIELDS = set(PLATFORM_DERIVATIVE_FIELDS)

PLATFORM_FORWARD_VALUE_FIELDS = {
    "forward_book_value_to_price",
    "forward_cash_flow_to_price",
    "forward_earnings_yield",
    "forward_sales_to_price",
}

PLATFORM_ANALYST_REVISION_FIELDS = {
    "actual_eps_value_quarterly",
    "anl4_af_eps_value",
    "anl4_adjusted_netincome_ft",
    "anl4_afv4_eps_mean",
    "change_in_eps_surprise",
    "snt1_d1_netearningsrevision",
}

PLATFORM_CASHFLOW_FIELDS = {
    "actual_cashflow_per_share_value_quarterly",
    "cashflow",
    "cashflow_fin",
    "cashflow_op",
}

ANALYST_FIELDS = {
    "actual_eps_value_quarterly",
    "actual_cashflow_per_share_value_quarterly",
    "anl4_af_eps_value",
    "anl4_afv4_eps_mean",
    "anl4_adjusted_netincome_ft",
    "change_in_eps_surprise",
    "earnings_momentum_composite_score",
    "snt1_d1_netearningsrevision",
}

FUNDAMENTAL_FIELDS = {
    "assets",
    "cap",
    "capex",
    "cashflow",
    "cashflow_fin",
    "cashflow_op",
    "debt",
    "debt_lt",
    "ebit",
    "ebitda",
    "enterprise_value",
    "gross_profit",
    "income",
    "liabilities",
    "net_income",
    "operating_income",
    "receivables",
    "sales",
}

RESEARCH_SPARSE_CONCENTRATION_FIELDS = {
    "actual_dividend_value_quarterly",
    "cashflow_op",
    "dividends_to_gross_profit",
    "enterprise_value",
}
REPAIR_SPARSE_CONCENTRATION_FIELDS = {
    "actual_dividend_value_quarterly",
    "actual_cashflow_per_share_value_quarterly",
    "cashflow",
    "cashflow_fin",
    "cashflow_op",
    "dividends_to_gross_profit",
    "enterprise_value",
}
SPARSE_CONCENTRATION_PREFIXES = ("pcr_",)
BROAD_DISPERSION_FIELDS = {
    "adv20",
    "cap",
    "close",
    "high",
    "low",
    "open",
    "volume",
    "vwap",
    "forward_book_value_to_price",
    "forward_cash_flow_to_price",
    "forward_sales_to_price",
    "coefficient_variation_fy1_eps",
    "credit_risk_premium_indicator",
    "earnings_certainty_rank_derivative",
    "relative_valuation_rank_derivative",
}
BROAD_DISPERSION_DATASETS = {"model16", "model77"}


def is_sparse_concentration_field(
    field: str,
    *,
    sparse_fields: set[str] = REPAIR_SPARSE_CONCENTRATION_FIELDS,
    sparse_prefixes: tuple[str, ...] = SPARSE_CONCENTRATION_PREFIXES,
) -> bool:
    text = str(field or "")
    if text in sparse_fields:
        return True
    if any(text.startswith(prefix) for prefix in sparse_prefixes):
        return True
    return "dividend" in text


def field_used_as_denominator(expression: str, field: str) -> bool:
    compact = re.sub(r"\s+", "", str(expression or "").lower())
    escaped_field = re.escape(str(field or "").lower())
    return bool(re.search(rf"/(?:ts_backfill\()?{escaped_field}\b", compact))


def is_price_volume_dispersion_field(field: str) -> bool:
    text = str(field or "")
    return text in PRICE_VOLUME_DISPERSION_FIELDS or bool(re.fullmatch(r"adv\d+", text or ""))


def is_broad_dispersion_field(
    field: str,
    *,
    spec: dict | None = None,
    sparse_fields: set[str] = REPAIR_SPARSE_CONCENTRATION_FIELDS,
) -> bool:
    text = str(field or "")
    if text in GROUP_FIELDS or text == "returns" or is_sparse_concentration_field(text, sparse_fields=sparse_fields):
        return False
    if text in BROAD_DISPERSION_FIELDS or re.fullmatch(r"adv\d+", text or ""):
        return True
    if not isinstance(spec, dict):
        return False
    try:
        coverage = float(spec.get("coverage"))
    except (TypeError, ValueError):
        coverage = None
    if coverage is not None and coverage < 0.9:
        return False
    dataset = str(spec.get("dataset_id") or "")
    domain = str(spec.get("domain") or "")
    category = str(spec.get("category") or "")
    return dataset in BROAD_DISPERSION_DATASETS or domain in {"pv", "core", "model"} or category in {"pv", "model"}
