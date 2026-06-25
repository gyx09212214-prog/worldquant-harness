import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from worldquant_harness.models import Base, SubmittedAlpha, WQAlphaExperiment, WQFailureMemory
from worldquant_harness.wq_alpha_ledger import (
    record_api_check_record,
    record_find_only_entry,
    record_submitted_alpha_in_ledger,
    should_block_expression,
)
from worldquant_harness.wq_failure_memory import classify_failures, lifecycle_status


@pytest_asyncio.fixture
async def ledger_factory():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def test_failure_classifier_blocks_correlation_and_marks_pending_as_non_failure():
    self_fail = {
        "status": "failed_correlation_check",
        "expression": "rank(close)",
        "self_correlation": {"name": "SELF_CORRELATION", "result": "FAIL", "value": 0.8457, "limit": 0.7},
    }
    failures = classify_failures(self_fail)
    assert failures == [{
        "failure_kind": "self_correlation_fail",
        "severity": "block",
        "reason": "SELF_CORRELATION failed",
        "value": 0.8457,
        "limit": 0.7,
    }]
    assert lifecycle_status(self_fail) == "self_corr_fail"

    pending = {
        "api_check_status": "api_check_pending",
        "expression": "rank(open)",
        "sc_result": "PENDING",
    }
    assert classify_failures(pending) == []
    assert lifecycle_status(pending) == "correlation_pending"


def test_failure_classifier_penalizes_platform_metric_failures():
    record = {
        "status": "failed_platform_check",
        "expression": "rank(volume)",
        "failed_platform_checks": [
            {"name": "LOW_FITNESS", "result": "FAIL", "value": 0.4, "limit": 1.0},
            {"name": "CONCENTRATED_WEIGHT", "result": "FAIL", "value": 0.5, "limit": 0.1},
        ],
    }
    failures = classify_failures(record)
    assert [item["failure_kind"] for item in failures] == ["low_fitness", "concentrated_weight"]
    assert {item["severity"] for item in failures} == {"penalize"}


@pytest.mark.asyncio
async def test_record_find_only_entry_is_idempotent_and_records_failure_memory(ledger_factory):
    entry = {
        "status": "failed_correlation_check",
        "expression": "rank(close)",
        "alpha_id": "alpha_sc",
        "submit_eligible": True,
        "self_correlation": {"name": "SELF_CORRELATION", "result": "FAIL", "value": 0.82, "limit": 0.7},
    }

    async with ledger_factory() as session:
        await record_find_only_entry(session, entry, source_run_id="run1")
        await record_find_only_entry(session, entry, source_run_id="run1")
        await session.commit()

        experiment_count = await session.scalar(select(func.count()).select_from(WQAlphaExperiment))
        memory_count = await session.scalar(select(func.count()).select_from(WQFailureMemory))
        exp = (await session.execute(select(WQAlphaExperiment))).scalar_one()
        memory = (await session.execute(select(WQFailureMemory))).scalar_one()

    assert experiment_count == 1
    assert memory_count == 1
    assert exp.lifecycle_status == "self_corr_fail"
    assert exp.failure_kind == "self_correlation_fail"
    assert memory.failure_kind == "self_correlation_fail"
    assert memory.severity == "block"
    assert memory.evidence_count == 2


@pytest.mark.asyncio
async def test_api_check_updates_existing_experiment_without_submit(ledger_factory):
    entry = {
        "status": "eligible",
        "expression": "rank(open)",
        "alpha_id": "alpha_pending",
        "submit_eligible": True,
        "submitted": False,
    }
    api_record = {
        "alpha_id": "alpha_pending",
        "expression": "rank(open)",
        "api_check_status": "api_check_pending",
        "platform_status": "UNSUBMITTED",
        "sc_result": "PENDING",
        "source_submit_eligible": True,
    }

    async with ledger_factory() as session:
        await record_find_only_entry(session, entry, source_run_id="run2")
        await record_api_check_record(session, api_record, source_run_id="run2")
        await session.commit()

        experiment_count = await session.scalar(select(func.count()).select_from(WQAlphaExperiment))
        exp = (await session.execute(select(WQAlphaExperiment))).scalar_one()

    assert experiment_count == 1
    assert exp.lifecycle_status == "correlation_pending"
    assert exp.api_check_status == "api_check_pending"
    assert exp.platform_status == "UNSUBMITTED"


@pytest.mark.asyncio
async def test_should_block_expression_uses_blocking_failure_memory(ledger_factory):
    entry = {
        "status": "failed_correlation_check",
        "expression": "rank(close / open)",
        "alpha_id": "alpha_blocked",
        "self_correlation": {"name": "SELF_CORRELATION", "result": "FAIL", "value": 0.84, "limit": 0.7},
    }

    async with ledger_factory() as session:
        await record_find_only_entry(session, entry, source_run_id="run3")
        await session.commit()

        block = await should_block_expression(
            session,
            "rank(close / open)",
            threshold=0.70,
        )

    assert block["blocked"] is True
    assert block["reasons"][0]["failure_kind"] in {"self_correlation_fail", "high_similarity"}


@pytest.mark.asyncio
async def test_record_submitted_alpha_mirrors_to_submitted_alpha_table(ledger_factory):
    user_id = "11111111-1111-1111-1111-111111111111"

    async with ledger_factory() as session:
        await record_submitted_alpha_in_ledger(
            session,
            user_id=user_id,
            alpha_id="active_alpha",
            expression="rank(open)",
            sharpe=1.8,
            fitness=1.2,
            turnover=0.2,
            status="active",
        )
        await record_submitted_alpha_in_ledger(
            session,
            user_id=user_id,
            alpha_id="active_alpha",
            expression="rank(open)",
            sharpe=1.9,
            fitness=1.3,
            turnover=0.22,
            status="active",
        )
        await session.commit()

        experiment_count = await session.scalar(select(func.count()).select_from(WQAlphaExperiment))
        submitted_count = await session.scalar(select(func.count()).select_from(SubmittedAlpha))
        memory_count = await session.scalar(select(func.count()).select_from(WQFailureMemory))
        submitted = (await session.execute(select(SubmittedAlpha))).scalar_one()

    assert experiment_count == 1
    assert submitted_count == 1
    assert memory_count == 1
    assert submitted.alpha_id == "active_alpha"
    assert submitted.status == "active"
    assert submitted.fitness == 1.3
