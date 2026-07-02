"""Submit and repair agents for the WQ workflow."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .artifact_io import append_jsonl as _append_jsonl
from .artifact_io import read_jsonl as _read_jsonl
from .artifact_io import utc_now as _now
from .artifact_io import write_json as _write_json
from .artifact_io import write_jsonl as _write_jsonl
from .expression_parser import normalize_expression
from .wq_agent_config import GENERATION_TEMPLATE_FALLBACK, WorkflowPaths, WQAgentWorkflowConfig
from .wq_brain_client import get_client
from .wq_brain_service import run_submit_by_ids
from .wq_policy_repair_planner import build_policy_repair_records
from .wq_workflow_constants import CONFIRMED_READY, NEAR_MISS_REPAIR, SUBMIT_PROBE_NEEDED
from .wq_workflow_context import (
    _community_repair_annotations,
    _submission_policy_for_config,
)
from .wq_workflow_prompts import (
    build_repair_generation_prompt,
    default_model_generate_repairs,
    parse_model_repair_response,
)
from .wq_workflow_scoring import (
    _row_can_submit,
    review_sort_key,
)


class SubmissionAgent:
    """Submit explicitly authorized candidates only."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths, *, dependencies: dict[str, Any] | None = None):
        self.config = config
        self.paths = paths
        self.dependencies = dependencies or {}

    def run(self) -> dict:
        selected = select_submission_candidates(
            _read_jsonl(self.paths.review_queue),
            explicit_ids=self.config.submit_alpha_ids,
            submit_count=self.config.submit_count,
            allow_submit_probe=self.config.allow_submit_probe,
        )
        if not selected:
            summary = {"ok": False, "submitted": 0, "reason": "no authorized candidates selected"}
            _write_jsonl(self.paths.submit_results, [])
            return summary
        if self.config.dry_run:
            rows = [{"alpha_id": alpha_id, "status": "dry_run_not_submitted"} for alpha_id in selected]
            _write_jsonl(self.paths.submit_results, rows)
            return {"ok": True, "dry_run": True, "submitted": 0, "selected": selected}

        submitter = self.dependencies.get("submit_by_ids")
        if submitter:
            result = submitter(selected, self.config)
        else:
            client = get_client(self.config.account)
            try:
                if not client.authenticate():
                    raise RuntimeError("WQ BRAIN authentication failed")
                result = run_submit_by_ids(client, selected)
            finally:
                client.close()

        rows = []
        for alpha_id, entry in (result.get("results") or {}).items():
            rows.append({"created_at": _now(), "alpha_id": alpha_id, **entry})
        _write_jsonl(self.paths.submit_results, rows)
        return {"ok": True, "selected": selected, "result": result, "output": str(self.paths.submit_results)}


class FailureReviewAgent:
    """Persist repairable misses and summarize failure modes."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths, *, dependencies: dict[str, Any] | None = None):
        self.config = config
        self.paths = paths
        self.dependencies = dependencies or {}

    def run(self) -> dict:
        rows = _read_jsonl(self.paths.review_queue) if self.paths.review_queue.is_file() else _read_jsonl(self.paths.simulation_results)
        repairable = [row for row in rows if should_repair(row)]
        repair_rows, model_summary = self._model_repairs(repairable)
        if not repair_rows:
            repair_rows = build_policy_repair_records(
                repairable,
                submission_policy=_submission_policy_for_config(self.config),
                max_repairs_per_row=3,
            )
            if repair_rows:
                model_summary = {
                    "ok": True,
                    "skipped": True,
                    "reason": "deterministic_policy_repair",
                    "generated": sum(len(row.get("candidate_expressions") or []) for row in repair_rows),
                }
        if not repair_rows:
            repair_rows = [build_repair_record(row) for row in repairable]
            repair_rows = [row for row in repair_rows if row]
        repair_rows = [_attach_repair_skill_annotations(record, repairable) for record in repair_rows]
        _write_jsonl(self.paths.repair_queue, repair_rows)
        postmortem = {
            "ok": True,
            "created_at": _now(),
            "total": len(rows),
            "bucket_counts": dict(sorted(Counter(row.get("triage_bucket") or row.get("status") for row in rows).items())),
            "community_skill_tags": dict(sorted(Counter(
                tag for row in repairable for tag in (row.get("community_skill_tags") or [])
            ).items())),
            "repair_strategy_hints": dict(sorted(Counter(
                hint for row in repairable for hint in (row.get("repair_strategy_hints") or [])
            ).items())),
            "repairable": len(repair_rows),
            "model_repairs": model_summary,
            "repair_queue": str(self.paths.repair_queue),
        }
        _write_json(self.paths.postmortem, postmortem)
        return postmortem

    def _model_repairs(self, repairable: list[dict]) -> tuple[list[dict], dict]:
        if not repairable:
            return [], {"ok": True, "skipped": True, "reason": "no repairable rows"}
        if self.config.no_model or self.config.generation_mode == GENERATION_TEMPLATE_FALLBACK:
            return [], {"ok": True, "skipped": True, "reason": "model disabled"}
        prompt = build_repair_generation_prompt(
            self.paths.memory_context_markdown.read_text(encoding="utf-8") if self.paths.memory_context_markdown.is_file() else "",
            repairable,
        )
        _append_jsonl(self.paths.model_repair_requests, {"created_at": _now(), "kind": "repair_generation", "prompt": prompt})
        generator = self.dependencies.get("model_generate_repairs") or default_model_generate_repairs
        raw_records: list[dict] = []
        parsed: list[dict] = []
        last_error = ""
        for attempt in range(max(1, self.config.model_retries + 1)):
            try:
                response = generator(prompt, self.config)
                parsed = parse_model_repair_response(response)
                raw_records.append({"created_at": _now(), "attempt": attempt + 1, "ok": True, "response": response})
                if parsed:
                    break
            except Exception as exc:
                last_error = str(exc)
                raw_records.append({"created_at": _now(), "attempt": attempt + 1, "ok": False, "error": last_error})
        _write_jsonl(self.paths.model_repairs_raw, raw_records)
        return parsed, {
            "ok": bool(parsed),
            "generated": len(parsed),
            "attempts": len(raw_records),
            "error": "" if parsed else last_error,
        }


def select_submission_candidates(
    review_rows: list[dict],
    *,
    explicit_ids: list[str],
    submit_count: int,
    allow_submit_probe: bool,
) -> list[str]:
    if explicit_ids:
        allowed = {str(row.get("alpha_id") or ""): row for row in review_rows}
        return [alpha_id for alpha_id in explicit_ids if alpha_id and _row_can_submit(allowed.get(alpha_id), allow_submit_probe=True)]
    if submit_count <= 0:
        return []
    eligible_buckets = {CONFIRMED_READY}
    if allow_submit_probe:
        eligible_buckets.add(SUBMIT_PROBE_NEEDED)
    selected = [
        str(row.get("alpha_id") or "")
        for row in sorted(review_rows, key=review_sort_key)
        if row.get("triage_bucket") in eligible_buckets and row.get("alpha_id")
    ]
    return selected[:submit_count]


def should_repair(row: dict) -> bool:
    return row.get("triage_bucket") == NEAR_MISS_REPAIR


def build_repair_record(row: dict) -> dict:
    expression = str(row.get("expression") or "")
    return {
        "created_at": _now(),
        "alpha_id": row.get("alpha_id"),
        "source_expression": expression,
        "tag": row.get("tag"),
        "failure_kind": row.get("review_failure_kind") or row.get("api_check_status") or row.get("status"),
        "triage_reason": row.get("triage_reason"),
        "diagnosis": row.get("triage_reason"),
        "repair_objective": "model repair required; no hard-coded expression generated",
        "sharpe": row.get("sharpe"),
        "fitness": row.get("fitness"),
        "turnover": row.get("turnover"),
        "candidate_expressions": [],
        "community_skill_tags": row.get("community_skill_tags") or [],
        "skill_failure_tags": row.get("skill_failure_tags") or [],
        "repair_strategy_hints": row.get("repair_strategy_hints") or [],
        "risk_notes": list(dict.fromkeys((row.get("risk_flags") or []) + (row.get("repair_strategy_hints") or []))),
        "source_row": row,
    }


def _attach_repair_skill_annotations(record: dict, repairable_rows: list[dict]) -> dict:
    source_expression = normalize_expression(str(record.get("source_expression") or ""))
    source_row = record.get("source_row") if isinstance(record.get("source_row"), dict) else {}
    if not source_row and source_expression:
        for row in repairable_rows:
            if normalize_expression(str(row.get("expression") or "")) == source_expression:
                source_row = row
                break
    annotations = _community_repair_annotations(source_row) if source_row else {}
    tags = list(dict.fromkeys((record.get("community_skill_tags") or []) + (annotations.get("community_skill_tags") or [])))
    failure_tags = list(dict.fromkeys((record.get("skill_failure_tags") or []) + (annotations.get("skill_failure_tags") or [])))
    hints = list(dict.fromkeys((record.get("repair_strategy_hints") or []) + (annotations.get("repair_strategy_hints") or [])))
    risk_notes = list(dict.fromkeys((record.get("risk_notes") or []) + hints))
    return {
        **record,
        "community_skill_tags": tags,
        "skill_failure_tags": failure_tags,
        "repair_strategy_hints": hints,
        "risk_notes": risk_notes,
    }
