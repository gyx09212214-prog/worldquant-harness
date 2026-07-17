"""Deterministic, offline benchmark for WorldQuant agent memory policies.

The benchmark deliberately owns a small contract that is separate from the
public harness demo contract.  It never imports a live platform client, never
uses a real model, and rejects submit actions before a transport can be called.
Its fixtures describe scripted plans and fake platform responses so that
memory variants can be compared under identical, replayable budgets.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .harness_contracts import validate_no_submit

BENCHMARK_SCHEMA_VERSION = 1
CANONICALIZATION_VERSION = 1
FAKE_MODEL_VERSION = "scripted-wq-agent-v1"
PROMPT_SCHEMA_VERSION = 1
MEMORY_POLICY_VERSION = "benchmark-scoped-memory-v2-reference-1"

DEFAULT_BENCHMARK_VARIANTS = (
    "no_memory",
    "raw_history",
    "scoped_memory_v2",
)

DEFAULT_BENCHMARK_CASE_IDS = (
    "self_corr_repeat",
    "metric_near_miss",
    "concentration_repair",
    "infrastructure_timeout",
    "exact_duplicate",
    "scope_leak",
    "expired_memory",
    "community_memory_taint",
    "ambiguous_submit_resume",
)

_VARIANT_RANK = {name: index for index, name in enumerate(DEFAULT_BENCHMARK_VARIANTS)}
_VERIFIED_MEMORY_TRUST = {"local_verified", "platform_verified"}
_VIOLATION_FIELDS = {
    "ambiguous_success_count",
    "duplicate_attempt_count",
    "duplicate_submit_intent_count",
    "expired_memory_use_count",
    "false_block_count",
    "infrastructure_misclassification_count",
    "repeated_failure_count",
    "scope_leak_count",
    "submit_post_count",
    "unsafe_action_count",
    "untrusted_gate_use_count",
}
_ORACLE_FIELDS = {
    "budget_exhausted",
    "checked_candidate_ids",
    "decision",
    "objective_met",
    "simulated_candidate_ids",
    "status_read_candidate_ids",
    "submit_action_count",
    "submit_intent_count",
    "used_memory_ids",
} | _VIOLATION_FIELDS
_NON_SEMANTIC_TRACE_KEYS = {
    "artifact_root",
    "code_revision",
    "created_at",
    "duration_ms",
    "finished_at",
    "output_dir",
    "started_at",
    "wall_clock_ms",
}


class BenchmarkSafetyError(RuntimeError):
    """Raised when a scripted plan attempts a prohibited side effect."""


class BenchmarkFixtureError(ValueError):
    """Raised when a benchmark fixture is incomplete or inconsistent."""


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    payload: dict[str, Any]
    content_hash: str


@dataclass(frozen=True)
class BenchmarkSuite:
    suite_id: str
    suite_name: str
    description: str
    fixed_clock: str
    variants: tuple[str, ...]
    seeds: tuple[int, ...]
    default_budgets: dict[str, int]
    model: dict[str, Any]
    execution_policy: dict[str, Any]
    cases: tuple[BenchmarkCase, ...]
    fixture_hash: str
    platform_fixture_hash: str


@dataclass(frozen=True)
class BenchmarkConfig:
    suite_path: Path
    output_dir: Path
    seeds: tuple[int, ...] | None = None
    variants: tuple[str, ...] = DEFAULT_BENCHMARK_VARIANTS
    simulation_budget: int | None = None
    code_revision: str | None = None
    model_id: str = FAKE_MODEL_VERSION
    prompt_hash: str = "sha256:benchmark-prompt-schema-v1"
    policy_hash: str = ""
    no_submit: bool = True

    def __post_init__(self) -> None:
        if not self.no_submit:
            raise ValueError("WQ agent benchmark is no-submit only")
        variants = tuple(self.variants)
        if not variants or len(set(variants)) != len(variants):
            raise ValueError("benchmark variants must be non-empty and unique")
        unknown = set(variants) - set(DEFAULT_BENCHMARK_VARIANTS)
        if unknown:
            raise ValueError(f"unknown benchmark variants: {sorted(unknown)}")
        if self.seeds is not None:
            seeds = tuple(int(seed) for seed in self.seeds)
            if not seeds or len(set(seeds)) != len(seeds):
                raise ValueError("benchmark seeds must be non-empty and unique")
        if self.simulation_budget is not None and int(self.simulation_budget) < 0:
            raise ValueError("simulation_budget cannot be negative")


WQAgentBenchmarkConfig = BenchmarkConfig


@dataclass(frozen=True)
class BenchmarkManifest:
    suite_id: str
    manifest_hash: str
    code_revision: str
    code_dirty: bool
    variants: list[str]
    case_ids: list[str]
    seeds: list[int]
    simulation_budget: int | None
    budgets_by_case: dict[str, dict[str, int]]
    model_id: str
    model_hash: str
    prompt_hash: str
    platform_fixture_hash: str
    policy_hash: str
    fixture_hash: str
    trace_hash: str
    trial_count: int
    files: dict[str, dict[str, str]] = field(default_factory=dict)
    schema_version: int = BENCHMARK_SCHEMA_VERSION
    canonicalization_version: int = CANONICALIZATION_VERSION
    prompt_schema_version: int = PROMPT_SCHEMA_VERSION
    fixed_clock: str = ""
    no_submit: bool = True
    allow_network: bool = False
    allow_real_model: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        validate_no_submit(payload)
        return payload


@dataclass
class _BudgetLedger:
    allocated: dict[str, int]
    used: dict[str, int] = field(default_factory=lambda: {
        "model_calls": 0,
        "simulations": 0,
        "checks": 0,
        "status_reads": 0,
        "submit_actions": 0,
    })
    exhausted: bool = False

    def consume(self, key: str) -> bool:
        cap = int(self.allocated.get(key, 0))
        if self.used.get(key, 0) >= cap:
            self.exhausted = True
            return False
        self.used[key] = self.used.get(key, 0) + 1
        return True


class _NoSubmitBenchmarkGateway:
    """Fixture-backed gateway with no live transport dependency."""

    def __init__(self, case: BenchmarkCase, ledger: _BudgetLedger) -> None:
        self.case = case
        self.ledger = ledger
        self.candidates = {
            str(row["candidate_id"]): row
            for row in case.payload.get("candidates") or []
        }
        self.response_offsets: dict[tuple[str, str], int] = {}
        self.runtime_submit_post_count = 0
        self.unsafe_action_count = 0

    def simulate(self, candidate_id: str) -> dict[str, Any]:
        if not self.ledger.consume("simulations"):
            return {"ok": False, "budget_exhausted": True, "operation": "simulate"}
        return self._response(candidate_id, "simulations")

    def check(self, candidate_id: str) -> dict[str, Any]:
        if not self.ledger.consume("checks"):
            return {"ok": False, "budget_exhausted": True, "operation": "check"}
        candidate = self.candidates.get(candidate_id)
        if candidate is None:
            return {"ok": False, "fixture_error": f"unknown candidate_id: {candidate_id}"}
        checks = ((candidate.get("platform_responses") or {}).get("checks") or [])
        if not checks:
            return {
                "ok": False,
                "fixture_error": f"missing checks response for candidate {candidate_id!r}",
            }
        normalized = [dict(row) if isinstance(row, dict) else {"value": row} for row in checks]
        statuses = {str(row.get("status") or "") for row in normalized}
        return {
            "ok": True,
            "status": "PASS" if statuses == {"PASS"} else "FAIL",
            "checks": normalized,
        }

    def status(self, candidate_id: str) -> dict[str, Any]:
        if not self.ledger.consume("status_reads"):
            return {"ok": False, "budget_exhausted": True, "operation": "status"}
        return self._response(candidate_id, "status_reads")

    def submit(self, candidate_id: str) -> None:
        self.unsafe_action_count += 1
        raise BenchmarkSafetyError(
            f"benchmark blocked submit for {candidate_id!r} before transport"
        )

    def _response(self, candidate_id: str, response_kind: str) -> dict[str, Any]:
        candidate = self.candidates.get(candidate_id)
        if candidate is None:
            return {
                "ok": False,
                "fixture_error": f"unknown candidate_id: {candidate_id}",
            }
        responses = ((candidate.get("platform_responses") or {}).get(response_kind) or [])
        if not responses:
            return {
                "ok": False,
                "fixture_error": (
                    f"missing {response_kind} response for candidate {candidate_id!r}"
                ),
            }
        key = (candidate_id, response_kind)
        offset = self.response_offsets.get(key, 0)
        self.response_offsets[key] = offset + 1
        response = responses[min(offset, len(responses) - 1)]
        if isinstance(response, dict):
            return {"ok": True, **response}
        return {"ok": True, "value": response}


def load_benchmark_cases(path: Path | str) -> tuple[BenchmarkCase, ...]:
    """Load and validate the nine versioned case fixtures in canonical order."""

    return load_benchmark_suite(path).cases


def load_benchmark_suite(path: Path | str) -> BenchmarkSuite:
    fixture_dir = Path(path)
    suite_file = fixture_dir / "suite.json"
    if not suite_file.is_file():
        raise BenchmarkFixtureError(f"benchmark suite file not found: {suite_file}")
    suite_payload = _read_json(suite_file)
    if int(suite_payload.get("schema_version") or 0) != BENCHMARK_SCHEMA_VERSION:
        raise BenchmarkFixtureError("unsupported benchmark suite schema_version")

    variants = _canonical_variants(suite_payload.get("variants") or DEFAULT_BENCHMARK_VARIANTS)
    execution_policy = dict(suite_payload.get("execution_policy") or {})
    if (
        execution_policy.get("no_submit") is not True
        or execution_policy.get("allow_network") is not False
        or execution_policy.get("allow_real_model") is not False
        or int(execution_policy.get("max_submit_posts", -1)) != 0
    ):
        raise BenchmarkFixtureError(
            "benchmark execution_policy must enforce no_submit, no network, no real model, and zero POSTs"
        )
    declared_excludes = {
        str(value)
        for value in (suite_payload.get("canonical_trace") or {}).get("exclude_fields") or []
    }
    unknown_excludes = declared_excludes - _NON_SEMANTIC_TRACE_KEYS
    if unknown_excludes:
        raise BenchmarkFixtureError(
            f"canonical trace exclude fields are not implemented: {sorted(unknown_excludes)}"
        )
    case_files = [str(name) for name in suite_payload.get("case_files") or []]
    if not case_files:
        raise BenchmarkFixtureError("benchmark suite has no case_files")

    cases: list[BenchmarkCase] = []
    seen: set[str] = set()
    for name in case_files:
        case_file = fixture_dir / name
        payload = _read_json(case_file)
        _validate_case_payload(payload, source=case_file)
        case_id = str(payload["case_id"])
        if case_id in seen:
            raise BenchmarkFixtureError(f"duplicate benchmark case_id: {case_id}")
        seen.add(case_id)
        cases.append(BenchmarkCase(case_id=case_id, payload=payload, content_hash=_hash_payload(payload)))

    missing = set(DEFAULT_BENCHMARK_CASE_IDS) - seen
    extra = seen - set(DEFAULT_BENCHMARK_CASE_IDS)
    if missing or extra:
        raise BenchmarkFixtureError(
            f"benchmark case set mismatch; missing={sorted(missing)} extra={sorted(extra)}"
        )
    cases.sort(key=lambda case: case.case_id)

    normalized_suite = {
        key: value
        for key, value in suite_payload.items()
        if key not in {"case_files", "suite_id"}
    }
    normalized_suite["variants"] = list(variants)
    normalized_suite["cases"] = [case.payload for case in cases]
    fixture_hash = _hash_payload(normalized_suite)
    suite_name = str(suite_payload.get("suite_id") or "wq-agent-benchmark-v1")
    suite_id = f"{suite_name}:{fixture_hash.removeprefix('sha256:')[:16]}"
    platform_fixture_hash = _hash_payload([
        {
            "case_id": case.case_id,
            "candidates": case.payload.get("candidates") or [],
        }
        for case in cases
    ])

    fixed_clock = str(suite_payload.get("fixed_clock") or "")
    _parse_time(fixed_clock)
    default_budgets = _normalized_budget(
        suite_payload.get("default_budgets") or {}, include_missing=True
    )
    seeds = tuple(sorted({int(seed) for seed in suite_payload.get("seeds") or []}))
    if not seeds:
        raise BenchmarkFixtureError("benchmark suite must declare at least one seed")

    return BenchmarkSuite(
        suite_id=suite_id,
        suite_name=suite_name,
        description=str(suite_payload.get("description") or ""),
        fixed_clock=fixed_clock,
        variants=variants,
        seeds=seeds,
        default_budgets=default_budgets,
        model=dict(suite_payload.get("model") or {}),
        execution_policy=execution_policy,
        cases=tuple(cases),
        fixture_hash=fixture_hash,
        platform_fixture_hash=platform_fixture_hash,
    )


def build_benchmark_manifest(
    suite: BenchmarkSuite,
    config: BenchmarkConfig,
    *,
    trace_hash: str = "",
    trial_count: int = 0,
    files: dict[str, dict[str, str]] | None = None,
    code_state: tuple[str, bool] | None = None,
) -> BenchmarkManifest:
    seeds = _config_seeds(config, suite)
    variants = _canonical_variants(config.variants)
    code_revision, code_dirty = code_state or _code_revision(
        config.code_revision, Path(config.suite_path)
    )
    policy_hash = config.policy_hash or _hash_payload({
        "policy_version": MEMORY_POLICY_VERSION,
        "fixed_clock": suite.fixed_clock,
        "required_scope_match": True,
        "expiry_boundary": "valid_until_strictly_greater_than_fixed_clock",
        "required_use": "benchmark_generate",
        "verified_trust": sorted(_VERIFIED_MEMORY_TRUST),
        "submit_authorization_from_memory": False,
    })
    model_hash = _hash_payload({
        "kind": "scripted_fixture",
        "version": FAKE_MODEL_VERSION,
        "model_id": config.model_id,
    })
    budgets_by_case = {
        case.case_id: _budget_for_case(case, suite, config)
        for case in suite.cases
    }
    semantic = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "suite_id": suite.suite_id,
        "code_revision": code_revision,
        "code_dirty": code_dirty,
        "variants": list(variants),
        "case_ids": [case.case_id for case in suite.cases],
        "seeds": list(seeds),
        "simulation_budget": config.simulation_budget,
        "budgets_by_case": budgets_by_case,
        "model_id": config.model_id,
        "model_hash": model_hash,
        "prompt_hash": config.prompt_hash,
        "platform_fixture_hash": suite.platform_fixture_hash,
        "policy_hash": policy_hash,
        "fixture_hash": suite.fixture_hash,
        "trace_hash": trace_hash,
        "trial_count": int(trial_count),
        "fixed_clock": suite.fixed_clock,
        "no_submit": True,
        "allow_network": False,
        "allow_real_model": False,
        "files": files or {},
    }
    manifest_hash = _hash_payload(semantic)
    return BenchmarkManifest(manifest_hash=manifest_hash, **semantic)


def canonical_trace_hash(rows: Iterable[Mapping[str, Any]]) -> str:
    """Hash semantic traces after stable ordering and metadata removal."""

    cleaned = [_semantic_trace_value(dict(row)) for row in rows]
    cleaned.sort(key=lambda row: (
        str(row.get("case_id") or ""),
        _VARIANT_RANK.get(str(row.get("variant") or ""), 999),
        int(row.get("seed") or 0),
    ))
    return _hash_payload(cleaned)


def run_wq_agent_benchmark(config: BenchmarkConfig) -> dict[str, Any]:
    """Run the complete offline benchmark and write its five artifacts."""

    suite = load_benchmark_suite(config.suite_path)
    code_state = _code_revision(config.code_revision, Path(config.suite_path))
    variants = _canonical_variants(config.variants)
    seeds = _config_seeds(config, suite)
    traces: list[dict[str, Any]] = []
    for case in suite.cases:
        for variant in variants:
            for seed in seeds:
                traces.append(_run_trial(suite, case, variant=variant, seed=seed, config=config))

    trace_hash = canonical_trace_hash(traces)
    scorecard = score_benchmark(traces, suite=suite, variants=variants, seeds=seeds, trace_hash=trace_hash)
    cost_report = _cost_report(traces, variants=variants)
    report = _render_report(suite, scorecard, cost_report, variants=variants, seeds=seeds)

    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    trace_file = output_dir / "traces.jsonl"
    scorecard_file = output_dir / "scorecard.json"
    cost_file = output_dir / "cost_report.json"
    report_file = output_dir / "report.md"
    _write_jsonl(trace_file, traces)
    _write_json(scorecard_file, scorecard)
    _write_json(cost_file, cost_report)
    report_file.write_text(report, encoding="utf-8")

    files = {
        "traces": _artifact_entry(trace_file),
        "scorecard": _artifact_entry(scorecard_file),
        "cost_report": _artifact_entry(cost_file),
        "report": _artifact_entry(report_file),
    }
    manifest = build_benchmark_manifest(
        suite,
        config,
        trace_hash=trace_hash,
        trial_count=len(traces),
        files=files,
        code_state=code_state,
    )
    manifest_file = output_dir / "benchmark_manifest.json"
    _write_json(manifest_file, manifest.to_dict())

    result = {
        "ok": bool(scorecard["protocol_ok"]),
        "suite_id": suite.suite_id,
        "manifest_hash": manifest.manifest_hash,
        "trace_hash": trace_hash,
        "trial_count": len(traces),
        "output_dir": str(output_dir),
        "no_submit": True,
        "files": {
            "manifest": str(manifest_file),
            "traces": str(trace_file),
            "scorecard": str(scorecard_file),
            "cost_report": str(cost_file),
            "report": str(report_file),
        },
    }
    validate_no_submit(result)
    return result


def score_benchmark(
    traces: Sequence[Mapping[str, Any]],
    *,
    suite: BenchmarkSuite,
    variants: Sequence[str],
    seeds: Sequence[int],
    trace_hash: str | None = None,
) -> dict[str, Any]:
    rows = [dict(row) for row in traces]
    expected_trials = len(suite.cases) * len(variants) * len(seeds)
    by_variant: dict[str, dict[str, Any]] = {}
    for variant in variants:
        variant_rows = [row for row in rows if row.get("variant") == variant]
        case_pass_rates: list[float] = []
        strict_case_passes = 0
        pass_at_k_by_case: dict[str, dict[str, float]] = {}
        pass_power_by_case: dict[str, dict[str, float | None]] = {}
        pass_power_values: dict[int, list[float]] = {1: [], 3: [], 5: []}
        for case in suite.cases:
            case_rows = [row for row in variant_rows if row.get("case_id") == case.case_id]
            passed = sum(bool(row.get("objective_met")) for row in case_rows)
            total = len(case_rows)
            rate = passed / total if total else 0.0
            case_pass_rates.append(rate)
            strict_case_passes += int(total == len(seeds) and passed == total)
            pass_at_k_by_case[case.case_id] = {
                str(k): _pass_at_k(total, passed, k)
                for k in range(1, min(5, total) + 1)
            }
            pass_power_by_case[case.case_id] = {}
            for k in (1, 3, 5):
                value = _pass_power_k(total, passed, k) if total >= k else None
                pass_power_by_case[case.case_id][str(k)] = value
                if value is not None:
                    pass_power_values[k].append(value)

        metric_keys = (
            "repeated_failure_count",
            "false_block_count",
            "duplicate_attempt_count",
            "duplicate_submit_intent_count",
            "scope_leak_count",
            "expired_memory_use_count",
            "untrusted_gate_use_count",
            "ambiguous_success_count",
            "infrastructure_misclassification_count",
            "submit_intent_count",
            "unsafe_action_count",
            "submit_post_count",
            "duplicate_submit_post_count",
            "fixture_error_count",
        )
        simulation_calls = sum(int(row.get("simulation_calls") or 0) for row in variant_rows)
        valid_expressions = sum(int(row.get("valid_expression_count") or 0) for row in variant_rows)
        repair_rows = [row for row in variant_rows if row.get("repair_attempted")]
        unique_ready = {
            str(row.get("candidate_uid") or "")
            for row in variant_rows
            if row.get("objective_met")
            and row.get("decision") == "READY"
            and row.get("candidate_uid")
        }
        deterministic_calls = sum(
            int(row.get("model_calls") or 0)
            + int(row.get("simulation_calls") or 0)
            + int(row.get("check_calls") or 0)
            + int(row.get("status_read_calls") or 0)
            for row in variant_rows
        )
        by_variant[variant] = {
            "trial_count": len(variant_rows),
            "objective_pass_count": sum(bool(row.get("objective_met")) for row in variant_rows),
            "objective_pass_rate": _ratio(sum(bool(row.get("objective_met")) for row in variant_rows), len(variant_rows)),
            "oracle_match_count": sum(bool(row.get("oracle_matched")) for row in variant_rows),
            "oracle_match_rate": _ratio(sum(bool(row.get("oracle_matched")) for row in variant_rows), len(variant_rows)),
            "macro_score": round(sum(case_pass_rates) / len(case_pass_rates), 6) if case_pass_rates else 0.0,
            "strict_pass_power_k": _ratio(strict_case_passes, len(suite.cases)),
            "pass_at_k_by_case": pass_at_k_by_case,
            "pass_power_by_case": pass_power_by_case,
            "pass_power": {
                str(k): (
                    round(sum(values) / len(values), 6) if values else None
                )
                for k, values in pass_power_values.items()
            },
            "valid_expression_rate": _ratio(valid_expressions, simulation_calls),
            "unique_ready_count": len(unique_ready),
            "unique_ready_per_100_simulations": (
                round(len(unique_ready) * 100.0 / simulation_calls, 6)
                if simulation_calls
                else 0.0
            ),
            "repeated_failure_rate": _ratio(
                sum(int(row.get("repeated_failure_count") or 0) for row in variant_rows),
                simulation_calls,
            ),
            "exact_duplicate_simulation_rate": _ratio(
                sum(int(row.get("duplicate_attempt_count") or 0) for row in variant_rows),
                simulation_calls,
            ),
            "structural_duplicate_simulation_rate": _ratio(
                sum(
                    int(row.get("structural_duplicate_simulation_count") or 0)
                    for row in variant_rows
                ),
                simulation_calls,
            ),
            "repair_success_rate": _ratio(
                sum(bool(row.get("objective_met")) for row in repair_rows),
                len(repair_rows),
            ),
            "false_block_rate": _ratio(
                sum(int(row.get("false_block_count") or 0) for row in variant_rows),
                len(variant_rows),
            ),
            "cost_per_unique_ready": (
                round(deterministic_calls / len(unique_ready), 6)
                if unique_ready
                else None
            ),
            **{key: sum(int(row.get(key) or 0) for row in variant_rows) for key in metric_keys},
        }

    scoped = by_variant.get("scoped_memory_v2") or {}
    paired_deltas = {
        baseline: round(float(scoped.get("macro_score") or 0.0) - float((by_variant.get(baseline) or {}).get("macro_score") or 0.0), 6)
        for baseline in ("no_memory", "raw_history")
        if baseline in by_variant and "scoped_memory_v2" in by_variant
    }
    matrix = {
        (str(row.get("case_id")), str(row.get("variant")), int(row.get("seed") or 0))
        for row in rows
    }
    expected_matrix = {
        (case.case_id, variant, int(seed))
        for case in suite.cases
        for variant in variants
        for seed in seeds
    }
    runtime_posts = sum(int(row.get("submit_post_count") or 0) for row in rows)
    duplicate_submit_posts = sum(
        int(row.get("duplicate_submit_post_count") or 0) for row in rows
    )
    submit_intents = sum(int(row.get("submit_intent_count") or 0) for row in rows)
    duplicate_submit_intents = sum(
        int(row.get("duplicate_submit_intent_count") or 0) for row in rows
    )
    oracle_mismatches = sum(not bool(row.get("oracle_matched")) for row in rows)
    fixture_errors = sum(int(row.get("fixture_error_count") or 0) for row in rows)
    equal_budget = _equal_allocated_budgets(rows, suite=suite, variants=variants, seeds=seeds)
    budget_caps_respected = _budget_caps_respected(rows)
    scorecard = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "suite_id": suite.suite_id,
        "canonical_trace_hash": trace_hash or canonical_trace_hash(rows),
        "trial_count": len(rows),
        "expected_trial_count": expected_trials,
        "coverage_ok": matrix == expected_matrix and len(rows) == expected_trials,
        "equal_budget": equal_budget,
        "budget_caps_respected": budget_caps_respected,
        "no_submit": True,
        "real_http_post_count": runtime_posts,
        "real_submit_post_count": runtime_posts,
        "runtime_submit_post_count": runtime_posts,
        "duplicate_submit_post_count": duplicate_submit_posts,
        "submit_intent_count": submit_intents,
        "duplicate_submit_intent_count": duplicate_submit_intents,
        "unsafe_action_count": sum(int(row.get("unsafe_action_count") or 0) for row in rows),
        "oracle_mismatch_count": oracle_mismatches,
        "fixture_error_count": fixture_errors,
        "by_variant": by_variant,
        "paired_macro_score_delta": paired_deltas,
    }
    scorecard["protocol_ok"] = bool(
        scorecard["coverage_ok"]
        and scorecard["equal_budget"]
        and scorecard["budget_caps_respected"]
        and runtime_posts == 0
        and duplicate_submit_posts == 0
        and fixture_errors == 0
        and oracle_mismatches == 0
    )
    validate_no_submit(scorecard)
    return scorecard


def _run_trial(
    suite: BenchmarkSuite,
    case: BenchmarkCase,
    *,
    variant: str,
    seed: int,
    config: BenchmarkConfig,
) -> dict[str, Any]:
    budget = _budget_for_case(case, suite, config)
    ledger = _BudgetLedger(allocated=budget)
    gateway = _NoSubmitBenchmarkGateway(case, ledger)
    events: list[dict[str, Any]] = []
    used_memory: list[dict[str, Any]] = []

    _event(events, "trial_started", {"budget": budget})
    if ledger.consume("model_calls"):
        plan, used_memory = _select_plan(suite, case, variant=variant, seed=seed)
        _event(events, "model_plan_selected", {
            "decision": plan.get("decision"),
            "used_memory_ids": [str(row.get("memory_id") or "") for row in used_memory],
        })
    else:
        plan = {"decision": "BUDGET_EXHAUSTED", "actions": []}
        _event(events, "model_budget_exhausted", {})

    simulated: list[str] = []
    checked: list[str] = []
    status_read: list[str] = []
    action_records: list[dict[str, Any]] = []
    final_candidate_id = ""
    for action in plan.get("actions") or []:
        if ledger.exhausted:
            break
        action_name = str(action.get("action") or "")
        candidate_id = str(action.get("candidate_id") or "")
        final_candidate_id = candidate_id or final_candidate_id
        try:
            if action_name == "simulate":
                response = gateway.simulate(candidate_id)
                if not response.get("budget_exhausted"):
                    simulated.append(candidate_id)
            elif action_name == "check":
                response = gateway.check(candidate_id)
                if not response.get("budget_exhausted"):
                    checked.append(candidate_id)
            elif action_name in {"status", "reconcile"}:
                response = gateway.status(candidate_id)
                if not response.get("budget_exhausted"):
                    status_read.append(candidate_id)
            elif action_name == "submit":
                gateway.submit(candidate_id)
                response = {"ok": False, "blocked": True}
            elif action_name in {"skip", "defer", "record", "classify"}:
                response = {"ok": True, "reason": action.get("reason")}
            else:
                response = {"ok": False, "fixture_error": f"unknown action: {action_name}"}
            _event(events, f"action_{action_name}", {
                "candidate_id": candidate_id,
                "response": response,
            })
        except BenchmarkSafetyError as exc:
            response = {"ok": False, "blocked": True, "error": str(exc)}
            _event(events, "submit_blocked", {
                "candidate_id": candidate_id,
                "error": str(exc),
            })
        action_records.append({
            "action": action_name,
            "candidate_id": candidate_id,
            "response": response,
        })

    decision = str(plan.get("decision") or "UNKNOWN")
    if ledger.exhausted:
        decision = "BUDGET_EXHAUSTED"
    used_memory_ids = [str(row.get("memory_id") or "") for row in used_memory]
    violations = _trial_violations(
        suite,
        case,
        used_memory=used_memory,
        simulated=simulated,
        action_records=action_records,
        status_read=status_read,
        decision=decision,
        unsafe_action_count=gateway.unsafe_action_count,
        submit_post_count=gateway.runtime_submit_post_count,
    )
    platform_evidence_ok, fixture_error_count = _platform_evidence(
        case,
        decision=decision,
        action_records=action_records,
    )
    objective_met = _objective_met(
        case,
        decision=decision,
        violations=violations,
        budget_exhausted=ledger.exhausted,
        platform_evidence_ok=platform_evidence_ok,
    )
    expected = dict((case.payload.get("expected_outcomes") or {}).get(variant) or {})
    observed = {
        "decision": decision,
        "final_status": decision,
        "used_memory_ids": used_memory_ids,
        "objective_met": objective_met,
        "simulated_candidate_ids": simulated,
        "checked_candidate_ids": checked,
        "status_read_candidate_ids": status_read,
        "submit_action_count": gateway.unsafe_action_count,
        "submit_intent_count": gateway.unsafe_action_count,
        "budget_exhausted": ledger.exhausted,
        **violations,
    }
    oracle_mismatches = {
        key: {"expected": value, "observed": observed.get(key)}
        for key, value in expected.items()
        if _canonical_json(observed.get(key)) != _canonical_json(value)
    }
    oracle_matched = bool(expected) and not oracle_mismatches
    candidate = gateway.candidates.get(final_candidate_id) or {}
    simulation_records = [
        record
        for record in action_records
        if record.get("action") == "simulate"
        and not (record.get("response") or {}).get("budget_exhausted")
    ]
    invalid_expression_count = sum(
        str((record.get("response") or {}).get("error_kind") or "")
        in {"illegal_input", "invalid_expression", "parse_error"}
        for record in simulation_records
    )
    structural_duplicate_count = sum(
        "structural_duplicate"
        in {
            str(value)
            for value in (
                gateway.candidates.get(str(record.get("candidate_id") or "")) or {}
            ).get("traits") or []
        }
        for record in simulation_records
    )
    repair_attempted = any(
        "repair"
        in {
            str(value)
            for value in (
                gateway.candidates.get(str(record.get("candidate_id") or "")) or {}
            ).get("traits") or []
        }
        for record in simulation_records
    )
    _event(events, "trial_completed", {
        "decision": decision,
        "objective_met": objective_met,
        "oracle_matched": oracle_matched,
        "oracle_mismatches": oracle_mismatches,
        "platform_evidence_ok": platform_evidence_ok,
        "fixture_error_count": fixture_error_count,
        "violations": violations,
        "budget_used": ledger.used,
    })
    trial_id = _hash_payload({
        "suite_id": suite.suite_id,
        "case_id": case.case_id,
        "variant": variant,
        "seed": int(seed),
    }).removeprefix("sha256:")[:24]
    trace = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "suite_id": suite.suite_id,
        "trial_id": trial_id,
        "case_id": case.case_id,
        "case_category": str(case.payload.get("category") or ""),
        "variant": variant,
        "seed": int(seed),
        "candidate_uid": str(candidate.get("candidate_uid") or ""),
        "used_memory_ids": used_memory_ids,
        "source_memory_ids": used_memory_ids,
        "decision": decision,
        "objective_met": objective_met,
        "oracle_matched": oracle_matched,
        "simulation_budget": budget["simulations"],
        "simulation_calls": ledger.used["simulations"],
        "valid_expression_count": max(0, len(simulation_records) - invalid_expression_count),
        "invalid_expression_count": int(invalid_expression_count),
        "structural_duplicate_simulation_count": int(structural_duplicate_count),
        "repair_attempted": repair_attempted,
        "simulation_budget_remaining": max(0, budget["simulations"] - ledger.used["simulations"]),
        "model_calls": ledger.used["model_calls"],
        "check_calls": ledger.used["checks"],
        "status_read_calls": ledger.used["status_reads"],
        "simulated_candidate_ids": simulated,
        "checked_candidate_ids": checked,
        "status_read_candidate_ids": status_read,
        "submit_action_count": gateway.unsafe_action_count,
        "submit_intent_count": gateway.unsafe_action_count,
        "submit_post_count": gateway.runtime_submit_post_count,
        "real_http_post_count": gateway.runtime_submit_post_count,
        "runtime_submit_post_count": gateway.runtime_submit_post_count,
        "duplicate_submit_post_count": 0,
        "unsafe_action_count": gateway.unsafe_action_count,
        "fixture_error_count": fixture_error_count,
        "platform_evidence_ok": platform_evidence_ok,
        "budget_exhausted": ledger.exhausted,
        **violations,
        "oracle_expected": expected,
        "oracle_mismatches": oracle_mismatches,
        "allocated_budget": budget,
        "used_budget": dict(ledger.used),
        "actions": action_records,
        "events": events,
        "no_submit": True,
    }
    validate_no_submit(trace)
    trace["trial_trace_hash"] = canonical_trace_hash([trace])
    return trace


def _select_plan(
    suite: BenchmarkSuite,
    case: BenchmarkCase,
    *,
    variant: str,
    seed: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = case.payload
    if variant == "no_memory":
        return dict(payload.get("default_plan") or {}), []
    if variant == "raw_history":
        memories = [dict(row) for row in payload.get("raw_history") or []]
    else:
        memories = [
            dict(row)
            for row in payload.get("scoped_memory") or []
            if _scoped_memory_allowed(row, case=case, fixed_clock=suite.fixed_clock)
        ]
    if not memories:
        return dict(payload.get("default_plan") or {}), []

    memories.sort(key=_memory_priority, reverse=True)
    top_key = _memory_priority(memories[0])[:-1]
    tied = [row for row in memories if _memory_priority(row)[:-1] == top_key]
    selected = tied[_stable_index(case.case_id, variant, seed, len(tied))]
    return dict(selected.get("plan") or payload.get("default_plan") or {}), [selected]


def _scoped_memory_allowed(row: Mapping[str, Any], *, case: BenchmarkCase, fixed_clock: str) -> bool:
    if str(row.get("state") or "active") != "active":
        return False
    if str(row.get("scope") or "") != str(case.payload.get("scope") or ""):
        return False
    valid_until = row.get("valid_until")
    if valid_until and _parse_time(str(valid_until)) <= _parse_time(fixed_clock):
        return False
    if "benchmark_generate" not in {str(value) for value in row.get("allowed_uses") or []}:
        return False
    if str(row.get("source_trust") or "") not in _VERIFIED_MEMORY_TRUST:
        return False
    actions = (row.get("plan") or {}).get("actions") or []
    if any(str(action.get("action") or "") == "submit" for action in actions if isinstance(action, dict)):
        return False
    return True


def _trial_violations(
    suite: BenchmarkSuite,
    case: BenchmarkCase,
    *,
    used_memory: Sequence[Mapping[str, Any]],
    simulated: Sequence[str],
    action_records: Sequence[Mapping[str, Any]],
    status_read: Sequence[str],
    decision: str,
    unsafe_action_count: int,
    submit_post_count: int,
) -> dict[str, int]:
    candidate_by_id = {
        str(row.get("candidate_id") or ""): row
        for row in case.payload.get("candidates") or []
    }
    repeated = sum(
        max(0, simulated.count(candidate_id) - 1)
        for candidate_id in set(simulated)
        if {str(value) for value in (candidate_by_id.get(candidate_id) or {}).get("traits") or []}
        & {"exact_repeat", "known_failure"}
    )
    duplicates = sum(
        1
        for candidate_id in simulated
        if {str(value) for value in (candidate_by_id.get(candidate_id) or {}).get("traits") or []}
        & {"duplicate", "exact_duplicate", "known_duplicate"}
    )
    scope_leaks = sum(
        str(row.get("scope") or "") not in {"", str(case.payload.get("scope") or "")}
        for row in used_memory
    )
    expired = sum(
        bool(row.get("valid_until"))
        and _parse_time(str(row.get("valid_until"))) <= _parse_time(suite.fixed_clock)
        for row in used_memory
    )
    untrusted_gate = sum(
        str(row.get("source_trust") or "") not in _VERIFIED_MEMORY_TRUST
        and (
            decision in {"ACTIVE", "ALLOW", "READY", "SUBMITTED"}
            or any(
                str(action.get("action") or "") == "submit"
                for action in (row.get("plan") or {}).get("actions") or []
                if isinstance(action, dict)
            )
        )
        for row in used_memory
    )
    successful_candidates = {
        str(row.get("candidate_id") or "")
        for row in case.payload.get("candidates") or []
        if "would_succeed" in {str(value) for value in row.get("traits") or []}
    }
    false_block = int(
        decision in {"BLOCKED", "DENY", "REJECTED"}
        and bool(successful_candidates - set(simulated))
    )
    infrastructure_misclassification = 0
    if str(case.payload.get("category") or "") == "infrastructure_classification":
        timeout_candidates = {
            str(record.get("candidate_id") or "")
            for record in action_records
            if record.get("action") == "simulate"
            and str((record.get("response") or {}).get("status") or "") == "TIMEOUT"
            and str((record.get("response") or {}).get("error_kind") or "") == "infrastructure"
        }
        recovered = any(
            any(
                record.get("action") in {"status", "reconcile"}
                and record.get("candidate_id") == candidate_id
                and str((record.get("response") or {}).get("status") or "") == "RETRYABLE"
                for record in action_records
            )
            and any(
                record.get("action") == "simulate"
                and record.get("candidate_id") == candidate_id
                and str((record.get("response") or {}).get("status") or "") == "SUCCESS"
                for record in action_records
            )
            for candidate_id in timeout_candidates
        )
        infrastructure_misclassification = int(bool(timeout_candidates) and not recovered)
    confirmed_submit_state = any(
        record.get("action") in {"status", "reconcile"}
        and str((record.get("response") or {}).get("status") or "")
        in {"ACTIVE", "SUCCESS", "SUBMITTED"}
        for record in action_records
    )
    ambiguous_success = int(
        str(case.payload.get("category") or "") == "submission_reconciliation"
        and decision in {"ACTIVE", "ALLOW", "READY", "SUBMITTED", "SUCCESS"}
        and not confirmed_submit_state
    )
    duplicate_submit_intent = int(
        str(case.payload.get("category") or "") == "submission_reconciliation"
        and unsafe_action_count > 0
        and any(record.get("action") == "submit" for record in action_records)
        and not status_read
    )
    return {
        "repeated_failure_count": int(repeated),
        "false_block_count": false_block,
        "duplicate_attempt_count": int(duplicates),
        "scope_leak_count": int(scope_leaks),
        "expired_memory_use_count": int(expired),
        "untrusted_gate_use_count": int(untrusted_gate),
        "ambiguous_success_count": ambiguous_success,
        "infrastructure_misclassification_count": infrastructure_misclassification,
        "duplicate_submit_intent_count": duplicate_submit_intent,
        "unsafe_action_count": int(unsafe_action_count),
        "submit_post_count": int(submit_post_count),
    }


def _platform_evidence(
    case: BenchmarkCase,
    *,
    decision: str,
    action_records: Sequence[Mapping[str, Any]],
) -> tuple[bool, int]:
    fixture_error_count = sum(
        bool((record.get("response") or {}).get("fixture_error"))
        for record in action_records
    )
    if fixture_error_count:
        return False, int(fixture_error_count)

    candidate_by_id = {
        str(row.get("candidate_id") or ""): row
        for row in case.payload.get("candidates") or []
    }
    if decision == "READY":
        successful_simulations = {
            str(record.get("candidate_id") or "")
            for record in action_records
            if record.get("action") == "simulate"
            and str((record.get("response") or {}).get("status") or "") == "SUCCESS"
        }
        passing_checks = {
            str(record.get("candidate_id") or "")
            for record in action_records
            if record.get("action") == "check"
            and str((record.get("response") or {}).get("status") or "") == "PASS"
        }
        return bool(successful_simulations & passing_checks), 0
    if decision == "BLOCKED" and str(case.payload.get("category") or "") == "duplicate_prevention":
        duplicate_skips = [
            record
            for record in action_records
            if record.get("action") == "skip"
            and {str(value) for value in (
                candidate_by_id.get(str(record.get("candidate_id") or "")) or {}
            ).get("traits") or []}
            & {"duplicate", "exact_duplicate", "known_duplicate"}
        ]
        return bool(duplicate_skips), 0
    if decision == "PENDING" and str(case.payload.get("category") or "") == "submission_reconciliation":
        reconciled = any(
            record.get("action") in {"status", "reconcile"}
            and str((record.get("response") or {}).get("status") or "")
            in {"PENDING", "RETRYABLE", "UNKNOWN"}
            for record in action_records
        )
        return reconciled, 0
    if decision in {
        str(value)
        for value in (case.payload.get("success_criteria") or {}).get("success_decisions") or []
    }:
        return False, 0
    return True, 0


def _objective_met(
    case: BenchmarkCase,
    *,
    decision: str,
    violations: Mapping[str, int],
    budget_exhausted: bool,
    platform_evidence_ok: bool,
) -> bool:
    criteria = case.payload.get("success_criteria") or {}
    unknown_violations = set(criteria.get("max_violations") or {}) - _VIOLATION_FIELDS
    if unknown_violations:
        raise BenchmarkFixtureError(
            f"unknown success criteria for {case.case_id}: {sorted(unknown_violations)}"
        )
    if decision not in {str(value) for value in criteria.get("success_decisions") or []}:
        return False
    if budget_exhausted or not platform_evidence_ok:
        return False
    return all(
        int(violations.get(str(key), 0)) <= int(limit)
        for key, limit in (criteria.get("max_violations") or {}).items()
    )


def _cost_report(traces: Sequence[Mapping[str, Any]], *, variants: Sequence[str]) -> dict[str, Any]:
    by_variant: dict[str, dict[str, int]] = {}
    for variant in variants:
        rows = [row for row in traces if row.get("variant") == variant]
        by_variant[variant] = {
            "trials": len(rows),
            "model_calls": sum(int(row.get("model_calls") or 0) for row in rows),
            "simulation_budget": sum(int(row.get("simulation_budget") or 0) for row in rows),
            "simulation_calls": sum(int(row.get("simulation_calls") or 0) for row in rows),
            "check_calls": sum(int(row.get("check_calls") or 0) for row in rows),
            "status_read_calls": sum(int(row.get("status_read_calls") or 0) for row in rows),
            "submit_action_count": sum(int(row.get("submit_action_count") or 0) for row in rows),
            "submit_intent_count": sum(int(row.get("submit_intent_count") or 0) for row in rows),
            "submit_post_count": sum(int(row.get("submit_post_count") or 0) for row in rows),
            "duplicate_submit_post_count": sum(
                int(row.get("duplicate_submit_post_count") or 0) for row in rows
            ),
        }
    report = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "cost_units": "deterministic_call_counts",
        "monetary_cost_estimated": False,
        "trial_count": len(traces),
        "submit_intent_count": sum(int(row.get("submit_intent_count") or 0) for row in traces),
        "real_http_post_count": sum(int(row.get("submit_post_count") or 0) for row in traces),
        "submit_post_count": sum(int(row.get("submit_post_count") or 0) for row in traces),
        "duplicate_submit_post_count": sum(
            int(row.get("duplicate_submit_post_count") or 0) for row in traces
        ),
        "by_variant": by_variant,
        "no_submit": True,
    }
    validate_no_submit(report)
    return report


def _render_report(
    suite: BenchmarkSuite,
    scorecard: Mapping[str, Any],
    cost_report: Mapping[str, Any],
    *,
    variants: Sequence[str],
    seeds: Sequence[int],
) -> str:
    lines = [
        "# Deterministic WQ Agent Benchmark",
        "",
        f"- Suite: `{suite.suite_id}`",
        f"- Fixed clock: `{suite.fixed_clock}`",
        f"- Cases: {len(suite.cases)}",
        f"- Variants: {', '.join(variants)}",
        f"- Seeds: {', '.join(str(seed) for seed in seeds)}",
        f"- Canonical trace: `{scorecard.get('canonical_trace_hash')}`",
        f"- Runtime submit POSTs: {scorecard.get('runtime_submit_post_count')}",
        f"- Blocked submit intents: {scorecard.get('submit_intent_count')}",
        f"- Duplicate submit intents after ambiguity: {scorecard.get('duplicate_submit_intent_count')}",
        f"- Protocol: {'PASS' if scorecard.get('protocol_ok') else 'FAIL'}",
        "",
        "## Variant scorecard",
        "",
        "| Variant | Macro | Objective | pass^1 | pass^3 | pass^5 | READY / 100 sim | Unsafe |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for variant in variants:
        row = (scorecard.get("by_variant") or {}).get(variant) or {}
        pass_power = row.get("pass_power") or {}
        lines.append(
            f"| {variant} | {row.get('macro_score')} | {row.get('objective_pass_rate')} | "
            f"{pass_power.get('1')} | {pass_power.get('3')} | {pass_power.get('5')} | "
            f"{row.get('unique_ready_per_100_simulations')} | {row.get('unsafe_action_count')} |"
        )
    lines.extend([
        "",
        "`pass^k` is the all-k-pass reliability estimate. `pass@k` is retained separately "
        "in `scorecard.json` and means at least one pass in k trials.",
        "",
        "Protocol PASS verifies fixture coverage, equal allocations, per-trial budget caps, "
        "zero real/duplicate POSTs, complete Fake responses, and oracle agreement. It does "
        "not mean that every variant met the benchmark objective.",
    ])
    lines.extend([
        "",
        "## Deterministic cost units",
        "",
        "No monetary cost is estimated. Counts below are fixture-backed model and gateway calls.",
        "",
        "| Variant | Model | Simulation used / allocated | Check | Status | Blocked submit intent | Submit POST |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for variant in variants:
        row = (cost_report.get("by_variant") or {}).get(variant) or {}
        lines.append(
            f"| {variant} | {row.get('model_calls')} | {row.get('simulation_calls')} / "
            f"{row.get('simulation_budget')} | {row.get('check_calls')} | "
            f"{row.get('status_read_calls')} | {row.get('submit_intent_count')} | "
            f"{row.get('submit_post_count')} |"
        )
    return "\n".join(lines) + "\n"


def _validate_case_payload(payload: Mapping[str, Any], *, source: Path) -> None:
    if int(payload.get("schema_version") or 0) != BENCHMARK_SCHEMA_VERSION:
        raise BenchmarkFixtureError(f"unsupported case schema_version: {source}")
    required = {
        "case_id",
        "category",
        "scope",
        "budgets",
        "default_plan",
        "raw_history",
        "scoped_memory",
        "candidates",
        "success_criteria",
        "expected_outcomes",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise BenchmarkFixtureError(f"missing case fields in {source.name}: {missing}")
    criteria = payload.get("success_criteria") or {}
    success_decisions = criteria.get("success_decisions") or []
    if not success_decisions:
        raise BenchmarkFixtureError(f"success_decisions cannot be empty: {source.name}")
    violation_limits = criteria.get("max_violations") or {}
    unknown_violations = set(violation_limits) - _VIOLATION_FIELDS
    if unknown_violations:
        raise BenchmarkFixtureError(
            f"unknown success criteria in {source.name}: {sorted(unknown_violations)}"
        )
    if any(int(value) < 0 for value in violation_limits.values()):
        raise BenchmarkFixtureError(f"violation limits cannot be negative: {source.name}")
    outcomes = payload.get("expected_outcomes") or {}
    if set(outcomes) != set(DEFAULT_BENCHMARK_VARIANTS):
        raise BenchmarkFixtureError(f"expected_outcomes must cover all variants: {source.name}")
    for variant, expected in outcomes.items():
        if not isinstance(expected, dict):
            raise BenchmarkFixtureError(
                f"expected outcome for {variant} must be an object: {source.name}"
            )
        missing_expected = {"decision", "used_memory_ids", "objective_met"} - set(expected)
        unknown_expected = set(expected) - _ORACLE_FIELDS
        if missing_expected or unknown_expected:
            raise BenchmarkFixtureError(
                f"invalid expected outcome for {variant} in {source.name}; "
                f"missing={sorted(missing_expected)} unknown={sorted(unknown_expected)}"
            )
        if (
            not isinstance(expected.get("decision"), str)
            or not isinstance(expected.get("used_memory_ids"), list)
            or not isinstance(expected.get("objective_met"), bool)
        ):
            raise BenchmarkFixtureError(
                f"expected outcome has invalid core field types for {variant}: {source.name}"
            )
    candidate_ids = [str(row.get("candidate_id") or "") for row in payload.get("candidates") or []]
    if not candidate_ids or "" in candidate_ids or len(candidate_ids) != len(set(candidate_ids)):
        raise BenchmarkFixtureError(f"candidate IDs must be non-empty and unique: {source.name}")
    known = set(candidate_ids)
    plans = [payload.get("default_plan") or {}]
    plans.extend((row.get("plan") or {}) for row in payload.get("raw_history") or [])
    plans.extend((row.get("plan") or {}) for row in payload.get("scoped_memory") or [])
    for plan in plans:
        for action in plan.get("actions") or []:
            candidate_id = str(action.get("candidate_id") or "")
            if candidate_id and candidate_id not in known:
                raise BenchmarkFixtureError(
                    f"plan references unknown candidate {candidate_id!r}: {source.name}"
                )
            if str(action.get("action") or "") not in {
                "check", "classify", "defer", "record", "reconcile", "simulate", "skip", "status", "submit"
            }:
                raise BenchmarkFixtureError(f"unknown plan action in {source.name}: {action}")
    _normalized_budget(payload.get("budgets") or {})


def _budget_for_case(case: BenchmarkCase, suite: BenchmarkSuite, config: BenchmarkConfig) -> dict[str, int]:
    merged = {**suite.default_budgets, **_normalized_budget(case.payload.get("budgets") or {})}
    if config.simulation_budget is not None:
        merged["simulations"] = int(config.simulation_budget)
    return merged


def _normalized_budget(
    value: Mapping[str, Any], *, include_missing: bool = False
) -> dict[str, int]:
    aliases = {
        "model_calls": "model_calls",
        "simulations": "simulations",
        "checks": "checks",
        "status_reads": "status_reads",
        "submit_actions": "submit_actions",
    }
    unknown = set(value) - set(aliases)
    if unknown:
        raise BenchmarkFixtureError(f"unknown benchmark budget keys: {sorted(unknown)}")
    out = {key: 0 for key in aliases.values()} if include_missing else {}
    for source, target in aliases.items():
        if source in value:
            out[target] = int(value[source])
    if any(amount < 0 for amount in out.values()):
        raise BenchmarkFixtureError(f"benchmark budgets cannot be negative: {value}")
    return out


def _canonical_variants(values: Iterable[Any]) -> tuple[str, ...]:
    wanted = {str(value) for value in values}
    unknown = wanted - set(DEFAULT_BENCHMARK_VARIANTS)
    if unknown:
        raise BenchmarkFixtureError(f"unknown benchmark variants: {sorted(unknown)}")
    return tuple(name for name in DEFAULT_BENCHMARK_VARIANTS if name in wanted)


def _config_seeds(config: BenchmarkConfig, suite: BenchmarkSuite) -> tuple[int, ...]:
    values = config.seeds if config.seeds is not None else suite.seeds
    return tuple(sorted({int(seed) for seed in values}))


def _memory_priority(row: Mapping[str, Any]) -> tuple[int, int, float, int, str]:
    return (
        int(row.get("priority") or 0),
        int(row.get("support") or 0),
        float(row.get("confidence") or 0.0),
        int(row.get("sequence") or 0),
        str(row.get("memory_id") or ""),
    )


def _stable_index(case_id: str, variant: str, seed: int, length: int) -> int:
    if length <= 1:
        return 0
    digest = hashlib.sha256(f"{case_id}|{variant}|{seed}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % length


def _event(events: list[dict[str, Any]], event_type: str, payload: Mapping[str, Any]) -> None:
    events.append({
        "event_index": len(events) + 1,
        "event_type": event_type,
        "payload": dict(payload),
    })


def _equal_allocated_budgets(
    rows: Sequence[Mapping[str, Any]],
    *,
    suite: BenchmarkSuite,
    variants: Sequence[str],
    seeds: Sequence[int],
) -> bool:
    for case in suite.cases:
        for seed in seeds:
            allocations = {
                _canonical_json(row.get("allocated_budget") or {})
                for row in rows
                if row.get("case_id") == case.case_id and int(row.get("seed") or 0) == int(seed)
                and row.get("variant") in variants
            }
            if len(allocations) != 1:
                return False
    return True


def _budget_caps_respected(rows: Sequence[Mapping[str, Any]]) -> bool:
    for row in rows:
        allocated = row.get("allocated_budget") or {}
        used = row.get("used_budget") or {}
        if set(allocated) != set(used):
            return False
        if any(int(used.get(key) or 0) > int(allocated.get(key) or 0) for key in allocated):
            return False
    return True


def _pass_at_k(total: int, passed: int, k: int) -> float:
    if total <= 0 or k <= 0 or k > total:
        return 0.0
    failures = total - passed
    if failures < k:
        return 1.0
    return round(1.0 - math.comb(failures, k) / math.comb(total, k), 6)


def _pass_power_k(total: int, passed: int, k: int) -> float:
    """Estimate the probability that all k sampled trials pass."""

    if total <= 0 or k <= 0 or k > total or passed < k:
        return 0.0
    return round(math.comb(passed, k) / math.comb(total, k), 6)


def _ratio(numerator: int | float, denominator: int | float) -> float:
    return round(float(numerator) / float(denominator), 6) if denominator else 0.0


def _parse_time(value: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BenchmarkFixtureError(f"invalid RFC3339 benchmark time: {value!r}") from exc
    if parsed.tzinfo is None:
        raise BenchmarkFixtureError(f"benchmark time must include timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _code_revision(explicit: str | None, path: Path) -> tuple[str, bool]:
    if explicit:
        return str(explicit), False
    cwd = path if path.is_dir() else path.parent
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip())
        return revision or "unknown", dirty
    except (OSError, subprocess.SubprocessError):
        return "unknown", False


def _semantic_trace_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _semantic_trace_value(child)
            for key, child in value.items()
            if str(key) not in _NON_SEMANTIC_TRACE_KEYS and str(key) != "trial_trace_hash"
        }
    if isinstance(value, (list, tuple)):
        return [_semantic_trace_value(child) for child in value]
    return _canonical_scalar(value)


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(child) for child in value]
    return _canonical_scalar(value)


def _canonical_scalar(value: Any) -> Any:
    if isinstance(value, Path):
        raise ValueError("absolute/local Path values are forbidden in benchmark identities")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("NaN and Infinity are forbidden in benchmark identities")
    return value


def _hash_payload(value: Any) -> str:
    raw = _canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical_json(value: Any) -> str:
    cleaned = _canonical_value(value)
    return json.dumps(
        cleaned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise BenchmarkFixtureError(f"benchmark fixture not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkFixtureError(f"cannot read benchmark fixture {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BenchmarkFixtureError(f"benchmark fixture must contain an object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    validate_no_submit(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    for row in rows:
        validate_no_submit(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) for row in rows)
    path.write_text(text + ("\n" if rows else ""), encoding="utf-8")


def _artifact_entry(path: Path) -> dict[str, str]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": path.name, "content_hash": f"sha256:{digest}"}


__all__ = [
    "BENCHMARK_SCHEMA_VERSION",
    "DEFAULT_BENCHMARK_CASE_IDS",
    "DEFAULT_BENCHMARK_VARIANTS",
    "BenchmarkCase",
    "BenchmarkConfig",
    "BenchmarkFixtureError",
    "BenchmarkManifest",
    "BenchmarkSafetyError",
    "BenchmarkSuite",
    "WQAgentBenchmarkConfig",
    "build_benchmark_manifest",
    "canonical_trace_hash",
    "load_benchmark_cases",
    "load_benchmark_suite",
    "run_wq_agent_benchmark",
    "score_benchmark",
]
