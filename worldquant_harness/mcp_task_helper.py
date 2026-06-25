"""Shared task lifecycle helpers for MCP tools.

Ensures MCP tools create Task records identical to HTTP API routes,
so all tasks appear in the same list with consistent format.
Task records are written to DB at creation (running) AND completion,
so server restarts never lose data.

All functions are async — MCP tools are async, so we await DB writes
directly instead of using run_coroutine_threadsafe (which deadlocks
the event loop when called from async context).
"""

import logging
import time
import uuid as uuid_mod

from .auth import _DEV_USER_ID
from .task_store import persist_task_to_db_async, tasks, tasks_lock

logger = logging.getLogger(__name__)

_DEV_USER_ID_STR = str(_DEV_USER_ID)


async def start_mcp_task(task_type: str, expression: str | None, params: dict) -> str:
    task_id = uuid_mod.uuid4().hex[:12]
    params = {**params, "source": "mcp"}
    task = {
        "task_id": task_id,
        "user_id": _DEV_USER_ID_STR,
        "session_id": None,
        "status": "running",
        "task_type": task_type,
        "cancelled": False,
        "params": params,
        "expression": expression,
        "created_at": time.time(),
    }
    with tasks_lock:
        tasks[task_id] = task

    try:
        await persist_task_to_db_async(task_id, _DEV_USER_ID_STR, task)
    except Exception as e:
        logger.error(f"[{task_id}] MCP task initial persist error: {e}")

    return task_id


async def complete_mcp_task(
    task_id: str,
    result: dict | None = None,
    error: str | None = None,
    expression: str | None = None,
):
    task = tasks.get(task_id)
    if not task:
        logger.warning(f"[{task_id}] task evicted from memory, reconstructing for DB persist")
        task = {
            "task_id": task_id,
            "user_id": _DEV_USER_ID_STR,
            "session_id": None,
            "status": "failed" if error else "completed",
            "task_type": "mcp_unknown",
            "cancelled": False,
            "params": {"source": "mcp", "note": "reconstructed after memory eviction"},
            "expression": expression,
            "created_at": time.time(),
            "completed_at": time.time(),
        }
        if error:
            task["error"] = error
        if result:
            task["result"] = result
        with tasks_lock:
            tasks[task_id] = task
    else:
        task["completed_at"] = time.time()
        if error:
            task["status"] = "failed"
            task["error"] = error
        else:
            task["status"] = "completed"

        if expression:
            task["expression"] = expression
        if result:
            task["result"] = result

    try:
        await persist_task_to_db_async(task_id, _DEV_USER_ID_STR, task)
    except Exception as e:
        logger.error(f"[{task_id}] MCP task persist error: {e}")
