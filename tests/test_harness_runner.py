import json
import shutil
import uuid
from pathlib import Path

import pytest

from worldquant_harness.harness_runner import (
    HarnessRunnerConfig,
    harness_memory_maintain,
    harness_status,
    run_public_harness_eval,
)


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / f"harness_runner_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_public_harness_eval_writes_contract_artifacts(workdir):
    result = run_public_harness_eval(
        HarnessRunnerConfig(
            output_root=workdir,
            run_id="contract-eval-test",
            topic="contract eval test",
        )
    )

    assert result["ok"] is True
    assert result["no_submit"] is True

    files = result["files"]
    expected_files = {
        "harness_run",
        "hypotheses",
        "alpha_gpt_candidate_specs",
        "review_decisions",
        "reflection_records",
        "submit_evidence",
        "agent_trace",
        "artifacts",
        "decisions",
        "memory_delta",
        "profile_patch",
        "manifest",
        "eval_cases",
        "eval_result",
    }
    for key in expected_files:
        assert Path(files[key]).is_file(), key

    eval_cases = _read_jsonl(Path(files["eval_cases"]))
    assert {case["case_id"] for case in eval_cases} == {
        "ready_candidate",
        "strict_self_correlation_rejected",
        "illegal_field_rejected",
        "duplicate_active_rejected",
        "no_real_submit",
        "profile_patch_generated_not_applied",
        "alpha_gpt_hypothesis_written",
        "alpha_gpt_candidate_specs_link_hypothesis",
        "alpha_gpt_review_decisions_written",
        "submit_evidence_requires_explicit_submit",
    }
    assert all(case["passed"] is True for case in eval_cases)

    harness_run = _read_json(Path(files["harness_run"]))
    assert harness_run["schema_version"] == 1
    assert harness_run["status"] == "completed"
    assert harness_run["no_submit"] is True
    assert harness_run["metrics"]["passed_count"] == 10
    assert {step["role"] for step in harness_run["steps"]} >= {
        "researcher",
        "verifier",
        "simulator",
        "critic",
        "reflector",
        "submitter",
    }
    assert [step for step in harness_run["steps"] if step["step_id"] == "submit_guard"][0]["status"] == "skipped"

    trace = _read_jsonl(Path(files["agent_trace"]))
    assert [row["event_type"] for row in trace] == [
        "run_created",
        "context_loaded",
        "hypothesis_created",
        "candidates_proposed",
        "candidate_specs_constrained",
        "candidates_validated",
        "presubmit_ran",
        "gate_reviewed",
        "review_decision_recorded",
        "evaluated",
        "reflected",
        "submit_evidence_recorded",
        "profile_candidate_written",
        "memory_delta_written",
        "run_completed",
    ]

    hypotheses = _read_jsonl(Path(files["hypotheses"]))
    candidate_specs = _read_jsonl(Path(files["alpha_gpt_candidate_specs"]))
    review_decisions = _read_jsonl(Path(files["review_decisions"]))
    submit_evidence = _read_json(Path(files["submit_evidence"]))
    assert len(hypotheses) == 1
    assert {row["hypothesis_id"] for row in candidate_specs} == {hypotheses[0]["hypothesis_id"]}
    assert {row["decision"] for row in review_decisions} >= {
        "promote_to_review",
        "retry_with_mutation",
        "reject_with_memory",
    }
    assert submit_evidence["boundary_role"] == "terminal_evidence_source"
    assert submit_evidence["explicit_submit_required"] is True
    assert submit_evidence["real_submit_attempted"] is False

    profile_patch = _read_json(Path(files["profile_patch"]))
    assert profile_patch["no_submit"] is True
    assert profile_patch["patch_ops"]
    assert all(op["auto_applied"] is False for op in profile_patch["patch_ops"])

    status = harness_status(workdir)
    assert status["ok"] is True
    assert status["run_id"] == "contract-eval-test"
    assert status["passed_count"] == 10


def test_memory_maintain_writes_delta_candidates(workdir):
    memory_file = workdir / "memory.jsonl"
    rows = [
        {"failure_kind": "illegal_field", "field_signature": "not_a_real_field", "expression": "rank(not_a_real_field)"},
        {"failure_kind": "illegal_field", "field_signature": "not_a_real_field", "expression": "rank(not_a_real_field)"},
        {"failure_kind": "illegal_field", "field_signature": "not_a_real_field", "expression": "rank(not_a_real_field)"},
    ]
    memory_file.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    result = harness_memory_maintain(
        [str(memory_file)],
        output_dir=workdir / "maintenance",
        absorb_threshold=3,
    )

    assert result["ok"] is True
    assert result["row_count"] == 3
    assert result["absorption_candidates"] == 1
    deltas = _read_jsonl(Path(result["files"]["memory_delta"]))
    assert deltas[0]["action"] == "absorb"
    assert deltas[0]["key"] == "illegal_field|not_a_real_field|"
