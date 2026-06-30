"""Minimal no-submit Alpha-GPT-style dry-run workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .expression_parser import extract_components, normalize_expression, parse_expression
from .harness_contracts import (
    AlphaGPTCandidateSpec,
    AlphaGPTHypothesis,
    AlphaGPTReflectionRecord,
    AlphaGPTReviewDecision,
    AlphaGPTSubmitEvidence,
    MemoryDelta,
    ProfilePatch,
    now_utc,
    write_json,
    write_jsonl,
)
from .wq_alpha_gpt_contracts import (
    default_placeholder_specs,
    review_decision_for_validation,
)
from .wq_legal_inputs import load_optional_legal_input_registry

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DRY_RUN_FIELDS = {"open", "high", "low", "close", "volume", "vwap", "returns", "cap", "market_cap"}


@dataclass(frozen=True)
class AlphaGPTWorkflowConfig:
    output_dir: Path
    topic: str
    run_id: str = "alpha-gpt-dry-run"
    profile_name: str = "default"
    account: str = "primary"
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    legal_inputs_file: Path | None = None
    strict_legal_inputs: bool = True
    include_negative_fixture: bool = True


@dataclass(frozen=True)
class AlphaGPTWorkflowPaths:
    output_dir: Path
    manifest: Path
    hypotheses: Path
    placeholder_templates: Path
    candidate_specs: Path
    local_validation: Path
    review_queue: Path
    reflection_memory: Path
    reflection_records: Path
    profile_patch: Path
    submit_evidence: Path
    summary: Path

    @classmethod
    def for_output_dir(cls, output_dir: Path) -> AlphaGPTWorkflowPaths:
        return cls(
            output_dir=output_dir,
            manifest=output_dir / "manifest.json",
            hypotheses=output_dir / "hypotheses.jsonl",
            placeholder_templates=output_dir / "placeholder_templates.jsonl",
            candidate_specs=output_dir / "candidate_specs.jsonl",
            local_validation=output_dir / "local_validation.jsonl",
            review_queue=output_dir / "review_queue.jsonl",
            reflection_memory=output_dir / "reflection_memory.jsonl",
            reflection_records=output_dir / "reflection_records.jsonl",
            profile_patch=output_dir / "profile_patch.json",
            submit_evidence=output_dir / "submit_evidence.json",
            summary=output_dir / "summary.json",
        )


def run_alpha_gpt_dry_run(config: AlphaGPTWorkflowConfig) -> dict[str, Any]:
    """Run topic -> hypothesis -> placeholder implementation -> review artifacts."""

    output_dir = _resolve_output_dir(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = AlphaGPTWorkflowPaths.for_output_dir(output_dir)
    hypothesis_id = f"{config.run_id}:hypothesis:001"

    hypothesis = AlphaGPTHypothesis(
        hypothesis_id=hypothesis_id,
        run_id=config.run_id,
        topic=config.topic,
        statement=f"Explore constrained WorldQuant-style alpha candidates for: {config.topic}",
        rationale=(
            "This dry run follows an Alpha-GPT-style loop: ideate a hypothesis, implement placeholder "
            "FASTEXPR templates, locally validate, review, and write memory/profile artifacts without submission."
        ),
        expected_signal="validated candidate specs plus reviewable rejection memory",
        status="dry_run",
        created_at=now_utc(),
    ).to_dict()
    placeholder_rows = [spec.to_dict() for spec in default_placeholder_specs(include_negative_fixture=config.include_negative_fixture)]
    candidate_rows = _candidate_specs(config, placeholder_rows, hypothesis_id=hypothesis_id)
    validation_rows = _validate_candidates(config, candidate_rows)
    review_rows = _review_rows(config, validation_rows, hypothesis_id=hypothesis_id)
    memory_rows = _memory_rows(config, validation_rows)
    reflection_rows = _reflection_rows(config, hypothesis_id, review_rows, memory_rows)
    profile_patch = _profile_patch(config, validation_rows, memory_rows)
    submit_evidence = AlphaGPTSubmitEvidence(
        run_id=config.run_id,
        boundary_role="terminal_evidence_source",
        status="not_attempted_in_alpha_gpt_dry_run",
        explicit_submit_required=True,
        selected_alpha_ids=[],
        real_submit_attempted=False,
        no_submit=True,
        created_at=now_utc(),
    ).to_dict()

    files = {
        "manifest": str(paths.manifest),
        "hypotheses": str(paths.hypotheses),
        "placeholder_templates": str(paths.placeholder_templates),
        "candidate_specs": str(paths.candidate_specs),
        "local_validation": str(paths.local_validation),
        "review_queue": str(paths.review_queue),
        "reflection_memory": str(paths.reflection_memory),
        "reflection_records": str(paths.reflection_records),
        "profile_patch": str(paths.profile_patch),
        "submit_evidence": str(paths.submit_evidence),
        "summary": str(paths.summary),
    }
    decision_counts = _counts(review_rows, "decision")
    summary = {
        "schema_version": 1,
        "ok": True,
        "created_at": now_utc(),
        "run_id": config.run_id,
        "mode": "alpha_gpt_dry_run",
        "topic": config.topic,
        "no_submit": True,
        "real_submit_attempted": False,
        "candidate_count": len(candidate_rows),
        "validation_passed": sum(1 for row in validation_rows if row.get("ok")),
        "validation_failed": sum(1 for row in validation_rows if not row.get("ok")),
        "review_decisions": decision_counts,
        "files": files,
    }
    manifest = {
        "schema_version": 1,
        "kind": "alpha_gpt_dry_run",
        "created_at": now_utc(),
        "run_id": config.run_id,
        "topic": config.topic,
        "no_submit": True,
        "config": _config_dict(config),
        "files": files,
    }

    write_jsonl(paths.hypotheses, [hypothesis])
    write_jsonl(paths.placeholder_templates, placeholder_rows)
    write_jsonl(paths.candidate_specs, candidate_rows)
    write_jsonl(paths.local_validation, validation_rows)
    write_jsonl(paths.review_queue, review_rows)
    write_jsonl(paths.reflection_memory, memory_rows)
    write_jsonl(paths.reflection_records, reflection_rows)
    write_json(paths.profile_patch, profile_patch)
    write_json(paths.submit_evidence, submit_evidence)
    write_json(paths.summary, summary)
    write_json(paths.manifest, manifest)
    return summary


def _candidate_specs(
    config: AlphaGPTWorkflowConfig,
    placeholder_rows: list[dict[str, Any]],
    *,
    hypothesis_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(placeholder_rows, start=1):
        expression = render_placeholder_template(
            str(row.get("placeholder_template") or ""),
            row.get("placeholder_bindings") if isinstance(row.get("placeholder_bindings"), dict) else {},
        )
        rows.append(
            AlphaGPTCandidateSpec(
                candidate_uid=f"{config.run_id}:candidate:{index:03d}",
                hypothesis_id=hypothesis_id,
                expression=expression,
                research_intent=str(row.get("research_intent") or ""),
                placeholder_template=str(row.get("placeholder_template") or ""),
                placeholder_bindings=row.get("placeholder_bindings") if isinstance(row.get("placeholder_bindings"), dict) else {},
                generation_constraints={
                    "source": "alpha_gpt_placeholder_dry_run",
                    "legal_field_registry": bool(config.legal_inputs_file),
                    "operator_registry": True,
                    "strict_legal_inputs": config.strict_legal_inputs,
                    "region": config.region,
                    "universe": config.universe,
                    "delay": config.delay,
                },
                source_family=str(row.get("source_family") or ""),
                risk_flags=row.get("risk_flags") if isinstance(row.get("risk_flags"), list) else [],
                created_at=now_utc(),
            ).to_dict()
            | {
                "template_id": row.get("template_id"),
                "review_hint": row.get("review_hint"),
                "no_submit": True,
            }
        )
    return rows


def render_placeholder_template(template: str, bindings: dict[str, Any]) -> str:
    expression = str(template or "")
    for key in sorted(bindings, key=len, reverse=True):
        expression = expression.replace(str(key), str(bindings[key]))
    return expression


def _validate_candidates(config: AlphaGPTWorkflowConfig, candidate_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    registry = load_optional_legal_input_registry(config.legal_inputs_file) if config.legal_inputs_file else None
    rows: list[dict[str, Any]] = []
    for row in candidate_rows:
        expression = str(row.get("expression") or "")
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        fields: list[str] = []
        operators: list[str] = []
        normalized = normalize_expression(expression)
        if registry is not None:
            result = registry.validate_candidate(
                {"expression": expression},
                account=config.account,
                region=config.region,
                universe=config.universe,
                delay=config.delay,
                strict=config.strict_legal_inputs,
            )
            errors = list(result.errors)
            warnings = list(result.warnings)
            fields = list(result.fields)
            operators = list(result.operators)
            normalized = result.normalized_expression
        else:
            try:
                parse_expression(expression, mode="wq")
                components = extract_components(expression)
                fields = sorted(str(field) for field in components.get("fields", []))
                operators = sorted(str(op) for op in components.get("operators", []))
                illegal_fields = [field for field in fields if field not in DEFAULT_DRY_RUN_FIELDS]
                if illegal_fields:
                    errors.append(
                        {
                            "code": "illegal_field",
                            "fields": illegal_fields,
                            "message": "field is not in the default dry-run field set",
                        }
                    )
            except Exception as exc:
                errors.append({"code": "illegal_expression", "message": str(exc)})
        rows.append(
            {
                "schema_version": 1,
                "created_at": now_utc(),
                "run_id": config.run_id,
                "candidate_uid": row.get("candidate_uid"),
                "template_id": row.get("template_id"),
                "expression": expression,
                "normalized_expression": normalized,
                "ok": not errors,
                "errors": errors,
                "warnings": warnings,
                "primary_error_code": str(errors[0].get("code") or "") if errors else "",
                "fields": fields,
                "operators": operators,
                "review_hint": row.get("review_hint"),
                "no_submit": True,
            }
        )
    return rows


def _review_rows(
    config: AlphaGPTWorkflowConfig,
    validation_rows: list[dict[str, Any]],
    *,
    hypothesis_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in validation_rows:
        decision, reason, next_action, human_required = review_decision_for_validation(row)
        payload = AlphaGPTReviewDecision(
            candidate_uid=str(row.get("candidate_uid") or ""),
            hypothesis_id=hypothesis_id,
            decision=decision,
            reason=reason,
            metrics={
                "local_validation_ok": row.get("ok"),
                "field_count": len(row.get("fields") or []),
                "operator_count": len(row.get("operators") or []),
            },
            next_action=next_action,
            human_required=human_required,
            created_at=now_utc(),
        ).to_dict()
        payload.update(
            {
                "run_id": config.run_id,
                "template_id": row.get("template_id"),
                "expression": row.get("expression"),
                "triage_bucket": decision,
                "no_submit": True,
            }
        )
        rows.append(payload)
    return rows


def _memory_rows(config: AlphaGPTWorkflowConfig, validation_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in validation_rows:
        if row.get("ok"):
            continue
        key = str(row.get("primary_error_code") or "local_validation_failed")
        rows.append(
            MemoryDelta(
                memory_kind="alpha_gpt_local_validation",
                action="block" if key in {"illegal_field", "illegal_operator"} else "down_weight",
                key=key,
                reason=f"{row.get('template_id')} failed local validation: {key}",
                payload={
                    "candidate_uid": row.get("candidate_uid"),
                    "template_id": row.get("template_id"),
                    "expression": row.get("expression"),
                    "errors": row.get("errors") or [],
                    "no_submit": True,
                },
                created_at=now_utc(),
            ).to_dict()
            | {"run_id": config.run_id}
        )
    return rows


def _reflection_rows(
    config: AlphaGPTWorkflowConfig,
    hypothesis_id: str,
    review_rows: list[dict[str, Any]],
    memory_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    decisions = _counts(review_rows, "decision")
    return [
        AlphaGPTReflectionRecord(
            reflection_id=f"{config.run_id}:reflection:001",
            run_id=config.run_id,
            hypothesis_id=hypothesis_id,
            conclusion=(
                "Dry-run Alpha-GPT workflow completed with "
                f"{decisions}; profile changes remain proposals and no real submit was attempted."
            ),
            memory_actions=sorted({str(row.get("action") or "") for row in memory_rows if row.get("action")}),
            profile_actions=["propose_placeholder_constrained_generation_policy"],
            created_at=now_utc(),
        ).to_dict()
    ]


def _profile_patch(
    config: AlphaGPTWorkflowConfig,
    validation_rows: list[dict[str, Any]],
    memory_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return ProfilePatch(
        target_profile=config.profile_name,
        patch_ops=[
            {
                "op": "add",
                "path": "/alpha_gpt/placeholders",
                "value": {
                    "enabled": True,
                    "validated_candidates": sum(1 for row in validation_rows if row.get("ok")),
                    "blocked_candidates": len(memory_rows),
                },
                "auto_applied": False,
            }
        ],
        risk_notes=[
            "Dry-run profile patch is not applied automatically.",
            "Real WQ submission remains behind explicit human-selected alpha IDs.",
        ],
        no_submit=True,
        created_at=now_utc(),
    ).to_dict()


def _counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        if value:
            out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


def _config_dict(config: AlphaGPTWorkflowConfig) -> dict[str, Any]:
    data = asdict(config)
    data["output_dir"] = str(config.output_dir)
    if config.legal_inputs_file is not None:
        data["legal_inputs_file"] = str(config.legal_inputs_file)
    return data


def _resolve_output_dir(path: Path | str) -> Path:
    value = Path(path)
    return value if value.is_absolute() else ROOT / value
