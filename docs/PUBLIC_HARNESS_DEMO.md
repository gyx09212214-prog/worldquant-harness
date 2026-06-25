# Public Harness Demo

This demo runs the worldquant-harness WQ research harness without WQ BRAIN, DeepSeek, Wind, or any private credentials.

It creates a guarded sandbox experiment, runs `presubmit-sequential` with fake platform/simulation/check adapters, applies the sandbox gate, evaluates harness metrics, and creates the next-generation child experiment.

```powershell
python scripts/run_public_harness_demo.py --output-root reports/public_harness_demo
python scripts/validate_public_harness_artifacts.py reports/public_harness_demo
python scripts/run_public_harness_eval.py --output-root reports/public_harness_eval
python scripts/wq_submit_efficiency_report.py `
  --run-roots reports/public_harness_demo `
  --current-name public-demo `
  --output reports/public_harness_demo/efficiency_summary.json `
  --markdown-output reports/public_harness_demo/efficiency_summary.md `
  --events-output reports/public_harness_demo/efficiency_events.jsonl
python scripts/wq_alpha_quality_review.py `
  --reports reports/public_harness_demo `
  --no-platform `
  --no-profile-candidate `
  --output-dir reports/public_harness_demo/quality_review
python scripts/build_public_visual_pack.py `
  --source reports/public_harness_demo `
  --output-dir docs/images `
  --report docs/VISUAL_GUIDE.md
```

The command writes:

- `inputs/demo_legal_inputs.json`
- `experiments/<exp-id>/candidate_specs.jsonl`
- `experiments/<exp-id>/presubmit_run/`
- `experiments/<exp-id>/critic_report.yaml`
- `experiments/<exp-id>/decision.yaml`
- `experiments/<exp-id>/evaluations/public-harness-demo/eval_summary.json`
- `experiments/<exp-id>/evaluations/public-harness-demo/run_report.md`
- `demo_summary.json`
- `reports/public_harness_eval/harness_run.json`
- `reports/public_harness_eval/agent_trace.jsonl`
- `reports/public_harness_eval/eval_cases.jsonl`
- `reports/public_harness_eval/memory_delta.jsonl`
- `reports/public_harness_eval/profile_patch.json`
- `efficiency_summary.md` and `efficiency_events.jsonl`
- `quality_review/summary.json`, `quality_review.md`, and `recommended_directions.json`
- `docs/VISUAL_GUIDE.md`
- `docs/images/worldquant-harness-overview.svg`
- `docs/images/public-demo-trace.svg`
- `docs/images/memory-feedback-graph.svg`
- `docs/images/factor-map-snapshot.svg`
- `docs/images/quality-review-dashboard.svg`
- `docs/images/profile-evolution-timeline.svg`

## Visual Output

The public demo visual pack gives reviewers a quick path through the harness mechanics:

![Public demo trace](images/public-demo-trace.svg)

![Artifact lifecycle](images/harness-artifact-lifecycle.svg)

The trace image is generated from demo artifacts. The lifecycle image is a static
public-safe explanation of the same artifact contract.

Supporting boundary visuals are also included for README and release review:

- `docs/images/submit-boundary.svg`
- `docs/images/release-safety-boundary.svg`

The fixture intentionally contains:

- one ready candidate,
- one strict self-correlation rejection,
- one repairable near miss,
- one illegal-input candidate rejected by the local legal-input registry,
- one duplicate active expression rejected by the virtual similarity gate.

The demo never calls a real submit endpoint. It is meant for public documentation, screenshots, and harness regression checks.

`run_public_harness_eval.py` wraps the same deterministic path as an agent contract suite. It asserts the ready candidate, strict self-correlation rejection, illegal-field rejection, active duplicate rejection, no-submit boundary, and profile patch non-application.

The visual pack is generated from the same artifacts. It avoids absolute local
paths and is safe to publish as static GitHub documentation.

For artifact meanings and the `harness_score` formula, see
[HARNESS_ARTIFACTS_AND_SCORE.md](HARNESS_ARTIFACTS_AND_SCORE.md). For the
agent contract files, see [AGENT_HARNESS_CONTRACT.md](AGENT_HARNESS_CONTRACT.md).
For submit boundaries and credential handling, see
[SECURITY_AND_LIMITATIONS.md](SECURITY_AND_LIMITATIONS.md).
