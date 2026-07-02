"""Progress-message normalization helpers for WQ simulation scripts."""

from __future__ import annotations


def ascii_progress_message(progress: int, message: str) -> str:
    if "并发限制" in message:
        return "Concurrent simulation limit; waiting before retry"
    if "速率限制" in message:
        return "Rate limited; waiting before retry"
    if "连接异常" in message:
        return "Connection error; waiting before retry"
    if "模拟完成" in message:
        return "Simulation completed"
    if "模拟进行中" in message:
        return f"Simulation running ({progress}%)"
    return message
