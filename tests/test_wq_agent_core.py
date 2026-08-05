from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from worldquant_harness.wq_agent_core import (
    CandidateIdentity,
    EffectiveSettings,
    ExecutionEngine,
    ExecutionPolicy,
    InMemoryAgentRepository,
    RunScope,
)
from worldquant_harness.wq_agent_core.engine import correlation_gate


def _simulation(*, ok: bool = True, eligible: bool = True) -> dict[str, Any]:
    if not ok:
        return {"ok": False, "error": "simulation transport failed"}
    return {
        "ok": True,
        "alpha_id": "alpha-1",
        "simulation_id": "sim-1",
        "submit_eligible": eligible,
        "wq_brain": {"wq_sharpe": 1.6, "wq_fitness": 1.2, "wq_turnover": 0.2},
        "is_metrics": {"checks": []},
    }


def _check(
    *,
    self_result: str = "PASS",
    self_value: float | None = 0.5,
    prod_result: str = "MISSING",
    ok: bool = True,
    failure_kind: str | None = None,
) -> dict[str, Any]:
    row = {
        "ok": ok,
        "review_checks": {
            "self_correlation": {"result": self_result, "value": self_value, "limit": 0.7},
            "prod_correlation": {"result": prod_result, "value": None, "limit": None},
        },
    }
    if failure_kind:
        row["failure_kind"] = failure_kind
    return row


class FakeGateway:
    def __init__(
        self,
        *,
        simulations: list[dict[str, Any]] | None = None,
        checks: list[dict[str, Any]] | None = None,
        submits: list[dict[str, Any] | BaseException] | None = None,
        statuses: list[dict[str, Any]] | None = None,
    ) -> None:
        self.simulations = list(simulations or [_simulation()])
        self.checks = list(checks or [_check()])
        self.submits = list(submits or [{"ok": True, "platform_status": "ACTIVE", "status_code": 200}])
        self.statuses = list(statuses or [])
        self.calls = {"simulate": 0, "check": 0, "submit": 0, "status": 0}

    def simulate(
        self,
        expression: str,
        settings: EffectiveSettings,
        *,
        tag: str | None = None,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> dict[str, Any]:
        self.calls["simulate"] += 1
        return self.simulations.pop(0)

    def check(self, alpha_id: str, *, max_polls: int, interval: int) -> dict[str, Any]:
        self.calls["check"] += 1
        return self.checks.pop(0)

    def submit(self, alpha_id: str) -> dict[str, Any]:
        self.calls["submit"] += 1
        result = self.submits.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def status(self, alpha_id: str) -> dict[str, Any]:
        self.calls["status"] += 1
        return self.statuses.pop(0)


def _execute(
    gateway: FakeGateway,
    repository: InMemoryAgentRepository,
    *,
    policy: ExecutionPolicy | None = None,
) -> Any:
    engine = ExecutionEngine(gateway, repository, validator=lambda expression: None)
    return engine.execute(
        {"expression": "rank(close)", "tag": "unit", "source_family": "test"},
        run_id="run-1",
        settings=EffectiveSettings(),
        policy=policy or ExecutionPolicy(submit_enabled=True),
    )


def test_candidate_identity_includes_effective_settings() -> None:
    first = CandidateIdentity.create(" rank( close ) ", EffectiveSettings(decay=4))
    equivalent = CandidateIdentity.create("rank(close)", EffectiveSettings(decay=4))
    changed = CandidateIdentity.create("rank(close)", EffectiveSettings(decay=8))

    assert first.expression_hash == equivalent.expression_hash
    assert first.candidate_uid == equivalent.candidate_uid
    assert first.candidate_uid != changed.candidate_uid
    assert RunScope.from_settings(changed.settings.to_platform_dict()).scope_key != RunScope(
        region="CHN",
        universe="TOP3000",
        delay=1,
    ).scope_key


def test_run_scope_rejects_invalid_tenant_uuid() -> None:
    with pytest.raises(ValueError, match="valid UUID"):
        RunScope(user_id="not-a-uuid")


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (
            {"api_check_status": "api_check_readable", "sc_result": "PASS", "sc_value": 0.7},
            (False, "self_correlation_above_cutoff"),
        ),
        (
            {
                "api_check_status": "api_check_readable",
                "sc_result": "PASS",
                "sc_value": 0.5,
                "prod_corr_result": "MISSING",
            },
            (True, "ready"),
        ),
        (
            {"api_check_status": "api_check_readable", "sc_result": "PASS", "sc_value": None},
            (False, "correlation_missing"),
        ),
        (
            {"api_check_status": "api_check_readable", "sc_result": "PASS", "sc_value": "nan"},
            (False, "correlation_missing"),
        ),
        (
            {"api_check_status": "api_check_readable", "sc_result": "ERROR", "sc_value": 0.5},
            (False, "correlation_pending"),
        ),
        ({"api_check_status": "api_check_readable", "sc_result": "MISSING"}, (False, "correlation_missing")),
        (
            {"api_check_status": "api_check_readable", "sc_result": "PASS", "sc_value": 0.7001},
            (False, "self_correlation_above_cutoff"),
        ),
        (
            {
                "api_check_status": "api_check_readable",
                "sc_result": "PASS",
                "sc_value": 0.5,
                "prod_corr_result": "PENDING",
            },
            (False, "correlation_pending"),
        ),
        (
            {
                "api_check_status": "api_check_readable",
                "sc_result": "PASS",
                "sc_value": 0.5,
                "prod_corr_result": "ERROR",
            },
            (False, "correlation_pending"),
        ),
        (
            {
                "api_check_status": "api_check_readable",
                "sc_result": "PASS",
                "sc_value": 0.5,
                "prod_corr_result": "FAIL",
            },
            (False, "prod_correlation_fail"),
        ),
    ],
)
def test_correlation_gate_requires_explicit_self_pass(
    row: dict[str, Any],
    expected: tuple[bool, str],
) -> None:
    assert correlation_gate(row, cutoff=0.7) == expected


def test_active_result_is_idempotent_on_resume() -> None:
    gateway = FakeGateway()
    repository = InMemoryAgentRepository()

    first = _execute(gateway, repository)
    second = _execute(gateway, repository)

    assert first.status == second.status == "ACTIVE"
    assert second.resumed is True
    assert gateway.calls == {"simulate": 1, "check": 1, "submit": 1, "status": 0}
    assert [row["event_type"] for row in repository.events.values()].count("submit_succeeded") == 1


def test_uncertain_submit_is_reconciled_without_second_post() -> None:
    gateway = FakeGateway(
        submits=[{"ok": False, "platform_status": "TIMEOUT", "failure_kind": "correlation_pending"}],
        statuses=[{"ok": True, "status": "ACTIVE"}],
    )
    repository = InMemoryAgentRepository()

    first = _execute(gateway, repository)
    second = _execute(gateway, repository)

    assert first.status == "SUBMISSION_PENDING"
    assert second.status == "ACTIVE"
    assert second.resumed is True
    assert gateway.calls["submit"] == 1
    assert gateway.calls["status"] == 1
    assert any(row["event_type"] == "active_confirmed" for row in repository.events.values())


def test_retry_requires_explicit_unsubmitted_and_rechecks_correlations() -> None:
    gateway = FakeGateway(
        checks=[_check(), _check(self_value=0.45)],
        submits=[
            {"ok": False, "platform_status": "TIMEOUT", "failure_kind": "correlation_pending"},
            {"ok": True, "platform_status": "ACTIVE", "status_code": 200},
        ],
        statuses=[{"ok": True, "status": "UNSUBMITTED"}],
    )
    repository = InMemoryAgentRepository()

    assert _execute(gateway, repository).status == "SUBMISSION_PENDING"
    assert _execute(gateway, repository).status == "ACTIVE"

    assert gateway.calls["simulate"] == 1
    assert gateway.calls["check"] == 2
    assert gateway.calls["submit"] == 2
    assert gateway.calls["status"] == 1
    assert [row["event_type"] for row in repository.events.values()].count("submit_started") == 2


def test_retry_budget_prevents_second_submit() -> None:
    gateway = FakeGateway(
        submits=[{"ok": False, "platform_status": "TIMEOUT", "failure_kind": "correlation_pending"}],
        statuses=[{"ok": True, "status": "UNSUBMITTED"}],
    )
    repository = InMemoryAgentRepository()
    policy = ExecutionPolicy(submit_enabled=True, max_submit_retries=0)

    assert _execute(gateway, repository, policy=policy).status == "SUBMISSION_PENDING"
    second = _execute(gateway, repository, policy=policy)

    assert second.status == "SUBMIT_FAILED"
    assert second.reason == "submit_retry_exhausted"
    assert gateway.calls["submit"] == 1


def test_submit_started_crash_reconciles_platform_before_retry() -> None:
    gateway = FakeGateway(
        submits=[RuntimeError("connection dropped after POST")],
        statuses=[{"ok": True, "status": "ACTIVE"}],
    )
    repository = InMemoryAgentRepository()

    with pytest.raises(RuntimeError, match="connection dropped"):
        _execute(gateway, repository)
    resumed = _execute(gateway, repository)

    assert resumed.status == "ACTIVE"
    assert gateway.calls["submit"] == 1
    assert gateway.calls["status"] == 1


def test_pending_check_can_be_rechecked_but_hard_simulation_failure_is_terminal() -> None:
    pending_gateway = FakeGateway(
        checks=[
            _check(self_result="PENDING", self_value=None, ok=False, failure_kind="correlation_pending"),
            _check(self_result="PASS", self_value=0.4),
        ]
    )
    pending_repository = InMemoryAgentRepository()

    assert _execute(pending_gateway, pending_repository).status == "CHECK_PENDING"
    assert _execute(pending_gateway, pending_repository).status == "ACTIVE"
    assert pending_gateway.calls["simulate"] == 1
    assert pending_gateway.calls["check"] == 2

    failed_gateway = FakeGateway(simulations=[_simulation(ok=False)])
    failed_repository = InMemoryAgentRepository()
    assert _execute(failed_gateway, failed_repository).status == "SIMULATION_FAILED"
    assert _execute(failed_gateway, failed_repository).status == "SIMULATION_FAILED"
    assert failed_gateway.calls["simulate"] == 1
