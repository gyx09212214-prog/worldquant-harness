"""Community and memory-context agents for the WQ workflow."""

from __future__ import annotations

import hashlib
from typing import Any

from .artifact_io import read_jsonl as _read_jsonl
from .artifact_io import utc_now as _now
from .artifact_io import write_json as _write_json
from .artifact_io import write_jsonl as _write_jsonl
from .expression_parser import normalize_expression
from .wq_agent_config import WorkflowPaths, WQAgentWorkflowConfig
from .wq_agent_records import workflow_settings as _settings
from .wq_workflow_active import _fields, _jaccard, _operators
from .wq_workflow_constants import NEAR_MISS_REPAIR, ROOT
from .wq_workflow_context import (
    _community_context_for_config,
    _community_skill_route_for_flags,
)
from .wq_workflow_prompts import (
    _summarize_rows,
    render_memory_context_markdown,
)


class CommunityScoutAgent:
    """Extract low-overlap field opportunities from existing community triage."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths):
        self.config = config
        self.paths = paths

    def run(self, *, active_inventory: dict | None = None) -> dict:
        active_fields = set((active_inventory or {}).get("field_counts") or {})
        rows: list[dict] = []
        context = _community_context_for_config(self.config)
        if context:
            for seed in context.seed_candidates(limit=max(10, self.config.target_candidates * 2)):
                fields = _fields(seed.expression)
                rows.append({
                    "created_at": _now(),
                    "source": "community_context",
                    "tag": seed.tag,
                    "source_expression_hash": hashlib.sha256(normalize_expression(seed.expression).encode("utf-8")).hexdigest()[:16],
                    "fields": fields,
                    "operators": _operators(seed.expression),
                    "low_overlap_fields": sorted(set(fields) - active_fields),
                    "field_overlap_with_active": _jaccard(set(fields), active_fields),
                    "experience_category": seed.experience_category,
                    "risk_flags": seed.risk_flags or [],
                    "community_skill_route": _community_skill_route_for_flags(seed.risk_flags or []),
                    "diagnosis": seed.diagnosis,
                })
        _write_jsonl(self.paths.field_opportunities, rows)
        return {
            "ok": True,
            "opportunities": len(rows),
            "community_context_dir": str(context.context_dir) if context else "",
            "community_skills": len(context.skills) if context else 0,
            "output": str(self.paths.field_opportunities),
        }


class MemoryContextBuilder:
    """Build the compact memory packet used by model-driven agents."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths, *, dependencies: dict[str, Any] | None = None):
        self.config = config
        self.paths = paths
        self.dependencies = dependencies or {}

    def run(self, *, active_inventory: dict | None = None) -> dict:
        active_inventory = active_inventory or {"active": []}
        community_context = _community_context_for_config(self.config)
        community_skills = community_context.skill_summary(limit=12) if community_context else []
        context = {
            "created_at": _now(),
            "settings": _settings(self.config),
            "active": _summarize_rows(active_inventory.get("active") or [], limit=20),
            "active_field_counts": active_inventory.get("field_counts") or {},
            "active_operator_counts": active_inventory.get("operator_counts") or {},
            "field_opportunities": _summarize_rows(_read_jsonl(self.paths.field_opportunities), limit=30),
            "community_context_dir": str(community_context.context_dir) if community_context else "",
            "community_skill_count": len(community_context.skills) if community_context else 0,
            "community_skills": community_skills,
            "ledger_failures": _summarize_rows(self._ledger_rows(["self_corr_fail", "prod_corr_fail", "weak", "invalid"], 40), limit=40),
            "ledger_near_miss": _summarize_rows(self._ledger_rows(["pre_submit_pass", "correlation_pending"], 20), limit=20),
            "post_submit_lessons": _summarize_rows(_latest_post_submit_lessons(limit=40), limit=40),
            "current_near_miss": _summarize_rows(
                [row for row in _read_jsonl(self.paths.review_queue) if row.get("triage_bucket") == NEAR_MISS_REPAIR],
                limit=20,
            ),
            "instructions": [
                "Generate new WorldQuant BRAIN FASTEXPR alphas using memory as constraints.",
                "Prefer old successful families as examples, but do not copy exact active expressions.",
                "Use community fields as low-correlation data inspiration.",
                "Treat community skills as conservative gates and repair routes, not direct formula sources.",
                "Transform public templates through field-family/operator-family changes and orthogonal overlays before simulation.",
                "After self-correlation failures, change field or operator family, not only windows.",
            ],
        }
        _write_json(self.paths.memory_context, context)
        markdown = render_memory_context_markdown(context)
        self.paths.memory_context_markdown.write_text(markdown, encoding="utf-8")
        return {
            "ok": True,
            "active": len(context["active"]),
            "ledger_failures": len(context["ledger_failures"]),
            "post_submit_lessons": len(context["post_submit_lessons"]),
            "field_opportunities": len(context["field_opportunities"]),
            "community_skills": len(context["community_skills"]),
            "community_context_dir": context["community_context_dir"],
            "output": str(self.paths.memory_context),
            "markdown": str(self.paths.memory_context_markdown),
        }

    def _ledger_rows(self, statuses: list[str], limit: int) -> list[dict]:
        provider = self.dependencies.get("ledger_rows")
        if provider:
            return list(provider(statuses, limit, self.config))
        if not self.config.use_ledger:
            return []
        try:
            from .wq_alpha_ledger import query_alpha_experiment_rows_sync

            return query_alpha_experiment_rows_sync(statuses=statuses, limit=limit, require_alpha_id=False)
        except Exception:
            return []


def _latest_post_submit_lessons(*, limit: int) -> list[dict]:
    root = ROOT / "reports" / "wq_agent_runs"
    if not root.exists():
        return []
    paths = sorted(
        root.rglob("post_submit_review/experience_delta.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[: max(1, limit)]
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        for row in _read_jsonl(path):
            key = (str(row.get("alpha_id") or ""), str(row.get("field_signature") or ""))
            if key in seen:
                continue
            seen.add(key)
            rows.append({**row, "source_file": str(path)})
            if len(rows) >= limit:
                return rows
    return rows
