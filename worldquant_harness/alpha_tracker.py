"""Track submitted alphas and check self-correlation before new submissions."""

import asyncio
import logging
import threading
import uuid as _uuid

from sqlalchemy import select

from .wq_similarity import compute_similarity

logger = logging.getLogger(__name__)


async def record_submitted_alpha(
    user_id: str,
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
):
    from .db import _get_session_factory
    from .expression_parser import normalize_expression
    from .models import SubmittedAlpha

    normalized = normalize_expression(expression)
    uid = _uuid.UUID(user_id) if isinstance(user_id, str) else user_id
    factory = _get_session_factory()

    async with factory() as session:
        try:
            record = SubmittedAlpha(
                user_id=uid,
                alpha_id=alpha_id,
                expression=expression,
                expression_normalized=normalized,
                region=region,
                universe=universe,
                delay=delay,
                decay=decay,
                neutralization=neutralization,
                truncation=truncation,
                sharpe=sharpe,
                fitness=fitness,
                returns=returns,
                turnover=turnover,
                tag=tag,
            )
            session.add(record)
            try:
                from .wq_alpha_ledger import record_submitted_alpha_in_ledger
                await record_submitted_alpha_in_ledger(
                    session,
                    user_id=uid,
                    alpha_id=alpha_id,
                    expression=expression,
                    region=region,
                    universe=universe,
                    delay=delay,
                    decay=decay,
                    neutralization=neutralization,
                    truncation=truncation,
                    sharpe=sharpe,
                    fitness=fitness,
                    returns=returns,
                    turnover=turnover,
                    tag=tag,
                    status="submitted",
                )
            except Exception as e:
                logger.warning(f"Failed to mirror submitted alpha into WQ ledger: {e}")
            await session.commit()
            logger.info(f"Recorded submitted alpha: {alpha_id}")
        except Exception as e:
            await session.rollback()
            logger.error(f"Failed to record submitted alpha: {e}")


def record_submitted_alpha_sync(user_id: str, alpha_id: str, **kwargs):
    from . import task_store

    loop = task_store.main_loop
    if loop and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(
            record_submitted_alpha(user_id, alpha_id, **kwargs), loop,
        )
        try:
            future.result(timeout=15)
        except Exception as e:
            logger.error(f"Alpha tracking sync error: {e}")
    else:
        def _run():
            try:
                asyncio.run(record_submitted_alpha(user_id, alpha_id, **kwargs))
            except Exception as e:
                logger.error(f"Alpha tracking thread error: {e}")
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=10)


async def update_submitted_alpha_status(alpha_id: str, new_status: str):
    from .db import _get_session_factory
    from .models import SubmittedAlpha

    factory = _get_session_factory()
    async with factory() as session:
        try:
            result = await session.execute(
                select(SubmittedAlpha).where(SubmittedAlpha.alpha_id == alpha_id)
            )
            record = result.scalar_one_or_none()
            if record:
                record.status = new_status
                try:
                    from .models import WQAlphaExperiment

                    mapped = {
                        "active": "active",
                        "submitted": "submitted",
                        "sc_fail": "self_corr_fail",
                        "self_corr_fail": "self_corr_fail",
                        "prod_corr_fail": "prod_corr_fail",
                    }.get(str(new_status or "").lower())
                    if mapped:
                        result = await session.execute(
                            select(WQAlphaExperiment).where(WQAlphaExperiment.alpha_id == alpha_id)
                        )
                        for exp in result.scalars().all():
                            exp.lifecycle_status = mapped
                except Exception as e:
                    logger.warning(f"Failed to mirror alpha status into WQ ledger: {e}")
                await session.commit()
                logger.info(f"Updated SubmittedAlpha {alpha_id} status to {new_status}")
            else:
                logger.debug(f"SubmittedAlpha {alpha_id} not found in DB, skip update")
        except Exception as e:
            await session.rollback()
            logger.error(f"Failed to update alpha status {alpha_id}: {e}")


def update_submitted_alpha_status_sync(alpha_id: str, new_status: str):
    from . import task_store

    loop = task_store.main_loop
    if loop and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(
            update_submitted_alpha_status(alpha_id, new_status), loop,
        )
        try:
            future.result(timeout=15)
        except Exception as e:
            logger.error(f"Alpha status update sync error: {e}")
    else:
        def _run():
            try:
                asyncio.run(update_submitted_alpha_status(alpha_id, new_status))
            except Exception as e:
                logger.error(f"Alpha status update thread error: {e}")
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=10)


async def check_self_correlation(
    user_id: str,
    expression: str,
    threshold: float = 0.85,
    session=None,
) -> dict:
    from .expression_parser import normalize_expression
    from .models import SubmittedAlpha

    uid = _uuid.UUID(user_id) if isinstance(user_id, str) else user_id
    normalized = normalize_expression(expression)

    if session is not None:
        result = await session.execute(
            select(SubmittedAlpha).where(SubmittedAlpha.user_id == uid)
        )
        existing = result.scalars().all()
    else:
        from .db import _get_session_factory
        factory = _get_session_factory()
        async with factory() as _session:
            result = await _session.execute(
                select(SubmittedAlpha).where(SubmittedAlpha.user_id == uid)
            )
            existing = result.scalars().all()

    if not existing:
        return {"safe": True, "matches": [], "total_submitted": 0}

    matches = []
    for alpha in existing:
        if alpha.expression_normalized == normalized:
            matches.append({
                "alpha_id": alpha.alpha_id,
                "expression": alpha.expression,
                "similarity": compute_similarity(expression, alpha.expression),
                "exact_match": True,
                "region": alpha.region,
                "universe": alpha.universe,
                "fitness": alpha.fitness,
            })
            continue

        sim = compute_similarity(expression, alpha.expression)
        if sim["overall_similarity"] >= threshold:
            matches.append({
                "alpha_id": alpha.alpha_id,
                "expression": alpha.expression,
                "similarity": sim,
                "exact_match": False,
                "region": alpha.region,
                "universe": alpha.universe,
                "fitness": alpha.fitness,
            })

    matches.sort(key=lambda m: m["similarity"]["overall_similarity"], reverse=True)

    return {
        "safe": len(matches) == 0,
        "matches": matches[:10],
        "total_submitted": len(existing),
        "expression_normalized": normalized,
    }
