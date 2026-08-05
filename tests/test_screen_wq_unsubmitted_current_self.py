from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import pytest

import scripts.screen_wq_unsubmitted_current_self as screener


class FakeReadOnlyClient:
    def __init__(self, inventory: list[dict[str, Any]], checks: dict[str, dict[str, Any]]) -> None:
        self.inventory = inventory
        self.checks = checks
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.closed = False

    def authenticate(self) -> bool:
        self.calls.append(("AUTH", "/authentication", None))
        return True

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("GET", path, params))
        assert path == "/users/self/alphas"
        assert params is not None
        offset = int(params["offset"])
        limit = int(params["limit"])
        return {"ok": True, "count": len(self.inventory), "results": self.inventory[offset : offset + limit]}

    def check_alpha_submission(self, alpha_id: str, *, max_polls: int, interval: float) -> dict[str, Any]:
        path = f"/alphas/{alpha_id}/check"
        self.calls.append(("GET", path, {"max_polls": max_polls, "interval": interval}))
        return self.checks[alpha_id]

    def close(self) -> None:
        self.closed = True


class InterruptingReadOnlyClient(FakeReadOnlyClient):
    def __init__(
        self,
        inventory: list[dict[str, Any]],
        checks: dict[str, dict[str, Any]],
        *,
        fail_on: str,
    ) -> None:
        super().__init__(inventory, checks)
        self.fail_on = fail_on

    def check_alpha_submission(self, alpha_id: str, *, max_polls: int, interval: float) -> dict[str, Any]:
        if alpha_id == self.fail_on:
            path = f"/alphas/{alpha_id}/check"
            self.calls.append(("GET", path, {"max_polls": max_polls, "interval": interval}))
            raise RuntimeError("simulated interruption")
        return super().check_alpha_submission(alpha_id, max_polls=max_polls, interval=interval)


class SequencedReadOnlyClient(FakeReadOnlyClient):
    def __init__(
        self,
        inventory: list[dict[str, Any]],
        checks: dict[str, list[dict[str, Any]]],
    ) -> None:
        super().__init__(inventory, {})
        self.sequenced_checks = {alpha_id: list(payloads) for alpha_id, payloads in checks.items()}

    def check_alpha_submission(self, alpha_id: str, *, max_polls: int, interval: float) -> dict[str, Any]:
        path = f"/alphas/{alpha_id}/check"
        self.calls.append(("GET", path, {"max_polls": max_polls, "interval": interval}))
        payloads = self.sequenced_checks[alpha_id]
        if not payloads:
            raise AssertionError(f"unexpected extra check for {alpha_id}")
        return payloads.pop(0)


class FlakyInventoryClient(FakeReadOnlyClient):
    def __init__(
        self,
        inventory: list[dict[str, Any]],
        *,
        fail_once_at_offset: int,
    ) -> None:
        super().__init__(inventory, {})
        self.fail_once_at_offset = fail_once_at_offset
        self.failed = False

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert params is not None
        offset = int(params["offset"])
        if offset == self.fail_once_at_offset and not self.failed:
            self.calls.append(("GET", path, params))
            self.failed = True
            return {"ok": False, "status_code": 500, "error": {}}
        return super().get_json(path, params)


def _alpha(
    alpha_id: str,
    expression: str,
    *,
    status: str = "UNSUBMITTED",
    sharpe: float = 1.4,
    fitness: float = 1.1,
    turnover: float = 0.2,
    created: str = "2026-07-20T00:00:00+00:00",
    inventory_self: float | None = None,
) -> dict[str, Any]:
    is_data: dict[str, Any] = {
        "sharpe": sharpe,
        "fitness": fitness,
        "turnover": turnover,
        "returns": 0.05,
    }
    if inventory_self is not None:
        is_data["checks"] = [
            {"name": "SELF_CORRELATION", "value": inventory_self, "limit": 0.7}
        ]
    return {
        "id": alpha_id,
        "status": status,
        "dateCreated": created,
        "regular": {"code": expression},
        "is": is_data,
        "settings": {"region": "USA", "universe": "TOP3000", "delay": 1},
    }


def _self_check(result: str, value: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "checks": [
            {"name": "SELF_CORRELATION", "result": result, "value": value, "limit": 0.7},
            {"name": "PROD_CORRELATION", "result": "PASS", "value": 0.2, "limit": 0.7},
        ],
    }


def _run(
    tmp_path: Path,
    client: FakeReadOnlyClient,
    *,
    max_checks: int = 40,
    selection_offset: int = 0,
    check_polls: int = 1,
    check_interval: float = 0,
    between_checks: float = 0,
    inventory_page_interval: float = 0,
    round_robin_polling: bool = False,
    prioritize_inventory_self: bool = False,
    sleep_fn: Callable[[float], None] = lambda _seconds: None,
) -> tuple[dict, list[dict], dict]:
    private_path = tmp_path / "private" / "current.jsonl"
    audit_path = tmp_path / "public" / "audit.json"
    receipt = screener.screen_current_self(
        client,
        screener.ScreenConfig(
            output_private=private_path,
            output_audit=audit_path,
            max_checks=max_checks,
            selection_offset=selection_offset,
            page_size=2,
            inventory_page_interval=inventory_page_interval,
            check_polls=check_polls,
            check_interval=check_interval,
            between_checks=between_checks,
            round_robin_polling=round_robin_polling,
            prioritize_inventory_self=prioritize_inventory_self,
        ),
        sleep_fn=sleep_fn,
    )
    private_rows = [json.loads(line) for line in private_path.read_text(encoding="utf-8").splitlines() if line]
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    return receipt, private_rows, audit


def test_source_has_no_network_or_live_write_call_and_runtime_only_uses_allowed_gets(tmp_path: Path) -> None:
    source = Path(screener.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called_attributes = {
        node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert "requests" not in imports
    assert "httpx" not in imports
    assert "sqlite3" not in imports
    assert "post" not in called_attributes
    assert not any(name.startswith("submit") for name in called_attributes)

    client = FakeReadOnlyClient(
        [_alpha("active", "rank(close)", status="ACTIVE"), _alpha("u1", "rank(volume)")],
        {"u1": _self_check("PASS", 0.59)},
    )
    sleep_calls: list[float] = []
    _run(tmp_path, client, sleep_fn=sleep_calls.append)

    assert client.closed is True
    assert client.calls[0] == ("AUTH", "/authentication", None)
    assert all(method == "GET" for method, _, _ in client.calls[1:])
    assert all(
        path == "/users/self/alphas" or (path.startswith("/alphas/") and path.endswith("/check"))
        for _, path, _ in client.calls[1:]
    )
    assert sum(path.endswith("/check") for _, path, _ in client.calls) == 1
    assert client.calls[-1][2] == {"max_polls": 1, "interval": 0}
    assert sleep_calls == []


def test_strict_threshold_rejects_equal_missing_pending_error_and_nonnumeric(tmp_path: Path) -> None:
    inventory = [
        _alpha("active", "rank(close)", status="ACTIVE"),
        _alpha("below", "rank(volume)"),
        _alpha("equal", "rank(vwap)"),
        _alpha("missing", "rank(open)"),
        _alpha("pending", "rank(high)"),
        _alpha("error", "rank(low)"),
        _alpha("text", "rank(returns)"),
        _alpha("rate", "rank(adv20)"),
    ]
    checks = {
        "below": _self_check("PASS", 0.599999),
        "equal": _self_check("PASS", 0.6),
        "missing": _self_check("MISSING", None),
        "pending": _self_check("PENDING", None),
        "error": _self_check("ERROR", None),
        "text": _self_check("PASS", "not-a-number"),
        "rate": {"ok": False, "status_code": 429, "error": "rate limited"},
    }
    _, rows, audit = _run(tmp_path, FakeReadOnlyClient(inventory, checks))

    assert [row["alpha_id"] for row in rows] == ["below"]
    assert rows[0]["current_self"]["value"] == 0.599999
    assert rows[0]["current_self"]["limit"] == 0.6
    assert audit["counts"]["checked"] == 7
    assert audit["counts"]["accepted_strict_self"] == 1
    rejected = audit["counts"]["rejected"]
    assert rejected["self_not_strictly_below_cutoff"] == 1
    assert rejected["self_missing"] == 1
    assert rejected["self_nonnumeric"] == 1
    assert rejected["self_pending"] == 1
    assert rejected["self_error"] == 1
    assert rejected["self_rate_limited"] == 1
    assert audit["counts"]["check_rate_limited"] == 1
    assert audit["counts"]["check_pending"] == 1
    assert audit["counts"]["check_missing"] == 1
    assert audit["counts"]["check_nonnumeric"] == 1


def test_performance_sort_dedupe_and_max_checks_happen_before_check_get(tmp_path: Path) -> None:
    inventory = [
        _alpha("active", "rank(close)", status="ACTIVE"),
        _alpha("same_expr_best", "rank(volume)", fitness=1.5, created="2026-07-19T00:00:00+00:00"),
        _alpha("same_expr_worse", " rank( volume ) ", fitness=1.2),
        _alpha("same_signature", "ts_rank(volume, 20)", fitness=1.4),
        _alpha("second", "rank(vwap)", fitness=1.3),
        _alpha("third", "rank(open)", fitness=1.1),
        _alpha("bad_sharpe", "rank(high)", sharpe=1.249),
        _alpha("bad_fitness", "rank(low)", fitness=0.999),
        _alpha("bad_turnover", "rank(returns)", turnover=0.701),
        _alpha("unsafe/id", "rank(adv20)"),
    ]
    checks = {
        "same_expr_best": _self_check("PASS", 0.5),
        "second": _self_check("PASS", 0.5),
        "third": _self_check("PASS", 0.5),
    }
    client = FakeReadOnlyClient(inventory, checks)
    _, rows, audit = _run(tmp_path, client, max_checks=2)

    assert [row["alpha_id"] for row in rows] == ["same_expr_best", "second"]
    assert audit["ids"]["checked"] == ["same_expr_best", "second"]
    assert audit["counts"]["performance_pass_unsubmitted"] == 5
    assert audit["counts"]["deduplicated_check_queue"] == 3
    assert audit["counts"]["rejected"]["duplicate_expression"] == 1
    assert audit["counts"]["rejected"]["duplicate_field_signature"] == 1
    assert audit["counts"]["rejected"]["max_checks_not_checked"] == 1
    assert audit["counts"]["rejected"]["unsafe_alpha_id"] == 1
    assert sum(path.endswith("/check") for _, path, _ in client.calls) == 2


def test_inventory_non_correlation_failure_is_rejected_before_check_get(
    tmp_path: Path,
) -> None:
    failed = _alpha("failed", "rank(volume)", fitness=1.6)
    failed["is"]["checks"] = [
        {"name": "LOW_SUB_UNIVERSE_SHARPE", "result": "FAIL"},
        {"name": "SELF_CORRELATION", "result": "PENDING"},
    ]
    allowed = _alpha("allowed", "rank(vwap)")
    allowed["is"]["checks"] = [
        {"name": "SELF_CORRELATION", "result": "FAIL", "value": 0.8},
    ]
    client = FakeReadOnlyClient(
        [_alpha("active", "rank(close)", status="ACTIVE"), failed, allowed],
        {"allowed": _self_check("PASS", 0.5)},
    )

    _, rows, audit = _run(tmp_path, client)

    assert [row["alpha_id"] for row in rows] == ["allowed"]
    assert not any(path.endswith("/failed/check") for _, path, _ in client.calls)
    assert audit["counts"]["rejected"]["platform_non_correlation_fail"] == 1
    assert sum(path.endswith("/check") for _, path, _ in client.calls) == 1


def test_selection_offset_checks_next_deterministic_window_and_is_audited(tmp_path: Path) -> None:
    inventory = [
        _alpha("first", "rank(volume)", fitness=1.4),
        _alpha("second", "rank(vwap)", fitness=1.3),
        _alpha("third", "rank(open)", fitness=1.2),
        _alpha("fourth", "rank(high)", fitness=1.1),
    ]
    checks = {
        "second": _self_check("PASS", 0.51),
        "third": _self_check("PASS", 0.52),
    }
    client = FakeReadOnlyClient(inventory, checks)

    _, rows, audit = _run(tmp_path, client, max_checks=2, selection_offset=1)

    assert [row["alpha_id"] for row in rows] == ["second", "third"]
    assert audit["ids"]["checked"] == ["second", "third"]
    assert audit["config"]["selection_offset"] == 1
    assert audit["counts"]["rejected"]["selection_offset_skipped"] == 1
    assert audit["counts"]["rejected"]["max_checks_not_checked"] == 1
    assert [path for _, path, _ in client.calls if path.endswith("/check")] == [
        "/alphas/second/check",
        "/alphas/third/check",
    ]


def test_inventory_self_priority_is_opt_in_and_still_requires_fresh_strict_check(
    tmp_path: Path,
) -> None:
    inventory = [
        _alpha("no_history", "rank(volume)", fitness=1.5),
        _alpha("historical_high", "rank(vwap)", fitness=1.4, inventory_self=0.65),
        _alpha("historical_low", "rank(open)", fitness=1.1, inventory_self=0.31),
    ]
    checks = {
        "historical_low": _self_check("FAIL", 0.81),
        "historical_high": _self_check("PASS", 0.52),
    }
    client = FakeReadOnlyClient(inventory, checks)

    _, rows, audit = _run(
        tmp_path,
        client,
        max_checks=2,
        prioritize_inventory_self=True,
    )

    assert [path for _, path, _ in client.calls if path.endswith("/check")] == [
        "/alphas/historical_low/check",
        "/alphas/historical_high/check",
    ]
    assert [row["alpha_id"] for row in rows] == ["historical_high"]
    assert audit["checks"][0]["inventory_self"] == 0.31
    assert audit["checks"][0]["current_self"]["value"] == 0.81
    assert audit["checks"][0]["accepted"] is False
    assert audit["config"]["prioritize_inventory_self"] is True
    assert audit["config"]["sort"][0] == "inventory_self_numeric_first_asc"


def test_public_audit_is_private_safe_and_binds_atomic_private_artifact(tmp_path: Path) -> None:
    expression = "rank(ts_mean(secret_field_name, 20))"
    client = FakeReadOnlyClient(
        [_alpha("active", "rank(close)", status="ACTIVE"), _alpha("safe_id", expression)],
        {"safe_id": _self_check("PASS", 0.42)},
    )
    receipt, rows, audit = _run(tmp_path, client)
    private_path = Path(receipt["outputs"]["private"]["path"])
    audit_path = Path(receipt["outputs"]["audit"]["path"])
    public_text = audit_path.read_text(encoding="utf-8")

    assert expression not in public_text
    assert "secret_field_name" not in public_text
    assert rows[0]["expression"] == expression
    assert rows[0]["field_signature"] == "secret_field_name"
    assert audit["checks"][0]["field_count"] == 1
    assert len(audit["checks"][0]["field_signature_sha256"]) == 64
    private_digest = hashlib.sha256(private_path.read_bytes()).hexdigest()
    audit_digest = hashlib.sha256(audit_path.read_bytes()).hexdigest()
    assert audit["private_output"] == {"records": 1, "sha256": private_digest}
    assert receipt["outputs"]["private"]["sha256"] == private_digest
    assert receipt["outputs"]["audit"]["sha256"] == audit_digest
    assert audit["read_only"] is True
    assert audit["submit_calls"] == 0
    assert audit["submit_attempts"] == 0
    assert not list(tmp_path.rglob("*.tmp"))


def test_checkpoint_resumes_at_next_candidate_without_exposing_private_inputs(tmp_path: Path) -> None:
    inventory = [
        _alpha("first", "rank(secret_first)", fitness=1.3),
        _alpha("second", "rank(secret_second)", fitness=1.2),
        _alpha("third", "rank(secret_third)", fitness=1.1),
    ]
    checks = {
        "first": _self_check("PASS", 0.51),
        "second": _self_check("PASS", 0.6),
        "third": _self_check("PASS", 0.52),
    }
    checkpoint = tmp_path / "state" / "screen.checkpoint.json"
    config = screener.ScreenConfig(
        output_private=tmp_path / "final" / "current.private.jsonl",
        output_audit=tmp_path / "final" / "current.audit.json",
        checkpoint=checkpoint,
        page_size=2,
    )
    interrupted = InterruptingReadOnlyClient(inventory, checks, fail_on="third")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        screener.screen_current_self(interrupted, config, sleep_fn=lambda _seconds: None)

    private_checkpoint = checkpoint.with_name("screen.checkpoint.private.json")
    public_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    private_payload = json.loads(private_checkpoint.read_text(encoding="utf-8"))
    public_text = checkpoint.read_text(encoding="utf-8")
    assert public_payload["next_index"] == 2
    assert public_payload["run"]["selected_ids"] == ["first", "second", "third"]
    assert public_payload["run"]["inventory_snapshot"]["items"] == [
        {"alpha_id": "first", "status": "UNSUBMITTED"},
        {"alpha_id": "second", "status": "UNSUBMITTED"},
        {"alpha_id": "third", "status": "UNSUBMITTED"},
    ]
    assert public_payload["accepted_ids"] == ["first"]
    assert all(secret not in public_text for secret in ("secret_first", "secret_second", "secret_third"))
    assert '"expression"' not in public_text
    assert private_payload["accepted_rows"][0]["expression"] == "rank(secret_first)"
    assert public_payload["private_checkpoint_sha256"] == hashlib.sha256(private_checkpoint.read_bytes()).hexdigest()
    assert not list(tmp_path.rglob("*.tmp"))

    resumed = FakeReadOnlyClient(inventory, checks)
    receipt = screener.screen_current_self(resumed, replace(config, resume=True), sleep_fn=lambda _seconds: None)

    check_ids = [path.split("/")[2] for _, path, _ in resumed.calls if path.endswith("/check")]
    assert check_ids == ["third"]
    final_rows = [
        json.loads(line)
        for line in Path(receipt["outputs"]["private"]["path"]).read_text(encoding="utf-8").splitlines()
        if line
    ]
    final_audit = json.loads(Path(receipt["outputs"]["audit"]["path"]).read_text(encoding="utf-8"))
    assert [row["alpha_id"] for row in final_rows] == ["first", "third"]
    assert final_audit["ids"]["checked"] == ["first", "second", "third"]
    assert final_audit["counts"]["checked"] == 3
    assert final_audit["counts"]["rejected"]["self_not_strictly_below_cutoff"] == 1
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["completed"] is True


def test_resume_rejects_config_or_inventory_binding_drift_before_any_check(tmp_path: Path) -> None:
    inventory = [_alpha("only", "rank(private_input)")]
    checks = {"only": _self_check("PASS", 0.5)}
    config = screener.ScreenConfig(
        output_private=tmp_path / "final.private.jsonl",
        output_audit=tmp_path / "final.audit.json",
        checkpoint=tmp_path / "checkpoint.json",
    )
    screener.screen_current_self(FakeReadOnlyClient(inventory, checks), config)

    config_drift_client = FakeReadOnlyClient(inventory, checks)
    with pytest.raises(ValueError, match="binding changed"):
        screener.screen_current_self(
            config_drift_client,
            replace(config, resume=True, self_corr_cutoff=0.59),
        )
    assert not any(path.endswith("/check") for _, path, _ in config_drift_client.calls)

    offset_drift_client = FakeReadOnlyClient(inventory, checks)
    with pytest.raises(ValueError, match="binding changed"):
        screener.screen_current_self(
            offset_drift_client,
            replace(config, resume=True, selection_offset=1),
        )
    assert not any(path.endswith("/check") for _, path, _ in offset_drift_client.calls)

    inventory_drift_client = FakeReadOnlyClient(
        [_alpha("replacement", "rank(private_input)")],
        {"replacement": _self_check("PASS", 0.5)},
    )
    with pytest.raises(ValueError, match="binding changed"):
        screener.screen_current_self(inventory_drift_client, replace(config, resume=True))
    assert not any(path.endswith("/check") for _, path, _ in inventory_drift_client.calls)


def test_resume_recovers_when_private_checkpoint_is_one_atomic_write_ahead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = [
        _alpha("first", "rank(private_one)", fitness=1.2),
        _alpha("second", "rank(private_two)", fitness=1.1),
    ]
    checks = {"first": _self_check("PASS", 0.5), "second": _self_check("PASS", 0.51)}
    checkpoint = tmp_path / "checkpoint.json"
    config = screener.ScreenConfig(
        output_private=tmp_path / "final.private.jsonl",
        output_audit=tmp_path / "final.audit.json",
        checkpoint=checkpoint,
    )
    real_atomic_write = screener._atomic_write

    def interrupt_second_public_write(path: Path, content: bytes) -> None:
        payload = json.loads(content.decode("utf-8"))
        if Path(path) == checkpoint and payload.get("next_index") == 2:
            raise RuntimeError("simulated crash between checkpoint pair writes")
        real_atomic_write(path, content)

    monkeypatch.setattr(screener, "_atomic_write", interrupt_second_public_write)
    with pytest.raises(RuntimeError, match="checkpoint pair"):
        screener.screen_current_self(FakeReadOnlyClient(inventory, checks), config)
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["next_index"] == 1
    assert json.loads(checkpoint.with_name("checkpoint.private.json").read_text(encoding="utf-8"))["next_index"] == 2

    monkeypatch.setattr(screener, "_atomic_write", real_atomic_write)
    resumed = FakeReadOnlyClient(inventory, checks)
    receipt = screener.screen_current_self(resumed, replace(config, resume=True))
    assert not any(path.endswith("/check") for _, path, _ in resumed.calls)
    assert receipt["counts"]["accepted_strict_self"] == 2
    repaired_public = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert repaired_public["next_index"] == 2
    assert repaired_public["completed"] is True
    assert not list(tmp_path.rglob("*.tmp"))


def test_configured_polling_and_between_check_throttle_are_forwarded(tmp_path: Path) -> None:
    client = FakeReadOnlyClient(
        [
            _alpha("first", "rank(volume)", fitness=1.3),
            _alpha("second", "rank(vwap)", fitness=1.2),
        ],
        {
            "first": _self_check("PASS", 0.51),
            "second": _self_check("PASS", 0.52),
        },
    )
    sleeps: list[float] = []
    _, rows, audit = _run(
        tmp_path,
        client,
        check_polls=12,
        check_interval=10,
        between_checks=3,
        sleep_fn=sleeps.append,
    )

    check_calls = [call for call in client.calls if call[1].endswith("/check")]
    assert len(rows) == 2
    assert [call[2] for call in check_calls] == [
        {"max_polls": 12, "interval": 10},
        {"max_polls": 12, "interval": 10},
    ]
    assert sleeps == [3]
    assert audit["config"]["check_max_polls"] == 12
    assert audit["config"]["check_sleep_seconds"] == 10
    assert audit["config"]["between_checks_seconds"] == 3
    assert audit["submit_calls"] == 0
    assert audit["submit_attempts"] == 0


def test_round_robin_polling_is_explicit_and_polls_each_unresolved_candidate_once_per_round(
    tmp_path: Path,
) -> None:
    client = SequencedReadOnlyClient(
        [
            _alpha("pending_then_pass", "rank(volume)", fitness=1.4),
            _alpha("rate_then_pass", "rank(vwap)", fitness=1.3),
            _alpha("terminal_first_round", "rank(open)", fitness=1.2),
        ],
        {
            "pending_then_pass": [_self_check("PENDING", None), _self_check("PASS", 0.51)],
            "rate_then_pass": [
                {"ok": False, "status_code": 429, "error": "rate limited"},
                _self_check("PASS", 0.52),
            ],
            "terminal_first_round": [_self_check("PASS", 0.53)],
        },
    )
    sleeps: list[float] = []

    _, rows, audit = _run(
        tmp_path,
        client,
        check_polls=2,
        check_interval=10,
        between_checks=3,
        round_robin_polling=True,
        sleep_fn=sleeps.append,
    )

    check_calls = [call for call in client.calls if call[1].endswith("/check")]
    assert [call[1].split("/")[2] for call in check_calls] == [
        "pending_then_pass",
        "rate_then_pass",
        "terminal_first_round",
        "pending_then_pass",
        "rate_then_pass",
    ]
    assert all(call[2] == {"max_polls": 1, "interval": 10} for call in check_calls)
    assert sleeps == [3, 60.0, 10, 3]
    assert [row["alpha_id"] for row in rows] == [
        "pending_then_pass",
        "rate_then_pass",
        "terminal_first_round",
    ]
    assert [row["poll_attempts"] for row in audit["checks"]] == [2, 2, 1]
    assert audit["config"]["polling_mode"] == "round_robin"
    assert audit["http_receipt"]["check_get_calls"] == 5
    assert audit["http_receipt"]["check_get_calls_lower_bound"] == 5
    assert audit["http_receipt"]["check_get_calls_upper_bound"] == 5
    assert audit["counts"]["check_get_reservations"] == 5
    assert audit["counts"]["check_get_responses_recorded"] == 5
    assert audit["counts"]["check_rate_limit_responses"] == 1
    assert audit["counts"]["check_rate_limit_consecutive"] == 0
    assert audit["counts"]["check_rate_limit_cooldown_seconds_pending"] == 0.0
    assert audit["submit_calls"] == 0
    assert audit["submit_attempts"] == 0

    assert screener._parse_args(["--output-private", "p", "--output-audit", "a"]).round_robin_polling is False
    assert (
        screener._parse_args(
            ["--output-private", "p", "--output-audit", "a", "--round-robin-polling"]
        ).round_robin_polling
        is True
    )
    assert (
        screener._parse_args(
            ["--output-private", "p", "--output-audit", "a", "--prioritize-inventory-self"]
        ).prioritize_inventory_self
        is True
    )


def test_round_robin_checkpoint_reserves_before_get_and_resume_does_not_replay_lost_poll(
    tmp_path: Path,
) -> None:
    inventory = [
        _alpha("first", "rank(private_first)", fitness=1.3),
        _alpha("second", "rank(private_second)", fitness=1.2),
    ]
    checks = {"first": _self_check("PASS", 0.51), "second": _self_check("PASS", 0.52)}
    checkpoint = tmp_path / "state" / "round-robin.checkpoint.json"
    config = screener.ScreenConfig(
        output_private=tmp_path / "final" / "accepted.private.jsonl",
        output_audit=tmp_path / "final" / "screen.audit.json",
        checkpoint=checkpoint,
        check_polls=2,
        check_interval=10,
        between_checks=3,
        round_robin_polling=True,
    )
    interrupted = InterruptingReadOnlyClient(inventory, checks, fail_on="first")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        screener.screen_current_self(interrupted, config, sleep_fn=lambda _seconds: None)

    public_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    public_text = checkpoint.read_text(encoding="utf-8")
    assert public_payload["next_index"] == 0
    assert public_payload["round_robin_state"] == {
        "round_index": 0,
        "cursor": 1,
        "attempts_reserved": [1, 0],
        "responses_recorded": [0, 0],
        "rate_limit": {
            "consecutive_429": 0,
            "total_429": 0,
            "last_retry_after_seconds": None,
            "cooldown_seconds_pending": 0.0,
        },
    }
    assert all(secret not in public_text for secret in ("private_first", "private_second"))
    assert '"expression"' not in public_text

    resumed = FakeReadOnlyClient(inventory, checks)
    sleeps: list[float] = []
    receipt = screener.screen_current_self(
        resumed,
        replace(config, resume=True),
        sleep_fn=sleeps.append,
    )

    check_ids = [path.split("/")[2] for _, path, _ in resumed.calls if path.endswith("/check")]
    assert check_ids == ["second", "first"]
    assert sleeps == [3, 10]
    final_rows = [
        json.loads(line)
        for line in Path(receipt["outputs"]["private"]["path"]).read_text(encoding="utf-8").splitlines()
        if line
    ]
    final_audit = json.loads(Path(receipt["outputs"]["audit"]["path"]).read_text(encoding="utf-8"))
    assert [row["alpha_id"] for row in final_rows] == ["first", "second"]
    assert final_audit["ids"]["checked"] == ["first", "second"]
    assert final_audit["counts"]["check_get_reservations"] == 3
    assert final_audit["counts"]["check_get_responses_recorded"] == 2
    assert final_audit["counts"]["check_get_calls_exact"] is None
    assert final_audit["http_receipt"]["check_get_calls"] is None
    assert final_audit["http_receipt"]["check_get_calls_lower_bound"] == 2
    assert final_audit["http_receipt"]["check_get_calls_upper_bound"] == 3
    completed = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert completed["completed"] is True
    assert completed["round_robin_state"]["attempts_reserved"] == [2, 1]
    assert completed["round_robin_state"]["responses_recorded"] == [1, 1]
    assert not list(tmp_path.rglob("*.tmp"))


def test_round_robin_persists_server_or_exponential_429_cooldown_before_next_get(
    tmp_path: Path,
) -> None:
    inventory = [
        _alpha("first", "rank(first_field)", fitness=1.3),
        _alpha("second", "rank(second_field)", fitness=1.2),
    ]
    checkpoint = tmp_path / "state" / "rate-limit.checkpoint.json"
    config = screener.ScreenConfig(
        output_private=tmp_path / "final" / "accepted.private.jsonl",
        output_audit=tmp_path / "final" / "screen.audit.json",
        checkpoint=checkpoint,
        check_polls=2,
        check_interval=10,
        round_robin_polling=True,
    )
    interrupted = SequencedReadOnlyClient(
        inventory,
        {
            "first": [
                {
                    "ok": False,
                    "status_code": 429,
                    "failure_kind": "rate_limited",
                    "retry_after_seconds": 120.0,
                }
            ],
            "second": [_self_check("PASS", 0.52)],
        },
    )

    def interrupt_during_cooldown(seconds: float) -> None:
        assert seconds == 120.0
        raise RuntimeError("cooldown interrupted")

    with pytest.raises(RuntimeError, match="cooldown interrupted"):
        screener.screen_current_self(interrupted, config, sleep_fn=interrupt_during_cooldown)

    check_ids = [path.split("/")[2] for _, path, _ in interrupted.calls if path.endswith("/check")]
    assert check_ids == ["first"]
    public_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert public_payload["round_robin_state"]["cursor"] == 1
    assert public_payload["round_robin_state"]["attempts_reserved"] == [1, 0]
    assert public_payload["round_robin_state"]["responses_recorded"] == [1, 0]
    assert public_payload["round_robin_state"]["rate_limit"] == {
        "consecutive_429": 1,
        "total_429": 1,
        "last_retry_after_seconds": 120.0,
        "cooldown_seconds_pending": 120.0,
    }

    resumed = SequencedReadOnlyClient(
        inventory,
        {
            "first": [_self_check("PASS", 0.51)],
            "second": [_self_check("PASS", 0.52)],
        },
    )
    sleeps: list[float] = []
    receipt = screener.screen_current_self(
        resumed,
        replace(config, resume=True),
        sleep_fn=sleeps.append,
    )

    assert sleeps == [120.0, 10]
    check_ids = [path.split("/")[2] for _, path, _ in resumed.calls if path.endswith("/check")]
    assert check_ids == ["second", "first"]
    audit = json.loads(Path(receipt["outputs"]["audit"]["path"]).read_text(encoding="utf-8"))
    assert audit["counts"]["check_rate_limit_responses"] == 1
    assert audit["counts"]["check_rate_limit_consecutive"] == 0
    assert audit["counts"]["check_rate_limit_cooldown_seconds_pending"] == 0.0


def test_round_robin_resume_migrates_legacy_checkpoint_without_rate_limit_state(
    tmp_path: Path,
) -> None:
    inventory = [
        _alpha("first", "rank(legacy_first)", fitness=1.3),
        _alpha("second", "rank(legacy_second)", fitness=1.2),
    ]
    checks = {"first": _self_check("PASS", 0.51), "second": _self_check("PASS", 0.52)}
    checkpoint = tmp_path / "state" / "legacy.checkpoint.json"
    config = screener.ScreenConfig(
        output_private=tmp_path / "final" / "accepted.private.jsonl",
        output_audit=tmp_path / "final" / "screen.audit.json",
        checkpoint=checkpoint,
        check_polls=2,
        check_interval=10,
        round_robin_polling=True,
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        screener.screen_current_self(
            InterruptingReadOnlyClient(inventory, checks, fail_on="first"),
            config,
            sleep_fn=lambda _seconds: None,
        )

    private_path = screener._checkpoint_private_path(config)
    private_payload = json.loads(private_path.read_text(encoding="utf-8"))
    private_payload.pop("payload_sha256")
    private_payload["round_robin_state"].pop("rate_limit")
    private_payload["progress_sha256"] = screener._round_robin_progress_sha256(
        state=private_payload["round_robin_state"],
        public_checked=private_payload["public_checked"],
        accepted_ids=[str(row["alpha_id"]) for row in private_payload["accepted_rows"]],
    )
    private_bytes = screener._checkpoint_bytes(private_payload)
    private_path.write_bytes(private_bytes)

    public_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    public_payload.pop("payload_sha256")
    public_payload["round_robin_state"].pop("rate_limit")
    public_payload["progress_sha256"] = screener._round_robin_progress_sha256(
        state=public_payload["round_robin_state"],
        public_checked=public_payload["public_checked"],
        accepted_ids=public_payload["accepted_ids"],
    )
    public_payload["private_checkpoint_sha256"] = hashlib.sha256(private_bytes).hexdigest()
    checkpoint.write_bytes(screener._checkpoint_bytes(public_payload))

    receipt = screener.screen_current_self(
        FakeReadOnlyClient(inventory, checks),
        replace(config, resume=True, inventory_page_interval=5),
        sleep_fn=lambda _seconds: None,
    )

    assert receipt["ok"] is True
    migrated = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert migrated["round_robin_state"]["rate_limit"] == {
        "consecutive_429": 0,
        "total_429": 0,
        "last_retry_after_seconds": None,
        "cooldown_seconds_pending": 0.0,
    }


def test_inventory_page_interval_paces_only_between_inventory_gets(tmp_path: Path) -> None:
    inventory = [
        _alpha("active", "rank(close)", status="ACTIVE"),
        _alpha("one", "rank(volume)"),
        _alpha("two", "rank(vwap)"),
        _alpha("three", "rank(open)"),
        _alpha("four", "rank(high)"),
    ]
    sleeps: list[float] = []

    _, _, audit = _run(
        tmp_path,
        FakeReadOnlyClient(inventory, {}),
        max_checks=0,
        inventory_page_interval=5,
        sleep_fn=sleeps.append,
    )

    assert sleeps == [5, 5]
    assert audit["config"]["inventory_page_interval_seconds"] == 5
    assert audit["http_receipt"]["inventory_get_calls"] == 3


def test_inventory_fetch_retries_same_page_with_bounded_backoff() -> None:
    inventory = [_alpha(f"alpha-{index}", f"rank(field_{index})") for index in range(5)]
    client = FlakyInventoryClient(inventory, fail_once_at_offset=2)
    sleeps: list[float] = []

    rows, get_calls = screener._fetch_full_inventory(
        client,
        page_size=2,
        page_interval=0,
        sleep_fn=sleeps.append,
    )

    assert len(rows) == 5
    assert get_calls == 4
    assert sleeps == [1.0]
    assert [call[2]["offset"] for call in client.calls] == [0, 2, 2, 4]


def test_inventory_fetch_stops_at_platform_display_cap() -> None:
    inventory = [
        _alpha(f"alpha-{index}", f"rank(field_{index})")
        for index in range(screener.INVENTORY_DISPLAY_CAP + 5)
    ]
    client = FakeReadOnlyClient(inventory, {})

    rows, get_calls = screener._fetch_full_inventory(
        client,
        page_size=100,
        page_interval=0,
        sleep_fn=lambda _seconds: None,
    )

    assert len(rows) == screener.INVENTORY_DISPLAY_CAP
    assert get_calls == 10
    assert client.calls[-1][2] == {
        "limit": 100,
        "offset": 900,
        "order": "-dateCreated",
    }


def test_inventory_fetch_respects_smaller_explicit_cap() -> None:
    inventory = [_alpha(f"alpha-{index}", f"rank(field_{index})") for index in range(20)]
    client = FakeReadOnlyClient(inventory, {})

    rows, get_calls = screener._fetch_full_inventory(
        client,
        page_size=5,
        display_cap=12,
        page_interval=0,
        sleep_fn=lambda _seconds: None,
    )

    assert len(rows) == 12
    assert get_calls == 3
    assert client.calls[-1][2] == {
        "limit": 5,
        "offset": 10,
        "order": "-dateCreated",
    }
