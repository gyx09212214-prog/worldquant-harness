from worldquant_harness.wq_knowledge_evolution import build_wq_knowledge_snippet


def test_knowledge_snippet_renders_reference_and_harness_signals():
    snippet = build_wq_knowledge_snippet(
        {
            "eval_id": "eval-1",
            "harness_score": 0.61,
            "metrics": {
                "ready_per_100_simulations": 1.2,
                "self_correlation_reject_share": 0.2,
                "too_similar_reject_share": 0.3,
                "illegal_input_reject_share": 0.0,
                "duplicate_field_signature_count": 1,
            },
            "gate": {"decision": "pass"},
        },
        catalog_status={
            "source_url": "https://example.test/ref",
            "reference_dir": "references/wq_alpha_research",
            "summary": {
                "field_count": 4367,
                "category_counts": {"fundamental": 1652, "pv": 195},
            },
        },
    )

    assert "Field count: 4367" in snippet
    assert "Ready per 100 simulations: 1.2" in snippet
    assert "train/validation/test" in snippet
