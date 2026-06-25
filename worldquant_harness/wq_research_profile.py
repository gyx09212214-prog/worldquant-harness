"""Versioned WQ research profile helpers.

The profile is deliberately stored as JSON so every mining generation can be
replayed and diffed before it is applied.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .wq_reference_catalog import DEFAULT_REFERENCE_DIR

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_DIR = REPO_ROOT / "configs" / "wq_research_profiles"
DEFAULT_PROFILE_NAME = "default"
PROFILE_SCHEMA_VERSION = 1


def default_research_profile(reference_catalog_path: str | None = None) -> dict[str, Any]:
    """Return the default WQ research profile."""

    now = _now()
    catalog = reference_catalog_path or _repo_relative(DEFAULT_REFERENCE_DIR)
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_name": DEFAULT_PROFILE_NAME,
        "profile_version": 0,
        "created_at": now,
        "updated_at": now,
        "reference_catalog_path": catalog,
        "strategy_notes": [
            "Use the bundled wq-alpha-research reference catalog as the field universe.",
            "Prefer profile candidates that improve ready yield without weakening no-real-submit guards.",
        ],
        "priority_biases": [],
        "similarity_policy": {
            "cutoff": 0.72,
        },
        "family_policy": {
            "max_family_count": 8,
        },
        "field_signature_policy": {
            "max_field_signature_count": 4,
            "blacklist": [],
        },
        "legal_input_policy": {
            "strict": True,
            "registry_path": "",
            "refresh_on_illegal_share": 0.10,
        },
        "repair_policy": {
            "enabled": True,
            "max_repairs_per_row": 4,
        },
        "promotion_gate": {
            "min_ready": 1,
            "max_self_correlation": 0.70,
            "max_daily_return_correlation": 0.70,
            "warn_daily_return_correlation": 0.50,
            "promote_requires_linked_submit_review": False,
        },
        "memory_policy": {
            "enable_outcomes": True,
            "compress_threshold": 50,
            "deprecated_excluded": True,
            "absorb_repeated_failures": True,
        },
        "mine_defaults": {
            "max_candidates": 200,
            "target_ready": 3,
            "max_total_simulations": 120,
            "cycle_candidate_count": 20,
            "max_cycles": 10,
            "allow_model": False,
            "use_ledger": True,
            "no_real_submit": True,
        },
    }


def init_profile(
    *,
    name: str = DEFAULT_PROFILE_NAME,
    profile_dir: Path | str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Initialize a profile directory and active pointer."""

    root = _profile_dir(profile_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = profile_path(name, root)
    if path.exists() and not force:
        profile = load_profile(name, profile_dir=root)
    else:
        profile = default_research_profile()
        profile["profile_name"] = name
        _write_json(path, profile)
    _write_json(active_pointer(root), {"active_profile": name, "updated_at": _now()})
    return {
        "ok": True,
        "active_profile": name,
        "profile_path": str(path),
        "profile": profile,
    }


def profile_status(profile_dir: Path | str | None = None) -> dict[str, Any]:
    """Return active profile metadata and candidate names."""

    root = _profile_dir(profile_dir)
    active_name = active_profile_name(root)
    profiles = sorted(
        path.stem
        for path in root.glob("*.json")
        if path.name != active_pointer(root).name and not path.name.endswith(".candidate.json")
    )
    candidates = sorted(path.name.removesuffix(".candidate.json") for path in root.glob("*.candidate.json"))
    profile = load_profile(active_name, profile_dir=root) if active_name else None
    return {
        "ok": bool(profile),
        "profile_dir": str(root),
        "active_profile": active_name,
        "profiles": profiles,
        "candidates": candidates,
        "profile": profile,
    }


def load_profile(name: str | None = None, *, profile_dir: Path | str | None = None) -> dict[str, Any]:
    """Load a named profile, or the active profile when name is omitted."""

    root = _profile_dir(profile_dir)
    profile_name = name or active_profile_name(root) or DEFAULT_PROFILE_NAME
    path = profile_path(profile_name, root)
    if not path.is_file():
        if profile_name == DEFAULT_PROFILE_NAME:
            return default_research_profile()
        raise FileNotFoundError(f"research profile not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid research profile: {path}")
    return payload


def save_profile(
    name: str,
    payload: dict[str, Any],
    *,
    profile_dir: Path | str | None = None,
    as_candidate: bool = False,
) -> dict[str, Any]:
    """Save a profile or candidate profile."""

    root = _profile_dir(profile_dir)
    root.mkdir(parents=True, exist_ok=True)
    profile = dict(payload)
    profile["profile_name"] = str(profile.get("profile_name") or name)
    profile["updated_at"] = _now()
    path = candidate_path(name, root) if as_candidate else profile_path(name, root)
    _write_json(path, profile)
    return {"ok": True, "path": str(path), "profile": profile}


def candidate_diff(name: str, *, profile_dir: Path | str | None = None) -> dict[str, Any]:
    """Diff a candidate profile against the active profile."""

    root = _profile_dir(profile_dir)
    base = load_profile(profile_dir=root)
    candidate_file = candidate_path(name, root)
    if not candidate_file.is_file():
        raise FileNotFoundError(f"profile candidate not found: {candidate_file}")
    candidate = json.loads(candidate_file.read_text(encoding="utf-8"))
    changes = _diff_dicts(base, candidate)
    return {
        "ok": True,
        "candidate": name,
        "active_profile": base.get("profile_name"),
        "candidate_file": str(candidate_file),
        "change_count": len(changes),
        "changes": changes,
    }


def apply_candidate(name: str, *, profile_dir: Path | str | None = None) -> dict[str, Any]:
    """Promote a candidate profile to the active profile."""

    root = _profile_dir(profile_dir)
    candidate_file = candidate_path(name, root)
    if not candidate_file.is_file():
        raise FileNotFoundError(f"profile candidate not found: {candidate_file}")
    candidate = json.loads(candidate_file.read_text(encoding="utf-8"))
    active_name = str(candidate.get("profile_name") or active_profile_name(root) or DEFAULT_PROFILE_NAME)
    candidate["profile_name"] = active_name
    candidate["profile_version"] = int(candidate.get("profile_version") or 0)
    candidate["applied_candidate"] = name
    candidate["updated_at"] = _now()
    _write_json(profile_path(active_name, root), candidate)
    _write_json(active_pointer(root), {"active_profile": active_name, "updated_at": _now()})
    return {
        "ok": True,
        "active_profile": active_name,
        "profile_path": str(profile_path(active_name, root)),
        "profile": candidate,
    }


def profile_to_mine_config(profile: dict[str, Any]) -> dict[str, Any]:
    """Translate a research profile into mining-loop config overrides."""

    mine = dict(profile.get("mine_defaults") or {})
    legal = profile.get("legal_input_policy") or {}
    signature = profile.get("field_signature_policy") or {}
    mine.update({
        "similarity_cutoff": _float_nested(profile, "similarity_policy", "cutoff", default=0.72),
        "max_family_count": int(_float_nested(profile, "family_policy", "max_family_count", default=8) or 8),
        "max_field_signature_count": int(signature.get("max_field_signature_count") or 4),
        "field_signature_blacklist": list(signature.get("blacklist") or []),
        "legal_inputs_file": str(legal.get("registry_path") or ""),
        "strict_legal_inputs": bool(legal.get("strict", True)),
        "priority_biases": list(profile.get("priority_biases") or []),
    })
    mine["no_real_submit"] = True
    return mine


def profile_to_gate(profile: dict[str, Any]) -> dict[str, Any]:
    """Translate a research profile into promotion gate overrides."""

    gate = dict(profile.get("promotion_gate") or {})
    return {
        "min_ready": int(gate.get("min_ready") or 1),
        "max_self_correlation": gate.get("max_self_correlation"),
        "max_daily_return_correlation": gate.get("max_daily_return_correlation"),
        "warn_daily_return_correlation": gate.get("warn_daily_return_correlation"),
        "promote_requires_linked_submit_review": bool(gate.get("promote_requires_linked_submit_review", False)),
    }


def profile_path(name: str, profile_dir: Path | str | None = None) -> Path:
    return _profile_dir(profile_dir) / f"{name}.json"


def candidate_path(name: str, profile_dir: Path | str | None = None) -> Path:
    return _profile_dir(profile_dir) / f"{name}.candidate.json"


def active_pointer(profile_dir: Path | str | None = None) -> Path:
    return _profile_dir(profile_dir) / "active_profile.json"


def active_profile_name(profile_dir: Path | str | None = None) -> str | None:
    pointer = active_pointer(profile_dir)
    if pointer.is_file():
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("active_profile"):
            return str(payload["active_profile"])
    default_path = profile_path(DEFAULT_PROFILE_NAME, profile_dir)
    return DEFAULT_PROFILE_NAME if default_path.is_file() else None


def _profile_dir(profile_dir: Path | str | None = None) -> Path:
    return Path(profile_dir) if profile_dir is not None else DEFAULT_PROFILE_DIR


def _diff_dicts(base: Any, candidate: Any, prefix: str = "") -> list[dict[str, Any]]:
    if isinstance(base, dict) and isinstance(candidate, dict):
        changes: list[dict[str, Any]] = []
        keys = sorted(set(base) | set(candidate))
        for key in keys:
            path = f"{prefix}.{key}" if prefix else str(key)
            changes.extend(_diff_dicts(base.get(key), candidate.get(key), path))
        return changes
    if base != candidate:
        return [{"path": prefix, "before": base, "after": candidate}]
    return []


def _float_nested(payload: dict[str, Any], *keys: str, default: float) -> float:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    try:
        return float(current)
    except (TypeError, ValueError):
        return default


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
