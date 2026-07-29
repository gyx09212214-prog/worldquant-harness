"""Prepare and audit the final ten-new-ACTIVE evidence after baseline 50."""

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

from scripts import build_wq_submit10_final_evidence as base

EXPECTED_IDS = (
    "d5R3GVvX",
    "omgJx91b",
    "omgGK6mE",
    "1YzKdO76",
    "E5ezeb20",
    "d5RzaQWx",
    "d5RzvGbx",
    "LLdzvl7n",
    "88eJMdwX",
    "YPgzRvp6",
)
BASELINE_DIR = Path(
    "reports/wq_agent_runs/submit10_new_after50_20260729_baseline"
)
FINAL_GET_DIR = Path(
    "reports/wq_agent_runs/submit10_new_after50_20260729_final_get"
)
GOAL_RUN_GLOB = "submit10_new_after50_20260729_*live*/summary.json"
BASELINE_COUNT = 50
RECEIPTS = (
    (
        "submit10_new_after50_20260729_live_batch272_lower_pair_gates",
        "5f8845ae27edd9573f98bfd01dc598819e491c305790f947381212653ed63916",
    ),
    (
        "submit10_new_after50_20260729_live_batch273_strict_pair_tails",
        "30f5cdebcef5c27bfb882b4d2b5803dd097a0dbe47a476f9c5853fc6ea5feba4",
    ),
    (
        "submit10_new_after50_20260729_live_batch288_targeted_disjoint_bands",
        "b883a45e8b5aac4e1f726f0358f662c85ee8afb2ed408d57e65e3a191bd589fb",
    ),
    (
        "submit10_new_after50_20260729_live_batch289_remaining_disjoint_bands",
        "45274df99aa45eb8ecb18a925cb43e0a5eeae33c3b5283cdd5d54888ca9cc45f",
    ),
    (
        "submit10_new_after50_20260729_live_batch290_final_disjoint_bands",
        "8e2be662fa626a31bc95e388f48c4524a50508d860cce7a6b3c5da5f1b992837",
    ),
    (
        "submit10_new_after50_20260729_live_batch294_active_state_gate_matrix",
        "05406f77a031b5b6fe542d754f3a464864e0174f5b8f2a77ea6d724fdd8a8d12",
    ),
    (
        "submit10_new_after50_20260729_live_batch298_measured_pair_retightening",
        "1b5a55fd005e7d24aa183e440c3eb94a3d52756adc2f4e4323f9a6b91546977e",
    ),
    (
        "submit10_new_after50_20260729_live_batch303b_narrow_state_subbands_corrected",
        "7d942a87283a09deccdd63c0ffca3b0633b1b64790c02fad4284e2fd44abd8fb",
    ),
    (
        "submit10_new_after50_20260729_live_batch305_microtrim_self_edge",
        "9e37bbef0870f4a156e7475d6177d23a3d50ad3bca0aafe27abfe3ecdd1f07fd",
    ),
    (
        "submit10_new_after50_20260729_live_batch306_final_quarter_subbands",
        "eba7403b2fca0e86029f857e3c377e266c78a60a2901285f43bb1be546532b83",
    ),
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def prepare_final_get_input(root: Path = ROOT) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    receipt_manifest: list[dict[str, str]] = []
    run_root = root / "reports/wq_agent_runs"
    for expected_id, (run_name, expected_sha256) in zip(
        EXPECTED_IDS, RECEIPTS, strict=True
    ):
        path = run_root / run_name / "submit_results.jsonl"
        actual_sha256 = _sha256(path.read_bytes())
        if actual_sha256 != expected_sha256:
            raise RuntimeError(f"final receipt hash mismatch: {run_name}")
        receipt_rows = _read_jsonl(path)
        if len(receipt_rows) != 1:
            raise RuntimeError(f"final receipt cardinality drifted: {run_name}")
        row = receipt_rows[0]
        if (
            row.get("alpha_id") != expected_id
            or str(
                row.get("final_status")
                or row.get("platform_status")
                or ""
            ).upper()
            != "ACTIVE"
            or int(row.get("submit_post_attempts") or 0) != 1
            or int(row.get("submit_post_limit") or 0) != 1
        ):
            raise RuntimeError(f"final receipt contract failed: {run_name}")
        rows.append(row)
        receipt_manifest.append(
            {
                "run_sha256": _sha256(run_name.encode("utf-8")),
                "receipt_sha256": actual_sha256,
            }
        )
    if len({str(row["alpha_id"]) for row in rows}) != 10:
        raise RuntimeError("final receipt IDs are not ten unique values")

    output_dir = root / FINAL_GET_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    combined_path = output_dir / "combined_submit_results.private.jsonl"
    ids_path = output_dir / "alpha_ids.jsonl"
    combined_path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    ids_path.write_text(
        "".join(
            json.dumps({"alpha_id": alpha_id}, separators=(",", ":")) + "\n"
            for alpha_id in EXPECTED_IDS
        ),
        encoding="utf-8",
    )
    return {
        "ok": True,
        "records": len(rows),
        "unique_ids": len({str(row["alpha_id"]) for row in rows}),
        "transport_submit_post_attempts": sum(
            int(row["submit_post_attempts"]) for row in rows
        ),
        "combined_input": str(combined_path),
        "combined_input_sha256": _sha256(combined_path.read_bytes()),
        "alpha_ids": str(ids_path),
        "alpha_ids_sha256": _sha256(ids_path.read_bytes()),
        "receipt_manifest": receipt_manifest,
        "privacy": {
            "combined_input_is_private": True,
            "public_receipt_formulas_omitted": True,
            "public_receipt_field_names_omitted": True,
        },
    }


def build_evidence(root: Path = ROOT) -> dict[str, Any]:
    return base.build_evidence(
        root,
        expected_ids=EXPECTED_IDS,
        baseline_dir=BASELINE_DIR,
        final_get_dir=FINAL_GET_DIR,
        goal_run_glob=GOAL_RUN_GLOB,
        baseline_count=BASELINE_COUNT,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-input", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if not args.prepare_input and not args.output:
        parser.error("one of --prepare-input or --output is required")

    result: dict[str, Any] = {"ok": True}
    if args.prepare_input:
        result["prepared"] = prepare_final_get_input()
    if args.output:
        audit = build_evidence()
        output = Path(args.output)
        if not output.is_absolute():
            output = ROOT / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(audit, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result["audit"] = {
            "output": str(output),
            "sha256": _sha256(output.read_bytes()),
            "new_active_verified": audit["counts"]["new_active_verified"],
            "combined_strict_active": audit["counts"][
                "combined_strict_active"
            ],
            "transport_submit_post_attempts": audit["counts"][
                "transport_submit_post_attempts"
            ],
            "privacy": audit["privacy"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
