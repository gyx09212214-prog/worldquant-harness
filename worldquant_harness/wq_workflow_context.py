"""Cached context and policy accessors for WQ workflow stages."""

from __future__ import annotations

import os
from typing import Any

from .community_context import CommunityContext
from .wq_agent_config import WQAgentWorkflowConfig
from .wq_failure_taxonomy import (
    community_repair_annotations as taxonomy_community_repair_annotations,
)
from .wq_failure_taxonomy import (
    community_skill_route_for_flags as taxonomy_community_skill_route_for_flags,
)
from .wq_forum_submission_optimizer import load_submission_policy
from .wq_legal_inputs import load_optional_legal_input_registry
from .wq_workflow_constants import NEAR_MISS_REPAIR


def _community_skill_route_for_flags(flags: list[str] | set[str]) -> list[str]:
    return taxonomy_community_skill_route_for_flags(flags)


def _community_repair_annotations(row: dict) -> dict:
    return taxonomy_community_repair_annotations(row, near_miss_bucket=NEAR_MISS_REPAIR)


_SUBMISSION_POLICY_CACHE: dict[str, dict[str, Any] | None] = {}


_LEGAL_INPUT_REGISTRY_CACHE: dict[str, Any] = {}


_COMMUNITY_CONTEXT_CACHE: dict[str, CommunityContext | None] = {}


def _submission_policy_for_config(config: WQAgentWorkflowConfig | None) -> dict[str, Any] | None:
    if config is None or not config.submission_policy_file:
        return None
    key = str(config.submission_policy_file)
    if key not in _SUBMISSION_POLICY_CACHE:
        _SUBMISSION_POLICY_CACHE[key] = load_submission_policy(config.submission_policy_file)
    return _SUBMISSION_POLICY_CACHE[key]


def _legal_input_registry_for_config(config: WQAgentWorkflowConfig | None) -> Any:
    if config is None or not config.legal_inputs_file:
        return None
    key = str(config.legal_inputs_file)
    if key not in _LEGAL_INPUT_REGISTRY_CACHE:
        _LEGAL_INPUT_REGISTRY_CACHE[key] = load_optional_legal_input_registry(config.legal_inputs_file)
    return _LEGAL_INPUT_REGISTRY_CACHE[key]


def _community_context_for_config(config: WQAgentWorkflowConfig | None) -> CommunityContext | None:
    if config is None:
        return None
    key = str(config.community_context_dir or os.environ.get("WQ_COMMUNITY_CONTEXT_DIR") or "__default__")
    if key not in _COMMUNITY_CONTEXT_CACHE:
        _COMMUNITY_CONTEXT_CACHE[key] = CommunityContext.from_dir(config.community_context_dir)
    return _COMMUNITY_CONTEXT_CACHE[key]
