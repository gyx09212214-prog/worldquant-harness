from __future__ import annotations

from pathlib import Path

from scripts import build_wq_submit10_after30_final_evidence as builder


def test_after30_builder_binds_the_final_goal_scope(monkeypatch) -> None:
    captured = {}
    expected = {
        "counts": {
            "strict_active_baseline": 30,
            "new_active_verified": 10,
            "combined_strict_active": 40,
        }
    }

    def fake_build_evidence(root: Path, **kwargs):
        captured["root"] = root
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(builder.base, "build_evidence", fake_build_evidence)
    assert builder.build_evidence(Path("fixture-root")) is expected
    assert captured["expected_ids"] == builder.EXPECTED_IDS
    assert captured["baseline_dir"] == builder.BASELINE_DIR
    assert captured["final_get_dir"] == builder.FINAL_GET_DIR
    assert captured["goal_run_glob"] == builder.GOAL_RUN_GLOB
    assert captured["baseline_count"] == 30
