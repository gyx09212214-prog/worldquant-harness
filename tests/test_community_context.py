import json
from pathlib import Path

from worldquant_harness.community_context import (
    CommunityContext,
    resolve_cache_path,
    retrieve_community_context,
    summarize_community_skills,
)


def _write_jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _make_context_dir(tmp_path: Path) -> Path:
    triage = tmp_path / "triage"
    _write_jsonl(
        triage / "triage_records.jsonl",
        [
            {
                "post_id": "p1",
                "comment_id": None,
                "title": "Volume and VWAP ideas",
                "hypothesis": "Price-volume correlation can capture participation and flow structure.",
                "excerpt": "Try ts_corr(close, volume, 10) and close/vwap reversal.",
                "relevance_score": 100,
                "value_type": "candidate_seed",
                "wq_fields": ["close", "volume", "vwap"],
                "operators": ["rank", "ts_corr", "decay_linear"],
                "risk_flags": ["high_turnover"],
                "candidate_expressions": ["rank(ts_corr(close, volume, 10))"],
            },
            {
                "post_id": "p2",
                "comment_id": None,
                "title": "Platform limit",
                "hypothesis": "Correlation checks require changing field or operator family.",
                "excerpt": "Changing only windows does not fix self correlation.",
                "relevance_score": 80,
                "value_type": "failure_case",
                "wq_fields": ["returns"],
                "operators": ["rank"],
                "risk_flags": ["correlation_risk"],
                "candidate_expressions": [],
            },
        ],
    )
    _write_jsonl(
        triage / "community_wq_candidates.jsonl",
        [
            {
                "expression": "rank(ts_corr(close, volume, 10))",
                "tag": "community-volume",
                "source_post_id": "p1",
                "source_comment_id": None,
                "relevance_score": 100,
            },
            {
                "expression": "rank(volume / ts_mean(volume, 20))",
                "tag": "community-volume",
                "source_post_id": "p1",
                "source_comment_id": None,
                "relevance_score": 90,
            },
        ],
    )
    knowledge = triage / "knowledge_suggestions"
    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / "rules.md").write_text("# Rules\n\n- Change field family after correlation failure.\n", encoding="utf-8")
    (knowledge / "failures.md").write_text("# Failures\n\n- Watch high turnover in volume shocks.\n", encoding="utf-8")
    _write_jsonl(
        triage.parent / "skill_memory" / "community_skill_memory.jsonl",
        [
            {
                "schema_version": 1,
                "memory_kind": "community_submission_gate_skill",
                "skill_id": "community::submission_gate",
                "action": "Gate correlation failures with a field-family change before submit.",
                "selection_rule": {"route_when": ["correlation_risk"]},
                "evidence": {"record_count": 2},
                "anti_patterns": ["single-window tweak"],
            }
        ],
    )
    return triage


def test_context_loads_and_retrieves_relevant_notes(tmp_path):
    triage = _make_context_dir(tmp_path)
    context = CommunityContext.from_dir(triage)

    assert context is not None
    assert len(context.records) == 2
    assert len(context.candidates) == 2
    assert context.skill_summary()[0]["skill_id"] == "community::submission_gate"

    notes = context.retrieve(expression="rank(ts_corr(close, volume, 20))", limit=1)

    assert "Community-derived reference notes" in notes
    assert "Price-volume correlation" in notes
    assert "community::submission_gate" in notes
    assert "rank(ts_corr(close, volume, 10))" not in notes
    assert "derived templates withheld" in notes
    assert "Change field family" in notes

    diagnostic_notes = context.retrieve(
        expression="rank(ts_corr(close, volume, 20))",
        limit=1,
        include_candidate_templates=True,
    )

    assert "rank(ts_corr(close, volume, 10))" in diagnostic_notes


def test_skill_summary_strips_raw_templates_from_selection_rule():
    summary = summarize_community_skills(
        [
            {
                "skill_id": "forum_recipe::volume",
                "memory_kind": "community_alpha_template_skill",
                "action": "Transform forum recipe before use.",
                "selection_rule": {
                    "source_theme": "volume",
                    "template": "rank(ts_corr(close, volume, 10))",
                    "fields": ["close", "volume"],
                    "required": ["field-family change"],
                },
                "evidence": {"recipe_evidence": 3},
            }
        ]
    )

    assert summary[0]["selection_rule"] == {
        "source_theme": "volume",
        "fields": ["close", "volume"],
        "required": ["field-family change"],
    }


def test_seed_selection_dedupes_existing_expressions(tmp_path):
    triage = _make_context_dir(tmp_path)
    context = CommunityContext.from_dir(triage)

    seeds = context.seed_candidates(limit=5, existing_expressions=["rank(ts_corr(close, volume, 10))"])

    assert [seed.expression for seed in seeds] == ["rank(volume / ts_mean(volume, 20))"]
    assert seeds[0].strategy == "community_seed"
    assert seeds[0].diagnosis["source"] == "worldquant_community"


def test_retrieve_community_context_missing_dir_is_empty(tmp_path):
    assert retrieve_community_context(context_dir=tmp_path / "missing", expression="rank(close)") == ""


def test_context_writes_json_cache_at_env_path(tmp_path, monkeypatch):
    triage = _make_context_dir(tmp_path)
    cache_path = tmp_path / "cache" / "community_context.json"
    monkeypatch.setenv("WQ_COMMUNITY_CONTEXT_DB", str(cache_path))

    context = CommunityContext.from_dir(triage)

    assert context is not None
    assert resolve_cache_path(triage) == cache_path
    assert cache_path.is_file()

    cached = CommunityContext.from_dir(triage)
    assert cached is not None
    assert len(cached.records) == 2
