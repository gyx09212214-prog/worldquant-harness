# Price Anchor Reversal Pattern

This is a sanitized example of a knowledge-base finding. It keeps the lesson and
record format, but omits private platform metrics, alpha IDs, submission state,
and exact release-sensitive variants.

## Pattern

Use a short-to-medium window reversal term around a price anchor, then test a
single orthogonal overlay rather than stacking many sparse fields.

Public template:

```text
rank(ts_decay_linear(rank(price_anchor_relation), window))
```

## Useful Repairs

- Try one broad liquidity or valuation overlay at a time.
- Keep the neutralization and decay grid small enough that changes remain
  attributable.
- Treat near-threshold self-correlation as a structure problem, not only a
  parameter problem.

## Failure Modes

- Same-family decay changes tend to stay highly correlated.
- Multiple sparse fundamental legs can improve headline metrics while increasing
  concentration or self-correlation risk.
- Semantically similar accounting ratios may be effectively duplicate features
  in platform checks.
