import json
from pathlib import Path

from quantgpt.wq_forum_expression_expander import (
    WQForumExpressionExpanderConfig,
    build_forum_expression_expansion,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_forum_expression_expander_writes_screened_candidates(tmp_path):
    memory = tmp_path / "forum_memory"
    output = tmp_path / "out"
    policy = tmp_path / "submission_policy.json"
    active = tmp_path / "active_inventory.json"
    _write_jsonl(memory / "forum_candidate_recipes.jsonl", [
        {
            "recipe_id": "industry_rank_value_anchor",
            "source_theme": "internal_group_compare",
            "fields": ["cashflow_op / enterprise_value", "actual_sales_value_quarterly / assets"],
        },
        {
            "recipe_id": "sentiment_revision_overlay",
            "source_theme": "sentiment_news_revision",
            "fields": ["snt1_d1_netearningsrevision", "scl12_sentiment_fast_d1"],
        },
    ])
    policy.write_text(json.dumps({
        "gates": {"low_priority_reject_below": 0},
        "theme_policies": {
            "internal_group_compare": {"action": "prefer", "research_priority_score": 55},
            "sentiment_news_revision": {"action": "prefer", "research_priority_score": 45},
        },
        "recipe_policies": {},
        "crowded_domains": [],
        "underexplored_domains": [],
    }), encoding="utf-8")
    active.write_text(json.dumps({"active": [{"alpha_id": "a1", "expression": "rank(close)", "status": "ACTIVE"}]}), encoding="utf-8")

    plan = build_forum_expression_expansion(WQForumExpressionExpanderConfig(
        forum_memory_dirs=(memory,),
        active_inventory_files=(active,),
        submission_policy_file=policy,
        output_dir=output,
        max_candidates=8,
    ))

    candidates = _read_jsonl(output / "forum_expansion_candidates.jsonl")
    assert plan["summary"]["selected"] == len(candidates)
    assert candidates
    assert all(row["source"] == "wq_forum_expression_expander" for row in candidates)
    assert any(row["forum_theme_id"] == "internal_group_compare" for row in candidates)
    assert (output / "forum_expression_expansion.md").read_text(encoding="utf-8").startswith("---")
