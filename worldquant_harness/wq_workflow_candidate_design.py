"""Candidate design agent for the WQ workflow."""

from __future__ import annotations

from typing import Any

from .artifact_io import append_jsonl as _append_jsonl
from .artifact_io import read_jsonl as _read_jsonl
from .artifact_io import utc_now as _now
from .artifact_io import write_jsonl as _write_jsonl
from .llm_service import clean_expression
from .wq_agent_config import (
    GENERATION_EVOLUTIONARY,
    GENERATION_MIXED,
    GENERATION_MIXED_EVOLUTIONARY,
    GENERATION_TEMPLATE_FALLBACK,
    WorkflowPaths,
    WQAgentWorkflowConfig,
)
from .wq_agent_records import candidate_dedupe_key as _candidate_dedupe_key
from .wq_agent_records import candidate_settings_override as _candidate_settings_override
from .wq_agent_records import read_candidate_rows as _read_candidate_rows
from .wq_agent_records import workflow_settings as _settings
from .wq_auto_mining import validate_wq_expression
from .wq_brain_service import submit_threshold_checks
from .wq_efficiency import annotate_candidate_identity
from .wq_evolutionary_generator import generate_evolutionary_candidates
from .wq_similarity import nearest_similarity
from .wq_workflow_active import _fields, _platform_candidate_family
from .wq_workflow_constants import HARD_FAIL, SUCCESS_FAMILY_SEEDS
from .wq_workflow_context import (
    _legal_input_registry_for_config,
)
from .wq_workflow_prompts import (
    build_candidate_generation_prompt,
    default_model_generate_candidates,
    parse_model_candidate_response,
)
from .wq_workflow_scoring import (
    _repair_candidate_block_reason,
    _repair_candidate_sort_key,
    review_sort_key,
)


class ModelCandidateDesignerAgent:
    """Build a candidate pool with model-generated candidates as the primary source."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths, *, dependencies: dict[str, Any] | None = None):
        self.config = config
        self.paths = paths
        self.dependencies = dependencies or {}

    def run(self, *, active_inventory: dict | None = None) -> dict:
        active_rows = (active_inventory or {}).get("active") or []
        model_rows, model_summary = self._model_candidates()
        file_rows = self._file_candidates()
        repair_rows = self._repair_candidates()
        platform_rows = self._platform_candidates()
        fallback_rows = self._fallback_candidates()
        evolutionary_rows, evolutionary_summary = self._evolutionary_candidates(
            active_rows=active_rows,
            file_rows=file_rows,
            platform_rows=platform_rows,
            fallback_rows=fallback_rows,
        )

        rows: list[dict] = []
        if self.config.generation_mode == GENERATION_EVOLUTIONARY:
            rows.extend(evolutionary_rows)
            rows.extend(file_rows)
            rows.extend(repair_rows)
            rows.extend(platform_rows)
            rows.extend(fallback_rows)
        elif self.config.generation_mode == GENERATION_MIXED_EVOLUTIONARY:
            rows.extend(model_rows)
            rows.extend(file_rows)
            rows.extend(evolutionary_rows)
            rows.extend(repair_rows)
            rows.extend(platform_rows)
            rows.extend(fallback_rows)
        elif self.config.generation_mode == GENERATION_TEMPLATE_FALLBACK or self.config.no_model:
            rows.extend(file_rows)
            rows.extend(repair_rows)
            rows.extend(platform_rows)
            rows.extend(fallback_rows)
        elif self.config.generation_mode == GENERATION_MIXED:
            rows.extend(model_rows)
            rows.extend(file_rows)
            rows.extend(repair_rows)
            rows.extend(platform_rows)
            rows.extend(fallback_rows)
        else:
            rows.extend(model_rows)
            rows.extend(file_rows)
            rows.extend(repair_rows)
            rows.extend(platform_rows)
            if len(rows) < self.config.target_candidates:
                rows.extend(fallback_rows[: max(0, self.config.target_candidates - len(rows))])

        unique = []
        seen: set[str] = set()
        legal_registry = _legal_input_registry_for_config(self.config)
        for index, row in enumerate(rows):
            expression = str(row.get("expression") or "").strip()
            if not expression:
                continue
            dedupe_key = _candidate_dedupe_key(row)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            nearest = nearest_similarity(expression, active_rows)
            if nearest and nearest["exact"] and self.config.target_ready <= 0:
                continue
            try:
                validate_wq_expression(expression)
            except Exception as exc:
                row = {**row, "validation_error": str(exc), "triage_bucket": HARD_FAIL}
                continue
            legal_validation = None
            if legal_registry is not None:
                legal_validation = legal_registry.validate_candidate(
                    row,
                    account=self.config.account,
                    region=self.config.region,
                    universe=self.config.universe,
                    delay=self.config.delay,
                    strict=self.config.strict_legal_inputs,
                )
                if not legal_validation.ok:
                    row = {
                        **row,
                        "validation_error": legal_validation.primary_error_code(),
                        "legal_input_validation": legal_validation.to_dict(),
                        "triage_bucket": HARD_FAIL,
                    }
                    if self.config.target_ready <= 0:
                        continue
            candidate_record = {
                "created_at": _now(),
                "candidate_rank": len(unique) + 1,
                "agent_stage": "candidate_design",
                "expression": expression,
                "tag": row.get("tag") or f"agent-candidate-{index + 1}",
                "source_family": row.get("source_family") or row.get("mutation_strategy") or "model_generated",
                "source": row.get("source"),
                "rationale": row.get("rationale"),
                "expected_low_corr_reason": row.get("expected_low_corr_reason"),
                "source_fields": row.get("source_fields") or _fields(expression),
                "mutation_strategy": row.get("mutation_strategy"),
                "parent_alpha_ids": row.get("parent_alpha_ids") or [],
                "risk_flags": row.get("risk_flags") or [],
                "simulation_settings": _candidate_settings_override(row),
                "active_similarity": nearest,
                "candidate_meta": row.get("candidate_meta") or {"model_generation": row.get("model_generation")},
            }
            if legal_validation is not None:
                candidate_record["legal_input_validation"] = legal_validation.to_dict()
            candidate_record = annotate_candidate_identity(candidate_record, _settings(self.config))
            unique.append(candidate_record)
            if len(unique) >= self.config.target_candidates:
                break

        _write_jsonl(self.paths.candidate_pool, unique)
        return {
            "ok": True,
            "candidates": len(unique),
            "model": model_summary,
            "evolutionary": evolutionary_summary,
            "repair_candidates": len(repair_rows),
            "platform_candidates": len(platform_rows),
            "fallback_candidates": len(fallback_rows),
            "output": str(self.paths.candidate_pool),
            "raw_model_output": str(self.paths.model_candidates_raw),
        }

    def _model_candidates(self) -> tuple[list[dict], dict]:
        if self.config.no_model or self.config.generation_mode == GENERATION_TEMPLATE_FALLBACK:
            return [], {"ok": True, "skipped": True, "reason": "model disabled"}
        prompt = build_candidate_generation_prompt(
            self.paths.memory_context_markdown.read_text(encoding="utf-8") if self.paths.memory_context_markdown.is_file() else "",
            target=self.config.model_candidates or max(self.config.target_candidates * 2, self.config.target_candidates),
            examples=self._fallback_candidates(),
        )
        request = {"created_at": _now(), "kind": "candidate_generation", "prompt": prompt}
        _append_jsonl(self.paths.model_design_requests, request)

        generator = self.dependencies.get("model_generate_candidates") or default_model_generate_candidates
        raw_records: list[dict] = []
        parsed: list[dict] = []
        last_error = ""
        for attempt in range(max(1, self.config.model_retries + 1)):
            try:
                response = generator(prompt, self.config)
                candidates = parse_model_candidate_response(response)
                parsed = [{**row, "source": "model_candidate_designer", "model_generation": {"attempt": attempt + 1}} for row in candidates]
                raw_records.append({"created_at": _now(), "attempt": attempt + 1, "ok": True, "response": response})
                if parsed:
                    break
            except Exception as exc:
                last_error = str(exc)
                raw_records.append({"created_at": _now(), "attempt": attempt + 1, "ok": False, "error": last_error})
        _write_jsonl(self.paths.model_candidates_raw, raw_records)
        return parsed, {
            "ok": bool(parsed),
            "generated": len(parsed),
            "attempts": len(raw_records),
            "error": "" if parsed else last_error,
        }

    def _evolutionary_candidates(
        self,
        *,
        active_rows: list[dict],
        file_rows: list[dict],
        platform_rows: list[dict],
        fallback_rows: list[dict],
    ) -> tuple[list[dict], dict]:
        if self.config.generation_mode not in {GENERATION_EVOLUTIONARY, GENERATION_MIXED_EVOLUTIONARY}:
            return [], {"ok": True, "skipped": True, "reason": "generation mode does not request evolutionary"}
        provider = self.dependencies.get("evolutionary_generate_candidates")
        target = self.config.evolutionary_candidates or max(self.config.target_candidates * 2, self.config.target_candidates)
        if provider:
            rows = list(provider(active_rows, file_rows, platform_rows, fallback_rows, self.config))
            return rows, {"ok": True, "generated": len(rows), "provider": "dependency", "target_count": target}
        field_rows = _read_jsonl(self.paths.field_opportunities) if self.paths.field_opportunities.is_file() else []
        repair_rows = _read_jsonl(self.paths.repair_queue) if self.paths.repair_queue.is_file() else []
        return generate_evolutionary_candidates(
            active_rows=active_rows,
            candidate_rows=[*file_rows, *platform_rows, *fallback_rows],
            field_opportunity_rows=field_rows,
            repair_rows=repair_rows,
            target_count=target,
            region=self.config.region,
            universe=self.config.universe,
        )

    def _file_candidates(self) -> list[dict]:
        rows: list[dict] = []
        for path in self.config.candidate_files:
            if not path.is_file():
                continue
            for row in _read_candidate_rows(path):
                rows.append({**row, "source": str(path)})
        return rows

    def _repair_candidates(self) -> list[dict]:
        if not self.paths.repair_queue.is_file():
            return []
        rows: list[dict] = []
        for item in _read_jsonl(self.paths.repair_queue):
            for record in item.get("candidate_records") or []:
                expr = str(record.get("expression") or "").strip()
                if not expr:
                    continue
                candidate = {
                    **record,
                    "expression": expr,
                    "tag": record.get("tag") or f"repair-{item.get('alpha_id') or item.get('tag') or 'candidate'}",
                    "source_family": record.get("source_family") or "near_miss_repair",
                    "source": str(self.paths.repair_queue),
                    "candidate_meta": {
                        **(record.get("candidate_meta") or {}),
                        "repair_source": item,
                    },
                }
                if _repair_candidate_block_reason(candidate):
                    continue
                rows.append(candidate)
            for expr in item.get("candidate_expressions") or item.get("repair_expressions") or []:
                candidate = {
                    "expression": expr,
                    "tag": f"repair-{item.get('alpha_id') or item.get('tag') or 'candidate'}",
                    "source_family": "near_miss_repair",
                    "source": str(self.paths.repair_queue),
                    "candidate_meta": {"repair_source": item},
                }
                if _repair_candidate_block_reason(candidate):
                    continue
                rows.append(candidate)
        rows.sort(key=_repair_candidate_sort_key)
        return rows

    def _platform_candidates(self) -> list[dict]:
        if not self.config.include_platform_candidates:
            return []
        rows = []
        for row in _read_jsonl(self.paths.platform_alphas):
            if str(row.get("status") or "").upper() != "UNSUBMITTED":
                continue
            expression = clean_expression(str(row.get("expression") or ""))
            if not expression:
                continue
            metrics = {
                "sharpe": row.get("sharpe"),
                "fitness": row.get("fitness"),
                "turnover": row.get("turnover"),
            }
            gate = submit_threshold_checks(metrics)
            if not gate["eligible"]:
                continue
            rows.append({
                "expression": expression,
                "tag": f"platform-memory-{row.get('alpha_id') or len(rows) + 1}",
                "source_family": _platform_candidate_family(expression),
                "source": str(self.paths.platform_alphas),
                "rationale": "Recent platform alpha already passed base submit metrics; re-simulate and check against current active inventory.",
                "expected_low_corr_reason": "Selected from non-active platform memory; exact active duplicates are filtered before simulation.",
                "source_fields": _fields(expression),
                "mutation_strategy": "platform_memory_retest",
                "parent_alpha_ids": [row.get("alpha_id")] if row.get("alpha_id") else [],
                "risk_flags": ["requires fresh self-correlation check"],
                "sharpe": metrics["sharpe"],
                "fitness": metrics["fitness"],
                "turnover": metrics["turnover"],
                "candidate_meta": {
                    "platform_alpha_id": row.get("alpha_id"),
                    "platform_status": row.get("status"),
                    "platform_metrics": metrics,
                },
            })
        rows.sort(key=review_sort_key)
        return rows[: max(self.config.target_candidates * 3, self.config.target_candidates)]

    def _fallback_candidates(self) -> list[dict]:
        rows: list[dict] = []
        limit = max(0, self.config.fallback_template_limit)
        for row in SUCCESS_FAMILY_SEEDS[:limit]:
            rows.append({**row, "source": "fallback_legacy_example"})
        if not self.paths.repair_queue.is_file():
            return rows
        for item in _read_jsonl(self.paths.repair_queue):
            for record in item.get("candidate_records") or []:
                expr = str(record.get("expression") or "").strip()
                if not expr:
                    continue
                if len(rows) >= limit:
                    return rows
                candidate = {
                    **record,
                    "expression": expr,
                    "tag": record.get("tag") or f"repair-{item.get('alpha_id') or item.get('tag') or 'candidate'}",
                    "source_family": record.get("source_family") or "near_miss_repair",
                    "source": str(self.paths.repair_queue),
                    "candidate_meta": {
                        **(record.get("candidate_meta") or {}),
                        "repair_source": item,
                    },
                }
                if _repair_candidate_block_reason(candidate):
                    continue
                rows.append(candidate)
            for expr in item.get("candidate_expressions") or item.get("repair_expressions") or []:
                if len(rows) >= limit:
                    return rows
                candidate = {
                    "expression": expr,
                    "tag": f"repair-{item.get('alpha_id') or item.get('tag') or 'candidate'}",
                    "source_family": "near_miss_repair",
                    "source": str(self.paths.repair_queue),
                    "candidate_meta": {"repair_source": item},
                }
                if _repair_candidate_block_reason(candidate):
                    continue
                rows.append(candidate)
        return rows


CandidateDesignerAgent = ModelCandidateDesignerAgent
