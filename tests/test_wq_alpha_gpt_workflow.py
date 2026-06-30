import json
import shutil
import uuid
from pathlib import Path

import pytest

from worldquant_harness.wq_alpha_gpt_workflow import AlphaGPTWorkflowConfig, run_alpha_gpt_dry_run


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / f"alpha_gpt_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_alpha_gpt_dry_run_writes_minimal_no_submit_artifacts(workdir):
    output_dir = workdir / "alpha_gpt_demo"
    summary = run_alpha_gpt_dry_run(
        AlphaGPTWorkflowConfig(
            output_dir=output_dir,
            topic="analyst revision momentum",
            run_id="alpha-gpt-test",
        )
    )

    assert summary["ok"] is True
    assert summary["no_submit"] is True
    assert summary["real_submit_attempted"] is False
    assert summary["candidate_count"] == 4
    assert summary["validation_failed"] == 1
    assert summary["review_decisions"] == {
        "promote_to_review": 2,
        "reject_with_memory": 1,
        "retry_with_mutation": 1,
    }

    files = summary["files"]
    for key in (
        "hypotheses",
        "placeholder_templates",
        "candidate_specs",
        "local_validation",
        "review_queue",
        "reflection_memory",
        "reflection_records",
        "profile_patch",
        "submit_evidence",
        "manifest",
    ):
        assert Path(files[key]).is_file(), key

    hypotheses = _read_jsonl(Path(files["hypotheses"]))
    candidates = _read_jsonl(Path(files["candidate_specs"]))
    validation = _read_jsonl(Path(files["local_validation"]))
    review = _read_jsonl(Path(files["review_queue"]))
    memory = _read_jsonl(Path(files["reflection_memory"]))
    submit_evidence = _read_json(Path(files["submit_evidence"]))
    profile_patch = _read_json(Path(files["profile_patch"]))

    assert len(hypotheses) == 1
    assert {row["hypothesis_id"] for row in candidates} == {hypotheses[0]["hypothesis_id"]}
    assert all(row["placeholder_template"] for row in candidates)
    assert any(row["primary_error_code"] == "illegal_field" for row in validation)
    assert {row["decision"] for row in review} == {
        "promote_to_review",
        "reject_with_memory",
        "retry_with_mutation",
    }
    assert memory[0]["action"] == "block"
    assert submit_evidence["explicit_submit_required"] is True
    assert submit_evidence["real_submit_attempted"] is False
    assert profile_patch["patch_ops"][0]["auto_applied"] is False


def test_alpha_gpt_dry_run_can_skip_negative_fixture(workdir):
    summary = run_alpha_gpt_dry_run(
        AlphaGPTWorkflowConfig(
            output_dir=workdir / "clean_demo",
            topic="price-volume reversal",
            run_id="alpha-gpt-clean",
            include_negative_fixture=False,
        )
    )

    assert summary["candidate_count"] == 3
    assert summary["validation_failed"] == 0
    assert summary["review_decisions"] == {
        "promote_to_review": 2,
        "retry_with_mutation": 1,
    }
