import json
import shutil
import uuid
from pathlib import Path

import pytest

from scripts import wq_daily_mining


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_daily_mining_rechecks_pending_runs_find_only_and_reports_ready(workdir, monkeypatch):
    candidates = workdir / "candidates.jsonl"
    _write_jsonl(candidates, [{"expression": "rank(close)", "tag": "seed"}])
    pending = workdir / "pending.jsonl"
    _write_jsonl(
        pending,
        [{
            "alpha_id": "pending_alpha",
            "expression": "rank(open)",
            "api_check_status": "api_check_pending",
            "platform_status": "UNSUBMITTED",
            "source_status": "eligible",
            "sharpe": 1.4,
            "fitness": 1.1,
            "turnover": 0.2,
            "sc_result": "PENDING",
        }],
    )

    checked_outputs: list[Path] = []

    def fake_check_wq_submissions(**kwargs):
        output_path = kwargs["output_path"]
        rows = []
        for input_path in kwargs["input_paths"]:
            for row in _read_jsonl(input_path):
                rows.append({
                    "alpha_id": row["alpha_id"],
                    "expression": row["expression"],
                    "api_check_status": "api_check_readable",
                    "platform_status": "UNSUBMITTED",
                    "source_status": row.get("status") or row.get("source_status"),
                    "sharpe": 1.8,
                    "fitness": 1.25,
                    "turnover": 0.3,
                    "sc_result": "PASS",
                    "sc_value": 0.62,
                    "sc_limit": 0.7,
                    "prod_corr_result": "MISSING",
                })
        _write_jsonl(output_path, rows)
        checked_outputs.append(output_path)
        return {
            "ok": True,
            "total": len(rows),
            "newly_checked": len(rows),
            "counts": {"api_check_readable": len(rows)},
            "output": str(output_path),
        }

    def fake_find_only(argv):
        output_dir = Path(argv[argv.index("--output-dir") + 1])
        rows = [
            {
                "alpha_id": "pending_new_alpha",
                "expression": "rank(open)",
                "status": "pending_correlation_check",
                "submit_eligible": True,
                "submitted": False,
                "sharpe": 1.7,
                "fitness": 1.2,
                "turnover": 0.25,
            },
            {
                "alpha_id": "platform_fail_alpha",
                "expression": "rank(low)",
                "status": "failed_platform_check",
                "submit_eligible": True,
                "submitted": False,
                "sharpe": 2.1,
                "fitness": 1.4,
                "turnover": 0.2,
            },
        ]
        _write_jsonl(output_dir / "results.jsonl", rows)
        _write_jsonl(
            output_dir / "hits.jsonl",
            [{
                "alpha_id": "hit_alpha",
                "expression": "rank(volume)",
                "status": "eligible",
                "submit_eligible": True,
                "submitted": False,
                "sharpe": 1.6,
                "fitness": 1.2,
                "turnover": 0.25,
            }],
        )
        return 0

    monkeypatch.setattr(wq_daily_mining.submission_checks, "check_wq_submissions", fake_check_wq_submissions)
    monkeypatch.setattr(wq_daily_mining.wq_find_only, "main", fake_find_only)

    config = wq_daily_mining.DailyMiningConfig(
        output_dir=workdir / "daily",
        candidate_files=[candidates],
        pending_inputs=[pending],
        target_ready=2,
        target_sim_hits=1,
        max_runs=2,
        cycles=1,
        sync_platform=False,
        use_ledger=False,
    )
    summary = wq_daily_mining.run_daily_mining(config)

    assert summary["status"] == "TARGET_REACHED"
    assert summary["ready_count"] == 2
    assert {row["alpha_id"] for row in summary["ready_records"]} == {"pending_alpha", "pending_new_alpha"}
    assert (config.output_dir / "manifest.json").is_file()
    assert (config.output_dir / "summary.md").read_text(encoding="utf-8").startswith("# WQ Daily Mining Summary")
    assert checked_outputs == [
        config.output_dir / "pending_submission_check.jsonl",
        config.output_dir / "cycle_01" / "candidate_submission_check.jsonl",
    ]


def test_build_daily_candidate_file_applies_submission_policy(workdir):
    candidates = workdir / "candidates.jsonl"
    policy_file = workdir / "submission_policy.json"
    _write_jsonl(
        candidates,
        [
            {
                "expression": "rank(ts_rank(volume, 20))",
                "tag": "direct-volume",
                "source_family": "forum_direct_triage",
            },
            {
                "expression": "rank(ts_rank(cashflow_op / cap, 80) - ts_rank(returns, 20))",
                "tag": "cashflow-overlay",
                "source_family": "forum_direct_triage",
            },
        ],
    )
    policy_file.write_text(json.dumps({
        "gates": {"low_priority_reject_below": 15.0},
        "crowded_domains": [],
        "underexplored_domains": [],
        "theme_policies": {},
        "recipe_policies": {},
    }), encoding="utf-8")
    config = wq_daily_mining.DailyMiningConfig(
        output_dir=workdir / "daily_policy",
        candidate_files=[candidates],
        submission_policy_file=policy_file,
        sync_platform=False,
        use_ledger=False,
    )
    config.output_dir.mkdir(parents=True, exist_ok=True)

    output = wq_daily_mining.build_daily_candidate_file(config)

    kept = _read_jsonl(output)
    skipped = _read_jsonl(config.output_dir / "daily_policy_skipped.jsonl")
    assert [row["tag"] for row in kept] == ["cashflow-overlay"]
    assert kept[0]["forum_policy_action"] == "allow"
    assert skipped[0]["candidate_skip_reason"] == "forum_direct_template_risk"
