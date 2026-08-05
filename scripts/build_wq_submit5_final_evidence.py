"""Build a privacy-safe final audit for the five-new-ACTIVE submission goal."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STRICT_SELF_CUTOFF = 0.599999
EXPECTED_IDS = (
    "le36jJkA",
    "1YzVGXKK",
    "58k6r0P5",
    "kqZ3Xrp6",
    "zqR9oApE",
)
BASELINE_AUDIT = Path(
    "reports/wq_agent_runs/current_active_recheck_20260721_priority/"
    "strict_active_cohort.audit.json"
)
FINAL_GET_DIR = Path(
    "reports/wq_agent_runs/submit5_new_after15_20260724_final_audit"
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _manifest(root: Path, paths: list[Path]) -> tuple[list[dict[str, str]], str]:
    rows = [
        {"path": _relative(root, path), "sha256": _sha256(path.read_bytes())}
        for path in sorted(paths)
    ]
    payload = json.dumps(
        rows, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return rows, _sha256(payload)


def build_evidence(
    root: Path = ROOT,
    *,
    expected_ids: tuple[str, ...] = EXPECTED_IDS,
    baseline_audit: Path = BASELINE_AUDIT,
    final_get_dir: Path = FINAL_GET_DIR,
) -> dict[str, Any]:
    expected = set(expected_ids)
    if len(expected) != 5:
        raise RuntimeError("expected exactly five unique goal Alpha IDs")

    baseline_path = root / baseline_audit
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if int(baseline.get("counts", {}).get("strict_active") or 0) != 15:
        raise RuntimeError("strict ACTIVE baseline is not 15")

    get_dir = root / final_get_dir
    get_status_path = get_dir / "platform_get.status.jsonl"
    get_summary_path = get_dir / "platform_get.summary.json"
    alpha_ids_path = get_dir / "alpha_ids.jsonl"
    get_rows = _read_jsonl(get_status_path)
    get_summary = json.loads(get_summary_path.read_text(encoding="utf-8"))
    get_by_id = {str(row.get("alpha_id")): row for row in get_rows}
    if set(get_by_id) != expected or len(get_rows) != 5:
        raise RuntimeError("final platform GET Alpha set drifted")
    if int(get_summary.get("checked") or 0) != 5:
        raise RuntimeError("final platform GET count is not five")

    for alpha_id, row in get_by_id.items():
        if str(row.get("platform_status") or "").upper() != "ACTIVE":
            raise RuntimeError(f"final platform status is not ACTIVE: {alpha_id}")
        self_value = float(row["sc_value"])
        if not self_value < STRICT_SELF_CUTOFF:
            raise RuntimeError(f"final platform SELF is not strict: {alpha_id}")
        if float(row["sharpe"]) < 1.25 or float(row["fitness"]) < 1.0:
            raise RuntimeError(f"final performance gate failed: {alpha_id}")
        if float(row["turnover"]) > 0.7:
            raise RuntimeError(f"final turnover gate failed: {alpha_id}")

    run_root = root / "reports/wq_agent_runs"
    summary_paths = sorted(
        path
        for path in run_root.glob("submit5_new_after15_*live*/summary.json")
        if path.is_file()
    )
    if not summary_paths:
        raise RuntimeError("no goal-scope live run summaries found")
    run_rows: list[dict[str, Any]] = []
    for path in summary_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        run_rows.append(
            {
                "run": path.parent.name,
                "submit_attempts": int(payload.get("submit_attempts") or 0),
                "submitted_successes": int(payload.get("submitted_successes") or 0),
                "simulated": int(payload.get("simulated") or 0),
                "checked": int(payload.get("checked") or 0),
                "summary_sha256": _sha256(path.read_bytes()),
            }
        )
    summary_submit_attempts = sum(row["submit_attempts"] for row in run_rows)
    summary_successes = sum(row["submitted_successes"] for row in run_rows)

    receipt_paths = [
        path.parent / "submit_results.jsonl"
        for path in summary_paths
        if (path.parent / "submit_results.jsonl").is_file()
    ]
    receipt_rows: list[dict[str, Any]] = []
    receipt_source_by_id: dict[str, str] = {}
    for path in receipt_paths:
        for row in _read_jsonl(path):
            alpha_id = str(row.get("alpha_id") or "")
            if not alpha_id:
                raise RuntimeError("transport receipt has no Alpha ID")
            receipt_rows.append(row)
            receipt_source_by_id[alpha_id] = path.parent.name

    receipt_ids = {str(row.get("alpha_id")) for row in receipt_rows}
    transport_post_attempts = sum(
        int(row.get("submit_post_attempts") or 0) for row in receipt_rows
    )
    if receipt_ids != expected or len(receipt_rows) != 5:
        raise RuntimeError("transport receipt Alpha set drifted")
    if transport_post_attempts != 5:
        raise RuntimeError("transport POST count is not exactly five")
    if summary_submit_attempts != transport_post_attempts or summary_successes != 5:
        raise RuntimeError("summary and transport receipt counts disagree")
    for row in receipt_rows:
        alpha_id = str(row["alpha_id"])
        if int(row.get("submit_post_attempts") or 0) != 1:
            raise RuntimeError(f"transport POST attempts are not one: {alpha_id}")
        if int(row.get("submit_post_limit") or 0) != 1:
            raise RuntimeError(f"transport POST limit is not one: {alpha_id}")
        if str(row.get("final_status") or "").upper() != "ACTIVE":
            raise RuntimeError(f"submit receipt is not ACTIVE: {alpha_id}")

    summary_manifest, summary_manifest_sha = _manifest(root, summary_paths)
    receipt_manifest, receipt_manifest_sha = _manifest(root, receipt_paths)
    successful_runs = [row for row in run_rows if row["submit_attempts"] > 0]
    records = []
    for alpha_id in expected_ids:
        get_row = get_by_id[alpha_id]
        receipt = next(
            row for row in receipt_rows if str(row.get("alpha_id")) == alpha_id
        )
        records.append(
            {
                "alpha_id": alpha_id,
                "platform_status": "ACTIVE",
                "sharpe": float(get_row["sharpe"]),
                "fitness": float(get_row["fitness"]),
                "returns": float(get_row["returns"]),
                "turnover": float(get_row["turnover"]),
                "self": float(get_row["sc_value"]),
                "transport_post_attempts": int(receipt["submit_post_attempts"]),
                "transport_post_limit": int(receipt["submit_post_limit"]),
                "source_run": receipt_source_by_id[alpha_id],
            }
        )

    source_paths = [baseline_path, alpha_ids_path, get_status_path, get_summary_path]
    source_manifest, source_manifest_sha = _manifest(root, source_paths)
    return {
        "artifact_type": "wq_submit5_new_active_final_audit",
        "schema_version": 1,
        "generated_from_platform_get_at": max(
            str(row.get("created_at") or "") for row in get_rows
        ),
        "criteria": {
            "platform_status": "ACTIVE",
            "self_correlation_operator": "<",
            "self_correlation_cutoff": STRICT_SELF_CUTOFF,
            "sharpe_min": 1.25,
            "fitness_min": 1.0,
            "turnover_max": 0.7,
            "requires_numeric_self": True,
            "max_transport_posts_per_alpha": 1,
        },
        "counts": {
            "strict_active_baseline": 15,
            "new_active_target": 5,
            "new_active_verified": 5,
            "combined_strict_active": 20,
            "goal_scope_live_runs": len(run_rows),
            "zero_post_live_runs": sum(
                1 for row in run_rows if row["submit_attempts"] == 0
            ),
            "live_runs_with_posts": len(successful_runs),
            "summary_submit_attempts": summary_submit_attempts,
            "transport_submit_post_attempts": transport_post_attempts,
            "platform_get_verified": len(get_rows),
        },
        "records": records,
        "successful_runs": successful_runs,
        "evidence": {
            "source_manifest": source_manifest,
            "source_manifest_sha256": source_manifest_sha,
            "run_summary_manifest_sha256": summary_manifest_sha,
            "run_summary_manifest_records": len(summary_manifest),
            "submit_receipt_manifest_sha256": receipt_manifest_sha,
            "submit_receipt_manifest_records": len(receipt_manifest),
        },
        "privacy": {
            "formulas_omitted": True,
            "field_names_omitted": True,
            "credentials_omitted": True,
            "private_candidate_artifacts_omitted": True,
            "github_publishable": True,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build final submit-five evidence")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    audit = build_evidence()
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(output),
                "sha256": _sha256(output.read_bytes()),
                "new_active_verified": audit["counts"]["new_active_verified"],
                "transport_submit_post_attempts": audit["counts"][
                    "transport_submit_post_attempts"
                ],
                "privacy": audit["privacy"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
