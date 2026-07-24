"""Recoverable single-candidate execution engine for WQ workflows."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from ..record_utils import first_float
from ..wq_auto_mining import validate_wq_expression
from ..wq_brain_service import run_single_simulation
from .domain import (
    AgentEvent,
    CandidateIdentity,
    EffectiveSettings,
    EventType,
    ExecutionPolicy,
    ExecutionResult,
    RunScope,
    utc_now,
)
from .domain import (
    attempt_uid as build_attempt_uid,
)
from .ports import AgentRepository, ExecutionObserver, PlatformGateway

IGNORED_SIMULATION_CHECKS = {"SELF_CORRELATION", "PROD_CORRELATION", "MATCHES_COMPETITION"}
ACTIVE_STATUSES = {"ACTIVE"}
RETRIABLE_CHECK_FAILURES = {
    "api_check_error",
    "api_check_pending",
    "correlation_check_error",
    "correlation_missing",
    "correlation_pending",
}
RETRIABLE_SUBMIT_FAILURES = {
    "connection_error",
    "network_error",
    "rate_limited",
    "request_error",
    "submission_pending",
    "timeout",
}


class WQBrainPlatformGateway:
    """Adapter for the existing synchronous WQ BRAIN client."""

    def __init__(self, client: Any, *, max_submit_posts_per_call: int = 1):
        self.client = client
        self.max_submit_posts_per_call = max_submit_posts_per_call

    def simulate(
        self,
        expression: str,
        settings: EffectiveSettings,
        *,
        tag: str | None = None,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> dict[str, Any]:
        return run_single_simulation(
            self.client,
            expression,
            region=settings.region,
            universe=settings.universe,
            delay=settings.delay,
            decay=settings.decay,
            neutralization=settings.neutralization,
            truncation=settings.truncation,
            max_trade=settings.max_trade,
            max_position=settings.max_position,
            auto_submit=False,
            tag=tag,
            progress_callback=progress_callback,
        )

    def check(self, alpha_id: str, *, max_polls: int, interval: int) -> dict[str, Any]:
        return self.client.check_alpha_submission(alpha_id, max_polls=max_polls, interval=interval)

    def submit(self, alpha_id: str) -> dict[str, Any]:
        return self.client.submit_alpha(
            alpha_id,
            max_post_attempts=self.max_submit_posts_per_call,
        )

    def status(self, alpha_id: str) -> dict[str, Any]:
        return self.client.check_alpha_status(alpha_id)


class ExecutionEngine:
    """Execute one candidate with idempotent events and resumable transitions."""

    def __init__(
        self,
        gateway: PlatformGateway,
        repository: AgentRepository,
        *,
        validator: Callable[[str], None] = validate_wq_expression,
    ) -> None:
        self.gateway = gateway
        self.repository = repository
        self.validator = validator

    def execute(
        self,
        candidate_row: dict[str, Any],
        *,
        run_id: str,
        settings: EffectiveSettings,
        scope: RunScope | None = None,
        policy: ExecutionPolicy | None = None,
        observer: ExecutionObserver | None = None,
        attempt_no: int = 1,
        resume: bool = True,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> ExecutionResult:
        policy = policy or ExecutionPolicy()
        scope = scope or RunScope.from_settings(settings.to_platform_dict())
        candidate = CandidateIdentity.create(str(candidate_row.get("expression") or ""), settings)
        attempt = build_attempt_uid(run_id, candidate.candidate_uid, attempt_no)
        existing = self.repository.events_for_attempt(attempt) if resume else []
        by_type = _latest_events_by_type(existing)

        resumed = self._resume_terminal(
            existing,
            by_type,
            candidate=candidate,
            attempt=attempt,
            run_id=run_id,
            scope=scope,
            policy=policy,
            observer=observer,
        )
        if resumed:
            return resumed

        metadata = _candidate_metadata(candidate_row, settings)
        self._emit(
            EventType.CANDIDATE_CREATED,
            run_id=run_id,
            attempt=attempt,
            candidate=candidate,
            scope=scope,
            stage="candidate",
            payload=metadata,
            observer=observer,
        )

        if EventType.VALIDATION_PASSED.value not in by_type:
            try:
                self.validator(candidate.expression)
            except Exception as exc:
                record = {**metadata, "status": "validation_failed", "failure_kind": "validation_error", "error": str(exc)}
                self._emit(
                    EventType.VALIDATION_REJECTED,
                    run_id=run_id,
                    attempt=attempt,
                    candidate=candidate,
                    scope=scope,
                    stage="validation",
                    payload={**metadata, "record": record},
                    observer=observer,
                    record=record,
                )
                return ExecutionResult(
                    status="VALIDATION_FAILED",
                    candidate=candidate,
                    attempt_uid=attempt,
                    reason=str(exc),
                )
            self._emit(
                EventType.VALIDATION_PASSED,
                run_id=run_id,
                attempt=attempt,
                candidate=candidate,
                scope=scope,
                stage="validation",
                payload=metadata,
                observer=observer,
            )

        sim_event = by_type.get(EventType.SIMULATION_SUCCEEDED.value)
        simulation_row = _event_record(sim_event) if sim_event else None
        if simulation_row is None:
            self._emit(
                EventType.SIMULATION_STARTED,
                run_id=run_id,
                attempt=attempt,
                candidate=candidate,
                scope=scope,
                stage="simulation",
                payload=metadata,
                observer=observer,
            )
            simulation = self.gateway.simulate(
                candidate.expression,
                settings,
                tag=str(candidate_row.get("tag") or "") or None,
                progress_callback=progress_callback,
            )
            simulation_row = build_simulation_record(candidate_row, candidate, settings, simulation)
            if not simulation_row.get("ok"):
                self._emit(
                    EventType.SIMULATION_FAILED,
                    run_id=run_id,
                    attempt=attempt,
                    candidate=candidate,
                    scope=scope,
                    stage="simulation",
                    payload={**metadata, "record": simulation_row, "failure_kind": "simulation_failed"},
                    observer=observer,
                    record=simulation_row,
                )
                return ExecutionResult(
                    status="SIMULATION_FAILED",
                    candidate=candidate,
                    attempt_uid=attempt,
                    simulation_row=simulation_row,
                    reason=str(simulation_row.get("error") or "simulation failed"),
                )
            self._emit(
                EventType.SIMULATION_SUCCEEDED,
                run_id=run_id,
                attempt=attempt,
                candidate=candidate,
                scope=scope,
                stage="simulation",
                payload={**metadata, "record": simulation_row},
                alpha_id=_text(simulation_row.get("alpha_id")),
                observer=observer,
                record=simulation_row,
            )

        alpha_id = _text(simulation_row.get("alpha_id"))
        if not _should_check(simulation_row):
            blocked = {
                **simulation_row,
                "failure_kind": _simulation_block_reason(simulation_row),
                "reason": "simulation did not pass metric/platform gates",
            }
            self._emit(
                EventType.CHECK_BLOCKED,
                run_id=run_id,
                attempt=attempt,
                candidate=candidate,
                scope=scope,
                stage="simulation_gate",
                payload={**metadata, "record": blocked, "failure_kind": blocked["failure_kind"]},
                alpha_id=alpha_id,
                observer=observer,
                record=blocked,
            )
            return ExecutionResult(
                status="BLOCKED",
                candidate=candidate,
                attempt_uid=attempt,
                alpha_id=alpha_id,
                simulation_row=simulation_row,
                reason=blocked["failure_kind"],
            )

        retrying_submission = _latest_event(
            existing,
            {
                EventType.SUBMIT_STARTED.value,
                EventType.SUBMIT_FAILED.value,
                EventType.SUBMISSION_PENDING.value,
            },
        ) is not None
        check_event = None if retrying_submission else _latest_event(
            existing,
            {EventType.CHECK_SUCCEEDED.value, EventType.CHECK_BLOCKED.value},
        )
        check_row = _event_record(check_event) if check_event else None
        if not check_row or _check_should_retry(check_row):
            check_attempt = _event_count(existing, EventType.CHECK_STARTED) + 1
            self._emit(
                EventType.CHECK_STARTED,
                run_id=run_id,
                attempt=attempt,
                candidate=candidate,
                scope=scope,
                stage="check",
                payload=metadata,
                alpha_id=alpha_id,
                observer=observer,
                event_key=f"{attempt}:check_started:{check_attempt}",
            )
            check = self.gateway.check(alpha_id, max_polls=policy.check_polls, interval=policy.check_interval)
            check_row = build_check_record(simulation_row, check)

        ready, gate_reason = correlation_gate(check_row, cutoff=policy.self_correlation_cutoff)
        if not ready:
            check_row = {**check_row, "failure_kind": check_row.get("failure_kind") or gate_reason, "reason": gate_reason}
            self._emit(
                EventType.CHECK_BLOCKED,
                run_id=run_id,
                attempt=attempt,
                candidate=candidate,
                scope=scope,
                stage="check",
                payload={**metadata, "record": check_row, "failure_kind": check_row.get("failure_kind")},
                alpha_id=alpha_id,
                observer=observer,
                record=check_row,
                event_key=f"{attempt}:check_blocked:{_check_fingerprint(check_row)}",
            )
            return ExecutionResult(
                status="CHECK_PENDING" if gate_reason in RETRIABLE_CHECK_FAILURES else "BLOCKED",
                candidate=candidate,
                attempt_uid=attempt,
                alpha_id=alpha_id,
                simulation_row=simulation_row,
                check_row=check_row,
                resumed=bool(check_event),
                reason=gate_reason,
            )

        self._emit(
            EventType.CHECK_SUCCEEDED,
            run_id=run_id,
            attempt=attempt,
            candidate=candidate,
            scope=scope,
            stage="check",
            payload={**metadata, "record": check_row},
            alpha_id=alpha_id,
            observer=observer,
            record=check_row,
            event_key=f"{attempt}:check_succeeded:{_check_fingerprint(check_row)}",
        )

        if not policy.submit_enabled:
            self._emit(
                EventType.READY,
                run_id=run_id,
                attempt=attempt,
                candidate=candidate,
                scope=scope,
                stage="ready",
                payload={**metadata, "record": {**simulation_row, **check_row, "status": "READY"}},
                alpha_id=alpha_id,
                observer=observer,
                record={**simulation_row, **check_row, "status": "READY"},
            )
            return ExecutionResult(
                status="READY",
                candidate=candidate,
                attempt_uid=attempt,
                alpha_id=alpha_id,
                simulation_row=simulation_row,
                check_row=check_row,
                resumed=bool(existing),
            )

        submit_attempt = _event_count(existing, EventType.SUBMIT_STARTED) + 1
        self._emit(
            EventType.SUBMIT_STARTED,
            run_id=run_id,
            attempt=attempt,
            candidate=candidate,
            scope=scope,
            stage="submit",
            payload=metadata,
            alpha_id=alpha_id,
            observer=observer,
            event_key=f"{attempt}:submit_started:{submit_attempt}",
        )
        submit = self.gateway.submit(alpha_id)
        submit_row = build_submit_record(simulation_row, check_row, submit)
        if submit_row.get("final_status") == "ACTIVE":
            self._emit(
                EventType.SUBMIT_SUCCEEDED,
                run_id=run_id,
                attempt=attempt,
                candidate=candidate,
                scope=scope,
                stage="submit",
                payload={**metadata, "record": submit_row},
                alpha_id=alpha_id,
                observer=observer,
                record=submit_row,
                event_key=f"{attempt}:submit_succeeded:{submit_attempt}",
            )
            return ExecutionResult(
                status="ACTIVE",
                candidate=candidate,
                attempt_uid=attempt,
                alpha_id=alpha_id,
                simulation_row=simulation_row,
                check_row=check_row,
                submit_row=submit_row,
            )

        event_type = EventType.SUBMISSION_PENDING if submit_row.get("final_status") == "CORR_PENDING" else EventType.SUBMIT_FAILED
        self._emit(
            event_type,
            run_id=run_id,
            attempt=attempt,
            candidate=candidate,
            scope=scope,
            stage="submit",
            payload={**metadata, "record": submit_row, "failure_kind": submit_row.get("failure_kind")},
            alpha_id=alpha_id,
            observer=observer,
            record=submit_row,
            event_key=f"{attempt}:{event_type.value}:{submit_attempt}",
        )
        return ExecutionResult(
            status="SUBMISSION_PENDING" if event_type == EventType.SUBMISSION_PENDING else "SUBMIT_FAILED",
            candidate=candidate,
            attempt_uid=attempt,
            alpha_id=alpha_id,
            simulation_row=simulation_row,
            check_row=check_row,
            submit_row=submit_row,
            reason=str(submit_row.get("failure_kind") or submit_row.get("detail") or "submit failed"),
        )

    def _resume_terminal(
        self,
        existing: list[dict[str, Any]],
        by_type: dict[str, dict[str, Any]],
        *,
        candidate: CandidateIdentity,
        attempt: str,
        run_id: str,
        scope: RunScope,
        policy: ExecutionPolicy,
        observer: ExecutionObserver | None,
    ) -> ExecutionResult | None:
        active_event = by_type.get(EventType.SUBMIT_SUCCEEDED.value) or by_type.get(EventType.ACTIVE_CONFIRMED.value)
        if active_event:
            record = _event_record(active_event)
            return ExecutionResult(
                status="ACTIVE",
                candidate=candidate,
                attempt_uid=attempt,
                alpha_id=_text(active_event.get("alpha_id") or record.get("alpha_id")),
                submit_row=record,
                resumed=True,
            )

        validation_event = by_type.get(EventType.VALIDATION_REJECTED.value)
        if validation_event:
            record = _event_record(validation_event) or {}
            return ExecutionResult(
                status="VALIDATION_FAILED",
                candidate=candidate,
                attempt_uid=attempt,
                reason=_text(record.get("error") or record.get("reason")),
                resumed=True,
            )

        simulation_event = by_type.get(EventType.SIMULATION_FAILED.value)
        if simulation_event:
            record = _event_record(simulation_event) or {}
            return ExecutionResult(
                status="SIMULATION_FAILED",
                candidate=candidate,
                attempt_uid=attempt,
                alpha_id=_text(simulation_event.get("alpha_id") or record.get("alpha_id")),
                simulation_row=record,
                reason=_text(record.get("error") or record.get("reason")),
                resumed=True,
            )

        latest_check = _latest_event(
            existing,
            {EventType.CHECK_SUCCEEDED.value, EventType.CHECK_BLOCKED.value},
        )
        if latest_check and latest_check.get("event_type") == EventType.CHECK_BLOCKED.value:
            record = _event_record(latest_check) or {}
            failure_kind = _check_failure_kind(record)
            if failure_kind not in RETRIABLE_CHECK_FAILURES:
                return ExecutionResult(
                    status="BLOCKED",
                    candidate=candidate,
                    attempt_uid=attempt,
                    alpha_id=_text(latest_check.get("alpha_id") or record.get("alpha_id")),
                    check_row=record,
                    reason=failure_kind,
                    resumed=True,
                )

        if EventType.READY.value in by_type and not policy.submit_enabled:
            event = by_type[EventType.READY.value]
            return ExecutionResult(
                status="READY",
                candidate=candidate,
                attempt_uid=attempt,
                alpha_id=_text(event.get("alpha_id")),
                check_row=_event_record(event),
                resumed=True,
            )
        submit_event = _latest_event(
            existing,
            {
                EventType.SUBMIT_STARTED.value,
                EventType.SUBMIT_FAILED.value,
                EventType.SUBMISSION_PENDING.value,
            },
        )
        if submit_event:
            record = _event_record(submit_event) or {}
            alpha_id = _text(submit_event.get("alpha_id") or record.get("alpha_id"))
            if not alpha_id:
                return ExecutionResult(
                    status="SUBMIT_FAILED",
                    candidate=candidate,
                    attempt_uid=attempt,
                    reason="submit event has no alpha_id",
                    resumed=True,
                )

            if (
                submit_event.get("event_type") == EventType.SUBMIT_FAILED.value
                and not _submit_failure_retriable(record)
            ):
                return ExecutionResult(
                    status="SUBMIT_FAILED",
                    candidate=candidate,
                    attempt_uid=attempt,
                    alpha_id=alpha_id,
                    submit_row=record,
                    reason=_submit_failure_kind(record),
                    resumed=True,
                )

            status = self.gateway.status(alpha_id)
            platform_status = str(status.get("status") or status.get("platform_status") or "").upper()
            if platform_status in ACTIVE_STATUSES:
                record = {"alpha_id": alpha_id, "final_status": "ACTIVE", "platform_status": platform_status, "ok": True}
                self._emit(
                    EventType.ACTIVE_CONFIRMED,
                    run_id=run_id,
                    attempt=attempt,
                    candidate=candidate,
                    scope=scope,
                    stage="submit_reconcile",
                    payload={"record": record},
                    alpha_id=alpha_id,
                    observer=observer,
                    record=record,
                )
                return ExecutionResult(
                    status="ACTIVE",
                    candidate=candidate,
                    attempt_uid=attempt,
                    alpha_id=alpha_id,
                    submit_row=record,
                    resumed=True,
                )

            if not status.get("ok") or not platform_status:
                return ExecutionResult(
                    status="SUBMISSION_PENDING",
                    candidate=candidate,
                    attempt_uid=attempt,
                    alpha_id=alpha_id,
                    submit_row={**record, "reconcile_status": status},
                    reason="platform status could not be confirmed",
                    resumed=True,
                )

            if platform_status != "UNSUBMITTED":
                record = {
                    "alpha_id": alpha_id,
                    "final_status": "CORR_PENDING",
                    "platform_status": platform_status,
                    "failure_kind": "submission_pending",
                    "ok": False,
                }
                self._emit(
                    EventType.SUBMISSION_PENDING,
                    run_id=run_id,
                    attempt=attempt,
                    candidate=candidate,
                    scope=scope,
                    stage="submit_reconcile",
                    payload={"record": record, "failure_kind": "submission_pending"},
                    alpha_id=alpha_id,
                    observer=observer,
                    record=record,
                )
                return ExecutionResult(
                    status="SUBMISSION_PENDING",
                    candidate=candidate,
                    attempt_uid=attempt,
                    alpha_id=alpha_id,
                    submit_row=record,
                    resumed=True,
                )

            max_submit_attempts = 1 + max(0, int(policy.max_submit_retries))
            submit_attempts = _event_count(existing, EventType.SUBMIT_STARTED)
            if submit_attempts >= max_submit_attempts:
                exhausted = {
                    **record,
                    "alpha_id": alpha_id,
                    "final_status": "OTHER_FAIL",
                    "platform_status": platform_status,
                    "failure_kind": "submit_retry_exhausted",
                    "detail": f"submit retry budget exhausted ({submit_attempts}/{max_submit_attempts})",
                    "ok": False,
                }
                self._emit(
                    EventType.SUBMIT_FAILED,
                    run_id=run_id,
                    attempt=attempt,
                    candidate=candidate,
                    scope=scope,
                    stage="submit_reconcile",
                    payload={"record": exhausted, "failure_kind": "submit_retry_exhausted"},
                    alpha_id=alpha_id,
                    observer=observer,
                    record=exhausted,
                    event_key=f"{attempt}:submit_retry_exhausted",
                )
                return ExecutionResult(
                    status="SUBMIT_FAILED",
                    candidate=candidate,
                    attempt_uid=attempt,
                    alpha_id=alpha_id,
                    submit_row=exhausted,
                    reason="submit_retry_exhausted",
                    resumed=True,
                )
        return None

    def _emit(
        self,
        event_type: EventType,
        *,
        run_id: str,
        attempt: str,
        candidate: CandidateIdentity,
        scope: RunScope,
        stage: str,
        payload: dict[str, Any],
        observer: ExecutionObserver | None,
        alpha_id: str | None = None,
        record: dict[str, Any] | None = None,
        event_key: str | None = None,
    ) -> AgentEvent:
        event = AgentEvent.create(
            event_type,
            run_id=run_id,
            attempt_uid=attempt,
            candidate=candidate,
            scope=scope,
            stage=stage,
            payload=payload,
            alpha_id=alpha_id,
            event_key=event_key,
        )
        inserted = self.repository.append(event, candidate)
        if inserted and observer:
            observer.on_event(event, record)
        return event


def build_simulation_record(
    candidate_row: dict[str, Any],
    candidate: CandidateIdentity,
    settings: EffectiveSettings,
    simulation: dict[str, Any],
) -> dict[str, Any]:
    row = {
        **_candidate_metadata(candidate_row, settings),
        "created_at": utc_now(),
        "candidate_uid": candidate.candidate_uid,
        "expression_hash": candidate.expression_hash,
        "settings_hash": candidate.settings_hash,
        "stage": "simulation",
        "simulation_settings_effective": settings.to_platform_dict(),
        "result": simulation,
        "ok": bool(simulation.get("ok")),
    }
    if not simulation.get("ok"):
        return {**row, "status": "simulation_failed", "error": simulation.get("error")}
    metrics = simulation.get("wq_brain") if isinstance(simulation.get("wq_brain"), dict) else {}
    is_metrics = simulation.get("is_metrics") if isinstance(simulation.get("is_metrics"), dict) else {}
    checks = is_metrics.get("checks") if isinstance(is_metrics.get("checks"), list) else []
    failed = _failed_platform_checks(checks)
    return {
        **row,
        "status": "pending_correlation_check" if simulation.get("submit_eligible") and not failed else "simulated",
        "alpha_id": simulation.get("alpha_id"),
        "sharpe": first_float(metrics.get("wq_sharpe"), is_metrics.get("sharpe")),
        "fitness": first_float(metrics.get("wq_fitness"), is_metrics.get("fitness")),
        "returns": first_float(metrics.get("wq_returns"), is_metrics.get("returns")),
        "turnover": first_float(metrics.get("wq_turnover"), is_metrics.get("turnover")),
        "submit_eligible": bool(simulation.get("submit_eligible")),
        "submit_checks": simulation.get("submit_checks") or {},
        "is_checks": checks,
        "failed_platform_checks": failed,
        "simulation_id": simulation.get("simulation_id"),
    }


def build_check_record(simulation_row: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    review = check.get("review_checks") if isinstance(check.get("review_checks"), dict) else {}
    self_check = review.get("self_correlation") if isinstance(review.get("self_correlation"), dict) else {}
    prod_check = review.get("prod_correlation") if isinstance(review.get("prod_correlation"), dict) else {}
    failure = str(check.get("failure_kind") or "")
    failed_platform = _failed_platform_checks(_extract_check_items(check))
    if failed_platform:
        api_status = "platform_check_fail"
    elif failure == "self_correlation":
        api_status = "self_correlation_fail"
    elif failure == "prod_correlation":
        api_status = "prod_correlation_fail"
    elif failure == "correlation_pending":
        api_status = "api_check_pending"
    else:
        api_status = "api_check_readable" if check.get("ok") else "api_check_error"
    return {
        **_record_context(simulation_row),
        "created_at": check.get("created_at") or utc_now(),
        "alpha_id": simulation_row.get("alpha_id"),
        "api_check_status": api_status,
        "ok": bool(check.get("ok")) and not failure,
        "failure_kind": failure or None,
        "detail": check.get("detail"),
        "sc_result": self_check.get("result"),
        "sc_value": self_check.get("value", check.get("sc_value")),
        "sc_limit": self_check.get("limit", check.get("sc_limit")),
        "prod_corr_result": prod_check.get("result"),
        "prod_corr_value": prod_check.get("value", check.get("prod_value")),
        "prod_corr_limit": prod_check.get("limit", check.get("prod_limit")),
        "failed_platform_checks": failed_platform,
        "raw_check": check,
    }


def build_submit_record(
    simulation_row: dict[str, Any],
    check_row: dict[str, Any],
    submit: dict[str, Any],
) -> dict[str, Any]:
    final_status = _submit_status(submit)
    return {
        **_record_context(simulation_row),
        "created_at": utc_now(),
        "alpha_id": simulation_row.get("alpha_id"),
        "sharpe": simulation_row.get("sharpe"),
        "fitness": simulation_row.get("fitness"),
        "returns": simulation_row.get("returns"),
        "turnover": simulation_row.get("turnover"),
        "sc_value": check_row.get("sc_value"),
        "prod_corr_value": check_row.get("prod_corr_value"),
        "ok": final_status == "ACTIVE",
        "accepted": bool(submit.get("ok")),
        "final_status": final_status,
        "status_code": submit.get("status_code"),
        "platform_status": submit.get("platform_status"),
        "submit_post_attempts": submit.get("submit_post_attempts"),
        "submit_post_limit": submit.get("submit_post_limit"),
        "failure_kind": submit.get("failure_kind"),
        "detail": submit.get("detail"),
        "submit_result": submit,
    }


def correlation_gate(check_row: dict[str, Any], *, cutoff: float) -> tuple[bool, str]:
    if check_row.get("failed_platform_checks"):
        return False, "platform_check_fail"
    status = str(check_row.get("api_check_status") or "")
    if status == "api_check_pending":
        return False, "correlation_pending"
    if status != "api_check_readable":
        return False, "correlation_check_error"
    self_result = str(check_row.get("sc_result") or "").upper()
    if self_result in {"PENDING", "ERROR"}:
        return False, "correlation_pending"
    if self_result != "PASS":
        return False, "self_correlation_fail" if self_result == "FAIL" else "correlation_missing"
    self_value = first_float(check_row.get("sc_value"))
    if self_value is None or not math.isfinite(self_value):
        return False, "correlation_missing"
    if self_value > float(cutoff):
        return False, "self_correlation_above_cutoff"
    prod_result = str(check_row.get("prod_corr_result") or "MISSING").upper()
    if prod_result in {"PENDING", "ERROR"}:
        return False, "correlation_pending"
    if prod_result == "FAIL":
        return False, "prod_correlation_fail"
    return True, "ready"


def _candidate_metadata(candidate_row: dict[str, Any], settings: EffectiveSettings) -> dict[str, Any]:
    return {
        "candidate_key": candidate_row.get("candidate_key"),
        "source_index": candidate_row.get("source_index"),
        "tag": candidate_row.get("tag"),
        "source": candidate_row.get("source"),
        "source_family": candidate_row.get("source_family") or candidate_row.get("mutation_strategy"),
        "field_signature": candidate_row.get("field_signature"),
        "provenance": candidate_row.get("provenance"),
        "mutation_strategy": candidate_row.get("mutation_strategy"),
        "rationale": candidate_row.get("rationale"),
        "expected_low_corr_reason": candidate_row.get("expected_low_corr_reason"),
        "parent_alpha_ids": candidate_row.get("parent_alpha_ids") or [],
        "source_run_id": candidate_row.get("source_run_id") or candidate_row.get("source_run"),
        "forum_evidence": candidate_row.get("forum_evidence"),
        "repair_evidence": candidate_row.get("repair_evidence"),
        "risk_flags": candidate_row.get("risk_flags") or [],
        "expression": candidate_row.get("expression"),
        "simulation_settings": settings.to_platform_dict(),
    }


def _record_context(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "candidate_key",
        "source_index",
        "candidate_uid",
        "expression_hash",
        "settings_hash",
        "tag",
        "source",
        "source_family",
        "field_signature",
        "provenance",
        "mutation_strategy",
        "rationale",
        "expected_low_corr_reason",
        "parent_alpha_ids",
        "source_run_id",
        "forum_evidence",
        "repair_evidence",
        "risk_flags",
        "expression",
        "simulation_settings",
        "simulation_settings_effective",
    )
    return {key: row.get(key) for key in keys if key in row}


def _should_check(row: dict[str, Any]) -> bool:
    return bool(row.get("alpha_id")) and bool(row.get("submit_eligible")) and not row.get("failed_platform_checks")


def _failed_platform_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        check
        for check in checks
        if str(check.get("name") or "").upper() not in IGNORED_SIMULATION_CHECKS
        and str(check.get("result") or "").upper() == "FAIL"
    ]


def _extract_check_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for container in (
        payload,
        payload.get("raw_check") or {},
        (payload.get("raw_check") or {}).get("is") or {},
        payload.get("is") or {},
    ):
        value = container.get("checks") if isinstance(container, dict) else None
        if isinstance(value, list):
            checks.extend(item for item in value if isinstance(item, dict))
    return checks


def _simulation_block_reason(row: dict[str, Any]) -> str:
    failed = row.get("failed_platform_checks") or []
    if failed:
        return str((failed[0] or {}).get("name") or "platform_check_fail").lower()
    if not row.get("submit_eligible"):
        return "metric_threshold_fail"
    return "simulation_not_checkable"


def _submit_status(result: dict[str, Any]) -> str:
    platform_status = str(result.get("platform_status") or "").upper()
    if result.get("ok") and platform_status in ACTIVE_STATUSES:
        return "ACTIVE"
    failure = str(result.get("failure_kind") or "").lower()
    if failure == "self_correlation":
        return "SC_FAIL"
    if failure == "prod_correlation":
        return "PROD_CORR_FAIL"
    if result.get("ok") or failure == "correlation_pending" or platform_status == "TIMEOUT":
        return "CORR_PENDING"
    return "OTHER_FAIL"


def _latest_events_by_type(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        latest[str(event.get("event_type") or "")] = event
    return latest


def _latest_event(events: list[dict[str, Any]], event_types: set[str]) -> dict[str, Any] | None:
    for event in reversed(events):
        if str(event.get("event_type") or "") in event_types:
            return event
    return None


def _event_count(events: list[dict[str, Any]], event_type: EventType | str) -> int:
    value = event_type.value if isinstance(event_type, EventType) else str(event_type)
    return sum(str(event.get("event_type") or "") == value for event in events)


def _check_failure_kind(record: dict[str, Any]) -> str:
    return str(record.get("failure_kind") or record.get("api_check_status") or "check_blocked")


def _check_should_retry(record: dict[str, Any]) -> bool:
    return _check_failure_kind(record) in RETRIABLE_CHECK_FAILURES


def _submit_failure_kind(record: dict[str, Any]) -> str:
    return str(record.get("failure_kind") or "submit_failed").lower()


def _submit_failure_retriable(record: dict[str, Any]) -> bool:
    failure_kind = _submit_failure_kind(record)
    if failure_kind in RETRIABLE_SUBMIT_FAILURES:
        return True
    status_code = int(first_float(record.get("status_code")) or 0)
    return status_code in {0, 408, 425, 429} or status_code >= 500


def _check_fingerprint(row: dict[str, Any]) -> str:
    return ":".join(
        str(row.get(key) or "")
        for key in ("api_check_status", "sc_result", "sc_value", "prod_corr_result", "prod_corr_value")
    )


def _event_record(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if not event:
        return None
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    record = payload.get("record")
    return dict(record) if isinstance(record, dict) else dict(payload)


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
