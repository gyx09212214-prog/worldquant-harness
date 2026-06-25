"""Build reviewable knowledge snippets from WQ harness evaluations."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .wq_reference_catalog import reference_catalog_status


def build_wq_knowledge_snippet(
    eval_summary: dict[str, Any],
    *,
    catalog_status: dict[str, Any] | None = None,
) -> str:
    """Render a compact, reviewable knowledge snippet from an eval summary."""

    catalog = catalog_status or reference_catalog_status()
    metrics = eval_summary.get("metrics") or {}
    gate = eval_summary.get("gate") or {}
    summary = catalog.get("summary") or {}
    category_counts = summary.get("category_counts") or {}
    categories = ", ".join(f"{key}:{value}" for key, value in sorted(category_counts.items())) or "unavailable"
    lines = [
        "---",
        "tags:",
        "  - worldquant",
        "  - research-profile",
        "  - knowledge-evolution",
        f"created: {date.today().isoformat()}",
        "---",
        "",
        "# WQ Alpha Research Knowledge Update",
        "",
        "## Reference Catalog",
        "",
        f"- Source: {catalog.get('source_url')}",
        f"- Local path: `{catalog.get('reference_dir')}`",
        f"- Field count: {summary.get('field_count')}",
        f"- Categories: {categories}",
        "",
        "## Latest Harness Signals",
        "",
        f"- Eval: `{eval_summary.get('eval_id')}`",
        f"- Harness score: {eval_summary.get('harness_score')}",
        f"- Gate: {gate.get('decision')}",
        f"- Ready per 100 simulations: {metrics.get('ready_per_100_simulations')}",
        f"- Self-correlation rejects: {metrics.get('self_correlation_reject_share')}",
        f"- Too-similar rejects: {metrics.get('too_similar_reject_share')}",
        f"- Illegal-input rejects: {metrics.get('illegal_input_reject_share')}",
        f"- Duplicate field signatures: {metrics.get('duplicate_field_signature_count')}",
        "",
        "## Iteration Policy",
        "",
        "- Keep the bundled reference catalog as the source of candidate field discovery.",
        "- Prefer profile candidates that reduce self-correlation and similarity blockers before increasing budget.",
        "- Promote only after validation evidence improves under train/validation/test isolation; keep final test data isolated.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def load_eval_summary(path: Path | str) -> dict[str, Any]:
    """Load a harness eval summary JSON file."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid eval summary: {path}")
    return payload
