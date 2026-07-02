"""Self-correlation repair template bank for WQ policy repair planning."""

from __future__ import annotations

from typing import Any

from .wq_repair_records import make_repair_candidate as _candidate


def self_corr_repairs(fields: set[str], tag: str, parent: list[Any]) -> list[dict]:
    out: list[dict] = []
    if {
        "equity",
        "cap",
        "forward_sales_to_price",
        "change_in_eps_surprise",
        "snt1_d1_netearningsrevision",
    } <= fields:
        out.extend([
            _candidate(
                "rank(0.32 * ts_rank(forward_book_value_to_price, 140) + "
                "0.22 * ts_rank(coefficient_variation_fy1_eps, 120) + "
                "0.18 * ts_rank(change_in_eps_surprise, 100) + "
                "0.16 * rank(ts_corr(vwap, volume, 120)) - "
                "0.12 * ts_rank(returns, 140))",
                tag=f"repair-{tag}-book-cv-eps-liquidity-no-snt",
                family="repair_self_corr_equity_sales_eps_rebuild",
                strategy="replace_equity_snt_with_forward_book_eps_dispersion",
                parent_alpha_ids=parent,
                rationale="Replace the high-self-correlation equity/SNT core with forward book, EPS dispersion, and a broad price-volume leg.",
            ),
            _candidate(
                "rank(group_neutralize(0.24 * ts_rank(equity / cap, 140) + "
                "0.24 * ts_rank(forward_sales_to_price, 150) + "
                "0.18 * ts_rank(coefficient_variation_fy1_eps, 120) + "
                "0.16 * rank(ts_corr(close, volume, 100)) - "
                "0.14 * ts_rank(returns, 150), sector))",
                tag=f"repair-{tag}-equity-forward-cv-sector-broad",
                family="repair_self_corr_equity_sales_eps_rebuild",
                strategy="slow_equity_forward_sales_with_price_volume_dispersion",
                parent_alpha_ids=parent,
                rationale="Keep one broad equity/value leg but remove the SNT revision leg and use slower sector-neutral dispersion.",
            ),
            _candidate(
                "rank(0.26 * ts_rank(forward_sales_to_price, 160) + "
                "0.22 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.20 * ts_rank(earnings_revision_magnitude, 140) + "
                "0.18 * rank(volume / adv20) - "
                "0.14 * ts_rank(returns, 160))",
                tag=f"repair-{tag}-forward-revision-liquidity-no-equity",
                family="repair_self_corr_equity_sales_eps_rebuild",
                strategy="replace_equity_eps_revision_with_model77_liquidity",
                parent_alpha_ids=parent,
                rationale="Move the signal into model77 revision/certainty plus liquidity dispersion to change the active field signature.",
            ),
        ])
    if "actual_eps_value_quarterly" in fields and (
        fields & {
            "implied_volatility_call_90",
            "implied_volatility_put_90",
            "implied_volatility_call_120",
            "implied_volatility_put_120",
        }
    ):
        out.extend([
            _candidate(
                "rank(0.30 * group_rank(ts_rank(actual_eps_value_quarterly / enterprise_value, 120), subindustry) + "
                "0.24 * ts_rank(coefficient_variation_fy1_eps, 80) + "
                "0.20 * ts_rank(forward_sales_to_price, 100) + "
                "0.14 * rank(ts_corr(vwap, volume, 90)) + "
                "0.12 * rank(-1 * ts_rank(pcr_oi_60, 80)))",
                tag=f"repair-{tag}-eps-ev-pcr60-forward-sales",
                family="repair_self_corr_eps_forward_options_flow",
                strategy="replace_iv90_iv120_micro_with_forward_pcr",
                parent_alpha_ids=parent,
                rationale="Replace the crowded IV90/IV120 and price microstructure legs with forward value and PCR flow.",
            ),
            _candidate(
                "rank(0.34 * ts_rank(actual_cashflow_per_share_value_quarterly / enterprise_value, 100) + "
                "0.24 * ts_rank(forward_book_value_to_price, 100) + "
                "0.18 * ts_rank(snt1_d1_netearningsrevision, 80) + "
                "0.14 * rank(ts_corr(vwap, volume, 90)) + "
                "0.10 * rank(-1 * ts_rank(pcr_vol_10, 80)))",
                tag=f"repair-{tag}-cashflow-forward-revision-pcrvol",
                family="repair_self_corr_cashflow_revision_flow",
                strategy="field_family_replacement",
                parent_alpha_ids=parent,
                rationale="Move the idea into cashflow, forward value, revision, and option-flow families.",
            ),
        ])
    if {
        "cashflow_op",
        "cashflow_efficiency_rank_derivative",
        "enterprise_value",
    } <= fields and (
        fields & {
            "implied_volatility_call_90",
            "implied_volatility_put_90",
            "implied_volatility_call_120",
            "implied_volatility_put_120",
        }
    ):
        iv120_ratio = "((implied_volatility_call_120 - implied_volatility_put_120) / (implied_volatility_call_120 + implied_volatility_put_120))"
        out.extend([
            _candidate(
                "rank(0.36 * ts_rank(forward_cash_flow_to_price, 160) + "
                "0.22 * ts_rank(forward_book_value_to_price, 140) + "
                "0.16 * rank(-1 * cashflow_efficiency_rank_derivative) + "
                "0.14 * rank(ts_corr(vwap, volume, 90)) - "
                "0.12 * ts_rank(returns, 120))",
                tag=f"repair-{tag}-cashflow-forwardbook-broad",
                family="repair_self_corr_cashflow_iv_near_threshold",
                strategy="replace_sparse_denominator_with_forward_broad_flow",
                parent_alpha_ids=parent,
                rationale="Keep cash-flow value exposure but avoid sparse denominator plus group repair risk.",
            ),
            _candidate(
                "rank(group_neutralize(0.30 * ts_rank(forward_cash_flow_to_price, 170) + "
                "0.22 * ts_rank(forward_book_value_to_price, 150) + "
                "0.16 * rank(-1 * cashflow_efficiency_rank_derivative) + "
                "0.14 * rank(ts_corr(vwap, volume, 100)) - "
                "0.12 * ts_rank(returns, 140), industry))",
                tag=f"repair-{tag}-cashflow-forwardbook-industry-broad",
                family="repair_self_corr_cashflow_iv_near_threshold",
                strategy="industry_neutral_forward_cashflow_broad_flow",
                parent_alpha_ids=parent,
                rationale="Use industry-neutral forward cashflow/book value with price-volume dispersion and no sparse denominator stack.",
            ),
            _candidate(
                "rank(0.76 * rank(0.44 * ts_rank(cashflow_op / enterprise_value, 120) + "
                "0.20 * rank(-1 * cashflow_efficiency_rank_derivative) + "
                "0.14 * rank(ts_corr(close, volume, 40)) + "
                "0.12 * ts_rank(forward_cash_flow_to_price, 120) - "
                "0.12 * ts_rank(returns, 60)) + "
                "0.24 * rank(-1 * ts_rank(pcr_oi_60, 90)))",
                tag=f"repair-{tag}-cashflow-core-pcr-overlay",
                family="repair_self_corr_cashflow_iv_near_threshold",
                strategy="replace_iv_overlay_with_pcr_flow",
                parent_alpha_ids=parent,
                rationale="Keep the strong cashflow core while replacing the high-SC IV90 overlay with slower PCR flow.",
            ),
            _candidate(
                f"rank(0.70 * rank(0.42 * ts_rank(cashflow_op / enterprise_value, 120) + "
                f"0.22 * rank(-1 * cashflow_efficiency_rank_derivative) + "
                f"0.14 * rank(ts_corr(vwap, volume, 60)) + "
                f"0.12 * rank(ts_mean({iv120_ratio}, 12)) - "
                f"0.12 * ts_rank(returns, 80)) + "
                f"0.30 * group_rank(ts_rank(forward_book_value_to_price, 120), industry))",
                tag=f"repair-{tag}-cashflow-forwardbook-iv120",
                family="repair_self_corr_cashflow_iv_near_threshold",
                strategy="dilute_iv_overlay_with_forward_book",
                parent_alpha_ids=parent,
                rationale="Dilute the IV overlay with forward book value while keeping the cashflow-efficiency core.",
            ),
            _candidate(
                f"rank(group_neutralize(0.38 * ts_rank(cashflow_op / enterprise_value, 120) + "
                f"0.20 * rank(-1 * cashflow_efficiency_rank_derivative) + "
                f"0.16 * rank(ts_corr(close, volume, 60)) + "
                f"0.14 * rank(ts_mean({iv120_ratio}, 10)) - "
                f"0.12 * ts_rank(returns, 80), industry))",
                tag=f"repair-{tag}-cashflow-iv120-industry-neutral",
                family="repair_self_corr_cashflow_iv_near_threshold",
                strategy="flatten_nested_iv_cashflow_structure",
                parent_alpha_ids=parent,
                rationale="Flatten the nested cashflow/IV structure and group-neutralize it to reduce self-correlation.",
            ),
        ])
    if {"implied_volatility_call_90", "implied_volatility_put_90"} <= fields:
        if {"cashflow_op", "forward_cash_flow_to_price"} <= fields or {
            "credit_risk_premium_indicator",
            "relative_valuation_rank_derivative",
        } <= fields:
            out.extend([
                _candidate(
                    "rank(group_neutralize(0.22 * ts_rank(forward_cash_flow_to_price, 170) + "
                    "0.15 * ts_rank(cashflow_op, 170) + "
                    "0.14 * rank(-1 * relative_valuation_rank_derivative) + "
                    "0.12 * rank(-1 * credit_risk_premium_indicator) + "
                    "0.11 * rank(ts_corr(vwap, volume, 120)) + "
                    "0.10 * rank(-1 * ts_rank(volume / adv20, 100)) - "
                    "0.14 * ts_rank(returns, 170), industry))",
                    tag=f"repair-{tag}-cashflow-credit-broad-noiv",
                    family="repair_self_corr_active_iv90_noiv_cashflow_credit",
                    strategy="remove_iv90_replace_with_cashflow_broad_dispersion",
                    parent_alpha_ids=parent,
                    rationale="Remove IV90/PCR and enterprise-value denominator while keeping one cashflow leg plus broad liquidity dispersion.",
                ),
                _candidate(
                    "rank(0.66 * rank(group_neutralize(0.20 * ts_rank(forward_cash_flow_to_price, 180) + "
                    "0.14 * ts_rank(cashflow_op, 180) + "
                    "0.12 * rank(-1 * credit_risk_premium_indicator) + "
                    "0.10 * rank(-1 * relative_valuation_rank_derivative) - "
                    "0.14 * ts_rank(returns, 180), sector)) + "
                    "0.19 * rank(ts_corr(vwap, volume, 120)) + "
                    "0.15 * rank(-1 * ts_rank(volume / adv20, 100)))",
                    tag=f"repair-{tag}-cashflow-credit-liquidity-noiv",
                    family="repair_self_corr_active_iv90_noiv_cashflow_credit",
                    strategy="remove_iv90_add_liquidity_no_sparse_stack",
                    parent_alpha_ids=parent,
                    rationale="Replace the sparse PCR/EV stack with broad price-volume dispersion around a single cashflow leg.",
                ),
                _candidate(
                    "rank(group_neutralize(0.24 * ts_rank(forward_cash_flow_to_price, 180) + "
                    "0.14 * rank(-1 * relative_valuation_rank_derivative) + "
                    "0.12 * rank(-1 * credit_risk_premium_indicator) + "
                    "0.12 * rank(-1 * ts_rank(pcr_oi_60, 110)) + "
                    "0.12 * rank(ts_corr(vwap, volume, 120)) - "
                    "0.14 * ts_rank(returns, 180), industry))",
                    tag=f"repair-{tag}-cashflow-credit-pcr-only-noiv",
                    family="repair_self_corr_active_iv90_noiv_cashflow_credit",
                    strategy="remove_iv90_use_single_pcr_leg_broad_dispersion",
                    parent_alpha_ids=parent,
                    rationale="Keep PCR decorrelation as the only sparse leg and disperse it with forward cashflow plus price-volume fields.",
                ),
                _candidate(
                    "rank(0.70 * rank(group_neutralize(0.24 * ts_rank(forward_cash_flow_to_price, 180) + "
                    "0.14 * rank(-1 * credit_risk_premium_indicator) + "
                    "0.12 * rank(-1 * relative_valuation_rank_derivative) - "
                    "0.14 * ts_rank(returns, 180), sector)) + "
                    "0.18 * rank(-1 * ts_rank(pcr_vol_10, 100)) + "
                    "0.12 * rank(volume / adv20))",
                    tag=f"repair-{tag}-cashflow-credit-pcrvol-only-noiv",
                    family="repair_self_corr_active_iv90_noiv_cashflow_credit",
                    strategy="remove_iv90_use_single_pcrvol_leg_broad_dispersion",
                    parent_alpha_ids=parent,
                    rationale="Use PCR volume as the only sparse leg, with forward cashflow and liquidity dispersion.",
                ),
                _candidate(
                    "rank(group_neutralize(0.20 * group_rank(ts_rank(forward_cash_flow_to_price, 150), industry) + "
                    "0.16 * group_rank(ts_rank(cashflow_op / enterprise_value, 120), subindustry) + "
                    "0.12 * rank(-1 * relative_valuation_rank_derivative) + "
                    "0.12 * rank(-1 * credit_risk_premium_indicator) + "
                    "0.12 * rank(-1 * ts_rank(pcr_oi_60, 90)) + "
                    "0.10 * rank(ts_corr(vwap, volume, 100)) - "
                    "0.14 * ts_rank(returns, 130), sector))",
                    tag=f"repair-{tag}-cashflow-credit-pcr-noiv",
                    family="repair_self_corr_active_iv90_noiv_cashflow_credit",
                    strategy="remove_iv90_replace_with_pcr_slow_cashflow",
                    parent_alpha_ids=parent,
                    rationale="Remove the crowded IV90 overlay and keep the cash-flow/credit core with slower PCR and volume flow legs.",
                ),
                _candidate(
                    "rank(0.70 * rank(group_neutralize(0.18 * ts_rank(forward_cash_flow_to_price, 160) + "
                    "0.16 * ts_rank(cashflow_op / enterprise_value, 140) + "
                    "0.12 * rank(-1 * credit_risk_premium_indicator) + "
                    "0.10 * rank(-1 * relative_valuation_rank_derivative) - "
                    "0.14 * ts_rank(returns, 150), industry)) + "
                    "0.18 * rank(-1 * ts_rank(pcr_vol_10, 80)) + "
                    "0.12 * rank(volume / adv20))",
                    tag=f"repair-{tag}-cashflow-credit-pcrvol-liquidity",
                    family="repair_self_corr_active_iv90_noiv_cashflow_credit",
                    strategy="remove_iv90_add_pcrvol_liquidity",
                    parent_alpha_ids=parent,
                    rationale="Replace IV90 with PCR volume and liquidity dispersion while slowing the cash-flow credit core.",
                ),
            ])
        if {"actual_sales_value_quarterly", "forward_sales_to_price"} <= fields:
            out.append(_candidate(
                "rank(group_neutralize(0.22 * ts_rank(ts_backfill(actual_sales_value_quarterly, 140) / cap, 170) + "
                "0.20 * ts_rank(forward_sales_to_price, 170) + "
                "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.12 * ts_rank(earnings_revision_magnitude, 150) + "
                "0.12 * rank(ts_corr(vwap, volume, 120)) - "
                "0.16 * ts_rank(returns, 170), industry))",
                tag=f"repair-{tag}-sales-revision-broad-noiv",
                family="repair_self_corr_active_iv90_noiv_sales_revision",
                strategy="remove_iv90_replace_with_sales_cap_broad_dispersion",
                parent_alpha_ids=parent,
                rationale="Remove IV90/PCR and enterprise-value denominator from the sales/revision repair.",
            ))
            out.append(_candidate(
                "rank(group_rank(0.20 * ts_rank(ts_backfill(actual_sales_value_quarterly, 120) / enterprise_value, 150) + "
                "0.18 * ts_rank(forward_sales_to_price, 150) + "
                "0.14 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.12 * ts_rank(earnings_revision_magnitude, 120) + "
                "0.12 * rank(-1 * ts_rank(pcr_oi_60, 90)) - "
                "0.18 * ts_rank(returns, 130), industry))",
                tag=f"repair-{tag}-sales-revision-pcr-noiv",
                family="repair_self_corr_active_iv90_noiv_sales_revision",
                strategy="remove_iv90_replace_with_pcr_sales_revision",
                parent_alpha_ids=parent,
                rationale="Remove IV90 from the sales/revision family and use a slower PCR flow leg for decorrelation.",
            ))
        if "anl4_adjusted_netincome_ft" in fields and "forward_cash_flow_to_price" in fields:
            out.append(_candidate(
                "rank(group_neutralize(0.22 * ts_rank(anl4_adjusted_netincome_ft / cap, 130) + "
                "0.20 * ts_rank(forward_cash_flow_to_price, 170) + "
                "0.14 * rank(-1 * credit_risk_premium_indicator) + "
                "0.12 * rank(-1 * relative_valuation_rank_derivative) + "
                "0.10 * rank(ts_corr(vwap, volume, 120)) + "
                "0.08 * rank(-1 * ts_rank(close / vwap, 100)) - "
                "0.16 * ts_rank(returns, 170), sector))",
                tag=f"repair-{tag}-netincome-forwardcf-broad-noiv",
                family="repair_self_corr_active_iv90_noiv_netincome_forwardcf",
                strategy="remove_iv90_replace_with_netincome_broad_dispersion",
                parent_alpha_ids=parent,
                rationale="Remove IV90/PCR from the net-income repair and disperse with price-volume legs.",
            ))
            out.append(_candidate(
                "rank(group_neutralize(0.20 * ts_rank(anl4_adjusted_netincome_ft / cap, 110) + "
                "0.18 * ts_rank(forward_cash_flow_to_price, 150) + "
                "0.14 * rank(-1 * credit_risk_premium_indicator) + "
                "0.12 * rank(-1 * relative_valuation_rank_derivative) + "
                "0.12 * rank(-1 * ts_rank(pcr_oi_60, 90)) + "
                "0.08 * rank(-1 * ts_rank(close / vwap, 80)) - "
                "0.16 * ts_rank(returns, 130), sector))",
                tag=f"repair-{tag}-netincome-forwardcf-pcr-noiv",
                family="repair_self_corr_active_iv90_noiv_netincome_forwardcf",
                strategy="remove_iv90_replace_with_pcr_netincome_forwardcf",
                parent_alpha_ids=parent,
                rationale="Remove IV90 from the net-income/forward-cash-flow blend and add PCR flow plus slower reversal.",
            ))
    if {"actual_sales_value_quarterly", "change_in_eps_surprise"} <= fields:
        out.extend([
            _candidate(
                "rank(0.34 * ts_rank(forward_sales_to_price, 100) + "
                "0.24 * ts_rank(coefficient_variation_fy1_eps, 80) + "
                "0.22 * ts_rank(snt1_d1_netearningsrevision, 80) + "
                "0.20 * rank(-1 * ts_rank(pcr_oi_60, 60)))",
                tag=f"repair-{tag}-forward-sales-cv-revision-pcr60",
                family="repair_self_corr_forward_revision_flow",
                strategy="replace_sales_eps_micro_core",
                parent_alpha_ids=parent,
                rationale="Replace actual sales/EPS and close-volume crowding with forward, dispersion, revision, and PCR fields.",
            ),
            _candidate(
                "rank(0.42 * group_rank(ts_rank(actual_cashflow_per_share_value_quarterly / enterprise_value, 100), industry) + "
                "0.24 * ts_rank(forward_book_value_to_price, 100) + "
                "0.18 * ts_rank(snt1_d1_netearningsrevision, 80) - "
                "0.16 * ts_rank(returns, 80))",
                tag=f"repair-{tag}-cashflow-forward-book-revision",
                family="repair_self_corr_cashflow_forward_revision",
                strategy="cashflow_value_replacement",
                parent_alpha_ids=parent,
                rationale="Use cashflow per share and forward book instead of the prior sales/EPS microstructure template.",
            ),
        ])
    if {
        "actual_sales_value_quarterly",
        "earnings_momentum_composite_score",
        "enterprise_value",
        "vwap",
        "volume",
    } <= fields:
        out.extend([
            _candidate(
                "rank(0.30 * ts_rank(forward_sales_to_price, 150) + "
                "0.24 * ts_rank(change_in_eps_surprise, 110) + "
                "0.20 * ts_rank(coefficient_variation_fy1_eps, 120) + "
                "0.16 * rank(ts_corr(close, volume, 120)) - "
                "0.12 * ts_rank(returns, 150))",
                tag=f"repair-{tag}-forward-eps-cv-liquidity-no-ev",
                family="repair_self_corr_sales_earnmom_rebuild",
                strategy="replace_sales_ev_earnmom_with_forward_eps_dispersion",
                parent_alpha_ids=parent,
                rationale="Replace the crowded sales/EV/earnings-momentum signature with forward sales, EPS surprise, EPS dispersion, and price-volume flow.",
            ),
            _candidate(
                "rank(group_neutralize(0.24 * ts_rank(ts_backfill(actual_sales_value_quarterly, 140) / cap, 170) + "
                "0.22 * ts_rank(forward_sales_to_price, 170) + "
                "0.18 * rank(-1 * earnings_certainty_rank_derivative) + "
                "0.14 * ts_rank(earnings_revision_magnitude, 150) + "
                "0.10 * rank(ts_corr(vwap, volume, 120)) - "
                "0.14 * ts_rank(returns, 170), industry))",
                tag=f"repair-{tag}-sales-cap-revision-broad-no-ev",
                family="repair_self_corr_sales_earnmom_rebuild",
                strategy="replace_ev_denominator_and_earnmom_with_revision_breadth",
                parent_alpha_ids=parent,
                rationale="Keep sales information but normalize by cap and add broad revision/liquidity legs instead of EV and PCR.",
            ),
            _candidate(
                "rank(0.26 * group_rank(ts_rank(actual_cashflow_per_share_value_quarterly / cap, 150), industry) + "
                "0.22 * ts_rank(forward_book_value_to_price, 140) + "
                "0.18 * ts_rank(snt1_d1_netearningsrevision, 100) + "
                "0.16 * ts_rank(coefficient_variation_fy1_eps, 100) + "
                "0.10 * rank(ts_corr(vwap, volume, 120)) - "
                "0.10 * ts_rank(returns, 140))",
                tag=f"repair-{tag}-cashflow-book-broad-no-sparse-stack",
                family="repair_self_corr_cashflow_book_revision",
                strategy="cashflow_forward_broad_rebuild_no_sparse_stack",
                parent_alpha_ids=parent,
                rationale="Move the sales/earnings-momentum template into cashflow/book/revision without EV/PCR sparse stacking.",
            ),
            _candidate(
                "rank(0.28 * group_rank(ts_rank(ts_backfill(forward_sales_to_price, 120), 120), industry) + "
                "0.22 * ts_rank(snt1_d1_netearningsrevision, 100) + "
                "0.18 * ts_rank(coefficient_variation_fy1_eps, 100) + "
                "0.16 * rank(-1 * ts_rank(ts_backfill(pcr_oi_60, 120), 90)) + "
                "0.16 * ts_rank(forward_book_value_to_price, 120))",
                tag=f"repair-{tag}-forward-revision-dispersion-pcr",
                family="repair_self_corr_forward_revision_dispersion",
                strategy="replace_sales_earnmom_micro_with_forward_revision",
                parent_alpha_ids=parent,
                rationale="Replace the high-SC sales/earnings-momentum/vwap-volume core with forward value, revision, dispersion, and PCR flow.",
            ),
            _candidate(
                "rank(0.24 * group_rank(ts_rank(actual_cashflow_per_share_value_quarterly / enterprise_value, 120), subindustry) + "
                "0.20 * group_rank(ts_rank(forward_book_value_to_price, 120), industry) + "
                "0.18 * ts_rank(snt1_d1_netearningsrevision, 100) + "
                "0.16 * rank(-1 * ts_rank(pcr_vol_10, 80)) + "
                "0.12 * ts_rank(coefficient_variation_fy1_eps, 100) - "
                "0.10 * ts_rank(returns, 120))",
                tag=f"repair-{tag}-cashflow-book-revision-pcrvol",
                family="repair_self_corr_cashflow_book_revision",
                strategy="cashflow_forward_rebuild",
                parent_alpha_ids=parent,
                rationale="Move the idea into cashflow, forward book, analyst revision, and option-volume flow with a slow returns control.",
            ),
        ])
    out.append(_candidate(
        "rank(0.38 * ts_rank(forward_sales_to_price, 120) + "
        "0.26 * ts_rank(coefficient_variation_fy1_eps, 100) + "
        "0.20 * rank(ts_corr(vwap, volume, 100)) + "
        "0.12 * rank(volume / adv20) - "
        "0.10 * ts_rank(returns, 120))",
        tag=f"repair-{tag}-minimal-forward-dispersion-liquidity",
        family="repair_self_corr_minimal_orthogonal",
        strategy="minimal_orthogonal_rebuild_no_pcr",
        parent_alpha_ids=parent,
        rationale="Minimal rebuild using low-active forward sales, EPS dispersion, and broad price-volume/liquidity legs without PCR.",
    ))
    return out
