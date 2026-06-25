import json
from pathlib import Path

import pytest

from worldquant_harness.harness_contracts import (
    HarnessEvent,
    ProfilePatch,
    artifact_ref,
    read_jsonl,
    write_jsonl,
)


def test_artifact_ref_hashes_file_content(tmp_path: Path):
    path = tmp_path / "artifact.json"
    path.write_text('{"ok": true}\n', encoding="utf-8")

    ref = artifact_ref(path, "test_artifact", producer_step="unit_test")

    assert ref.path == str(path)
    assert ref.artifact_type == "test_artifact"
    assert ref.producer_step == "unit_test"
    assert ref.content_hash.startswith("sha256:")
    assert len(ref.content_hash) == len("sha256:") + 64


def test_event_contract_rejects_unknown_role():
    event = HarnessEvent(
        event_id="e1",
        run_id="r1",
        event_type="run_created",
        role="planner",
    )

    with pytest.raises(ValueError, match="invalid role"):
        event.to_dict()


def test_profile_patch_requires_no_submit_true():
    patch = ProfilePatch(target_profile="default", no_submit=False)

    with pytest.raises(ValueError, match="no_submit"):
        patch.to_dict()


def test_jsonl_roundtrip_validates_contract_payload(tmp_path: Path):
    path = tmp_path / "rows.jsonl"
    rows = [{"case_id": "ready_candidate", "passed": True, "no_submit": True}]

    write_jsonl(path, rows)

    assert read_jsonl(path) == rows
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["case_id"] == "ready_candidate"


def test_jsonl_writer_rejects_submit_boundary_crossing(tmp_path: Path):
    with pytest.raises(ValueError, match="real submit"):
        write_jsonl(tmp_path / "bad.jsonl", [{"real_submit_attempted": True}])

    with pytest.raises(ValueError, match="no_real_submit"):
        write_jsonl(tmp_path / "bad_no_real_submit.jsonl", [{"no_real_submit": False}])
