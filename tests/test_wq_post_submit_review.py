import json
from pathlib import Path

from worldquant_harness.wq_post_submit_review import WQPostSubmitReviewConfig, build_post_submit_review


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_post_submit_review_labels_active_strength_and_threshold(tmp_path):
    run_dir = tmp_path / "run"
    _write_jsonl(run_dir / "submit_existing_results.jsonl", [
        {
            "alpha_id": "strong",
            "ok": True,
            "final_status": "ACTIVE",
            "expression": "rank(0.4 * rank(close) + 0.6 * rank(volume))",
            "candidate_metrics": {"sharpe": 1.8, "fitness": 1.35, "returns": 0.1, "turnover": 0.12},
            "live_precheck": {
                "is": {"checks": [
                    {"name": "SELF_CORRELATION", "result": "PASS", "value": 0.62, "limit": 0.7},
                    {"name": "LOW_SUB_UNIVERSE_SHARPE", "result": "PASS", "value": 1.05, "limit": 0.7},
                ]}
            },
        },
        {
            "alpha_id": "thin",
            "ok": True,
            "final_status": "ACTIVE",
            "expression": "rank(0.2 * rank(ts_rank(actual_eps_value_quarterly / vwap, 80)) + 0.8 * rank(ts_corr(open, volume, 60)))",
            "candidate_metrics": {"sharpe": 1.29, "fitness": 1.09, "returns": 0.09, "turnover": 0.09},
            "live_precheck": {
                "is": {"checks": [
                    {"name": "SELF_CORRELATION", "result": "PASS", "value": 0.6541, "limit": 0.7},
                    {"name": "LOW_SUB_UNIVERSE_SHARPE", "result": "PASS", "value": 0.71, "limit": 0.56},
                ]}
            },
        },
    ])

    report = build_post_submit_review(WQPostSubmitReviewConfig(
        run_dirs=(run_dir,),
        output_dir=tmp_path / "review",
    ))

    labels = {row["alpha_id"]: row for row in _read_jsonl(tmp_path / "review" / "alpha_labels.jsonl")}
    constraints = json.loads((tmp_path / "review" / "next_run_constraints.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert labels["strong"]["label"] == "strong_seed_active"
    assert labels["thin"]["label"] == "threshold_repair_active"
    assert constraints["preferred_seed_alpha_ids"] == ["strong"]
    assert constraints["threshold_only_alpha_ids"] == ["thin"]
    assert "Threshold ACTIVE" in labels["thin"]["lesson"]


def test_post_submit_review_blocks_near_miss_and_sparse_group_risk(tmp_path):
    run_dir = tmp_path / "run"
    _write_jsonl(run_dir / "submit_existing_results.jsonl", [
        {
            "alpha_id": "sc_fail",
            "ok": False,
            "final_status": "SC_FAIL",
            "expression": "rank(0.4 * rank(close) + 0.6 * rank(volume))",
            "candidate_metrics": {"sharpe": 1.7, "fitness": 1.2, "returns": 0.1, "turnover": 0.12},
            "sc_value": 0.91,
        },
        {
            "alpha_id": "sparse_group",
            "ok": False,
            "final_status": "OTHER_FAIL",
            "expression": "rank(group_rank(ts_rank(cashflow_op / enterprise_value, 100), industry))",
            "candidate_metrics": {"sharpe": 0.7, "fitness": 0.4, "turnover": 0.2},
        },
    ])

    build_post_submit_review(WQPostSubmitReviewConfig(
        run_dirs=(run_dir,),
        output_dir=tmp_path / "review",
    ))

    labels = {row["alpha_id"]: row for row in _read_jsonl(tmp_path / "review" / "alpha_labels.jsonl")}
    constraints = json.loads((tmp_path / "review" / "next_run_constraints.json").read_text(encoding="utf-8"))
    assert labels["sc_fail"]["label"] == "blocked_near_miss"
    assert labels["sparse_group"]["label"] == "do_not_seed"
    assert labels["sparse_group"]["has_sparse_group_risk"] is True
    assert any("avoid sparse fundamental/PCR legs" in item for item in constraints["avoid_expression_patterns"])
    assert "self-correlation failures must change field/operator family before recheck" in constraints["required_repairs"]
