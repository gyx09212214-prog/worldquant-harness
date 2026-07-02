"""Record parsing and settings normalization for the WQ agent workflow."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .expression_parser import normalize_expression
from .wq_agent_config import WorkflowPaths, WQAgentWorkflowConfig


def candidate_dedupe_key(row: dict[str, Any]) -> str:
    expression = normalize_expression(str(row.get("expression") or ""))
    settings = candidate_settings_override(row)
    if not settings:
        return expression
    return f"{expression}||settings={json.dumps(settings, sort_keys=True, separators=(',', ':'))}"


def candidate_settings_override(row: dict[str, Any]) -> dict[str, Any]:
    return clean_simulation_settings(row.get("simulation_settings") or row.get("settings_override"))


def clean_simulation_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(settings, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("region", "universe", "neutralization"):
        value = settings.get(key)
        if value not in (None, ""):
            out[key] = str(value)
    for out_key, *input_keys in (
        ("maxTrade", "maxTrade", "max_trade"),
        ("maxPosition", "maxPosition", "max_position"),
    ):
        value = next((settings.get(key) for key in input_keys if settings.get(key) not in (None, "")), None)
        if value not in (None, ""):
            text = str(value).upper()
            if text in {"ON", "OFF"}:
                out[out_key] = text
    for key in ("delay", "decay"):
        value = settings.get(key)
        if value in (None, ""):
            continue
        try:
            out[key] = int(value)
        except (TypeError, ValueError):
            continue
    if settings.get("truncation") not in (None, ""):
        try:
            truncation = float(settings["truncation"])
        except (TypeError, ValueError):
            truncation = None
        if truncation is not None and 0 < truncation <= 0.2:
            out["truncation"] = truncation
    return out


def source_simulation_settings(row: dict[str, Any]) -> dict[str, Any]:
    for value in (
        row.get("actual_simulation_settings"),
        (row.get("result") or {}).get("settings") if isinstance(row.get("result"), dict) else None,
        row.get("simulation_settings"),
        row.get("effective_simulation_settings"),
    ):
        settings = clean_simulation_settings(value)
        if settings:
            return settings
    return {}


def simulation_setting_mismatches(requested: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    if not requested or not actual:
        return []
    mismatches: list[dict[str, Any]] = []
    for key in ("region", "universe", "delay", "decay", "neutralization", "truncation", "maxTrade", "maxPosition"):
        if key not in requested or key not in actual:
            continue
        requested_value = requested.get(key)
        actual_value = actual.get(key)
        if normalized_setting_value(key, requested_value) == normalized_setting_value(key, actual_value):
            continue
        mismatches.append({
            "key": key,
            "requested": requested_value,
            "actual": actual_value,
        })
    return mismatches


def normalized_setting_value(key: str, value: Any) -> Any:
    if value in (None, ""):
        return None
    if key in {"delay", "decay"}:
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if key == "truncation":
        try:
            return round(float(value), 8)
        except (TypeError, ValueError):
            return value
    return str(value).upper()


def simulation_settings_for_candidate(candidate: dict[str, Any], config: WQAgentWorkflowConfig) -> dict[str, Any]:
    settings = {
        "region": config.region,
        "universe": config.universe,
        "delay": config.delay,
        "decay": config.decay,
        "neutralization": config.neutralization,
        "truncation": config.truncation,
        "maxTrade": "OFF",
        "maxPosition": "OFF",
    }
    settings.update(candidate_settings_override(candidate))
    return settings


def read_candidate_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, list):
            return [candidate_from_value(value, str(path)) for value in data]
    return [candidate_from_value(value, str(path)) for value in iter_jsonish(path)]


def candidate_from_value(value: Any, source: str) -> dict[str, Any]:
    if isinstance(value, str):
        return {"expression": value, "source": source}
    if isinstance(value, dict):
        return {
            "expression": value.get("expression") or (value.get("result") or {}).get("expression"),
            "tag": value.get("tag"),
            "source_family": value.get("source_family"),
            "mutation_strategy": value.get("mutation_strategy"),
            "rationale": value.get("rationale"),
            "expected_low_corr_reason": value.get("expected_low_corr_reason"),
            "source_fields": value.get("source_fields") or value.get("fields"),
            "parent_alpha_ids": value.get("parent_alpha_ids") or [],
            "risk_flags": value.get("risk_flags") or [],
            "simulation_settings": candidate_settings_override(value),
            "candidate_meta": {
                **(value.get("candidate_meta") or {}),
                **{key: value.get(key) for key in ("alpha_id", "status", "source_family") if value.get(key) is not None},
            },
            "source": source,
        }
    return {"expression": "", "source": source}


def iter_jsonish(path: Path) -> Iterable[Any]:
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("{"):
            yield line
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def workflow_settings(config: WQAgentWorkflowConfig) -> dict[str, Any]:
    return {
        "account": config.account,
        "region": config.region,
        "universe": config.universe,
        "delay": config.delay,
        "decay": config.decay,
        "neutralization": config.neutralization,
        "truncation": config.truncation,
    }


def workflow_config_dict(config: WQAgentWorkflowConfig) -> dict[str, Any]:
    data = asdict(config)
    for key in ("output_dir", "community_context_dir", "submission_policy_file", "legal_inputs_file", "post_submit_profile_dir"):
        if data.get(key) is not None:
            data[key] = str(data[key])
    data["candidate_files"] = [str(path) for path in config.candidate_files]
    data["seed_ready_files"] = [str(path) for path in config.seed_ready_files]
    data["seed_rejected_files"] = [str(path) for path in config.seed_rejected_files]
    data["post_submit_baseline_roots"] = [str(path) for path in config.post_submit_baseline_roots]
    return data


def workflow_files(paths: WorkflowPaths) -> dict[str, str]:
    return {key: str(value) for key, value in asdict(paths).items() if key != "output_dir"}
