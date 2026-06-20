import json
import shutil
import uuid
from pathlib import Path

import pytest

from quantgpt.wq_auto_mining import (
    WQAutoMiner,
    WQAutoMiningConfig,
    build_wq_mutation_prompt,
    classify_submit_result,
    diagnose_wq_result,
    extract_wq_metrics,
    generate_child_expressions,
    write_json,
)


class FakeClient:
    def __init__(self):
        self.submitted: list[str] = []
        self.closed = False

    def authenticate(self):
        return True

    def submit_alpha(self, alpha_id: str):
        self.submitted.append(alpha_id)
        return {"ok": True, "detail": "submitted and ACTIVE", "platform_status": "ACTIVE"}

    def close(self):
        self.closed = True


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _result(expression: str, *, sharpe=0.5, fitness=0.2, returns=0.01, turnover=0.2, eligible=False):
    return {
        "ok": True,
        "expression": expression,
        "alpha_id": "alpha_" + str(abs(hash(expression)))[:6],
        "submit_eligible": eligible,
        "wq_brain": {
            "wq_sharpe": sharpe,
            "wq_fitness": fitness,
            "wq_returns": returns,
            "wq_turnover": turnover,
            "wq_rating": "A" if eligible else "D",
        },
        "backtest_summary": {
            "long_short_sharpe": sharpe,
            "wq_fitness": fitness,
            "turnover": turnover,
        },
    }


def _config(tmp_path: Path, candidates_file: Path, **overrides):
    output_dir = tmp_path / "out"
    values = {
        "candidates_file": candidates_file,
        "output_dir": output_dir,
        "results_file": output_dir / "candidates.jsonl",
        "checkpoint_file": output_dir / "checkpoint.json",
        "status_file": output_dir / "status.json",
        "submitted_file": output_dir / "submitted.jsonl",
        "summary_file": output_dir / "summary.md",
        "stop_file": output_dir / "STOP",
        "max_runs": 5,
        "max_rounds": 5,
        "parents_per_round": 1,
        "children_per_parent": 1,
        "target_submissions": 1,
        "community_context_mode": "off",
    }
    values.update(overrides)
    return WQAutoMiningConfig(**values)


def _read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def test_write_json_retries_transient_replace_permission_error(workdir, monkeypatch):
    target = workdir / "status.json"
    original_replace = type(target).replace
    calls = {"count": 0}

    def flaky_replace(self, other):
        if str(other).endswith("status.json") and calls["count"] == 0:
            calls["count"] += 1
            raise PermissionError("simulated reader lock")
        return original_replace(self, other)

    monkeypatch.setattr(type(target), "replace", flaky_replace)

    write_json(target, {"status": "RUNNING"})

    assert calls["count"] == 1
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "RUNNING"}


def _community_context_dir(workdir: Path) -> Path:
    triage = workdir / "community" / "triage"
    _write_jsonl(
        triage / "triage_records.jsonl",
        [{
            "post_id": "p1",
            "title": "Volume ideas",
            "hypothesis": "Price-volume correlation can capture participation and flow structure.",
            "excerpt": "Use close-volume correlation and volume shocks.",
            "relevance_score": 100,
            "value_type": "candidate_seed",
            "wq_fields": ["close", "volume"],
            "operators": ["rank", "ts_corr"],
            "risk_flags": ["high_turnover"],
            "candidate_expressions": ["rank(ts_corr(close, volume, 10))"],
        }],
    )
    _write_jsonl(
        triage / "community_wq_candidates.jsonl",
        [{
            "expression": "rank(ts_corr(close, volume, 10))",
            "tag": "community-volume",
            "source_post_id": "p1",
            "source_comment_id": None,
            "relevance_score": 100,
        }],
    )
    return triage


def test_extract_metrics_and_submit_classification():
    result = _result("rank(close)", sharpe=1.4, fitness=1.2, returns=0.08, turnover=0.25, eligible=True)
    metrics = extract_wq_metrics(result)
    assert metrics["sharpe"] == 1.4
    assert metrics["fitness"] == 1.2
    assert metrics["submit_eligible"] is True

    active = classify_submit_result({"ok": True, "detail": "done", "platform_status": "ACTIVE"})
    sc_fail = classify_submit_result({"ok": False, "detail": "SC FAIL", "sc_value": 0.9, "sc_limit": 0.7})
    prod_fail = classify_submit_result({
        "ok": False,
        "detail": "PROD_CORRELATION FAIL",
        "failure_kind": "prod_correlation",
        "prod_value": 0.8,
        "prod_limit": 0.7,
    })
    corr_pending = classify_submit_result({
        "ok": False,
        "detail": "pending",
        "failure_kind": "correlation_pending",
        "review_checks": {"pending": ["self_correlation"]},
    })
    timeout = classify_submit_result({"ok": False, "detail": "pending", "platform_status": "TIMEOUT"})

    assert active["status"] == "active"
    assert sc_fail["status"] == "self_corr_failed"
    assert prod_fail["status"] == "prod_corr_failed"
    assert corr_pending["status"] == "corr_pending"
    assert timeout["status"] == "submit_pending"


def test_diagnose_wq_result_prioritizes_platform_and_metric_failures():
    sc = diagnose_wq_result(
        "rank(close)",
        _result("rank(close)", eligible=True),
        {"ok": False, "detail": "SC FAIL", "sc_value": 0.9, "sc_limit": 0.7},
    )
    prod = diagnose_wq_result(
        "rank(close)",
        _result("rank(close)", eligible=True),
        {"ok": False, "failure_kind": "prod_correlation", "detail": "PROD_CORRELATION FAIL"},
    )
    pending = diagnose_wq_result(
        "rank(close)",
        _result("rank(close)", eligible=True),
        {"ok": False, "failure_kind": "correlation_pending", "detail": "pending"},
    )
    high_turnover = diagnose_wq_result(
        "rank(close)",
        _result("rank(close)", sharpe=1.4, fitness=0.7, returns=0.08, turnover=0.9),
    )
    low_sharpe = diagnose_wq_result(
        "rank(close)",
        _result("rank(close)", sharpe=0.2, fitness=0.1, returns=0.03, turnover=0.2),
    )

    assert sc["strategy"] == "avoid_self_correlation"
    assert prod["strategy"] == "avoid_prod_correlation"
    assert pending["strategy"] == "wait_correlation_review"
    assert high_turnover["strategy"] == "reduce_turnover"
    assert low_sharpe["strategy"] == "improve_sharpe"


def test_generate_child_expressions_has_non_llm_fallback(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    children = generate_child_expressions(
        "rank(close)",
        _result("rank(close)", sharpe=1.4, fitness=0.7, returns=0.08, turnover=0.9),
        ["rank(close)"],
        n_children=2,
    )

    assert children
    assert all(child["strategy"] == "reduce_turnover" for child in children)
    assert all(child["expression"] != "rank(close)" for child in children)


def test_mutation_prompt_includes_community_context():
    prompt = build_wq_mutation_prompt(
        "rank(close)",
        {"strategy": "improve_sharpe", "reason": "low sharpe", "details": {"sharpe": 0.2}},
        [],
        community_context="Community-derived reference notes:\n- [100] Price-volume correlation",
    )

    assert "Community-derived reference notes" in prompt
    assert "Use the Community notes as inspiration only" in prompt


def test_auto_miner_appends_community_seeds_after_user_candidates(workdir):
    candidates_file = workdir / "candidates.jsonl"
    candidates_file.write_text('{"expression": "rank(close)", "tag": "seed"}\n', encoding="utf-8")
    context_dir = _community_context_dir(workdir)
    config = _config(
        workdir,
        candidates_file,
        community_context_dir=context_dir,
        community_context_mode="auto",
        community_seed_limit=2,
        target_submissions=0,
        max_runs=1,
        max_rounds=2,
    )

    calls: list[str] = []

    def fake_simulation(_client, expression, **_kwargs):
        calls.append(expression)
        return _result(expression)

    miner = WQAutoMiner(
        config,
        client_factory=lambda account: FakeClient(),
        configured_check=lambda account: True,
        simulation_fn=fake_simulation,
        child_generator=lambda *args, **kwargs: [],
    )

    assert miner.run() == 0

    checkpoint = json.loads(config.checkpoint_file.read_text(encoding="utf-8"))
    assert calls == ["rank(close)"]
    assert checkpoint["queue"][0]["expression"] == "rank(ts_corr(close, volume, 10))"
    assert checkpoint["queue"][0]["strategy"] == "community_seed"
    assert checkpoint["counters"]["community_seeds_added"] == 1
    assert checkpoint["community_context"]["loaded"] is True


def test_auto_miner_passes_retrieved_community_context_to_child_generator(workdir):
    candidates_file = workdir / "candidates.jsonl"
    candidates_file.write_text('{"expression": "rank(volume)", "tag": "seed"}\n', encoding="utf-8")
    context_dir = _community_context_dir(workdir)
    config = _config(
        workdir,
        candidates_file,
        community_context_dir=context_dir,
        community_context_mode="auto",
        community_seed_limit=0,
        target_submissions=0,
        max_runs=1,
        max_rounds=2,
    )
    captured: dict = {}

    def fake_children(expression, result, history, **kwargs):
        captured["community_context"] = kwargs.get("community_context")
        return []

    miner = WQAutoMiner(
        config,
        client_factory=lambda account: FakeClient(),
        configured_check=lambda account: True,
        simulation_fn=lambda _client, expression, **_kwargs: _result(expression, sharpe=0.1, fitness=0.1),
        child_generator=fake_children,
    )

    assert miner.run() == 0
    assert "Community-derived reference notes" in captured["community_context"]
    assert "Price-volume correlation" in captured["community_context"]


def test_auto_miner_community_context_off_keeps_old_seed_queue(workdir):
    candidates_file = workdir / "candidates.jsonl"
    candidates_file.write_text('{"expression": "rank(close)", "tag": "seed"}\n', encoding="utf-8")
    context_dir = _community_context_dir(workdir)
    config = _config(
        workdir,
        candidates_file,
        community_context_dir=context_dir,
        community_context_mode="off",
        community_seed_limit=2,
        target_submissions=0,
        max_runs=1,
    )

    miner = WQAutoMiner(
        config,
        client_factory=lambda account: FakeClient(),
        configured_check=lambda account: True,
        simulation_fn=lambda _client, expression, **_kwargs: _result(expression),
        child_generator=lambda *args, **kwargs: [],
    )

    assert miner.run() == 0
    checkpoint = json.loads(config.checkpoint_file.read_text(encoding="utf-8"))
    assert checkpoint["queue"] == []
    assert checkpoint["community_context"]["loaded"] is False
    assert checkpoint["counters"]["community_seeds_added"] == 0


def test_auto_miner_generates_child_submits_and_checkpoints(workdir):
    candidates_file = workdir / "candidates.jsonl"
    candidates_file.write_text('{"expression": "rank(close)", "tag": "seed"}\n', encoding="utf-8")
    config = _config(workdir, candidates_file)
    fake_client = FakeClient()

    calls: list[str] = []

    def fake_simulation(client, expression, **kwargs):
        calls.append(expression)
        assert kwargs["auto_submit"] is False
        if expression == "rank(close)":
            return _result(expression, sharpe=0.2, fitness=0.1, returns=0.01, turnover=0.2)
        return _result(expression, sharpe=1.6, fitness=1.3, returns=0.09, turnover=0.25, eligible=True)

    def fake_children(expression, result, history, **kwargs):
        assert expression == "rank(close)"
        return [{
            "expression": "rank(ts_delta(close,5))",
            "strategy": "improve_sharpe",
            "diagnosis": {"strategy": "improve_sharpe", "reason": "test"},
        }]

    miner = WQAutoMiner(
        config,
        client_factory=lambda account: fake_client,
        configured_check=lambda account: True,
        simulation_fn=fake_simulation,
        child_generator=fake_children,
        sleep_fn=lambda seconds: None,
    )

    assert miner.run() == 0

    status = json.loads(config.status_file.read_text(encoding="utf-8"))
    checkpoint = json.loads(config.checkpoint_file.read_text(encoding="utf-8"))
    submitted = _read_jsonl(config.submitted_file)
    results = _read_jsonl(config.results_file)

    assert calls == ["rank(close)", "rank(ts_delta(close,5))"]
    assert status["reason"] == "target_submissions_reached"
    assert checkpoint["counters"]["submitted"] == 1
    assert fake_client.submitted == [results[-1]["alpha_id"]]
    assert submitted[0]["status"] == "active"
    assert submitted[0]["expression"] == "rank(ts_delta(close,5))"


def test_auto_miner_records_prod_correlation_failure(workdir):
    candidates_file = workdir / "candidates.jsonl"
    candidates_file.write_text('{"expression": "rank(close)", "tag": "seed"}\n', encoding="utf-8")
    config = _config(workdir, candidates_file, max_runs=1, target_submissions=1)

    class ProdFailClient(FakeClient):
        def submit_alpha(self, alpha_id: str):
            self.submitted.append(alpha_id)
            return {
                "ok": False,
                "failure_kind": "prod_correlation",
                "detail": "PROD_CORRELATION FAIL",
                "prod_value": 0.82,
                "prod_limit": 0.7,
            }

    fake_client = ProdFailClient()

    def fake_simulation(_client, expression, **_kwargs):
        return _result(expression, sharpe=1.6, fitness=1.2, returns=0.08, turnover=0.2, eligible=True)

    miner = WQAutoMiner(
        config,
        client_factory=lambda account: fake_client,
        configured_check=lambda account: True,
        simulation_fn=fake_simulation,
        child_generator=lambda *args, **kwargs: [],
        sleep_fn=lambda seconds: None,
    )

    assert miner.run() == 0

    checkpoint = json.loads(config.checkpoint_file.read_text(encoding="utf-8"))
    results = _read_jsonl(config.results_file)

    assert checkpoint["counters"]["submitted"] == 0
    assert checkpoint["counters"]["prod_corr_failed"] == 1
    assert results[0]["status"] == "prod_corr_failed"
    assert results[0]["diagnosis"]["strategy"] == "avoid_prod_correlation"
    assert not config.submitted_file.exists()


def test_auto_miner_resume_does_not_resimulate_seen_candidate(workdir):
    candidates_file = workdir / "candidates.json"
    candidates_file.write_text(json.dumps(["rank(close)", "rank(volume)"]), encoding="utf-8")
    config = _config(workdir, candidates_file, target_submissions=0, max_runs=1)

    first_calls: list[str] = []

    def fake_simulation(_client, expression, **_kwargs):
        first_calls.append(expression)
        return _result(expression)

    miner = WQAutoMiner(
        config,
        client_factory=lambda account: FakeClient(),
        configured_check=lambda account: True,
        simulation_fn=fake_simulation,
        child_generator=lambda *args, **kwargs: [],
    )
    assert miner.run() == 0
    assert first_calls == ["rank(close)"]

    second_calls: list[str] = []

    def fake_simulation_second(_client, expression, **_kwargs):
        second_calls.append(expression)
        return _result(expression, sharpe=0.8, fitness=0.4)

    config.max_runs = 2
    miner2 = WQAutoMiner(
        config,
        client_factory=lambda account: FakeClient(),
        configured_check=lambda account: True,
        simulation_fn=fake_simulation_second,
        child_generator=lambda *args, **kwargs: [],
    )
    assert miner2.run() == 0
    assert second_calls == ["rank(volume)"]
