"""Prompt rendering and model JSON parsing for WQ workflow stages."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .artifact_io import utc_now as _now
from .llm_service import clean_expression
from .wq_agent_config import WQAgentWorkflowConfig
from .wq_workflow_active import _fields, _operators


def render_memory_context_markdown(context: dict) -> str:
    lines = [
        "# WQ Agent Memory Context",
        "",
        "## Active Alpha Inventory",
    ]
    for row in context.get("active") or []:
        lines.append(
            f"- {row.get('alpha_id') or 'unknown'}: sharpe={row.get('sharpe')} "
            f"fitness={row.get('fitness')} turnover={row.get('turnover')} expr={_short_expr(row.get('expression'))}"
        )
    lines.extend(["", "## Ledger Failure Memory"])
    for row in context.get("ledger_failures") or []:
        lines.append(
            f"- status={row.get('status') or row.get('source_status')} failure={row.get('failure_kind')} "
            f"sc={row.get('sc_value')} expr={_short_expr(row.get('expression'))}"
        )
    lines.extend(["", "## Near Miss / Pending Candidates"])
    for row in (context.get("ledger_near_miss") or []) + (context.get("current_near_miss") or []):
        lines.append(
            f"- {row.get('alpha_id') or 'candidate'}: status={row.get('status') or row.get('triage_bucket')} "
            f"sharpe={row.get('sharpe')} fitness={row.get('fitness')} turnover={row.get('turnover')} "
            f"expr={_short_expr(row.get('expression') or row.get('source_expression'))}"
        )
    lines.extend(["", "## Post Submit Lessons"])
    for row in context.get("post_submit_lessons") or []:
        lines.append(
            f"- {row.get('alpha_id') or 'candidate'}: label={row.get('label')} action={row.get('next_action')} "
            f"lesson={row.get('lesson')} expr={_short_expr(row.get('expression'))}"
        )
    lines.extend(["", "## Community Field Opportunities"])
    for row in context.get("field_opportunities") or []:
        fields = ", ".join(str(field) for field in (row.get("low_overlap_fields") or row.get("fields") or [])[:8])
        risks = ", ".join(str(flag) for flag in (row.get("risk_flags") or [])[:6])
        route = ", ".join(str(item) for item in (row.get("community_skill_route") or [])[:4])
        expression = row.get("expression")
        suffix = f"; risks={risks}" if risks else ""
        suffix += f"; route={route}" if route else ""
        suffix += f"; expr={_short_expr(expression)}" if expression else "; expr=withheld"
        lines.append(f"- {row.get('tag') or row.get('source') or 'community'}: fields={fields}{suffix}")
    lines.extend(["", "## Community Skills"])
    for skill in context.get("community_skills") or []:
        risks = ", ".join(str(flag) for flag in (skill.get("top_risk_flags") or [])[:6])
        fields = ", ".join(str(field) for field in (skill.get("top_fields") or [])[:6])
        lines.append(
            f"- {skill.get('skill_id')}: evidence={skill.get('record_count') or skill.get('recipe_evidence') or 0}; "
            f"risks={risks or 'none'}; fields={fields or 'none'}; action={_short_expr(skill.get('action'), 220)}"
        )
    lines.extend([
        "",
        "## Generation Rules",
        "- Return only valid WorldQuant BRAIN FASTEXPR expressions.",
        "- Do not copy exact ACTIVE expressions.",
        "- Prefer behaviorally different fields/operators when avoiding self-correlation.",
        "- Do not submit unchanged community/forum templates; require transformed structure and fresh checks.",
        "- Use near-pass skill routes for repair before spending fresh exploration budget.",
        "- Use simple, testable structures before adding complex blends.",
    ])
    return "\n".join(lines) + "\n"


def build_candidate_generation_prompt(memory_markdown: str, *, target: int, examples: list[dict]) -> str:
    example_text = "\n".join(f"- {row['expression']}" for row in examples[:3] if row.get("expression"))
    return (
        "You are the model-driven CandidateDesignerAgent for WorldQuant BRAIN.\n"
        f"Generate up to {target} diverse candidate alphas as JSON.\n"
        "Return a JSON array. Each object must include: expression, rationale, "
        "expected_low_corr_reason, source_fields, mutation_strategy, parent_alpha_ids, risk_flags.\n"
        "Do not include markdown or commentary outside JSON.\n"
        "Hard constraints: valid FASTEXPR, no exact copies of active alphas, keep expressions concise.\n\n"
        "Fallback examples for style only, do not copy verbatim:\n"
        f"{example_text}\n\n"
        f"{memory_markdown}"
    )


def build_repair_generation_prompt(memory_markdown: str, repairable: list[dict]) -> str:
    rows = []
    for row in repairable[:20]:
        rows.append({
            "alpha_id": row.get("alpha_id"),
            "expression": row.get("expression"),
            "triage_reason": row.get("triage_reason"),
            "sharpe": row.get("sharpe"),
            "fitness": row.get("fitness"),
            "turnover": row.get("turnover"),
            "failed_platform_checks": row.get("failed_platform_checks"),
            "sc_value": row.get("sc_value"),
        })
    return (
        "You are the model-driven FailureReviewAgent for WorldQuant BRAIN.\n"
        "For each repairable row, propose repair plans and candidate expressions as JSON.\n"
        "Return a JSON array. Each object must include: source_expression, failure_kind, diagnosis, "
        "repair_objective, candidate_expressions, risk_notes.\n"
        "Do not include markdown or commentary outside JSON.\n"
        "If self-correlation is the issue, change field/operator family rather than just windows.\n\n"
        f"Repairable rows:\n{json.dumps(rows, ensure_ascii=False, default=str)[:8000]}\n\n"
        f"{memory_markdown}"
    )


def default_model_generate_candidates(prompt: str, config: WQAgentWorkflowConfig) -> str:
    return _call_deepseek_json(prompt, temperature=0.7, max_tokens=1800)


def default_model_generate_repairs(prompt: str, config: WQAgentWorkflowConfig) -> str:
    return _call_deepseek_json(prompt, temperature=0.4, max_tokens=1800)


def parse_model_candidate_response(response: Any) -> list[dict]:
    candidates = _response_items(response, preferred_key="candidates")
    parsed: list[dict] = []
    for index, item in enumerate(candidates):
        if not isinstance(item, dict):
            continue
        expression = clean_expression(str(item.get("expression") or ""))
        if not expression:
            continue
        parsed.append({
            "expression": expression,
            "tag": item.get("tag") or f"model-candidate-{index + 1}",
            "rationale": item.get("rationale"),
            "expected_low_corr_reason": item.get("expected_low_corr_reason"),
            "source_fields": item.get("source_fields") if isinstance(item.get("source_fields"), list) else _fields(expression),
            "mutation_strategy": item.get("mutation_strategy") or "model_generated",
            "parent_alpha_ids": item.get("parent_alpha_ids") if isinstance(item.get("parent_alpha_ids"), list) else [],
            "risk_flags": item.get("risk_flags") if isinstance(item.get("risk_flags"), list) else [],
            "source_family": item.get("source_family") or item.get("mutation_strategy") or "model_generated",
        })
    return parsed


def parse_model_repair_response(response: Any) -> list[dict]:
    repairs = _response_items(response, preferred_key="repairs")
    parsed: list[dict] = []
    for item in repairs:
        if not isinstance(item, dict):
            continue
        expressions = []
        for expr in item.get("candidate_expressions") or item.get("repair_expressions") or []:
            cleaned = clean_expression(str(expr))
            if cleaned:
                expressions.append(cleaned)
        parsed.append({
            "created_at": _now(),
            "source_expression": item.get("source_expression") or item.get("expression"),
            "failure_kind": item.get("failure_kind"),
            "diagnosis": item.get("diagnosis"),
            "repair_objective": item.get("repair_objective"),
            "candidate_expressions": expressions,
            "risk_notes": item.get("risk_notes") if isinstance(item.get("risk_notes"), list) else [],
            "model_generated": True,
        })
    return parsed


def _response_items(response: Any, *, preferred_key: str) -> list[Any]:
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        value = response.get(preferred_key)
        if isinstance(value, list):
            return value
        if isinstance(response.get("items"), list):
            return response["items"]
        return [response]
    if isinstance(response, str):
        payload = _extract_json_payload(response)
        return _response_items(payload, preferred_key=preferred_key)
    return []


def _extract_json_payload(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"(\[.*\]|\{.*\})", cleaned, flags=re.DOTALL)
    if not match:
        raise ValueError("model response did not contain JSON")
    return json.loads(match.group(1))


def _call_deepseek_json(prompt: str, *, temperature: float, max_tokens: int) -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"))
    response = client.chat.completions.create(
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        messages=[
            {"role": "system", "content": "Return strict JSON only. No markdown. No prose outside JSON."},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=60,
    )
    return response.choices[0].message.content or ""


def _summarize_rows(rows: list[dict], *, limit: int) -> list[dict]:
    out = []
    for row in rows[:limit]:
        expression = row.get("expression") or row.get("source_expression")
        out.append({
            "alpha_id": row.get("alpha_id"),
            "status": row.get("status") or row.get("source_status") or row.get("triage_bucket"),
            "failure_kind": row.get("failure_kind") or row.get("review_failure_kind"),
            "expression": expression,
            "fields": row.get("fields") or _fields(expression or ""),
            "operators": row.get("operators") or _operators(expression or ""),
            "sharpe": row.get("sharpe"),
            "fitness": row.get("fitness"),
            "turnover": row.get("turnover"),
            "sc_value": row.get("sc_value"),
            "triage_reason": row.get("triage_reason"),
            "tag": row.get("tag"),
            "low_overlap_fields": row.get("low_overlap_fields"),
            "risk_flags": row.get("risk_flags") or [],
            "community_skill_route": row.get("community_skill_route") or row.get("community_skill_tags") or [],
            "repair_strategy_hints": row.get("repair_strategy_hints") or [],
            "community_skill_risk_flags": row.get("community_skill_risk_flags") or [],
        })
    return out


def _short_expr(expression: Any, limit: int = 180) -> str:
    text = str(expression or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."
