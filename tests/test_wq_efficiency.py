from worldquant_harness.wq_efficiency import annotate_candidate_identity, candidate_uid, settings_hash


def test_candidate_uid_is_stable_for_expression_and_settings():
    settings = {"region": "USA", "universe": "TOP3000", "delay": 1, "decay": 8, "neutralization": "SUBINDUSTRY"}

    left = candidate_uid("rank(close)", settings)
    right = candidate_uid(" rank ( close ) ", settings)
    changed = candidate_uid("rank(close)", {**settings, "decay": 12})

    assert left == right
    assert left != changed


def test_annotate_candidate_identity_preserves_efficiency_settings():
    row = annotate_candidate_identity(
        {"expression": "rank(ts_rank(close, 20))", "tag": "demo"},
        {"region": "USA", "universe": "TOP3000", "delay": 1, "decay": 8},
    )

    reannotated = annotate_candidate_identity(row)

    assert row["candidate_uid"] == reannotated["candidate_uid"]
    assert row["settings_hash"] == reannotated["settings_hash"]
    assert row["settings_hash"] == settings_hash(row["efficiency_settings"])
    assert row["field_signature"] == "close"
