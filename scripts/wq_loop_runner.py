"""Sequential, resumable WQ BRAIN loop runner helpers."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.expression_parser import parse_expression
from worldquant_harness.wq_brain_client import get_client, is_configured
from worldquant_harness.wq_brain_service import run_single_simulation
from worldquant_harness.wq_progress import ascii_progress_message as _ascii_progress_message


@dataclass(frozen=True)
class Candidate:
    expression: str
    tag: str | None = None
    source_index: int = 0


@dataclass
class LoopConfig:
    candidates_file: Path
    output_dir: Path
    results_file: Path
    checkpoint_file: Path
    status_file: Path
    stop_file: Path
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    decay: int = 0
    neutralization: str = "SUBINDUSTRY"
    truncation: float = 0.08
    auto_submit: bool = False
    tag: str = "wq-loop"
    max_runs: int = 50
    max_consecutive_failures: int = 5
    target_submissions: int = 0


def load_dotenv(root: Path = ROOT) -> None:
    env_file = root / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if value.strip():
            os.environ.setdefault(key.strip(), value.strip())


def normalize_expression(expression: str) -> str:
    return " ".join(expression.strip().split())


def expression_hash(expression: str) -> str:
    normalized = normalize_expression(expression)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def load_candidates(path: Path) -> list[Candidate]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        return _load_jsonl_candidates(text)

    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("candidate JSON must be an array")
    return [_candidate_from_value(item, i) for i, item in enumerate(data)]


def _load_jsonl_candidates(text: str) -> list[Candidate]:
    candidates: list[Candidate] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            value = line
        candidates.append(_candidate_from_value(value, line_no))
    return candidates


def _candidate_from_value(value: Any, source_index: int) -> Candidate:
    if isinstance(value, str):
        expression = value.strip()
        if not expression:
            raise ValueError(f"empty expression at candidate #{source_index}")
        return Candidate(expression=expression, source_index=source_index)

    if isinstance(value, dict):
        expression = str(value.get("expression", "")).strip()
        if not expression:
            raise ValueError(f"missing expression at candidate #{source_index}")
        tag = value.get("tag")
        return Candidate(expression=expression, tag=str(tag) if tag else None, source_index=source_index)

    raise ValueError(f"unsupported candidate at #{source_index}: {type(value).__name__}")


def validate_expression(expression: str) -> None:
    normalized = normalize_expression(expression)
    if not normalized:
        raise ValueError("empty expression")

    depth = 0
    for char in normalized:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced parentheses")
    if depth != 0:
        raise ValueError("unbalanced parentheses")

    if normalized[-1] in "+-*/^,":
        raise ValueError("expression ends with an operator")

    parse_expression(normalized, mode="wq")


def default_checkpoint() -> dict:
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": None,
        "seen_hashes": [],
        "runs_started": 0,
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "submitted": 0,
        "best": None,
    }


def load_checkpoint(path: Path) -> dict:
    if not path.is_file():
        return default_checkpoint()
    data = json.loads(path.read_text(encoding="utf-8"))
    base = default_checkpoint()
    base.update(data)
    base.setdefault("seen_hashes", [])
    return base


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def update_status(config: LoopConfig, **updates) -> None:
    current: dict[str, Any] = {}
    if config.status_file.is_file():
        try:
            current = json.loads(config.status_file.read_text(encoding="utf-8"))
        except Exception:
            current = {}
    current.update(updates)
    current.update({
        "schema_version": 1,
        "canonical_entrypoint": "scripts/wq_agent_workflow.py presubmit-sequential",
        "legacy_entrypoint": "scripts/run_wq_loop.py",
        "status_reader": "scripts/wq_status.py --kind loop",
        "submit_guard": "legacy loop only submits when auto_submit is true",
        "authoritative_status_file": str(config.status_file),
    })
    current["updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_json(config.status_file, current)


def metric_value(result: dict, key: str) -> float | None:
    metrics = result.get("wq_brain") or {}
    value = metrics.get(f"wq_{key}")
    if value is None and key == "sharpe":
        value = result.get("backtest_summary", {}).get("long_short_sharpe")
    if value is None and key == "fitness":
        value = result.get("backtest_summary", {}).get("wq_fitness")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def best_candidate(existing: dict | None, candidate: dict) -> dict:
    if not existing:
        return candidate
    existing_key = (_safe_score(existing.get("fitness")), _safe_score(existing.get("sharpe")))
    candidate_key = (_safe_score(candidate.get("fitness")), _safe_score(candidate.get("sharpe")))
    return candidate if candidate_key > existing_key else existing


def _safe_score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("-inf")


def result_entry(
    *,
    status: str,
    candidate: Candidate,
    candidate_hash: str,
    config: LoopConfig,
    result: dict | None = None,
    error: str | None = None,
) -> dict:
    entry = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "hash": candidate_hash,
        "source_index": candidate.source_index,
        "expression": candidate.expression,
        "tag": candidate.tag or config.tag,
        "params": {
            "region": config.region,
            "universe": config.universe,
            "delay": config.delay,
            "decay": config.decay,
            "neutralization": config.neutralization,
            "truncation": config.truncation,
            "auto_submit": config.auto_submit,
        },
    }
    if result is not None:
        entry["result"] = result
        entry["alpha_id"] = result.get("alpha_id")
        entry["sharpe"] = metric_value(result, "sharpe")
        entry["fitness"] = metric_value(result, "fitness")
        entry["returns"] = metric_value(result, "returns")
        entry["turnover"] = metric_value(result, "turnover")
        entry["submit_eligible"] = result.get("submit_eligible")
        entry["submitted"] = result.get("submitted")
    if error:
        entry["error"] = error
    return entry


def run_loop(config: LoopConfig) -> int:
    load_dotenv()
    config.output_dir.mkdir(parents=True, exist_ok=True)

    candidates = load_candidates(config.candidates_file)
    checkpoint = load_checkpoint(config.checkpoint_file)
    seen_hashes = set(checkpoint.get("seen_hashes", []))
    resume_hashes = set(seen_hashes)
    session_hashes: set[str] = set()
    counters = {
        "runs_started": int(checkpoint.get("runs_started", 0)),
        "completed": int(checkpoint.get("completed", 0)),
        "failed": int(checkpoint.get("failed", 0)),
        "skipped": int(checkpoint.get("skipped", 0)),
        "submitted": int(checkpoint.get("submitted", 0)),
    }
    best = checkpoint.get("best")

    update_status(
        config,
        kind="wq_loop",
        status="STARTING",
        candidates_file=str(config.candidates_file),
        output_dir=str(config.output_dir),
        results_file=str(config.results_file),
        checkpoint_file=str(config.checkpoint_file),
        total_candidates=len(candidates),
        max_runs=config.max_runs,
        target_submissions=config.target_submissions,
        **counters,
        best=best,
    )

    if config.max_runs <= 0:
        _finish(config, checkpoint, seen_hashes, counters, best, "SUCCESS", "max_runs_zero")
        return 0

    if not is_configured("primary"):
        _finish(config, checkpoint, seen_hashes, counters, best, "FAILED", "WQ credentials are not configured")
        return 2

    client = get_client("primary")
    consecutive_failures = 0
    try:
        update_status(config, status="AUTHENTICATING", message="Authenticating to WQ BRAIN")
        if not client.authenticate():
            _finish(config, checkpoint, seen_hashes, counters, best, "FAILED", "WQ authentication failed")
            return 3

        for position, candidate in enumerate(candidates, start=1):
            if config.stop_file.is_file():
                _finish(config, checkpoint, seen_hashes, counters, best, "STOPPED", "stop_file_detected")
                return 0
            if config.target_submissions > 0 and counters["submitted"] >= config.target_submissions:
                _finish(config, checkpoint, seen_hashes, counters, best, "SUCCESS", "target_submissions_reached")
                return 0
            if counters["runs_started"] >= config.max_runs:
                _finish(config, checkpoint, seen_hashes, counters, best, "SUCCESS", "max_runs_reached")
                return 0

            candidate_hash = expression_hash(candidate.expression)
            if candidate_hash in resume_hashes:
                continue
            if candidate_hash in session_hashes:
                counters["skipped"] += 1
                entry = result_entry(
                    status="SKIPPED_DUPLICATE",
                    candidate=candidate,
                    candidate_hash=candidate_hash,
                    config=config,
                    error="expression already processed",
                )
                append_jsonl(config.results_file, entry)
                _save_progress(config, checkpoint, seen_hashes, counters, best)
                continue

            try:
                validate_expression(candidate.expression)
            except Exception as exc:
                seen_hashes.add(candidate_hash)
                session_hashes.add(candidate_hash)
                counters["skipped"] += 1
                entry = result_entry(
                    status="SKIPPED_INVALID",
                    candidate=candidate,
                    candidate_hash=candidate_hash,
                    config=config,
                    error=str(exc),
                )
                append_jsonl(config.results_file, entry)
                _save_progress(config, checkpoint, seen_hashes, counters, best)
                continue

            counters["runs_started"] += 1
            update_status(
                config,
                status="RUNNING",
                current_position=position,
                current_hash=candidate_hash,
                current_expression=candidate.expression,
                current_progress=0,
                message="Submitting simulation",
                **counters,
                best=best,
            )

            def on_progress(progress: int, message: str) -> None:
                update_status(
                    config,
                    status="RUNNING",
                    current_position=position,
                    current_hash=candidate_hash,
                    current_expression=candidate.expression,
                    current_progress=progress,
                    message=_ascii_progress_message(progress, message),
                    **counters,
                    best=best,
                )

            result = run_single_simulation(
                client,
                candidate.expression,
                region=config.region,
                universe=config.universe,
                delay=config.delay,
                decay=config.decay,
                neutralization=config.neutralization,
                truncation=config.truncation,
                auto_submit=config.auto_submit,
                tag=candidate.tag or config.tag,
                progress_callback=on_progress,
            )
            seen_hashes.add(candidate_hash)
            session_hashes.add(candidate_hash)

            if result.get("ok"):
                counters["completed"] += 1
                consecutive_failures = 0
                entry = result_entry(
                    status="COMPLETED",
                    candidate=candidate,
                    candidate_hash=candidate_hash,
                    config=config,
                    result=result,
                )
                best = best_candidate(best, _best_summary(entry))
                if result.get("submitted"):
                    counters["submitted"] += 1
            else:
                counters["failed"] += 1
                consecutive_failures += 1
                entry = result_entry(
                    status="FAILED",
                    candidate=candidate,
                    candidate_hash=candidate_hash,
                    config=config,
                    result=result,
                    error=result.get("error", "simulation failed"),
                )
            append_jsonl(config.results_file, entry)
            _save_progress(config, checkpoint, seen_hashes, counters, best)

            if config.target_submissions > 0 and counters["submitted"] >= config.target_submissions:
                _finish(config, checkpoint, seen_hashes, counters, best, "SUCCESS", "target_submissions_reached")
                return 0

            if consecutive_failures >= config.max_consecutive_failures:
                reason = f"max_consecutive_failures_reached ({consecutive_failures})"
                _finish(config, checkpoint, seen_hashes, counters, best, "FAILED", reason)
                return 4

            time.sleep(1)

        _finish(config, checkpoint, seen_hashes, counters, best, "SUCCESS", "candidates_exhausted")
        return 0
    finally:
        client.close()


def _best_summary(entry: dict) -> dict:
    return {
        "hash": entry["hash"],
        "expression": entry["expression"],
        "alpha_id": entry.get("alpha_id"),
        "sharpe": entry.get("sharpe"),
        "fitness": entry.get("fitness"),
        "returns": entry.get("returns"),
        "turnover": entry.get("turnover"),
        "submitted": entry.get("submitted"),
        "created_at": entry["created_at"],
    }


def _save_progress(
    config: LoopConfig,
    checkpoint: dict,
    seen_hashes: set[str],
    counters: dict,
    best: dict | None,
) -> None:
    checkpoint.update(counters)
    checkpoint["seen_hashes"] = sorted(seen_hashes)
    checkpoint["best"] = best
    checkpoint["updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_json(config.checkpoint_file, checkpoint)
    update_status(config, **counters, best=best)


def _finish(
    config: LoopConfig,
    checkpoint: dict,
    seen_hashes: set[str],
    counters: dict,
    best: dict | None,
    status: str,
    reason: str,
) -> None:
    _save_progress(config, checkpoint, seen_hashes, counters, best)
    update_status(
        config,
        status=status,
        reason=reason,
        ended_at=datetime.now().isoformat(timespec="seconds"),
        **counters,
        best=best,
    )


def config_to_dict(config: LoopConfig) -> dict:
    data = asdict(config)
    return {key: str(value) if isinstance(value, Path) else value for key, value in data.items()}
