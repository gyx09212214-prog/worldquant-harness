"""Lightweight Alpha-GPT workflow contracts for dry-run research artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

REVIEW_PROMOTE = "promote_to_review"
REVIEW_RETRY = "retry_with_mutation"
REVIEW_REJECT = "reject_with_memory"
REVIEW_HOLD = "hold_for_human"


@dataclass(frozen=True)
class PlaceholderTemplateSpec:
    template_id: str
    placeholder_template: str
    placeholder_bindings: dict[str, Any]
    research_intent: str
    source_family: str
    review_hint: str = REVIEW_PROMOTE
    risk_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "template_id": self.template_id,
            "placeholder_template": self.placeholder_template,
            "placeholder_bindings": dict(self.placeholder_bindings),
            "research_intent": self.research_intent,
            "source_family": self.source_family,
            "review_hint": self.review_hint,
            "risk_flags": list(self.risk_flags),
            "no_submit": True,
        }


def default_placeholder_specs(*, include_negative_fixture: bool = True) -> list[PlaceholderTemplateSpec]:
    """Return deterministic placeholder specs for the public dry-run loop."""

    specs = [
        PlaceholderTemplateSpec(
            template_id="price_reversal_rank",
            placeholder_template="rank(ts_rank(DATA_FIELD1, WINDOW1) - ts_rank(DATA_FIELD2, WINDOW2))",
            placeholder_bindings={
                "DATA_FIELD1": "close",
                "DATA_FIELD2": "returns",
                "WINDOW1": 20,
                "WINDOW2": 5,
            },
            research_intent="Test a slow price reversal rank against recent returns.",
            source_family="price_reversal",
            review_hint=REVIEW_PROMOTE,
        ),
        PlaceholderTemplateSpec(
            template_id="liquidity_correlation_retry",
            placeholder_template="rank(ts_corr(DATA_FIELD1, DATA_FIELD2, WINDOW1))",
            placeholder_bindings={"DATA_FIELD1": "vwap", "DATA_FIELD2": "volume", "WINDOW1": 10},
            research_intent="Probe price-volume participation; mark for budgeted near-miss retry in dry-run review.",
            source_family="price_volume_correlation",
            review_hint=REVIEW_RETRY,
            risk_flags=["near_miss_retry_fixture"],
        ),
        PlaceholderTemplateSpec(
            template_id="volume_reversal_rank",
            placeholder_template="rank(ts_rank(DATA_FIELD1, WINDOW1) - ts_rank(DATA_FIELD2, WINDOW2))",
            placeholder_bindings={
                "DATA_FIELD1": "vwap",
                "DATA_FIELD2": "volume",
                "WINDOW1": 20,
                "WINDOW2": 10,
            },
            research_intent="Test a liquidity reversal variant with different field exposure.",
            source_family="liquidity_reversal",
            review_hint=REVIEW_PROMOTE,
        ),
    ]
    if include_negative_fixture:
        specs.append(
            PlaceholderTemplateSpec(
                template_id="illegal_field_memory_fixture",
                placeholder_template="rank(DATA_FIELD1)",
                placeholder_bindings={"DATA_FIELD1": "not_a_real_field"},
                research_intent="Exercise illegal-field rejection and memory writing in the public dry run.",
                source_family="validation_fixture",
                review_hint=REVIEW_REJECT,
                risk_flags=["negative_fixture", "illegal_field"],
            )
        )
    return specs


def review_decision_for_validation(row: dict[str, Any]) -> tuple[str, str, str, bool]:
    """Map local validation into Alpha-GPT review decision fields."""

    if not row.get("ok"):
        reason = str(row.get("primary_error_code") or "local_validation_failed")
        return REVIEW_REJECT, reason, "write_memory_delta", False
    if str(row.get("review_hint") or "") == REVIEW_RETRY:
        return REVIEW_RETRY, "dry-run near-miss fixture requires budgeted mutation", "create_budgeted_repair_candidate", False
    if str(row.get("review_hint") or "") == REVIEW_HOLD:
        return REVIEW_HOLD, "human review requested by generation policy", "human_review", True
    return REVIEW_PROMOTE, "local validation passed; promote to review queue", "human_select_explicit_submit_id", True
