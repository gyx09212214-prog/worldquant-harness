from worldquant_harness.wq_profile_evolver import evolve_research_profile
from worldquant_harness.wq_research_profile import default_research_profile


def test_profile_evolver_generates_recommended_candidate_from_harness_metrics():
    profile = default_research_profile()
    summary = {
        "eval_id": "eval-test",
        "harness_score": 0.48,
        "metrics": {
            "self_correlation_reject_share": 0.3,
            "too_similar_reject_share": 0.35,
            "illegal_input_reject_share": 0.12,
            "duplicate_field_signature_count": 2,
            "ready_per_100_simulations": 0.5,
            "promote_submit_success_rate": 0.4,
        },
    }

    result = evolve_research_profile(profile, summary, field_signature_blacklist=["open"])

    assert result["recommended_candidate"] in {"candidate_a", "candidate_b", "candidate_c"}
    assert set(result["candidates"]) == {"candidate_a", "candidate_b", "candidate_c"}
    candidate_a = result["candidates"]["candidate_a"]["profile"]
    assert "open" in candidate_a["field_signature_policy"]["blacklist"]
    assert candidate_a["legal_input_policy"]["strict"] is True
    assert result["candidates"][result["recommended_candidate"]]["recommended"] is True
