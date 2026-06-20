from quantgpt.wq_alpha_detail import extract_pnl_curve, render_probe_markdown, summarize_alpha_probe


def test_extract_pnl_curve_from_chart_payload():
    payload = {
        "id": "alpha1",
        "pnlChart": {
            "points": [
                {"date": "2026-01-01", "pnl": 0.1},
                {"date": "2026-01-02", "pnl": -0.2},
            ]
        },
    }

    curve = extract_pnl_curve(payload)

    assert len(curve) == 2
    assert curve[0]["date"] == "2026-01-01"
    assert curve[0]["pnl"] == 0.1


def test_summarize_alpha_probe_reports_best_pnl_endpoint():
    probe = {
        "ok": True,
        "alpha_id": "alpha1",
        "read_only": True,
        "endpoints": [
            {"ok": True, "path": "/alphas/alpha1", "status_code": 200, "data": {"status": "ACTIVE"}},
            {
                "ok": True,
                "path": "/alphas/alpha1/pnl",
                "status_code": 200,
                "data": {"data": [["2026-01-01", 1.0], ["2026-01-02", 1.2]]},
            },
        ],
    }

    summary = summarize_alpha_probe(probe)
    markdown = render_probe_markdown([summary])

    assert summary["read_only"] is True
    assert summary["pnl_curve_found"] is True
    assert summary["pnl_points"] == 2
    assert summary["pnl_curve_path"] == "/alphas/alpha1/pnl"
    assert "alpha1" in markdown


def test_summarize_alpha_probe_reads_recordset_records_payload():
    probe = {
        "ok": True,
        "alpha_id": "alpha1",
        "read_only": True,
        "endpoints": [
            {
                "ok": True,
                "path": "/alphas/alpha1/recordsets/daily-pnl",
                "status_code": 200,
                "data": {
                    "schema": {"name": "daily-pnl", "title": "Daily PnL"},
                    "records": [["2019-01-02", 32427.0], ["2019-01-03", -1200.0]],
                },
            },
        ],
    }

    summary = summarize_alpha_probe(probe)

    assert summary["pnl_curve_found"] is True
    assert summary["pnl_curve_path"] == "/alphas/alpha1/recordsets/daily-pnl"
    assert summary["pnl_curve"][0]["date"] == "2019-01-02"
    assert summary["pnl_curve"][0]["pnl"] == 32427.0
