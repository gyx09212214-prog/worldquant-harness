import json
import shutil
import uuid
from pathlib import Path

import pytest

from quantgpt.wq_research_sandbox import (
    ResearchSandboxMineConfig,
    gate_research_experiment,
    mine_research_experiment,
    new_research_experiment,
)


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / f"wq_research_sandbox_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_new_research_experiment_creates_guarded_artifacts(workdir):
    result = new_research_experiment(
        "cashflow options decorrelation",
        root=workdir,
        hypothesis="Cash-flow quality plus options skew may reduce self-correlation.",
        citations=["forum:cashflow-options"],
    )

    exp_dir = Path(result["experiment_dir"])
    record = _read_json(exp_dir / "experiment.yaml")
    decision = _read_json(exp_dir / "decision.yaml")

    assert result["ok"] is True
    assert record["type"] == "wq_research_experiment"
    assert record["decision"] == "hold"
    assert record["hypothesis"]["citations"] == ["forum:cashflow-options"]
    assert "No real submit" in record["submit_guard"]
    assert (exp_dir / "candidate_specs.jsonl").is_file()
    assert (exp_dir / "critic_report.yaml").is_file()
    assert decision["decision"] == "hold"


def test_mine_runs_presubmit_without_submit_and_adds_research_metadata(workdir):
    result = new_research_experiment(
        "cashflow quality retest",
        root=workdir,
        hypothesis="Cash-flow quality with return reversal may produce a low-overlap ready alpha.",
    )
    exp_dir = Path(result["experiment_dir"])
    ready_file = workdir / "ready.jsonl"
    _write_jsonl(
        ready_file,
        [
            {
                "alpha_id": "seed_ready",
                "expression": "rank(ts_rank(cashflow_op / assets, 80) - ts_rank(returns, 20))",
                "tag": "seed-cashflow-assets",
                "sharpe": 1.7,
                "fitness": 1.2,
                "turnover": 0.18,
            }
        ],
    )
    submit_calls = []

    def fake_simulate(candidate, config):
        return {
            "ok": True,
            "alpha_id": f"alpha_{candidate['candidate_rank']}",
            "is_metrics": {
                "sharpe": 1.9,
                "fitness": 1.3,
                "returns": 0.12,
                "turnover": 0.24,
                "checks": [{"name": "SELF_CORRELATION", "result": "PENDING"}],
            },
            "submit_eligible": True,
            "submitted": False,
        }

    def fake_check(ids, config):
        return {
            alpha_id: {
                "status": "UNSUBMITTED",
                "sharpe": 1.9,
                "fitness": 1.3,
                "turnover": 0.24,
                "sc_result": "PASS",
                "sc_value": 0.52,
                "prod_corr_result": "MISSING",
            }
            for alpha_id in ids
        }

    summary = mine_research_experiment(
        ResearchSandboxMineConfig(
            experiment=exp_dir,
            ready_files=(ready_file,),
            target_ready=1,
            max_total_simulations=2,
            cycle_candidate_count=2,
            max_cycles=2,
            max_family_count=6,
            max_field_signature_count=6,
            use_ledger=False,
        ),
        dependencies={
            "list_alphas": lambda config: [],
            "simulate": fake_simulate,
            "check_submissions": fake_check,
            "submit_by_ids": lambda ids, config: submit_calls.append(list(ids)),
        },
    )

    candidates = _read_jsonl(exp_dir / "candidate_specs.jsonl")
    ready = _read_jsonl(exp_dir / "presubmit_run" / "presubmit_ready_sequential.jsonl")

    assert summary["candidate_count"] == len(candidates)
    assert candidates
    assert candidates[0]["research_experiment_id"] == result["experiment_id"]
    assert candidates[0]["candidate_spec_id"].startswith("cand-")
    assert candidates[0]["candidate_meta"]["research_sandbox"]["experiment_id"] == result["experiment_id"]
    assert ready
    assert ready[0]["presubmit_accepted"] is True
    assert submit_calls == []


def test_gate_promotes_after_ready_presubmit(workdir):
    result = new_research_experiment("ready gate", root=workdir)
    exp_dir = Path(result["experiment_dir"])
    presubmit = exp_dir / "presubmit_run"
    _write_json(presubmit / "summary.json", {"ok": True, "presubmit_loop": {"ready_count": 1, "stop_reason": "target_ready_reached"}})
    _write_json(presubmit / "loop_status.json", {"ok": True, "ready_count": 1, "stop_reason": "target_ready_reached"})
    _write_jsonl(
        presubmit / "presubmit_ready_sequential.jsonl",
        [{"alpha_id": "ready1", "expression": "rank(open)", "presubmit_accepted": True, "triage_bucket": "confirmed_ready"}],
    )
    _write_jsonl(presubmit / "presubmit_rejected.jsonl", [])
    _write_jsonl(presubmit / "review_queue.jsonl", [])

    gated = gate_research_experiment(exp_dir)

    assert gated["decision"] == "promote_candidate"
    decision = _read_json(exp_dir / "decision.yaml")
    assert decision["decision"] == "promote_candidate"
    assert "ready candidate" in decision["reasons"][0]


def test_gate_holds_when_presubmit_is_missing(workdir):
    result = new_research_experiment("missing run gate", root=workdir)
    exp_dir = Path(result["experiment_dir"])

    gated = gate_research_experiment(exp_dir)

    assert gated["decision"] == "hold"
    assert "missing presubmit run results" in gated["reasons"]


def test_gate_retires_when_budget_stops_without_ready(workdir):
    result = new_research_experiment("retire gate", root=workdir)
    exp_dir = Path(result["experiment_dir"])
    presubmit = exp_dir / "presubmit_run"
    _write_json(presubmit / "summary.json", {"ok": False, "presubmit_loop": {"ready_count": 0, "stop_reason": "max_total_simulations_reached"}})
    _write_json(presubmit / "loop_status.json", {"ok": False, "ready_count": 0, "stop_reason": "max_total_simulations_reached"})
    _write_jsonl(presubmit / "presubmit_ready_sequential.jsonl", [])
    _write_jsonl(presubmit / "presubmit_rejected.jsonl", [])
    _write_jsonl(presubmit / "review_queue.jsonl", [])

    gated = gate_research_experiment(exp_dir)

    assert gated["decision"] == "retire"
    critic = _read_json(exp_dir / "critic_report.yaml")
    assert critic["blockers"] == ["no ready candidates before stop_reason=max_total_simulations_reached"]
