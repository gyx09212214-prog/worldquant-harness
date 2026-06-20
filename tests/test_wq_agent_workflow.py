import json
import shutil
import uuid
from pathlib import Path

import pytest

from quantgpt.wq_agent_workflow import (
    CONFIRMED_READY,
    HARD_FAIL,
    NEAR_MISS_REPAIR,
    SUBMIT_PROBE_NEEDED,
    WQAgentWorkflowConfig,
    _filter_candidate_pool_for_presubmit,
    classify_review_row,
    presubmit_acceptance_gate,
    run_workflow,
    select_presubmit_ready_candidate,
    select_submission_candidates,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / f"wq_agent_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _source_row(**overrides):
    row = {
        "alpha_id": "alpha1",
        "expression": "rank(open)",
        "status": "pending_correlation_check",
        "submit_eligible": True,
        "sharpe": 1.8,
        "fitness": 1.2,
        "turnover": 0.25,
        "failed_platform_checks": [],
    }
    row.update(overrides)
    return row


def test_classify_review_row_distinguishes_ready_probe_and_failures():
    ready = classify_review_row(
        _source_row(),
        {
            "status": "UNSUBMITTED",
            "sharpe": 1.8,
            "fitness": 1.2,
            "turnover": 0.25,
            "sc_result": "PASS",
            "sc_value": 0.61,
            "prod_corr_result": "MISSING",
        },
    )
    assert ready["triage_bucket"] == CONFIRMED_READY

    pending = classify_review_row(_source_row(), {"status": "UNSUBMITTED", "sc_result": "PENDING"})
    assert pending["triage_bucket"] == SUBMIT_PROBE_NEEDED

    repairable_sc = classify_review_row(_source_row(), {"status": "UNSUBMITTED", "sc_result": "FAIL", "sc_value": 0.79})
    assert repairable_sc["triage_bucket"] == NEAR_MISS_REPAIR

    hard_sc = classify_review_row(_source_row(), {"status": "UNSUBMITTED", "sc_result": "FAIL", "sc_value": 0.93})
    assert hard_sc["triage_bucket"] == HARD_FAIL

    platform_near_miss = classify_review_row(
        _source_row(
            status="failed_platform_check",
            failed_platform_checks=[{"name": "LOW_SUB_UNIVERSE_SHARPE", "result": "FAIL", "value": 0.8, "limit": 0.9}],
        )
    )
    assert platform_near_miss["triage_bucket"] == NEAR_MISS_REPAIR


def test_select_submission_candidates_requires_authorization_for_probe():
    rows = [
        {"alpha_id": "ready", "triage_bucket": CONFIRMED_READY, "fitness": 1.2, "sharpe": 1.8, "turnover": 0.2},
        {"alpha_id": "probe", "triage_bucket": SUBMIT_PROBE_NEEDED, "fitness": 1.8, "sharpe": 2.0, "turnover": 0.2},
    ]

    assert select_submission_candidates(rows, explicit_ids=[], submit_count=2, allow_submit_probe=False) == ["ready"]
    assert select_submission_candidates(rows, explicit_ids=[], submit_count=2, allow_submit_probe=True) == ["ready", "probe"]
    assert select_submission_candidates(rows, explicit_ids=["probe"], submit_count=0, allow_submit_probe=False) == ["probe"]
    assert select_submission_candidates(rows, explicit_ids=[], submit_count=0, allow_submit_probe=True) == []


def test_run_workflow_writes_artifacts_without_submit(workdir):
    candidates = workdir / "candidates.jsonl"
    _write_jsonl(candidates, [{"expression": "rank(open)", "tag": "manual-open"}])

    submit_calls = []

    def fake_list_alphas(config):
        return [
            {
                "alpha_id": "active1",
                "expression": "rank(close)",
                "status": "ACTIVE",
                "sharpe": 1.6,
                "fitness": 1.1,
                "turnover": 0.2,
            }
        ]

    def fake_simulate(candidate, config):
        return {
            "ok": True,
            "alpha_id": f"sim_{candidate['candidate_rank']}",
            "is_metrics": {
                "sharpe": 1.8,
                "fitness": 1.2,
                "returns": 0.12,
                "turnover": 0.25,
                "checks": [
                    {"name": "LOW_SHARPE", "result": "PASS", "limit": 1.25, "value": 1.8},
                    {"name": "LOW_FITNESS", "result": "PASS", "limit": 1.0, "value": 1.2},
                    {"name": "LOW_TURNOVER", "result": "PASS", "limit": 0.01, "value": 0.25},
                    {"name": "HIGH_TURNOVER", "result": "PASS", "limit": 0.7, "value": 0.25},
                    {"name": "CONCENTRATED_WEIGHT", "result": "PASS"},
                    {"name": "LOW_SUB_UNIVERSE_SHARPE", "result": "PASS", "limit": 0.8, "value": 1.1},
                    {"name": "SELF_CORRELATION", "result": "PENDING"},
                ],
            },
            "submit_eligible": True,
            "submitted": False,
        }

    def fake_check(ids, config):
        return {
            alpha_id: {
                "status": "UNSUBMITTED",
                "sharpe": 1.8,
                "fitness": 1.2,
                "turnover": 0.25,
                "sc_result": "PASS",
                "sc_value": 0.61,
                "prod_corr_result": "MISSING",
            }
            for alpha_id in ids
        }

    def fake_submit(ids, config):
        submit_calls.append(ids)
        return {"results": {}}

    config = WQAgentWorkflowConfig(
        output_dir=workdir / "run",
        candidate_files=[candidates],
        target_candidates=4,
        max_simulations=2,
        use_ledger=False,
    )
    summary = run_workflow(
        config,
        mode="run",
        dependencies={
            "list_alphas": fake_list_alphas,
            "simulate": fake_simulate,
            "check_alphas": fake_check,
            "submit_by_ids": fake_submit,
        },
    )

    assert summary["ok"] is True
    assert submit_calls == []
    assert (config.output_dir / "platform_alphas.jsonl").is_file()
    assert (config.output_dir / "memory_context.json").is_file()
    assert (config.output_dir / "candidate_pool.jsonl").is_file()
    assert (config.output_dir / "simulation_results.jsonl").is_file()
    review = _read_jsonl(config.output_dir / "review_queue.jsonl")
    assert review
    assert {row["triage_bucket"] for row in review} == {CONFIRMED_READY}
    memory = json.loads((config.output_dir / "memory_context.json").read_text(encoding="utf-8"))
    assert memory["active"][0]["alpha_id"] == "active1"


def test_model_candidate_designer_uses_model_output_before_fallback(workdir):
    submit_calls = []

    def fake_model(prompt, config):
        return json.dumps([
            {
                "expression": "rank(ts_corr(vwap, volume, 20))",
                "rationale": "Uses volume-price relation away from active close rank.",
                "expected_low_corr_reason": "Different field and operator family.",
                "source_fields": ["vwap", "volume"],
                "mutation_strategy": "community_memory_diversify",
                "parent_alpha_ids": [],
                "risk_flags": [],
            }
        ])

    def fake_simulate(candidate, config):
        assert candidate["source"] == "model_candidate_designer"
        return {
            "ok": True,
            "alpha_id": "model_alpha",
            "is_metrics": {
                "sharpe": 1.9,
                "fitness": 1.3,
                "returns": 0.13,
                "turnover": 0.3,
                "checks": [{"name": "SELF_CORRELATION", "result": "PENDING"}],
            },
            "submit_eligible": True,
            "submitted": False,
        }

    config = WQAgentWorkflowConfig(
        output_dir=workdir / "model_run",
        target_candidates=1,
        max_simulations=1,
        use_ledger=False,
    )
    summary = run_workflow(
        config,
        mode="run",
        dependencies={
            "list_alphas": lambda config: [{"alpha_id": "active1", "expression": "rank(close)", "status": "ACTIVE"}],
            "model_generate_candidates": fake_model,
            "simulate": fake_simulate,
            "check_alphas": lambda ids, config: {
                "model_alpha": {"status": "UNSUBMITTED", "sc_result": "PASS", "sc_value": 0.55, "prod_corr_result": "MISSING"}
            },
            "submit_by_ids": lambda ids, config: submit_calls.append(ids),
        },
    )

    assert summary["candidate_design"]["model"]["ok"] is True
    assert submit_calls == []
    candidates = _read_jsonl(config.output_dir / "candidate_pool.jsonl")
    assert candidates[0]["expression"] == "rank(ts_corr(vwap, volume, 20))"
    assert candidates[0]["mutation_strategy"] == "community_memory_diversify"


def test_model_failure_falls_back_to_limited_templates(workdir):
    def broken_model(prompt, config):
        return "not json"

    config = WQAgentWorkflowConfig(
        output_dir=workdir / "fallback_run",
        target_candidates=5,
        max_simulations=5,
        fallback_template_limit=1,
        model_retries=0,
        dry_run=True,
        use_ledger=False,
    )
    summary = run_workflow(
        config,
        mode="run",
        dependencies={"model_generate_candidates": broken_model},
    )

    candidates = _read_jsonl(config.output_dir / "candidate_pool.jsonl")
    assert summary["candidate_design"]["model"]["ok"] is False
    assert len(candidates) == 1
    assert candidates[0]["source"] == "fallback_legacy_example"


def test_no_model_candidate_design_prioritizes_repair_queue(workdir):
    run_dir = workdir / "repair_priority_run"
    _write_jsonl(
        run_dir / "repair_queue.jsonl",
        [{
            "alpha_id": "near1",
            "tag": "near-concentration",
            "candidate_records": [
                {
                    "expression": "rank(ts_rank(forward_sales_to_price, 100))",
                    "tag": "repair-forward-sales",
                    "source_family": "repair_self_corr",
                    "forum_policy_action": "allow",
                    "repair_priority_score": 90,
                }
            ],
        }],
    )

    config = WQAgentWorkflowConfig(
        output_dir=run_dir,
        no_model=True,
        target_candidates=1,
        max_simulations=0,
        dry_run=True,
        use_ledger=False,
    )
    summary = run_workflow(
        config,
        mode="run",
        dependencies={
            "list_alphas": lambda config: [
                {"alpha_id": "active1", "expression": "rank(close)", "status": "ACTIVE"},
                {
                    "alpha_id": "platform1",
                    "expression": "rank(ts_rank(cashflow_op / cap, 80))",
                    "status": "UNSUBMITTED",
                    "sharpe": 2.0,
                    "fitness": 1.4,
                    "turnover": 0.2,
                },
            ],
        },
    )

    candidates = _read_jsonl(config.output_dir / "candidate_pool.jsonl")
    assert summary["candidate_design"]["repair_candidates"] == 1
    assert candidates[0]["tag"] == "repair-forward-sales"
    assert candidates[0]["source"] == str(run_dir / "repair_queue.jsonl")


def test_evolutionary_generation_mode_feeds_existing_candidate_pool(workdir):
    candidates = workdir / "seed_candidates.jsonl"
    _write_jsonl(candidates, [{"expression": "rank(volume / adv20)", "tag": "liquidity-seed"}])

    config = WQAgentWorkflowConfig(
        output_dir=workdir / "evolutionary_run",
        candidate_files=[candidates],
        generation_mode="evolutionary",
        evolutionary_candidates=4,
        target_candidates=2,
        max_simulations=0,
        dry_run=True,
        use_ledger=False,
    )
    summary = run_workflow(
        config,
        mode="run",
        dependencies={
            "platform_rows": [
                {
                    "alpha_id": "active_cf",
                    "expression": "rank(ts_rank(cashflow_op / assets, 120) - ts_rank(returns, 30))",
                    "status": "ACTIVE",
                    "sharpe": 1.7,
                    "fitness": 1.15,
                    "turnover": 0.18,
                }
            ],
        },
    )

    assert summary["candidate_design"]["evolutionary"]["generated"] >= 1
    pool = _read_jsonl(config.output_dir / "candidate_pool.jsonl")
    assert pool[0]["source"] == "evolutionary_alpha_generator"
    assert pool[0]["candidate_meta"]["evolutionary"] is True
    assert any(row["candidate_meta"]["evolutionary"] is True for row in pool)


def test_model_repair_planner_writes_repair_queue(workdir):
    run_dir = workdir / "repair_run"
    run_dir.mkdir(parents=True)
    _write_jsonl(
        run_dir / "review_queue.jsonl",
        [{
            "alpha_id": "near1",
            "expression": "rank(open)",
            "triage_bucket": NEAR_MISS_REPAIR,
            "triage_reason": "self-correlation failed (0.79)",
            "sharpe": 1.8,
            "fitness": 1.2,
            "turnover": 0.25,
        }],
    )

    def fake_repairs(prompt, config):
        return {
            "repairs": [
                {
                    "source_expression": "rank(open)",
                    "failure_kind": "self_correlation_fail",
                    "diagnosis": "Too close to open/price family.",
                    "repair_objective": "Switch to vwap-volume relation.",
                    "candidate_expressions": ["rank(ts_corr(vwap, volume, 30))"],
                    "risk_notes": ["check sub-universe sharpe"],
                }
            ]
        }

    summary = run_workflow(
        WQAgentWorkflowConfig(output_dir=run_dir, use_ledger=False),
        mode="postmortem",
        dependencies={"model_generate_repairs": fake_repairs},
    )

    assert summary["postmortem"]["model_repairs"]["ok"] is True
    repairs = _read_jsonl(run_dir / "repair_queue.jsonl")
    assert repairs[0]["candidate_expressions"] == ["rank(ts_corr(vwap, volume, 30))"]
    assert repairs[0]["model_generated"] is True


def test_no_model_postmortem_writes_deterministic_repair_candidates(workdir):
    run_dir = workdir / "deterministic_repair"
    run_dir.mkdir(parents=True)
    _write_jsonl(
        run_dir / "review_queue.jsonl",
        [{
            "alpha_id": "near_sc",
            "expression": "rank(0.45 * ts_rank(actual_sales_value_quarterly / enterprise_value, 80) + 0.25 * ts_rank(actual_eps_value_quarterly / vwap, 80) + 0.15 * ts_rank(change_in_eps_surprise, 60) + 0.15 * rank(ts_corr(close, volume, 20)) - ts_rank(returns, 40))",
            "tag": "sales-eps",
            "triage_bucket": NEAR_MISS_REPAIR,
            "triage_reason": "self-correlation failed (0.8251)",
            "sc_value": 0.8251,
            "sharpe": 1.8,
            "fitness": 1.1,
            "turnover": 0.32,
            "source_fields": [
                "actual_sales_value_quarterly",
                "actual_eps_value_quarterly",
                "change_in_eps_surprise",
            ],
        }],
    )

    summary = run_workflow(
        WQAgentWorkflowConfig(output_dir=run_dir, no_model=True, use_ledger=False),
        mode="postmortem",
    )

    repairs = _read_jsonl(run_dir / "repair_queue.jsonl")
    assert summary["postmortem"]["model_repairs"]["reason"] == "deterministic_policy_repair"
    assert repairs[0]["candidate_expressions"]
    assert any("pcr_oi_60" in expression for expression in repairs[0]["candidate_expressions"])


def test_candidate_settings_variants_are_not_deduped(workdir):
    candidates = workdir / "settings_candidates.jsonl"
    _write_jsonl(
        candidates,
        [
            {"expression": "rank(open)", "tag": "base"},
            {
                "expression": "rank(open)",
                "tag": "low-trunc",
                "simulation_settings": {"truncation": 0.05, "maxPosition": "ON"},
            },
        ],
    )
    observed_truncations = []

    def fake_simulate(candidate, config):
        observed_truncations.append(candidate["effective_simulation_settings"]["truncation"])
        return {
            "ok": True,
            "alpha_id": f"sim_{len(observed_truncations)}",
            "is_metrics": {
                "sharpe": 0.5,
                "fitness": 0.2,
                "returns": 0.01,
                "turnover": 0.25,
                "checks": [],
            },
            "submit_eligible": False,
            "submitted": False,
        }

    config = WQAgentWorkflowConfig(
        output_dir=workdir / "settings_variants",
        candidate_files=[candidates],
        target_candidates=2,
        max_simulations=2,
        fallback_template_limit=0,
        no_model=True,
        run_checks=False,
        use_ledger=False,
        truncation=0.08,
    )
    summary = run_workflow(
        config,
        mode="run",
        dependencies={
            "list_alphas": lambda config: [],
            "simulate": fake_simulate,
        },
    )

    assert summary["simulation"]["simulated"] == 2
    assert observed_truncations == [0.08, 0.05]
    results = _read_jsonl(config.output_dir / "simulation_results.jsonl")
    assert results[1]["effective_simulation_settings"]["maxPosition"] == "ON"
    pool = _read_jsonl(config.output_dir / "candidate_pool.jsonl")
    assert [row.get("tag") for row in pool] == ["base", "low-trunc"]


def test_pnl_enrichment_prioritizes_temporally_stable_ready_candidates(workdir):
    candidates = workdir / "pnl_candidates.jsonl"
    _write_jsonl(
        candidates,
        [
            {"expression": "rank(open)", "tag": "high-fitness-unstable"},
            {"expression": "rank(close)", "tag": "stable"},
        ],
    )

    def fake_simulate(candidate, config):
        rank = candidate["candidate_rank"]
        return {
            "ok": True,
            "alpha_id": f"alpha_{rank}",
            "is_metrics": {
                "sharpe": 2.2 if rank == 1 else 1.5,
                "fitness": 1.8 if rank == 1 else 1.1,
                "returns": 0.12,
                "turnover": 0.2,
                "checks": [{"name": "SELF_CORRELATION", "result": "PENDING"}],
            },
            "submit_eligible": True,
            "submitted": False,
        }

    def fake_check(ids, config):
        return {
            alpha_id: {
                "status": "UNSUBMITTED",
                "sharpe": 2.2 if alpha_id == "alpha_1" else 1.5,
                "fitness": 1.8 if alpha_id == "alpha_1" else 1.1,
                "turnover": 0.2,
                "sc_result": "PASS",
                "sc_value": 0.4,
                "prod_corr_result": "MISSING",
            }
            for alpha_id in ids
        }

    def fake_pnl_enrichment(targets, config):
        assert [row["alpha_id"] for row in targets] == ["alpha_1", "alpha_2"]
        return [
            {
                "alpha_id": "alpha_1",
                "tag": "high-fitness-unstable",
                "pnl_curve_found": True,
                "pnl_points": 100,
                "pnl_curve_path": "/fake/alpha_1",
                "yearly": [{"year": 2020, "return": -0.01, "sharpe": -0.2}],
                "stability": {"temporal_stability_score": 35.0, "positive_year_ratio": 0.5},
                "warnings": ["negative_year_sharpe"],
            },
            {
                "alpha_id": "alpha_2",
                "tag": "stable",
                "pnl_curve_found": True,
                "pnl_points": 100,
                "pnl_curve_path": "/fake/alpha_2",
                "yearly": [{"year": 2020, "return": 0.04, "sharpe": 1.8}],
                "stability": {"temporal_stability_score": 92.0, "positive_year_ratio": 1.0},
                "warnings": [],
            },
        ]

    config = WQAgentWorkflowConfig(
        output_dir=workdir / "pnl_enrichment",
        candidate_files=[candidates],
        target_candidates=2,
        max_simulations=2,
        fallback_template_limit=0,
        no_model=True,
        enrich_pnl=True,
        use_ledger=False,
    )
    summary = run_workflow(
        config,
        mode="run",
        dependencies={
            "list_alphas": lambda config: [],
            "simulate": fake_simulate,
            "check_alphas": fake_check,
            "pnl_enrichment": fake_pnl_enrichment,
        },
    )

    rows = _read_jsonl(config.output_dir / "review_queue.jsonl")
    assert summary["review"]["pnl_enrichment"]["enriched"] == 2
    assert rows[0]["alpha_id"] == "alpha_2"
    assert rows[0]["temporal_stability_score"] == 92.0
    assert rows[1]["pnl_warnings"] == ["negative_year_sharpe"]
    assert select_submission_candidates(rows, explicit_ids=[], submit_count=1, allow_submit_probe=False) == ["alpha_2"]
    assert (config.output_dir / "pnl_analysis.md").is_file()


def test_run_submit_loop_submits_until_target(workdir):
    submit_calls = []

    def fake_model(prompt, config):
        return [
            {"expression": "rank(open)", "rationale": "price level"},
            {"expression": "rank(close)", "rationale": "close level"},
            {"expression": "rank(volume)", "rationale": "volume level"},
        ]

    def fake_simulate(candidate, config):
        return {
            "ok": True,
            "alpha_id": f"alpha_{candidate['candidate_rank']}",
            "is_metrics": {
                "sharpe": 1.8,
                "fitness": 1.2,
                "returns": 0.12,
                "turnover": 0.25,
                "checks": [{"name": "SELF_CORRELATION", "result": "PENDING"}],
            },
            "submit_eligible": True,
            "submitted": False,
        }

    def fake_check(ids, config):
        return {
            alpha_id: {
                "status": "UNSUBMITTED",
                "sharpe": 1.8,
                "fitness": 1.2,
                "turnover": 0.25,
                "sc_result": "PASS",
                "sc_value": 0.52,
                "prod_corr_result": "MISSING",
            }
            for alpha_id in ids
        }

    def fake_submit(ids, config):
        submit_calls.append(list(ids))
        return {
            "total": len(ids),
            "active": len(ids),
            "results": {alpha_id: {"ok": True, "final_status": "ACTIVE"} for alpha_id in ids},
        }

    config = WQAgentWorkflowConfig(
        output_dir=workdir / "run_submit_success",
        target_submissions=2,
        max_total_simulations=10,
        cycle_candidate_count=3,
        max_simulations=3,
        max_cycles=5,
        fallback_template_limit=0,
        use_ledger=False,
    )
    summary = run_workflow(
        config,
        mode="run-submit",
        dependencies={
            "list_alphas": lambda config: [],
            "model_generate_candidates": fake_model,
            "simulate": fake_simulate,
            "check_alphas": fake_check,
            "submit_by_ids": fake_submit,
        },
    )

    loop = summary["run_submit_loop"]
    assert summary["ok"] is True
    assert loop["stop_reason"] == "target_submissions_reached"
    assert loop["submitted_successes"] == 2
    assert loop["total_simulations"] == 3
    assert submit_calls == [["alpha_1", "alpha_2"]]
    submitted = _read_jsonl(config.output_dir / "submitted_accumulator.jsonl")
    assert [row["alpha_id"] for row in submitted] == ["alpha_1", "alpha_2"]
    assert (config.output_dir / "cycles" / "cycle_001" / "summary.json").is_file()


def test_run_submit_loop_stops_at_total_simulation_cap(workdir):
    submit_calls = []

    def fake_model(prompt, config):
        return [{"expression": "rank(open)"}, {"expression": "rank(close)"}]

    def fake_simulate(candidate, config):
        return {
            "ok": True,
            "alpha_id": f"weak_{candidate['candidate_rank']}",
            "is_metrics": {"sharpe": 0.5, "fitness": 0.2, "returns": 0.01, "turnover": 0.25, "checks": []},
            "submit_eligible": False,
            "submitted": False,
        }

    config = WQAgentWorkflowConfig(
        output_dir=workdir / "run_submit_cap",
        target_submissions=1,
        max_total_simulations=3,
        cycle_candidate_count=2,
        max_simulations=2,
        max_cycles=5,
        run_checks=False,
        fallback_template_limit=0,
        use_ledger=False,
    )
    summary = run_workflow(
        config,
        mode="run-submit",
        dependencies={
            "list_alphas": lambda config: [],
            "model_generate_candidates": fake_model,
            "simulate": fake_simulate,
            "submit_by_ids": lambda ids, config: submit_calls.append(list(ids)),
        },
    )

    loop = summary["run_submit_loop"]
    assert summary["ok"] is False
    assert loop["stop_reason"] == "max_total_simulations_reached"
    assert loop["submitted_successes"] == 0
    assert loop["total_simulations"] == 3
    assert submit_calls == []


def test_run_submit_loop_probe_submission_requires_flag(workdir):
    def fake_model(prompt, config):
        return [{"expression": "rank(open)"}]

    def fake_simulate(candidate, config):
        return {
            "ok": True,
            "alpha_id": "probe_1",
            "is_metrics": {
                "sharpe": 1.8,
                "fitness": 1.2,
                "returns": 0.12,
                "turnover": 0.25,
                "checks": [{"name": "SELF_CORRELATION", "result": "PENDING"}],
            },
            "submit_eligible": True,
            "submitted": False,
        }

    def fake_check(ids, config):
        return {
            "probe_1": {
                "status": "UNSUBMITTED",
                "sharpe": 1.8,
                "fitness": 1.2,
                "turnover": 0.25,
                "sc_result": "PENDING",
            }
        }

    def fake_submit(ids, config):
        return {
            "total": len(ids),
            "active": len(ids),
            "results": {alpha_id: {"ok": True, "final_status": "ACTIVE"} for alpha_id in ids},
        }

    submit_calls_without_flag = []
    no_probe_summary = run_workflow(
        WQAgentWorkflowConfig(
            output_dir=workdir / "run_submit_probe_guard",
            target_submissions=1,
            max_total_simulations=1,
            cycle_candidate_count=1,
            max_simulations=1,
            max_cycles=1,
            fallback_template_limit=0,
            use_ledger=False,
        ),
        mode="run-submit",
        dependencies={
            "list_alphas": lambda config: [],
            "model_generate_candidates": fake_model,
            "simulate": fake_simulate,
            "check_alphas": fake_check,
            "submit_by_ids": lambda ids, config: submit_calls_without_flag.append(list(ids)),
        },
    )
    assert no_probe_summary["ok"] is False
    assert submit_calls_without_flag == []

    submit_calls_with_flag = []
    with_probe_summary = run_workflow(
        WQAgentWorkflowConfig(
            output_dir=workdir / "run_submit_probe_allowed",
            target_submissions=1,
            max_total_simulations=1,
            cycle_candidate_count=1,
            max_simulations=1,
            max_cycles=1,
            fallback_template_limit=0,
            allow_submit_probe=True,
            use_ledger=False,
        ),
        mode="run-submit",
        dependencies={
            "list_alphas": lambda config: [],
            "model_generate_candidates": fake_model,
            "simulate": fake_simulate,
            "check_alphas": fake_check,
            "submit_by_ids": lambda ids, config: (submit_calls_with_flag.append(list(ids)) or fake_submit(ids, config)),
        },
    )
    assert with_probe_summary["ok"] is True
    assert submit_calls_with_flag == [["probe_1"]]


def test_presubmit_sequential_adds_virtual_active_and_never_submits(workdir):
    submit_calls = []

    def fake_model(prompt, config):
        return [
            {"expression": "rank(open)", "source_family": "price_level"},
            {"expression": "rank(volume)", "source_family": "volume_level"},
            {"expression": "rank(vwap)", "source_family": "vwap_level"},
        ]

    def fake_simulate(candidate, config):
        expression = candidate["expression"]
        return {
            "ok": True,
            "alpha_id": {
                "rank(open)": "alpha_open",
                "rank(volume)": "alpha_volume",
                "rank(vwap)": "alpha_vwap",
            }[expression],
            "is_metrics": {
                "sharpe": 1.8,
                "fitness": 1.2,
                "returns": 0.12,
                "turnover": 0.25,
                "checks": [{"name": "SELF_CORRELATION", "result": "PENDING"}],
            },
            "submit_eligible": True,
            "submitted": False,
        }

    def fake_check(ids, config):
        return {
            alpha_id: {
                "status": "UNSUBMITTED",
                "sharpe": 1.8,
                "fitness": 1.2,
                "turnover": 0.25,
                "sc_result": "PASS",
                "sc_value": 0.52,
                "prod_corr_result": "MISSING",
            }
            for alpha_id in ids
        }

    config = WQAgentWorkflowConfig(
        output_dir=workdir / "presubmit_success",
        target_ready=2,
        max_total_simulations=10,
        cycle_candidate_count=3,
        max_simulations=3,
        max_cycles=5,
        virtual_similarity_cutoff=1.0,
        max_virtual_field_signature_count=10,
        fallback_template_limit=0,
        use_ledger=False,
    )
    summary = run_workflow(
        config,
        mode="presubmit-sequential",
        dependencies={
            "list_alphas": lambda config: [{"alpha_id": "active1", "expression": "rank(close)", "status": "ACTIVE"}],
            "model_generate_candidates": fake_model,
            "simulate": fake_simulate,
            "check_submissions": fake_check,
            "submit_by_ids": lambda ids, config: submit_calls.append(list(ids)),
        },
    )

    loop = summary["presubmit_loop"]
    assert summary["ok"] is True
    assert loop["stop_reason"] == "target_ready_reached"
    assert loop["ready_count"] == 2
    assert submit_calls == []
    ready = _read_jsonl(config.output_dir / "presubmit_ready_sequential.jsonl")
    assert [row["alpha_id"] for row in ready] == ["alpha_open", "alpha_volume"]
    inventory = json.loads((config.output_dir / "virtual_active_inventory.json").read_text(encoding="utf-8"))
    assert inventory["real_active_count"] == 1
    assert inventory["virtual_active_count"] == 2
    assert [row["status"] for row in inventory["virtual_active"]] == ["VIRTUAL_ACTIVE", "VIRTUAL_ACTIVE"]


def test_presubmit_accepts_missing_platform_status_when_candidate_is_not_submitted(workdir):
    def fake_model(prompt, config):
        return [{"expression": "rank(open)", "source_family": "price_level"}]

    def fake_simulate(candidate, config):
        return {
            "ok": True,
            "alpha_id": "alpha_missing_status",
            "is_metrics": {
                "sharpe": 1.8,
                "fitness": 1.2,
                "returns": 0.12,
                "turnover": 0.25,
                "checks": [{"name": "SELF_CORRELATION", "result": "PENDING"}],
            },
            "submit_eligible": True,
            "submitted": False,
        }

    config = WQAgentWorkflowConfig(
        output_dir=workdir / "presubmit_missing_status",
        target_ready=1,
        max_total_simulations=1,
        cycle_candidate_count=1,
        max_simulations=1,
        max_cycles=1,
        virtual_similarity_cutoff=1.0,
        fallback_template_limit=0,
        use_ledger=False,
    )
    summary = run_workflow(
        config,
        mode="presubmit-sequential",
        dependencies={
            "list_alphas": lambda config: [],
            "model_generate_candidates": fake_model,
            "simulate": fake_simulate,
            "check_submissions": lambda ids, config: {
                "alpha_missing_status": {
                    "sharpe": 1.8,
                    "fitness": 1.2,
                    "turnover": 0.25,
                    "sc_result": "PASS",
                    "sc_value": 0.52,
                    "prod_corr_result": "MISSING",
                }
            },
        },
    )

    assert summary["ok"] is True
    assert summary["presubmit_loop"]["ready_count"] == 1


def test_presubmit_sequential_rejects_passed_check_when_sc_value_exceeds_strict_cutoff(workdir):
    def fake_model(prompt, config):
        return [{"expression": "rank(open)", "source_family": "price_level"}]

    def fake_simulate(candidate, config):
        return {
            "ok": True,
            "alpha_id": "alpha_high_sc",
            "is_metrics": {
                "sharpe": 2.0,
                "fitness": 1.35,
                "returns": 0.14,
                "turnover": 0.3,
                "checks": [{"name": "SELF_CORRELATION", "result": "PENDING"}],
            },
            "submit_eligible": True,
            "submitted": False,
        }

    config = WQAgentWorkflowConfig(
        output_dir=workdir / "presubmit_strict_sc",
        target_ready=1,
        max_total_simulations=1,
        cycle_candidate_count=1,
        max_simulations=1,
        max_cycles=1,
        virtual_similarity_cutoff=1.0,
        fallback_template_limit=0,
        use_ledger=False,
    )
    summary = run_workflow(
        config,
        mode="presubmit-sequential",
        dependencies={
            "list_alphas": lambda config: [],
            "model_generate_candidates": fake_model,
            "simulate": fake_simulate,
            "check_submissions": lambda ids, config: {
                "alpha_high_sc": {
                    "status": "UNSUBMITTED",
                    "sharpe": 2.0,
                    "fitness": 1.35,
                    "turnover": 0.3,
                    "sc_result": "PASS",
                    "sc_value": 0.7961,
                    "prod_corr_result": "MISSING",
                }
            },
        },
    )

    assert summary["ok"] is False
    assert summary["presubmit_loop"]["ready_count"] == 0
    rejected = _read_jsonl(config.output_dir / "presubmit_rejected.jsonl")
    assert rejected[0]["alpha_id"] == "alpha_high_sc"
    assert rejected[0]["presubmit_reject_reason"] == "self_correlation_value_above_strict_cutoff"


def test_presubmit_selection_defers_probe_needed_rows(workdir):
    config = WQAgentWorkflowConfig(output_dir=workdir / "presubmit_defer_probe")
    review_rows = [
        {
            "alpha_id": "alpha_pending",
            "expression": "rank(open)",
            "triage_bucket": SUBMIT_PROBE_NEEDED,
            "api_check_status": "api_check_pending",
            "sharpe": 1.8,
            "fitness": 1.2,
            "turnover": 0.25,
            "submit_eligible": True,
            "sc_result": "MISSING",
            "sc_value": None,
            "prod_corr_result": "MISSING",
            "failed_platform_checks": [],
        }
    ]

    accepted, rejected = select_presubmit_ready_candidate(review_rows, [], config=config, cycle_index=1)

    assert accepted is None
    assert rejected == []


def test_presubmit_candidate_filter_applies_submission_policy(workdir):
    policy_file = workdir / "submission_policy.json"
    policy_file.write_text(json.dumps({
        "gates": {"low_priority_reject_below": 15.0},
        "crowded_domains": [],
        "underexplored_domains": [],
        "theme_policies": {},
        "recipe_policies": {},
    }), encoding="utf-8")
    candidate_file = workdir / "candidate_pool.jsonl"
    _write_jsonl(
        candidate_file,
        [
            {
                "expression": "rank(ts_rank(volume, 20))",
                "tag": "direct-volume",
                "source_family": "forum_direct_triage",
            },
            {
                "expression": "rank(ts_rank(cashflow_op / cap, 80) - ts_rank(returns, 20))",
                "tag": "cashflow-overlay",
                "source_family": "forum_direct_triage",
            },
        ],
    )
    config = WQAgentWorkflowConfig(
        output_dir=workdir / "presubmit_policy",
        submission_policy_file=policy_file,
        max_virtual_family_count=10,
        max_virtual_field_signature_count=10,
    )

    summary = _filter_candidate_pool_for_presubmit(
        candidate_file,
        skip_normalized_expressions=set(),
        active_rows=[],
        config=config,
    )

    kept = _read_jsonl(candidate_file)
    assert summary["kept"] == 1
    assert summary["skip_reasons"] == {"forum_direct_template_risk": 1}
    assert kept[0]["tag"] == "cashflow-overlay"
    assert kept[0]["forum_policy_action"] == "allow"


def test_presubmit_acceptance_gate_blocks_direct_forum_template(workdir):
    policy_file = workdir / "submission_policy.json"
    policy_file.write_text(json.dumps({
        "gates": {"low_priority_reject_below": 15.0},
        "crowded_domains": [],
        "underexplored_domains": [],
        "theme_policies": {},
        "recipe_policies": {},
    }), encoding="utf-8")
    row = {
        "alpha_id": "alpha_policy_block",
        "expression": "rank(ts_rank(volume, 20))",
        "source_family": "forum_direct_triage",
        "triage_bucket": CONFIRMED_READY,
        "api_check_status": "api_check_readable",
        "platform_status": "UNSUBMITTED",
        "sharpe": 1.8,
        "fitness": 1.2,
        "turnover": 0.25,
        "sc_result": "PASS",
        "sc_value": 0.52,
        "prod_corr_result": "MISSING",
        "failed_platform_checks": [],
    }

    ok, reason, gate = presubmit_acceptance_gate(
        row,
        [],
        config=WQAgentWorkflowConfig(output_dir=workdir / "presubmit_gate_policy", submission_policy_file=policy_file),
    )

    assert ok is False
    assert reason == "forum_direct_template_risk"
    assert gate["forum_policy"]["action"] == "block"


def test_presubmit_sequential_screens_against_virtual_active_similarity(workdir):
    def fake_model(prompt, config):
        return [
            {"expression": "rank(ts_rank(open, 20))", "source_family": "open_reversal"},
            {"expression": "rank(ts_rank(open, 21))", "source_family": "open_reversal"},
        ]

    def fake_simulate(candidate, config):
        return {
            "ok": True,
            "alpha_id": candidate["expression"].replace(" ", "_"),
            "is_metrics": {
                "sharpe": 1.7,
                "fitness": 1.1,
                "returns": 0.1,
                "turnover": 0.22,
                "checks": [{"name": "SELF_CORRELATION", "result": "PENDING"}],
            },
            "submit_eligible": True,
            "submitted": False,
        }

    def fake_check(ids, config):
        return {
            alpha_id: {
                "status": "UNSUBMITTED",
                "sharpe": 1.7,
                "fitness": 1.1,
                "turnover": 0.22,
                "sc_result": "PASS",
                "sc_value": 0.55,
                "prod_corr_result": "MISSING",
            }
            for alpha_id in ids
        }

    config = WQAgentWorkflowConfig(
        output_dir=workdir / "presubmit_virtual_similarity",
        target_ready=2,
        max_total_simulations=4,
        cycle_candidate_count=2,
        max_simulations=2,
        max_cycles=2,
        max_consecutive_empty_cycles=1,
        virtual_similarity_cutoff=0.4,
        max_virtual_family_count=10,
        max_virtual_field_signature_count=10,
        fallback_template_limit=0,
        use_ledger=False,
    )
    summary = run_workflow(
        config,
        mode="presubmit-sequential",
        dependencies={
            "list_alphas": lambda config: [],
            "model_generate_candidates": fake_model,
            "simulate": fake_simulate,
            "check_submissions": fake_check,
        },
    )

    ready = _read_jsonl(config.output_dir / "presubmit_ready_sequential.jsonl")
    rejected = _read_jsonl(config.output_dir / "presubmit_rejected.jsonl")
    assert summary["ok"] is False
    assert [row["expression"] for row in ready] == ["rank(ts_rank(open, 20))"]
    skip_reasons = [
        (cycle.get("candidate_skip") or {}).get("skip_reasons") or {}
        for cycle in (summary.get("presubmit_loop") or {}).get("cycles") or []
    ]
    assert (
        any(row["presubmit_reject_reason"] == "too_similar_to_real_or_virtual_active" for row in rejected)
        or any("too_similar_to_real_or_virtual_active" in reasons for reasons in skip_reasons)
    )


def test_presubmit_sequential_skips_multi_statement_candidates_before_simulation(workdir):
    simulated_expressions = []

    def fake_model(prompt, config):
        return [
            {"expression": "x = close; rank(x)", "source_family": "multi_statement"},
            {"expression": "rank(open)", "source_family": "price_level"},
        ]

    def fake_simulate(candidate, config):
        simulated_expressions.append(candidate["expression"])
        assert ";" not in candidate["expression"]
        return {
            "ok": True,
            "alpha_id": "alpha_open",
            "is_metrics": {
                "sharpe": 1.8,
                "fitness": 1.2,
                "returns": 0.12,
                "turnover": 0.25,
                "checks": [{"name": "SELF_CORRELATION", "result": "PENDING"}],
            },
            "submit_eligible": True,
            "submitted": False,
        }

    config = WQAgentWorkflowConfig(
        output_dir=workdir / "presubmit_statement_filter",
        target_ready=1,
        max_total_simulations=2,
        cycle_candidate_count=2,
        max_simulations=2,
        max_cycles=1,
        fallback_template_limit=0,
        use_ledger=False,
    )
    summary = run_workflow(
        config,
        mode="presubmit-sequential",
        dependencies={
            "list_alphas": lambda config: [],
            "model_generate_candidates": fake_model,
            "simulate": fake_simulate,
            "check_submissions": lambda ids, config: {
                "alpha_open": {
                    "status": "UNSUBMITTED",
                    "sharpe": 1.8,
                    "fitness": 1.2,
                    "turnover": 0.25,
                    "sc_result": "PASS",
                    "sc_value": 0.52,
                    "prod_corr_result": "MISSING",
                }
            },
        },
    )

    assert summary["ok"] is True
    assert simulated_expressions == ["rank(open)"]
    skip_reasons = summary["presubmit_loop"]["cycles"][0]["candidate_skip"]["skip_reasons"]
    assert skip_reasons["unsupported_statement_separator"] == 1
