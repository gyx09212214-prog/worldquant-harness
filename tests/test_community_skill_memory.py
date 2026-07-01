import json
from pathlib import Path

from worldquant_harness.community_skill_memory import CommunitySkillMemoryConfig, build_community_skill_memory


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_build_community_skill_memory_groups_experience_categories(tmp_path):
    triage = tmp_path / "triage"
    forum = tmp_path / "forum"
    output = tmp_path / "skill_memory"
    _write_jsonl(
        triage / "triage_records.jsonl",
        [
            {
                "post_id": "p1",
                "title": "Near pass",
                "excerpt": "Almost pass but self correlation fails.",
                "relevance_score": 90,
                "value_type": "failure_case",
                "experience_category": "near_pass_repair",
                "risk_flags": ["metric_near_pass", "correlation_risk"],
                "wq_fields": ["returns"],
                "operators": ["rank"],
            },
            {
                "post_id": "p2",
                "title": "Template",
                "excerpt": "Template using close and volume.",
                "relevance_score": 85,
                "value_type": "candidate_seed",
                "experience_category": "alpha_template",
                "risk_flags": ["template_clone_risk"],
                "wq_fields": ["close", "volume"],
                "operators": ["ts_corr", "rank"],
                "candidate_expressions": ["rank(ts_corr(close, volume, 10))"],
            },
            {
                "post_id": "p3",
                "title": "Unit check",
                "excerpt": "Unit check failed.",
                "relevance_score": 75,
                "value_type": "failure_case",
                "experience_category": "operation_attribution",
                "risk_flags": ["unit_check"],
                "wq_fields": ["assets"],
                "operators": ["rank"],
            },
        ],
    )
    _write_jsonl(
        triage / "community_wq_candidates.jsonl",
        [
            {
                "expression": "rank(ts_corr(close, volume, 10))",
                "tag": "community-template",
                "source_post_id": "p2",
                "source_comment_id": None,
                "relevance_score": 85,
                "experience_category": "alpha_template",
            }
        ],
    )
    _write_jsonl(
        forum / "forum_candidate_recipes.jsonl",
        [
            {
                "recipe_id": "sentiment_overlay",
                "source_theme": "sentiment_news_revision",
                "template": "rank(ts_delta(scl12_sentiment_fast_d1, 5))",
                "fields": ["scl12_sentiment_fast_d1"],
                "evidence_records": 4,
                "non_course_sources": 3,
            }
        ],
    )

    manifest = build_community_skill_memory(
        CommunitySkillMemoryConfig(triage_dir=triage, output_dir=output, forum_memory_dirs=(forum,))
    )
    skills = _read_jsonl(output / "community_skill_memory.jsonl")
    manifest_on_disk = json.loads((output / "community_skill_manifest.json").read_text(encoding="utf-8"))
    skill_ids = {row["skill_id"] for row in skills}

    assert manifest["skill_count"] == len(skills)
    assert manifest_on_disk["files"]["skills"].endswith("community_skill_memory.jsonl")
    assert "community::near_pass_repair" in skill_ids
    assert "community::alpha_template_transform" in skill_ids
    assert "community::operation_attribution" in skill_ids
    assert "forum_recipe::sentiment_overlay" in skill_ids
    assert (output / "community_skill_summary.md").is_file()
