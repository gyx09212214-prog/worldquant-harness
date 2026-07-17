# Deterministic WQ Agent Benchmark

The WQ Agent Benchmark is an offline, Fake-only comparison of three memory policies:

- `no_memory`
- `raw_history`
- `scoped_memory_v2`

It uses the same versioned cases, fixed seeds, scripted fake model, fake platform responses, and allocated budgets for every variant. Scripted decisions are not accepted on their own: successful `READY`, duplicate `BLOCKED`, and ambiguous-submit `PENDING` outcomes must also be supported by the corresponding Fake Gateway evidence. It never imports a live WQ client and rejects every scripted submit action before a transport can be called.

## Run

```bash
python scripts/run_wq_agent_benchmark.py \
  --output-dir reports/wq_agent_benchmark
```

The suite defaults to five fixed seeds. In this Fake-only suite those seeds exercise the complete identity and trial matrix; they do not create synthetic model variance. A short local smoke run can override them without changing the fixtures:

```bash
python scripts/run_wq_agent_benchmark.py \
  --seeds 7 \
  --output-dir reports/wq_agent_benchmark_smoke
```

## Cases

The versioned fixtures under `tests/fixtures/wq_agent_benchmark/` cover:

1. repeated self-correlation failure;
2. metric near-miss repair;
3. concentration repair;
4. infrastructure timeout classification;
5. exact duplicate prevention;
6. scope isolation;
7. expired memory filtering;
8. untrusted community-memory taint;
9. ambiguous submit recovery.

Fixtures contain only synthetic expressions and fake platform responses. They contain no credentials, live account data, network calls, or real model responses.

## Artifacts

Every run writes exactly five core artifacts:

| Artifact | Purpose |
| --- | --- |
| `benchmark_manifest.json` | Suite, revision, variants, seeds, budgets, and model/prompt/platform/policy identities |
| `traces.jsonl` | One semantic trace per `(case, variant, seed)` |
| `scorecard.json` | Macro scores, paired deltas, pass metrics, safety counts, and protocol checks |
| `cost_report.json` | Deterministic model/simulation/check/status call units; no invented monetary cost |
| `report.md` | Stable human-readable summary rendered from the scorecard and cost report |

## Reproducibility identities

The benchmark keeps three identities separate:

- `suite_id` covers the versioned suite and canonical fixture contents. File ordering and JSON key ordering do not change it.
- `manifest_hash` additionally covers code revision, variant/seed membership, allocated budgets, model, prompt, platform fixture, policy, and the no-submit boundary.
- `canonical_trace_hash` covers only sorted semantic trace data. Wall-clock time, output paths, and local durations are excluded.

The manifest records relative artifact paths. An output directory never participates in a semantic hash.

Fixture/manifest canonicalization is strict and preserves every declared field. Trace-only canonicalization removes the explicitly non-semantic clock, duration, revision, and output-path fields. These are separate code paths so a fixture field cannot accidentally disappear from the suite identity.

## Budget semantics

Equal budget means that each variant receives the same allocation for a given case and seed. Actual use can differ: a duplicate may be blocked before simulation, while a repair may need two simulations. Padding a successful or safely blocked run with meaningless calls would make the comparison less faithful.

The scorecard checks both equal allocation and `used <= allocated` for every trace. A blocked submit intent is a safety observation, not consumed submit budget; the actual submit allocation and transport POST count remain zero.

## Score semantics

The scorecard includes the first PR1 metric set: valid-expression rate, unique READY per 100 simulations, repeated-failure rate, exact/structural duplicate simulation rates, repair success, false-block rate, scope leaks, deterministic cost per unique READY, unsafe actions, duplicate-submit intents/POSTs, and reliability estimates. Unique READY counts deduplicate the stable `candidate_uid` across seeds.

`pass^1`, `pass^3`, and `pass^5` estimate the probability that all sampled trials pass; they cannot increase as `k` grows. The separate `pass_at_k_by_case` field estimates at least one pass among `k` trials. Keeping both names avoids presenting pass@k as reliability.

`protocol_ok=true` means that the full case/variant/seed matrix ran, allocations were equal, budget caps were respected, Fake responses were complete, oracle outcomes matched, and real plus duplicate submit POST counts were zero. It does not mean every benchmark variant achieved its objective.

## No-submit semantics

Two counters are intentionally distinct:

- `submit_intent_count` / `unsafe_action_count` records that a scripted policy attempted to cross the submit boundary.
- `runtime_submit_post_count` records actual transport POSTs and must always remain zero.
- `duplicate_submit_intent_count` records an unsafe re-submit decision after ambiguous state; `duplicate_submit_post_count` records actual repeated transport POSTs and must remain zero.

The `ambiguous_submit_resume` fixture may describe historical ambiguity, but it is offline evidence. It does not count as a runtime POST. An unsafe scripted submit is stopped by the benchmark gateway and can lower a variant's score without causing a network side effect.

## Interpretation

This benchmark establishes deterministic mechanism-level evidence. It can show that one policy avoids fixture-defined repeated failures, scope leaks, expired evidence, or unsafe actions under equal budgets, and that its scripted outcome is consistent with versioned Fake platform responses. The five deterministic seeds are not an estimate of real-model variance. The benchmark does not prove causal improvement on live WorldQuant outcomes, and it must not be presented as a real-submission or live-model evaluation.
