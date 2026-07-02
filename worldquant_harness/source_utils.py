"""Source-file identity helpers for WQ artifact records."""

from __future__ import annotations

from pathlib import Path


def source_run_id_from_cycle_path(path: Path | str) -> str:
    source_path = Path(path)
    if source_path.parent.name.startswith("cycle_"):
        return source_path.parent.parent.name
    return source_path.parent.name


def source_run_id_from_platform_or_path(source_file: Path | str) -> str:
    if str(source_file).startswith("platform:"):
        return "platform"
    path = Path(source_file)
    return path.parent.name if path.parent.name else path.stem


def source_run_id_from_report_path(path: Path | str) -> str:
    source_path = Path(path)
    if source_path.parent.name == "reports":
        return source_path.stem
    return source_path.parent.name
