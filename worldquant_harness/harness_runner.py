"""No-submit runner wrappers for the public agent harness contract."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .harness_contracts import (
    ArtifactRef,
    DecisionGate,
    HarnessEvent,
    HarnessRun,
    HarnessStep,
    MemoryDelta,
    ProfilePatch,
    artifact_ref,
    now_utc,
    read_json,
    write_json,
    write_jsonl,
)

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class HarnessRunnerConfig:
    output_root: Path
    run_id: str = "public-harness-eval"
    topic: str = "public harness eval"
    mode: str = "public_harness_eval"
    profile_name: str = "default"
    no_submit: bool = True


def run_public_harness_eval(config: HarnessRunnerConfig) -> dict[str, Any]:
    """Run the deterministic public demo and write contract-level eval artifacts."""

    if not config.no_submit:
        raise ValueError("public harness eval is no-submit only")

    output_root = _resolve_path(config.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    from scripts.run_public_harness_demo import run_public_harness_demo

    demo = run_public_harness_demo(output_root, run_id=config.run_id)
    if demo.get("real_submit_attempted"):
        raise RuntimeError("public demo attempted a real submit")

    files = {str(key): str(value) for key, value in (demo.get("files") or {}).items()}
    eval_summary = read_json(files.get("eval_summary", ""))
    evolution_result = read_json(files.get("evolution_result", ""))
    metrics = eval_summary.get("metrics") or {}
    reject_counts = eval_summary.get("reject_counts") or {}

    existing_refs = _artifact_refs(files)
    profile_patch_payload = _profile_patch(config, existing_refs, evolution_result)
    memory_delta_rows = _memory_deltas(existing_refs, reject_counts)
    decision_rows = _decision_rows(config, demo, eval_summary)
    trace_rows = _trace_rows(config, demo, eval_summary, profile_patch_payload, memory_delta_rows)
    eval_cases = _eval_cases(
        demo=demo,
        metrics=metrics,
        reject_counts=reject_counts,
        profile_patch=profile_patch_payload,
    )
    eval_result = {
        "schema_version": 1,
        "ok": all(bool(case.get("passed")) for case in eval_cases),
        "created_at": now_utc(),
        "run_id": config.run_id,
        "mode": config.mode,
        "no_submit": True,
        "case_count": len(eval_cases),
        "passed_count": sum(1 for case in eval_cases if case.get("passed")),
        "failed_cases": [case["case_id"] for case in eval_cases if not case.get("passed")],
        "harness_score": eval_summary.get("harness_score"),
        "metrics": metrics,
        "reject_counts": reject_counts,
    }

    generated_files = {
        "agent_trace": output_root / "agent_trace.jsonl",
        "decisions": output_root / "decisions.jsonl",
        "memory_delta": output_root / "memory_delta.jsonl",
        "profile_patch": output_root / "profile_patch.json",
        "eval_cases": output_root / "eval_cases.jsonl",
        "eval_result": output_root / "eval_result.json",
    }
    write_jsonl(generated_files["agent_trace"], trace_rows)
    write_jsonl(generated_files["decisions"], decision_rows)
    write_jsonl(generated_files["memory_delta"], memory_delta_rows)
    write_json(generated_files["profile_patch"], profile_patch_payload)
    write_jsonl(generated_files["eval_cases"], eval_cases)
    write_json(generated_files["eval_result"], eval_result)

    generated_refs = _artifact_refs({key: str(path) for key, path in generated_files.items()})
    artifact_rows = [ref.to_dict() for ref in [*existing_refs, *generated_refs]]
    artifacts_file = output_root / "artifacts.jsonl"
    write_jsonl(artifacts_file, artifact_rows)
    artifacts_ref = artifact_ref(artifacts_file, "artifact_index", producer_step="manifest")

    steps = _steps(config, existing_refs, generated_refs, eval_result)
    decisions = [
        DecisionGate(
            gate_name="public_harness_contract",
            decision="pass" if eval_result["ok"] else "fail",
            reasons=[case["case_id"] for case in eval_cases if case.get("passed")],
            metrics={"passed_count": eval_result["passed_count"], "case_count": eval_result["case_count"]},
            human_required=False,
            created_at=now_utc(),
        ),
        DecisionGate(
            gate_name="submit_boundary",
            decision="hold",
            reasons=["public harness eval is no-submit; real submission remains an explicit human-selected command"],
            metrics={"real_submit_attempted": False},
            human_required=True,
            created_at=now_utc(),
        ),
    ]
    harness_run = HarnessRun(
        run_id=config.run_id,
        topic=config.topic,
        mode=config.mode,
        status="completed" if eval_result["ok"] else "failed",
        no_submit=True,
        profile_name=config.profile_name,
        source_refs=[ref for ref in existing_refs if ref.artifact_type in {"candidate_specs", "demo_summary"}],
        steps=steps,
        artifacts=[*existing_refs, *generated_refs, artifacts_ref],
        decisions=decisions,
        metrics=eval_result,
        created_at=demo.get("created_at") or now_utc(),
        updated_at=now_utc(),
    ).to_dict()
    harness_run_file = output_root / "harness_run.json"
    write_json(harness_run_file, harness_run)

    manifest = {
        "schema_version": 1,
        "created_at": now_utc(),
        "kind": "worldquant_harness_agent_contract_run",
        "run_id": config.run_id,
        "mode": config.mode,
        "no_submit": True,
        "output_root": str(output_root),
        "files": {
            **files,
            **{key: str(path) for key, path in generated_files.items()},
            "artifacts": str(artifacts_file),
            "harness_run": str(harness_run_file),
            "manifest": str(output_root / "manifest.json"),
        },
        "entrypoints": {
            "public_demo": "scripts/run_public_harness_demo.py",
            "public_eval": "scripts/run_public_harness_eval.py",
        },
    }
    manifest_file = output_root / "manifest.json"
    write_json(manifest_file, manifest)

    return {
        "ok": bool(eval_result["ok"]),
        "run_id": config.run_id,
        "mode": config.mode,
        "no_submit": True,
        "output_root": str(output_root),
        "harness_run": harness_run,
        "eval_result": eval_result,
        "files": manifest["files"],
    }


def harness_new(
    output_root: Path | str,
    *,
    topic: str = "WQ harness run",
    run_id: str = "harness-run",
    mode: str = "manual_harness",
    profile_name: str = "default",
) -> dict[str, Any]:
    """Create a no-submit harness run folder without running candidates."""

    root = _resolve_path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    event = HarnessEvent(
        event_id=f"{run_id}:run_created",
        run_id=run_id,
        event_type="run_created",
        role="researcher",
        payload={"topic": topic, "mode": mode, "no_submit": True},
        created_at=now_utc(),
    ).to_dict()
    run = HarnessRun(
        run_id=run_id,
        topic=topic,
        mode=mode,
        status="created",
        no_submit=True,
        profile_name=profile_name,
        created_at=now_utc(),
        updated_at=now_utc(),
    ).to_dict()
    write_json(root / "harness_run.json", run)
    write_jsonl(root / "agent_trace.jsonl", [event])
    write_json(
        root / "manifest.json",
        {
            "schema_version": 1,
            "created_at": now_utc(),
            "kind": "worldquant_harness_manual_run",
            "run_id": run_id,
            "no_submit": True,
            "files": {
                "harness_run": str(root / "harness_run.json"),
                "agent_trace": str(root / "agent_trace.jsonl"),
                "manifest": str(root / "manifest.json"),
            },
        },
    )
    return {"ok": True, "run_id": run_id, "no_submit": True, "output_root": str(root), "harness_run": run}


def harness_run_presubmit(
    output_root: Path | str,
    *,
    run_id: str = "public-harness-eval",
    mode: str = "public_demo",
    topic: str = "public harness eval",
) -> dict[str, Any]:
    """Run a no-submit presubmit wrapper.

    The first stable mode is the deterministic public demo. Live generic
    presubmit remains in the existing workflow scripts because it may call
    platform simulation/check adapters.
    """

    if mode not in {"public_demo", "public_harness_eval"}:
        return {
            "ok": False,
            "no_submit": True,
            "mode": mode,
            "error": "generic live presubmit is intentionally not exposed by this no-submit wrapper",
        }
    return run_public_harness_eval(
        HarnessRunnerConfig(
            output_root=_resolve_path(output_root),
            run_id=run_id,
            topic=topic,
            mode="public_harness_eval",
            no_submit=True,
        )
    )


def harness_evaluate(
    experiment: Path | str,
    *,
    eval_id: str | None = None,
    output_dir: Path | str | None = None,
    submit_run_dirs: list[str] | None = None,
) -> dict[str, Any]:
    """Evaluate one existing sandbox experiment without submitting."""

    from .wq_research_harness import WQHarnessEvalConfig, run_wq_harness_evaluation

    result = run_wq_harness_evaluation(
        WQHarnessEvalConfig(
            experiment=_resolve_experiment_path(experiment),
            eval_id=eval_id or None,
            output_dir=_resolve_optional_path(output_dir),
            submit_run_dirs=tuple(_resolve_path(path) for path in (submit_run_dirs or [])),
        )
    )
    result["no_submit"] = True
    return result


def harness_evolve(
    experiment: Path | str,
    *,
    eval_dir: Path | str | None = None,
    output_root: Path | str | None = None,
    min_improvement: float = 0.02,
    create_child_experiment: bool = True,
) -> dict[str, Any]:
    """Reflect on an evaluated sandbox experiment and write the next profile candidate."""

    from .wq_research_harness import WQHarnessEvolutionConfig, evolve_wq_research_experiment

    result = evolve_wq_research_experiment(
        WQHarnessEvolutionConfig(
            experiment=_resolve_experiment_path(experiment),
            eval_dir=_resolve_optional_path(eval_dir),
            output_root=_resolve_optional_path(output_root),
            min_improvement=min_improvement,
            create_child_experiment=create_child_experiment,
        )
    )
    result["no_submit"] = True
    return result


def harness_history_ingest(
    reports_dir: Path | str = "reports",
    output_dir: Path | str = "reports/harness_history_experience",
    *,
    no_platform: bool = True,
    local_file_limit: int = 0,
    event_limit: int = 0,
) -> dict[str, Any]:
    """Collect local history into canonical experience artifacts."""

    from .wq_history_experience import WQHistoryExperienceConfig, collect_history_experience

    result = collect_history_experience(
        WQHistoryExperienceConfig(
            reports_dir=_resolve_path(reports_dir),
            output_dir=_resolve_path(output_dir),
            platform_enabled=not no_platform,
            local_file_limit=local_file_limit,
            event_limit=event_limit,
            write_ledger=False,
        )
    )
    result["no_submit"] = True
    return result


def harness_memory_maintain(
    memory_files: list[str] | None,
    output_dir: Path | str = "reports/harness_memory_maintenance",
    *,
    compress_threshold: int = 50,
    absorb_threshold: int = 3,
) -> dict[str, Any]:
    """Build memory maintenance and memory-delta artifacts without mutating source memory."""

    from .wq_memory_maintenance import load_memory_rows, memory_maintenance_report, render_memory_maintenance_markdown

    root = _resolve_path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    resolved_files = [_resolve_path(path) for path in (memory_files or [])]
    rows = load_memory_rows(resolved_files)
    report = memory_maintenance_report(rows, compress_threshold=compress_threshold, absorb_threshold=absorb_threshold)
    evidence_refs = [artifact_ref(path, "memory_source", producer_step="memory_maintenance") for path in resolved_files]
    deltas = _maintenance_deltas(report, evidence_refs)
    files = {
        "memory_maintenance": str(root / "memory_maintenance.json"),
        "memory_maintenance_md": str(root / "memory_maintenance.md"),
        "memory_delta": str(root / "memory_delta.jsonl"),
    }
    write_json(files["memory_maintenance"], {**report, "no_submit": True})
    Path(files["memory_maintenance_md"]).write_text(render_memory_maintenance_markdown(report), encoding="utf-8")
    write_jsonl(files["memory_delta"], deltas)
    return {
        "ok": True,
        "no_submit": True,
        "row_count": report.get("row_count"),
        "compression_candidates": len(report.get("compression_candidates") or []),
        "absorption_candidates": len(report.get("absorption_candidates") or []),
        "files": files,
    }


def harness_status(root: Path | str = "reports/public_harness_eval") -> dict[str, Any]:
    """Read persisted harness status from a run directory."""

    path = _resolve_path(root)
    harness_run = read_json(path / "harness_run.json")
    eval_result = read_json(path / "eval_result.json")
    manifest = read_json(path / "manifest.json")
    if not harness_run and not eval_result and not manifest:
        return {"ok": False, "root": str(path), "error": "no harness artifacts found"}
    return {
        "ok": True,
        "root": str(path),
        "run_id": harness_run.get("run_id") or eval_result.get("run_id") or manifest.get("run_id"),
        "status": harness_run.get("status"),
        "no_submit": harness_run.get("no_submit", eval_result.get("no_submit", True)),
        "eval_ok": eval_result.get("ok"),
        "case_count": eval_result.get("case_count"),
        "passed_count": eval_result.get("passed_count"),
        "files": manifest.get("files") or {},
    }


def _artifact_refs(files: dict[str, str]) -> list[ArtifactRef]:
    refs: list[ArtifactRef] = []
    for key, value in sorted(files.items()):
        if not value:
            continue
        path = Path(value)
        if path.is_file():
            refs.append(artifact_ref(path, key, producer_step=_producer_step_for_artifact(key)))
    return refs


def _producer_step_for_artifact(key: str) -> str:
    if key in {"candidate_specs", "legal_inputs"}:
        return "context_loaded"
    if key in {"presubmit_summary", "presubmit_loop_status", "ready", "rejected"}:
        return "presubmit_ran"
    if key in {"critic_report", "decision"}:
        return "gate_reviewed"
    if key in {"eval_summary", "run_report"}:
        return "evaluated"
    if key in {"eval_cases", "eval_result"}:
        return "evaluated"
    if key in {"evolution_result", "profile_patch", "memory_delta"}:
        return "reflected"
    if key in {"decisions"}:
        return "gate_reviewed"
    if key in {"agent_trace"}:
        return "run_completed"
    return "manifest"


def _profile_patch(
    config: HarnessRunnerConfig,
    refs: list[ArtifactRef],
    evolution_result: dict[str, Any],
) -> dict[str, Any]:
    next_generation = evolution_result.get("next_generation") if isinstance(evolution_result.get("next_generation"), dict) else {}
    recommended_candidate = next_generation.get("recommended_profile_candidate")
    recommended_profile = next_generation.get("recommended_research_profile")
    actions = next_generation.get("actions") if isinstance(next_generation.get("actions"), list) else []
    patch_ops = [
        {
            "op": "propose_profile_candidate",
            "path": "/research_profile",
            "candidate": recommended_candidate,
            "value": recommended_profile or {},
            "auto_applied": False,
        }
    ]
    patch_ops.extend(
        {
            "op": "add_iteration_constraint",
            "path": "/mine_defaults/priority_biases",
            "trigger": action.get("trigger"),
            "value": action.get("change"),
            "auto_applied": False,
        }
        for action in actions
        if isinstance(action, dict)
    )
    evidence_refs = [ref for ref in refs if ref.artifact_type in {"eval_summary", "evolution_result", "rejected"}]
    return ProfilePatch(
        target_profile=config.profile_name,
        patch_ops=patch_ops,
        evidence_refs=evidence_refs,
        risk_notes=[
            "Profile patch is a candidate artifact only; it is not applied to a live profile by the runner.",
            "Real WQ submission remains outside the public harness eval path.",
        ],
        no_submit=True,
        created_at=now_utc(),
    ).to_dict()


def _memory_deltas(refs: list[ArtifactRef], reject_counts: dict[str, Any]) -> list[dict[str, Any]]:
    evidence_refs = [ref for ref in refs if ref.artifact_type in {"rejected", "eval_summary"}]
    deltas: list[dict[str, Any]] = []
    for reason, count in sorted(reject_counts.items()):
        if not count:
            continue
        action = "block" if reason in {"illegal_field", "exact_active_duplicate"} else "down_weight"
        deltas.append(
            MemoryDelta(
                memory_kind="presubmit_rejection",
                action=action,
                key=str(reason),
                reason=f"{count} public demo candidate(s) hit {reason}",
                evidence_refs=evidence_refs,
                payload={"count": count, "no_submit": True},
                created_at=now_utc(),
            ).to_dict()
        )
    return deltas


def _maintenance_deltas(report: dict[str, Any], evidence_refs: list[ArtifactRef]) -> list[dict[str, Any]]:
    deltas: list[dict[str, Any]] = []
    for row in report.get("compression_candidates") or []:
        deltas.append(
            MemoryDelta(
                memory_kind="maintenance",
                action="compress",
                key=str(row.get("group_key") or ""),
                reason=f"{row.get('count')} memory rows can be compressed",
                evidence_refs=evidence_refs,
                payload=row,
                created_at=now_utc(),
            ).to_dict()
        )
    for row in report.get("absorption_candidates") or []:
        deltas.append(
            MemoryDelta(
                memory_kind="maintenance",
                action="absorb",
                key=str(row.get("group_key") or ""),
                reason=str(row.get("proposed_policy") or ""),
                evidence_refs=evidence_refs,
                payload=row,
                created_at=now_utc(),
            ).to_dict()
        )
    return deltas


def _decision_rows(
    config: HarnessRunnerConfig,
    demo: dict[str, Any],
    eval_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    gate = eval_summary.get("gate") if isinstance(eval_summary.get("gate"), dict) else {}
    return [
        DecisionGate(
            gate_name="sandbox_gate",
            decision=str((demo.get("gate") or {}).get("decision") or ""),
            reasons=[str(reason) for reason in (demo.get("gate") or {}).get("reasons") or []],
            metrics=demo.get("presubmit") or {},
            human_required=True,
            created_at=now_utc(),
        ).to_dict(),
        DecisionGate(
            gate_name="harness_eval_gate",
            decision=str(gate.get("decision") or ""),
            reasons=[str(reason) for reason in gate.get("reasons") or []],
            metrics={"harness_score": eval_summary.get("harness_score"), **(eval_summary.get("metrics") or {})},
            human_required=False,
            created_at=now_utc(),
        ).to_dict(),
        DecisionGate(
            gate_name="submit_boundary",
            decision="hold",
            reasons=[f"{config.run_id} is a no-submit harness run"],
            metrics={"real_submit_attempted": bool(demo.get("real_submit_attempted"))},
            human_required=True,
            created_at=now_utc(),
        ).to_dict(),
    ]


def _trace_rows(
    config: HarnessRunnerConfig,
    demo: dict[str, Any],
    eval_summary: dict[str, Any],
    profile_patch: dict[str, Any],
    memory_delta_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    reject_counts = eval_summary.get("reject_counts") or {}
    metrics = eval_summary.get("metrics") or {}
    payloads = [
        ("run_created", "researcher", "context_loaded", {"topic": config.topic, "mode": config.mode}),
        ("context_loaded", "researcher", "context_loaded", {"experiment_dir": demo.get("experiment_dir")}),
        ("candidates_proposed", "researcher", "candidates_proposed", {"candidate_count": 5}),
        ("candidates_validated", "verifier", "candidates_validated", {"reject_counts": reject_counts}),
        ("presubmit_ran", "simulator", "presubmit_ran", demo.get("presubmit") or {}),
        ("gate_reviewed", "critic", "gate_reviewed", demo.get("gate") or {}),
        ("evaluated", "verifier", "evaluated", {"harness_score": eval_summary.get("harness_score"), "metrics": metrics}),
        ("reflected", "reflector", "reflected", demo.get("evolution") or {}),
        (
            "profile_candidate_written",
            "reflector",
            "profile_candidate_written",
            {"patch_ops": len(profile_patch.get("patch_ops") or []), "auto_applied": False},
        ),
        (
            "memory_delta_written",
            "reflector",
            "memory_delta_written",
            {"delta_count": len(memory_delta_rows)},
        ),
        ("run_completed", "verifier", "run_completed", {"no_submit": True, "real_submit_attempted": False}),
    ]
    return [
        HarnessEvent(
            event_id=f"{config.run_id}:{index:03d}:{event_type}",
            run_id=config.run_id,
            event_type=event_type,
            role=role,
            step_id=step_id,
            payload=payload,
            created_at=now_utc(),
        ).to_dict()
        for index, (event_type, role, step_id, payload) in enumerate(payloads, start=1)
    ]


def _steps(
    config: HarnessRunnerConfig,
    existing_refs: list[ArtifactRef],
    generated_refs: list[ArtifactRef],
    eval_result: dict[str, Any],
) -> list[HarnessStep]:
    metrics = eval_result.get("metrics") if isinstance(eval_result.get("metrics"), dict) else {}

    def refs_for(step_id: str, refs: list[ArtifactRef]) -> list[ArtifactRef]:
        return [ref for ref in refs if ref.producer_step == step_id or ref.artifact_type == step_id]

    time = now_utc()
    return [
        HarnessStep(
            step_id="context_loaded",
            run_id=config.run_id,
            role="researcher",
            action="load public demo legal inputs and sandbox experiment",
            output_refs=refs_for("context_loaded", existing_refs),
            started_at=time,
            finished_at=time,
        ),
        HarnessStep(
            step_id="candidates_proposed",
            run_id=config.run_id,
            role="researcher",
            action="write deterministic public candidate batch",
            output_refs=[ref for ref in existing_refs if ref.artifact_type == "candidate_specs"],
            started_at=time,
            finished_at=time,
            metrics={"candidate_count": 5},
        ),
        HarnessStep(
            step_id="candidates_validated",
            run_id=config.run_id,
            role="verifier",
            action="reject illegal input and known active duplicate before promotion",
            input_refs=[ref for ref in existing_refs if ref.artifact_type == "candidate_specs"],
            output_refs=[ref for ref in existing_refs if ref.artifact_type == "rejected"],
            started_at=time,
            finished_at=time,
            metrics={"illegal_input_reject_count": metrics.get("illegal_input_reject_count")},
        ),
        HarnessStep(
            step_id="presubmit_ran",
            run_id=config.run_id,
            role="simulator",
            action="run fake simulation and check adapters",
            output_refs=refs_for("presubmit_ran", existing_refs),
            started_at=time,
            finished_at=time,
            metrics={"ready_count": metrics.get("ready_count"), "total_simulations": metrics.get("total_simulations")},
        ),
        HarnessStep(
            step_id="gate_reviewed",
            run_id=config.run_id,
            role="critic",
            action="review sandbox gate and reject pending blockers",
            output_refs=refs_for("gate_reviewed", existing_refs),
            started_at=time,
            finished_at=time,
        ),
        HarnessStep(
            step_id="evaluated",
            run_id=config.run_id,
            role="verifier",
            action="compute harness score and eval cases",
            output_refs=[*refs_for("evaluated", existing_refs), *[ref for ref in generated_refs if ref.artifact_type in {"eval_cases", "eval_result"}]],
            started_at=time,
            finished_at=time,
            metrics={"harness_score": eval_result.get("harness_score"), "ready_count": metrics.get("ready_count")},
        ),
        HarnessStep(
            step_id="reflected",
            run_id=config.run_id,
            role="reflector",
            action="write profile candidate and memory deltas",
            output_refs=[*refs_for("reflected", existing_refs), *[ref for ref in generated_refs if ref.artifact_type in {"profile_patch", "memory_delta"}]],
            started_at=time,
            finished_at=time,
        ),
        HarnessStep(
            step_id="submit_guard",
            run_id=config.run_id,
            role="submitter",
            action="hold real submission behind explicit human-selected alpha IDs",
            status="skipped",
            started_at=time,
            finished_at=time,
            metrics={"real_submit_attempted": False},
        ),
    ]


def _eval_cases(
    *,
    demo: dict[str, Any],
    metrics: dict[str, Any],
    reject_counts: dict[str, Any],
    profile_patch: dict[str, Any],
) -> list[dict[str, Any]]:
    cases = [
        ("ready_candidate", metrics.get("ready_count") == 1, {"ready_count": metrics.get("ready_count")}),
        (
            "strict_self_correlation_rejected",
            reject_counts.get("self_correlation_value_above_strict_cutoff") == 1,
            {"count": reject_counts.get("self_correlation_value_above_strict_cutoff")},
        ),
        ("illegal_field_rejected", reject_counts.get("illegal_field") == 1, {"count": reject_counts.get("illegal_field")}),
        (
            "duplicate_active_rejected",
            reject_counts.get("exact_active_duplicate") == 1,
            {"count": reject_counts.get("exact_active_duplicate")},
        ),
        (
            "no_real_submit",
            demo.get("real_submit_attempted") is False,
            {"real_submit_attempted": demo.get("real_submit_attempted")},
        ),
        (
            "profile_patch_generated_not_applied",
            bool(profile_patch.get("patch_ops")) and all(not op.get("auto_applied") for op in profile_patch.get("patch_ops") or []),
            {"patch_ops": len(profile_patch.get("patch_ops") or [])},
        ),
    ]
    return [
        {
            "schema_version": 1,
            "case_id": case_id,
            "passed": bool(passed),
            "observed": observed,
            "created_at": now_utc(),
            "no_submit": True,
        }
        for case_id, passed, observed in cases
    ]


def _resolve_path(path: Path | str) -> Path:
    value = Path(path)
    return value if value.is_absolute() else ROOT / value


def _resolve_experiment_path(path: Path | str) -> Path:
    value = Path(path)
    if value.is_absolute() or value.exists():
        return value
    rooted = ROOT / value
    return rooted if rooted.exists() else value


def _resolve_optional_path(path: Path | str | None) -> Path | None:
    if path is None or str(path) == "":
        return None
    return _resolve_path(path)
