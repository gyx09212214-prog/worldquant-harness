from scripts.generate_wq_submit5_more_existing_diverse_candidates import (
    build_blocklist,
    candidate_from_platform,
)


def _platform_row(alpha_id: str, expression: str) -> dict:
    return {
        "alpha_id": alpha_id,
        "status": "UNSUBMITTED",
        "regular": {"code": expression},
        "sharpe": 1.8,
        "fitness": 1.25,
        "returns": 0.05,
        "turnover": 0.25,
    }


def test_blocklist_filters_failed_ids_correlated_anchors_and_sc_field_siblings():
    blocklist = build_blocklist([
        {
            "alpha_id": "failed_alpha",
            "candidate_meta": {"platform_alpha_id": "failed_platform"},
            "expression": "rank(ts_rank(news_open_gap, 20) - ts_rank(volume, 20))",
            "final_status": "SC_FAIL",
            "live_precheck": {
                "raw_check": {
                    "is": {
                        "selfCorrelated": {
                            "schema": {
                                "properties": [
                                    {"name": "id"},
                                    {"name": "unused"},
                                    {"name": "instrument"},
                                    {"name": "region"},
                                    {"name": "universe"},
                                    {"name": "correlation"},
                                ]
                            },
                            "records": [["anchor_alpha", None, "EQUITY", "USA", "TOP3000", 0.82]],
                        }
                    }
                }
            },
        }
    ])

    assert "failed_alpha" in blocklist["alpha_ids"]
    assert "failed_platform" in blocklist["alpha_ids"]
    assert "anchor_alpha" in blocklist["anchor_ids"]

    assert candidate_from_platform(
        _platform_row("failed_platform", "rank(ts_rank(news_open_gap, 20) - ts_rank(volume, 20))"),
        set(),
        set(),
        [],
        blocklist=blocklist,
    ) is None
    assert candidate_from_platform(
        _platform_row("anchor_alpha", "rank(ts_rank(dividend, 63))"),
        set(),
        set(),
        [],
        blocklist=blocklist,
    ) is None
    assert candidate_from_platform(
        _platform_row("sibling_alpha", "rank(ts_rank(news_open_gap, 21) - ts_rank(volume, 21))"),
        set(),
        set(),
        [],
        blocklist=blocklist,
    ) is None

    good = candidate_from_platform(
        _platform_row("fresh_alpha", "rank(ts_rank(actual_dividend_value_quarterly, 63))"),
        set(),
        set(),
        [],
        blocklist=blocklist,
    )
    assert good is not None
    assert good["alpha_id"] == "fresh_alpha"


def test_candidate_filters_returns_anchor_and_large_field_basket():
    assert candidate_from_platform(
        _platform_row("returns_alpha", "rank(ts_rank(returns, 120) + ts_rank(close, 20))"),
        set(),
        set(),
        [],
        max_returns_references=0,
    ) is None

    assert candidate_from_platform(
        _platform_row("wide_alpha", "rank(open + close + high + low + volume + vwap)"),
        set(),
        set(),
        [],
        max_field_count=4,
    ) is None

    compact = candidate_from_platform(
        _platform_row("compact_alpha", "rank(ts_rank(actual_dividend_value_quarterly, 63))"),
        set(),
        set(),
        [],
        max_returns_references=0,
        max_field_count=4,
    )
    assert compact is not None
