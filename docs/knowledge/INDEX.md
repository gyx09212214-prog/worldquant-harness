# Knowledge Base Index

Agent-driven factor research produces three types of knowledge entries.
Each entry is a standalone markdown file categorized by outcome.

> This is a sample of the knowledge base structure. The full knowledge base (40+ entries) is built up over multiple research sessions and is not included in the open-source release.

## Rules (Stable Rules)
Permanent platform constraints and validated operational rules.

- [SC Saturation Rule](rules/sc-saturation-rule.md) — Each operator family saturates at ~3-5 ACTIVEs before SC blocks further submissions

## Findings (Empirical Discoveries)
Sanitized signal-structure notes that show how a finding is recorded without
publishing private platform outputs.

- [Price Anchor Reversal Pattern](findings/vwap-decay-reversal.md) — price-anchor reversal plus orthogonal overlay, with private metrics removed

## Failures (Dead Ends)
Documented failed approaches — prevents re-exploring exhausted directions.

- [Pasteurize Unavailable](failures/pasteurize-unavailable.md) — pasteurize() operator not available on Gold tier

---

## Conventions

- **`[Agent+DS Consensus]`**: Both Claude and DeepSeek agree on the conclusion
- **`[Agent+DS Disagreement]`**: Views differ — both positions documented
- **One file per discovery**: Each entry gets its own markdown file
- **Date**: Include discovery date for time-sensitive findings
