from __future__ import annotations

import json
from pathlib import Path

from scripts import build_wq_submit10_final_evidence as builder


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _fixture(tmp_path: Path) -> tuple[Path, tuple[str, ...], Path, Path]:
    ids = tuple(f"alpha{index}" for index in range(1, 11))
    baseline_dir = Path("reports/baseline")
    get_dir = Path("reports/final")
    baseline_rows = [
        {
            "alpha_id": f"baseline{index}",
            "platform_status": "ACTIVE",
            "sc_value": 0.50,
        }
        for index in range(1, 21)
    ]
    _write_jsonl(
        tmp_path / baseline_dir / "platform_get.status.jsonl",
        baseline_rows,
    )
    _write_json(
        tmp_path / baseline_dir / "platform_get.summary.json",
        {"checked": 20, "active": 20},
    )
    _write_jsonl(
        tmp_path / get_dir / "alpha_ids.jsonl",
        [{"alpha_id": alpha_id} for alpha_id in ids],
    )
    _write_jsonl(
        tmp_path / get_dir / "platform_get.status.jsonl",
        [
            {
                "created_at": "2026-07-27T10:00:00",
                "alpha_id": alpha_id,
                "platform_status": "ACTIVE",
                "sharpe": 1.5,
                "fitness": 1.1,
                "returns": 0.07,
                "turnover": 0.15,
                "sc_value": 0.59 - index * 0.005,
            }
            for index, alpha_id in enumerate(ids)
        ],
    )
    _write_json(
        tmp_path / get_dir / "platform_get.summary.json",
        {"checked": 10, "active": 10},
    )

    run_root = tmp_path / "reports/wq_agent_runs"
    for index, alpha_id in enumerate(ids, start=1):
        run = run_root / f"submit10_new_after20_20260727_live_batch{index}"
        _write_json(
            run / "summary.json",
            {
                "submit_attempts": 1,
                "submitted_successes": 1,
                "simulated": 1,
                "checked": 1,
            },
        )
        _write_jsonl(
            run / "submit_results.jsonl",
            [
                {
                    "alpha_id": alpha_id,
                    "submit_post_attempts": 1,
                    "submit_post_limit": 1,
                    "final_status": "ACTIVE",
                }
            ],
        )
    for index in range(2):
        zero_run = (
            run_root
            / f"submit10_new_after20_20260727_live_zero_batch{index}"
        )
        _write_json(
            zero_run / "summary.json",
            {
                "submit_attempts": 0,
                "submitted_successes": 0,
                "simulated": 2,
                "checked": 1,
            },
        )
    return tmp_path, ids, baseline_dir, get_dir


def test_build_evidence_requires_ten_active_strict_self_and_exact_posts(
    tmp_path: Path,
) -> None:
    root, ids, baseline_dir, get_dir = _fixture(tmp_path)
    audit = builder.build_evidence(
        root,
        expected_ids=ids,
        baseline_dir=baseline_dir,
        final_get_dir=get_dir,
    )

    assert audit["counts"]["strict_active_baseline"] == 20
    assert audit["counts"]["new_active_verified"] == 10
    assert audit["counts"]["combined_strict_active"] == 30
    assert audit["counts"]["transport_submit_post_attempts"] == 10
    assert audit["counts"]["goal_scope_live_runs"] == 12
    assert audit["counts"]["zero_post_live_runs"] == 2
    assert all(record["self"] < 0.599999 for record in audit["records"])
    assert all(record["transport_post_attempts"] == 1 for record in audit["records"])


def test_final_audit_is_privacy_safe(tmp_path: Path) -> None:
    root, ids, baseline_dir, get_dir = _fixture(tmp_path)
    audit = builder.build_evidence(
        root,
        expected_ids=ids,
        baseline_dir=baseline_dir,
        final_get_dir=get_dir,
    )

    serialized = json.dumps(audit, sort_keys=True).lower()
    assert '"expression"' not in serialized
    assert '"field_signature"' not in serialized
    assert audit["privacy"]["formulas_omitted"] is True
    assert audit["privacy"]["field_names_omitted"] is True
    assert audit["privacy"]["github_publishable"] is True
