import json
from pathlib import Path

from scripts.build_public_visual_pack import build_public_visual_pack


def test_public_visual_pack_generates_sanitized_svg_and_guide(tmp_path):
    source = tmp_path / "public_harness_demo"
    _write_demo_source(source)
    output_dir = tmp_path / "docs" / "images"
    report = tmp_path / "docs" / "VISUAL_GUIDE.md"

    result = build_public_visual_pack(source, output_dir, report)

    assert result["ok"] is True
    assert result["data"]["counts"]["candidates"] == 5
    assert result["data"]["counts"]["ready"] == 1
    assert result["data"]["quality_available"] is True
    assert result["data"]["profile_available"] is True

    expected = {
        "worldquant-harness-overview.svg",
        "public-demo-trace.svg",
        "memory-feedback-graph.svg",
        "factor-map-snapshot.svg",
        "quality-review-dashboard.svg",
        "profile-evolution-timeline.svg",
        "harness-artifact-lifecycle.svg",
        "submit-boundary.svg",
        "release-safety-boundary.svg",
    }
    assert expected == {path.name for path in output_dir.glob("*.svg")}
    assert report.is_file()

    combined = "\n".join(path.read_text(encoding="utf-8") for path in [*output_dir.glob("*.svg"), report])
    for label in ("Agent", "Harness", "Memory", "Profile", "candidate_uid", "self-correlation", "Submit Boundary", "Release Boundary"):
        assert label in combined
    for private_fragment in ("C:\\Users\\", "D:\\code", "F:\\Obsidian Vault"):
        assert private_fragment not in combined
    assert "candidates 5 -> simulated 3 -> ready 1 -> submitted 0" in report.read_text(encoding="utf-8")


def test_public_visual_pack_degrades_when_optional_artifacts_are_missing(tmp_path):
    source = tmp_path / "minimal_demo"
    source.mkdir(parents=True)
    output_dir = tmp_path / "images"
    report = tmp_path / "VISUAL_GUIDE.md"

    result = build_public_visual_pack(source, output_dir, report)

    assert result["ok"] is True
    assert result["data"]["quality_available"] is False
    quality_svg = (output_dir / "quality-review-dashboard.svg").read_text(encoding="utf-8")
    guide = report.read_text(encoding="utf-8")
    assert "Quality review not available in this demo" in quality_svg
    assert "not available in this demo" in guide


def _write_demo_source(source: Path) -> None:
    exp = source / "experiments" / "exp-demo"
    presubmit = exp / "presubmit_run"
    eval_dir = exp / "evaluations" / "public-harness-demo"
    quality = source / "quality_review"
    for path in (presubmit, eval_dir, quality):
        path.mkdir(parents=True, exist_ok=True)

    _write_json(
        source / "demo_summary.json",
        {
            "ok": True,
            "experiment_id": "exp-demo",
            "submit_guard": "No real WQ submit call is made; submit_by_ids is a no-op recorder.",
            "real_submit_attempted": False,
        },
    )
    candidates = [
        {
            "expression": "rank(ts_rank(close, 20) - ts_rank(returns, 5))",
            "tag": "demo-ready-lowcorr",
            "source_family": "demo_price_reversal",
            "field_signature": "close|returns",
        },
        {
            "expression": "rank(ts_rank(vwap, 20) - ts_rank(volume, 10))",
            "tag": "demo-strict-selfcorr",
            "source_family": "demo_liquidity_reversal",
            "field_signature": "volume|vwap",
        },
        {
            "expression": "rank(ts_corr(close, volume, 10))",
            "tag": "demo-near-miss-repair",
            "source_family": "demo_price_volume_corr",
            "field_signature": "close|volume",
        },
        {
            "expression": "rank(not_a_real_field)",
            "tag": "demo-illegal-field",
            "source_family": "demo_illegal_input",
            "field_signature": "not_a_real_field",
        },
        {
            "expression": "rank(close)",
            "tag": "demo-active-duplicate",
            "source_family": "demo_duplicate",
            "field_signature": "close",
        },
    ]
    _write_jsonl(exp / "candidate_specs.jsonl", candidates)
    _write_jsonl(
        presubmit / "presubmit_ready_sequential.jsonl",
        [
            {
                **candidates[0],
                "candidate_uid": "uid-ready",
                "sharpe": 1.82,
                "fitness": 1.24,
                "sc_value": 0.42,
            }
        ],
    )
    _write_jsonl(
        presubmit / "presubmit_rejected.jsonl",
        [
            {
                **candidates[1],
                "candidate_uid": "uid-sc",
                "presubmit_reject_reason": "self_correlation_value_above_strict_cutoff",
                "sc_value": 0.68,
            },
            {
                **candidates[2],
                "candidate_uid": "uid-near",
                "presubmit_reject_reason": "not_confirmed_ready",
                "sc_value": 0.79,
            },
        ],
    )
    _write_json(
        eval_dir / "eval_summary.json",
        {
            "harness_score": 0.885417,
            "metrics": {
                "ready_count": 1,
                "review_count": 3,
                "presubmit_rejected_count": 2,
                "total_simulations": 3,
                "self_correlation_reject_count": 1,
                "real_submit_attempt_count": 0,
                "real_submit_success_count": 0,
            },
            "reject_counts": {
                "exact_active_duplicate": 1,
                "illegal_field": 1,
                "not_confirmed_ready": 1,
                "self_correlation_value_above_strict_cutoff": 1,
            },
            "field_signature": {
                "rows": [
                    {"field_signature": "close|returns", "count": 1, "ready_count": 1, "rejected_count": 0},
                    {"field_signature": "volume|vwap", "count": 1, "ready_count": 0, "rejected_count": 1},
                    {"field_signature": "close|volume", "count": 1, "ready_count": 0, "rejected_count": 1},
                ]
            },
        },
    )
    _write_json(
        eval_dir / "evolution_result.json",
        {
            "next_generation": {
                "harness_score": 0.885417,
                "child_experiment": {"experiment_id": "exp-demo-g1"},
                "profile_evolution": {
                    "baseline_score": 0.885417,
                    "recommended_candidate": "candidate_a",
                    "candidates": {
                        "candidate_a": {
                            "actions": [
                                {
                                    "trigger": "self_correlation_reject_share",
                                    "change": "tighten family and field-signature reuse",
                                }
                            ],
                            "profile": {
                                "priority_biases": [
                                    "cross_domain_overlay",
                                    "low_overlap_field_family",
                                ]
                            },
                        }
                    },
                },
            }
        },
    )
    _write_json(
        source / "efficiency_summary.json",
        {
            "current": {
                "funnel": {
                    "candidates": 5,
                    "simulated": 3,
                    "reviewed": 3,
                    "ready": 1,
                    "rejected": 2,
                    "submitted": 0,
                    "active": 0,
                },
                "leaderboards": {
                    "field_signature": [
                        {"field_signature": "close|returns", "count": 1, "ready_count": 1, "rejected_count": 0},
                        {"field_signature": "volume|vwap", "count": 1, "ready_count": 0, "rejected_count": 1},
                    ]
                },
            }
        },
    )
    _write_json(
        quality / "summary.json",
        {
            "metrics": {
                "period_quality_score": 0.628045,
                "generated_metric_pass_rate": 0.272727,
                "generated_ready_rate": 0.090909,
                "generated_self_correlation_fail_share": 0.181818,
                "quality_bucket_counts": {
                    "active": 1,
                    "blocked_self_correlation": 2,
                    "generated_candidate": 8,
                    "ready": 1,
                },
            },
            "self_correlation_pressure": [
                {"group_key": "volume|vwap", "self_correlation_fail_share": 0.333333},
                {"group_key": "close|returns", "self_correlation_fail_share": 0.0},
            ],
        },
    )
    _write_json(
        quality / "recommended_directions.json",
        {
            "directions": [
                {
                    "title": "Self-correlation pressure overlay repair",
                    "expected_blocker": "self_correlation",
                }
            ]
        },
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
