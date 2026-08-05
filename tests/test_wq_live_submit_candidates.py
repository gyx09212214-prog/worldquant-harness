import json

from scripts import wq_live_submit_candidates as runner
from scripts.wq_live_submit_candidates import _is_ready_to_submit
from worldquant_harness.artifact_io import read_jsonl
from worldquant_harness.wq_agent_core import InMemoryAgentRepository


def test_ready_to_submit_honors_local_self_corr_cutoff():
    row = {
        "api_check_status": "api_check_readable",
        "sc_result": "PASS",
        "sc_value": 0.8324,
        "prod_corr_result": "MISSING",
    }

    assert not _is_ready_to_submit(row, 0.7)


def test_ready_to_submit_allows_platform_pass_below_cutoff():
    row = {
        "api_check_status": "api_check_readable",
        "sc_result": "PASS",
        "sc_value": "0.52",
        "prod_corr_result": "MISSING",
    }

    assert _is_ready_to_submit(row, 0.7)


def test_ready_to_submit_rejects_value_equal_to_exclusive_cutoff():
    row = {
        "api_check_status": "api_check_readable",
        "sc_result": "PASS",
        "sc_value": 0.5,
        "prod_corr_result": "MISSING",
    }

    assert not _is_ready_to_submit(row, 0.5)


def test_ready_to_submit_pending_requires_explicit_flag():
    row = {"api_check_status": "api_check_pending"}

    assert not _is_ready_to_submit(row, 0.7)
    assert _is_ready_to_submit(row, 0.7, submit_pending=True)


def test_live_runner_exports_canonical_events_and_legacy_artifacts(tmp_path, monkeypatch):
    candidate_file = tmp_path / "candidates.jsonl"
    candidate_file.write_text(
        json.dumps({"expression": "rank(close)", "tag": "integration", "source_family": "unit"}) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    repository = InMemoryAgentRepository()
    gateway = _Gateway()
    client = _Client()

    monkeypatch.setattr(runner, "is_configured", lambda account: True)
    monkeypatch.setattr(runner, "get_client", lambda account: client)
    monkeypatch.setattr(runner, "SqlAgentRepository", lambda: repository)
    monkeypatch.setattr(runner, "WQBrainPlatformGateway", lambda current_client: gateway)

    exit_code = runner.main(
        [
            "--candidate-file",
            str(candidate_file),
            "--output-dir",
            str(output_dir),
            "--target-successes",
            "1",
            "--delay-seconds",
            "0",
        ]
    )

    assert exit_code == 0
    assert client.closed is True
    assert gateway.submit_calls == 1
    assert [row["event_type"] for row in read_jsonl(output_dir / "agent_events.jsonl")][-1] == "submit_succeeded"
    assert read_jsonl(output_dir / "simulation_results.jsonl")[0]["alpha_id"] == "alpha-live"
    assert read_jsonl(output_dir / "check_results.jsonl")[0]["sc_value"] == 0.42
    assert read_jsonl(output_dir / "submit_results.jsonl")[0]["final_status"] == "ACTIVE"
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["submitted_successes"] == 1
    assert summary["event_counts"]["submit_succeeded"] == 1


def test_live_runner_upgrades_legacy_resume_without_resimulation(tmp_path, monkeypatch):
    candidate_file = tmp_path / "candidates.jsonl"
    candidate_file.write_text(json.dumps({"expression": "rank(close)", "tag": "legacy"}) + "\n", encoding="utf-8")
    output_dir = tmp_path / "legacy-output"
    output_dir.mkdir()
    (output_dir / "simulation_results.jsonl").write_text(
        json.dumps(
            {
                "expression": "rank(close)",
                "tag": "legacy",
                "alpha_id": "alpha-legacy",
                "ok": True,
                "submit_eligible": True,
                "simulation_settings_effective": {
                    "region": "USA",
                    "universe": "TOP3000",
                    "delay": 1,
                    "decay": 8,
                    "neutralization": "SUBINDUSTRY",
                    "truncation": 0.08,
                },
            }
        ) + "\n",
        encoding="utf-8",
    )
    (output_dir / "check_results.jsonl").write_text(
        json.dumps(
            {
                "expression": "rank(close)",
                "alpha_id": "alpha-legacy",
                "api_check_status": "api_check_readable",
                "sc_result": "PASS",
                "sc_value": 0.4,
                "prod_corr_result": "MISSING",
            }
        ) + "\n",
        encoding="utf-8",
    )
    repository = InMemoryAgentRepository()
    gateway = _ResumeGateway()
    client = _Client()
    monkeypatch.setattr(runner, "is_configured", lambda account: True)
    monkeypatch.setattr(runner, "get_client", lambda account: client)
    monkeypatch.setattr(runner, "SqlAgentRepository", lambda: repository)
    monkeypatch.setattr(runner, "WQBrainPlatformGateway", lambda current_client: gateway)

    exit_code = runner.main(
        [
            "--candidate-file",
            str(candidate_file),
            "--output-dir",
            str(output_dir),
            "--target-successes",
            "1",
            "--delay-seconds",
            "0",
            "--resume",
        ]
    )

    assert exit_code == 0
    assert gateway.submit_calls == 1
    assert (output_dir / "legacy_resume_import.json").is_file()
    assert any(row["event_type"] == "submit_succeeded" for row in read_jsonl(output_dir / "agent_events.jsonl"))


def test_live_runner_blocks_diversity_before_simulation_or_submit(tmp_path, monkeypatch):
    candidate_file = tmp_path / "candidates.jsonl"
    candidate_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "expression": "rank(signal_one)",
                        "tag": "first",
                        "diversity_family": "family-one",
                        "diversity_core_signature": "same-core",
                    }
                ),
                json.dumps(
                    {
                        "expression": "rank(signal_two)",
                        "tag": "second",
                        "diversity_family": "family-two",
                        "diversity_core_signature": "same-core",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    ledger = tmp_path / "diversity-ledger.json"
    repository = InMemoryAgentRepository()
    gateway = _Gateway()
    client = _Client()

    monkeypatch.setattr(runner, "is_configured", lambda account: True)
    monkeypatch.setattr(runner, "get_client", lambda account: client)
    monkeypatch.setattr(runner, "SqlAgentRepository", lambda: repository)
    monkeypatch.setattr(runner, "WQBrainPlatformGateway", lambda current_client: gateway)

    exit_code = runner.main(
        [
            "--candidate-file",
            str(candidate_file),
            "--output-dir",
            str(output_dir),
            "--target-successes",
            "2",
            "--delay-seconds",
            "0",
            "--diversity-ledger",
            str(ledger),
            "--max-core-signature-count",
            "1",
        ]
    )

    assert exit_code == 1
    assert gateway.submit_calls == 1
    assert len(read_jsonl(output_dir / "simulation_results.jsonl")) == 1
    diversity_rows = read_jsonl(output_dir / "diversity_results.jsonl")
    assert [row["ok"] for row in diversity_rows] == [True, False]
    assert diversity_rows[1]["reasons"] == ["core_signature_capacity_reached"]
    ledger_payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert ledger_payload["accepted_count"] == 1


class _Client:
    def __init__(self):
        self.closed = False

    def authenticate(self, _max_retries=2):
        return True

    def close(self):
        self.closed = True


class _Gateway:
    def __init__(self):
        self.submit_calls = 0

    def simulate(self, expression, settings, *, tag=None, progress_callback=None):
        return {
            "ok": True,
            "alpha_id": "alpha-live",
            "simulation_id": "simulation-live",
            "submit_eligible": True,
            "wq_brain": {"wq_sharpe": 1.8, "wq_fitness": 1.3, "wq_turnover": 0.2},
            "is_metrics": {"checks": []},
        }

    def check(self, alpha_id, *, max_polls, interval):
        return {
            "ok": True,
            "review_checks": {
                "self_correlation": {"result": "PASS", "value": 0.42, "limit": 0.7},
                "prod_correlation": {"result": "MISSING", "value": None, "limit": None},
            },
        }

    def submit(self, alpha_id):
        self.submit_calls += 1
        return {"ok": True, "platform_status": "ACTIVE", "status_code": 200}

    def status(self, alpha_id):
        raise AssertionError("status reconciliation is not expected in a fresh run")


class _ResumeGateway(_Gateway):
    def simulate(self, expression, settings, *, tag=None, progress_callback=None):
        raise AssertionError("legacy resume must not re-simulate")

    def check(self, alpha_id, *, max_polls, interval):
        raise AssertionError("legacy PASS check must be reused")
