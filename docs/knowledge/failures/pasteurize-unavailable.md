# pasteurize() Operator Unavailable

Date: 2026-04-28

`pasteurize()` returns "inaccessible or unknown operator" on our WQ BRAIN account.
This is likely a premium-tier operator.

Workaround: Avoid divisions that could produce Inf/NaN, or use structure where WQ handles it internally (e.g., rank() already handles NaN).
