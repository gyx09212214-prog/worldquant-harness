import json
from pathlib import Path

from quantgpt.wq_iqc_tracker import TrackerConfig, build_novelty_audit, build_tracker, write_tracker_outputs


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _submit_run(root: Path, name: str, alpha_id: str, expression: str, *, ok: bool = True) -> None:
    run = root / name
    _write_jsonl(
        run / "review_queue.jsonl",
        [
            {
                "alpha_id": alpha_id,
                "tag": f"tag-{alpha_id}",
                "expression": expression,
                "source_fields": ["close", "returns"],
                "sharpe": 1.4,
                "fitness": 1.1,
                "turnover": 0.2,
                "result": {
                    "settings": {
                        "region": "USA",
                        "universe": "TOP3000",
                        "delay": 1,
                        "decay": 8,
                        "neutralization": "SUBINDUSTRY",
                        "truncation": 0.08,
                    }
                },
            }
        ],
    )
    _write_json(
        run / "summary.json",
        {
            "submission": {
                "result": {
                    "results": {
                        alpha_id: {
                            "ok": ok,
                            "platform_status": "ACTIVE" if ok else "ERROR",
                            "final_status": "ACTIVE" if ok else "ERROR",
                        }
                    }
                }
            }
        },
    )


def test_tracker_counts_only_active_after_anchor(tmp_path):
    run_root = tmp_path / "runs"
    _submit_run(run_root, "01_anchor", "RRrQo83z", "rank(close)")
    _submit_run(run_root, "02_new", "NEW1", "rank(earnings_momentum_composite_score)")
    _submit_run(run_root, "03_failed", "BAD1", "rank(volume)", ok=False)
    _write_json(
        run_root / "latest" / "active_inventory.json",
        {
            "active": [
                {"alpha_id": "RRrQo83z", "status": "ACTIVE", "expression": "rank(close)"},
                {"alpha_id": "NEW1", "status": "ACTIVE", "expression": "rank(earnings_momentum_composite_score)"},
            ]
        },
    )

    summary = build_tracker(
        TrackerConfig(
            run_root=run_root,
            db_path=tmp_path / "missing.db",
            jsonl_output=tmp_path / "tracker.jsonl",
            markdown_output=tmp_path / "tracker.md",
            round_start_after_alpha="RRrQo83z",
        )
    )

    counted = [row["alpha_id"] for row in summary["records"] if row["counted_for_round"]]
    assert counted == ["NEW1"]
    assert summary["new_active_count"] == 1


def test_tracker_writes_jsonl_and_markdown(tmp_path):
    run_root = tmp_path / "runs"
    _submit_run(run_root, "01_anchor", "RRrQo83z", "rank(close)")
    _submit_run(run_root, "02_new", "NEW1", "rank(actual_eps_value_quarterly)")
    config = TrackerConfig(
        run_root=run_root,
        db_path=tmp_path / "missing.db",
        jsonl_output=tmp_path / "tracker.jsonl",
        markdown_output=tmp_path / "tracker.md",
        round_start_after_alpha="RRrQo83z",
    )

    write_tracker_outputs(build_tracker(config), config)

    assert "NEW1" in config.jsonl_output.read_text(encoding="utf-8")
    assert "Target: 1/10" in config.markdown_output.read_text(encoding="utf-8")


def test_novelty_audit_detects_mpx_signature():
    records = [
        {
            "alpha_id": "MPX",
            "expression": (
                "rank(multi_factor_acceleration_score_derivative + "
                "fifty_to_two_hundred_day_price_ratio + returns + volume + vwap)"
            ),
        }
    ]

    novelty = build_novelty_audit(records, [])

    assert novelty["mpx_signature_active_alpha_ids"] == ["MPX"]
    assert novelty["strict_ledger_similarity_cutoff"] == 0.62
