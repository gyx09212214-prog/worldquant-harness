# Agent Roles

worldquant-harness treats an LLM as a research agent, not as a direct submitter. The harness owns state, gates, artifacts, and memory.

worldquant-harness 把 LLM 视为研究 agent。Harness 管状态、门禁、产物、记忆。

## Role Contract

| Role | Responsibility | Main Artifacts | Submit Boundary |
|:--|:--|:--|:--|
| `researcher` | Load context, propose hypotheses and candidate batches | `candidate_specs.jsonl`, source tags, rationale | no submit |
| `verifier` | Validate syntax, legal inputs, duplicates, eval cases | `eval_cases.jsonl`, `eval_result.json` | no submit |
| `simulator` | Run sandbox or fake/public simulation paths | `simulation_results.jsonl`, `presubmit_ready_sequential.jsonl` | no submit |
| `critic` | Apply gate rules and explain promote/hold/reject | `critic_report.yaml`, `decision.yaml`, `decisions.jsonl` | no submit |
| `reflector` | Turn results into memory deltas and profile candidates | `memory_delta.jsonl`, `profile_patch.json` | no submit |
| `submitter` | Hold the submit boundary and require explicit alpha IDs | `harness_run.json` step `submit_guard` | skipped in harness runs |

## State Flow

```text
context_loaded
  -> candidates_proposed
  -> candidates_validated
  -> presubmit_ran
  -> gate_reviewed
  -> evaluated
  -> reflected
  -> profile_candidate_written
```

The public eval runner writes this flow to `agent_trace.jsonl`. Each row has `run_id`, `event_type`, `role`, `step_id`, `payload`, and `created_at`.

公开 eval runner 把流程写入 `agent_trace.jsonl`。每行记录角色、步骤、事件、payload。

## Design Rules

- Harness runs default to `no_submit=true`.
- Public demo and public eval use synthetic fixtures and fake adapters.
- Real WQ BRAIN submit tools remain separate MCP commands.
- Profile changes are written as candidate patches. They are not applied to a live profile by the public eval runner.
- Memory deltas record what should be blocked, down-weighted, compressed, or absorbed in later runs.

## Why This Matters

The agent can make creative proposals, but every important state transition is replayable. This makes failures useful: a rejected candidate becomes memory; a gate decision becomes evidence; a profile change becomes a reviewable patch.

Agent 可以探索。每个状态变化可复盘。失败候选进入记忆。门禁结论保留证据。profile 修改先生成 patch。
