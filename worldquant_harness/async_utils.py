"""Async/sync bridge helpers for local service wrappers."""

from __future__ import annotations

import asyncio
import threading
from typing import Any


def run_coro_sync(coro, *, timeout: float = 30, timeout_message: str = "timed out waiting for async operation") -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            result_box["result"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - defensive bridge
            error_box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise TimeoutError(timeout_message)
    if error_box:
        raise error_box["error"]
    return result_box.get("result")
