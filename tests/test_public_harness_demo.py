import json
import shutil
import uuid
from pathlib import Path

import pytest

from scripts.run_public_harness_demo import run_public_harness_demo
from scripts.validate_public_harness_artifacts import validate_public_harness_artifacts


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / f"public_harness_demo_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_public_harness_demo_writes_replayable_artifacts_without_submit(workdir):
    result = run_public_harness_demo(workdir, run_id="eval-demo-test")

    assert result["ok"] is True
    assert result["real_submit_attempted"] is False

    files = result["files"]
    for key in ("candidate_specs", "presubmit_summary", "ready", "rejected", "eval_summary", "run_report", "evolution_result"):
        assert Path(files[key]).is_file(), key

    summary = _read_json(Path(files["eval_summary"]))
    metrics = summary["metrics"]
    reject_counts = summary["reject_counts"]

    assert metrics["ready_count"] == 1
    assert metrics["total_simulations"] == 3
    assert metrics["self_correlation_reject_count"] == 1
    assert metrics["illegal_input_reject_count"] == 1
    assert reject_counts["self_correlation_value_above_strict_cutoff"] == 1
    assert reject_counts["illegal_field"] == 1
    assert reject_counts["exact_active_duplicate"] == 1

    evolution = _read_json(Path(files["evolution_result"]))
    child = evolution["next_generation"]["child_experiment"]
    assert child["ok"] is True
    assert Path(child["experiment"]).is_file()


def test_public_harness_demo_artifact_validator_accepts_root_and_experiment(workdir):
    result = run_public_harness_demo(workdir, run_id="eval-demo-test")

    root_validation = validate_public_harness_artifacts(workdir)
    assert root_validation["ok"] is True
    score = root_validation["metrics"]["harness_score"]
    recomputed_score = root_validation["metrics"]["recomputed_harness_score"]
    assert score == pytest.approx(recomputed_score, abs=1e-6)
    assert score == pytest.approx(0.885417, abs=2e-6)

    experiment_validation = validate_public_harness_artifacts(Path(result["experiment_dir"]))
    assert experiment_validation["ok"] is True
    assert experiment_validation["metrics"]["ready_count"] == 1
    assert experiment_validation["reject_counts"]["illegal_field"] == 1
