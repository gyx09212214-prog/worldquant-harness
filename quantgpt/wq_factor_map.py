"""Build a factor-domain map from QuantGPT WQ artifacts.

The map is intentionally derived from existing ledger rows and JSON artifacts.
It does not call WQ BRAIN and does not mutate the database.
"""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .alpha_tracker import compute_similarity
from .expression_parser import extract_components, normalize_expression
from .models import SubmittedAlpha, WQAlphaExperiment, WQFailureMemory
from .wq_evolutionary_generator import classify_domain, family_hash


METRIC_KEYS = ("sharpe", "fitness", "returns", "turnover", "drawdown", "margin")
COUNT_KEYS = ("long_count", "short_count")
SUCCESS_STATUSES = {
    "active",
    "submitted",
    "eligible",
    "pre_submit_pass",
    "api_check_pass",
    "ready",
    "accepted",
}
SELF_CORR_TOKENS = ("self_corr", "self_correlation")
HIGH_SIM_TOKENS = ("high_similarity", "duplicate", "too_similar")
FORUM_SOURCE_TOKENS = ("forum", "community")


@dataclass(frozen=True)
class FactorMapConfig:
    input_paths: tuple[Path, ...] = field(default_factory=tuple)
    output_dir: Path | None = None
    obsidian_output: Path | None = None
    account: str | None = "primary"
    region: str | None = "USA"
    universe: str | None = "TOP3000"
    similarity_threshold: float = 0.70
    max_edge_nodes: int = 800
    max_edges: int = 5000
    db_limit: int | None = None
    title: str = "QuantGPT 因子地图"


async def build_factor_map(session: AsyncSession, config: FactorMapConfig) -> dict[str, Any]:
    """Collect DB/artifact rows and return a factor map payload."""
    raw_nodes: list[dict[str, Any]] = []
    raw_nodes.extend(await _db_experiment_nodes(session, config))
    raw_nodes.extend(await _db_submitted_nodes(session, config))
    raw_nodes.extend(await _db_failure_memory_nodes(session, config))
    raw_nodes.extend(_artifact_nodes(config.input_paths))

    nodes = merge_factor_nodes(raw_nodes)
    edges = build_similarity_edges(
        nodes,
        threshold=config.similarity_threshold,
        max_nodes=config.max_edge_nodes,
        max_edges=config.max_edges,
    )
    domain_summary = build_domain_summary(nodes, edges)
    field_summary = build_field_summary(nodes)
    report = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "account": config.account,
            "region": config.region,
            "universe": config.universe,
            "similarity_threshold": config.similarity_threshold,
            "max_edge_nodes": config.max_edge_nodes,
            "max_edges": config.max_edges,
            "db_limit": config.db_limit,
            "input_paths": [str(path) for path in config.input_paths],
        },
        "summary": {
            "raw_nodes": len(raw_nodes),
            "nodes": len(nodes),
            "edges": len(edges),
            "domains": len(domain_summary),
            "fields": len(field_summary),
        },
        "nodes": nodes,
        "edges": edges,
        "domain_summary": domain_summary,
        "field_summary": field_summary,
    }
    report["markdown"] = render_factor_map_markdown(report, title=config.title, output_dir=config.output_dir)
    if config.output_dir or config.obsidian_output:
        write_factor_map_artifacts(report, output_dir=config.output_dir, obsidian_output=config.obsidian_output)
    return report


def merge_factor_nodes(raw_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe rows by normalized expression while preserving source evidence."""
    by_key: dict[str, dict[str, Any]] = {}
    for raw in raw_nodes:
        expression = str(raw.get("expression") or "").strip()
        if not expression:
            continue
        normalized = str(raw.get("normalized_expression") or normalize_expression(expression))
        key = normalized or expression
        node = by_key.get(key)
        if node is None:
            node = _base_node(raw, expression=expression, normalized=normalized)
            by_key[key] = node
        _merge_node_payload(node, raw)

    nodes = list(by_key.values())
    for node in nodes:
        node["source_count"] = len(node["sources"])
        node["forum_count"] = sum(1 for source in node["sources"] if _is_forum_source(source.get("source")))
        node["status_flags"] = _status_flags(node)
    nodes.sort(key=lambda item: (-_score_node(item), item["domain"], item["node_id"]))
    return nodes


def build_similarity_edges(
    nodes: list[dict[str, Any]],
    *,
    threshold: float = 0.70,
    max_nodes: int = 800,
    max_edges: int = 5000,
) -> list[dict[str, Any]]:
    """Build high-similarity edges between factor nodes."""
    selected = nodes[: max(0, max_nodes)]
    edges: list[dict[str, Any]] = []
    for left_index, left in enumerate(selected):
        for right in selected[left_index + 1:]:
            same_family = bool(left.get("family_hash") and left.get("family_hash") == right.get("family_hash"))
            if not same_family and not _shares_component(left, right):
                continue
            similarity = compute_similarity(left["expression"], right["expression"])
            score = _safe_float(similarity.get("overall_similarity")) or 0.0
            if not same_family and score < threshold:
                continue
            edge_type = "same_family_similarity" if same_family and score >= threshold else "same_family"
            if not same_family:
                edge_type = "similarity"
            edges.append({
                "source": left["node_id"],
                "target": right["node_id"],
                "edge_type": edge_type,
                "similarity": similarity,
                "same_domain": left.get("domain") == right.get("domain"),
                "same_family": same_family,
                "shared_fields": sorted(set(left.get("fields", [])) & set(right.get("fields", []))),
                "shared_operators": sorted(set(left.get("operators", [])) & set(right.get("operators", []))),
            })
    edges.sort(key=lambda item: (-item["similarity"]["overall_similarity"], item["source"], item["target"]))
    return edges[: max(0, max_edges)]


def build_domain_summary(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_domain: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        by_domain.setdefault(str(node.get("domain") or "unknown"), []).append(node)

    edge_by_node: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        edge_by_node.setdefault(str(edge["source"]), []).append(edge)
        edge_by_node.setdefault(str(edge["target"]), []).append(edge)

    summaries: list[dict[str, Any]] = []
    for domain, domain_nodes in by_domain.items():
        similarities = [
            _safe_float(edge.get("similarity", {}).get("overall_similarity")) or 0.0
            for node in domain_nodes
            for edge in edge_by_node.get(node["node_id"], [])
            if edge.get("same_domain")
        ]
        fitness_values = [_safe_float(node.get("metrics", {}).get("fitness")) for node in domain_nodes]
        fitness_values = [value for value in fitness_values if value is not None]
        sharpe_values = [_safe_float(node.get("metrics", {}).get("sharpe")) for node in domain_nodes]
        sharpe_values = [value for value in sharpe_values if value is not None]
        active_count = sum(1 for node in domain_nodes if node["status_flags"]["success_like"])
        self_corr_fail_count = sum(1 for node in domain_nodes if node["status_flags"]["self_corr_fail"])
        high_similarity_fail_count = sum(1 for node in domain_nodes if node["status_flags"]["high_similarity"])
        forum_count = sum(int(node.get("forum_count") or 0) for node in domain_nodes)
        avg_similarity = round(statistics.mean(similarities), 4) if similarities else 0.0
        crowded_score = round(
            len(domain_nodes) * 0.45
            + active_count * 1.80
            + self_corr_fail_count * 2.00
            + high_similarity_fail_count * 1.25
            + avg_similarity * 6.0,
            4,
        )
        opportunity_score = round(
            forum_count * 1.35
            + max(0.0, 1.0 - avg_similarity) * 1.50
            - len(domain_nodes) * 0.30
            - self_corr_fail_count * 0.60,
            4,
        )
        summaries.append({
            "domain": domain,
            "node_count": len(domain_nodes),
            "active_or_submitted_count": active_count,
            "self_corr_fail_count": self_corr_fail_count,
            "high_similarity_fail_count": high_similarity_fail_count,
            "forum_count": forum_count,
            "avg_intra_similarity": avg_similarity,
            "avg_fitness": round(statistics.mean(fitness_values), 4) if fitness_values else None,
            "max_fitness": round(max(fitness_values), 4) if fitness_values else None,
            "avg_sharpe": round(statistics.mean(sharpe_values), 4) if sharpe_values else None,
            "crowded_score": crowded_score,
            "opportunity_score": opportunity_score,
            "top_fields": _top_values((field for node in domain_nodes for field in node.get("fields", [])), limit=8),
            "top_failures": _top_values(
                (failure for node in domain_nodes for failure in node.get("failure_kinds", [])), limit=6
            ),
        })
    summaries.sort(key=lambda item: (-item["crowded_score"], item["domain"]))
    return summaries


def build_field_summary(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_field: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        for field in node.get("fields", []):
            by_field.setdefault(str(field), []).append(node)
    rows: list[dict[str, Any]] = []
    for field, field_nodes in by_field.items():
        fitness_values = [_safe_float(node.get("metrics", {}).get("fitness")) for node in field_nodes]
        fitness_values = [value for value in fitness_values if value is not None]
        rows.append({
            "field": field,
            "node_count": len(field_nodes),
            "domain_count": len({node.get("domain") for node in field_nodes}),
            "domains": _top_values((node.get("domain") for node in field_nodes), limit=5),
            "self_corr_fail_count": sum(1 for node in field_nodes if node["status_flags"]["self_corr_fail"]),
            "active_or_submitted_count": sum(1 for node in field_nodes if node["status_flags"]["success_like"]),
            "avg_fitness": round(statistics.mean(fitness_values), 4) if fitness_values else None,
        })
    rows.sort(key=lambda item: (-item["node_count"], item["field"]))
    return rows


def write_factor_map_artifacts(
    report: dict[str, Any],
    *,
    output_dir: Path | None = None,
    obsidian_output: Path | None = None,
) -> dict[str, str]:
    written: dict[str, str] = {}
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        written["nodes"] = str(_write_jsonl(output_dir / "nodes.jsonl", report["nodes"]))
        written["edges"] = str(_write_jsonl(output_dir / "edges.jsonl", report["edges"]))
        written["domain_summary"] = str(_write_csv(output_dir / "domain_summary.csv", report["domain_summary"]))
        written["field_summary"] = str(_write_csv(output_dir / "field_summary.csv", report["field_summary"]))
        written["summary"] = str(_write_json(output_dir / "summary.json", _summary_payload(report, written)))
        written["markdown"] = str(_write_text(output_dir / "factor_map.md", report["markdown"]))
    if obsidian_output:
        obsidian_output.parent.mkdir(parents=True, exist_ok=True)
        written["obsidian"] = str(_write_text(obsidian_output, report["markdown"]))
    report.setdefault("files", {}).update(written)
    return written


def render_factor_map_markdown(report: dict[str, Any], *, title: str, output_dir: Path | None = None) -> str:
    generated_at = report.get("generated_at", "")
    summary = report.get("summary", {})
    domains = report.get("domain_summary", [])
    fields = report.get("field_summary", [])
    edges = report.get("edges", [])
    nodes = report.get("nodes", [])
    crowded = sorted(domains, key=lambda item: (-item["crowded_score"], item["domain"]))[:8]
    opportunities = sorted(domains, key=lambda item: (-item["opportunity_score"], item["domain"]))[:8]
    self_corr_nodes = [
        node for node in nodes
        if node.get("status_flags", {}).get("self_corr_fail") or node.get("status_flags", {}).get("high_similarity")
    ][:12]

    lines = [
        "---",
        "tags:",
        "  - quantgpt",
        "  - worldquant",
        "  - factor-map",
        f"generated_at: {generated_at}",
        "---",
        "",
        f"# {title}",
        "",
        "## 概览",
        "",
        f"- 节点：{summary.get('nodes', 0)}，相似度边：{summary.get('edges', 0)}，领域：{summary.get('domains', 0)}。",
        f"- 原始输入行：{summary.get('raw_nodes', 0)}；相似度阈值：{report.get('config', {}).get('similarity_threshold')}.",
        "- 这个地图只读取本地 ledger / artifact，不调用 WQ submit/delete。",
    ]
    if output_dir:
        lines.append(f"- 输出目录：`{output_dir}`")

    lines.extend([
        "",
        "## 拥挤领域",
        "",
        _domain_table(crowded),
        "",
        "## 低覆盖机会",
        "",
        _domain_table(opportunities),
        "",
        "## 高频字段",
        "",
        _field_table(fields[:15]),
        "",
        "## 高相关风险样本",
        "",
        _risk_table(self_corr_nodes),
        "",
        "## 最强相似度边",
        "",
        _edge_table(edges[:12], nodes),
        "",
        "## 使用方式",
        "",
        "- 拥挤领域：优先避免同字段、同算子、同 family 的微调式变体。",
        "- 低覆盖机会：优先作为下一轮 forum idea 或 cross-domain overlay 的候选来源。",
        "- 高相关风险样本：用于更新候选生成器的排除清单和修复模板。",
    ])
    return "\n".join(lines).rstrip() + "\n"


async def _db_experiment_nodes(session: AsyncSession, config: FactorMapConfig) -> list[dict[str, Any]]:
    stmt = select(WQAlphaExperiment)
    if config.account:
        stmt = stmt.where(WQAlphaExperiment.account == config.account)
    if config.region:
        stmt = stmt.where(WQAlphaExperiment.region == config.region)
    if config.universe:
        stmt = stmt.where(WQAlphaExperiment.universe == config.universe)
    stmt = stmt.order_by(WQAlphaExperiment.created_at.desc())
    if config.db_limit:
        stmt = stmt.limit(config.db_limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [_node_from_experiment(row) for row in rows]


async def _db_submitted_nodes(session: AsyncSession, config: FactorMapConfig) -> list[dict[str, Any]]:
    stmt = select(SubmittedAlpha)
    if config.region:
        stmt = stmt.where(SubmittedAlpha.region == config.region)
    if config.universe:
        stmt = stmt.where(SubmittedAlpha.universe == config.universe)
    stmt = stmt.order_by(SubmittedAlpha.submitted_at.desc())
    if config.db_limit:
        stmt = stmt.limit(config.db_limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [_node_from_submitted(row) for row in rows]


async def _db_failure_memory_nodes(session: AsyncSession, config: FactorMapConfig) -> list[dict[str, Any]]:
    stmt = select(WQFailureMemory).where(WQFailureMemory.expression.is_not(None))
    stmt = stmt.order_by(WQFailureMemory.last_seen_at.desc())
    if config.db_limit:
        stmt = stmt.limit(config.db_limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [_node_from_failure_memory(row) for row in rows]


def _artifact_nodes(paths: tuple[Path, ...]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for path in paths:
        for row in _read_structured_rows(path):
            node = _node_from_artifact_row(row, source_file=path)
            if node:
                nodes.append(node)
    return nodes


def _node_from_experiment(row: WQAlphaExperiment) -> dict[str, Any]:
    return _standard_node(
        expression=row.expression,
        source="wq_alpha_experiment",
        alpha_id=row.alpha_id,
        source_type=row.source_type,
        source_family=row.source_family,
        source_run_id=row.source_run_id,
        source_tag=row.source_tag,
        source_file=row.source_file,
        account=row.account,
        region=row.region,
        universe=row.universe,
        delay=row.delay,
        decay=row.decay,
        neutralization=row.neutralization,
        truncation=row.truncation,
        lifecycle_status=row.lifecycle_status,
        platform_status=row.platform_status,
        metrics=_metrics_from(row),
        failure_kind=row.failure_kind or row.review_failure_kind,
        self_correlation=_corr_payload(
            row.self_correlation_result, row.self_correlation_value, row.self_correlation_limit
        ),
        prod_correlation=_corr_payload(row.prod_correlation_result, row.prod_correlation_value, row.prod_correlation_limit),
        candidate_meta=row.candidate_meta,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _node_from_submitted(row: SubmittedAlpha) -> dict[str, Any]:
    return _standard_node(
        expression=row.expression,
        source="submitted_alpha",
        alpha_id=row.alpha_id,
        source_type="submitted",
        source_family=row.tag,
        source_tag=row.tag,
        region=row.region,
        universe=row.universe,
        delay=row.delay,
        decay=row.decay,
        neutralization=row.neutralization,
        truncation=row.truncation,
        lifecycle_status=row.status,
        metrics=_metrics_from(row),
        created_at=row.submitted_at,
    )


def _node_from_failure_memory(row: WQFailureMemory) -> dict[str, Any]:
    return _standard_node(
        expression=row.expression or "",
        source="wq_failure_memory",
        source_type=row.memory_type,
        source_family=row.scope,
        lifecycle_status=row.severity,
        failure_kind=row.failure_kind,
        fields=row.fields,
        operators=row.operators,
        candidate_meta={
            "pattern_signature": row.pattern_signature,
            "confidence": row.confidence,
            "evidence_count": row.evidence_count,
        },
        created_at=row.first_seen_at,
        updated_at=row.last_seen_at,
    )


def _node_from_artifact_row(row: dict[str, Any], *, source_file: Path) -> dict[str, Any] | None:
    expression = _first_text(
        row.get("expression"),
        row.get("regular"),
        _nested(row, "result", "expression"),
        _nested(row, "result", "regular"),
        _nested(row, "candidate", "expression"),
        _nested(row, "code", "code"),
    )
    if not expression:
        return None
    source_name = _artifact_source_name(source_file, row)
    is_data = row.get("is") if isinstance(row.get("is"), dict) else {}
    return _standard_node(
        expression=expression,
        source=source_name,
        alpha_id=_first_text(row.get("alpha_id"), _nested(row, "result", "alpha_id")),
        source_type=_first_text(row.get("source_type"), row.get("source"), row.get("memory_kind")),
        source_family=_first_text(row.get("source_family"), row.get("family"), row.get("tag")),
        source_run_id=_first_text(row.get("source_run_id"), row.get("run_id")),
        source_tag=_first_text(row.get("tag"), row.get("title"), row.get("topic")),
        source_file=str(source_file),
        account=_first_text(row.get("account")),
        region=_first_text(row.get("region"), _nested(row, "settings", "region")),
        universe=_first_text(row.get("universe"), _nested(row, "settings", "universe")),
        delay=_safe_int(_first_present(row.get("delay"), _nested(row, "settings", "delay"))),
        decay=_safe_int(_first_present(row.get("decay"), _nested(row, "settings", "decay"))),
        neutralization=_first_text(row.get("neutralization"), _nested(row, "settings", "neutralization")),
        truncation=_safe_float(_first_present(row.get("truncation"), _nested(row, "settings", "truncation"))),
        lifecycle_status=_first_text(row.get("lifecycle_status"), row.get("status"), row.get("final_status")),
        platform_status=_first_text(row.get("platform_status")),
        metrics={
            "sharpe": _safe_float(_first_present(row.get("sharpe"), is_data.get("sharpe"))),
            "fitness": _safe_float(_first_present(row.get("fitness"), is_data.get("fitness"))),
            "returns": _safe_float(_first_present(row.get("returns"), is_data.get("returns"))),
            "turnover": _safe_float(_first_present(row.get("turnover"), is_data.get("turnover"))),
            "drawdown": _safe_float(_first_present(row.get("drawdown"), is_data.get("drawdown"))),
            "margin": _safe_float(_first_present(row.get("margin"), is_data.get("margin"))),
        },
        failure_kind=_first_text(row.get("failure_kind"), row.get("reject_reason"), row.get("presubmit_reject_reason")),
        self_correlation=_corr_payload(row.get("sc_result"), row.get("sc_value"), row.get("sc_limit")),
        prod_correlation=_corr_payload(row.get("prod_corr_result"), row.get("prod_corr_value"), row.get("prod_corr_limit")),
        candidate_meta=_artifact_meta(row),
    )


def _standard_node(expression: str, *, source: str, **kwargs: Any) -> dict[str, Any]:
    expression = str(expression or "").strip()
    normalized = normalize_expression(expression) if expression else ""
    components = extract_components(expression) if expression else {"fields": set(), "operators": set()}
    fields = sorted(set(kwargs.pop("fields", None) or components.get("fields", [])))
    operators = sorted(set(kwargs.pop("operators", None) or components.get("operators", [])))
    domain = _first_text(_nested(kwargs.get("candidate_meta") or {}, "domain"))
    domain = domain or classify_domain(fields, expression)
    family = _first_text(kwargs.pop("family", None)) or family_hash(expression, domain=domain)
    failure_kind = kwargs.get("failure_kind")
    return {
        "expression": expression,
        "normalized_expression": normalized,
        "fields": fields,
        "operators": operators,
        "domain": domain,
        "family_hash": family,
        "source": source,
        "alpha_id": kwargs.get("alpha_id"),
        "source_type": kwargs.get("source_type"),
        "source_family": kwargs.get("source_family"),
        "source_run_id": kwargs.get("source_run_id"),
        "source_tag": kwargs.get("source_tag"),
        "source_file": kwargs.get("source_file"),
        "account": kwargs.get("account"),
        "region": kwargs.get("region"),
        "universe": kwargs.get("universe"),
        "delay": kwargs.get("delay"),
        "decay": kwargs.get("decay"),
        "neutralization": kwargs.get("neutralization"),
        "truncation": kwargs.get("truncation"),
        "lifecycle_status": kwargs.get("lifecycle_status"),
        "platform_status": kwargs.get("platform_status"),
        "metrics": {key: _safe_float((kwargs.get("metrics") or {}).get(key)) for key in METRIC_KEYS},
        "counts": {key: _safe_int((kwargs.get("counts") or {}).get(key)) for key in COUNT_KEYS},
        "failure_kinds": [failure_kind] if failure_kind else [],
        "self_correlation": kwargs.get("self_correlation") or {},
        "prod_correlation": kwargs.get("prod_correlation") or {},
        "candidate_meta": kwargs.get("candidate_meta") or {},
        "created_at": _stringify(kwargs.get("created_at")),
        "updated_at": _stringify(kwargs.get("updated_at")),
    }


def _base_node(raw: dict[str, Any], *, expression: str, normalized: str) -> dict[str, Any]:
    components = extract_components(expression)
    fields = sorted(set(raw.get("fields") or components.get("fields", [])))
    operators = sorted(set(raw.get("operators") or components.get("operators", [])))
    domain = str(raw.get("domain") or classify_domain(fields, expression))
    family = str(raw.get("family_hash") or family_hash(expression, domain=domain))
    node_id = "N_" + _short_hash(normalized or expression, 12)
    return {
        "node_id": node_id,
        "expression": expression,
        "normalized_expression": normalized,
        "domain": domain,
        "family_hash": family,
        "fields": fields,
        "operators": operators,
        "alpha_ids": [],
        "sources": [],
        "metrics": {},
        "counts": {},
        "settings": {},
        "failure_kinds": [],
        "self_correlation": {},
        "prod_correlation": {},
        "lifecycle_statuses": [],
        "platform_statuses": [],
        "tags": [],
        "candidate_meta": {},
    }


def _merge_node_payload(node: dict[str, Any], raw: dict[str, Any]) -> None:
    if raw.get("alpha_id") and raw["alpha_id"] not in node["alpha_ids"]:
        node["alpha_ids"].append(raw["alpha_id"])
    node["sources"].append({
        "source": raw.get("source"),
        "source_type": raw.get("source_type"),
        "source_family": raw.get("source_family"),
        "source_run_id": raw.get("source_run_id"),
        "source_tag": raw.get("source_tag"),
        "source_file": raw.get("source_file"),
        "alpha_id": raw.get("alpha_id"),
        "lifecycle_status": raw.get("lifecycle_status"),
        "platform_status": raw.get("platform_status"),
    })
    for key in METRIC_KEYS:
        node["metrics"][key] = _best_metric_value(key, node["metrics"].get(key), (raw.get("metrics") or {}).get(key))
    for key in COUNT_KEYS:
        node["counts"][key] = _first_present(node["counts"].get(key), (raw.get("counts") or {}).get(key))
    for key in ("account", "region", "universe", "delay", "decay", "neutralization", "truncation"):
        node["settings"][key] = _first_present(node["settings"].get(key), raw.get(key))
    for key in ("self_correlation", "prod_correlation"):
        node[key] = _best_correlation(node.get(key) or {}, raw.get(key) or {})
    for failure in raw.get("failure_kinds", []):
        if failure and failure not in node["failure_kinds"]:
            node["failure_kinds"].append(failure)
    for key, target in (("lifecycle_status", "lifecycle_statuses"), ("platform_status", "platform_statuses")):
        value = raw.get(key)
        if value and value not in node[target]:
            node[target].append(value)
    for tag in (raw.get("source_tag"), raw.get("source_family")):
        if tag and tag not in node["tags"]:
            node["tags"].append(tag)
    if raw.get("candidate_meta"):
        node["candidate_meta"].update({k: v for k, v in raw["candidate_meta"].items() if v is not None})
    if raw.get("created_at") and not node.get("created_at"):
        node["created_at"] = raw["created_at"]
    if raw.get("updated_at"):
        node["updated_at"] = raw["updated_at"]


def _status_flags(node: dict[str, Any]) -> dict[str, bool]:
    statuses = " ".join(str(value).lower() for value in node.get("lifecycle_statuses", []) + node.get("platform_statuses", []))
    failures = " ".join(str(value).lower() for value in node.get("failure_kinds", []))
    sources = " ".join(str(source.get("source") or "").lower() for source in node.get("sources", []))
    return {
        "success_like": any(status in statuses for status in SUCCESS_STATUSES),
        "self_corr_fail": any(token in statuses or token in failures for token in SELF_CORR_TOKENS),
        "high_similarity": any(token in statuses or token in failures for token in HIGH_SIM_TOKENS),
        "forum_derived": any(token in sources for token in FORUM_SOURCE_TOKENS),
    }


def _read_structured_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix.lower() == ".jsonl":
        rows: list[dict[str, Any]] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows
    if path.suffix.lower() == ".json":
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for key in ("results", "alphas", "candidates", "rows", "items", "data"):
                if isinstance(value.get(key), list):
                    return [item for item in value[key] if isinstance(item, dict)]
            return [value]
    return []


def _metrics_from(row: Any) -> dict[str, float | None]:
    return {key: _safe_float(getattr(row, key, None)) for key in METRIC_KEYS}


def _corr_payload(result: Any, value: Any, limit: Any) -> dict[str, Any]:
    payload = {
        "result": _first_text(result),
        "value": _safe_float(value),
        "limit": _safe_float(limit),
    }
    return {key: val for key, val in payload.items() if val is not None and val != ""}


def _best_metric_value(key: str, old: Any, new: Any) -> float | None:
    old_float = _safe_float(old)
    new_float = _safe_float(new)
    if old_float is None:
        return new_float
    if new_float is None:
        return old_float
    if key in {"fitness", "sharpe", "returns", "margin"}:
        return max(old_float, new_float)
    if key in {"turnover", "drawdown"}:
        return min(old_float, new_float)
    return old_float


def _best_correlation(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    old_value = _safe_float(old.get("value"))
    new_value = _safe_float(new.get("value"))
    if old_value is None:
        return dict(new)
    if new_value is None:
        return dict(old)
    return dict(new if new_value > old_value else old)


def _artifact_meta(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "memory_kind",
        "severity",
        "lesson",
        "topic",
        "title",
        "url",
        "rationale",
        "expected_low_corr_reason",
        "nearest_similarity",
    )
    return {key: row.get(key) for key in keys if row.get(key) is not None}


def _artifact_source_name(path: Path, row: dict[str, Any]) -> str:
    explicit = _first_text(row.get("source"))
    if explicit:
        return explicit
    name = path.name.lower()
    if any(token in name for token in FORUM_SOURCE_TOKENS):
        return "wq_forum_artifact"
    return "wq_artifact"


def _shares_component(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("domain") == right.get("domain"):
        return True
    if set(left.get("fields", [])) & set(right.get("fields", [])):
        return True
    return bool(set(left.get("operators", [])) & set(right.get("operators", [])))


def _score_node(node: dict[str, Any]) -> float:
    metrics = node.get("metrics", {})
    return (
        (_safe_float(metrics.get("fitness")) or 0.0) * 2.0
        + (_safe_float(metrics.get("sharpe")) or 0.0)
        + len(node.get("sources", [])) * 0.05
    )


def _top_values(values: Any, *, limit: int) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for value in values:
        if value is None or value == "":
            continue
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return [
        {"value": key, "count": count}
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _domain_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_暂无数据_"
    lines = [
        "| Domain | Nodes | Active/Submitted | Self Corr | High Sim | Forum | Avg Sim | Score | Top Fields |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            f"| {_md(row['domain'])} | {row['node_count']} | {row['active_or_submitted_count']} | "
            f"{row['self_corr_fail_count']} | {row['high_similarity_fail_count']} | {row['forum_count']} | "
            f"{row['avg_intra_similarity']} | {row['crowded_score']} | {_md(_compact_top(row['top_fields']))} |"
        )
    return "\n".join(lines)


def _field_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_暂无数据_"
    lines = [
        "| Field | Nodes | Domains | Active/Submitted | Self Corr | Avg Fitness |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| `{_md(row['field'])}` | {row['node_count']} | {row['domain_count']} | "
            f"{row['active_or_submitted_count']} | {row['self_corr_fail_count']} | {row['avg_fitness']} |"
        )
    return "\n".join(lines)


def _risk_table(nodes: list[dict[str, Any]]) -> str:
    if not nodes:
        return "_暂无高相关风险样本_"
    lines = [
        "| Node | Domain | Failures | Similarity/Corr | Expression |",
        "|---|---|---|---|---|",
    ]
    for node in nodes:
        risk = _first_present(
            node.get("self_correlation", {}).get("value"),
            node.get("prod_correlation", {}).get("value"),
            "",
        )
        lines.append(
            f"| `{node['node_id']}` | {_md(node['domain'])} | {_md(', '.join(node.get('failure_kinds', [])))} | "
            f"{risk} | `{_md(_truncate(node['expression'], 96))}` |"
        )
    return "\n".join(lines)


def _edge_table(edges: list[dict[str, Any]], nodes: list[dict[str, Any]]) -> str:
    if not edges:
        return "_暂无相似度边_"
    node_by_id = {node["node_id"]: node for node in nodes}
    lines = [
        "| Score | Type | Source | Target | Shared Fields |",
        "|---:|---|---|---|---|",
    ]
    for edge in edges:
        source = node_by_id.get(edge["source"], {})
        target = node_by_id.get(edge["target"], {})
        lines.append(
            f"| {edge['similarity']['overall_similarity']} | {_md(edge['edge_type'])} | "
            f"`{edge['source']}` {_md(source.get('domain', ''))} | "
            f"`{edge['target']}` {_md(target.get('domain', ''))} | "
            f"{_md(', '.join(edge.get('shared_fields') or []))} |"
        )
    return "\n".join(lines)


def _compact_top(rows: list[dict[str, Any]]) -> str:
    return ", ".join(f"{item['value']}({item['count']})" for item in rows[:5])


def _summary_payload(report: dict[str, Any], written: dict[str, str]) -> dict[str, Any]:
    return {
        "ok": report.get("ok"),
        "generated_at": report.get("generated_at"),
        "config": report.get("config"),
        "summary": report.get("summary"),
        "files": written,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})
    return path


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def _short_hash(text: str, length: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def _nested(payload: Any, *keys: str) -> Any:
    cur = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _first_text(*values: Any) -> str | None:
    value = _first_present(*values)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _is_forum_source(value: Any) -> bool:
    text = str(value or "").lower()
    return any(token in text for token in FORUM_SOURCE_TOKENS)


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _truncate(value: str, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
