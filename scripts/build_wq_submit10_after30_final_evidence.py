"""Build the privacy-safe final audit for ten new ACTIVE Alphas after baseline 30."""

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
    "JjvekMXO",
    "JjveaMOj",
    "Xg8ambJ1",
    "Grempnoo",
    "j2rVnnpo",
    "88eGlR9z",
    "d5R96dxg",
    "d5R9M5GX",
    "d5R9NrGj",
    "np8m1X5a",
)
BASELINE_DIR = Path(
    "reports/wq_agent_runs/submit10_new_after30_20260727_baseline"
)
FINAL_GET_DIR = Path(
    "reports/wq_agent_runs/submit10_new_after30_20260728_final_reconcile"
)
GOAL_RUN_GLOB = "submit10_new_after30_*live*/summary.json"
BASELINE_COUNT = 30


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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
                "combined_strict_active": audit["counts"][
                    "combined_strict_active"
                ],
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
