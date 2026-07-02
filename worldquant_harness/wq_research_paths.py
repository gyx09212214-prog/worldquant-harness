"""Shared path helpers for WQ research experiments."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPERIMENT_ROOT = ROOT / "reports" / "wq_research_experiments"


def resolve_research_experiment_dir(
    experiment: Path | str,
    *,
    root: Path = DEFAULT_EXPERIMENT_ROOT,
) -> Path:
    path = Path(experiment)
    if (path / "experiment.yaml").is_file():
        return path
    if path.is_file():
        return path.parent
    candidate = root / str(experiment)
    if (candidate / "experiment.yaml").is_file():
        return candidate
    named_candidate = root / path.name
    if (named_candidate / "experiment.yaml").is_file():
        return named_candidate
    raise FileNotFoundError(f"experiment not found: {experiment}")
