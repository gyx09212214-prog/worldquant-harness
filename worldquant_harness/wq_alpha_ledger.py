"""Database-backed ledger for WorldQuant alpha experiments."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .async_utils import run_coro_sync
from .expression_parser import normalize_expression
from .models import SubmittedAlpha, WQAlphaExperiment, WQFailureMemory
from .record_utils import first_float as _first_float
from .record_utils import nested as _nested
from .wq_failure_memory import (
    classify_failures,
    expression_components,
    expression_hash,
    lifecycle_status,
    memory_specs_for_record,
    normalized_settings,
    params_hash,
    pattern_signature,
    primary_failure_kind,
)
from .wq_similarity import compute_similarity

logger = logging.getLogger(__name__)

BLOCKING_LEDGER_STATUSES = {
    "submitted",
    "active",
    "self_corr_fail",
    "prod_corr_fail",
    "skipped_similar",
}
STATUS_RANK = {
    "candidate": 0,
    "simulated": 1,
    "weak": 2,
    "invalid": 2,
    "api_check_failed": 2,
    "correlation_pending": 3,
    "pre_submit_pass": 4,
    "skipped_similar": 5,
    "self_corr_fail": 6,
    "prod_corr_fail": 6,
    "submitted": 7,
    "active": 8,
}

_ensured_db = False


async def record_find_only_entry(
    session: AsyncSession,
    entry: dict,
    *,
    settings: dict | None = None,
    source_run_id: str | None = None,
    source_file: str | None = None,
    source_type: str | None = None,
    user_id: str | uuid.UUID | None = None,
) -> WQAlphaExperiment | None:
    """Upsert one find-only/candidate result and associated failure memory."""
    expression = str(entry.get("expression") or "").strip()
    if not expression:
        return None

    settings = normalized_settings({**(settings or {}), "account": (settings or {}).get("account") or entry.get("account")})
    exp = await _upsert_experiment(
        session,
        entry,
        settings=settings,
        source_run_id=source_run_id,
        source_file=source_file,
        source_type=source_type or _source_type_for_entry(entry),
        user_id=user_id,
    )
    await session.flush()
    await record_failure_memory_for_record(session, entry, exp)
    return exp


async def record_api_check_record(
    session: AsyncSession,
    record: dict,
    *,
    settings: dict | None = None,
    source_run_id: str | None = None,
    user_id: str | uuid.UUID | None = None,
) -> WQAlphaExperiment | None:
    """Upsert one read-only API check record and associated failure memory."""
    expression = str(record.get("expression") or "").strip()
    if not expression and not record.get("alpha_id"):
        return None

    settings = normalized_settings(settings)
    exp = await _find_existing_for_api_record(session, record, settings, source_run_id)
    if exp is None and expression:
        exp = await _upsert_experiment(
            session,
            record,
            settings=settings,
            source_run_id=source_run_id or _infer_source_run_id(record.get("source_file")),
            source_file=record.get("source_file"),
            source_type="api_check",
            user_id=user_id,
        )
    elif exp is not None:
        _apply_record_to_experiment(exp, record, settings, source_type="api_check")
        if source_run_id and not exp.source_run_id:
            exp.source_run_id = source_run_id

    if exp is None:
        return None

    await session.flush()
    await record_failure_memory_for_record(session, record, exp)
    return exp


async def record_submitted_alpha_in_ledger(
    session: AsyncSession,
    *,
    user_id: str | uuid.UUID | None,
    alpha_id: str,
    expression: str,
    region: str = "USA",
    universe: str = "TOP3000",
    delay: int = 1,
    decay: int = 0,
    neutralization: str = "SUBINDUSTRY",
    truncation: float = 0.08,
    sharpe: float | None = None,
    fitness: float | None = None,
    returns: float | None = None,
    turnover: float | None = None,
    tag: str | None = None,
    status: str = "submitted",
) -> WQAlphaExperiment | None:
    """Mirror a real submitted alpha into the ledger and block rediscovery."""
    status = str(status or "submitted").lower()
    record = {
        "alpha_id": alpha_id,
        "expression": expression,
        "tag": tag,
        "status": status,
        "submit_eligible": True,
        "sharpe": sharpe,
        "fitness": fitness,
        "returns": returns,
        "turnover": turnover,
        "api_check_status": "platform_active_check_readable" if status == "active" else None,
    }
    settings = normalized_settings({
        "region": region,
        "universe": universe,
        "delay": delay,
        "decay": decay,
        "neutralization": neutralization,
        "truncation": truncation,
    })
    exp = await _upsert_experiment(
        session,
        record,
        settings=settings,
        source_run_id="submitted_alpha",
        source_type="submitted_alpha",
        user_id=user_id,
    )
    exp.lifecycle_status = status
    uid = _submitted_alpha_user_id(user_id)
    submitted = None
    if uid is not None:
        result = await session.execute(
            select(SubmittedAlpha)
            .where(SubmittedAlpha.user_id == uid, SubmittedAlpha.alpha_id == str(alpha_id))
            .limit(1)
        )
        submitted = result.scalar_one_or_none()
        if submitted is None:
            submitted = SubmittedAlpha(user_id=uid, alpha_id=str(alpha_id))
            session.add(submitted)
        submitted.expression = expression
        submitted.expression_normalized = normalize_expression(expression)
        submitted.region = settings["region"]
        submitted.universe = settings["universe"]
        submitted.delay = settings["delay"]
        submitted.decay = settings["decay"]
        submitted.neutralization = settings["neutralization"]
        submitted.truncation = settings["truncation"]
        submitted.tag = tag
        submitted.sharpe = sharpe
        submitted.fitness = fitness
        submitted.returns = returns
        submitted.turnover = turnover
        submitted.status = status
    await session.flush()
    await _record_memory_specs(session, [{
        "memory_type": "platform_alpha",
        "scope": "global",
        "expression": expression,
        "expression_normalized": normalize_expression(expression),
        "expression_hash": expression_hash(expression),
        "pattern_signature": pattern_signature(expression),
        "fields": expression_components(expression)["fields"],
        "operators": expression_components(expression)["operators"],
        "failure_kind": "platform_alpha",
        "severity": "block",
        "evidence": {"alpha_id": alpha_id, "status": status, "reason": "real submitted alpha"},
        "source_experiment_ids": [str(exp.id)],
    }], user_id=user_id, experiment_id=str(exp.id))
    return exp


async def record_failure_memory_for_record(
    session: AsyncSession,
    record: dict,
    experiment: WQAlphaExperiment,
) -> list[WQFailureMemory]:
    specs = memory_specs_for_record(record, experiment_id=str(experiment.id))
    if not specs:
        return []
    memories = await _record_memory_specs(session, specs, user_id=experiment.user_id, experiment_id=str(experiment.id))
    failures = classify_failures(record)
    if failures:
        experiment.failure_kind = primary_failure_kind(record)
        experiment.failure_reasons = failures
    return memories


async def should_block_expression(
    session: AsyncSession,
    expression: str,
    *,
    settings: dict | None = None,
    user_id: str | uuid.UUID | None = None,
    threshold: float = 0.70,
) -> dict:
    """Return whether an expression should be skipped before simulation."""
    expression = str(expression or "").strip()
    if not expression:
        return {"blocked": True, "reasons": [{"failure_kind": "validation_error", "reason": "empty expression"}]}

    uid = _coerce_uuid(user_id)
    expr_hash = expression_hash(expression)
    settings = normalized_settings(settings)
    reasons: list[dict] = []
    candidates: list[dict] = []

    exact_memory_stmt = select(WQFailureMemory).where(
        WQFailureMemory.severity == "block",
        WQFailureMemory.expression_hash == expr_hash,
    )
    if uid is not None:
        exact_memory_stmt = exact_memory_stmt.where(or_(WQFailureMemory.user_id == uid, WQFailureMemory.user_id.is_(None)))
    exact_memories = (await session.execute(exact_memory_stmt)).scalars().all()
    for memory in exact_memories:
        reasons.append({
            "source": "failure_memory",
            "failure_kind": memory.failure_kind,
            "severity": memory.severity,
            "expression": memory.expression,
            "exact_match": True,
        })

    submitted_stmt = select(SubmittedAlpha)
    if uid is not None:
        submitted_stmt = submitted_stmt.where(SubmittedAlpha.user_id == uid)
    for alpha in (await session.execute(submitted_stmt)).scalars().all():
        candidates.append({
            "source": "submitted_alpha",
            "alpha_id": alpha.alpha_id,
            "expression": alpha.expression,
            "failure_kind": "platform_alpha",
        })

    memory_stmt = select(WQFailureMemory).where(
        WQFailureMemory.severity == "block",
        WQFailureMemory.expression.is_not(None),
    )
    if uid is not None:
        memory_stmt = memory_stmt.where(or_(WQFailureMemory.user_id == uid, WQFailureMemory.user_id.is_(None)))
    for memory in (await session.execute(memory_stmt)).scalars().all():
        candidates.append({
            "source": "failure_memory",
            "alpha_id": None,
            "expression": memory.expression,
            "failure_kind": memory.failure_kind,
        })

    experiment_stmt = select(WQAlphaExperiment).where(
        WQAlphaExperiment.lifecycle_status.in_(sorted(BLOCKING_LEDGER_STATUSES)),
        WQAlphaExperiment.expression.is_not(None),
    )
    if uid is not None:
        experiment_stmt = experiment_stmt.where(or_(WQAlphaExperiment.user_id == uid, WQAlphaExperiment.user_id.is_(None)))
    for exp in (await session.execute(experiment_stmt)).scalars().all():
        candidates.append({
            "source": "ledger",
            "alpha_id": exp.alpha_id,
            "expression": exp.expression,
            "failure_kind": exp.failure_kind or exp.lifecycle_status,
        })

    nearest = _nearest_similarity(expression, candidates)
    if nearest and nearest["similarity"]["overall_similarity"] >= threshold:
        reasons.append({
            "source": nearest["source"],
            "failure_kind": "high_similarity",
            "severity": "block",
            "alpha_id": nearest.get("alpha_id"),
            "expression": nearest.get("expression"),
            "similarity": nearest["similarity"],
            "threshold": threshold,
        })

    return {
        "blocked": bool(reasons),
        "reasons": reasons,
        "nearest": nearest,
        "settings": settings,
    }


async def query_presubmit_candidates(
    session: AsyncSession,
    *,
    limit: int = 20,
    include_pending: bool = False,
) -> list[WQAlphaExperiment]:
    statuses = ["pre_submit_pass"]
    if include_pending:
        statuses.append("correlation_pending")
    result = await session.execute(
        select(WQAlphaExperiment)
        .where(WQAlphaExperiment.lifecycle_status.in_(statuses))
        .order_by(WQAlphaExperiment.fitness.desc().nullslast(), WQAlphaExperiment.sharpe.desc().nullslast())
        .limit(limit)
    )
    return list(result.scalars().all())


async def query_alpha_experiment_rows(
    session: AsyncSession,
    *,
    statuses: list[str] | tuple[str, ...],
    limit: int = 50,
    require_alpha_id: bool = True,
    user_id: str | uuid.UUID | None = None,
) -> list[dict]:
    """Return ledger experiment rows in a JSONL-friendly shape for follow-up checks."""
    stmt = select(WQAlphaExperiment).where(WQAlphaExperiment.lifecycle_status.in_(list(statuses)))
    if require_alpha_id:
        stmt = stmt.where(WQAlphaExperiment.alpha_id.is_not(None))
    uid = _coerce_uuid(user_id)
    if uid is not None:
        stmt = stmt.where(or_(WQAlphaExperiment.user_id == uid, WQAlphaExperiment.user_id.is_(None)))
    stmt = (
        stmt.order_by(
            WQAlphaExperiment.last_checked_at.asc().nullsfirst(),
            WQAlphaExperiment.fitness.desc().nullslast(),
            WQAlphaExperiment.sharpe.desc().nullslast(),
        )
        .limit(max(1, limit))
    )
    result = await session.execute(stmt)
    return [_experiment_row(exp) for exp in result.scalars().all()]


def record_find_only_entry_sync(entry: dict, **kwargs) -> dict | None:
    return _safe_sync(_record_find_only_entry_with_db(entry, **kwargs))


def record_api_check_record_sync(record: dict, **kwargs) -> dict | None:
    return _safe_sync(_record_api_check_record_with_db(record, **kwargs))


def record_api_check_records_sync(records: list[dict], **kwargs) -> dict:
    result = _safe_sync(_record_api_check_records_with_db(records, **kwargs))
    return result or {"ok": False, "recorded": 0}


def record_api_check_records_safe(records: list[dict], *, account: str, source_run_id: str) -> dict:
    """Record API-check rows with the script-facing error payload shape."""
    try:
        return record_api_check_records_sync(
            records,
            settings={"account": account},
            source_run_id=source_run_id,
        )
    except Exception as exc:
        return {"ok": False, "recorded": 0, "error": str(exc)}


def record_submitted_alpha_in_ledger_sync(**kwargs) -> dict | None:
    return _safe_sync(_record_submitted_alpha_in_ledger_with_db(**kwargs))


def should_block_expression_sync(expression: str, **kwargs) -> dict:
    result = _safe_sync(_should_block_expression_with_db(expression, **kwargs))
    return result or {"blocked": False, "reasons": [], "nearest": None}


def build_exclusion_expressions_sync(**kwargs) -> list[str]:
    result = _safe_sync(_build_exclusion_expressions_with_db(**kwargs))
    return result or []


def query_alpha_experiment_rows_sync(**kwargs) -> list[dict]:
    result = _safe_sync(_query_alpha_experiment_rows_with_db(**kwargs))
    return result or []


async def _upsert_experiment(
    session: AsyncSession,
    record: dict,
    *,
    settings: dict,
    source_run_id: str | None,
    source_file: str | None = None,
    source_type: str | None = None,
    user_id: str | uuid.UUID | None = None,
) -> WQAlphaExperiment:
    expression = str(record.get("expression") or "").strip()
    expr_hash = expression_hash(expression)
    p_hash = params_hash(settings)
    alpha_id = record.get("alpha_id")
    source_run_id = source_run_id or _infer_source_run_id(record.get("source_file") or source_file) or "manual"

    exp = None
    if alpha_id:
        result = await session.execute(
            select(WQAlphaExperiment)
            .where(WQAlphaExperiment.alpha_id == str(alpha_id))
            .order_by(WQAlphaExperiment.created_at.desc())
            .limit(1)
        )
        exp = result.scalar_one_or_none()

    if exp is None:
        result = await session.execute(
            select(WQAlphaExperiment)
            .where(
                WQAlphaExperiment.expression_hash == expr_hash,
                WQAlphaExperiment.params_hash == p_hash,
                WQAlphaExperiment.source_run_id == source_run_id,
            )
            .limit(1)
        )
        exp = result.scalar_one_or_none()

    if exp is None:
        exp = WQAlphaExperiment(
            user_id=_coerce_uuid(user_id),
            expression=expression,
            expression_normalized=normalize_expression(expression),
            expression_hash=expr_hash,
            params_hash=p_hash,
            source_run_id=source_run_id,
        )
        session.add(exp)

    _apply_record_to_experiment(
        exp,
        record,
        settings,
        source_file=source_file or record.get("source_file"),
        source_type=source_type,
        user_id=user_id,
    )
    return exp


async def _find_existing_for_api_record(
    session: AsyncSession,
    record: dict,
    settings: dict,
    source_run_id: str | None,
) -> WQAlphaExperiment | None:
    alpha_id = record.get("alpha_id")
    if alpha_id:
        result = await session.execute(
            select(WQAlphaExperiment)
            .where(WQAlphaExperiment.alpha_id == str(alpha_id))
            .order_by(WQAlphaExperiment.created_at.desc())
            .limit(1)
        )
        exp = result.scalar_one_or_none()
        if exp is not None:
            return exp

    expression = str(record.get("expression") or "").strip()
    if not expression:
        return None
    run_id = source_run_id or _infer_source_run_id(record.get("source_file")) or "manual"
    result = await session.execute(
        select(WQAlphaExperiment)
        .where(
            WQAlphaExperiment.expression_hash == expression_hash(expression),
            WQAlphaExperiment.params_hash == params_hash(settings),
            WQAlphaExperiment.source_run_id == run_id,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


def _apply_record_to_experiment(
    exp: WQAlphaExperiment,
    record: dict,
    settings: dict,
    *,
    source_file: str | None = None,
    source_type: str | None = None,
    user_id: str | uuid.UUID | None = None,
) -> None:
    expression = str(record.get("expression") or exp.expression or "").strip()
    if expression:
        exp.expression = expression
        exp.expression_normalized = normalize_expression(expression)
        exp.expression_hash = expression_hash(expression)

    exp.params_hash = params_hash(settings)
    exp.account = settings["account"]
    exp.region = settings["region"]
    exp.universe = settings["universe"]
    exp.delay = settings["delay"]
    exp.decay = settings["decay"]
    exp.neutralization = settings["neutralization"]
    exp.truncation = settings["truncation"]

    if user_id is not None:
        exp.user_id = _coerce_uuid(user_id)
    if record.get("alpha_id"):
        exp.alpha_id = str(record.get("alpha_id"))
    if source_type:
        exp.source_type = source_type
    if record.get("source_family"):
        exp.source_family = record.get("source_family")
    if source_file:
        exp.source_file = str(source_file)
    exp.source_tag = record.get("tag") or record.get("source_tag") or exp.source_tag
    exp.candidate_meta = _candidate_meta(record) or exp.candidate_meta

    new_status = lifecycle_status(record)
    if record.get("api_check_status") or STATUS_RANK.get(new_status, 0) >= STATUS_RANK.get(exp.lifecycle_status, 0):
        exp.lifecycle_status = new_status

    exp.submit_eligible = _first_bool(record.get("submit_eligible"), record.get("source_submit_eligible"), exp.submit_eligible)
    exp.non_correlation_pass = _non_correlation_pass(record)
    exp.api_check_status = record.get("api_check_status") or exp.api_check_status
    exp.platform_status = record.get("platform_status") or exp.platform_status
    exp.review_failure_kind = record.get("review_failure_kind") or exp.review_failure_kind

    exp.sharpe = _first_float(record.get("sharpe"), _nested(record, "result", "wq_brain", "wq_sharpe"), exp.sharpe)
    exp.fitness = _first_float(record.get("fitness"), _nested(record, "result", "wq_brain", "wq_fitness"), exp.fitness)
    exp.returns = _first_float(record.get("returns"), _nested(record, "result", "wq_brain", "wq_returns"), exp.returns)
    exp.turnover = _first_float(record.get("turnover"), _nested(record, "result", "wq_brain", "wq_turnover"), exp.turnover)
    exp.grade = record.get("grade") or _nested(record, "result", "wq_brain", "wq_rating") or exp.grade

    self_check = record.get("self_correlation") or {}
    prod_check = record.get("prod_correlation") or {}
    exp.self_correlation_result = record.get("sc_result") or self_check.get("result") or exp.self_correlation_result
    exp.self_correlation_value = _first_float(record.get("sc_value"), self_check.get("value"), exp.self_correlation_value)
    exp.self_correlation_limit = _first_float(record.get("sc_limit"), self_check.get("limit"), exp.self_correlation_limit)
    exp.prod_correlation_result = record.get("prod_corr_result") or prod_check.get("result") or exp.prod_correlation_result
    exp.prod_correlation_value = _first_float(record.get("prod_corr_value"), prod_check.get("value"), exp.prod_correlation_value)
    exp.prod_correlation_limit = _first_float(record.get("prod_corr_limit"), prod_check.get("limit"), exp.prod_correlation_limit)

    similarity_to_blocked = record.get("similarity_to_blocked") or record.get("similarity")
    similarity_to_hits = record.get("similarity_to_hits")
    exp.max_similarity_to_blocked = _first_float(_overall_similarity(similarity_to_blocked), exp.max_similarity_to_blocked)
    exp.max_similarity_to_hits = _first_float(_overall_similarity(similarity_to_hits), exp.max_similarity_to_hits)
    exp.similarity_details = {
        "similarity_to_blocked": similarity_to_blocked,
        "similarity_to_hits": similarity_to_hits,
    }

    failure_kind = primary_failure_kind(record)
    if failure_kind:
        exp.failure_kind = failure_kind
        exp.failure_reasons = classify_failures(record)

    if record.get("api_check_status"):
        exp.raw_api_check = record
        exp.last_checked_at = datetime.now(timezone.utc)
    else:
        exp.raw_result = record
    exp.updated_at = datetime.now(timezone.utc)


async def _record_memory_specs(
    session: AsyncSession,
    specs: list[dict],
    *,
    user_id: str | uuid.UUID | None,
    experiment_id: str | None,
) -> list[WQFailureMemory]:
    memories: list[WQFailureMemory] = []
    uid = _coerce_uuid(user_id)
    for spec in specs:
        expr_hash = spec.get("expression_hash")
        signature = spec.get("pattern_signature")
        conditions = [
            WQFailureMemory.memory_type == spec["memory_type"],
            WQFailureMemory.failure_kind == spec["failure_kind"],
            WQFailureMemory.severity == spec["severity"],
            WQFailureMemory.scope == spec.get("scope", "global"),
        ]
        if expr_hash:
            conditions.append(WQFailureMemory.expression_hash == expr_hash)
        else:
            conditions.append(WQFailureMemory.pattern_signature == signature)

        result = await session.execute(select(WQFailureMemory).where(and_(*conditions)).limit(1))
        memory = result.scalar_one_or_none()
        if memory is None:
            memory = WQFailureMemory(
                user_id=uid,
                experiment_id=_coerce_uuid(experiment_id),
                memory_type=spec["memory_type"],
                scope=spec.get("scope", "global"),
                expression=spec.get("expression"),
                expression_normalized=spec.get("expression_normalized"),
                expression_hash=expr_hash,
                pattern_signature=signature,
                fields=spec.get("fields"),
                operators=spec.get("operators"),
                params=spec.get("params"),
                failure_kind=spec["failure_kind"],
                severity=spec["severity"],
                confidence=1.0,
                evidence_count=1,
                evidence=[spec.get("evidence")],
                source_experiment_ids=spec.get("source_experiment_ids") or [],
            )
            session.add(memory)
        else:
            memory.last_seen_at = datetime.now(timezone.utc)
            memory.evidence_count = int(memory.evidence_count or 0) + 1
            memory.confidence = max(float(memory.confidence or 0.0), 1.0)
            evidence = list(memory.evidence or [])
            evidence.append(spec.get("evidence"))
            memory.evidence = evidence[-20:]
            ids = set(str(item) for item in (memory.source_experiment_ids or []))
            if experiment_id:
                ids.add(str(experiment_id))
            memory.source_experiment_ids = sorted(ids)
        memories.append(memory)
    return memories


async def _record_find_only_entry_with_db(entry: dict, **kwargs) -> dict | None:
    await _ensure_db()
    from .db import _get_session_factory

    factory = _get_session_factory()
    async with factory() as session:
        try:
            exp = await record_find_only_entry(session, entry, **kwargs)
            await session.commit()
            return _experiment_summary(exp) if exp else None
        except Exception:
            await session.rollback()
            raise


async def _record_api_check_record_with_db(record: dict, **kwargs) -> dict | None:
    await _ensure_db()
    from .db import _get_session_factory

    factory = _get_session_factory()
    async with factory() as session:
        try:
            exp = await record_api_check_record(session, record, **kwargs)
            await session.commit()
            return _experiment_summary(exp) if exp else None
        except Exception:
            await session.rollback()
            raise


async def _record_api_check_records_with_db(records: list[dict], **kwargs) -> dict:
    await _ensure_db()
    from .db import _get_session_factory

    factory = _get_session_factory()
    async with factory() as session:
        try:
            recorded = 0
            for record in records:
                if await record_api_check_record(session, record, **kwargs):
                    recorded += 1
            await session.commit()
            return {"ok": True, "recorded": recorded}
        except Exception:
            await session.rollback()
            raise


async def _record_submitted_alpha_in_ledger_with_db(**kwargs) -> dict | None:
    await _ensure_db()
    from .db import _get_session_factory

    factory = _get_session_factory()
    async with factory() as session:
        try:
            exp = await record_submitted_alpha_in_ledger(session, **kwargs)
            await session.commit()
            return _experiment_summary(exp) if exp else None
        except Exception:
            await session.rollback()
            raise


async def _should_block_expression_with_db(expression: str, **kwargs) -> dict:
    await _ensure_db()
    from .db import _get_session_factory

    factory = _get_session_factory()
    async with factory() as session:
        return await should_block_expression(session, expression, **kwargs)


async def _build_exclusion_expressions_with_db(
    *,
    user_id: str | uuid.UUID | None = None,
    include_penalized: bool = False,
) -> list[str]:
    await _ensure_db()
    from .db import _get_session_factory

    uid = _coerce_uuid(user_id)
    factory = _get_session_factory()
    async with factory() as session:
        expressions: list[str] = []
        submitted_stmt = select(SubmittedAlpha)
        if uid is not None:
            submitted_stmt = submitted_stmt.where(SubmittedAlpha.user_id == uid)
        expressions.extend(a.expression for a in (await session.execute(submitted_stmt)).scalars().all())

        severities = ["block", "penalize"] if include_penalized else ["block"]
        memory_stmt = select(WQFailureMemory.expression).where(
            WQFailureMemory.severity.in_(severities),
            WQFailureMemory.expression.is_not(None),
        )
        if uid is not None:
            memory_stmt = memory_stmt.where(or_(WQFailureMemory.user_id == uid, WQFailureMemory.user_id.is_(None)))
        expressions.extend(str(row[0]) for row in (await session.execute(memory_stmt)).all() if row[0])

        experiment_stmt = select(WQAlphaExperiment.expression).where(
            WQAlphaExperiment.lifecycle_status.in_(sorted(BLOCKING_LEDGER_STATUSES)),
        )
        if uid is not None:
            experiment_stmt = experiment_stmt.where(or_(WQAlphaExperiment.user_id == uid, WQAlphaExperiment.user_id.is_(None)))
        expressions.extend(str(row[0]) for row in (await session.execute(experiment_stmt)).all() if row[0])

    seen: set[str] = set()
    unique: list[str] = []
    for expression in expressions:
        normalized = normalize_expression(expression)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(expression)
    return unique


async def _query_alpha_experiment_rows_with_db(**kwargs) -> list[dict]:
    await _ensure_db()
    from .db import _get_session_factory

    factory = _get_session_factory()
    async with factory() as session:
        return await query_alpha_experiment_rows(session, **kwargs)


async def _ensure_db() -> None:
    global _ensured_db
    if _ensured_db or os.environ.get("WQ_LEDGER_DISABLED"):
        return
    from .db import init_db

    await init_db()
    _ensured_db = True


def _safe_sync(coro):
    if os.environ.get("WQ_LEDGER_DISABLED"):
        return None
    try:
        return run_coro_sync(coro, timeout=30, timeout_message="timed out waiting for WQ ledger operation")
    except Exception as exc:
        logger.warning("WQ alpha ledger operation failed: %s", exc)
        return None


def _nearest_similarity(expression: str, candidates: list[dict]) -> dict | None:
    nearest = None
    seen: set[str] = set()
    for candidate in candidates:
        other = str(candidate.get("expression") or "").strip()
        if not other:
            continue
        normalized = normalize_expression(other)
        if normalized in seen:
            continue
        seen.add(normalized)
        similarity = compute_similarity(expression, other)
        item = {**candidate, "similarity": similarity}
        if nearest is None or similarity["overall_similarity"] > nearest["similarity"]["overall_similarity"]:
            nearest = item
    return nearest


def _source_type_for_entry(entry: dict) -> str:
    if entry.get("api_check_status"):
        return "api_check"
    if entry.get("candidate_meta"):
        return "find_only"
    status = str(entry.get("status") or "")
    return "candidate" if not status else "find_only"


def _infer_source_run_id(source_file: Any) -> str | None:
    if not source_file:
        return None
    parts = str(source_file).replace("\\", "/").split("/")
    if len(parts) >= 2 and parts[-1].endswith(".jsonl"):
        return parts[-2] if parts[-2] != "reports" else parts[-1].rsplit(".", 1)[0]
    return None


def _candidate_meta(record: dict) -> dict:
    explicit = record.get("candidate_meta")
    if isinstance(explicit, dict):
        return explicit
    excluded = {
        "created_at", "status", "expression", "tag", "alpha_id",
        "sharpe", "fitness", "returns", "turnover", "submit_eligible",
        "submitted", "submit_checks", "self_correlation", "prod_correlation",
        "is_checks", "similarity_to_blocked", "similarity_to_hits", "result",
        "api_check_status", "platform_status", "grade", "dateCreated",
        "sc_result", "sc_value", "prod_corr_result", "prod_corr_value",
        "review_failure_kind", "error", "source_status", "source_submit_eligible",
        "source_submitted", "source_file",
    }
    return {key: value for key, value in record.items() if key not in excluded and value is not None}


def _non_correlation_pass(record: dict) -> bool | None:
    checks = record.get("is_checks") or []
    if not checks:
        return None
    for check in checks:
        name = str(check.get("name") or "").upper()
        if "CORRELATION" in name:
            continue
        if str(check.get("result") or "").upper() == "FAIL":
            return False
    return True


def _overall_similarity(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("overall_similarity")
    return None


def _first_bool(*values: Any) -> bool | None:
    for value in values:
        if isinstance(value, bool):
            return value
    return None


def _coerce_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _submitted_alpha_user_id(value: str | uuid.UUID | None) -> uuid.UUID | None:
    uid = _coerce_uuid(value)
    if uid is not None:
        return uid
    try:
        from .auth import _DEV_USER_ID

        return _coerce_uuid(_DEV_USER_ID)
    except Exception:
        return None


def _experiment_summary(exp: WQAlphaExperiment | None) -> dict | None:
    if exp is None:
        return None
    return {
        "id": str(exp.id),
        "alpha_id": exp.alpha_id,
        "expression_hash": exp.expression_hash,
        "lifecycle_status": exp.lifecycle_status,
        "failure_kind": exp.failure_kind,
    }


def _experiment_row(exp: WQAlphaExperiment) -> dict:
    return {
        "alpha_id": exp.alpha_id,
        "expression": exp.expression,
        "tag": exp.source_tag,
        "status": exp.lifecycle_status,
        "source_status": exp.lifecycle_status,
        "source_submit_eligible": exp.submit_eligible,
        "api_check_status": exp.api_check_status,
        "platform_status": exp.platform_status,
        "sharpe": exp.sharpe,
        "fitness": exp.fitness,
        "returns": exp.returns,
        "turnover": exp.turnover,
        "sc_result": exp.self_correlation_result,
        "sc_value": exp.self_correlation_value,
        "sc_limit": exp.self_correlation_limit,
        "prod_corr_result": exp.prod_correlation_result,
        "prod_corr_value": exp.prod_correlation_value,
        "prod_corr_limit": exp.prod_correlation_limit,
        "source_run_id": exp.source_run_id,
        "source_file": exp.source_file,
        "last_checked_at": exp.last_checked_at.isoformat() if exp.last_checked_at else None,
    }
