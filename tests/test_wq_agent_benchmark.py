import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from worldquant_harness.wq_agent_benchmark import (
    BenchmarkConfig,
    BenchmarkFixtureError,
    canonical_trace_hash,
    load_benchmark_suite,
    run_wq_agent_benchmark,
    score_benchmark,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "wq_agent_benchmark"
EXPECTED_VARIANTS = ("no_memory", "raw_history", "scoped_memory_v2")
EXPECTED_CASE_IDS = {
    "self_corr_repeat",
    "metric_near_miss",
    "concentration_repair",
    "infrastructure_timeout",
    "exact_duplicate",
    "scope_leak",
    "expired_memory",
    "community_memory_taint",
    "ambiguous_submit_resume",
}
EXPECTED_ARTIFACTS = {
    "benchmark_manifest.json",
    "traces.jsonl",
    "scorecard.json",
    "cost_report.json",
    "report.md",
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _copy_fixture_dir(tmp_path: Path, name: str) -> Path:
    copied = tmp_path / name
    shutil.copytree(FIXTURE_DIR, copied)
    return copied


def _trace_index(traces: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(trace["case_id"]), str(trace["variant"])): trace
        for trace in traces
    }


def _value(record: Any, key: str) -> Any:
    if isinstance(record, dict):
        return record[key]
    return getattr(record, key)


def _case_ids(suite: Any) -> set[str]:
    return {_value(case, "case_id") for case in _value(suite, "cases")}


def _config(output_dir: Path, *, prompt_hash: str = "sha256:test-prompt-v1") -> BenchmarkConfig:
    return BenchmarkConfig(
        suite_path=FIXTURE_DIR,
        output_dir=output_dir,
        seeds=(7,),
        simulation_budget=3,
        code_revision="test-revision",
        model_id="fake-model-v1",
        prompt_hash=prompt_hash,
    )


def test_suite_defines_three_variants_and_all_nine_cases() -> None:
    suite = load_benchmark_suite(FIXTURE_DIR)

    assert tuple(_value(suite, "variants")) == EXPECTED_VARIANTS
    assert _case_ids(suite) == EXPECTED_CASE_IDS
    assert len(_value(suite, "cases")) == 9


def test_fixture_case_order_does_not_change_suite_identity(tmp_path: Path) -> None:
    reordered = _copy_fixture_dir(tmp_path, "reordered-suite")
    suite_file = reordered / "suite.json"
    payload = _read_json(suite_file)
    payload["case_files"] = list(reversed(payload["case_files"]))
    _write_json(suite_file, payload)

    original_suite = load_benchmark_suite(FIXTURE_DIR)
    reordered_suite = load_benchmark_suite(reordered)

    assert _value(reordered_suite, "suite_id") == _value(original_suite, "suite_id")
    assert _case_ids(reordered_suite) == _case_ids(original_suite)


def test_semantic_fixture_change_changes_suite_identity(tmp_path: Path) -> None:
    changed = _copy_fixture_dir(tmp_path, "changed-suite")
    case_file = changed / "self_corr_repeat.json"
    payload = _read_json(case_file)
    payload["candidates"][0]["platform_responses"]["simulations"][0]["duration_ms"] = 123
    _write_json(case_file, payload)

    original_suite = load_benchmark_suite(FIXTURE_DIR)
    changed_suite = load_benchmark_suite(changed)

    assert _value(changed_suite, "suite_id") != _value(original_suite, "suite_id")
    assert _value(changed_suite, "fixture_hash") != _value(original_suite, "fixture_hash")


def test_repeated_runs_have_the_same_canonical_trace_hash(tmp_path: Path) -> None:
    first = run_wq_agent_benchmark(_config(tmp_path / "first"))
    second = run_wq_agent_benchmark(_config(tmp_path / "second"))

    first_manifest = _read_json(Path(_value(first, "output_dir")) / "benchmark_manifest.json")
    second_manifest = _read_json(Path(_value(second, "output_dir")) / "benchmark_manifest.json")
    first_scorecard = _read_json(Path(_value(first, "output_dir")) / "scorecard.json")
    second_scorecard = _read_json(Path(_value(second, "output_dir")) / "scorecard.json")
    assert first_scorecard["canonical_trace_hash"] == second_scorecard["canonical_trace_hash"]
    assert _value(first, "trace_hash") == _value(second, "trace_hash")
    assert first_manifest["manifest_hash"] == second_manifest["manifest_hash"]


def test_all_fixture_oracles_match_all_27_case_variant_combinations(tmp_path: Path) -> None:
    suite = load_benchmark_suite(FIXTURE_DIR)
    result = run_wq_agent_benchmark(_config(tmp_path / "fixture-oracles"))
    traces = _read_jsonl(Path(_value(result, "output_dir")) / "traces.jsonl")
    indexed = _trace_index(traces)

    checked = 0
    for case in _value(suite, "cases"):
        case_id = str(_value(case, "case_id"))
        payload = _value(case, "payload")
        for variant, expected in payload["expected_outcomes"].items():
            trace = indexed[(case_id, variant)]
            assert trace["oracle_expected"] == expected
            assert trace["oracle_mismatches"] == {}
            assert trace["oracle_matched"] is True
            for key, expected_value in expected.items():
                assert trace[key] == expected_value, (case_id, variant, key)
            checked += 1

    assert checked == 27


def test_memory_and_platform_edge_cases_have_the_expected_semantics(tmp_path: Path) -> None:
    result = run_wq_agent_benchmark(_config(tmp_path / "edge-semantics"))
    traces = _read_jsonl(Path(_value(result, "output_dir")) / "traces.jsonl")
    indexed = _trace_index(traces)

    scope_raw = indexed[("scope_leak", "raw_history")]
    scope_scoped = indexed[("scope_leak", "scoped_memory_v2")]
    assert scope_raw["scope_leak_count"] == 1
    assert scope_scoped["scope_leak_count"] == 0

    expiry_raw = indexed[("expired_memory", "raw_history")]
    expiry_scoped = indexed[("expired_memory", "scoped_memory_v2")]
    assert expiry_raw["expired_memory_use_count"] == 1
    assert expiry_scoped["expired_memory_use_count"] == 0

    community_raw = indexed[("community_memory_taint", "raw_history")]
    community_scoped = indexed[("community_memory_taint", "scoped_memory_v2")]
    assert community_raw["untrusted_gate_use_count"] == 1
    assert community_raw["unsafe_action_count"] == 1
    assert community_scoped["untrusted_gate_use_count"] == 0
    assert community_scoped["unsafe_action_count"] == 0

    timeout_no_memory = indexed[("infrastructure_timeout", "no_memory")]
    timeout_raw = indexed[("infrastructure_timeout", "raw_history")]
    timeout_scoped = indexed[("infrastructure_timeout", "scoped_memory_v2")]
    assert timeout_no_memory["infrastructure_misclassification_count"] == 1
    assert timeout_raw["infrastructure_misclassification_count"] == 1
    assert timeout_scoped["infrastructure_misclassification_count"] == 0
    assert timeout_scoped["status_read_candidate_ids"] == ["timeout_candidate"]
    assert timeout_scoped["simulated_candidate_ids"] == [
        "timeout_candidate",
        "timeout_candidate",
    ]

    ambiguous_no_memory = indexed[("ambiguous_submit_resume", "no_memory")]
    ambiguous_raw = indexed[("ambiguous_submit_resume", "raw_history")]
    ambiguous_scoped = indexed[("ambiguous_submit_resume", "scoped_memory_v2")]
    assert ambiguous_no_memory["ambiguous_success_count"] == 1
    assert ambiguous_raw["duplicate_submit_intent_count"] == 1
    assert ambiguous_raw["submit_intent_count"] == 1
    assert ambiguous_raw["status_read_calls"] == 0
    assert ambiguous_scoped["duplicate_submit_intent_count"] == 0
    assert ambiguous_scoped["submit_intent_count"] == 0
    assert ambiguous_scoped["status_read_candidate_ids"] == ["resume_alpha_001"]
    assert all(
        trace["submit_post_count"] == 0
        for trace in (ambiguous_no_memory, ambiguous_raw, ambiguous_scoped)
    )

    duplicate_scoped = indexed[("exact_duplicate", "scoped_memory_v2")]
    assert duplicate_scoped["decision"] == "BLOCKED"
    assert duplicate_scoped["simulation_calls"] == 0
    assert duplicate_scoped["duplicate_attempt_count"] == 0
    assert duplicate_scoped["actions"][0]["action"] == "skip"


def test_all_variants_receive_equal_budgets_without_submit(tmp_path: Path, monkeypatch: Any) -> None:
    def fail_on_real_post(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("benchmark attempted a real HTTP POST")

    monkeypatch.setattr("requests.sessions.Session.post", fail_on_real_post)
    result = run_wq_agent_benchmark(_config(tmp_path / "benchmark"))
    output_dir = Path(_value(result, "output_dir"))
    manifest = _read_json(output_dir / "benchmark_manifest.json")
    traces = _read_jsonl(output_dir / "traces.jsonl")
    scorecard = _read_json(output_dir / "scorecard.json")
    cost_report = _read_json(output_dir / "cost_report.json")

    assert manifest["no_submit"] is True
    assert tuple(manifest["variants"]) == EXPECTED_VARIANTS
    assert len(traces) == len(EXPECTED_VARIANTS) * len(EXPECTED_CASE_IDS)
    assert {
        (trace["variant"], trace["case_id"], trace["seed"])
        for trace in traces
    } == {
        (variant, case_id, 7)
        for variant in EXPECTED_VARIANTS
        for case_id in EXPECTED_CASE_IDS
    }

    per_variant = cost_report["by_variant"]
    expected_calls = len(EXPECTED_CASE_IDS) * manifest["simulation_budget"]
    assert {
        per_variant[variant]["simulation_budget"]
        for variant in EXPECTED_VARIANTS
    } == {expected_calls}
    assert all(per_variant[variant]["simulation_calls"] <= expected_calls for variant in EXPECTED_VARIANTS)
    assert all(trace["simulation_calls"] <= manifest["simulation_budget"] for trace in traces)
    assert all(
        trace["simulation_budget_remaining"] == manifest["simulation_budget"] - trace["simulation_calls"]
        for trace in traces
    )

    tainted_raw_history = next(
        trace
        for trace in traces
        if trace["case_id"] == "community_memory_taint" and trace["variant"] == "raw_history"
    )
    assert tainted_raw_history["unsafe_action_count"] == 1
    assert tainted_raw_history["submit_post_count"] == 0
    assert scorecard["unsafe_action_count"] >= 1
    assert scorecard["duplicate_submit_post_count"] == 0
    assert cost_report["submit_post_count"] == 0
    assert all(trace["submit_post_count"] == 0 for trace in traces)


def test_simulation_budget_exhaustion_stops_additional_calls(tmp_path: Path) -> None:
    result = run_wq_agent_benchmark(
        BenchmarkConfig(
            suite_path=FIXTURE_DIR,
            output_dir=tmp_path / "limited",
            seeds=(7,),
            simulation_budget=1,
            code_revision="test-revision",
            model_id="fake-model-v1",
            prompt_hash="sha256:test-prompt-v1",
        )
    )
    traces = _read_jsonl(Path(_value(result, "output_dir")) / "traces.jsonl")
    trace = next(
        row
        for row in traces
        if row["case_id"] == "concentration_repair" and row["variant"] == "scoped_memory_v2"
    )

    assert trace["simulation_calls"] == 1
    assert trace["simulation_budget_remaining"] == 0
    assert trace["budget_exhausted"] is True


def test_manifest_hash_is_sensitive_to_prompt_identity(tmp_path: Path) -> None:
    first = run_wq_agent_benchmark(_config(tmp_path / "prompt-v1", prompt_hash="sha256:prompt-v1"))
    second = run_wq_agent_benchmark(_config(tmp_path / "prompt-v2", prompt_hash="sha256:prompt-v2"))

    first_manifest = _read_json(Path(_value(first, "output_dir")) / "benchmark_manifest.json")
    second_manifest = _read_json(Path(_value(second, "output_dir")) / "benchmark_manifest.json")

    assert first_manifest["prompt_hash"] == "sha256:prompt-v1"
    assert second_manifest["prompt_hash"] == "sha256:prompt-v2"
    assert first_manifest["manifest_hash"] != second_manifest["manifest_hash"]
    for key in ("code_revision", "platform_fixture_hash", "policy_hash"):
        assert first_manifest[key]


def test_run_writes_the_benchmark_artifact_contract(tmp_path: Path) -> None:
    result = run_wq_agent_benchmark(_config(tmp_path / "artifacts"))
    output_dir = Path(_value(result, "output_dir"))

    assert _value(result, "ok") is True
    assert {path.name for path in output_dir.iterdir() if path.is_file()} == EXPECTED_ARTIFACTS
    assert _read_json(output_dir / "benchmark_manifest.json")["schema_version"] == 1
    assert _read_json(output_dir / "scorecard.json")["schema_version"] == 1
    assert _read_json(output_dir / "cost_report.json")["schema_version"] == 1
    assert (output_dir / "report.md").read_text(encoding="utf-8").startswith("#")


def test_default_five_seed_run_has_135_trials(tmp_path: Path) -> None:
    result = run_wq_agent_benchmark(
        BenchmarkConfig(
            suite_path=FIXTURE_DIR,
            output_dir=tmp_path / "default-five-seeds",
            code_revision="test-revision",
            model_id="fake-model-v1",
            prompt_hash="sha256:test-prompt-v1",
        )
    )
    output_dir = Path(_value(result, "output_dir"))
    manifest = _read_json(output_dir / "benchmark_manifest.json")
    scorecard = _read_json(output_dir / "scorecard.json")
    traces = _read_jsonl(output_dir / "traces.jsonl")

    assert manifest["seeds"] == [7, 19, 43, 71, 101]
    assert manifest["trial_count"] == 135
    assert scorecard["expected_trial_count"] == 135
    assert scorecard["trial_count"] == 135
    assert len(traces) == 135
    assert _value(result, "trial_count") == 135


def test_canonical_trace_hash_has_strict_semantic_boundaries(tmp_path: Path) -> None:
    result = run_wq_agent_benchmark(_config(tmp_path / "canonical"))
    traces = _read_jsonl(Path(_value(result, "output_dir")) / "traces.jsonl")
    expected_hash = canonical_trace_hash(traces)

    assert expected_hash == _value(result, "trace_hash")
    assert canonical_trace_hash(reversed(traces)) == expected_hash
    assert all(
        canonical_trace_hash([trace]) == trace["trial_trace_hash"]
        for trace in traces
    )

    with_nonsemantic_metadata = copy.deepcopy(traces)
    for index, trace in enumerate(with_nonsemantic_metadata):
        trace.update(
            {
                "artifact_root": f"D:/arbitrary-output/{index}",
                "code_revision": f"nonsemantic-revision-{index}",
                "created_at": f"2030-01-01T00:00:{index:02d}Z",
                "duration_ms": index * 10,
                "finished_at": "2030-01-01T00:01:00Z",
                "output_dir": f"D:/another-root/{index}",
                "started_at": "2030-01-01T00:00:00Z",
                "wall_clock_ms": index * 100,
            }
        )
    assert canonical_trace_hash(with_nonsemantic_metadata) == expected_hash

    semantic_change = copy.deepcopy(traces)
    semantic_change[0]["decision"] = "SEMANTIC_MUTATION"
    assert canonical_trace_hash(semantic_change) != expected_hash


@pytest.mark.parametrize("invalid_value", [Path("local-only"), float("nan"), float("inf"), float("-inf")])
def test_canonical_trace_hash_rejects_paths_and_non_finite_numbers(
    invalid_value: Any,
) -> None:
    with pytest.raises(ValueError, match="Path|NaN|Infinity"):
        canonical_trace_hash(
            [
                {
                    "case_id": "case",
                    "variant": "no_memory",
                    "seed": 7,
                    "semantic_value": invalid_value,
                }
            ]
        )


def test_manifest_artifact_hashes_and_cost_report_reconcile(tmp_path: Path) -> None:
    result = run_wq_agent_benchmark(_config(tmp_path / "reconciliation"))
    output_dir = Path(_value(result, "output_dir"))
    manifest = _read_json(output_dir / "benchmark_manifest.json")
    scorecard = _read_json(output_dir / "scorecard.json")
    cost_report = _read_json(output_dir / "cost_report.json")
    traces = _read_jsonl(output_dir / "traces.jsonl")

    assert manifest["manifest_hash"] == _value(result, "manifest_hash")
    assert manifest["trace_hash"] == canonical_trace_hash(traces)
    assert scorecard["canonical_trace_hash"] == manifest["trace_hash"]
    assert manifest["trial_count"] == scorecard["trial_count"] == len(traces)
    for artifact in manifest["files"].values():
        artifact_path = output_dir / artifact["path"]
        assert artifact_path.is_file()
        digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        assert artifact["content_hash"] == f"sha256:{digest}"

    cost_fields = {
        "model_calls": "model_calls",
        "simulation_budget": "simulation_budget",
        "simulation_calls": "simulation_calls",
        "check_calls": "check_calls",
        "status_read_calls": "status_read_calls",
        "submit_action_count": "submit_action_count",
        "submit_post_count": "submit_post_count",
    }
    for variant in EXPECTED_VARIANTS:
        variant_traces = [trace for trace in traces if trace["variant"] == variant]
        variant_cost = cost_report["by_variant"][variant]
        assert variant_cost["trials"] == len(variant_traces)
        for report_key, trace_key in cost_fields.items():
            assert variant_cost[report_key] == sum(
                int(trace[trace_key]) for trace in variant_traces
            )
    assert cost_report["trial_count"] == len(traces)
    assert cost_report["submit_post_count"] == sum(
        int(trace["submit_post_count"]) for trace in traces
    )


def test_every_budget_dimension_stays_within_its_allocation(tmp_path: Path) -> None:
    result = run_wq_agent_benchmark(_config(tmp_path / "all-budget-caps"))
    output_dir = Path(_value(result, "output_dir"))
    traces = _read_jsonl(output_dir / "traces.jsonl")
    scorecard = _read_json(output_dir / "scorecard.json")

    for trace in traces:
        allocated = trace["allocated_budget"]
        used = trace["used_budget"]
        assert set(used) == set(allocated)
        assert all(int(used[key]) <= int(cap) for key, cap in allocated.items())

    for case_id in EXPECTED_CASE_IDS:
        allocations = {
            json.dumps(trace["allocated_budget"], sort_keys=True)
            for trace in traces
            if trace["case_id"] == case_id and trace["seed"] == 7
        }
        assert len(allocations) == 1
    assert scorecard["equal_budget"] is True
    assert scorecard["budget_caps_respected"] is True


def test_unknown_success_criterion_is_a_fixture_error(tmp_path: Path) -> None:
    malformed = _copy_fixture_dir(tmp_path, "unknown-criterion")
    case_file = malformed / "self_corr_repeat.json"
    payload = _read_json(case_file)
    payload["success_criteria"]["max_violations"]["unknown_violation"] = 0
    _write_json(case_file, payload)

    with pytest.raises(BenchmarkFixtureError, match="unknown success criteria"):
        load_benchmark_suite(malformed)


@pytest.mark.parametrize(
    ("policy_mutation", "message"),
    [
        ({"no_submit": False}, "execution_policy"),
        ({"allow_network": True}, "execution_policy"),
        ({"allow_real_model": True}, "execution_policy"),
        ({"max_submit_posts": 1}, "execution_policy"),
    ],
)
def test_unsafe_execution_policy_is_a_fixture_error(
    tmp_path: Path,
    policy_mutation: dict[str, Any],
    message: str,
) -> None:
    malformed = _copy_fixture_dir(tmp_path, "unsafe-policy")
    suite_file = malformed / "suite.json"
    payload = _read_json(suite_file)
    payload["execution_policy"].update(policy_mutation)
    _write_json(suite_file, payload)

    with pytest.raises(BenchmarkFixtureError, match=message):
        load_benchmark_suite(malformed)


def test_unknown_canonical_exclusion_policy_is_a_fixture_error(tmp_path: Path) -> None:
    malformed = _copy_fixture_dir(tmp_path, "unknown-canonical-policy")
    suite_file = malformed / "suite.json"
    payload = _read_json(suite_file)
    payload["canonical_trace"]["exclude_fields"].append("decision")
    _write_json(suite_file, payload)

    with pytest.raises(BenchmarkFixtureError, match="exclude fields are not implemented"):
        load_benchmark_suite(malformed)


def test_missing_platform_response_fails_protocol(tmp_path: Path) -> None:
    malformed = _copy_fixture_dir(tmp_path, "missing-platform-response")
    case_file = malformed / "metric_near_miss.json"
    payload = _read_json(case_file)
    candidate = next(
        row for row in payload["candidates"] if row["candidate_id"] == "near_miss_60"
    )
    candidate["platform_responses"]["checks"] = []
    _write_json(case_file, payload)

    result = run_wq_agent_benchmark(
        BenchmarkConfig(
            suite_path=malformed,
            output_dir=tmp_path / "missing-response-output",
            seeds=(7,),
            code_revision="test-revision",
        )
    )
    output_dir = Path(_value(result, "output_dir"))
    scorecard = _read_json(output_dir / "scorecard.json")
    traces = _read_jsonl(output_dir / "traces.jsonl")
    affected = [
        trace
        for trace in traces
        if trace["case_id"] == "metric_near_miss"
        and trace["variant"] in {"no_memory", "scoped_memory_v2"}
    ]

    assert _value(result, "ok") is False
    assert scorecard["protocol_ok"] is False
    assert scorecard["fixture_error_count"] == 2
    assert all(trace["fixture_error_count"] == 1 for trace in affected)
    assert all(trace["platform_evidence_ok"] is False for trace in affected)


def test_failed_platform_evidence_cannot_keep_ready_objective(tmp_path: Path) -> None:
    changed = _copy_fixture_dir(tmp_path, "failed-platform-evidence")
    case_file = changed / "metric_near_miss.json"
    payload = _read_json(case_file)
    candidate = next(
        row for row in payload["candidates"] if row["candidate_id"] == "near_miss_60"
    )
    candidate["platform_responses"]["checks"][0]["status"] = "FAIL"
    _write_json(case_file, payload)

    result = run_wq_agent_benchmark(
        BenchmarkConfig(
            suite_path=changed,
            output_dir=tmp_path / "failed-evidence-output",
            seeds=(7,),
            code_revision="test-revision",
        )
    )
    output_dir = Path(_value(result, "output_dir"))
    scorecard = _read_json(output_dir / "scorecard.json")
    traces = _read_jsonl(output_dir / "traces.jsonl")
    affected = [
        trace
        for trace in traces
        if trace["case_id"] == "metric_near_miss"
        and trace["variant"] in {"no_memory", "scoped_memory_v2"}
    ]

    assert _value(result, "ok") is False
    assert scorecard["fixture_error_count"] == 0
    assert scorecard["oracle_mismatch_count"] == 2
    assert all(trace["platform_evidence_ok"] is False for trace in affected)
    assert all(trace["objective_met"] is False for trace in affected)


def test_pass_power_one_three_five_is_distinct_from_pass_at_k() -> None:
    suite = load_benchmark_suite(FIXTURE_DIR)
    seeds = (7, 19, 43, 71, 101)
    rows = [
        {
            "case_id": "metric_near_miss",
            "variant": "no_memory",
            "seed": seed,
            "objective_met": index < 3,
            "oracle_matched": True,
            "allocated_budget": {"model_calls": 1},
            "used_budget": {"model_calls": 1},
        }
        for index, seed in enumerate(seeds)
    ]

    scorecard = score_benchmark(
        rows,
        suite=suite,
        variants=("no_memory",),
        seeds=seeds,
    )
    variant = scorecard["by_variant"]["no_memory"]
    pass_at_k = variant["pass_at_k_by_case"]["metric_near_miss"]
    pass_power = variant["pass_power_by_case"]["metric_near_miss"]

    assert pass_at_k == {"1": 0.6, "2": 0.9, "3": 1.0, "4": 1.0, "5": 1.0}
    assert pass_power == {"1": 0.6, "3": 0.1, "5": 0.0}
    assert variant["pass_power"] == {"1": 0.6, "3": 0.1, "5": 0.0}
    assert pass_at_k["3"] != pass_power["3"]
    assert pass_at_k["5"] != pass_power["5"]
