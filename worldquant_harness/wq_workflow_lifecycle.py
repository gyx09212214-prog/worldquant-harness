"""Lifecycle event helpers for WQ workflow loops."""

from __future__ import annotations

from pathlib import Path

from .artifact_io import append_jsonl as _append_jsonl
from .wq_agent_config import WorkflowPaths, WQAgentWorkflowConfig
from .wq_agent_records import workflow_settings as _settings
from .wq_efficiency import lifecycle_event


def _append_lifecycle_event(
    paths: WorkflowPaths,
    event_type: str,
    row: dict,
    *,
    config: WQAgentWorkflowConfig,
    artifact_path: Path | None = None,
) -> None:
    _append_jsonl(
        paths.lifecycle_events,
        lifecycle_event(
            event_type,
            row,
            default_settings=_settings(config),
            artifact_path=str(artifact_path) if artifact_path else None,
            run_id=paths.output_dir.name,
        ),
    )
