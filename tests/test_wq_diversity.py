from worldquant_harness.wq_diversity import (
    DiversityPolicy,
    build_diversity_record,
    core_signature,
    evaluate_diversity,
    final_diversity_summary,
    weighted_field_jaccard,
)


def _record(expression: str, *, alpha_id: str, family: str = "fixture", core: str | None = None):
    row = {"expression": expression, "alpha_id": alpha_id, "diversity_family": family}
    if core:
        row["diversity_core_signature"] = core
    return build_diversity_record(row)


def test_weighted_field_jaccard_downweights_market_and_grouping_fields():
    left = ["research_a", "returns", "sector"]
    right = ["research_b", "returns", "sector"]

    # Shared weight = .5 + .25; union weight = 1 + 1 + .5 + .25.
    assert weighted_field_jaccard(left, right) == 0.272727


def test_diversity_rejects_duplicate_signature_and_legacy_overlap():
    candidate = {
        "expression": "rank(anl_signal_a / vwap) + rank(returns)",
        "diversity_family": "analyst",
    }
    same_fields = _record(
        "rank(anl_signal_a / vwap) - rank(returns)",
        alpha_id="new-1",
        family="analyst",
    )
    legacy_same = _record(candidate["expression"], alpha_id="old-1", family="analyst")

    gate = evaluate_diversity(candidate, [same_fields], [legacy_same])

    assert gate["ok"] is False
    assert "duplicate_new_field_signature" in gate["reasons"]
    assert "legacy_inventory_weighted_jaccard_exceeded" in gate["reasons"]


def test_diversity_enforces_core_field_and_family_capacities():
    policy = DiversityPolicy(
        max_core_signature_count=2,
        max_research_field_count=3,
        max_family_count=4,
    )
    accepted = [
        _record(
            f"rank(shared_research + unique_{index})",
            alpha_id=f"a-{index}",
            family="analyst",
            core="shared-core" if index < 2 else f"core-{index}",
        )
        for index in range(4)
    ]
    candidate = {
        "expression": "rank(shared_research + fresh_research)",
        "diversity_family": "analyst",
        "diversity_core_signature": "shared-core",
    }

    gate = evaluate_diversity(candidate, accepted, [], policy=policy)

    assert gate["ok"] is False
    assert "core_signature_capacity_reached" in gate["reasons"]
    assert "research_field_capacity_reached" in gate["reasons"]
    assert "diversity_family_capacity_reached" in gate["reasons"]
    assert gate["capacity_fields"]["research"] == ["shared_research"]


def test_core_signature_uses_seed_identity_for_partition_siblings():
    first = {
        "expression": "rank(signal_a) * (rank(mask_a) > 0.1)",
        "partition_design": {"seed_alpha_id": "seed-123"},
    }
    second = {
        "expression": "rank(signal_a) * (rank(mask_b) > 0.8)",
        "partition_design": {"seed_alpha_id": "seed-123"},
    }

    assert core_signature(first) == core_signature(second) == "seed-alpha:seed-123"


def test_final_summary_reports_family_and_signature_contract():
    accepted = [
        _record(f"rank(research_{index})", alpha_id=f"a-{index}", family=f"family-{index}")
        for index in range(8)
    ]

    summary = final_diversity_summary(accepted)

    assert summary["family_count"] == 8
    assert summary["family_minimum_met"] is True
    assert summary["field_signature_unique_count"] == 8
    assert summary["field_signature_duplicate_count"] == 0
