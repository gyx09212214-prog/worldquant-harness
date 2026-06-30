"""Run a deterministic public WQ harness demo without external credentials.

The demo exercises the guarded research sandbox path:

experiment -> presubmit-sequential -> gate -> harness eval -> evolve

It uses fake platform/simulation/check adapters, never calls WQ BRAIN, and
never submits. The generated artifacts are suitable for README screenshots and
regression tests.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_agent_workflow import (  # noqa: E402
    GENERATION_TEMPLATE_FALLBACK,
    WQAgentWorkflowConfig,
    run_workflow,
)
from worldquant_harness.wq_legal_inputs import WQLegalInputRegistry  # noqa: E402
from worldquant_harness.wq_research_harness import (  # noqa: E402
    WQHarnessEvalConfig,
    WQHarnessEvolutionConfig,
    evolve_wq_research_experiment,
    render_wq_harness_report,
    run_wq_harness_evaluation,
)
from worldquant_harness.wq_research_sandbox import (  # noqa: E402
    ResearchSandboxPaths,
    gate_research_experiment,
    new_research_experiment,
)

DEMO_TOPIC = "public harness demo"
DEMO_HYPOTHESIS = (
    "A guarded research harness should surface ready, duplicate, illegal-input, "
    "and strict self-correlation outcomes without any real WQ submit call."
)


def run_public_harness_demo(output_root: Path, *, run_id: str = "public-harness-demo") -> dict[str, Any]:
    """Create a deterministic demo experiment and return a file manifest."""

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    inputs_dir = output_root / "inputs"
    experiment_root = output_root / "experiments"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    experiment_root.mkdir(parents=True, exist_ok=True)

    legal_inputs = _write_demo_legal_inputs(inputs_dir)
    experiment = new_research_experiment(
        DEMO_TOPIC,
        root=experiment_root,
        hypothesis=DEMO_HYPOTHESIS,
        citations=["demo:synthetic-fixture"],
        settings={
            "account": "primary",
            "region": "USA",
            "universe": "TOP3000",
            "delay": 1,
            "decay": 8,
            "neutralization": "SUBINDUSTRY",
            "truncation": 0.08,
            "legal_inputs_file": str(legal_inputs),
            "strict_legal_inputs": True,
        },
        gate={"min_ready": 1},
    )
    exp_dir = Path(experiment["experiment_dir"])
    paths = ResearchSandboxPaths.for_dir(exp_dir)
    _write_jsonl(paths.candidate_specs, _demo_candidates())

    submit_calls: list[list[str]] = []
    presubmit = run_workflow(
        WQAgentWorkflowConfig(
            output_dir=paths.presubmit_run,
            candidate_files=[paths.candidate_specs],
            account="primary",
            region="USA",
            universe="TOP3000",
            delay=1,
            decay=8,
            neutralization="SUBINDUSTRY",
            truncation=0.08,
            target_candidates=8,
            max_simulations=8,
            check_chunk_size=8,
            run_checks=True,
            use_ledger=False,
            dry_run=False,
            generation_mode=GENERATION_TEMPLATE_FALLBACK,
            fallback_template_limit=0,
            no_model=True,
            include_platform_candidates=False,
            target_ready=1,
            max_total_simulations=8,
            cycle_candidate_count=8,
            max_cycles=1,
            max_consecutive_empty_cycles=1,
            virtual_similarity_cutoff=0.62,
            presubmit_self_correlation_cutoff=0.60,
            max_virtual_family_count=3,
            max_virtual_field_signature_count=3,
            legal_inputs_file=legal_inputs,
            strict_legal_inputs=True,
        ),
        mode="presubmit-sequential",
        dependencies={
            "list_alphas": _demo_platform_rows,
            "simulate": _fake_simulate,
            "check_submissions": _fake_check_submissions,
            "submit_by_ids": lambda ids, config: submit_calls.append(list(ids)) or {"results": {}},
        },
    )
    gated = gate_research_experiment(exp_dir)
    eval_result = run_wq_harness_evaluation(
        WQHarnessEvalConfig(
            experiment=exp_dir,
            eval_id=run_id,
        )
    )
    evolved = evolve_wq_research_experiment(
        WQHarnessEvolutionConfig(
            experiment=exp_dir,
            eval_dir=Path(eval_result["eval_dir"]),
            min_improvement=0.02,
            create_child_experiment=True,
        )
    )
    report = render_wq_harness_report(Path(eval_result["eval_dir"]))
    eval_summary = _read_json(Path(eval_result["eval_dir"]) / "eval_summary.json")

    result = {
        "ok": True,
        "created_at": _now(),
        "mode": "public_harness_demo",
        "hypothesis": DEMO_HYPOTHESIS,
        "submit_guard": "No real WQ submit call is made; submit_by_ids is a no-op recorder.",
        "real_submit_attempted": bool(submit_calls),
        "output_root": str(output_root),
        "experiment_id": experiment.get("experiment_id"),
        "experiment_dir": str(exp_dir),
        "presubmit": _compact_presubmit(presubmit),
        "gate": {
            "decision": gated.get("decision"),
            "reasons": gated.get("reasons") or [],
        },
        "harness": {
            "score": eval_result.get("harness_score"),
            "gate_decision": (eval_result.get("gate") or {}).get("decision"),
            "metrics": eval_result.get("metrics") or {},
            "reject_counts": eval_summary.get("reject_counts") or {},
        },
        "evolution": {
            "decision": evolved.get("decision"),
            "child_experiment": (evolved.get("next_generation") or {}).get("child_experiment"),
        },
        "files": {
            "legal_inputs": str(legal_inputs),
            "candidate_specs": str(paths.candidate_specs),
            "presubmit_summary": str(paths.presubmit_run / "summary.json"),
            "presubmit_loop_status": str(paths.presubmit_run / "loop_status.json"),
            "ready": str(paths.presubmit_run / "presubmit_ready_sequential.jsonl"),
            "rejected": str(paths.presubmit_run / "presubmit_rejected.jsonl"),
            "critic_report": str(paths.critic_report),
            "decision": str(paths.decision),
            "eval_summary": str(Path(eval_result["eval_dir"]) / "eval_summary.json"),
            "run_report": report["run_report"],
            "evolution_result": str(Path(eval_result["eval_dir"]) / "evolution_result.json"),
        },
    }
    _write_json(output_root / "demo_summary.json", result)
    result["files"]["demo_summary"] = str(output_root / "demo_summary.json")
    return result


def _demo_candidates() -> list[dict[str, Any]]:
    return [
        {
            "expression": "rank(ts_rank(close, 20) - ts_rank(returns, 5))",
            "tag": "demo-ready-lowcorr",
            "source_family": "demo_price_reversal",
            "mutation_strategy": "public_demo_ready",
            "hypothesis_id": "public-demo:hypothesis:001",
            "placeholder_template": "rank(ts_rank(DATA_FIELD1, WINDOW1) - ts_rank(DATA_FIELD2, WINDOW2))",
            "placeholder_bindings": {
                "DATA_FIELD1": "close",
                "DATA_FIELD2": "returns",
                "WINDOW1": 20,
                "WINDOW2": 5,
            },
            "generation_constraints": {"field_registry": "demo_legal_inputs", "operator_registry": "FASTEXPR"},
            "rationale": "Expected ready candidate with clean self-correlation.",
            "risk_flags": [],
        },
        {
            "expression": "rank(ts_rank(vwap, 20) - ts_rank(volume, 10))",
            "tag": "demo-strict-selfcorr",
            "source_family": "demo_liquidity_reversal",
            "mutation_strategy": "public_demo_strict_selfcorr",
            "hypothesis_id": "public-demo:hypothesis:001",
            "placeholder_template": "rank(ts_rank(DATA_FIELD1, WINDOW1) - ts_rank(DATA_FIELD2, WINDOW2))",
            "placeholder_bindings": {
                "DATA_FIELD1": "vwap",
                "DATA_FIELD2": "volume",
                "WINDOW1": 20,
                "WINDOW2": 10,
            },
            "generation_constraints": {"field_registry": "demo_legal_inputs", "operator_registry": "FASTEXPR"},
            "rationale": "Passes platform SC but breaches a stricter local presubmit cutoff.",
            "risk_flags": ["strict self-correlation gate"],
        },
        {
            "expression": "rank(ts_corr(close, volume, 10))",
            "tag": "demo-near-miss-repair",
            "source_family": "demo_price_volume_corr",
            "mutation_strategy": "public_demo_repairable_sc",
            "hypothesis_id": "public-demo:hypothesis:001",
            "placeholder_template": "rank(ts_corr(DATA_FIELD1, DATA_FIELD2, WINDOW1))",
            "placeholder_bindings": {"DATA_FIELD1": "close", "DATA_FIELD2": "volume", "WINDOW1": 10},
            "generation_constraints": {"field_registry": "demo_legal_inputs", "operator_registry": "FASTEXPR"},
            "rationale": "Repairable near miss used to populate the repair queue.",
            "risk_flags": ["self-correlation near miss"],
        },
        {
            "expression": "rank(not_a_real_field)",
            "tag": "demo-illegal-field",
            "source_family": "demo_illegal_input",
            "mutation_strategy": "public_demo_illegal_input",
            "hypothesis_id": "public-demo:hypothesis:001",
            "placeholder_template": "rank(DATA_FIELD1)",
            "placeholder_bindings": {"DATA_FIELD1": "not_a_real_field"},
            "generation_constraints": {"field_registry": "demo_legal_inputs", "operator_registry": "FASTEXPR"},
            "rationale": "Strict legal input registry should reject this before simulation.",
            "risk_flags": ["illegal field"],
        },
        {
            "expression": "rank(close)",
            "tag": "demo-active-duplicate",
            "source_family": "demo_duplicate",
            "mutation_strategy": "public_demo_duplicate",
            "hypothesis_id": "public-demo:hypothesis:001",
            "placeholder_template": "rank(DATA_FIELD1)",
            "placeholder_bindings": {"DATA_FIELD1": "close"},
            "generation_constraints": {"field_registry": "demo_legal_inputs", "operator_registry": "FASTEXPR"},
            "rationale": "Virtual similarity gate should reject this exact ACTIVE duplicate.",
            "risk_flags": ["duplicate active expression"],
        },
    ]


def _demo_platform_rows(config: WQAgentWorkflowConfig) -> list[dict[str, Any]]:
    return [
        {
            "alpha_id": "demo_active_close",
            "expression": "rank(close)",
            "status": "ACTIVE",
            "sharpe": 1.7,
            "fitness": 1.1,
            "turnover": 0.22,
            "source": "public_harness_demo",
        }
    ]


def _fake_simulate(candidate: dict[str, Any], config: WQAgentWorkflowConfig) -> dict[str, Any]:
    tag = str(candidate.get("tag") or "candidate")
    result_by_tag = {
        "demo-ready-lowcorr": _sim_result(tag, sharpe=1.82, fitness=1.24, returns=0.13, turnover=0.22),
        "demo-strict-selfcorr": _sim_result(tag, sharpe=1.70, fitness=1.08, returns=0.11, turnover=0.25),
        "demo-near-miss-repair": _sim_result(tag, sharpe=1.66, fitness=1.02, returns=0.10, turnover=0.30),
    }
    return result_by_tag.get(
        tag,
        {
            "ok": False,
            "alpha_id": _alpha_id(tag),
            "error": "demo simulator received an unexpected candidate",
            "submit_eligible": False,
            "submitted": False,
        },
    )


def _fake_check_submissions(alpha_ids: list[str], config: WQAgentWorkflowConfig) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for alpha_id in alpha_ids:
        if alpha_id == _alpha_id("demo-ready-lowcorr"):
            out[alpha_id] = _check_result(sharpe=1.82, fitness=1.24, returns=0.13, turnover=0.22, sc_value=0.42)
        elif alpha_id == _alpha_id("demo-strict-selfcorr"):
            out[alpha_id] = _check_result(sharpe=1.70, fitness=1.08, returns=0.11, turnover=0.25, sc_value=0.68)
        elif alpha_id == _alpha_id("demo-near-miss-repair"):
            out[alpha_id] = _check_result(
                sharpe=1.66,
                fitness=1.02,
                returns=0.10,
                turnover=0.30,
                sc_value=0.79,
                sc_result="FAIL",
            )
        else:
            out[alpha_id] = {"status": "UNSUBMITTED", "sc_result": "MISSING", "prod_corr_result": "MISSING"}
    return out


def _sim_result(tag: str, *, sharpe: float, fitness: float, returns: float, turnover: float) -> dict[str, Any]:
    return {
        "ok": True,
        "alpha_id": _alpha_id(tag),
        "is_metrics": {
            "sharpe": sharpe,
            "fitness": fitness,
            "returns": returns,
            "turnover": turnover,
            "checks": [
                {"name": "LOW_SHARPE", "result": "PASS", "limit": 1.25, "value": sharpe},
                {"name": "LOW_FITNESS", "result": "PASS", "limit": 1.0, "value": fitness},
                {"name": "LOW_TURNOVER", "result": "PASS", "limit": 0.01, "value": turnover},
                {"name": "HIGH_TURNOVER", "result": "PASS", "limit": 0.70, "value": turnover},
                {"name": "SELF_CORRELATION", "result": "PENDING"},
            ],
        },
        "submit_eligible": True,
        "submitted": False,
    }


def _check_result(
    *,
    sharpe: float,
    fitness: float,
    returns: float,
    turnover: float,
    sc_value: float,
    sc_result: str = "PASS",
) -> dict[str, Any]:
    return {
        "status": "UNSUBMITTED",
        "sharpe": sharpe,
        "fitness": fitness,
        "returns": returns,
        "turnover": turnover,
        "sc_result": sc_result,
        "sc_value": sc_value,
        "prod_corr_result": "MISSING",
    }


def _write_demo_legal_inputs(inputs_dir: Path) -> Path:
    discovery = inputs_dir / "demo_field_discovery.json"
    registry = inputs_dir / "demo_legal_inputs.json"
    _write_json(
        discovery,
        {
            "created_at": "2026-06-24T00:00:00+00:00",
            "combos": [
                {
                    "region": "USA",
                    "universe": "TOP3000",
                    "delay": 1,
                    "datasets": {"results": []},
                    "fields_by_dataset": {},
                }
            ],
        },
    )
    WQLegalInputRegistry.compile_from_discovery(discovery, account="primary").write(registry)
    return registry


def _alpha_id(tag: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", tag).strip("_").lower()
    return f"demo_{slug}"


def _compact_presubmit(summary: dict[str, Any]) -> dict[str, Any]:
    loop = summary.get("presubmit_loop") if isinstance(summary.get("presubmit_loop"), dict) else {}
    return {
        "ok": summary.get("ok"),
        "ready_count": loop.get("ready_count"),
        "total_simulations": loop.get("total_simulations"),
        "stop_reason": loop.get("stop_reason"),
        "files": summary.get("files") or {},
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the deterministic public worldquant-harness harness demo")
    parser.add_argument(
        "--output-root",
        default="reports/public_harness_demo",
        help="Directory where demo inputs, experiment artifacts, and summary are written.",
    )
    parser.add_argument("--run-id", default="public-harness-demo", help="Stable eval id under the experiment evaluations directory.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    output_root = Path(args.output_root)
    if not output_root.is_absolute():
        output_root = ROOT / output_root
    try:
        result = run_public_harness_demo(output_root, run_id=args.run_id)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
