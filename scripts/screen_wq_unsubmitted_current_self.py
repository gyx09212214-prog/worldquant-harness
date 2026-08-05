"""Read-only screen of current WQ UNSUBMITTED alphas with strict SELF evidence.

The only platform operations after authentication are paginated inventory GETs
and bounded check GETs for selected alphas.  This module has no live-submission
or persistence-database dependency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_auto_mining import load_dotenv
from worldquant_harness.wq_brain_client import get_client
from worldquant_harness.wq_expression_utils import expression_fields
from worldquant_harness.wq_review import parse_review_checks

SCHEMA_VERSION = "wq-current-self-screen/v1"
PRIVATE_ARTIFACT_TYPE = "wq_current_self_screen_candidate"
AUDIT_ARTIFACT_TYPE = "wq_current_self_screen_audit"
PUBLIC_CHECKPOINT_ARTIFACT_TYPE = "wq_current_self_screen_checkpoint"
PRIVATE_CHECKPOINT_ARTIFACT_TYPE = "wq_current_self_screen_private_checkpoint"
CHECKPOINT_SCHEMA_VERSION = "wq-current-self-screen-checkpoint/v1"
INVENTORY_PATH = "/users/self/alphas"
INVENTORY_DISPLAY_CAP = 1_000
_INVENTORY_GET_MAX_ATTEMPTS = 5
_INVENTORY_GET_RETRY_BASE_SECONDS = 1.0
_ROUND_ROBIN_RATE_LIMIT_BASE_SECONDS = 60.0
_ROUND_ROBIN_RATE_LIMIT_MAX_EXPONENTIAL_SECONDS = 900.0
_ROUND_ROBIN_RATE_LIMIT_MAX_SERVER_SECONDS = 86_400.0


@dataclass(frozen=True)
class ScreenConfig:
    output_private: Path
    output_audit: Path
    account: str = "primary"
    sharpe_min: float = 1.25
    fitness_min: float = 1.0
    turnover_max: float = 0.7
    self_corr_cutoff: float = 0.6
    max_checks: int = 40
    selection_offset: int = 0
    page_size: int = 100
    inventory_cap: int = INVENTORY_DISPLAY_CAP
    inventory_page_interval: float = 0.0
    check_polls: int = 1
    check_interval: float = 0.0
    between_checks: float = 0.0
    checkpoint: Path | None = None
    checkpoint_private: Path | None = None
    resume: bool = False
    round_robin_polling: bool = False
    prioritize_inventory_self: bool = False


def screen_current_self(
    client: Any,
    config: ScreenConfig,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Authenticate, perform the read-only screen, and atomically write artifacts."""
    _validate_config(config)
    authenticated = False
    try:
        authenticated = bool(client.authenticate())
        if not authenticated:
            raise RuntimeError("WQ authentication failed")

        inventory, inventory_get_calls = _fetch_full_inventory(
            client,
            page_size=config.page_size,
            display_cap=config.inventory_cap,
            page_interval=config.inventory_page_interval,
            sleep_fn=sleep_fn,
        )
        private_rows, checked_rows, screen_counts = _screen_inventory(
            client,
            inventory,
            config,
            sleep_fn=sleep_fn,
        )

        private_bytes = _jsonl_bytes(private_rows)
        _atomic_write(config.output_private, private_bytes)
        private_sha256 = hashlib.sha256(private_bytes).hexdigest()

        audit = _build_public_audit(
            config=config,
            inventory=inventory,
            checked_rows=checked_rows,
            private_rows=private_rows,
            screen_counts=screen_counts,
            inventory_get_calls=inventory_get_calls,
            private_sha256=private_sha256,
        )
        audit_bytes = _json_bytes(audit)
        _atomic_write(config.output_audit, audit_bytes)
        audit_sha256 = hashlib.sha256(audit_bytes).hexdigest()

        return {
            "ok": True,
            "read_only": True,
            "submit_calls": 0,
            "submit_attempts": 0,
            "counts": audit["counts"],
            "outputs": {
                "private": {
                    "path": str(config.output_private),
                    "sha256": private_sha256,
                    "records": len(private_rows),
                },
                "audit": {
                    "path": str(config.output_audit),
                    "sha256": audit_sha256,
                    "records": 1,
                },
            },
        }
    finally:
        if hasattr(client, "close"):
            client.close()


def _fetch_full_inventory(
    client: Any,
    *,
    page_size: int,
    display_cap: int = INVENTORY_DISPLAY_CAP,
    page_interval: float,
    sleep_fn: Callable[[float], None],
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    offset = 0
    get_calls = 0
    while True:
        payload: dict[str, Any] | None = None
        page: list[Any] | None = None
        for attempt in range(_INVENTORY_GET_MAX_ATTEMPTS):
            response = client.get_json(
                INVENTORY_PATH,
                params={"limit": page_size, "offset": offset, "order": "-dateCreated"},
            )
            get_calls += 1
            if isinstance(response, dict) and response.get("ok") is not False:
                response_page = response.get("results")
                if isinstance(response_page, list):
                    payload = response
                    page = response_page
                    break
            if attempt + 1 < _INVENTORY_GET_MAX_ATTEMPTS:
                retry_delay = max(
                    page_interval,
                    _INVENTORY_GET_RETRY_BASE_SECONDS * (2**attempt),
                )
                sleep_fn(retry_delay)
        if payload is None or page is None:
            raise RuntimeError(
                f"WQ inventory GET failed after {_INVENTORY_GET_MAX_ATTEMPTS} attempts at offset={offset}"
            )

        page_rows = [row for row in page if isinstance(row, dict)]
        rows.extend(page_rows)
        offset += len(page)
        if offset >= display_cap:
            rows = rows[:display_cap]
            break
        total = _integer(payload.get("count"))
        if total is not None and offset >= total:
            break
        if not page:
            break
        if len(page) < page_size and total is None:
            break
        if page_interval > 0:
            sleep_fn(page_interval)
    return rows, get_calls


def _screen_inventory(
    client: Any,
    inventory: list[dict[str, Any]],
    config: ScreenConfig,
    *,
    sleep_fn: Callable[[float], None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    active_field_sets = [
        set(expression_fields(expression))
        for row in inventory
        if _status(row) == "ACTIVE"
        if (expression := _expression(row))
    ]
    active_field_sets = [fields for fields in active_field_sets if fields]

    rejection_counts: Counter[str] = Counter()
    performance_rows: list[dict[str, Any]] = []
    for row in inventory:
        if _status(row) != "UNSUBMITTED":
            continue
        prepared, reason = _prepare_performance_candidate(row, active_field_sets, config)
        if prepared is None:
            rejection_counts[reason or "invalid_candidate"] += 1
            continue
        performance_rows.append(prepared)

    performance_rows.sort(
        key=lambda row: _candidate_sort_key(
            row,
            prioritize_inventory_self=config.prioritize_inventory_self,
        )
    )
    check_queue: list[dict[str, Any]] = []
    seen_expressions: set[str] = set()
    seen_signatures: set[str] = set()
    for row in performance_rows:
        expression_key = _canonical_expression(row["expression"])
        signature = str(row["field_signature"])
        if expression_key in seen_expressions:
            rejection_counts["duplicate_expression"] += 1
            continue
        if signature in seen_signatures:
            rejection_counts["duplicate_field_signature"] += 1
            continue
        seen_expressions.add(expression_key)
        seen_signatures.add(signature)
        check_queue.append(row)

    window_start = min(config.selection_offset, len(check_queue))
    window_end = min(window_start + config.max_checks, len(check_queue))
    selected = check_queue[window_start:window_end]
    if window_start:
        rejection_counts["selection_offset_skipped"] += window_start
    if window_end < len(check_queue):
        rejection_counts["max_checks_not_checked"] += len(check_queue) - window_end

    if config.round_robin_polling:
        accepted, public_checked, round_robin_counts = _screen_selected_round_robin(
            client,
            inventory=inventory,
            selected=selected,
            config=config,
            base_rejected=rejection_counts,
            sleep_fn=sleep_fn,
        )
        rejection_counts = Counter(round_robin_counts.pop("rejected"))
    else:
        accepted, public_checked = _screen_selected_serial(
            client,
            inventory=inventory,
            selected=selected,
            config=config,
            base_rejected=rejection_counts,
            sleep_fn=sleep_fn,
        )
        rejection_counts = Counter(rejection_counts)
        for checked_row in public_checked:
            reason = checked_row.get("rejection_reason")
            if reason:
                rejection_counts[str(reason)] += 1
        round_robin_counts = {}

    unique_inventory_ids = {
        str(row.get("id") or row.get("alpha_id") or "").strip()
        for row in inventory
        if str(row.get("id") or row.get("alpha_id") or "").strip()
    }
    counts = {
        "inventory_rows": len(inventory),
        "inventory_unique_ids": len(unique_inventory_ids),
        "active": sum(_status(row) == "ACTIVE" for row in inventory),
        "active_with_fields": len(active_field_sets),
        "unsubmitted": sum(_status(row) == "UNSUBMITTED" for row in inventory),
        "performance_pass_unsubmitted": len(performance_rows),
        "deduplicated_check_queue": len(check_queue),
        "checked": len(public_checked),
        "accepted_strict_self": len(accepted),
        "check_rate_limited": rejection_counts["self_rate_limited"],
        "check_pending": rejection_counts["self_pending"],
        "check_missing": rejection_counts["self_missing"],
        "check_nonnumeric": rejection_counts["self_nonnumeric"],
        "check_error": rejection_counts["self_error"],
        **round_robin_counts,
        "rejected": dict(sorted(rejection_counts.items())),
    }
    return accepted, public_checked, counts


def _screen_selected_serial(
    client: Any,
    *,
    inventory: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    config: ScreenConfig,
    base_rejected: Counter[str],
    sleep_fn: Callable[[float], None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    public_checked: list[dict[str, Any]] = []
    next_index = 0
    checkpoint_context: dict[str, Any] | None = None
    if config.checkpoint is not None:
        checkpoint_context = _build_checkpoint_context(
            config=config,
            inventory=inventory,
            selected=selected,
            base_rejected=base_rejected,
        )
        if config.resume:
            next_index, public_checked, accepted = _load_checkpoint_progress(
                config=config,
                context=checkpoint_context,
                selected=selected,
            )
        _write_checkpoints(
            config=config,
            context=checkpoint_context,
            next_index=next_index,
            public_checked=public_checked,
            accepted=accepted,
        )

    for index in range(next_index, len(selected)):
        candidate = selected[index]
        if index > 0 and config.between_checks > 0:
            sleep_fn(config.between_checks)
        alpha_id = str(candidate["alpha_id"])
        payload = client.check_alpha_submission(
            alpha_id,
            max_polls=config.check_polls,
            interval=config.check_interval,
        )
        current_self, check, reason = _current_self_evidence(
            payload,
            config.self_corr_cutoff,
            max_polls=config.check_polls,
            interval=config.check_interval,
        )
        public_row = _public_checked_row(candidate, current_self, accepted=reason is None, rejection_reason=reason)
        public_checked.append(public_row)
        if reason is None:
            accepted.append(_private_accepted_row(candidate, check=check, current_self=current_self))
        if checkpoint_context is not None:
            _write_checkpoints(
                config=config,
                context=checkpoint_context,
                next_index=index + 1,
                public_checked=public_checked,
                accepted=accepted,
            )
    return accepted, public_checked


_ROUND_ROBIN_RETRYABLE_REASONS = frozenset(
    {"self_error", "self_missing", "self_pending", "self_rate_limited"}
)


def _screen_selected_round_robin(
    client: Any,
    *,
    inventory: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    config: ScreenConfig,
    base_rejected: Counter[str],
    sleep_fn: Callable[[float], None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Poll one unresolved candidate at a time, with a durable per-GET budget reservation."""
    accepted: list[dict[str, Any]] = []
    public_checked: list[dict[str, Any]] = []
    state = _new_round_robin_state(len(selected))
    checkpoint_context: dict[str, Any] | None = None
    if config.checkpoint is not None:
        checkpoint_context = _build_checkpoint_context(
            config=config,
            inventory=inventory,
            selected=selected,
            base_rejected=base_rejected,
        )
        if config.resume:
            state, public_checked, accepted = _load_round_robin_checkpoint_progress(
                config=config,
                context=checkpoint_context,
                selected=selected,
            )
        _ensure_round_robin_rate_limit_state(state)
        _write_round_robin_checkpoints(
            config=config,
            context=checkpoint_context,
            state=state,
            public_checked=public_checked,
            accepted=accepted,
        )

    selected_order = {str(candidate["alpha_id"]): index for index, candidate in enumerate(selected)}
    resolved_ids = {str(row["alpha_id"]) for row in public_checked}
    while len(resolved_ids) < len(selected):
        round_index = int(state["round_index"])
        cursor = int(state["cursor"])
        while cursor < len(selected):
            cooldown_consumed = _consume_round_robin_rate_limit_cooldown(
                state=state,
                config=config,
                checkpoint_context=checkpoint_context,
                public_checked=public_checked,
                accepted=accepted,
                sleep_fn=sleep_fn,
            )
            candidate = selected[cursor]
            alpha_id = str(candidate["alpha_id"])
            candidate_index = cursor
            cursor += 1
            state["cursor"] = cursor
            if alpha_id in resolved_ids:
                continue

            attempts_reserved = state["attempts_reserved"]
            if attempts_reserved[candidate_index] >= config.check_polls:
                continue
            already_reserved_this_round = any(
                attempt == round_index + 1 for attempt in attempts_reserved
            )
            if already_reserved_this_round and config.between_checks > 0 and not cooldown_consumed:
                sleep_fn(config.between_checks)

            # Reserve before the network call.  A crash may consume a poll without
            # evidence, but resume will never silently exceed check_polls.
            attempts_reserved[candidate_index] += 1
            if checkpoint_context is not None:
                _write_round_robin_checkpoints(
                    config=config,
                    context=checkpoint_context,
                    state=state,
                    public_checked=public_checked,
                    accepted=accepted,
                )

            payload = client.check_alpha_submission(
                alpha_id,
                max_polls=1,
                interval=config.check_interval,
            )
            state["responses_recorded"][candidate_index] += 1
            current_self, check, reason = _current_self_evidence(
                payload,
                config.self_corr_cutoff,
                max_polls=config.check_polls,
                interval=config.check_interval,
            )
            if reason == "self_rate_limited":
                _record_round_robin_rate_limit(state, payload)
            else:
                _reset_round_robin_rate_limit_streak(state)
            attempt_count = int(attempts_reserved[candidate_index])
            check["polls"] = attempt_count
            if reason in _ROUND_ROBIN_RETRYABLE_REASONS and attempt_count < config.check_polls:
                if checkpoint_context is not None:
                    _write_round_robin_checkpoints(
                        config=config,
                        context=checkpoint_context,
                        state=state,
                        public_checked=public_checked,
                        accepted=accepted,
                    )
                continue

            public_checked.append(
                _public_checked_row(
                    candidate,
                    current_self,
                    accepted=reason is None,
                    rejection_reason=reason,
                    poll_attempts=attempt_count,
                )
            )
            resolved_ids.add(alpha_id)
            if reason is None:
                accepted.append(_private_accepted_row(candidate, check=check, current_self=current_self))
            _sort_round_robin_progress(public_checked, accepted, selected_order)
            if checkpoint_context is not None:
                _write_round_robin_checkpoints(
                    config=config,
                    context=checkpoint_context,
                    state=state,
                    public_checked=public_checked,
                    accepted=accepted,
                )

        if len(resolved_ids) == len(selected):
            break
        if round_index + 1 >= config.check_polls:
            for candidate_index, candidate in enumerate(selected):
                alpha_id = str(candidate["alpha_id"])
                if alpha_id in resolved_ids:
                    continue
                attempt_count = int(state["attempts_reserved"][candidate_index])
                current_self, check, reason = _uncheckpointed_poll_evidence(
                    config=config,
                    attempt_count=attempt_count,
                )
                public_checked.append(
                    _public_checked_row(
                        candidate,
                        current_self,
                        accepted=False,
                        rejection_reason=reason,
                        poll_attempts=attempt_count,
                    )
                )
                resolved_ids.add(alpha_id)
            _sort_round_robin_progress(public_checked, accepted, selected_order)
            if checkpoint_context is not None:
                _write_round_robin_checkpoints(
                    config=config,
                    context=checkpoint_context,
                    state=state,
                    public_checked=public_checked,
                    accepted=accepted,
                )
            break

        round_wait = max(config.check_interval, config.between_checks)
        if round_wait > 0:
            sleep_fn(round_wait)
        state["round_index"] = round_index + 1
        state["cursor"] = 0
        if checkpoint_context is not None:
            _write_round_robin_checkpoints(
                config=config,
                context=checkpoint_context,
                state=state,
                public_checked=public_checked,
                accepted=accepted,
            )

    rejection_counts = Counter(base_rejected)
    for checked_row in public_checked:
        reason = checked_row.get("rejection_reason")
        if reason:
            rejection_counts[str(reason)] += 1
    reserved = sum(int(value) for value in state["attempts_reserved"])
    recorded = sum(int(value) for value in state["responses_recorded"])
    rate_limit_state = _ensure_round_robin_rate_limit_state(state)
    return accepted, public_checked, {
        "check_get_reservations": reserved,
        "check_get_responses_recorded": recorded,
        "check_get_calls_exact": recorded if recorded == reserved else None,
        "check_rate_limit_responses": int(rate_limit_state["total_429"]),
        "check_rate_limit_consecutive": int(rate_limit_state["consecutive_429"]),
        "check_rate_limit_cooldown_seconds_pending": float(
            rate_limit_state["cooldown_seconds_pending"]
        ),
        "rejected": dict(sorted(rejection_counts.items())),
    }


def _new_round_robin_state(candidate_count: int) -> dict[str, Any]:
    return {
        "round_index": 0,
        "cursor": 0,
        "attempts_reserved": [0] * candidate_count,
        "responses_recorded": [0] * candidate_count,
        "rate_limit": _new_round_robin_rate_limit_state(),
    }


def _new_round_robin_rate_limit_state() -> dict[str, Any]:
    return {
        "consecutive_429": 0,
        "total_429": 0,
        "last_retry_after_seconds": None,
        "cooldown_seconds_pending": 0.0,
    }


def _ensure_round_robin_rate_limit_state(state: dict[str, Any]) -> dict[str, Any]:
    rate_limit = state.get("rate_limit")
    if rate_limit is None:
        # Legacy v1 checkpoints did not persist rate-limit state.  Add the
        # neutral state only after the old payload and progress hash validate.
        rate_limit = _new_round_robin_rate_limit_state()
        state["rate_limit"] = rate_limit
    if not isinstance(rate_limit, dict):  # pragma: no cover - validation owns this path
        raise ValueError("round-robin rate-limit state must be an object")
    return rate_limit


def _consume_round_robin_rate_limit_cooldown(
    *,
    state: dict[str, Any],
    config: ScreenConfig,
    checkpoint_context: dict[str, Any] | None,
    public_checked: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    sleep_fn: Callable[[float], None],
) -> bool:
    rate_limit = _ensure_round_robin_rate_limit_state(state)
    delay = float(rate_limit["cooldown_seconds_pending"])
    if delay <= 0:
        return False

    # The pending delay is checkpointed before this sleep.  If the process is
    # interrupted during sleep, resume safely repeats the full cooldown rather
    # than issuing an early GET.
    sleep_fn(delay)
    rate_limit["cooldown_seconds_pending"] = 0.0
    if checkpoint_context is not None:
        _write_round_robin_checkpoints(
            config=config,
            context=checkpoint_context,
            state=state,
            public_checked=public_checked,
            accepted=accepted,
        )
    return True


def _record_round_robin_rate_limit(state: dict[str, Any], payload: Any) -> None:
    rate_limit = _ensure_round_robin_rate_limit_state(state)
    consecutive = int(rate_limit["consecutive_429"]) + 1
    exponential = min(
        _ROUND_ROBIN_RATE_LIMIT_MAX_EXPONENTIAL_SECONDS,
        _ROUND_ROBIN_RATE_LIMIT_BASE_SECONDS * (2 ** min(consecutive - 1, 30)),
    )
    retry_after = None
    if isinstance(payload, dict):
        retry_after = _number(payload.get("retry_after_seconds"))
    if retry_after is not None:
        retry_after = min(max(0.0, retry_after), _ROUND_ROBIN_RATE_LIMIT_MAX_SERVER_SECONDS)
    cooldown = max(exponential, retry_after or 0.0)
    rate_limit.update(
        {
            "consecutive_429": consecutive,
            "total_429": int(rate_limit["total_429"]) + 1,
            "last_retry_after_seconds": retry_after,
            "cooldown_seconds_pending": cooldown,
        }
    )


def _reset_round_robin_rate_limit_streak(state: dict[str, Any]) -> None:
    rate_limit = _ensure_round_robin_rate_limit_state(state)
    rate_limit["consecutive_429"] = 0
    rate_limit["last_retry_after_seconds"] = None
    rate_limit["cooldown_seconds_pending"] = 0.0


def _sort_round_robin_progress(
    public_checked: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    selected_order: dict[str, int],
) -> None:
    public_checked.sort(key=lambda row: selected_order[str(row["alpha_id"])])
    accepted.sort(key=lambda row: selected_order[str(row["alpha_id"])])


def _uncheckpointed_poll_evidence(
    *,
    config: ScreenConfig,
    attempt_count: int,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    return (
        {
            "result": "ERROR",
            "value": None,
            "limit": config.self_corr_cutoff,
            "platform_limit": None,
        },
        {
            "status": "ERROR",
            "result": "ERROR",
            "max_polls": config.check_polls,
            "interval_seconds": config.check_interval,
            "polls": attempt_count,
            "retries": 0,
        },
        "self_error",
    )


def _prepare_performance_candidate(
    row: dict[str, Any],
    active_field_sets: list[set[str]],
    config: ScreenConfig,
) -> tuple[dict[str, Any] | None, str | None]:
    alpha_id = str(row.get("id") or row.get("alpha_id") or "").strip()
    expression = _expression(row)
    fields = expression_fields(expression)
    if not alpha_id:
        return None, "missing_alpha_id"
    if any(token in alpha_id for token in ("/", "\\", "?", "#", "..")):
        return None, "unsafe_alpha_id"
    if not expression or not fields:
        return None, "missing_expression_fields"

    raw_is_metrics = row.get("is")
    is_metrics: dict[str, Any] = raw_is_metrics if isinstance(raw_is_metrics, dict) else {}
    metrics = {
        "sharpe": _number(row.get("sharpe"), is_metrics.get("sharpe")),
        "fitness": _number(row.get("fitness"), is_metrics.get("fitness")),
        "turnover": _number(row.get("turnover"), is_metrics.get("turnover")),
        "returns": _number(row.get("returns"), is_metrics.get("returns")),
    }
    if metrics["sharpe"] is None or metrics["sharpe"] < config.sharpe_min:
        return None, "performance_sharpe"
    if metrics["fitness"] is None or metrics["fitness"] < config.fitness_min:
        return None, "performance_fitness"
    if metrics["turnover"] is None or metrics["turnover"] > config.turnover_max:
        return None, "performance_turnover"
    if _has_non_correlation_platform_failure(row):
        return None, "platform_non_correlation_fail"

    signature = "|".join(fields)
    max_overlap = max((_jaccard(set(fields), active) for active in active_field_sets), default=0.0)
    settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
    inventory_review = parse_review_checks(row)
    inventory_self = _number(
        (inventory_review.get("self_correlation") or {}).get("value")
    )
    return {
        "alpha_id": alpha_id,
        "expression": expression,
        "settings": settings,
        "metrics": metrics,
        "performance_gate": {
            "result": "PASS",
            "thresholds": {
                "sharpe_min": config.sharpe_min,
                "fitness_min": config.fitness_min,
                "turnover_max": config.turnover_max,
            },
        },
        "field_signature": signature,
        "field_signature_sha256": hashlib.sha256(signature.encode("utf-8")).hexdigest(),
        "field_count": len(fields),
        "max_active_field_jaccard": round(max_overlap, 6),
        "inventory_self": inventory_self,
        "date_created": row.get("dateCreated") or row.get("date_created"),
    }, None


def _has_non_correlation_platform_failure(row: dict[str, Any]) -> bool:
    """Reject inventory rows already known to fail a non-correlation gate."""
    raw_checks: list[Any] = []
    for value in (row.get("checks"), (row.get("is") or {}).get("checks")):
        if isinstance(value, list):
            raw_checks.extend(value)
        elif isinstance(value, dict):
            raw_checks.extend(value.values())
    for check in raw_checks:
        if not isinstance(check, dict):
            continue
        if str(check.get("result") or "").strip().upper() != "FAIL":
            continue
        name = str(check.get("name") or "").strip().upper()
        if "CORRELATION" in name and ("SELF" in name or "PROD" in name):
            continue
        return True
    return False


def _current_self_evidence(
    payload: Any,
    cutoff: float,
    *,
    max_polls: int,
    interval: float,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    rate_limited = False
    pending = False
    transport_error = False
    if not isinstance(payload, dict):
        result = "ERROR"
        value = None
        platform_limit = None
    else:
        status_code = _integer(payload.get("status_code"))
        failure_kind = str(payload.get("failure_kind") or "").strip().lower()
        error_text = str(payload.get("error") or payload.get("detail") or "").lower()
        rate_limited = status_code == 429 or "rate_limit" in failure_kind or "rate limit" in error_text
        pending = failure_kind == "correlation_pending"
        supplied_review = payload.get("review_checks")
        if isinstance(supplied_review, dict) and isinstance(supplied_review.get("self_correlation"), dict):
            self_check = supplied_review["self_correlation"]
        else:
            self_check = parse_review_checks(payload).get("self_correlation") or {}
        result = str(self_check.get("result") or "MISSING").upper()
        value = _number(self_check.get("value"))
        platform_limit = _number(self_check.get("limit"))
        transport_error = bool(payload.get("ok") is False and not supplied_review and not pending and not rate_limited)

    if rate_limited:
        result = "ERROR"
        value = None
    elif pending and result == "MISSING":
        result = "PENDING"
    elif transport_error:
        result = "ERROR"
        value = None

    current_self = {
        "result": result,
        "value": value,
        "limit": cutoff,
        "platform_limit": platform_limit,
    }
    complete = result not in {"MISSING", "PENDING", "ERROR"} and value is not None
    check = {
        "status": "RATE_LIMITED" if rate_limited else ("COMPLETE" if complete else result),
        "result": result,
        "max_polls": max_polls,
        "interval_seconds": interval,
        "polls": _integer(payload.get("polls")) if isinstance(payload, dict) else None,
        "retries": 0,
    }
    if rate_limited:
        retry_after = _number(payload.get("retry_after_seconds")) if isinstance(payload, dict) else None
        if retry_after is not None:
            check["retry_after_seconds"] = retry_after
        return current_self, check, "self_rate_limited"
    if result == "ERROR":
        return current_self, check, "self_error"
    if result == "PENDING":
        return current_self, check, "self_pending"
    if result == "MISSING":
        return current_self, check, "self_missing"
    if value is None:
        return current_self, check, "self_nonnumeric"
    if result not in {"PASS", "WARNING"}:
        return current_self, check, "self_platform_fail"
    if not value < cutoff:
        return current_self, check, "self_not_strictly_below_cutoff"
    return current_self, check, None


def _public_checked_row(
    candidate: dict[str, Any],
    current_self: dict[str, Any],
    *,
    accepted: bool,
    rejection_reason: str | None,
    poll_attempts: int | None = None,
) -> dict[str, Any]:
    row = {
        "alpha_id": candidate["alpha_id"],
        "metrics": candidate["metrics"],
        "current_self": current_self,
        "field_count": candidate["field_count"],
        "field_signature_sha256": candidate["field_signature_sha256"],
        "max_active_field_jaccard": candidate["max_active_field_jaccard"],
        "inventory_self": candidate["inventory_self"],
        "date_created": candidate["date_created"],
        "accepted": accepted,
        "rejection_reason": rejection_reason,
    }
    if poll_attempts is not None:
        row["poll_attempts"] = poll_attempts
    return row


def _private_accepted_row(
    candidate: dict[str, Any],
    *,
    check: dict[str, Any],
    current_self: dict[str, Any],
) -> dict[str, Any]:
    return {
        "artifact_type": PRIVATE_ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "alpha_id": str(candidate["alpha_id"]),
        "platform_status": "UNSUBMITTED",
        "expression": candidate["expression"],
        "expression_sha256": hashlib.sha256(_canonical_expression(candidate["expression"]).encode("utf-8")).hexdigest(),
        "settings": candidate["settings"],
        "metrics": candidate["metrics"],
        "performance_gate": candidate["performance_gate"],
        "check": check,
        "current_self": current_self,
        "field_signature": candidate["field_signature"],
        "field_signature_sha256": candidate["field_signature_sha256"],
        "field_count": candidate["field_count"],
        "max_active_field_jaccard": candidate["max_active_field_jaccard"],
        "inventory_self": candidate["inventory_self"],
        "date_created": candidate["date_created"],
    }


def _build_checkpoint_context(
    *,
    config: ScreenConfig,
    inventory: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    base_rejected: Counter[str],
) -> dict[str, Any]:
    safe_config = {
        "account": config.account,
        "sharpe_min": config.sharpe_min,
        "fitness_min": config.fitness_min,
        "turnover_max": config.turnover_max,
        "self_corr_cutoff": config.self_corr_cutoff,
        "max_checks": config.max_checks,
        "selection_offset": config.selection_offset,
        "page_size": config.page_size,
        "inventory_cap": config.inventory_cap,
        "check_polls": config.check_polls,
        "check_interval": config.check_interval,
        "between_checks": config.between_checks,
    }
    if config.round_robin_polling:
        safe_config["round_robin_polling"] = True
    if config.prioritize_inventory_self:
        safe_config["prioritize_inventory_self"] = True
    bound_config = {
        **safe_config,
        "output_private": str(Path(config.output_private).resolve()),
        "output_audit": str(Path(config.output_audit).resolve()),
    }
    inventory_items = sorted(
        (
            {
                "alpha_id": alpha_id,
                "status": _status(row),
            }
            for row in inventory
            if (alpha_id := str(row.get("id") or row.get("alpha_id") or "").strip())
        ),
        key=lambda item: (item["alpha_id"], item["status"]),
    )
    inventory_snapshot = {
        "rows": len(inventory),
        "items": inventory_items,
        "missing_id_rows": len(inventory) - len(inventory_items),
    }
    selected_ids = [str(candidate["alpha_id"]) for candidate in selected]
    private_selection = [
        {
            "alpha_id": str(candidate["alpha_id"]),
            "expression_sha256": hashlib.sha256(
                _canonical_expression(candidate["expression"]).encode("utf-8")
            ).hexdigest(),
            "settings": candidate["settings"],
            "metrics": candidate["metrics"],
            "field_signature_sha256": candidate["field_signature_sha256"],
            "field_count": candidate["field_count"],
            "max_active_field_jaccard": candidate["max_active_field_jaccard"],
            "date_created": candidate["date_created"],
        }
        for candidate in selected
    ]
    config_sha256 = _canonical_json_sha256(bound_config)
    inventory_sha256 = _canonical_json_sha256(inventory_snapshot)
    selection_sha256 = _canonical_json_sha256(private_selection)
    binding = {
        "config_sha256": config_sha256,
        "inventory_sha256": inventory_sha256,
        "selection_sha256": selection_sha256,
    }
    return {
        "config": safe_config,
        "inventory_snapshot": inventory_snapshot,
        "selected_ids": selected_ids,
        "base_rejected": dict(sorted(base_rejected.items())),
        "binding": {
            **binding,
            "run_sha256": _canonical_json_sha256(binding),
        },
    }


def _checkpoint_private_path(config: ScreenConfig) -> Path:
    if config.checkpoint_private is not None:
        return Path(config.checkpoint_private)
    if config.checkpoint is None:  # pragma: no cover - guarded by callers
        raise ValueError("checkpoint is required")
    public_path = Path(config.checkpoint)
    suffix = public_path.suffix or ".json"
    stem = public_path.stem if public_path.suffix else public_path.name
    return public_path.with_name(f"{stem}.private{suffix}")


def _checkpoint_progress_sha256(
    *,
    next_index: int,
    public_checked: list[dict[str, Any]],
    accepted_ids: list[str],
) -> str:
    return _canonical_json_sha256(
        {
            "next_index": next_index,
            "public_checked": public_checked,
            "accepted_ids": accepted_ids,
        }
    )


def _private_checkpoint_payload(
    *,
    context: dict[str, Any],
    next_index: int,
    public_checked: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    updated_at: str,
) -> dict[str, Any]:
    accepted_ids = [str(row["alpha_id"]) for row in accepted]
    return {
        "artifact_type": PRIVATE_CHECKPOINT_ARTIFACT_TYPE,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "updated_at": updated_at,
        "run": context,
        "next_index": next_index,
        "completed": next_index == len(context["selected_ids"]),
        "public_checked": public_checked,
        "accepted_rows": accepted,
        "progress_sha256": _checkpoint_progress_sha256(
            next_index=next_index,
            public_checked=public_checked,
            accepted_ids=accepted_ids,
        ),
    }


def _public_checkpoint_payload(
    *,
    context: dict[str, Any],
    next_index: int,
    public_checked: list[dict[str, Any]],
    accepted_ids: list[str],
    private_file_sha256: str,
    updated_at: str,
) -> dict[str, Any]:
    return {
        "artifact_type": PUBLIC_CHECKPOINT_ARTIFACT_TYPE,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "updated_at": updated_at,
        "read_only": True,
        "submit_calls": 0,
        "submit_attempts": 0,
        "run": context,
        "next_index": next_index,
        "completed": next_index == len(context["selected_ids"]),
        "public_checked": public_checked,
        "accepted_ids": accepted_ids,
        "progress_sha256": _checkpoint_progress_sha256(
            next_index=next_index,
            public_checked=public_checked,
            accepted_ids=accepted_ids,
        ),
        "private_checkpoint_sha256": private_file_sha256,
    }


def _write_checkpoints(
    *,
    config: ScreenConfig,
    context: dict[str, Any],
    next_index: int,
    public_checked: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
) -> None:
    if config.checkpoint is None:  # pragma: no cover - guarded by callers
        return
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    private_payload = _private_checkpoint_payload(
        context=context,
        next_index=next_index,
        public_checked=public_checked,
        accepted=accepted,
        updated_at=updated_at,
    )
    private_bytes = _checkpoint_bytes(private_payload)
    _atomic_write(_checkpoint_private_path(config), private_bytes)
    public_payload = _public_checkpoint_payload(
        context=context,
        next_index=next_index,
        public_checked=public_checked,
        accepted_ids=[str(row["alpha_id"]) for row in accepted],
        private_file_sha256=hashlib.sha256(private_bytes).hexdigest(),
        updated_at=updated_at,
    )
    _atomic_write(Path(config.checkpoint), _checkpoint_bytes(public_payload))


def _round_robin_progress_sha256(
    *,
    state: dict[str, Any],
    public_checked: list[dict[str, Any]],
    accepted_ids: list[str],
) -> str:
    return _canonical_json_sha256(
        {
            "round_robin_state": state,
            "public_checked": public_checked,
            "accepted_ids": accepted_ids,
        }
    )


def _write_round_robin_checkpoints(
    *,
    config: ScreenConfig,
    context: dict[str, Any],
    state: dict[str, Any],
    public_checked: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
) -> None:
    if config.checkpoint is None:  # pragma: no cover - guarded by callers
        return
    accepted_ids = [str(row["alpha_id"]) for row in accepted]
    completed = len(public_checked) == len(context["selected_ids"])
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    private_payload = {
        "artifact_type": PRIVATE_CHECKPOINT_ARTIFACT_TYPE,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "updated_at": updated_at,
        "run": context,
        "next_index": len(public_checked),
        "completed": completed,
        "round_robin_state": state,
        "public_checked": public_checked,
        "accepted_rows": accepted,
        "progress_sha256": _round_robin_progress_sha256(
            state=state,
            public_checked=public_checked,
            accepted_ids=accepted_ids,
        ),
    }
    private_bytes = _checkpoint_bytes(private_payload)
    _atomic_write(_checkpoint_private_path(config), private_bytes)
    public_payload = {
        "artifact_type": PUBLIC_CHECKPOINT_ARTIFACT_TYPE,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "updated_at": updated_at,
        "read_only": True,
        "submit_calls": 0,
        "submit_attempts": 0,
        "run": context,
        "next_index": len(public_checked),
        "completed": completed,
        "round_robin_state": state,
        "public_checked": public_checked,
        "accepted_ids": accepted_ids,
        "progress_sha256": _round_robin_progress_sha256(
            state=state,
            public_checked=public_checked,
            accepted_ids=accepted_ids,
        ),
        "private_checkpoint_sha256": hashlib.sha256(private_bytes).hexdigest(),
    }
    _atomic_write(Path(config.checkpoint), _checkpoint_bytes(public_payload))


def _load_round_robin_checkpoint_progress(
    *,
    config: ScreenConfig,
    context: dict[str, Any],
    selected: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    private_path = _checkpoint_private_path(config)
    if not private_path.is_file():
        raise ValueError(f"private checkpoint does not exist: {private_path}")
    private_payload, private_file_sha256 = _read_checkpoint(
        private_path,
        artifact_type=PRIVATE_CHECKPOINT_ARTIFACT_TYPE,
    )
    _validate_checkpoint_run(private_payload, context, path=private_path)
    state, public_checked, accepted = _validate_round_robin_private_progress(
        private_payload,
        context=context,
        selected=selected,
        path=private_path,
    )

    public_path = Path(config.checkpoint) if config.checkpoint is not None else None
    if public_path is not None and public_path.is_file():
        public_payload, _ = _read_checkpoint(public_path, artifact_type=PUBLIC_CHECKPOINT_ARTIFACT_TYPE)
        _validate_checkpoint_run(public_payload, context, path=public_path)
        _validate_round_robin_public_prefix(
            public_payload,
            context=context,
            selected=selected,
            private_state=state,
            private_public_checked=public_checked,
            private_accepted=accepted,
            private_file_sha256=private_file_sha256,
            path=public_path,
        )
    return state, public_checked, accepted


def _validate_round_robin_private_progress(
    payload: dict[str, Any],
    *,
    context: dict[str, Any],
    selected: list[dict[str, Any]],
    path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    public_checked = payload.get("public_checked")
    accepted = payload.get("accepted_rows")
    if not isinstance(public_checked, list) or not all(isinstance(row, dict) for row in public_checked):
        raise ValueError(f"checkpoint public_checked must be a list of objects: {path}")
    if not isinstance(accepted, list) or not all(isinstance(row, dict) for row in accepted):
        raise ValueError(f"checkpoint accepted_rows must be a list of objects: {path}")
    state = _validate_round_robin_progress_common(
        payload,
        context=context,
        selected=selected,
        public_checked=public_checked,
        accepted_ids=[str(row.get("alpha_id") or "") for row in accepted],
        path=path,
    )
    accepted_from_public = [
        str(row.get("alpha_id") or "") for row in public_checked if row.get("accepted") is True
    ]
    accepted_ids = [str(row.get("alpha_id") or "") for row in accepted]
    if accepted_ids != accepted_from_public:
        raise ValueError(f"checkpoint accepted rows do not match public progress: {path}")
    selected_by_id = {str(candidate["alpha_id"]): candidate for candidate in selected}
    for row in accepted:
        candidate = selected_by_id.get(str(row.get("alpha_id") or ""))
        if candidate is None:
            raise ValueError(f"checkpoint accepted ID is not selected: {path}")
        expected_expression_sha256 = hashlib.sha256(
            _canonical_expression(candidate["expression"]).encode("utf-8")
        ).hexdigest()
        if row.get("expression_sha256") != expected_expression_sha256:
            raise ValueError(f"checkpoint accepted expression binding changed: {path}")
    return state, public_checked, accepted


def _validate_round_robin_progress_common(
    payload: dict[str, Any],
    *,
    context: dict[str, Any],
    selected: list[dict[str, Any]],
    public_checked: list[dict[str, Any]],
    accepted_ids: list[str],
    path: Path,
) -> dict[str, Any]:
    state = payload.get("round_robin_state")
    if not isinstance(state, dict):
        raise ValueError(f"checkpoint round_robin_state must be an object: {path}")
    rate_limit = state.get("rate_limit")
    if rate_limit is not None:
        _validate_round_robin_rate_limit_state(rate_limit, path=path)
    round_index = state.get("round_index")
    cursor = state.get("cursor")
    attempts = state.get("attempts_reserved")
    responses = state.get("responses_recorded")
    candidate_count = len(selected)
    max_polls = int(context["config"]["check_polls"])
    if isinstance(round_index, bool) or not isinstance(round_index, int) or not 0 <= round_index < max_polls:
        raise ValueError(f"checkpoint round_index is invalid: {path}")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or not 0 <= cursor <= candidate_count:
        raise ValueError(f"checkpoint round cursor is invalid: {path}")
    if not isinstance(attempts, list) or len(attempts) != candidate_count:
        raise ValueError(f"checkpoint attempts_reserved is invalid: {path}")
    if not isinstance(responses, list) or len(responses) != candidate_count:
        raise ValueError(f"checkpoint responses_recorded is invalid: {path}")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in [*attempts, *responses]):
        raise ValueError(f"checkpoint poll counters must be integers: {path}")
    if any(not 0 <= responses[index] <= attempts[index] <= max_polls for index in range(candidate_count)):
        raise ValueError(f"checkpoint poll counters are out of bounds: {path}")

    selected_ids = [str(candidate["alpha_id"]) for candidate in selected]
    checked_ids = [str(row.get("alpha_id") or "") for row in public_checked]
    if len(set(checked_ids)) != len(checked_ids) or any(alpha_id not in selected_ids for alpha_id in checked_ids):
        raise ValueError(f"checkpoint checked IDs are not a unique selected subset: {path}")
    expected_checked_order = [alpha_id for alpha_id in selected_ids if alpha_id in set(checked_ids)]
    if checked_ids != expected_checked_order:
        raise ValueError(f"checkpoint checked IDs are not in selected order: {path}")
    resolved_ids = set(checked_ids)
    for index, alpha_id in enumerate(selected_ids):
        maximum_reachable = round_index + 1 if index < cursor else round_index
        if attempts[index] > maximum_reachable:
            raise ValueError(f"checkpoint poll reservation is not reachable: {path}")
        if alpha_id not in resolved_ids and attempts[index] != maximum_reachable:
            raise ValueError(f"checkpoint unresolved poll reservation is not a round prefix: {path}")
    for row in public_checked:
        alpha_id = str(row.get("alpha_id") or "")
        index = selected_ids.index(alpha_id)
        if row.get("poll_attempts") != attempts[index]:
            raise ValueError(f"checkpoint checked poll count does not match reservations: {path}")

    if payload.get("next_index") != len(public_checked):
        raise ValueError(f"checkpoint next_index does not match resolved rows: {path}")
    completed = len(public_checked) == candidate_count
    if payload.get("completed") is not completed:
        raise ValueError(f"checkpoint completed flag mismatch: {path}")
    expected_progress_sha256 = _round_robin_progress_sha256(
        state=state,
        public_checked=public_checked,
        accepted_ids=accepted_ids,
    )
    if payload.get("progress_sha256") != expected_progress_sha256:
        raise ValueError(f"checkpoint progress hash mismatch: {path}")
    return state


def _validate_round_robin_rate_limit_state(value: Any, *, path: Path) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"checkpoint rate-limit state must be an object: {path}")
    consecutive = value.get("consecutive_429")
    total = value.get("total_429")
    retry_after = value.get("last_retry_after_seconds")
    pending = value.get("cooldown_seconds_pending")
    if (
        isinstance(consecutive, bool)
        or not isinstance(consecutive, int)
        or consecutive < 0
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total < consecutive
    ):
        raise ValueError(f"checkpoint rate-limit counters are invalid: {path}")
    for name, number in (
        ("last_retry_after_seconds", retry_after),
        ("cooldown_seconds_pending", pending),
    ):
        if number is None and name == "last_retry_after_seconds":
            continue
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(float(number))
            or not 0 <= float(number) <= _ROUND_ROBIN_RATE_LIMIT_MAX_SERVER_SECONDS
        ):
            raise ValueError(f"checkpoint rate-limit {name} is invalid: {path}")


def _validate_round_robin_public_prefix(
    payload: dict[str, Any],
    *,
    context: dict[str, Any],
    selected: list[dict[str, Any]],
    private_state: dict[str, Any],
    private_public_checked: list[dict[str, Any]],
    private_accepted: list[dict[str, Any]],
    private_file_sha256: str,
    path: Path,
) -> None:
    public_checked = payload.get("public_checked")
    accepted_ids = payload.get("accepted_ids")
    if not isinstance(public_checked, list) or not all(isinstance(row, dict) for row in public_checked):
        raise ValueError(f"checkpoint public_checked must be a list of objects: {path}")
    if not isinstance(accepted_ids, list) or not all(isinstance(alpha_id, str) for alpha_id in accepted_ids):
        raise ValueError(f"checkpoint accepted_ids must be a list of strings: {path}")
    public_state = _validate_round_robin_progress_common(
        payload,
        context=context,
        selected=selected,
        public_checked=public_checked,
        accepted_ids=accepted_ids,
        path=path,
    )
    expected_public_accepted = [
        str(row["alpha_id"]) for row in public_checked if row.get("accepted") is True
    ]
    if accepted_ids != expected_public_accepted:
        raise ValueError(f"checkpoint accepted IDs do not match public progress: {path}")

    public_position = (int(public_state["round_index"]), int(public_state["cursor"]))
    private_position = (int(private_state["round_index"]), int(private_state["cursor"]))
    if public_position > private_position:
        raise ValueError(f"public checkpoint is ahead of private checkpoint: {path}")
    if any(
        public_value > private_value
        for public_value, private_value in zip(
            public_state["attempts_reserved"], private_state["attempts_reserved"], strict=True
        )
    ) or any(
        public_value > private_value
        for public_value, private_value in zip(
            public_state["responses_recorded"], private_state["responses_recorded"], strict=True
        )
    ):
        raise ValueError(f"public checkpoint poll counters are ahead of private checkpoint: {path}")
    private_checked_by_id = {str(row["alpha_id"]): row for row in private_public_checked}
    if any(private_checked_by_id.get(str(row["alpha_id"])) != row for row in public_checked):
        raise ValueError(f"public checkpoint is not a valid private checkpoint prefix: {path}")
    private_accepted_ids = {str(row["alpha_id"]) for row in private_accepted}
    if any(alpha_id not in private_accepted_ids for alpha_id in accepted_ids):
        raise ValueError(f"public checkpoint accepted IDs are not a private subset: {path}")

    exactly_current = (
        public_state == private_state
        and public_checked == private_public_checked
        and accepted_ids == [str(row["alpha_id"]) for row in private_accepted]
    )
    if exactly_current and payload.get("private_checkpoint_sha256") != private_file_sha256:
        raise ValueError(f"public checkpoint does not bind the private checkpoint: {path}")


def _load_checkpoint_progress(
    *,
    config: ScreenConfig,
    context: dict[str, Any],
    selected: list[dict[str, Any]],
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    private_path = _checkpoint_private_path(config)
    if not private_path.is_file():
        raise ValueError(f"private checkpoint does not exist: {private_path}")
    private_payload, private_file_sha256 = _read_checkpoint(
        private_path,
        artifact_type=PRIVATE_CHECKPOINT_ARTIFACT_TYPE,
    )
    _validate_checkpoint_run(private_payload, context, path=private_path)
    next_index, public_checked, accepted = _validate_private_checkpoint_progress(
        private_payload,
        context=context,
        selected=selected,
        path=private_path,
    )

    public_path = Path(config.checkpoint) if config.checkpoint is not None else None
    if public_path is not None and public_path.is_file():
        public_payload, _ = _read_checkpoint(public_path, artifact_type=PUBLIC_CHECKPOINT_ARTIFACT_TYPE)
        _validate_checkpoint_run(public_payload, context, path=public_path)
        _validate_public_checkpoint_prefix(
            public_payload,
            context=context,
            private_next_index=next_index,
            private_public_checked=public_checked,
            private_accepted=accepted,
            private_file_sha256=private_file_sha256,
            path=public_path,
        )
    return next_index, public_checked, accepted


def _validate_checkpoint_run(payload: dict[str, Any], context: dict[str, Any], *, path: Path) -> None:
    if payload.get("run") != context:
        raise ValueError(f"checkpoint config, inventory, or selected candidate binding changed: {path}")


def _validate_private_checkpoint_progress(
    payload: dict[str, Any],
    *,
    context: dict[str, Any],
    selected: list[dict[str, Any]],
    path: Path,
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    next_index = _checkpoint_index(payload.get("next_index"), len(selected), path=path)
    public_checked = payload.get("public_checked")
    accepted = payload.get("accepted_rows")
    if not isinstance(public_checked, list) or not all(isinstance(row, dict) for row in public_checked):
        raise ValueError(f"checkpoint public_checked must be a list of objects: {path}")
    if not isinstance(accepted, list) or not all(isinstance(row, dict) for row in accepted):
        raise ValueError(f"checkpoint accepted_rows must be a list of objects: {path}")
    if len(public_checked) != next_index:
        raise ValueError(f"checkpoint next_index does not match checked rows: {path}")
    expected_checked_ids = context["selected_ids"][:next_index]
    checked_ids = [str(row.get("alpha_id") or "") for row in public_checked]
    if checked_ids != expected_checked_ids:
        raise ValueError(f"checkpoint checked IDs are not the selected queue prefix: {path}")
    accepted_ids_from_public = [str(row.get("alpha_id") or "") for row in public_checked if row.get("accepted") is True]
    accepted_ids = [str(row.get("alpha_id") or "") for row in accepted]
    if accepted_ids != accepted_ids_from_public:
        raise ValueError(f"checkpoint accepted rows do not match public progress: {path}")
    selected_by_id = {str(candidate["alpha_id"]): candidate for candidate in selected[:next_index]}
    for row in accepted:
        alpha_id = str(row.get("alpha_id") or "")
        candidate = selected_by_id.get(alpha_id)
        if candidate is None:
            raise ValueError(f"checkpoint accepted ID is not selected: {path}")
        expected_expression_sha256 = hashlib.sha256(
            _canonical_expression(candidate["expression"]).encode("utf-8")
        ).hexdigest()
        if row.get("expression_sha256") != expected_expression_sha256:
            raise ValueError(f"checkpoint accepted expression binding changed: {path}")
    expected_progress_sha256 = _checkpoint_progress_sha256(
        next_index=next_index,
        public_checked=public_checked,
        accepted_ids=accepted_ids,
    )
    if payload.get("progress_sha256") != expected_progress_sha256:
        raise ValueError(f"checkpoint progress hash mismatch: {path}")
    if payload.get("completed") is not (next_index == len(selected)):
        raise ValueError(f"checkpoint completed flag mismatch: {path}")
    return next_index, public_checked, accepted


def _validate_public_checkpoint_prefix(
    payload: dict[str, Any],
    *,
    context: dict[str, Any],
    private_next_index: int,
    private_public_checked: list[dict[str, Any]],
    private_accepted: list[dict[str, Any]],
    private_file_sha256: str,
    path: Path,
) -> None:
    public_next_index = _checkpoint_index(payload.get("next_index"), private_next_index, path=path)
    public_checked = payload.get("public_checked")
    accepted_ids = payload.get("accepted_ids")
    if not isinstance(public_checked, list) or not all(isinstance(row, dict) for row in public_checked):
        raise ValueError(f"checkpoint public_checked must be a list of objects: {path}")
    if not isinstance(accepted_ids, list) or not all(isinstance(alpha_id, str) for alpha_id in accepted_ids):
        raise ValueError(f"checkpoint accepted_ids must be a list of strings: {path}")
    expected_checked = private_public_checked[:public_next_index]
    expected_accepted_ids = [
        str(row["alpha_id"])
        for row in private_accepted
        if str(row["alpha_id"])
        in {str(checked["alpha_id"]) for checked in expected_checked if checked.get("accepted") is True}
    ]
    if public_checked != expected_checked or accepted_ids != expected_accepted_ids:
        raise ValueError(f"public checkpoint is not a valid private checkpoint prefix: {path}")
    expected_progress_sha256 = _checkpoint_progress_sha256(
        next_index=public_next_index,
        public_checked=public_checked,
        accepted_ids=accepted_ids,
    )
    if payload.get("progress_sha256") != expected_progress_sha256:
        raise ValueError(f"checkpoint progress hash mismatch: {path}")
    if public_next_index == private_next_index and payload.get("private_checkpoint_sha256") != private_file_sha256:
        raise ValueError(f"public checkpoint does not bind the private checkpoint: {path}")
    if payload.get("completed") is not (public_next_index == len(context["selected_ids"])):
        raise ValueError(f"checkpoint completed flag mismatch: {path}")


def _checkpoint_index(value: Any, upper_bound: int, *, path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= upper_bound:
        raise ValueError(f"checkpoint next_index is invalid: {path}")
    return value


def _checkpoint_bytes(payload: dict[str, Any]) -> bytes:
    envelope = dict(payload)
    envelope["payload_sha256"] = _canonical_json_sha256(payload)
    return _json_bytes(envelope)


def _read_checkpoint(path: Path, *, artifact_type: str) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"checkpoint is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"checkpoint must be a JSON object: {path}")
    if payload.get("artifact_type") != artifact_type or payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError(f"checkpoint type or schema mismatch: {path}")
    supplied_sha256 = payload.pop("payload_sha256", None)
    if supplied_sha256 != _canonical_json_sha256(payload):
        raise ValueError(f"checkpoint payload hash mismatch: {path}")
    return payload, hashlib.sha256(raw).hexdigest()


def _canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _build_public_audit(
    *,
    config: ScreenConfig,
    inventory: list[dict[str, Any]],
    checked_rows: list[dict[str, Any]],
    private_rows: list[dict[str, Any]],
    screen_counts: dict[str, Any],
    inventory_get_calls: int,
    private_sha256: str,
) -> dict[str, Any]:
    def ids_for_status(status: str) -> list[str]:
        return sorted(
            {
                str(row.get("id") or row.get("alpha_id") or "").strip()
                for row in inventory
                if _status(row) == status and str(row.get("id") or row.get("alpha_id") or "").strip()
            }
        )

    inventory_ids = sorted(
        {
            str(row.get("id") or row.get("alpha_id") or "").strip()
            for row in inventory
            if str(row.get("id") or row.get("alpha_id") or "").strip()
        }
    )
    return {
        "artifact_type": AUDIT_ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "read_only": True,
        "submit_calls": 0,
        "submit_attempts": 0,
        "config": {
            "account": config.account,
            "performance_thresholds": {
                "sharpe_min": config.sharpe_min,
                "fitness_min": config.fitness_min,
                "turnover_max": config.turnover_max,
            },
            "self_corr_cutoff": config.self_corr_cutoff,
            "self_comparison": "numeric_value < self_corr_cutoff",
            "max_checks": config.max_checks,
            "selection_offset": config.selection_offset,
            "prioritize_inventory_self": config.prioritize_inventory_self,
            "check_max_polls": config.check_polls,
            "check_sleep_seconds": config.check_interval,
            "check_retries": 0,
            "between_checks_seconds": config.between_checks,
            "inventory_page_interval_seconds": config.inventory_page_interval,
            "polling_mode": "round_robin" if config.round_robin_polling else "serial",
            "rate_limit_backoff": {
                "base_seconds": _ROUND_ROBIN_RATE_LIMIT_BASE_SECONDS,
                "max_exponential_seconds": _ROUND_ROBIN_RATE_LIMIT_MAX_EXPONENTIAL_SECONDS,
                "server_retry_after_is_floor": True,
            },
            "page_size": config.page_size,
            "inventory_display_cap": config.inventory_cap,
            "inventory_scope": "platform_most_recent_window",
            "sort": (
                [
                    "inventory_self_numeric_first_asc",
                    "max_active_field_jaccard_asc",
                    "fitness_desc",
                    "date_created_desc",
                    "alpha_id_asc",
                ]
                if config.prioritize_inventory_self
                else ["max_active_field_jaccard_asc", "fitness_desc", "date_created_desc", "alpha_id_asc"]
            ),
            "dedupe": ["canonical_exact_expression", "field_signature"],
        },
        "http_receipt": {
            "authentication": 1,
            "inventory_get_calls": inventory_get_calls,
            "check_get_calls": (
                screen_counts.get("check_get_calls_exact")
                if config.round_robin_polling
                else (len(checked_rows) if config.check_polls == 1 else None)
            ),
            "check_actions": (
                screen_counts.get("check_get_reservations")
                if config.round_robin_polling
                else len(checked_rows)
            ),
            "check_get_calls_lower_bound": (
                screen_counts.get("check_get_responses_recorded")
                if config.round_robin_polling
                else len(checked_rows)
            ),
            "check_get_calls_upper_bound": (
                screen_counts.get("check_get_reservations")
                if config.round_robin_polling
                else len(checked_rows) * config.check_polls
            ),
            "allowed_get_paths": [INVENTORY_PATH, "/alphas/{alpha_id}/check"],
            "other_calls": 0,
        },
        "counts": screen_counts,
        "ids": {
            "inventory": inventory_ids,
            "active": ids_for_status("ACTIVE"),
            "unsubmitted": ids_for_status("UNSUBMITTED"),
            "checked": [str(row["alpha_id"]) for row in checked_rows],
            "accepted": [str(row["alpha_id"]) for row in private_rows],
        },
        "checks": checked_rows,
        "private_output": {
            "sha256": private_sha256,
            "records": len(private_rows),
        },
    }


def _candidate_sort_key(
    row: dict[str, Any],
    *,
    prioritize_inventory_self: bool = False,
) -> tuple[Any, ...]:
    base = (
        float(row["max_active_field_jaccard"]),
        -float(row["metrics"]["fitness"]),
        -_date_timestamp(row.get("date_created")),
        str(row["alpha_id"]),
    )
    if not prioritize_inventory_self:
        return base
    inventory_self = _number(row.get("inventory_self"))
    return (
        inventory_self is None,
        inventory_self if inventory_self is not None else math.inf,
        *base,
    )


def _date_timestamp(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return 0.0


def _expression(row: dict[str, Any]) -> str:
    regular = row.get("regular")
    if isinstance(regular, dict) and regular.get("code"):
        return str(regular["code"]).strip()
    return str(row.get("expression") or row.get("code") or "").strip()


def _status(row: dict[str, Any]) -> str:
    return str(row.get("status") or row.get("platform_status") or "").strip().upper()


def _canonical_expression(expression: str) -> str:
    return "".join(expression.split()).lower()


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    return len(left & right) / len(left | right)


def _number(*values: Any) -> float | None:
    for value in values:
        if value is None or value == "" or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    text = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n"
    return text.encode("utf-8")


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _validate_config(config: ScreenConfig) -> None:
    if not isinstance(config.round_robin_polling, bool):
        raise ValueError("round_robin_polling must be a boolean")
    if not isinstance(config.prioritize_inventory_self, bool):
        raise ValueError("prioritize_inventory_self must be a boolean")
    if Path(config.output_private).resolve() == Path(config.output_audit).resolve():
        raise ValueError("private and audit outputs must be different files")
    if config.resume and config.checkpoint is None:
        raise ValueError("resume requires checkpoint")
    if config.checkpoint_private is not None and config.checkpoint is None:
        raise ValueError("checkpoint_private requires checkpoint")
    if config.checkpoint is not None:
        checkpoint_paths = {
            "output_private": Path(config.output_private).resolve(),
            "output_audit": Path(config.output_audit).resolve(),
            "checkpoint": Path(config.checkpoint).resolve(),
            "checkpoint_private": _checkpoint_private_path(config).resolve(),
        }
        if len(set(checkpoint_paths.values())) != len(checkpoint_paths):
            raise ValueError("final outputs and checkpoint files must all be different")
    for name, value in (
        ("sharpe_min", config.sharpe_min),
        ("fitness_min", config.fitness_min),
        ("turnover_max", config.turnover_max),
        ("self_corr_cutoff", config.self_corr_cutoff),
        ("check_interval", config.check_interval),
        ("between_checks", config.between_checks),
        ("inventory_page_interval", config.inventory_page_interval),
    ):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
    if config.turnover_max < 0:
        raise ValueError("turnover_max must be non-negative")
    if config.self_corr_cutoff <= 0:
        raise ValueError("self_corr_cutoff must be positive")
    if config.max_checks < 0:
        raise ValueError("max_checks must be non-negative")
    if config.selection_offset < 0:
        raise ValueError("selection_offset must be non-negative")
    if config.check_polls < 1:
        raise ValueError("check_polls must be at least one")
    if config.check_interval < 0:
        raise ValueError("check_interval must be non-negative")
    if config.between_checks < 0:
        raise ValueError("between_checks must be non-negative")
    if config.inventory_page_interval < 0:
        raise ValueError("inventory_page_interval must be non-negative")
    if not 1 <= config.page_size <= 100:
        raise ValueError("page_size must be between 1 and 100")
    if not 1 <= config.inventory_cap <= INVENTORY_DISPLAY_CAP:
        raise ValueError(f"inventory_cap must be between 1 and {INVENTORY_DISPLAY_CAP}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only current-SELF screen for WQ UNSUBMITTED alphas")
    parser.add_argument(
        "--output-private", required=True, help="Private JSONL output with expressions and field signatures"
    )
    parser.add_argument("--output-audit", required=True, help="Public JSON audit without expressions or field names")
    parser.add_argument("--account", default="primary")
    parser.add_argument("--sharpe-min", type=float, default=1.25)
    parser.add_argument("--fitness-min", type=float, default=1.0)
    parser.add_argument("--turnover-max", type=float, default=0.7)
    parser.add_argument("--self-corr-cutoff", type=float, default=0.6)
    parser.add_argument("--max-checks", type=int, default=40)
    parser.add_argument(
        "--selection-offset",
        type=int,
        default=0,
        help="Skip this many candidates in the deterministic deduplicated check queue",
    )
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument(
        "--inventory-cap",
        type=int,
        default=INVENTORY_DISPLAY_CAP,
        help="Bound the most-recent inventory window (maximum 1000)",
    )
    parser.add_argument(
        "--inventory-page-interval",
        type=float,
        default=0,
        help="Operational sleep between inventory page GETs; excluded from checkpoint selection binding",
    )
    parser.add_argument("--check-polls", type=int, default=1)
    parser.add_argument("--check-interval", type=float, default=0)
    parser.add_argument("--between-checks", type=float, default=0)
    parser.add_argument(
        "--round-robin-polling",
        action="store_true",
        help=(
            "Opt in to one GET per unresolved candidate per round; wait check-interval "
            "between rounds instead of polling one candidate to completion"
        ),
    )
    parser.add_argument(
        "--prioritize-inventory-self",
        action="store_true",
        help=(
            "Sort candidates with an existing numeric inventory SELF value first, "
            "ascending; every candidate still requires a fresh current check"
        ),
    )
    parser.add_argument(
        "--checkpoint",
        default="",
        help="Optional public-safe JSON checkpoint; enables atomic per-candidate progress",
    )
    parser.add_argument(
        "--checkpoint-private",
        default="",
        help="Optional private companion checkpoint path (default: <checkpoint>.private.json)",
    )
    parser.add_argument("--resume", action="store_true", help="Resume after validating checkpoint bindings")
    return parser.parse_args(argv)


def _resolve_output(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_dotenv(ROOT)
    config = ScreenConfig(
        output_private=_resolve_output(args.output_private),
        output_audit=_resolve_output(args.output_audit),
        account=args.account,
        sharpe_min=args.sharpe_min,
        fitness_min=args.fitness_min,
        turnover_max=args.turnover_max,
        self_corr_cutoff=args.self_corr_cutoff,
        max_checks=args.max_checks,
        selection_offset=args.selection_offset,
        page_size=args.page_size,
        inventory_cap=args.inventory_cap,
        inventory_page_interval=args.inventory_page_interval,
        check_polls=args.check_polls,
        check_interval=args.check_interval,
        between_checks=args.between_checks,
        checkpoint=_resolve_output(args.checkpoint) if args.checkpoint else None,
        checkpoint_private=_resolve_output(args.checkpoint_private) if args.checkpoint_private else None,
        resume=args.resume,
        round_robin_polling=args.round_robin_polling,
        prioritize_inventory_self=args.prioritize_inventory_self,
    )
    receipt = screen_current_self(get_client(config.account), config)
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
