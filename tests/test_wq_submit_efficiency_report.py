import json

from scripts import wq_submit_efficiency_report as efficiency_report


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def test_efficiency_report_writes_funnel_markdown_and_events(tmp_path):
    run_dir = tmp_path / "experiment" / "presubmit_run"
    _write_json(
        run_dir / "manifest.json",
        {
            "config": {
                "account": "primary",
                "region": "USA",
                "universe": "TOP3000",
                "delay": 1,
                "decay": 8,
                "neutralization": "SUBINDUSTRY",
                "truncation": 0.08,
            }
        },
    )
    _write_jsonl(
        run_dir.parent / "candidate_specs.jsonl",
        [
            {"expression": "rank(close)", "tag": "ready", "source_family": "price"},
            {"expression": "rank(volume)", "tag": "reject", "source_family": "volume"},
        ],
    )
    _write_jsonl(
        run_dir / "simulation_results.jsonl",
        [
            {
                "expression": "rank(close)",
                "tag": "ready",
                "source_family": "price",
                "alpha_id": "alpha_ready",
                "status": "pending_correlation_check",
                "sharpe": 1.8,
                "fitness": 1.2,
                "turnover": 0.2,
                "effective_simulation_settings": {"region": "USA", "universe": "TOP3000", "delay": 1, "decay": 8},
            },
            {
                "expression": "rank(volume)",
                "tag": "reject",
                "source_family": "volume",
                "alpha_id": "alpha_reject",
                "status": "pending_correlation_check",
                "sharpe": 1.5,
                "fitness": 1.0,
                "turnover": 0.3,
                "effective_simulation_settings": {"region": "USA", "universe": "TOP3000", "delay": 1, "decay": 8},
            },
        ],
    )
    _write_jsonl(
        run_dir / "review_queue.jsonl",
        [
            {
                "expression": "rank(close)",
                "tag": "ready",
                "source_family": "price",
                "alpha_id": "alpha_ready",
                "triage_bucket": "confirmed_ready",
                "api_check_status": "api_check_readable",
                "sc_result": "PASS",
            },
            {
                "expression": "rank(volume)",
                "tag": "reject",
                "source_family": "volume",
                "alpha_id": "alpha_reject",
                "triage_bucket": "near_miss_repair",
                "api_check_status": "self_correlation_fail",
                "sc_result": "FAIL",
            },
        ],
    )
    _write_jsonl(
        run_dir / "presubmit_ready_sequential.jsonl",
        [
            {
                "expression": "rank(close)",
                "tag": "ready",
                "source_family": "price",
                "alpha_id": "alpha_ready",
                "triage_bucket": "confirmed_ready",
            }
        ],
    )
    _write_jsonl(
        run_dir / "presubmit_rejected.jsonl",
        [
            {
                "expression": "rank(volume)",
                "tag": "reject",
                "source_family": "volume",
                "alpha_id": "alpha_reject",
                "presubmit_reject_reason": "self_correlation_value_above_strict_cutoff",
            }
        ],
    )

    output = tmp_path / "efficiency.json"
    markdown = tmp_path / "efficiency.md"
    events = tmp_path / "events.jsonl"
    code = efficiency_report.main([
        "--current-run-dirs",
        str(run_dir),
        "--output",
        str(output),
        "--markdown-output",
        str(markdown),
        "--events-output",
        str(events),
    ])

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert payload["current"]["funnel"]["candidates"] == 2
    assert payload["current"]["funnel"]["simulated"] == 2
    assert payload["current"]["funnel"]["ready"] == 1
    assert payload["current"]["metrics"]["ready_per_100_simulations"] == 50.0
    assert payload["current"]["leaderboards"]["source_family"][0]["source_family"] == "price"
    assert "WQ Alpha Submit Efficiency" in markdown.read_text(encoding="utf-8")
    assert "candidate_ready" in events.read_text(encoding="utf-8")
