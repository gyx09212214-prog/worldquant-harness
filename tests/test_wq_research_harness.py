import json
import shutil
import uuid
from pathlib import Path

import pytest

from worldquant_harness.wq_research_harness import (
    WQHarnessEvalConfig,
    WQHarnessEvolutionConfig,
    evolve_wq_research_experiment,
    run_wq_harness_evaluation,
)
from worldquant_harness.wq_research_sandbox import new_research_experiment


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / f"wq_research_harness_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _seed_evaluated_experiment(workdir: Path) -> tuple[Path, Path]:
    result = new_research_experiment(
        "harness metric test",
        root=workdir,
        hypothesis="Field signature diversity should improve ready yield.",
    )
    exp_dir = Path(result["experiment_dir"])
    record = _read_json(exp_dir / "experiment.yaml")
    record["created_at"] = "2026-06-20T00:00:00+00:00"
    _write_json(exp_dir / "experiment.yaml", record)

    _write_jsonl(
        exp_dir / "candidate_specs.jsonl",
        [
            {"candidate_spec_id": "c1", "expression": "rank(open)", "source_family": "fam_a"},
            {"candidate_spec_id": "c2", "expression": "rank(ts_rank(open, 5))", "source_family": "fam_a"},
            {"candidate_spec_id": "c3", "expression": "rank(close)", "source_family": "fam_b"},
        ],
    )
    presubmit = exp_dir / "presubmit_run"
    _write_json(
        presubmit / "summary.json",
        {
            "ok": True,
            "presubmit_loop": {
                "ready_count": 1,
                "total_simulations": 100,
                "stop_reason": "target_ready_reached",
            },
        },
    )
    _write_json(
        presubmit / "loop_status.json",
        {
            "ok": True,
            "ready_count": 1,
            "target_ready": 1,
            "total_simulations": 100,
            "max_total_simulations": 100,
            "stop_reason": "target_ready_reached",
            "virtual_similarity_cutoff": 0.70,
            "max_virtual_field_signature_count": 4,
            "cycles": [
                {
                    "cycle_index": 1,
                    "candidate_skip": {
                        "skipped": 3,
                        "skip_reasons": {
                            "too_similar_to_real_or_virtual_active": 1,
                            "field_signature_capacity_reached": 2,
                        },
                    },
                }
            ],
        },
    )
    _write_jsonl(
        presubmit / "presubmit_ready_sequential.jsonl",
        [
            {
                "alpha_id": "ready1",
                "expression": "rank(open)",
                "presubmit_accepted": True,
                "presubmit_accept_reason": "accepted",
                "created_at": "2026-06-20T00:10:00+00:00",
                "sharpe": 1.8,
                "fitness": 1.2,
            }
        ],
    )
    _write_jsonl(
        presubmit / "presubmit_rejected.jsonl",
        [
            {
                "alpha_id": "reject_sc",
                "expression": "rank(high)",
                "presubmit_reject_reason": "self_correlation_value_above_strict_cutoff",
                "sc_value": 0.78,
            },
            {
                "alpha_id": "reject_sim",
                "expression": "rank(volume)",
                "presubmit_reject_reason": "too_similar_to_real_or_virtual_active",
                "nearest_similarity": 0.81,
            },
        ],
    )
    _write_jsonl(presubmit / "review_queue.jsonl", [])
    _write_json(exp_dir / "decision.yaml", {"decision": "promote_candidate"})

    submit_dir = workdir / "submit_run"
    _write_jsonl(
        submit_dir / "submit_results.jsonl",
        [
            {"alpha_id": "s1", "ok": True, "final_status": "ACTIVE"},
            {"alpha_id": "s2", "ok": False, "final_status": "SC_FAIL"},
            {"alpha_id": "s3", "ok": True, "final_status": "SUBMITTED"},
        ],
    )
    return exp_dir, submit_dir


def test_harness_evaluation_writes_core_metrics_and_artifacts(workdir):
    exp_dir, submit_dir = _seed_evaluated_experiment(workdir)

    result = run_wq_harness_evaluation(
        WQHarnessEvalConfig(
            experiment=exp_dir,
            submit_run_dirs=(submit_dir,),
            eval_id="eval-test",
        )
    )

    metrics = result["metrics"]
    assert result["ok"] is True
    assert metrics["ready_per_100_simulations"] == 1.0
    assert metrics["self_correlation_reject_share"] == 0.2
    assert metrics["too_similar_reject_share"] == 0.4
    assert metrics["duplicate_field_signature_count"] == 1
    assert metrics["hypothesis_to_first_ready_seconds"] == 600
    assert metrics["promote_submit_success_rate"] == round(2 / 3, 6)
    assert metrics["real_submit_success_count"] == 2

    eval_dir = Path(result["eval_dir"])
    assert (eval_dir / "eval_records.csv").is_file()
    assert (eval_dir / "eval_summary.csv").is_file()
    assert (eval_dir / "summary_by_field_signature.csv").is_file()
    assert (eval_dir / "summary_by_reject_reason.csv").is_file()
    assert (eval_dir / "gate_report.json").is_file()
    assert (eval_dir / "run_report.md").is_file()

    saved = _read_json(eval_dir / "eval_summary.json")
    assert saved["metrics"]["total_rejection_count"] == 5
    assert saved["submit_stats"]["active_alpha_ids"] == ["s1", "s3"]


def test_harness_counts_illegal_input_rejections(workdir):
    exp_dir, submit_dir = _seed_evaluated_experiment(workdir)
    loop_status = _read_json(exp_dir / "presubmit_run" / "loop_status.json")
    loop_status["cycles"][0]["candidate_skip"]["skip_reasons"]["illegal_field"] = 2
    loop_status["cycles"][0]["candidate_skip"]["skip_reasons"]["illegal_field_type"] = 1
    _write_json(exp_dir / "presubmit_run" / "loop_status.json", loop_status)

    result = run_wq_harness_evaluation(
        WQHarnessEvalConfig(
            experiment=exp_dir,
            submit_run_dirs=(submit_dir,),
            eval_id="eval-illegal-inputs",
        )
    )

    metrics = result["metrics"]
    assert metrics["total_rejection_count"] == 8
    assert metrics["illegal_input_reject_count"] == 3
    assert metrics["illegal_input_reject_share"] == round(3 / 8, 6)
    assert metrics["invalid_field_reject_count"] == 2
    assert metrics["illegal_field_type_reject_count"] == 1


def test_harness_evolution_creates_child_generation_with_rule_based_overrides(workdir):
    exp_dir, submit_dir = _seed_evaluated_experiment(workdir)
    eval_result = run_wq_harness_evaluation(
        WQHarnessEvalConfig(
            experiment=exp_dir,
            submit_run_dirs=(submit_dir,),
            eval_id="eval-evolve",
        )
    )

    result = evolve_wq_research_experiment(
        WQHarnessEvolutionConfig(
            experiment=exp_dir,
            eval_dir=Path(eval_result["eval_dir"]),
        )
    )

    next_gen = result["next_generation"]
    mine_config = next_gen["mine_config_overrides"]
    assert result["ok"] is True
    assert mine_config["similarity_cutoff"] < 0.72
    assert mine_config["max_field_signature_count"] == 3
    assert "low_overlap_field_family" in mine_config["priority_biases"]
    assert next_gen["field_signature_blacklist"] == ["open"]
    assert next_gen["recommended_profile_candidate"] in {"candidate_a", "candidate_b", "candidate_c"}
    assert "profile_evolution" in next_gen
    assert next_gen["recommended_research_profile"]["mine_defaults"]["no_real_submit"] is True
    assert (Path(eval_result["eval_dir"]) / "evolution_result.json").is_file()
    assert (Path(eval_result["eval_dir"]) / "reflector_report.md").is_file()

    child = next_gen["child_experiment"]
    child_record = _read_json(Path(child["experiment"]))
    assert child_record["evolution"]["parent_experiment_id"] == _read_json(exp_dir / "experiment.yaml")["id"]
    assert child_record["suggested_mine_config"]["no_real_submit"] is True
    assert child_record["research_profile"]["mine_defaults"]["no_real_submit"] is True
    assert child_record["profile_evolution"]["recommended_candidate"] == next_gen["recommended_profile_candidate"]
