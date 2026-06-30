# Alpha-GPT Harness Layer

worldquant-harness uses an Alpha-GPT-style research semantics layer on top of
the existing harness contract. The goal is not to add an autonomous submit bot.
It is to make each research run explainable from hypothesis to candidate,
review, reflection, and explicit submit evidence.

## Research Loop

```text
research brief
  -> hypothesis
  -> constrained candidate spec
  -> presubmit and review
  -> explicit submit evidence
  -> reflection and profile patch
```

The public path remains no-submit. Real WQ BRAIN submission is treated as a
terminal evidence source and requires explicit human-selected alpha IDs outside
the public eval runner.

## Semantic Artifacts

`scripts/run_public_harness_eval.py` writes the regular harness contract files
plus Alpha-GPT semantic artifacts:

| File | Purpose |
|:--|:--|
| `hypotheses.jsonl` | Structured research hypothesis for the run |
| `alpha_gpt_candidate_specs.jsonl` | Candidate specs linked to the hypothesis, placeholder template, bindings, and generation constraints |
| `review_decisions.jsonl` | Review outcomes such as `promote_to_review`, `retry_with_mutation`, and `reject_with_memory` |
| `reflection_records.jsonl` | Memory/profile lessons proposed after evaluation |
| `submit_evidence.json` | Explicit-submit boundary record; public eval records no real submit attempt |

These files are public-safe synthetic artifacts in the demo. They are meant to
show the intended lifecycle without exposing private alpha expressions or
platform credentials.

## Minimal Dry-Run Workflow

The smallest Alpha-GPT loop is available without a WQ account:

```bash
python scripts/wq_alpha_gpt_workflow.py demo --topic "analyst revision momentum"
```

It writes a public-safe bundle under `reports/examples/alpha_gpt_demo/`:

| File | Purpose |
|:--|:--|
| `hypotheses.jsonl` | Topic-specific research hypothesis |
| `placeholder_templates.jsonl` | Placeholder FASTEXPR templates and bindings |
| `candidate_specs.jsonl` | Rendered expressions linked to hypothesis and template |
| `local_validation.jsonl` | Parser/field validation results |
| `review_queue.jsonl` | `promote_to_review`, `retry_with_mutation`, or `reject_with_memory` decisions |
| `reflection_memory.jsonl` | Proposed failure-memory updates |
| `profile_patch.json` | Reviewable profile patch, not auto-applied |
| `submit_evidence.json` | Explicit-submit boundary record with no real submit attempt |

This workflow is intentionally small. It proves the Alpha-GPT vocabulary and
artifact lifecycle before connecting live simulation, MCP tools, or frontend
views.

## Design Boundary

- Alpha-GPT contributes the workflow language: hypothesis, constrained
  implementation, review, and feedback.
- worldquant-miner-style heuristics can be absorbed only through explicit,
  reviewable mechanisms such as placeholder generation, near-miss retry
  taxonomy, candidate pools, and field/operator constraints.
- Submit remains explicit. The harness can learn from post-submit evidence, but
  the public runner does not submit and profile patches are not auto-applied.
