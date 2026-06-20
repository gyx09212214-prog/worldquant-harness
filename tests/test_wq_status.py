import json
import shutil
import uuid
from pathlib import Path

import pytest

from scripts import wq_status


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_status_file_overrides_stale_latest_running_pointer(workdir, monkeypatch):
    latest = workdir / "logs" / "wq_find_only_latest.json"
    status_file = workdir / "reports" / "run1" / "status.json"
    write_json(latest, {
        "status": "RUNNING",
        "pid": 123,
        "status_file": str(status_file),
        "output_dir": str(status_file.parent),
        "target_eligible": 10,
    })
    write_json(status_file, {
        "status": "STOPPED",
        "reason": "stop_file_detected",
        "output_dir": str(status_file.parent),
        "counters": {"processed": 16, "completed": 16, "failed": 0, "skipped": 0, "eligible": 0},
        "best": {"alpha_id": "alpha1", "fitness": 1.2, "sharpe": 1.8},
    })
    monkeypatch.setattr(wq_status, "_pid_running", lambda pid: True)

    snapshot = wq_status.build_status_snapshot(kind="find-only", root=workdir)

    assert snapshot["status"] == "STOPPED"
    assert snapshot["running"] is False
    assert snapshot["exit_code"] == 0
    assert "STOPPED pid=123 state=STOPPED" in wq_status.format_status(snapshot)
    assert "eligible=0/10" in wq_status.format_status(snapshot)


def test_status_falls_back_to_latest_when_authoritative_file_is_missing(workdir, monkeypatch):
    latest = workdir / "logs" / "wq_loop_latest.json"
    status_file = workdir / "reports" / "run2" / "status.json"
    write_json(latest, {
        "status": "RUNNING",
        "pid": 456,
        "status_file": str(status_file),
        "output_dir": str(status_file.parent),
        "max_runs": 20,
        "completed": 2,
        "failed": 1,
        "skipped": 0,
        "runs_started": 3,
    })
    monkeypatch.setattr(wq_status, "_pid_running", lambda pid: True)

    snapshot = wq_status.build_status_snapshot(kind="loop", root=workdir)

    assert snapshot["status"] == "RUNNING"
    assert snapshot["authoritative_status_file"] == ""
    assert snapshot["counters"]["processed"] == 3
    assert snapshot["exit_code"] == 0


def test_nonterminal_without_live_process_is_stale(workdir, monkeypatch):
    latest = workdir / "logs" / "wq_find_only_latest.json"
    write_json(latest, {"status": "RUNNING", "pid": 999, "output_dir": str(workdir / "reports" / "run3")})
    monkeypatch.setattr(wq_status, "_pid_running", lambda pid: False)

    snapshot = wq_status.build_status_snapshot(kind="find-only", root=workdir)

    assert snapshot["status"] == "STALE"
    assert snapshot["stale"] is True
    assert snapshot["exit_code"] == 2


def test_json_cli_output_is_stable(workdir, monkeypatch, capsys):
    latest = workdir / "logs" / "wq_loop_latest.json"
    status_file = workdir / "reports" / "run4" / "status.json"
    write_json(latest, {"status": "RUNNING", "pid": 111, "status_file": str(status_file)})
    write_json(status_file, {"status": "SUCCESS", "reason": "candidates_exhausted", "completed": 1})
    monkeypatch.setattr(wq_status, "_pid_running", lambda pid: False)

    rc = wq_status.main(["--kind", "loop", "--root", str(workdir), "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["status"] == "SUCCESS"
    assert out["state"] == "SUCCESS"
    assert out["authoritative_status_file"] == str(status_file)
