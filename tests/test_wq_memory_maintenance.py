from worldquant_harness.wq_memory_maintenance import memory_maintenance_report, render_memory_maintenance_markdown


def test_memory_maintenance_finds_absorption_candidates():
    rows = [
        {
            "failure_kind": "self_correlation_fail",
            "field_signature": "cashflow,open",
            "expression": "rank(open)",
        },
        {
            "failure_kind": "self_correlation_fail",
            "field_signature": "cashflow,open",
            "expression": "rank(ts_rank(open, 5))",
        },
        {
            "failure_kind": "self_correlation_fail",
            "field_signature": "cashflow,open",
            "expression": "rank(ts_rank(open, 20))",
        },
        {
            "failure_kind": "too_similar_to_real_or_virtual_active",
            "field_signature": "volume",
            "deprecated": True,
        },
    ]

    report = memory_maintenance_report(rows, compress_threshold=10, absorb_threshold=3)
    markdown = render_memory_maintenance_markdown(report)

    assert report["active_row_count"] == 3
    assert report["deprecated_row_count"] == 1
    assert report["absorption_candidates"]
    assert report["absorption_candidates"][0]["failure_kind"] == "self_correlation_fail"
    assert "self_correlation_fail" in markdown
