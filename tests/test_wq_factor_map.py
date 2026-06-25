import json

import pytest

from worldquant_harness.models import WQAlphaExperiment, WQFailureMemory
from worldquant_harness.wq_factor_map import FactorMapConfig, build_factor_map, build_similarity_edges


@pytest.mark.asyncio
async def test_build_factor_map_merges_sources_and_writes_artifacts(db_session, tmp_path):
    exp_hit = WQAlphaExperiment(
        alpha_id="alpha_hit",
        expression="rank(close / open)",
        expression_normalized="rank(close/open)",
        expression_hash="h_hit",
        params_hash="p1",
        account="primary",
        region="USA",
        universe="TOP3000",
        delay=1,
        decay=6,
        neutralization="SUBINDUSTRY",
        truncation=0.08,
        source_type="find_only",
        source_family="price_reversal",
        lifecycle_status="active",
        platform_status="ACTIVE",
        sharpe=1.8,
        fitness=1.2,
        turnover=0.12,
    )
    exp_risky = WQAlphaExperiment(
        alpha_id="alpha_sc",
        expression="rank(close / open + 0.01 * returns)",
        expression_normalized="rank(close/open+0.01*returns)",
        expression_hash="h_sc",
        params_hash="p1",
        account="primary",
        region="USA",
        universe="TOP3000",
        delay=1,
        decay=6,
        neutralization="SUBINDUSTRY",
        truncation=0.08,
        source_type="api_check",
        source_family="price_reversal",
        lifecycle_status="self_corr_fail",
        platform_status="UNSUBMITTED",
        failure_kind="self_correlation_fail",
        self_correlation_result="FAIL",
        self_correlation_value=0.83,
        self_correlation_limit=0.70,
        sharpe=1.7,
        fitness=1.1,
        turnover=0.10,
    )
    memory = WQFailureMemory(
        memory_type="constraint",
        scope="global",
        expression="rank(close / open + 0.01 * returns)",
        expression_normalized="rank(close/open+0.01*returns)",
        expression_hash="h_sc",
        pattern_signature="price_reversal_self_corr",
        fields=["close", "open", "returns"],
        operators=["rank"],
        failure_kind="self_correlation_fail",
        severity="block",
        confidence=1.0,
        evidence_count=2,
    )
    db_session.add_all([exp_hit, exp_risky, memory])
    await db_session.commit()

    forum_file = tmp_path / "community_wq_candidates.jsonl"
    forum_file.write_text(
        json.dumps({
            "expression": "rank(ts_mean(adv20, 10))",
            "source": "wq_forum_artifact",
            "topic": "liquidity breadth",
            "status": "idea",
        }) + "\n",
        encoding="utf-8",
    )

    output_dir = tmp_path / "factor_map"
    obsidian_output = tmp_path / "obsidian.md"
    report = await build_factor_map(
        db_session,
        FactorMapConfig(
            input_paths=(forum_file,),
            output_dir=output_dir,
            obsidian_output=obsidian_output,
            similarity_threshold=0.55,
            max_edge_nodes=50,
            max_edges=20,
        ),
    )

    assert report["summary"]["nodes"] == 3
    assert report["summary"]["edges"] >= 1
    assert any(row["domain"] == "price_reversal" for row in report["domain_summary"])
    price_summary = next(row for row in report["domain_summary"] if row["domain"] == "price_reversal")
    assert price_summary["self_corr_fail_count"] == 1
    assert any(row["domain"] == "liquidity_microstructure" and row["forum_count"] == 1 for row in report["domain_summary"])
    assert (output_dir / "nodes.jsonl").is_file()
    assert (output_dir / "edges.jsonl").is_file()
    assert (output_dir / "factor_map.md").is_file()
    assert obsidian_output.is_file()
    assert "worldquant-harness 因子地图" in obsidian_output.read_text(encoding="utf-8")


def test_build_similarity_edges_marks_same_family_without_high_score():
    nodes = [
        {
            "node_id": "N1",
            "expression": "rank(close)",
            "domain": "price_reversal",
            "family_hash": "fam",
            "fields": ["close"],
            "operators": ["rank"],
        },
        {
            "node_id": "N2",
            "expression": "zscore(volume)",
            "domain": "liquidity_microstructure",
            "family_hash": "fam",
            "fields": ["volume"],
            "operators": ["zscore"],
        },
    ]

    edges = build_similarity_edges(nodes, threshold=0.99, max_nodes=10, max_edges=10)

    assert edges
    assert edges[0]["edge_type"] == "same_family"
