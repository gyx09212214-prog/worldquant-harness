import json

from scripts import submit_wq_existing_until_target as submit_existing


def test_check_only_records_precheck_pass_without_submit(tmp_path, monkeypatch):
    candidate_file = tmp_path / "candidates.jsonl"
    candidate_file.write_text(
        json.dumps({
            "alpha_id": "alpha_pass",
            "expression": "rank(close)",
            "domain": "other",
            "score": 1.0,
            "sharpe": 1.8,
            "fitness": 1.2,
            "turnover": 0.25,
        }) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    submit_calls = []

    class FakeClient:
        def authenticate(self, _max_retries=2):
            return True

        def check_alpha_submission(self, alpha_id, max_polls=3, interval=10):
            return {"ok": True, "status": "UNSUBMITTED", "failure_kind": None, "raw_check": {"is": {"checks": []}}}

        def submit_alpha(self, alpha_id):
            submit_calls.append(alpha_id)
            raise AssertionError("check-only must not submit")

        def close(self):
            pass

    monkeypatch.setattr(submit_existing, "load_dotenv", lambda root: None)
    monkeypatch.setattr(submit_existing, "is_configured", lambda account: True)
    monkeypatch.setattr(submit_existing, "get_client", lambda account: FakeClient())

    code = submit_existing.main([
        "--candidate-file",
        str(candidate_file),
        "--output-dir",
        str(output_dir),
        "--target",
        "1",
        "--max-attempts",
        "1",
        "--check-only",
        "--check-polls",
        "1",
        "--check-interval",
        "0",
    ])

    rows = [
        json.loads(line)
        for line in (output_dir / "submit_existing_results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summary = json.loads((output_dir / "submit_existing_summary.json").read_text(encoding="utf-8"))
    assert code == 0
    assert submit_calls == []
    assert rows[0]["final_status"] == "PRECHECK_PASS"
    assert summary["check_only"] is True
    assert summary["precheck_pass"] == 1


def test_submit_existing_writes_post_submit_review(tmp_path, monkeypatch):
    candidate_file = tmp_path / "candidates.jsonl"
    candidate_file.write_text(
        json.dumps({
            "alpha_id": "alpha_active",
            "expression": "rank(close)",
            "domain": "other",
            "score": 1.0,
            "sharpe": 1.8,
            "fitness": 1.3,
            "returns": 0.1,
            "turnover": 0.12,
        }) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"

    class FakeClient:
        def authenticate(self, _max_retries=2):
            return True

        def check_alpha_submission(self, alpha_id, max_polls=3, interval=10):
            return {
                "ok": True,
                "status": "UNSUBMITTED",
                "failure_kind": None,
                "is": {"checks": [
                    {"name": "SELF_CORRELATION", "result": "PASS", "value": 0.62, "limit": 0.7},
                    {"name": "LOW_SUB_UNIVERSE_SHARPE", "result": "PASS", "value": 1.0, "limit": 0.7},
                ]},
            }

        def submit_alpha(self, alpha_id):
            return {"ok": True, "platform_status": "ACTIVE"}

        def close(self):
            pass

    monkeypatch.setattr(submit_existing, "load_dotenv", lambda root: None)
    monkeypatch.setattr(submit_existing, "is_configured", lambda account: True)
    monkeypatch.setattr(submit_existing, "get_client", lambda account: FakeClient())

    code = submit_existing.main([
        "--candidate-file",
        str(candidate_file),
        "--output-dir",
        str(output_dir),
        "--target",
        "1",
        "--max-attempts",
        "1",
        "--check-before-submit",
        "--check-polls",
        "1",
        "--check-interval",
        "0",
    ])

    summary = json.loads((output_dir / "submit_existing_summary.json").read_text(encoding="utf-8"))
    labels = [
        json.loads(line)
        for line in (output_dir / "post_submit_review" / "alpha_labels.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert code == 0
    assert summary["post_submit_review"]["ok"] is True
    assert labels[0]["alpha_id"] == "alpha_active"
    assert labels[0]["label"] == "strong_seed_active"
