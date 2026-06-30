"""Stable contracts for no-submit agent harness runs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HARNESS_SCHEMA_VERSION = 1

HARNESS_ROLES = {
    "researcher",
    "verifier",
    "simulator",
    "critic",
    "reflector",
    "submitter",
}

HARNESS_STEP_STATUSES = {
    "pending",
    "running",
    "completed",
    "failed",
    "skipped",
}

HARNESS_RUN_STATUSES = {
    "created",
    "running",
    "completed",
    "failed",
}

HARNESS_EVENT_TYPES = {
    "run_created",
    "context_loaded",
    "hypothesis_created",
    "candidates_proposed",
    "candidate_specs_constrained",
    "candidates_validated",
    "presubmit_ran",
    "gate_reviewed",
    "review_decision_recorded",
    "evaluated",
    "reflected",
    "submit_evidence_recorded",
    "profile_candidate_written",
    "memory_delta_written",
    "run_completed",
    "run_failed",
}


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    artifact_type: str
    schema_version: int = HARNESS_SCHEMA_VERSION
    producer_step: str = ""
    content_hash: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict_clean(self)


@dataclass(frozen=True)
class HarnessEvent:
    event_id: str
    run_id: str
    event_type: str
    role: str
    step_id: str = ""
    candidate_uid: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    schema_version: int = HARNESS_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        _validate_member(self.event_type, HARNESS_EVENT_TYPES, "event_type")
        _validate_member(self.role, HARNESS_ROLES, "role")
        return asdict_clean(self)


@dataclass(frozen=True)
class HarnessStep:
    step_id: str
    run_id: str
    role: str
    action: str
    status: str = "completed"
    input_refs: list[ArtifactRef] = field(default_factory=list)
    output_refs: list[ArtifactRef] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    schema_version: int = HARNESS_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        _validate_member(self.role, HARNESS_ROLES, "role")
        _validate_member(self.status, HARNESS_STEP_STATUSES, "status")
        payload = asdict_clean(self)
        payload["input_refs"] = [ref.to_dict() for ref in self.input_refs]
        payload["output_refs"] = [ref.to_dict() for ref in self.output_refs]
        return payload


@dataclass(frozen=True)
class DecisionGate:
    gate_name: str
    decision: str
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    human_required: bool = False
    schema_version: int = HARNESS_SCHEMA_VERSION
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict_clean(self)


@dataclass(frozen=True)
class MemoryDelta:
    memory_kind: str
    action: str
    key: str
    reason: str
    evidence_refs: list[ArtifactRef] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = HARNESS_SCHEMA_VERSION
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict_clean(self)
        payload["evidence_refs"] = [ref.to_dict() for ref in self.evidence_refs]
        return payload


@dataclass(frozen=True)
class ProfilePatch:
    target_profile: str
    patch_ops: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[ArtifactRef] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    no_submit: bool = True
    schema_version: int = HARNESS_SCHEMA_VERSION
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict_clean(self)
        payload["evidence_refs"] = [ref.to_dict() for ref in self.evidence_refs]
        validate_no_submit(payload)
        return payload


@dataclass(frozen=True)
class AlphaGPTHypothesis:
    hypothesis_id: str
    run_id: str
    topic: str
    statement: str
    rationale: str = ""
    source_refs: list[ArtifactRef] = field(default_factory=list)
    expected_signal: str = ""
    status: str = "proposed"
    no_submit: bool = True
    schema_version: int = HARNESS_SCHEMA_VERSION
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict_clean(self)
        payload["source_refs"] = [ref.to_dict() for ref in self.source_refs]
        validate_no_submit(payload)
        return payload


@dataclass(frozen=True)
class AlphaGPTCandidateSpec:
    candidate_uid: str
    hypothesis_id: str
    expression: str
    research_intent: str = ""
    placeholder_template: str = ""
    placeholder_bindings: dict[str, Any] = field(default_factory=dict)
    generation_constraints: dict[str, Any] = field(default_factory=dict)
    source_family: str = ""
    risk_flags: list[str] = field(default_factory=list)
    no_submit: bool = True
    schema_version: int = HARNESS_SCHEMA_VERSION
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict_clean(self)
        validate_no_submit(payload)
        return payload


@dataclass(frozen=True)
class AlphaGPTReviewDecision:
    candidate_uid: str
    hypothesis_id: str
    decision: str
    reason: str
    evidence_refs: list[ArtifactRef] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    next_action: str = ""
    human_required: bool = False
    no_submit: bool = True
    schema_version: int = HARNESS_SCHEMA_VERSION
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict_clean(self)
        payload["evidence_refs"] = [ref.to_dict() for ref in self.evidence_refs]
        validate_no_submit(payload)
        return payload


@dataclass(frozen=True)
class AlphaGPTReflectionRecord:
    reflection_id: str
    run_id: str
    hypothesis_id: str
    conclusion: str
    memory_actions: list[str] = field(default_factory=list)
    profile_actions: list[str] = field(default_factory=list)
    evidence_refs: list[ArtifactRef] = field(default_factory=list)
    no_submit: bool = True
    schema_version: int = HARNESS_SCHEMA_VERSION
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict_clean(self)
        payload["evidence_refs"] = [ref.to_dict() for ref in self.evidence_refs]
        validate_no_submit(payload)
        return payload


@dataclass(frozen=True)
class AlphaGPTSubmitEvidence:
    run_id: str
    boundary_role: str
    status: str
    explicit_submit_required: bool = True
    selected_alpha_ids: list[str] = field(default_factory=list)
    evidence_refs: list[ArtifactRef] = field(default_factory=list)
    real_submit_attempted: bool = False
    no_submit: bool = True
    schema_version: int = HARNESS_SCHEMA_VERSION
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict_clean(self)
        payload["evidence_refs"] = [ref.to_dict() for ref in self.evidence_refs]
        validate_no_submit(payload)
        return payload


@dataclass(frozen=True)
class HarnessRun:
    run_id: str
    topic: str
    mode: str = "public_harness_eval"
    status: str = "completed"
    no_submit: bool = True
    profile_name: str = "default"
    source_refs: list[ArtifactRef] = field(default_factory=list)
    steps: list[HarnessStep] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    decisions: list[DecisionGate] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    schema_version: int = HARNESS_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        _validate_member(self.status, HARNESS_RUN_STATUSES, "status")
        payload = asdict_clean(self)
        payload["source_refs"] = [ref.to_dict() for ref in self.source_refs]
        payload["steps"] = [step.to_dict() for step in self.steps]
        payload["artifacts"] = [ref.to_dict() for ref in self.artifacts]
        payload["decisions"] = [decision.to_dict() for decision in self.decisions]
        validate_no_submit(payload)
        return payload


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(path: Path | str) -> str:
    file_path = Path(path)
    if not file_path.is_file():
        return ""
    digest = hashlib.sha256()
    with file_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def artifact_ref(
    path: Path | str,
    artifact_type: str,
    *,
    producer_step: str = "",
    created_at: str = "",
) -> ArtifactRef:
    file_path = Path(path)
    return ArtifactRef(
        path=str(file_path),
        artifact_type=artifact_type,
        producer_step=producer_step,
        content_hash=content_hash(file_path),
        created_at=created_at or now_utc(),
    )


def asdict_clean(value: Any) -> dict[str, Any]:
    payload = asdict(value)
    return _clean(payload)


def read_json(path: Path | str) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    text = file_path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not text:
        return {}
    payload = json.loads(text)
    return payload if isinstance(payload, dict) else {}


def read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw in file_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        text = raw.strip()
        if not text or not text.startswith("{"):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def write_json(path: Path | str, payload: dict[str, Any]) -> None:
    validate_no_submit(payload)
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def write_jsonl(path: Path | str, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        validate_no_submit(row)
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
    file_path.write_text(text + ("\n" if rows else ""), encoding="utf-8")


def validate_no_submit(payload: Any) -> None:
    """Reject contract payloads that cross the no-submit boundary."""

    for key, value in _walk_items(payload):
        if key == "no_submit" and value is False:
            raise ValueError("harness contract requires no_submit=True")
        if key == "no_real_submit" and value is False:
            raise ValueError("harness contract requires no_real_submit=True when present")
        if key == "real_submit_attempted" and bool(value):
            raise ValueError("harness contract cannot contain a real submit attempt")


def _walk_items(value: Any) -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            items.append((str(key), child))
            items.extend(_walk_items(child))
    elif isinstance(value, list):
        for child in value:
            items.extend(_walk_items(child))
    return items


def _validate_member(value: str, allowed: set[str], field_name: str) -> None:
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"invalid {field_name}: {value!r}; expected one of {choices}")


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(child) for key, child in value.items() if child not in (None, "", [], {})}
    if isinstance(value, list):
        return [_clean(child) for child in value]
    return value
