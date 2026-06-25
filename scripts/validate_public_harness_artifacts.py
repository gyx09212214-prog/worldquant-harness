"""Validate deterministic public harness demo artifacts.

The validator is intentionally read-only. It accepts either the public demo
output root that contains ``demo_summary.json`` or a single experiment
directory under that root.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_research_harness import _harness_score  # noqa: E402

REQUIRED_FILES = (
    "candidate_specs",
    "presubmit_summary",
    "ready",
    "rejected",
    "critic_report",
    "decision",
    "eval_summary",
    "run_report",
    "evolution_result",
)

EXPECTED_REJECT_COUNTS = {
    "exact_active_duplicate": 1,
    "illegal_field": 1,
    "self_correlation_value_above_strict_cutoff": 1,
}


def validate_public_harness_artifacts(path: str | Path) -> dict[str, Any]:
    """Return validation results for a public harness demo output."""

    target = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    context = _discover_context(target, errors)
    demo_summary = context.get("demo_summary") or {}
    experiment_dir = context.get("experiment_dir")
    output_root = context.get("output_root")
    known_files = _known_files(experiment_dir)
    files = dict(known_files)
    files.update({key: Path(value) for key, value in (demo_summary.get("files") or {}).items() if value})

    if any(path.is_absolute() for path in files.values()):
        warnings.append("manifest_contains_absolute_paths")

    resolved: dict[str, str] = {}
    for key in REQUIRED_FILES:
        candidate = _resolve_file(key, files.get(key), known_files, output_root)
        if candidate is None or not candidate.is_file():
            errors.append(f"missing_file:{key}")
            continue
        resolved[key] = str(candidate)

    eval_summary = _read_json(Path(resolved["eval_summary"])) if "eval_summary" in resolved else {}
    metrics = eval_summary.get("metrics") if isinstance(eval_summary.get("metrics"), dict) else {}
    reject_counts = eval_summary.get("reject_counts") if isinstance(eval_summary.get("reject_counts"), dict) else {}

    if demo_summary:
        if demo_summary.get("ok") is not True:
            errors.append("demo_summary_not_ok")
        if demo_summary.get("real_submit_attempted") is not False:
            errors.append("real_submit_attempted")
        submit_guard = str(demo_summary.get("submit_guard") or "")
        if "No real WQ submit call" not in submit_guard:
            errors.append("missing_submit_guard")

    ready_rows = _read_jsonl(Path(resolved["ready"])) if "ready" in resolved else []
    rejected_rows = _read_jsonl(Path(resolved["rejected"])) if "rejected" in resolved else []
    if len(ready_rows) != int(metrics.get("ready_count") or 0):
        errors.append("ready_count_mismatch")
    if len(rejected_rows) != int(metrics.get("presubmit_rejected_count") or 0):
        errors.append("rejected_count_mismatch")

    if int(metrics.get("real_submit_attempt_count") or 0) != 0:
        errors.append("eval_reports_real_submit_attempts")
    if int(metrics.get("ready_count") or 0) < 1:
        errors.append("no_ready_candidate")
    if int(metrics.get("total_simulations") or 0) < 1:
        errors.append("no_simulations")

    for reason, minimum in EXPECTED_REJECT_COUNTS.items():
        if int(reject_counts.get(reason) or 0) < minimum:
            errors.append(f"missing_expected_reject:{reason}")

    stored_score = _safe_float(eval_summary.get("harness_score"))
    recomputed_score = _harness_score(metrics) if metrics else None
    if stored_score is None:
        errors.append("missing_harness_score")
    elif recomputed_score is None or not math.isclose(stored_score, recomputed_score, abs_tol=1e-6):
        errors.append("harness_score_mismatch")

    result = {
        "ok": not errors,
        "target": str(target),
        "experiment_dir": str(experiment_dir) if experiment_dir else None,
        "errors": errors,
        "warnings": warnings,
        "files": resolved,
        "metrics": {
            "harness_score": stored_score,
            "recomputed_harness_score": recomputed_score,
            "ready_count": metrics.get("ready_count"),
            "presubmit_rejected_count": metrics.get("presubmit_rejected_count"),
            "total_simulations": metrics.get("total_simulations"),
            "real_submit_attempt_count": metrics.get("real_submit_attempt_count"),
        },
        "reject_counts": reject_counts,
    }
    return result


def _discover_context(target: Path, errors: list[str]) -> dict[str, Any]:
    context: dict[str, Any] = {"output_root": None, "experiment_dir": None, "demo_summary": {}}
    if target.is_file() and target.name == "demo_summary.json":
        context["output_root"] = target.parent
        context["demo_summary"] = _read_json(target)
    elif target.is_dir() and (target / "demo_summary.json").is_file():
        context["output_root"] = target
        context["demo_summary"] = _read_json(target / "demo_summary.json")
    elif target.is_dir() and (target / "experiment.yaml").is_file():
        context["experiment_dir"] = target
        context["output_root"] = target.parents[1] if target.parent.name == "experiments" else target.parent
    else:
        errors.append("target_is_not_demo_root_or_experiment_dir")
        return context

    summary_exp = str((context.get("demo_summary") or {}).get("experiment_dir") or "")
    if summary_exp:
        exp_path = Path(summary_exp)
        if not exp_path.is_dir() and context.get("output_root"):
            exp_id = (context.get("demo_summary") or {}).get("experiment_id")
            fallback = Path(context["output_root"]) / "experiments" / str(exp_id)
            exp_path = fallback if fallback.is_dir() else exp_path
        context["experiment_dir"] = exp_path

    if context.get("experiment_dir") is None and context.get("output_root"):
        experiments = sorted((Path(context["output_root"]) / "experiments").glob("exp-*"))
        if experiments:
            context["experiment_dir"] = experiments[-1]
    return context


def _known_files(experiment_dir: Path | None) -> dict[str, Path]:
    if experiment_dir is None:
        return {}
    eval_summary = _find_eval_summary(experiment_dir)
    eval_dir = eval_summary.parent if eval_summary else experiment_dir / "evaluations" / "public-harness-demo"
    return {
        "candidate_specs": experiment_dir / "candidate_specs.jsonl",
        "presubmit_summary": experiment_dir / "presubmit_run" / "summary.json",
        "ready": experiment_dir / "presubmit_run" / "presubmit_ready_sequential.jsonl",
        "rejected": experiment_dir / "presubmit_run" / "presubmit_rejected.jsonl",
        "critic_report": experiment_dir / "critic_report.yaml",
        "decision": experiment_dir / "decision.yaml",
        "eval_summary": eval_summary or eval_dir / "eval_summary.json",
        "run_report": eval_dir / "run_report.md",
        "evolution_result": eval_dir / "evolution_result.json",
    }


def _find_eval_summary(experiment_dir: Path) -> Path | None:
    preferred = experiment_dir / "evaluations" / "public-harness-demo" / "eval_summary.json"
    if preferred.is_file():
        return preferred
    summaries = sorted((experiment_dir / "evaluations").glob("*/eval_summary.json"))
    return summaries[-1] if summaries else None


def _resolve_file(
    key: str,
    candidate: Path | None,
    known_files: dict[str, Path],
    output_root: Path | None,
) -> Path | None:
    if candidate and candidate.is_file():
        return candidate
    if candidate and not candidate.is_absolute() and output_root:
        relative = output_root / candidate
        if relative.is_file():
            return relative
    known = known_files.get(key)
    if known and known.is_file():
        return known
    return candidate or known


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate worldquant-harness public harness demo artifacts")
    parser.add_argument(
        "path",
        nargs="?",
        default="reports/public_harness_demo",
        help="Demo output root, demo_summary.json, or experiment directory.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = validate_public_harness_artifacts(args.path)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
