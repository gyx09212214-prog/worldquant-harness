# WQ Legal Inputs

This registry makes the mining loop explicit about legal WorldQuant BRAIN
inputs. It is offline by design: discovery is a separate authenticated command,
and candidate validation never calls WQ.

## Build The Registry

First run the existing discovery script when you want to refresh field
availability:

```powershell
python scripts/wq_discover_fields.py `
  --regions USA `
  --universes TOP3000 `
  --delays 1 `
  --limit 200 `
  --output reports/wq_available_fields_<date>.json
```

Then compile the sanitized registry:

```powershell
python scripts/wq_legal_inputs.py compile `
  --discover-file reports/wq_available_fields_<date>.json `
  --output configs/wq_legal_inputs.default.json `
  --account primary
```

The raw discovery file can contain account profile data under `user`. Do not
commit it. The compiled registry omits that block and keeps only dataset,
field, type, coverage, and operator metadata needed for validation.

## Validate Candidates

```powershell
python scripts/wq_legal_inputs.py validate-file `
  --registry configs/wq_legal_inputs.default.json `
  --candidate-file reports/wq_agent_runs/<run>/candidate_pool.jsonl `
  --region USA `
  --universe TOP3000 `
  --delay 1
```

Strict validation rejects:

- unknown or unavailable fields for the selected account/region/universe/delay
- forbidden fields such as `short_interest` and `short_ratio`
- forbidden or local-only operators such as `pasteurize`
- VECTOR fields used without `vec_*` or vector neutralization operators
- malformed candidate objects or unsupported `simulation_settings` keys

Use `--no-strict` only for auditing a candidate file without blocking unknown
fields.

## Use In Mining

Pass the compiled registry into the local planner:

```powershell
python scripts/wq_research_miner.py generate `
  --output reports/wq_agent_runs/<candidate_file>.jsonl `
  --run-dirs reports/wq_agent_runs/<prior_run> `
  --legal-inputs configs/wq_legal_inputs.default.json
```

Pass the same registry into presubmit:

```powershell
python scripts/wq_agent_workflow.py presubmit-sequential `
  --output-dir reports/wq_agent_runs/<run_id> `
  --candidate-files reports/wq_agent_runs/<candidate_file>.jsonl `
  --legal-inputs configs/wq_legal_inputs.default.json `
  --target-ready 4 `
  --max-total-simulations 40
```

Or use it through the research sandbox:

```powershell
python scripts/wq_research_sandbox.py mine `
  --experiment reports/wq_research_experiments/<exp-id> `
  --run-dirs reports/wq_agent_runs/<prior-run> `
  --legal-inputs configs/wq_legal_inputs.default.json
```

When enabled, illegal inputs are rejected before simulation and appear in
miner summaries, presubmit `candidate_skip.skip_reasons`, and harness metrics
as `illegal_field`, `illegal_operator`, `illegal_field_type`,
`illegal_candidate_schema`, or `unavailable_dataset_field`.

The research miner also uses field coverage metadata for a concentrated-weight
pre-screen. Candidates are rejected as `concentration_sparse_group_risk` when
they combine multiple sparse legs, such as `enterprise_value`, `cashflow_op`,
dividend fields, or `pcr_*`, with group transforms. A single sparse main leg is
allowed only when the expression also includes a broad high-coverage
price-volume or model-field dispersion leg. For `pcr_*` specifically, live
round22 showed that model-field overlays alone were not enough; require an
explicit price-volume dispersion leg such as `volume`, `vwap`, `adv20`, `close`,
`open`, `high`, or `low`.
