import shutil
import uuid
from pathlib import Path

import pytest

from worldquant_harness.wq_research_profile import (
    apply_candidate,
    candidate_diff,
    init_profile,
    load_profile,
    profile_status,
    profile_to_gate,
    profile_to_mine_config,
    save_profile,
)


@pytest.fixture
def profile_dir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / f"wq_research_profile_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_profile_init_diff_apply_and_config(profile_dir):
    init = init_profile(profile_dir=profile_dir)
    assert init["ok"] is True

    active = load_profile(profile_dir=profile_dir)
    candidate = dict(active)
    candidate["profile_version"] = 1
    candidate["similarity_policy"] = {"cutoff": 0.66}
    candidate["priority_biases"] = ["low_overlap_field_family"]
    save_profile("candidate_a", candidate, profile_dir=profile_dir, as_candidate=True)

    diff = candidate_diff("candidate_a", profile_dir=profile_dir)
    assert diff["change_count"] > 0
    assert any(row["path"] == "similarity_policy.cutoff" for row in diff["changes"])

    applied = apply_candidate("candidate_a", profile_dir=profile_dir)
    assert applied["ok"] is True
    status = profile_status(profile_dir=profile_dir)
    assert status["active_profile"] == "default"
    assert status["candidates"] == ["candidate_a"]

    mine = profile_to_mine_config(load_profile(profile_dir=profile_dir))
    gate = profile_to_gate(load_profile(profile_dir=profile_dir))
    assert mine["similarity_cutoff"] == 0.66
    assert mine["no_real_submit"] is True
    assert gate["max_daily_return_correlation"] == 0.7
