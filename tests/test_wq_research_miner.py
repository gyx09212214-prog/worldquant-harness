import json
import shutil
import uuid
from pathlib import Path

import pytest

from quantgpt.wq_research_miner import (
    WQResearchMinerConfig,
    build_experience_memory,
    run_research_miner,
    screen_candidate_drafts,
)


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / f"wq_research_miner_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_build_experience_memory_distills_success_and_failure_kinds():
    ready = [{
        "alpha_id": "ready1",
        "expression": "rank(close)",
        "tag": "ready-close",
        "sharpe": 1.5,
        "fitness": 1.1,
        "turnover": 0.2,
        "sc_value": 0.62,
    }]
    rejected = [
        {
            "alpha_id": "sc1",
            "expression": "rank(ts_rank(cashflow_op / cap, 80) - ts_rank(returns, 20))",
            "presubmit_reject_reason": "self_correlation_value_above_strict_cutoff",
            "sharpe": 2.0,
            "fitness": 1.5,
            "turnover": 0.16,
            "sc_value": 0.76,
        },
        {
            "alpha_id": "sub1",
            "expression": "rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 5))",
            "status": "failed_platform_check",
            "sharpe": 1.9,
            "fitness": 1.4,
            "turnover": 0.11,
            "failed_platform_checks": [{"name": "LOW_SUB_UNIVERSE_SHARPE", "result": "FAIL", "value": 0.4}],
        },
    ]

    memory = build_experience_memory(ready, rejected)

    assert [row["memory_kind"] for row in memory].count("success_ready") == 1
    assert {row["failure_kind"] for row in memory} >= {
        "accepted",
        "self_correlation_high",
        "platform_distribution_fail",
    }
    sc_memory = next(row for row in memory if row["failure_kind"] == "self_correlation_high")
    assert "orthogonal overlay" in sc_memory["lesson"]
    assert sc_memory["fields"] == ["cap", "cashflow_op", "returns"]


def test_screen_candidate_drafts_blocks_duplicates_and_limits_families(workdir):
    output = workdir / "candidates.jsonl"
    config = WQResearchMinerConfig(
        output=output,
        max_candidates=5,
        similarity_cutoff=0.65,
        max_family_count=1,
        max_field_signature_count=2,
    )
    active = [{"alpha_id": "active1", "expression": "rank(close)", "status": "ACTIVE"}]
    drafts = [
        {"expression": "rank(close)", "tag": "duplicate", "source_family": "family_a"},
        {
            "expression": "rank(ts_mean(implied_volatility_call_90 - implied_volatility_put_90, 10))",
            "tag": "iv10",
            "source_family": "family_a",
        },
        {
            "expression": "rank(ts_corr(vwap, volume, 40))",
            "tag": "vwap-volume",
            "source_family": "family_a",
        },
        {
            "expression": "rank(ts_rank(assets, 20) - ts_rank(returns, 20))",
            "tag": "asset-return",
            "source_family": "family_a",
        },
    ]

    selected, rejected = screen_candidate_drafts(drafts, active, config=config)

    assert [row["tag"] for row in selected] == ["vwap-volume"]
    assert {row["reject_reason"] for row in rejected} == {
        "duplicate_or_active_expression",
        "pure_options_only_distribution_risk",
        "family_capacity_reached",
    }
    assert selected[0]["no_external_llm"] is True
    assert selected[0]["candidate_rank"] if "candidate_rank" in selected[0] else True


def test_run_research_miner_generates_deterministic_local_candidates(workdir):
    ready_file = workdir / "ready.jsonl"
    rejected_file = workdir / "rejected.jsonl"
    active_file = workdir / "active_inventory.json"
    output = workdir / "generated.jsonl"
    memory_output = workdir / "memory.jsonl"
    summary_output = workdir / "summary.json"

    _write_jsonl(ready_file, [{
        "alpha_id": "ready1",
        "expression": "rank(0.25 * rank(power((high - close) / (high - low), 2)) + 0.75 * rank(-1 * multi_factor_acceleration_score_derivative))",
        "tag": "ready-mfactor",
        "sharpe": 1.32,
        "fitness": 1.07,
        "turnover": 0.0864,
        "sc_value": 0.6771,
    }])
    _write_jsonl(rejected_file, [{
        "alpha_id": "near1",
        "expression": "rank(0.50 * ts_rank(cashflow_op / cap, 80) + 0.50 * rank(-1 * cashflow_efficiency_rank_derivative) - ts_rank(returns, 30))",
        "tag": "cfop-near",
        "presubmit_reject_reason": "self_correlation_value_above_strict_cutoff",
        "sharpe": 2.08,
        "fitness": 1.75,
        "turnover": 0.1659,
        "sc_value": 0.7075,
    }])
    active_file.write_text(json.dumps({"active": [{"alpha_id": "active1", "expression": "rank(open)", "status": "ACTIVE"}]}), encoding="utf-8")

    summary = run_research_miner(WQResearchMinerConfig(
        output=output,
        ready_files=(ready_file,),
        rejected_files=(rejected_file,),
        active_inventory_files=(active_file,),
        memory_output=memory_output,
        summary_output=summary_output,
        max_candidates=4,
    ))

    generated = _read_jsonl(output)
    memory = _read_jsonl(memory_output)
    saved_summary = json.loads(summary_output.read_text(encoding="utf-8"))

    assert summary["ok"] is True
    assert summary["no_external_llm"] is True
    assert saved_summary["outputs"]["candidates"] == len(generated)
    assert any(row["failure_kind"] == "self_correlation_high" for row in memory)
    assert any("implied_volatility_call_" in row["expression"] for row in generated)
    assert all("short_interest" not in row["expression"] for row in generated)
    assert all("short_ratio" not in row["expression"] for row in generated)
    assert all(row["source"] == "wq_research_miner" for row in generated)


def test_run_dirs_load_prior_artifacts_and_platform_memory(workdir):
    run_dir = workdir / "prior_run"
    run_dir.mkdir()
    output = workdir / "generated.jsonl"

    _write_jsonl(run_dir / "presubmit_ready_sequential.jsonl", [{
        "alpha_id": "ready1",
        "expression": "rank(ts_rank(cashflow_op / cap, 80) - ts_rank(returns, 20))",
        "sharpe": 1.4,
        "fitness": 1.1,
        "turnover": 0.12,
    }])
    _write_jsonl(run_dir / "presubmit_rejected.jsonl", [{
        "alpha_id": "reject1",
        "expression": "rank(ts_rank(cashflow_op / cap, 100) - ts_rank(returns, 40))",
        "presubmit_reject_reason": "self_correlation_value_above_strict_cutoff",
        "sharpe": 1.6,
        "fitness": 1.2,
        "turnover": 0.15,
        "sc_value": 0.76,
    }])
    (run_dir / "active_inventory.json").write_text(
        json.dumps({"active": [{"alpha_id": "active1", "expression": "rank(open)", "status": "ACTIVE"}]}),
        encoding="utf-8",
    )
    _write_jsonl(run_dir / "platform_alphas.jsonl", [{
        "alpha_id": "platform1",
        "expression": "rank(ts_rank(ebit / enterprise_value, 60) - ts_rank(returns, 20))",
        "status": "UNSUBMITTED",
        "sharpe": 1.7,
        "fitness": 1.1,
        "turnover": 0.2,
    }])

    summary = run_research_miner(WQResearchMinerConfig(
        output=output,
        run_dirs=(run_dir,),
        max_candidates=12,
        similarity_cutoff=0.72,
        max_family_count=6,
        max_field_signature_count=4,
    ))

    generated = _read_jsonl(output)
    assert summary["inputs"]["run_dirs"] == 1
    assert summary["inputs"]["ready"] == 1
    assert summary["inputs"]["rejected"] == 1
    assert summary["inputs"]["active_inventory"] == 1
    assert summary["inputs"]["platform"] == 1
    assert any(row["tag"] == "platform-memory-platform1" for row in generated)


def test_run_dirs_load_cycle_failures_and_reject_platform_snippets(workdir):
    run_dir = workdir / "prior_run"
    cycle_dir = run_dir / "cycles" / "cycle_001"
    cycle_dir.mkdir(parents=True)
    output = workdir / "generated.jsonl"

    high_sc_expression = (
        "rank(0.35 * ts_rank(actual_sales_value_quarterly / cap, 60) "
        "+ 0.30 * ts_rank(actual_eps_value_quarterly / close, 60) "
        "+ 0.35 * ts_rank(change_in_eps_surprise, 60) - ts_rank(returns, 20))"
    )
    _write_jsonl(cycle_dir / "review_queue.jsonl", [{
        "alpha_id": "cycle_sc",
        "expression": high_sc_expression,
        "tag": "cycle-high-sc",
        "triage_bucket": "confirmed_ready",
        "sharpe": 2.0,
        "fitness": 1.4,
        "turnover": 0.2,
        "sc_value": 0.81,
    }])
    _write_jsonl(run_dir / "platform_alphas.jsonl", [{
        "alpha_id": "snippet1",
        "expression": "/* comment */ ts_rank(operating_income,252)-returns",
        "status": "UNSUBMITTED",
        "sharpe": 1.4,
        "fitness": 1.1,
        "turnover": 0.2,
    }])

    summary = run_research_miner(WQResearchMinerConfig(
        output=output,
        run_dirs=(run_dir,),
        max_candidates=12,
        similarity_cutoff=0.72,
        max_family_count=6,
        max_field_signature_count=4,
    ))

    memory = _read_jsonl(output.with_name("experience_memory.jsonl"))
    generated = _read_jsonl(output)
    assert summary["inputs"]["rejected"] == 1
    assert any(row["failure_kind"] == "self_correlation_high" and row["alpha_id"] == "cycle_sc" for row in memory)
    assert all(row["expression"] != high_sc_expression for row in generated)
    assert all("/*" not in row["expression"] for row in generated)
    assert summary["counts"]["screen_reject_reason"]["unsupported_embedded_comment"] == 1


def test_screen_candidate_drafts_blocks_historical_rejections(workdir):
    output = workdir / "candidates.jsonl"
    config = WQResearchMinerConfig(output=output, max_candidates=5)
    expression = "rank(ts_rank(cashflow_op / cap, 80) - ts_rank(returns, 20))"

    selected, rejected = screen_candidate_drafts(
        [{"expression": expression, "tag": "known-fail", "source_family": "family_a"}],
        [],
        config=config,
        blocked_rows=[{"expression": expression}],
    )

    assert selected == []
    assert rejected[0]["reject_reason"] == "historical_rejected_expression"


def test_research_miner_uses_weak_active_memory_for_repairs(workdir):
    weak_memory_file = workdir / "weak_active_memory.jsonl"
    output = workdir / "generated.jsonl"
    weak_expression = "rank(ts_rank(cashflow_op / cap, 80) - ts_rank(returns, 20))"
    _write_jsonl(weak_memory_file, [{
        "memory_kind": "weak_active_constraint",
        "severity": "penalize",
        "failure_kind": "active_low_returns",
        "lesson": "Do not reuse as a standalone return signal.",
        "repair_hints": ["test inversion or use only as a low-weight contrarian overlay"],
        "alpha_id": "weak_active_1",
        "status": "ACTIVE",
        "expression": weak_expression,
        "weak_score": 7.0,
        "quality_percentile": 0.0,
        "weak_reasons": ["negative_returns", "low_fitness"],
    }])

    summary = run_research_miner(WQResearchMinerConfig(
        output=output,
        weak_memory_files=(weak_memory_file,),
        max_candidates=8,
        similarity_cutoff=0.72,
        max_family_count=6,
        max_field_signature_count=6,
    ))

    generated = _read_jsonl(output)
    memory = _read_jsonl(output.with_name("experience_memory.jsonl"))

    assert summary["inputs"]["weak_active_memory"] == 1
    assert any(row["memory_kind"] == "weak_active_constraint" for row in memory)
    assert all(row["expression"] != weak_expression for row in generated)
    assert any(
        "weak_active_1" in (row.get("parent_alpha_ids") or [])
        and str(row.get("mutation_strategy") or "").startswith("weak_active_")
        for row in generated
    )


def test_screen_candidate_drafts_penalizes_weak_active_signatures(workdir):
    output = workdir / "candidates.jsonl"
    config = WQResearchMinerConfig(output=output, max_candidates=5)
    expression = "rank(ts_rank(cashflow_op / cap, 80) - ts_rank(returns, 20))"

    selected, rejected = screen_candidate_drafts(
        [{"expression": expression, "tag": "weak-repeat", "source_family": "family_a"}],
        [],
        config=config,
        weak_memory_rows=[{
            "memory_kind": "weak_active_constraint",
            "failure_kind": "active_low_returns",
            "expression": expression,
        }],
    )

    assert selected == []
    assert rejected[0]["reject_reason"] == "weak_active_signature_risk"


def test_screen_candidate_drafts_applies_forum_submission_policy(workdir):
    output = workdir / "candidates.jsonl"
    config = WQResearchMinerConfig(output=output, max_candidates=5, max_family_count=5)
    policy = {
        "gates": {"low_priority_reject_below": 15.0},
        "crowded_domains": [],
        "underexplored_domains": [],
        "theme_policies": {
            "fundamental_value_quality": {
                "action": "prefer",
                "research_priority_score": 55.0,
                "domains": ["fundamental_quality"],
            }
        },
        "recipe_policies": {},
    }

    selected, rejected = screen_candidate_drafts(
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
        [],
        config=config,
        submission_policy=policy,
    )

    assert [row["tag"] for row in selected] == ["cashflow-overlay"]
    assert selected[0]["forum_policy_action"] == "allow"
    assert selected[0]["forum_policy"]["orthogonal_overlay"] is True
    assert rejected[0]["reject_reason"] == "forum_direct_template_risk"


def test_research_miner_broadens_candidate_pack_for_long_runs(workdir):
    ready_file = workdir / "ready.jsonl"
    rejected_file = workdir / "rejected.jsonl"
    output = workdir / "generated.jsonl"

    _write_jsonl(ready_file, [{
        "alpha_id": "ready1",
        "expression": "rank(0.34 * ts_rank(forward_cash_flow_to_price, 60) + 0.33 * ts_rank(forward_book_value_to_price, 60) + 0.33 * ts_rank(earnings_momentum_composite_score, 50) - ts_rank(returns, 20))",
        "sharpe": 1.87,
        "fitness": 1.05,
        "turnover": 0.275,
        "sc_value": 0.6947,
    }])
    _write_jsonl(rejected_file, [{
        "alpha_id": "near1",
        "expression": "rank(0.40 * ts_rank(cashflow_op / cap, 100) + 0.45 * rank(-1 * cashflow_efficiency_rank_derivative) + 0.15 * rank(-1 * earnings_certainty_rank_derivative) - ts_rank(returns, 40))",
        "presubmit_reject_reason": "self_correlation_value_above_strict_cutoff",
        "sharpe": 1.88,
        "fitness": 1.6,
        "turnover": 0.1564,
        "sc_value": 0.7683,
    }])

    summary = run_research_miner(WQResearchMinerConfig(
        output=output,
        ready_files=(ready_file,),
        rejected_files=(rejected_file,),
        max_candidates=30,
        similarity_cutoff=0.72,
        max_family_count=8,
        max_field_signature_count=4,
    ))

    generated = _read_jsonl(output)
    families = {row["source_family"] for row in generated}
    assert summary["outputs"]["candidates"] >= 20
    assert len(generated) >= 20
    assert len(families) >= 8
    assert all(row["llm_provider"] == "none" for row in generated)
