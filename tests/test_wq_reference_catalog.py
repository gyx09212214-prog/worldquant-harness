from pathlib import Path

from worldquant_harness.wq_reference_catalog import reference_catalog_status, search_fields


def test_reference_catalog_status_reads_bundled_summary():
    status = reference_catalog_status()

    assert status["ok"] is True
    assert status["summary"]["field_count"] == 4367
    assert status["summary"]["category_counts"]["fundamental"] == 1652
    assert Path(status["files"]["wq_usa_top3000_delay1_data_fields.json"]["path"]).is_file()


def test_reference_catalog_search_filters_fields():
    rows = search_fields("cashflow", category="fundamental", limit=5)

    assert rows
    assert len(rows) <= 5
    assert all("id" in row for row in rows)
