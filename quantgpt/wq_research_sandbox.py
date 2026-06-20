"""Local research sandbox for WorldQuant alpha mining.

The sandbox adds quant_wiki-style experiment records around the existing WQ
candidate generator and presubmit workflow. It is intentionally conservative:
it can generate candidates, run find/check presubmit cycles, and write a gate
decision, but it never calls the real submit endpoint.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .expression_parser import extract_components, normalize_expression
from .wq_agent_workflow import (
    GENERATION_EVOLUTIONARY,
    GENERATION_MIXED_EVOLUTIONARY,
    SUBMIT_PROBE_NEEDED,
    WQAgentWorkflowConfig,
    run_workflow,
)
from .wq_research_miner import WQResearchMinerConfig, run_research_miner

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPERIMENT_ROOT = ROOT / "reports" / "wq_research_experiments"
SCHEMA_VERSION = 1


DEFAULT_GATE = {
    "min_ready": 1,
    "require_presubmit_gate": True,
    "require_no_real_submit": True,
    "hold_on_pending_checks": True,
}


@dataclass(frozen=True)
class ResearchSandboxPaths:
    experiment_dir: Path
    experiment: Path
    candidate_specs: Path
    experience_memory: Path
    research_miner_summary: Path
    presubmit_run: Path
    critic_report: Path
    decision: Path
    readme: Path

    @classmethod
    def for_dir(cls, experiment_dir: Path) -> ResearchSandboxPaths:
        return cls(
            experiment_dir=experiment_dir,
            experiment=experiment_dir / "experiment.yaml",
            candidate_specs=experiment_dir / "candidate_specs.jsonl",
            experience_memory=experiment_dir / "experience_memory.jsonl",
            research_miner_summary=experiment_dir / "wq_research_miner_summary.json",
            presubmit_run=experiment_dir / "presubmit_run",
            critic_report=experiment_dir / "critic_report.yaml",
            decision=experiment_dir / "decision.yaml",
            readme=experiment_dir / "README.md",
        )


@dataclass(frozen=True)
class ResearchSandboxMineConfig:
    experiment: Path
    run_dirs: tuple[Path, ...] = field(default_factory=tuple)
    ready_files: tuple[Path, ...] = field(default_factory=tuple)
    rejected_files: tuple[Path, ...] = field(default_factory=tuple)
    active_inventory_files: tuple[Path, ...] = field(default_factory=tuple)
    platform_files: tuple[Path, ...] = field(default_factory=tuple)
    weak_memory_files: tuple[Path, ...] = field(default_factory=tuple)
    submission_policy_file: Path | None = None
    max_candidates: int = 200
    similarity_cutoff: float = 0.72
    max_family_count: int = 8
    max_field_signature_count: int = 4
    target_ready: int = 3
    max_total_simulations: int = 120
    cycle_candidate_count: int = 20
    max_cycles: int = 10
    max_consecutive_empty_cycles: int = 3
    account: str = "primary"
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    decay: int = 8
    neutralization: str = "SUBINDUSTRY"
    truncation: float = 0.08
    allow_model: bool = False
    use_ledger: bool = True
    dry_run: bool = False


def init_research_sandbox(root: Path | None = None) -> dict[str, Any]:
    """Create the local experiment root."""

    root = root or DEFAULT_EXPERIMENT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "manifest.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "kind": "wq_research_sandbox_root",
        "submit_guard": "sandbox never submits; use explicit WQ submit commands for real submission",
        "experiment_root": str(root),
    }
    if not manifest.exists():
        _write_json(manifest, payload)
    return {"ok": True, "experiment_root": str(root), "manifest": str(manifest)}


def new_research_experiment(
    topic: str,
    *,
    root: Path | None = None,
    hypothesis: str = "",
    citations: list[str] | None = None,
    settings: dict[str, Any] | None = None,
    gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one WQ research experiment folder."""

    root = root or DEFAULT_EXPERIMENT_ROOT
    init_research_sandbox(root)
    exp_id = _experiment_id(topic)
    exp_dir = root / exp_id
    exp_dir.mkdir(parents=True, exist_ok=False)
    paths = ResearchSandboxPaths.for_dir(exp_dir)
    merged_settings = {
        "account": "primary",
        "region": "USA",
        "universe": "TOP3000",
        "delay": 1,
        "decay": 8,
        "neutralization": "SUBINDUSTRY",
        "truncation": 0.08,
        **(settings or {}),
    }
    record = {
        "schema_version": SCHEMA_VERSION,
        "id": exp_id,
        "type": "wq_research_experiment",
        "status": "draft",
        "decision": "hold",
        "autonomy": "semi",
        "created_at": _now(),
        "updated_at": _now(),
        "topic": topic,
        "hypothesis": {
            "statement": hypothesis,
            "rationale": "",
            "citations": citations or [],
            "expected_direction": "unknown",
        },
        "settings": merged_settings,
        "gate": {**DEFAULT_GATE, **(gate or {})},
        "submit_guard": "No real submit is allowed from this experiment. Use explicit alpha IDs or run-submit outside the sandbox.",
        "paths": {
            "candidate_specs": paths.candidate_specs.name,
            "experience_memory": paths.experience_memory.name,
            "research_miner_summary": paths.research_miner_summary.name,
            "presubmit_run": paths.presubmit_run.name,
            "critic_report": paths.critic_report.name,
            "decision": paths.decision.name,
        },
    }
    _write_json(paths.experiment, record)
    _write_jsonl(paths.candidate_specs, [])
    _write_jsonl(paths.experience_memory, [])
    _write_json(paths.critic_report, _empty_critic())
    _write_json(paths.decision, _decision_payload(exp_id, "hold", ["experiment created; no presubmit run yet"], record["gate"]))
    paths.readme.write_text(_experiment_readme(exp_id, topic), encoding="utf-8")
    return {
        "ok": True,
        "experiment_id": exp_id,
        "experiment_dir": str(exp_dir),
        "experiment": str(paths.experiment),
    }


def mine_research_experiment(
    config: ResearchSandboxMineConfig,
    *,
    dependencies: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate local candidates and run the presubmit workflow."""

    paths = ResearchSandboxPaths.for_dir(_resolve_experiment_dir(config.experiment))
    record = _read_json(paths.experiment)
    _ensure_experiment(record, paths)
    settings = {**(record.get("settings") or {}), **_settings_from_mine_config(config)}
    hypothesis = str((record.get("hypothesis") or {}).get("statement") or "")

    miner_summary = run_research_miner(
        WQResearchMinerConfig(
            output=paths.candidate_specs,
            run_dirs=config.run_dirs,
            ready_files=config.ready_files,
            rejected_files=config.rejected_files,
            active_inventory_files=config.active_inventory_files,
            platform_files=config.platform_files,
            weak_memory_files=config.weak_memory_files,
            submission_policy_file=config.submission_policy_file,
            memory_output=paths.experience_memory,
            summary_output=paths.research_miner_summary,
            max_candidates=config.max_candidates,
            similarity_cutoff=config.similarity_cutoff,
            max_family_count=config.max_family_count,
            max_field_signature_count=config.max_field_signature_count,
            llm_provider="none",
        )
    )
    candidate_rows = _annotate_candidates(
        _read_jsonl(paths.candidate_specs),
        experiment_id=str(record.get("id")),
        hypothesis=hypothesis,
    )
    _write_jsonl(paths.candidate_specs, candidate_rows)

    workflow_config = WQAgentWorkflowConfig(
        output_dir=paths.presubmit_run,
        candidate_files=[paths.candidate_specs],
        account=str(settings.get("account") or "primary"),
        region=str(settings.get("region") or "USA"),
        universe=str(settings.get("universe") or "TOP3000"),
        delay=int(settings.get("delay") if settings.get("delay") is not None else 1),
        decay=int(settings.get("decay") if settings.get("decay") is not None else 8),
        neutralization=str(settings.get("neutralization") or "SUBINDUSTRY"),
        truncation=float(settings.get("truncation") if settings.get("truncation") is not None else 0.08),
        target_ready=config.target_ready,
        max_total_simulations=config.max_total_simulations,
        cycle_candidate_count=config.cycle_candidate_count,
        max_simulations=config.cycle_candidate_count,
        max_cycles=config.max_cycles,
        max_consecutive_empty_cycles=config.max_consecutive_empty_cycles,
        generation_mode=GENERATION_MIXED_EVOLUTIONARY if config.allow_model else GENERATION_EVOLUTIONARY,
        no_model=not config.allow_model,
        evolutionary_candidates=max(config.cycle_candidate_count * 2, config.cycle_candidate_count),
        fallback_template_limit=0,
        submission_policy_file=config.submission_policy_file,
        use_ledger=config.use_ledger,
        dry_run=config.dry_run,
    )
    presubmit_summary = run_workflow(
        workflow_config,
        mode="presubmit-sequential",
        dependencies=dependencies or {},
    )
    record["status"] = "mined"
    record["updated_at"] = _now()
    record["settings"] = settings
    record["last_mine"] = {
        "candidate_count": len(candidate_rows),
        "research_miner_summary": miner_summary,
        "presubmit_summary": _compact_presubmit_summary(presubmit_summary),
    }
    _write_json(paths.experiment, record)
    return {
        "ok": bool(presubmit_summary.get("ok") or miner_summary.get("ok")),
        "experiment_id": record.get("id"),
        "experiment_dir": str(paths.experiment_dir),
        "candidate_count": len(candidate_rows),
        "research_miner": miner_summary,
        "presubmit": presubmit_summary,
        "files": {
            "candidate_specs": str(paths.candidate_specs),
            "presubmit_run": str(paths.presubmit_run),
            "experiment": str(paths.experiment),
        },
    }


def gate_research_experiment(experiment: Path) -> dict[str, Any]:
    """Apply the sandbox gate and persist critic/decision artifacts."""

    paths = ResearchSandboxPaths.for_dir(_resolve_experiment_dir(experiment))
    record = _read_json(paths.experiment)
    _ensure_experiment(record, paths)
    gate = {**DEFAULT_GATE, **(record.get("gate") or {})}
    presubmit_summary = _read_json(paths.presubmit_run / "summary.json")
    loop_status = _read_json(paths.presubmit_run / "loop_status.json")
    ready = _read_jsonl(paths.presubmit_run / "presubmit_ready_sequential.jsonl")
    rejected = _read_jsonl(paths.presubmit_run / "presubmit_rejected.jsonl")
    review = _read_jsonl(paths.presubmit_run / "review_queue.jsonl")

    critic = build_critic_report(
        record=record,
        gate=gate,
        presubmit_summary=presubmit_summary,
        loop_status=loop_status,
        ready_rows=ready,
        rejected_rows=rejected,
        review_rows=review,
    )
    decision = critic["decision"]
    reasons = critic["reasons"]
    record["decision"] = decision
    record["status"] = "reviewed" if decision in {"promote_candidate", "retire"} else "draft"
    record["updated_at"] = _now()

    decision_payload = _decision_payload(str(record.get("id")), decision, reasons, gate)
    decision_payload["critic_summary"] = critic["summary"]
    _write_json(paths.critic_report, critic)
    _write_json(paths.decision, decision_payload)
    _write_json(paths.experiment, record)
    return {
        "ok": True,
        "experiment_id": record.get("id"),
        "decision": decision,
        "reasons": reasons,
        "critic_report": str(paths.critic_report),
        "decision_file": str(paths.decision),
    }


def build_critic_report(
    *,
    record: dict[str, Any],
    gate: dict[str, Any],
    presubmit_summary: dict[str, Any],
    loop_status: dict[str, Any],
    ready_rows: list[dict[str, Any]],
    rejected_rows: list[dict[str, Any]],
    review_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic critic report from presubmit artifacts."""

    bucket_counts: dict[str, int] = {}
    for row in review_rows:
        bucket = str(row.get("triage_bucket") or "unknown")
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1

    reject_counts: dict[str, int] = {}
    for row in rejected_rows:
        reason = str(row.get("presubmit_reject_reason") or row.get("candidate_skip_reason") or "unknown")
        reject_counts[reason] = reject_counts.get(reason, 0) + 1

    pending_rows = [
        row for row in review_rows
        if row.get("triage_bucket") == SUBMIT_PROBE_NEEDED
        or str(row.get("api_check_status") or "") in {"api_check_pending", "api_check_missing"}
        or str(row.get("sc_result") or "").upper() in {"PENDING", "MISSING"}
    ]
    ready_pass = [row for row in ready_rows if row.get("presubmit_accepted", True)]
    reasons: list[str] = []
    blockers: list[str] = []
    warnings: list[str] = []

    if not presubmit_summary and not loop_status:
        warnings.append("missing presubmit summary")
        reasons.append("missing presubmit run results")
        decision = "hold"
    elif len(ready_pass) >= int(gate.get("min_ready") or 1):
        reasons.append(f"{len(ready_pass)} ready candidate(s) passed presubmit gate")
        decision = "promote_candidate"
    elif gate.get("hold_on_pending_checks", True) and pending_rows:
        reasons.append(f"{len(pending_rows)} candidate(s) still need readable correlation checks")
        decision = "hold"
    else:
        stop_reason = str(loop_status.get("stop_reason") or _nested(presubmit_summary, "presubmit_loop", "stop_reason") or "")
        if stop_reason in {"max_total_simulations_reached", "max_cycles_reached", "max_consecutive_empty_cycles_reached"}:
            blockers.append(f"no ready candidates before stop_reason={stop_reason}")
            reasons.append(blockers[-1])
            decision = "retire"
        else:
            reasons.append(stop_reason or "presubmit incomplete or inconclusive")
            decision = "hold"

    if gate.get("require_no_real_submit", True):
        submitted = [row for row in review_rows if row.get("submitted")]
        if submitted:
            blockers.append("sandbox contains submitted rows")
            reasons.append("sandbox submit guard violated")
            decision = "retire"

    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "experiment_id": record.get("id"),
        "status": "reviewed",
        "decision": decision,
        "reasons": reasons,
        "blockers": blockers,
        "warnings": warnings,
        "summary": {
            "ready_count": len(ready_rows),
            "ready_pass_count": len(ready_pass),
            "rejected_count": len(rejected_rows),
            "review_count": len(review_rows),
            "pending_count": len(pending_rows),
            "bucket_counts": dict(sorted(bucket_counts.items())),
            "reject_counts": dict(sorted(reject_counts.items())),
            "stop_reason": loop_status.get("stop_reason") or _nested(presubmit_summary, "presubmit_loop", "stop_reason"),
            "total_simulations": loop_status.get("total_simulations") or _nested(presubmit_summary, "presubmit_loop", "total_simulations"),
            "no_real_submit": True,
        },
        "checks": {
            "submit_guard": "pass" if not blockers else "fail",
            "presubmit_ready": "pass" if ready_pass else "missing",
            "pending_checks": "warn" if pending_rows else "pass",
        },
        "ready_preview": [_row_preview(row) for row in ready_rows[:10]],
        "rejected_preview": [_row_preview(row) for row in rejected_rows[:10]],
    }


def _settings_from_mine_config(config: ResearchSandboxMineConfig) -> dict[str, Any]:
    return {
        "account": config.account,
        "region": config.region,
        "universe": config.universe,
        "delay": config.delay,
        "decay": config.decay,
        "neutralization": config.neutralization,
        "truncation": config.truncation,
    }


def _annotate_candidates(rows: list[dict[str, Any]], *, experiment_id: str, hypothesis: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        expression = str(row.get("expression") or "")
        meta = dict(row.get("candidate_meta") or {})
        meta["research_sandbox"] = {
            "experiment_id": experiment_id,
            "candidate_spec_id": _candidate_spec_id(experiment_id, expression, index),
        }
        out.append({
            **row,
            "research_experiment_id": experiment_id,
            "hypothesis": hypothesis,
            "candidate_spec_id": meta["research_sandbox"]["candidate_spec_id"],
            "source_fields": row.get("source_fields") or _fields(expression),
            "operators": row.get("operators") or _operators(expression),
            "candidate_meta": meta,
        })
    return out


def _candidate_spec_id(experiment_id: str, expression: str, index: int) -> str:
    digest = hashlib.sha256(f"{experiment_id}|{normalize_expression(expression)}|{index}".encode()).hexdigest()[:8]
    return f"cand-{index:04d}-{digest}"


def _fields(expression: str) -> list[str]:
    try:
        parts = extract_components(expression)
    except Exception:
        return []
    return sorted(str(item) for item in parts.get("fields", []))


def _operators(expression: str) -> list[str]:
    try:
        parts = extract_components(expression)
    except Exception:
        return []
    return sorted(str(item) for item in parts.get("operators", []))


def _compact_presubmit_summary(summary: dict[str, Any]) -> dict[str, Any]:
    loop = summary.get("presubmit_loop") if isinstance(summary.get("presubmit_loop"), dict) else {}
    return {
        "ok": summary.get("ok"),
        "mode": summary.get("mode"),
        "ready_count": loop.get("ready_count"),
        "target_ready": loop.get("target_ready"),
        "stop_reason": loop.get("stop_reason"),
        "total_simulations": loop.get("total_simulations"),
        "files": summary.get("files") or {},
    }


def _empty_critic() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pending",
        "decision": "hold",
        "blockers": [],
        "warnings": [],
        "checks": {
            "submit_guard": "unknown",
            "presubmit_ready": "unknown",
            "pending_checks": "unknown",
        },
    }


def _decision_payload(experiment_id: str, decision: str, reasons: list[str], gate: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "decision": decision,
        "reasons": reasons,
        "gate": gate,
        "evaluated_at": _now(),
    }


def _row_preview(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "alpha_id": row.get("alpha_id"),
        "tag": row.get("tag"),
        "expression": row.get("expression"),
        "triage_bucket": row.get("triage_bucket"),
        "sharpe": row.get("sharpe"),
        "fitness": row.get("fitness"),
        "turnover": row.get("turnover"),
        "sc_value": row.get("sc_value"),
        "reason": row.get("presubmit_reject_reason") or row.get("triage_reason"),
    }


def _ensure_experiment(record: dict[str, Any], paths: ResearchSandboxPaths) -> None:
    if not record:
        raise FileNotFoundError(f"experiment record not found: {paths.experiment}")
    if record.get("type") != "wq_research_experiment":
        raise ValueError(f"not a WQ research experiment: {paths.experiment}")


def _resolve_experiment_dir(experiment: Path) -> Path:
    path = Path(experiment)
    if (path / "experiment.yaml").is_file():
        return path
    if path.is_file():
        return path.parent
    candidate = DEFAULT_EXPERIMENT_ROOT / str(experiment)
    if (candidate / "experiment.yaml").is_file():
        return candidate
    named_candidate = DEFAULT_EXPERIMENT_ROOT / path.name
    if (named_candidate / "experiment.yaml").is_file():
        return named_candidate
    raise FileNotFoundError(f"experiment not found: {experiment}")


def _experiment_id(topic: str) -> str:
    slug = _slug(topic)
    return f"exp-{datetime.now(timezone.utc):%Y%m%d}-{slug}-{secrets.token_hex(2)}"


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "wq-research")[:48].strip("-") or "wq-research"


def _experiment_readme(exp_id: str, topic: str) -> str:
    return f"""# {exp_id}

Topic: {topic}

This sandbox is local research state for WorldQuant alpha mining.

1. `candidate_specs.jsonl` stores generated candidate expressions.
2. `presubmit_run/` contains the existing find/check-only workflow artifacts.
3. `critic_report.yaml` and `decision.yaml` are written by the fixed gate.

No command in this sandbox submits alphas.
"""


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    return json.loads(text)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows) + "\n", encoding="utf-8")
