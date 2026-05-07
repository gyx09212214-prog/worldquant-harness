"""Backfill SQLite from WQ BRAIN platform — recovers all alpha records as tasks.

Usage: python scripts/backfill_from_wq.py
"""
import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


async def main():
    from quantgpt.db import _get_session_factory, init_db
    from quantgpt.models import Task as TaskModel, User
    from quantgpt.auth import _DEV_USER_ID
    from quantgpt.wq_brain_client import WQBrainClient, is_configured
    from sqlalchemy import func, select

    await init_db()
    factory = _get_session_factory()

    # Ensure dev user exists
    async with factory() as session:
        user = await session.get(User, _DEV_USER_ID)
        if not user:
            session.add(User(id=_DEV_USER_ID, email="dev@localhost", nickname="Dev User"))
            await session.commit()
            print("Created dev user")

    # Check existing task count and collect known alpha_ids
    async with factory() as session:
        count_before = (await session.execute(select(func.count()).select_from(TaskModel))).scalar()
        rows = await session.execute(select(TaskModel.result))
        existing_alpha_ids = set()
        for (result_json,) in rows:
            if isinstance(result_json, dict):
                aid = result_json.get("alpha_id")
                if aid:
                    existing_alpha_ids.add(aid)
        print(f"DB before: {count_before} tasks, {len(existing_alpha_ids)} known alpha_ids")

    # Fetch all alphas from WQ BRAIN
    if not is_configured("primary"):
        print("WQ BRAIN not configured, exiting")
        return

    client = WQBrainClient()
    if not client.authenticate():
        print("WQ BRAIN auth failed")
        return

    s = client._get_session()
    all_alphas = []
    offset = 0
    while True:
        r = s.get(
            "https://api.worldquantbrain.com/users/self/alphas",
            params={"limit": 100, "offset": offset, "order": "-dateCreated"},
        )
        if r.status_code != 200:
            print(f"API error: {r.status_code}")
            break
        data = r.json()
        results = data.get("results", [])
        if not results:
            break
        all_alphas.extend(results)
        offset += len(results)
        total = data.get("count", "?")
        print(f"  Fetched {offset}/{total} alphas...", end="\r")
        if isinstance(total, int) and offset >= total:
            break

    client.close()
    print(f"\nWQ BRAIN: {len(all_alphas)} total alphas")

    # Insert new alphas as tasks
    inserted = 0
    async with factory() as session:
        for alpha in all_alphas:
            alpha_id = alpha.get("id", "")
            if alpha_id in existing_alpha_ids:
                continue

            settings = alpha.get("settings", {})
            is_data = alpha.get("is", {})
            expression = alpha.get("regular", {}).get("code", "") or alpha.get("code", "")

            created_str = alpha.get("dateCreated", "")
            try:
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            except Exception:
                created_dt = datetime.now(timezone.utc)

            fitness = is_data.get("fitness")
            grade = "-"
            if fitness is not None:
                grade = "A" if fitness >= 1.0 else "B" if fitness >= 0.7 else "C" if fitness >= 0.4 else "D"

            platform_status = alpha.get("status", "")
            task_status = "completed" if platform_status not in ("ERROR",) else "failed"

            result = {
                "expression": expression,
                "alpha_id": alpha_id,
                "is_metrics": {
                    "sharpe": is_data.get("sharpe"),
                    "fitness": fitness,
                    "returns": is_data.get("returns"),
                    "turnover": is_data.get("turnover"),
                    "checks": is_data.get("checks", []),
                },
                "settings": settings,
                "backtest_summary": {
                    "long_short_sharpe": is_data.get("sharpe"),
                    "wq_fitness": fitness,
                    "turnover": is_data.get("turnover"),
                },
                "grade": grade,
                "platform_status": platform_status,
            }

            task = TaskModel(
                id=uuid.uuid4().hex[:12],
                user_id=_DEV_USER_ID,
                session_id=None,
                status=task_status,
                task_type="wq_brain_submit",
                params={
                    "expression": expression,
                    "source": "platform_backfill",
                    "region": settings.get("region", "USA"),
                    "universe": settings.get("universe", "TOP3000"),
                    "delay": settings.get("delay", 1),
                    "neutralization": settings.get("neutralization", "SUBINDUSTRY"),
                },
                expression=expression,
                result=result,
                error=None,
                created_at=created_dt,
                updated_at=created_dt,
            )
            session.add(task)
            existing_alpha_ids.add(alpha_id)
            inserted += 1

        await session.commit()

    async with factory() as session:
        count_after = (await session.execute(select(func.count()).select_from(TaskModel))).scalar()

    print(f"\nInserted {inserted} new task records")
    print(f"DB after: {count_after} tasks")


if __name__ == "__main__":
    asyncio.run(main())
