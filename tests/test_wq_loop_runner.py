import json
import shutil
import uuid
from pathlib import Path

import pytest

from scripts import wq_loop_runner as loop


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FakeClient:
    def __init__(self):
        self.closed = False

    def authenticate(self):
        return True

    def close(self):
        self.closed = True


def make_config(tmp_path: Path, candidates_file: Path, max_runs: int = 10) -> loop.LoopConfig:
    output_dir = tmp_path / "loop_output"
    return loop.LoopConfig(
        candidates_file=candidates_file,
        output_dir=output_dir,
        results_file=output_dir / "results.jsonl",
        checkpoint_file=output_dir / "checkpoint.json",
        status_file=output_dir / "status.json",
        stop_file=output_dir / "STOP",
        max_runs=max_runs,
    )


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def fake_wq_result(expression: str) -> dict:
    is_reversal = "ts_delta" in expression
    return {
        "ok": True,
        "expression": expression,
        "alpha_id": "alpha2" if is_reversal else "alpha1",
        "is_metrics": {},
        "submit_eligible": is_reversal,
        "submitted": False,
        "wq_brain": {
            "wq_sharpe": 1.6 if is_reversal else 0.4,
            "wq_fitness": 1.2 if is_reversal else 0.2,
            "wq_returns": 0.1 if is_reversal else 0.01,
            "wq_turnover": 0.3,
            "wq_rating": "A" if is_reversal else "D",
        },
        "backtest_summary": {
            "long_short_sharpe": 1.6 if is_reversal else 0.4,
            "wq_fitness": 1.2 if is_reversal else 0.2,
        },
    }


def test_load_candidates_supports_json_array_and_jsonl(workdir):
    json_path = workdir / "candidates.json"
    json_path.write_text(json.dumps(["rank(close)", {"expression": "rank(open)", "tag": "open"}]), encoding="utf-8")

    jsonl_path = workdir / "candidates.jsonl"
    jsonl_path.write_text(
        "\n".join([
            '{"expression": "rank(volume)", "tag": "volume"}',
            '"rank(high)"',
            "rank(low)",
        ]),
        encoding="utf-8",
    )

    json_candidates = loop.load_candidates(json_path)
    jsonl_candidates = loop.load_candidates(jsonl_path)

    assert [c.expression for c in json_candidates] == ["rank(close)", "rank(open)"]
    assert json_candidates[1].tag == "open"
    assert [c.expression for c in jsonl_candidates] == ["rank(volume)", "rank(high)", "rank(low)"]


def test_run_loop_writes_results_checkpoint_and_best(workdir, monkeypatch):
    candidates_file = workdir / "candidates.jsonl"
    candidates_file.write_text(
        "\n".join([
            '{"expression": "rank(close)", "tag": "first"}',
            '{"expression": "rank(close)", "tag": "duplicate"}',
            '{"expression": "rank("}',
            '{"expression": "rank(ts_delta(close,5))", "tag": "delta"}',
        ]),
        encoding="utf-8",
    )
    config = make_config(workdir, candidates_file)
    calls: list[str] = []

    monkeypatch.setattr(loop, "is_configured", lambda account: True)
    monkeypatch.setattr(loop, "get_client", lambda account: FakeClient())

    def fake_run_single(client, expression, **kwargs):
        calls.append(expression)
        kwargs["progress_callback"](50, "Simulation running (50%)")
        return fake_wq_result(expression)

    monkeypatch.setattr(loop, "run_single_simulation", fake_run_single)

    assert loop.run_loop(config) == 0

    results = read_jsonl(config.results_file)
    checkpoint = json.loads(config.checkpoint_file.read_text(encoding="utf-8"))
    status = json.loads(config.status_file.read_text(encoding="utf-8"))

    assert calls == ["rank(close)", "rank(ts_delta(close,5))"]
    assert [entry["status"] for entry in results] == [
        "COMPLETED",
        "SKIPPED_DUPLICATE",
        "SKIPPED_INVALID",
        "COMPLETED",
    ]
    assert checkpoint["runs_started"] == 2
    assert checkpoint["completed"] == 2
    assert checkpoint["skipped"] == 2
    assert checkpoint["submitted"] == 0
    assert checkpoint["best"]["alpha_id"] == "alpha2"
    assert status["status"] == "SUCCESS"
    assert status["reason"] == "candidates_exhausted"


def test_run_loop_resumes_from_checkpoint(workdir, monkeypatch):
    candidates_file = workdir / "candidates.json"
    candidates_file.write_text(json.dumps(["rank(close)", "rank(ts_delta(close,5))"]), encoding="utf-8")
    config = make_config(workdir, candidates_file, max_runs=1)
    calls: list[str] = []

    monkeypatch.setattr(loop, "is_configured", lambda account: True)
    monkeypatch.setattr(loop, "get_client", lambda account: FakeClient())

    def fake_run_single(client, expression, **kwargs):
        calls.append(expression)
        return fake_wq_result(expression)

    monkeypatch.setattr(loop, "run_single_simulation", fake_run_single)

    assert loop.run_loop(config) == 0
    first_status = json.loads(config.status_file.read_text(encoding="utf-8"))
    assert first_status["reason"] == "max_runs_reached"

    config.max_runs = 2
    assert loop.run_loop(config) == 0

    results = read_jsonl(config.results_file)
    checkpoint = json.loads(config.checkpoint_file.read_text(encoding="utf-8"))
    assert calls == ["rank(close)", "rank(ts_delta(close,5))"]
    assert [entry["status"] for entry in results] == ["COMPLETED", "COMPLETED"]
    assert checkpoint["runs_started"] == 2


def test_run_loop_stops_at_target_submissions(workdir, monkeypatch):
    candidates_file = workdir / "candidates.json"
    candidates_file.write_text(
        json.dumps(["rank(close)", "rank(ts_delta(close,5))", "rank(volume)"]),
        encoding="utf-8",
    )
    config = make_config(workdir, candidates_file, max_runs=10)
    config.auto_submit = True
    config.target_submissions = 1
    calls: list[str] = []

    monkeypatch.setattr(loop, "is_configured", lambda account: True)
    monkeypatch.setattr(loop, "get_client", lambda account: FakeClient())

    def fake_run_single(client, expression, **kwargs):
        calls.append(expression)
        result = fake_wq_result(expression)
        result["submitted"] = "ts_delta" in expression
        return result

    monkeypatch.setattr(loop, "run_single_simulation", fake_run_single)

    assert loop.run_loop(config) == 0

    status = json.loads(config.status_file.read_text(encoding="utf-8"))
    checkpoint = json.loads(config.checkpoint_file.read_text(encoding="utf-8"))
    assert calls == ["rank(close)", "rank(ts_delta(close,5))"]
    assert status["reason"] == "target_submissions_reached"
    assert checkpoint["submitted"] == 1
