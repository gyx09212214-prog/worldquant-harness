import csv
import json
import shutil
import uuid
from pathlib import Path

import pytest

from worldquant_harness.wq_forum_submission_optimizer import (
    ForumSubmissionOptimizerConfig,
    build_forum_submission_plan,
    evaluate_candidate_policy,
)


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / f"wq_forum_submission_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_build_forum_submission_plan_writes_policy_and_playbook(workdir):
    factor_map = workdir / "factor_map"
    memory = workdir / "memory"
    output = workdir / "out"
    _write_jsonl(factor_map / "nodes.jsonl", [{"node_id": "n1", "expression": "rank(cashflow_op / cap)"}])
    _write_jsonl(factor_map / "edges.jsonl", [])
    _write_csv(
        factor_map / "domain_summary.csv",
        [
            {
                "domain": "fundamental_quality",
                "crowded_score": 40,
                "opportunity_score": 55,
                "avg_intra_similarity": 0.2,
                "self_corr_fail_count": 2,
                "high_similarity_fail_count": 1,
                "active_or_submitted_count": 1,
            },
            {
                "domain": "liquidity_microstructure",
                "crowded_score": 120,
                "opportunity_score": 12,
                "avg_intra_similarity": 0.7,
                "self_corr_fail_count": 20,
                "high_similarity_fail_count": 10,
                "active_or_submitted_count": 8,
            },
            {
                "domain": "unknown",
                "crowded_score": 5,
                "opportunity_score": 60,
                "avg_intra_similarity": 0.1,
                "self_corr_fail_count": 0,
                "high_similarity_fail_count": 0,
                "active_or_submitted_count": 0,
            },
        ],
    )
    _write_csv(factor_map / "field_summary.csv", [{"field": "cashflow_op", "count": 1}])
    _write_jsonl(
        memory / "forum_idea_clusters_strict.jsonl",
        [
            {
                "theme_id": "fundamental_value_quality",
                "title": "Fundamental value quality",
                "logic": "cash-flow quality with reversal overlay",
                "member_count": 80,
                "non_course_count": 55,
                "course_noise_count": 4,
                "top_fields": [{"value": "cashflow_op", "count": 9}, {"value": "cap", "count": 8}],
                "top_operators": [{"value": "ts_rank", "count": 7}, {"value": "rank", "count": 5}],
            },
            {
                "theme_id": "fundamental_value_quality",
                "title": "Fundamental noisy copy",
                "logic": "lower-evidence duplicate from another memory build",
                "member_count": 2,
                "non_course_count": 1,
                "course_noise_count": 1,
                "top_fields": [{"value": "cashflow_op", "count": 1}],
                "top_operators": [{"value": "rank", "count": 1}],
            },
            {
                "theme_id": "correlation_similarity",
                "title": "Correlation controls",
                "logic": "avoid template clones",
                "member_count": 50,
                "non_course_count": 40,
                "course_noise_count": 0,
                "top_fields": [{"value": "returns", "count": 4}],
                "top_operators": [{"value": "ts_corr", "count": 6}],
            },
            {
                "theme_id": "missingness_coverage",
                "title": "Sparse coverage",
                "logic": "small-batch missingness probe",
                "member_count": 30,
                "non_course_count": 25,
                "course_noise_count": 1,
                "top_fields": [{"value": "custom_sparse_field", "count": 4}],
                "top_operators": [{"value": "is_nan", "count": 4}],
            },
        ],
    )
    _write_jsonl(
        memory / "forum_candidate_recipes.jsonl",
        [
            {
                "recipe_id": "fundamental_cashflow_overlay",
                "source_theme": "fundamental_value_quality",
                "template": "rank(ts_rank(cashflow_op / cap, 80) - ts_rank(returns, 20))",
                "fields": ["cashflow_op", "cap", "returns"],
                "evidence_records": 20,
                "non_course_sources": 18,
                "stop_if": ["self-correlation above strict cutoff"],
            }
        ],
    )
    _write_jsonl(memory / "forum_pattern_rules.jsonl", [{"rule_id": "direct", "logic": "do not submit direct snippets"}])
    _write_jsonl(memory / "forum_idea_theme_combinations.jsonl", [{"theme_a": "fundamental_value_quality", "theme_b": "correlation_similarity", "shared_sources": 3}])
    _write_jsonl(
        memory / "community_skill_memory.jsonl",
        [
            {
                "skill_id": "community::submission_gate",
                "memory_kind": "community_submission_gate_skill",
                "action": "Gate direct templates before submit.",
                "evidence": {"record_count": 3},
            }
        ],
    )

    plan = build_forum_submission_plan(ForumSubmissionOptimizerConfig(
        factor_map_dir=factor_map,
        forum_memory_dirs=(memory,),
        output_dir=output,
        max_directions=10,
    ))

    assert plan["ok"] is True
    assert plan["summary"]["community_skills"] == 1
    assert [row["direction_id"] for row in plan["directions"]].count("theme:fundamental_value_quality") == 1
    assert "fundamental_value_quality" in plan["submission_policy"]["theme_policies"]
    assert "correlation_similarity" in plan["submission_policy"]["rules"]
    assert plan["submission_policy"]["community_skill_policy"]["enabled"] is True
    assert plan["candidate_budget"]["allocations"]
    assert (output / "submission_policy.json").is_file()
    playbook = (output / "forum_submission_playbook.md").read_text(encoding="utf-8")
    assert playbook.startswith("---")
    assert "Community Skill Gates" in playbook


def test_candidate_policy_blocks_direct_forum_templates_without_overlay(workdir):
    plan = build_forum_submission_plan(ForumSubmissionOptimizerConfig(
        factor_map_dir=workdir / "missing_factor_map",
        forum_memory_dirs=(),
        output_dir=None,
    ))
    policy = plan["submission_policy"]

    blocked = evaluate_candidate_policy(
        {"expression": "rank(ts_rank(volume, 20))", "source_family": "forum_direct_triage"},
        policy,
    )
    allowed = evaluate_candidate_policy(
        {
            "expression": "rank(ts_rank(cashflow_op / cap, 80) - ts_rank(returns, 20))",
            "source_family": "forum_direct_triage",
        },
        policy,
    )

    assert blocked["action"] == "block"
    assert blocked["reason"] == "forum_direct_template_risk"
    assert allowed["action"] != "block"
    assert allowed["orthogonal_overlay"] is True


def test_candidate_policy_applies_community_skill_risk_flags(workdir):
    memory = workdir / "memory"
    factor_map = workdir / "factor_map"
    _write_jsonl(factor_map / "nodes.jsonl", [])
    _write_jsonl(factor_map / "edges.jsonl", [])
    _write_jsonl(memory / "community_skill_memory.jsonl", [
        {
            "skill_id": "community::alpha_template_transform",
            "memory_kind": "community_alpha_template_skill",
            "action": "Transform public templates before use.",
            "evidence": {"record_count": 2, "risk_counts": {"template_clone_risk": 2, "field_family_crowding": 1}},
        }
    ])
    policy = build_forum_submission_plan(ForumSubmissionOptimizerConfig(
        factor_map_dir=factor_map,
        forum_memory_dirs=(memory,),
        output_dir=None,
    ))["submission_policy"]

    blocked = evaluate_candidate_policy(
        {"expression": "rank(ts_rank(volume, 20))", "risk_flags": ["template_clone_risk"]},
        policy,
    )
    penalized = evaluate_candidate_policy(
        {
            "expression": "rank(ts_rank(cashflow_op / cap, 80) - ts_rank(returns, 20))",
            "risk_flags": ["field_family_crowding"],
        },
        policy,
    )

    assert blocked["action"] == "block"
    assert blocked["reason"] == "template_clone_risk"
    assert penalized["action"] == "penalize"
    assert "community_skill_risk:field_family_crowding" in penalized["reason"]


def test_submitted_alpha_map_policy_penalizes_returns_main_anchor(workdir):
    submitted_map = workdir / "submitted_map"
    submitted_map.mkdir()
    (submitted_map / "submitted_alpha_map_summary.json").write_text(json.dumps({
        "summary": {
            "active_or_submitted_count": 43,
            "median_nearest_similarity": 0.6258,
            "high_internal_similarity_pairs_ge_0_70": 8,
        }
    }), encoding="utf-8")
    _write_csv(
        submitted_map / "submitted_field_summary.csv",
        [
            {
                "field": "returns",
                "active_alpha_count": 38,
                "active_share": 0.8837,
                "map_node_count": 752,
                "map_self_corr_fail_count": 36,
                "map_avg_fitness": 0.9893,
            },
            {
                "field": "close",
                "active_alpha_count": 23,
                "active_share": 0.5349,
                "map_node_count": 493,
                "map_self_corr_fail_count": 25,
                "map_avg_fitness": 1.0114,
            },
            {
                "field": "volume",
                "active_alpha_count": 23,
                "active_share": 0.5349,
                "map_node_count": 450,
                "map_self_corr_fail_count": 15,
                "map_avg_fitness": 0.95,
            },
            {
                "field": "industry",
                "active_alpha_count": 13,
                "active_share": 0.3023,
                "map_node_count": 100,
                "map_self_corr_fail_count": 2,
                "map_avg_fitness": 1.0,
            },
            {
                "field": "subindustry",
                "active_alpha_count": 10,
                "active_share": 0.2326,
                "map_node_count": 90,
                "map_self_corr_fail_count": 1,
                "map_avg_fitness": 1.0,
            },
        ],
    )
    _write_csv(
        submitted_map / "submitted_domain_summary.csv",
        [
            {
                "domain": "fundamental_quality",
                "active_count": 7,
                "share": 0.1628,
                "avg_nearest_similarity": 0.7744,
                "map_crowded_score": 209.295,
                "map_opportunity_score": 280.7887,
                "map_high_similarity_fail_count": 73,
                "map_self_corr_fail_count": 8,
            }
        ],
    )
    _write_csv(
        submitted_map / "submitted_similarity_pairs.csv",
        [
            {
                "alpha_id_a": "a",
                "alpha_id_b": "b",
                "overall_similarity": 0.91,
                "field_overlap": 0.8,
                "operator_overlap": 1.0,
            }
        ],
    )

    plan = build_forum_submission_plan(ForumSubmissionOptimizerConfig(
        factor_map_dir=workdir / "missing_factor_map",
        forum_memory_dirs=(),
        submitted_alpha_map_dir=submitted_map,
        output_dir=None,
    ))
    policy = plan["submission_policy"]

    returns_only = evaluate_candidate_policy({"expression": "rank(ts_rank(returns, 60))"}, policy)
    returns_main = evaluate_candidate_policy(
        {"expression": "rank(ts_rank(cashflow_op / cap, 80) - ts_rank(returns, 40))"},
        policy,
    )
    returns_control = evaluate_candidate_policy(
        {"expression": "rank(0.60 * ts_rank(cashflow_op / cap, 80) - 0.10 * ts_rank(returns, 40))"},
        policy,
    )
    neutralized_stack = evaluate_candidate_policy(
        {
            "expression": (
                "rank(ts_rank(cashflow_op / cap, 80) + "
                "group_rank(ts_rank(close, 20), industry) + "
                "group_rank(ts_rank(volume, 20), subindustry))"
            )
        },
        policy,
    )

    assert policy["submitted_alpha_map"]["overused_anchor_fields"][0] == "returns"
    assert returns_only["action"] == "block"
    assert "returns_or_price_liquidity_only" in returns_only["reason"]
    assert returns_main["action"] == "penalize"
    assert "returns_main_anchor" in returns_main["reason"]
    assert returns_control["action"] == "allow"
    assert "returns_risk_control_use" in returns_control["reason"]
    assert neutralized_stack["submitted_alpha_constraints"]["used_saturated_fields"] == ["close", "volume"]
