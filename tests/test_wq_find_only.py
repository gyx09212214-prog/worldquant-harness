import json
import shutil
import uuid
from pathlib import Path

import pytest

from scripts import wq_find_only


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


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _candidate_file(workdir: Path, expressions: list[str]) -> Path:
    path = workdir / "candidates.jsonl"
    path.write_text(
        "\n".join(json.dumps({"expression": expression, "tag": f"tag-{i}"}) for i, expression in enumerate(expressions)),
        encoding="utf-8",
    )
    return path


def _result(expression: str, *, eligible: bool, submitted: bool = False) -> dict:
    return {
        "ok": True,
        "expression": expression,
        "alpha_id": "alpha_" + str(abs(hash(expression)))[:6],
        "submit_eligible": eligible,
        "submitted": submitted,
        "submit_checks": {
            "sharpe": eligible,
            "fitness": eligible,
            "turnover_min": True,
            "turnover_max": True,
        },
        "is_metrics": {"checks": []},
        "wq_brain": {
            "wq_sharpe": 1.7 if eligible else 0.8,
            "wq_fitness": 1.1 if eligible else 0.4,
            "wq_returns": 0.1 if eligible else 0.02,
            "wq_turnover": 0.3,
            "wq_rating": "A" if eligible else "D",
        },
    }


def _result_with_correlation_fail(expression: str) -> dict:
    result = _result(expression, eligible=True)
    result["is_metrics"]["checks"] = [
        {"name": "LOW_SHARPE", "result": "PASS", "limit": 1.25, "value": 1.7},
        {"name": "LOW_FITNESS", "result": "PASS", "limit": 1.0, "value": 1.1},
        {"name": "LOW_TURNOVER", "result": "PASS", "limit": 0.01, "value": 0.3},
        {"name": "HIGH_TURNOVER", "result": "PASS", "limit": 0.7, "value": 0.3},
        {"name": "SELF_CORRELATION", "result": "FAIL", "limit": 0.7, "value": 0.82},
    ]
    return result


def _result_with_pending_correlation(expression: str) -> dict:
    result = _result(expression, eligible=True)
    result["is_metrics"]["checks"] = [
        {"name": "LOW_SHARPE", "result": "PASS", "limit": 1.25, "value": 1.7},
        {"name": "LOW_FITNESS", "result": "PASS", "limit": 1.0, "value": 1.1},
        {"name": "LOW_TURNOVER", "result": "PASS", "limit": 0.01, "value": 0.3},
        {"name": "HIGH_TURNOVER", "result": "PASS", "limit": 0.7, "value": 0.3},
        {"name": "SELF_CORRELATION", "result": "PENDING"},
    ]
    return result


def _result_with_platform_check_fail(expression: str) -> dict:
    result = _result(expression, eligible=True)
    result["is_metrics"]["checks"] = [
        {"name": "LOW_SHARPE", "result": "PASS", "limit": 1.25, "value": 1.7},
        {"name": "LOW_FITNESS", "result": "PASS", "limit": 1.0, "value": 1.1},
        {"name": "LOW_TURNOVER", "result": "PASS", "limit": 0.01, "value": 0.3},
        {"name": "HIGH_TURNOVER", "result": "PASS", "limit": 0.7, "value": 0.3},
        {"name": "CONCENTRATED_WEIGHT", "result": "FAIL", "limit": 0.1, "value": 0.5},
        {"name": "SELF_CORRELATION", "result": "PENDING"},
    ]
    return result


def _patch_runtime(monkeypatch, fake_run):
    monkeypatch.setattr(wq_find_only, "load_dotenv", lambda root: None)
    monkeypatch.setattr(wq_find_only, "is_configured", lambda account: True)
    monkeypatch.setattr(wq_find_only, "get_client", lambda account: FakeClient())
    monkeypatch.setattr(wq_find_only, "_max_similarity", lambda expression, existing: {"overall_similarity": 0.0})
    monkeypatch.setattr(wq_find_only, "run_single_simulation", fake_run)


def test_find_only_collects_multiple_hits_without_submitting(workdir, monkeypatch):
    candidates_file = _candidate_file(workdir, ["rank(close)", "rank(open)", "rank(volume)"])
    output_dir = workdir / "out"
    calls: list[str] = []

    def fake_run(_client, expression, **kwargs):
        calls.append(expression)
        assert kwargs["auto_submit"] is False
        return _result(expression, eligible=expression in {"rank(close)", "rank(volume)"})

    _patch_runtime(monkeypatch, fake_run)

    rc = wq_find_only.main([
        "--candidates",
        str(candidates_file),
        "--output-dir",
        str(output_dir),
        "--target-eligible",
        "2",
        "--max-runs",
        "3",
    ])

    assert rc == 0
    assert calls == ["rank(close)", "rank(open)", "rank(volume)"]

    status = read_json(output_dir / "status.json")
    results = read_jsonl(output_dir / "results.jsonl")
    hits = read_jsonl(output_dir / "hits.jsonl")

    assert status["status"] == "FOUND"
    assert status["reason"] == "target_eligible_reached"
    assert status["counters"]["eligible"] == 2
    assert [entry["status"] for entry in results] == ["eligible", "simulated", "eligible"]
    assert [entry["expression"] for entry in hits] == ["rank(close)", "rank(volume)"]
    assert all(entry["submitted"] is False for entry in hits)


def test_find_only_submission_guard_fails_if_result_reports_submitted(workdir, monkeypatch):
    candidates_file = _candidate_file(workdir, ["rank(close)"])
    output_dir = workdir / "out"

    def fake_run(_client, expression, **kwargs):
        assert kwargs["auto_submit"] is False
        return _result(expression, eligible=True, submitted=True)

    _patch_runtime(monkeypatch, fake_run)

    rc = wq_find_only.main([
        "--candidates",
        str(candidates_file),
        "--output-dir",
        str(output_dir),
        "--target-eligible",
        "1",
        "--max-runs",
        "1",
    ])

    assert rc == 4

    status = read_json(output_dir / "status.json")
    results = read_jsonl(output_dir / "results.jsonl")

    assert status["status"] == "FAILED_SUBMISSION_GUARD"
    assert status["reason"] == "simulation result unexpectedly reported submitted=true"
    assert results[0]["status"] == "failed_submission_guard"
    assert results[0]["submitted"] is True
    assert not (output_dir / "hits.jsonl").exists()


def test_find_only_skips_excluded_similar_candidates(workdir, monkeypatch):
    candidates_file = _candidate_file(workdir, ["rank(close)", "rank(open)"])
    exclude_file = workdir / "exclude.jsonl"
    exclude_file.write_text('{"expression": "rank(close)"}\n', encoding="utf-8")
    output_dir = workdir / "out"
    calls: list[str] = []

    def fake_similarity(expression, existing):
        if existing and expression == "rank(close)":
            return {"overall_similarity": 0.95}
        return {"overall_similarity": 0.0}

    def fake_run(_client, expression, **kwargs):
        calls.append(expression)
        return _result(expression, eligible=True)

    _patch_runtime(monkeypatch, fake_run)
    monkeypatch.setattr(wq_find_only, "_max_similarity", fake_similarity)

    rc = wq_find_only.main([
        "--candidates",
        str(candidates_file),
        "--exclude-expressions",
        str(exclude_file),
        "--output-dir",
        str(output_dir),
        "--target-eligible",
        "1",
        "--max-runs",
        "2",
        "--similarity-threshold",
        "0.75",
    ])

    assert rc == 0
    assert calls == ["rank(open)"]
    assert [entry["status"] for entry in read_jsonl(output_dir / "results.jsonl")] == [
        "skipped_similar",
        "eligible",
    ]


def test_find_only_skips_candidates_similar_to_prior_hit(workdir, monkeypatch):
    candidates_file = _candidate_file(workdir, ["rank(close)", "rank(open)", "rank(volume)"])
    output_dir = workdir / "out"
    calls: list[str] = []

    def fake_similarity(expression, existing):
        if expression == "rank(open)" and existing == ["rank(close)"]:
            return {"overall_similarity": 0.9}
        return {"overall_similarity": 0.0}

    def fake_run(_client, expression, **kwargs):
        calls.append(expression)
        return _result(expression, eligible=True)

    _patch_runtime(monkeypatch, fake_run)
    monkeypatch.setattr(wq_find_only, "_max_similarity", fake_similarity)

    rc = wq_find_only.main([
        "--candidates",
        str(candidates_file),
        "--output-dir",
        str(output_dir),
        "--target-eligible",
        "2",
        "--max-runs",
        "3",
        "--hit-similarity-threshold",
        "0.75",
    ])

    assert rc == 0
    assert calls == ["rank(close)", "rank(volume)"]
    assert [entry["status"] for entry in read_jsonl(output_dir / "results.jsonl")] == [
        "eligible",
        "skipped_similar_to_hit",
        "eligible",
    ]


def test_find_only_does_not_count_failed_correlation_check(workdir, monkeypatch):
    candidates_file = _candidate_file(workdir, ["rank(close)"])
    output_dir = workdir / "out"

    def fake_run(_client, expression, **kwargs):
        return _result_with_correlation_fail(expression)

    _patch_runtime(monkeypatch, fake_run)

    rc = wq_find_only.main([
        "--candidates",
        str(candidates_file),
        "--output-dir",
        str(output_dir),
        "--target-eligible",
        "1",
        "--max-runs",
        "1",
    ])

    assert rc == 1
    status = read_json(output_dir / "status.json")
    results = read_jsonl(output_dir / "results.jsonl")
    assert status["status"] == "NOT_FOUND"
    assert status["counters"]["eligible"] == 0
    assert results[0]["status"] == "failed_correlation_check"


def test_find_only_does_not_count_pending_correlation_check(workdir, monkeypatch):
    candidates_file = _candidate_file(workdir, ["rank(close)"])
    output_dir = workdir / "out"

    def fake_run(_client, expression, **kwargs):
        return _result_with_pending_correlation(expression)

    _patch_runtime(monkeypatch, fake_run)

    rc = wq_find_only.main([
        "--candidates",
        str(candidates_file),
        "--output-dir",
        str(output_dir),
        "--target-eligible",
        "1",
        "--max-runs",
        "1",
    ])

    assert rc == 1
    status = read_json(output_dir / "status.json")
    results = read_jsonl(output_dir / "results.jsonl")
    assert status["status"] == "NOT_FOUND"
    assert status["counters"]["eligible"] == 0
    assert results[0]["status"] == "pending_correlation_check"


def test_find_only_can_run_read_only_api_check_after_run(workdir, monkeypatch):
    candidates_file = _candidate_file(workdir, ["rank(close)"])
    output_dir = workdir / "out"
    api_calls: list[dict] = []

    def fake_run(_client, expression, **kwargs):
        return _result_with_pending_correlation(expression)

    def fake_check_generated_alphas(**kwargs):
        api_calls.append(kwargs)
        assert kwargs["account"] == "primary"
        assert kwargs["include_all"] is False
        assert kwargs["delay_seconds"] == 0
        return {"total": 1, "counts": {"api_check_pending": 1}}

    _patch_runtime(monkeypatch, fake_run)
    monkeypatch.setattr(wq_find_only, "check_generated_alphas", fake_check_generated_alphas)

    rc = wq_find_only.main([
        "--candidates",
        str(candidates_file),
        "--output-dir",
        str(output_dir),
        "--target-eligible",
        "1",
        "--max-runs",
        "1",
        "--api-check-after-run",
    ])

    assert rc == 1
    status = read_json(output_dir / "status.json")
    assert status["status"] == "NOT_FOUND"
    assert status["api_check"]["ok"] is True
    assert status["api_check"]["summary"] == {"total": 1, "counts": {"api_check_pending": 1}}
    assert api_calls[0]["input_paths"] == [output_dir / "results.jsonl"]
    assert api_calls[0]["output_path"] == output_dir / "api_check.jsonl"


def test_find_only_does_not_count_failed_platform_check(workdir, monkeypatch):
    candidates_file = _candidate_file(workdir, ["rank(close)"])
    output_dir = workdir / "out"

    def fake_run(_client, expression, **kwargs):
        return _result_with_platform_check_fail(expression)

    _patch_runtime(monkeypatch, fake_run)

    rc = wq_find_only.main([
        "--candidates",
        str(candidates_file),
        "--output-dir",
        str(output_dir),
        "--target-eligible",
        "1",
        "--max-runs",
        "1",
    ])

    assert rc == 1
    status = read_json(output_dir / "status.json")
    results = read_jsonl(output_dir / "results.jsonl")
    assert status["status"] == "NOT_FOUND"
    assert status["counters"]["eligible"] == 0
    assert results[0]["status"] == "failed_platform_check"
    assert results[0]["failed_platform_checks"][0]["name"] == "CONCENTRATED_WEIGHT"


def test_find_only_can_start_from_later_candidate(workdir, monkeypatch):
    candidates_file = _candidate_file(workdir, ["rank(close)", "rank(open)", "rank(volume)"])
    output_dir = workdir / "out"
    calls: list[str] = []

    def fake_run(_client, expression, **kwargs):
        calls.append(expression)
        return _result(expression, eligible=True)

    _patch_runtime(monkeypatch, fake_run)

    rc = wq_find_only.main([
        "--candidates",
        str(candidates_file),
        "--output-dir",
        str(output_dir),
        "--target-eligible",
        "1",
        "--max-runs",
        "1",
        "--start-index",
        "3",
    ])

    assert rc == 0
    assert calls == ["rank(volume)"]
    status = read_json(output_dir / "status.json")
    assert status["latest_hit"]["expression"] == "rank(volume)"
