import json
from pathlib import Path

from worldquant_harness.wq_active_weak_review import WQActiveWeakReviewConfig, run_active_weak_review


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_active_weak_review_selects_only_active_and_writes_memory(tmp_path):
    platform_file = tmp_path / "platform.jsonl"
    output_dir = tmp_path / "review"
    _write_jsonl(platform_file, [
        {
            "alpha_id": "alpha_weak_returns",
            "expression": "rank(ts_rank(cashflow_op / cap, 80) - ts_rank(returns, 20))",
            "status": "ACTIVE",
            "sharpe": 1.10,
            "fitness": 0.70,
            "returns": -0.02,
            "turnover": 0.20,
        },
        {
            "alpha_id": "alpha_turnover",
            "expression": "rank(ts_rank(ebit / enterprise_value, 60) - ts_rank(returns, 20))",
            "status": "SUBMITTED",
            "sharpe": 1.40,
            "fitness": 1.20,
            "returns": 0.04,
            "turnover": 0.82,
        },
        {
            "alpha_id": "alpha_strong_1",
            "expression": "rank(ts_rank(forward_cash_flow_to_price, 80) - ts_rank(returns, 40))",
            "status": "ACTIVE",
            "sharpe": 1.80,
            "fitness": 1.45,
            "returns": 0.09,
            "turnover": 0.18,
        },
        {
            "alpha_id": "alpha_strong_2",
            "expression": "rank(ts_rank(forward_book_value_to_price, 80) + ts_rank(snt1_d1_netearningsrevision, 60))",
            "status": "ACTIVE",
            "sharpe": 1.70,
            "fitness": 1.25,
            "returns": 0.07,
            "turnover": 0.16,
        },
        {
            "alpha_id": "alpha_unsubmitted",
            "expression": "rank(volume)",
            "status": "UNSUBMITTED",
            "sharpe": 0.20,
            "fitness": 0.10,
            "returns": -0.30,
            "turnover": 0.90,
        },
    ])

    summary = run_active_weak_review(WQActiveWeakReviewConfig(
        output_dir=output_dir,
        platform_file=platform_file,
        max_checks=0,
    ))

    weak_rows = _read_jsonl(output_dir / "weak_active_review.jsonl")
    memory = _read_jsonl(output_dir / "weak_active_memory.jsonl")

    assert summary["ok"] is True
    assert summary["no_external_llm"] is True
    assert summary["active_count"] == 4
    assert summary["weak_count"] >= 2
    assert {row["status"] for row in weak_rows} <= {"ACTIVE", "SUBMITTED"}
    assert "alpha_unsubmitted" not in {row["alpha_id"] for row in weak_rows}
    assert any(row["primary_weakness"] == "active_low_returns" for row in weak_rows)
    assert any(row["primary_weakness"] == "active_turnover_drag" for row in weak_rows)
    assert {row["memory_kind"] for row in memory} == {"weak_active_constraint"}
    assert all(row["severity"] == "penalize" for row in memory)
    assert (output_dir / "weak_active_report.md").is_file()


def test_active_weak_review_can_merge_check_only_correlation(tmp_path):
    platform_file = tmp_path / "platform.jsonl"
    output_dir = tmp_path / "review"
    _write_jsonl(platform_file, [
        {
            "alpha_id": "alpha_corr",
            "expression": "rank(ts_rank(cashflow_op / cap, 80) - ts_rank(returns, 20))",
            "status": "ACTIVE",
            "sharpe": 1.32,
            "fitness": 1.02,
            "returns": 0.04,
            "turnover": 0.20,
        },
        {
            "alpha_id": "alpha_ok",
            "expression": "rank(ts_rank(forward_cash_flow_to_price, 80) - ts_rank(returns, 40))",
            "status": "ACTIVE",
            "sharpe": 1.70,
            "fitness": 1.30,
            "returns": 0.08,
            "turnover": 0.18,
        },
    ])
    checked_ids = []

    def fake_check(ids, _config):
        checked_ids.extend(ids)
        return {
            "alphas": {
                "alpha_corr": {
                    "status": "ACTIVE",
                    "sharpe": 1.32,
                    "fitness": 1.02,
                    "returns": 0.04,
                    "turnover": 0.20,
                    "sc_result": "WARNING",
                    "sc_value": 0.68,
                },
                "alpha_ok": {
                    "status": "ACTIVE",
                    "sharpe": 1.70,
                    "fitness": 1.30,
                    "returns": 0.08,
                    "turnover": 0.18,
                    "sc_result": "PASS",
                    "sc_value": 0.20,
                }
            }
        }

    summary = run_active_weak_review(
        WQActiveWeakReviewConfig(
            output_dir=output_dir,
            platform_file=platform_file,
            max_checks=2,
            weak_score_cutoff=2.0,
            bottom_quantile=1.0,
        ),
        dependencies={"check_submissions": fake_check},
    )

    weak_rows = _read_jsonl(output_dir / "weak_active_review.jsonl")
    corr = next(row for row in weak_rows if row["alpha_id"] == "alpha_corr")

    assert "alpha_corr" in checked_ids
    assert summary["checked_count"] == 2
    assert corr["primary_weakness"] == "active_correlation_risk"
    assert corr["sc_value"] == 0.68
