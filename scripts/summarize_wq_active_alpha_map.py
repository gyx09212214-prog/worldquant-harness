"""Summarize active WQ submitted alphas with factor-map and yearly PnL views."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.expression_parser import extract_components, normalize_expression
from worldquant_harness.wq_alpha_detail import render_probe_markdown, summarize_alpha_probe, write_probe_outputs
from worldquant_harness.wq_auto_mining import load_dotenv
from worldquant_harness.wq_brain_client import READ_ONLY_ALPHA_DETAIL_PATHS, get_client, is_configured
from worldquant_harness.wq_evolutionary_generator import classify_domain, family_hash
from worldquant_harness.wq_factor_map import (
    build_domain_summary,
    build_field_summary,
    build_similarity_edges,
    merge_factor_nodes,
)
from worldquant_harness.wq_pnl_analysis import analyze_probe_directory, write_pnl_analysis_artifacts
from worldquant_harness.wq_review import parse_review_checks


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_dotenv(ROOT)

    output_dir = _resolve_path(args.output_dir) if args.output_dir else (
        ROOT / "reports" / f"wq_active_alpha_map_pnl_{datetime.now():%Y%m%d_%H%M%S}"
    )
    probe_dir = output_dir / "probe"
    output_dir.mkdir(parents=True, exist_ok=True)
    probe_dir.mkdir(parents=True, exist_ok=True)

    if not is_configured(args.account):
        print(json.dumps({"ok": False, "error": f"WQ credentials are not configured for account={args.account}"}), file=sys.stderr)
        return 2

    client = get_client(args.account)
    try:
        if not client.authenticate():
            print(json.dumps({"ok": False, "error": "WQ authentication failed"}), file=sys.stderr)
            return 2

        raw_platform_alphas, platform_count = fetch_platform_alphas(
            client,
            page_limit=args.page_limit,
            page_delay_seconds=args.page_delay_seconds,
            max_pages=args.max_pages,
        )
        platform_rows = [_normalize_platform_alpha(row, account=args.account) for row in raw_platform_alphas]
        selected_status = args.status.upper()
        active_rows = [row for row in platform_rows if str(row.get("status") or "").upper() == selected_status]

        _write_jsonl(output_dir / "platform_alphas.jsonl", platform_rows)
        _write_jsonl(output_dir / "selected_platform_alphas.jsonl", active_rows)
        (output_dir / "selected_alpha_ids.txt").write_text(
            "\n".join(str(row["alpha_id"]) for row in active_rows if row.get("alpha_id")) + "\n",
            encoding="utf-8",
        )

        local_records = load_local_submission_records(ROOT / "reports")
        local_by_id = _local_records_by_id(local_records)
        enriched_active = [_merge_local_metadata(row, local_by_id.get(str(row.get("alpha_id") or ""), [])) for row in active_rows]
        _write_jsonl(output_dir / "selected_alpha_inventory.jsonl", enriched_active)

        if not args.no_probe:
            probe_summaries = probe_alpha_details(
                client,
                enriched_active,
                probe_dir=probe_dir,
                delay_seconds=args.probe_delay_seconds,
                retry_missing=args.retry_missing_pnl,
                refresh=args.refresh_probe,
                limit=args.probe_limit,
            )
            probe_payload = {
                "ok": True,
                "read_only": True,
                "alpha_count": len(probe_summaries),
                "pnl_found_count": sum(1 for item in probe_summaries if item.get("pnl_curve_found")),
                "summaries": probe_summaries,
            }
            (probe_dir / "summary.json").write_text(json.dumps(probe_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            (probe_dir / "summary.md").write_text(render_probe_markdown(probe_summaries, output_dir=probe_dir), encoding="utf-8")
    finally:
        client.close()

    submitted_rows = list(enriched_active)
    submitted_rows.extend(local_records)
    if args.no_probe:
        pnl_report = {
            "ok": True,
            "probe_dir": str(probe_dir),
            "alpha_count": len(enriched_active),
            "pnl_found_count": 0,
            "alpha_reports": [],
            "portfolio_yearly": [],
            "markdown": "PnL probe was skipped for this run.\n",
        }
    else:
        pnl_report = analyze_probe_directory(probe_dir, submitted_rows=submitted_rows)
    pnl_files = write_pnl_analysis_artifacts(pnl_report, output_dir)
    pnl_by_id = {str(row.get("alpha_id") or ""): row for row in pnl_report.get("alpha_reports") or []}

    nodes = build_active_nodes(enriched_active, pnl_by_id)
    edges = build_similarity_edges(
        nodes,
        threshold=args.similarity_threshold,
        max_nodes=args.max_edge_nodes,
        max_edges=args.max_edges,
    )
    domain_summary = enrich_domain_summary(build_domain_summary(nodes, edges), nodes, pnl_by_id)
    field_summary = enrich_field_summary(build_field_summary(nodes), nodes, pnl_by_id)

    files = write_map_artifacts(
        output_dir,
        nodes=nodes,
        edges=edges,
        domain_summary=domain_summary,
        field_summary=field_summary,
        pnl_report=pnl_report,
        platform_rows=platform_rows,
        platform_count=platform_count,
        local_records=local_records,
        status=args.status.upper(),
        obsidian_output=_resolve_path(args.obsidian_output) if args.obsidian_output else _default_obsidian_output(),
    )
    files.update({f"pnl_{key}": value for key, value in pnl_files.items()})

    summary = {
        "ok": True,
        "output_dir": str(output_dir),
        "platform_count": platform_count,
        "platform_fetched": len(platform_rows),
        "status": args.status.upper(),
        "selected_count": len(enriched_active),
        "local_record_count": len(local_records),
        "pnl_found_count": pnl_report.get("pnl_found_count"),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "domain_count": len(domain_summary),
        "files": files,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


def fetch_platform_alphas(
    client: Any,
    *,
    page_limit: int = 100,
    page_delay_seconds: float = 0.2,
    max_pages: int = 0,
) -> tuple[list[dict[str, Any]], int | None]:
    session = client._get_session()
    rows: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None
    page = 0
    while True:
        if max_pages and page >= max_pages:
            break
        data = None
        for attempt in range(4):
            response = session.get(
                "https://api.worldquantbrain.com/users/self/alphas",
                params={"limit": min(max(1, page_limit), 100), "offset": offset, "order": "-dateCreated"},
                timeout=(10, 60),
            )
            if response.status_code == 200:
                data = response.json()
                break
            if response.status_code not in {429, 502, 503, 504}:
                raise RuntimeError(f"platform list failed at offset={offset}: HTTP {response.status_code}: {response.text[:300]}")
            wait = (attempt + 1) * 3
            time.sleep(wait)
        if data is None:
            raise RuntimeError(f"platform list failed at offset={offset} after retries")
        page_rows = data if isinstance(data, list) else data.get("results", [])
        if isinstance(data, dict) and isinstance(data.get("count"), int):
            total = int(data["count"])
        if not page_rows:
            break
        rows.extend(page_rows)
        offset += len(page_rows)
        page += 1
        _log(f"fetched platform page {page}: offset={offset}, total={total or '?'}")
        if total is not None and offset >= total:
            break
        if page_delay_seconds > 0:
            time.sleep(page_delay_seconds)
    return rows, total


def probe_alpha_details(
    client: Any,
    rows: list[dict[str, Any]],
    *,
    probe_dir: Path,
    delay_seconds: float = 0.5,
    retry_missing: int = 1,
    refresh: bool = False,
    limit: int = 0,
) -> list[dict[str, Any]]:
    targets = [str(row.get("alpha_id") or "") for row in rows if row.get("alpha_id")]
    if limit and limit > 0:
        targets = targets[:limit]
    summaries = []
    for index, alpha_id in enumerate(targets, start=1):
        if delay_seconds > 0 and index > 1:
            time.sleep(delay_seconds)
        _log(f"probe {index}/{len(targets)} alpha={alpha_id}")
        summaries.append(_probe_or_reuse(client, alpha_id, probe_dir=probe_dir, refresh=refresh))

    for _attempt in range(max(0, retry_missing)):
        missing = [str(item.get("alpha_id") or "") for item in summaries if not item.get("pnl_curve_found")]
        if not missing:
            break
        _log(f"retry missing pnl: {len(missing)} alpha(s)")
        by_id = {str(item.get("alpha_id") or ""): item for item in summaries}
        for index, alpha_id in enumerate(missing):
            if delay_seconds > 0 and index > 0:
                time.sleep(delay_seconds)
            by_id[alpha_id] = _probe_or_reuse(client, alpha_id, probe_dir=probe_dir, refresh=True)
        summaries = [by_id[alpha_id] for alpha_id in targets if alpha_id in by_id]
    return summaries


def _probe_or_reuse(client: Any, alpha_id: str, *, probe_dir: Path, refresh: bool) -> dict[str, Any]:
    summary_path = probe_dir / f"{_safe_filename(alpha_id)}_summary.json"
    if not refresh and summary_path.is_file():
        try:
            return json.loads(summary_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            pass
    probe = client.probe_alpha_detail(alpha_id, paths=READ_ONLY_ALPHA_DETAIL_PATHS)
    summary = summarize_alpha_probe(probe)
    write_probe_outputs(probe_dir, alpha_id, probe, summary)
    return summary


def load_local_submission_records(reports_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    patterns = ("submitted_accumulator.jsonl", "submit_results.jsonl", "submission_check*.jsonl")
    for pattern in patterns:
        for path in sorted(reports_dir.rglob(pattern)):
            for row in _read_jsonl(path):
                alpha_id = _first_text(row.get("alpha_id"), _nested(row, "result", "alpha_id"))
                if not alpha_id:
                    continue
                expression = _first_text(row.get("expression"), _nested(row, "result", "expression"))
                records.append({
                    "alpha_id": alpha_id,
                    "expression": expression,
                    "tag": row.get("tag"),
                    "status": _first_text(row.get("final_status"), row.get("platform_status"), row.get("status")),
                    "platform_status": row.get("platform_status"),
                    "sharpe": _safe_float(_first_present(row.get("sharpe"), _nested(row, "is", "sharpe"))),
                    "fitness": _safe_float(_first_present(row.get("fitness"), _nested(row, "is", "fitness"))),
                    "returns": _safe_float(_first_present(row.get("returns"), _nested(row, "is", "returns"))),
                    "turnover": _safe_float(_first_present(row.get("turnover"), _nested(row, "is", "turnover"))),
                    "sc_result": row.get("sc_result"),
                    "sc_value": _safe_float(row.get("sc_value")),
                    "prod_corr_result": row.get("prod_corr_result"),
                    "prod_corr_value": _safe_float(row.get("prod_corr_value")),
                    "source_file": str(path),
                    "source_mtime": path.stat().st_mtime,
                })
    return records


def build_active_nodes(active_rows: list[dict[str, Any]], pnl_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    raw_nodes: list[dict[str, Any]] = []
    for row in active_rows:
        expression = str(row.get("expression") or "").strip()
        if not expression:
            continue
        components = extract_components(expression)
        fields = sorted(set(components.get("fields") or []))
        operators = sorted(set(components.get("operators") or []))
        domain = classify_domain(fields, expression)
        review = row.get("review_checks") or {}
        pnl = pnl_by_id.get(str(row.get("alpha_id") or "")) or {}
        raw_nodes.append({
            "expression": expression,
            "normalized_expression": normalize_expression(expression),
            "fields": fields,
            "operators": operators,
            "domain": domain,
            "family_hash": family_hash(expression, domain=domain),
            "source": "platform_active_alpha",
            "alpha_id": row.get("alpha_id"),
            "source_type": "active_submitted",
            "source_family": row.get("tag") or row.get("local_tag"),
            "source_tag": row.get("tag") or row.get("local_tag"),
            "source_file": row.get("local_source_file"),
            "account": row.get("account"),
            "region": _nested(row, "settings", "region"),
            "universe": _nested(row, "settings", "universe"),
            "delay": _safe_int(_nested(row, "settings", "delay")),
            "decay": _safe_int(_nested(row, "settings", "decay")),
            "neutralization": _nested(row, "settings", "neutralization"),
            "truncation": _safe_float(_nested(row, "settings", "truncation")),
            "lifecycle_status": "active",
            "platform_status": row.get("status"),
            "metrics": {
                "sharpe": _safe_float(row.get("sharpe")),
                "fitness": _safe_float(row.get("fitness")),
                "returns": _safe_float(row.get("returns")),
                "turnover": _safe_float(row.get("turnover")),
                "drawdown": _safe_float(row.get("drawdown")),
                "margin": _safe_float(row.get("margin")),
            },
            "self_correlation": review.get("self_correlation") or {},
            "prod_correlation": review.get("prod_correlation") or {},
            "candidate_meta": {
                "dateCreated": row.get("dateCreated"),
                "pnl_stability": (pnl.get("stability") or {}).get("temporal_stability_score"),
                "pnl_warnings": pnl.get("warnings") or [],
            },
        })
    return merge_factor_nodes(raw_nodes)


def enrich_domain_summary(
    domain_summary: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    pnl_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    nodes_by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        nodes_by_domain[str(node.get("domain") or "unknown")].append(node)
    for row in domain_summary:
        domain_nodes = nodes_by_domain.get(str(row.get("domain") or ""), [])
        stability_values = []
        worst_returns = []
        for node in domain_nodes:
            for alpha_id in node.get("alpha_ids") or []:
                stability = (pnl_by_id.get(str(alpha_id)) or {}).get("stability") or {}
                if _safe_float(stability.get("temporal_stability_score")) is not None:
                    stability_values.append(float(stability["temporal_stability_score"]))
                if _safe_float(stability.get("worst_year_return")) is not None:
                    worst_returns.append(float(stability["worst_year_return"]))
        row["avg_temporal_stability"] = round(statistics.mean(stability_values), 4) if stability_values else None
        row["min_worst_year_return"] = round(min(worst_returns), 6) if worst_returns else None
    domain_summary.sort(key=lambda item: (-(item.get("node_count") or 0), -(item.get("crowded_score") or 0), str(item.get("domain"))))
    return domain_summary


def enrich_field_summary(
    field_summary: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    pnl_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    nodes_by_field: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        for field in node.get("fields") or []:
            nodes_by_field[str(field)].append(node)
    for row in field_summary:
        values = []
        for node in nodes_by_field.get(str(row.get("field") or ""), []):
            for alpha_id in node.get("alpha_ids") or []:
                stability = (pnl_by_id.get(str(alpha_id)) or {}).get("stability") or {}
                value = _safe_float(stability.get("temporal_stability_score"))
                if value is not None:
                    values.append(value)
        row["avg_temporal_stability"] = round(statistics.mean(values), 4) if values else None
    return field_summary


def write_map_artifacts(
    output_dir: Path,
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    domain_summary: list[dict[str, Any]],
    field_summary: list[dict[str, Any]],
    pnl_report: dict[str, Any],
    platform_rows: list[dict[str, Any]],
    platform_count: int | None,
    local_records: list[dict[str, Any]],
    status: str,
    obsidian_output: Path | None,
) -> dict[str, str]:
    files: dict[str, str] = {}
    files["nodes"] = str(_write_jsonl(output_dir / "active_nodes.jsonl", nodes))
    files["edges"] = str(_write_jsonl(output_dir / "active_edges.jsonl", edges))
    files["domain_summary"] = str(_write_csv(output_dir / "active_domain_summary.csv", domain_summary))
    files["field_summary"] = str(_write_csv(output_dir / "active_field_summary.csv", field_summary))
    markdown = render_active_map_markdown(
        nodes=nodes,
        edges=edges,
        domain_summary=domain_summary,
        field_summary=field_summary,
        pnl_report=pnl_report,
        platform_rows=platform_rows,
        platform_count=platform_count,
        local_records=local_records,
        status=status,
        output_dir=output_dir,
    )
    files["markdown"] = str(_write_text(output_dir / "active_alpha_map_with_pnl.md", markdown))
    if obsidian_output:
        obsidian_output.parent.mkdir(parents=True, exist_ok=True)
        files["obsidian"] = str(_write_text(obsidian_output, markdown))
    return files


def render_active_map_markdown(
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    domain_summary: list[dict[str, Any]],
    field_summary: list[dict[str, Any]],
    pnl_report: dict[str, Any],
    platform_rows: list[dict[str, Any]],
    platform_count: int | None,
    local_records: list[dict[str, Any]],
    status: str,
    output_dir: Path,
) -> str:
    pnl_by_id = {str(row.get("alpha_id") or ""): row for row in pnl_report.get("alpha_reports") or []}
    active_rows = _active_rows_from_nodes(nodes, pnl_by_id)
    status_counts = Counter(str(row.get("status") or "") for row in platform_rows)
    generated_at = datetime.now(timezone.utc).isoformat()
    years = sorted({
        int(year.get("year"))
        for report in pnl_by_id.values()
        for year in (report.get("yearly") or [])
        if year.get("year") is not None
    })

    lines = [
        "---",
        "tags:",
        "  - worldquant",
        "  - submitted-alpha",
        "  - factor-map",
        "  - pnl-analysis",
        f"generated_at: {generated_at}",
        "---",
        "",
        f"# WorldQuant {status} Alpha 地图与年度指标",
        "",
        "## 口径",
        "",
        f"- 主口径：平台 `/users/self/alphas` 当前 `{status}` alpha，表达式去重后形成地图节点。",
        "- 本地 `submitted_accumulator` / `submit_results` 只用于补充 tag、source 和提交线索。",
        "- PnL 如未跳过，则来自只读 alpha detail/recordset probe；没有 submit/delete 行为。",
        f"- 输出目录：`{output_dir}`",
        "",
        "## 总览",
        "",
        f"- 平台 alpha 总数：{platform_count if platform_count is not None else len(platform_rows)}；状态分布：{dict(status_counts)}。",
        f"- 当前 {status}：{sum(1 for row in platform_rows if str(row.get('status')).upper() == status)}；表达式去重节点：{len(nodes)}；相似度边：{len(edges)}。",
        f"- 本地提交记录：{len(local_records)}；PnL 找到：{pnl_report.get('pnl_found_count')}/{pnl_report.get('alpha_count')}。",
    ]
    lines.extend(_key_findings(nodes, domain_summary, edges, pnl_report))

    lines.extend([
        "",
        "## 领域地图",
        "",
        "| Domain | Nodes | Avg Sharpe | Avg Fitness | Avg Stability | Avg Intra Sim | Crowded | Top Fields |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ])
    for row in domain_summary:
        lines.append(
            f"| `{_md(row.get('domain'))}` | {row.get('node_count')} | {_fmt(row.get('avg_sharpe'))} | "
            f"{_fmt(row.get('avg_fitness'))} | {_fmt(row.get('avg_temporal_stability'))} | "
            f"{_fmt(row.get('avg_intra_similarity'))} | {_fmt(row.get('crowded_score'))} | "
            f"{_top_value_text(row.get('top_fields'))} |"
        )

    lines.extend([
        "",
        "## 高频字段",
        "",
        "| Field | Count | Domains | Avg Fitness | Avg Stability | Self Corr Fails |",
        "|---|---:|---|---:|---:|---:|",
    ])
    for row in field_summary[:25]:
        lines.append(
            f"| `{_md(row.get('field'))}` | {row.get('node_count')} | {_top_value_text(row.get('domains'))} | "
            f"{_fmt(row.get('avg_fitness'))} | {_fmt(row.get('avg_temporal_stability'))} | {row.get('self_corr_fail_count')} |"
        )

    lines.extend([
        "",
        f"## {status} Alpha 排名",
        "",
        "| Alpha | Tag | Domain | Sharpe | Fitness | Ret | Turnover | SELF | PROD | Stability | PosY | MinYSharpe | WorstYRet | Recent2Y | Warnings |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ])
    for row in active_rows:
        lines.append(
            f"| `{_md(row['alpha_id'])}` | `{_md(row.get('tag'))}` | `{_md(row.get('domain'))}` | "
            f"{_fmt(row.get('sharpe'))} | {_fmt(row.get('fitness'))} | {_fmt_pct(row.get('returns'))} | {_fmt(row.get('turnover'))} | "
            f"{_fmt(row.get('self_corr'))} | {_fmt(row.get('prod_corr'))} | {_fmt(row.get('stability'))} | "
            f"{_fmt_pct(row.get('positive_year_ratio'))} | {_fmt(row.get('min_year_sharpe'))} | "
            f"{_fmt_pct(row.get('worst_year_return'))} | {_fmt(row.get('recent_2y_sharpe'))} | "
            f"{', '.join(row.get('warnings') or [])} |"
        )

    lines.extend([
        "",
        "## 年度 PnL 横截面",
        "",
        "| Alpha | Domain | Stability | " + " | ".join(str(year) for year in years) + " |",
        "|---|---|---:|" + "|".join("---:" for _ in years) + "|",
    ])
    for row in active_rows:
        report = pnl_by_id.get(row["alpha_id"]) or {}
        yearly = {int(item["year"]): item for item in report.get("yearly") or [] if item.get("year") is not None}
        cells = []
        for year in years:
            item = yearly.get(year)
            if item:
                cells.append(f"{_fmt_pct(item.get('return'))}/{_fmt(item.get('sharpe'))}")
            else:
                cells.append("-")
        lines.append(
            f"| `{_md(row['alpha_id'])}` | `{_md(row.get('domain'))}` | {_fmt(row.get('stability'))} | "
            + " | ".join(cells)
            + " |"
        )

    lines.extend([
        "",
        "## 等权组合年度视角",
        "",
        "| Year | Alpha Count | EW Return | Positive Alpha Ratio | Min Ret | Max Ret |",
        "|---:|---:|---:|---:|---:|---:|",
    ])
    for row in pnl_report.get("portfolio_yearly") or []:
        lines.append(
            f"| {row.get('year')} | {row.get('alpha_count')} | {_fmt_pct(row.get('equal_weight_return'))} | "
            f"{_fmt(row.get('positive_alpha_ratio'))} | {_fmt_pct(row.get('min_alpha_return'))} | {_fmt_pct(row.get('max_alpha_return'))} |"
        )

    lines.extend([
        "",
        "## 高相似度边",
        "",
        "| Left | Right | Sim | Shared Fields | Type |",
        "|---|---|---:|---|---|",
    ])
    node_by_id = {node["node_id"]: node for node in nodes}
    for edge in edges[:30]:
        left = node_by_id.get(edge["source"], {})
        right = node_by_id.get(edge["target"], {})
        lines.append(
            f"| `{_md(_alpha_label(left))}` | `{_md(_alpha_label(right))}` | "
            f"{_fmt(_nested(edge, 'similarity', 'overall_similarity'))} | "
            f"{', '.join(edge.get('shared_fields') or [])} | `{_md(edge.get('edge_type'))}` |"
        )

    lines.extend([
        "",
        "## 后续使用",
        "",
        "- 生成新表达式时，先避开高频字段和高相似度边里的同 family 微调。",
        "- 保留年度稳定性高、但领域拥挤度低的表达式作为下一轮扩展种子。",
        "- 对年度弱点明显的 alpha，优先做 regime/行业中性/低相关 overlay，而不是只调 decay 或 truncation。",
    ])
    return "\n".join(lines).rstrip() + "\n"


def _key_findings(
    nodes: list[dict[str, Any]],
    domain_summary: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    pnl_report: dict[str, Any],
) -> list[str]:
    findings = ["", "## 数据结论", ""]
    if domain_summary:
        crowded = domain_summary[0]
        stable_domains = sorted(
            [row for row in domain_summary if row.get("avg_temporal_stability") is not None],
            key=lambda item: (-(item.get("avg_temporal_stability") or 0), -(item.get("node_count") or 0)),
        )[:3]
        findings.append(
            f"- 最拥挤领域是 `{crowded.get('domain')}`：{crowded.get('node_count')} 个节点，"
            f"crowded_score={_fmt(crowded.get('crowded_score'))}，高频字段为 {_top_value_text(crowded.get('top_fields'))}。"
        )
        if stable_domains:
            text = "、".join(
                f"`{row.get('domain')}`({_fmt(row.get('avg_temporal_stability'))})" for row in stable_domains
            )
            findings.append(f"- 年度稳定性较好的领域：{text}。")
    if edges:
        high_edges = [edge for edge in edges if (_safe_float(_nested(edge, "similarity", "overall_similarity")) or 0) >= 0.80]
        findings.append(f"- 相似度边共 {len(edges)} 条，其中 overall_similarity >= 0.80 的高重合边 {len(high_edges)} 条。")
    reports = pnl_report.get("alpha_reports") or []
    weak = sorted(
        reports,
        key=lambda item: ((item.get("stability") or {}).get("temporal_stability_score") or 0),
    )[:5]
    if weak:
        findings.append(
            "- 年度稳定性最低的 alpha："
            + "、".join(
                f"`{row.get('alpha_id')}`({_fmt((row.get('stability') or {}).get('temporal_stability_score'))})"
                for row in weak
            )
            + "。"
        )
    return findings


def _normalize_platform_alpha(row: dict[str, Any], *, account: str) -> dict[str, Any]:
    regular = row.get("regular") if isinstance(row.get("regular"), dict) else {}
    expression = regular.get("code") if regular else row.get("regular")
    settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
    is_data = row.get("is") if isinstance(row.get("is"), dict) else {}
    review_checks = parse_review_checks(row)
    return {
        "alpha_id": row.get("id"),
        "account": account,
        "expression": str(expression or ""),
        "status": row.get("status"),
        "stage": row.get("stage"),
        "dateCreated": row.get("dateCreated"),
        "dateSubmitted": row.get("dateSubmitted"),
        "settings": settings,
        "sharpe": _safe_float(is_data.get("sharpe")),
        "fitness": _safe_float(is_data.get("fitness")),
        "returns": _safe_float(is_data.get("returns")),
        "turnover": _safe_float(is_data.get("turnover")),
        "drawdown": _safe_float(is_data.get("drawdown")),
        "margin": _safe_float(is_data.get("margin")),
        "pnl": _safe_float(is_data.get("pnl")),
        "review_checks": review_checks,
        "self_correlation": (review_checks.get("self_correlation") or {}).get("value"),
        "prod_correlation": (review_checks.get("prod_correlation") or {}).get("value"),
    }


def _local_records_by_id(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        grouped[str(row.get("alpha_id") or "")].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda item: item.get("source_mtime") or 0, reverse=True)
    return grouped


def _merge_local_metadata(row: dict[str, Any], local_rows: list[dict[str, Any]]) -> dict[str, Any]:
    merged = dict(row)
    if not local_rows:
        return merged
    best = local_rows[0]
    merged["local_tag"] = best.get("tag")
    merged["tag"] = best.get("tag") or merged.get("tag")
    merged["local_status"] = best.get("status")
    merged["local_source_file"] = best.get("source_file")
    return merged


def _active_rows_from_nodes(nodes: list[dict[str, Any]], pnl_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for node in nodes:
        metrics = node.get("metrics") or {}
        self_corr = node.get("self_correlation") or {}
        prod_corr = node.get("prod_correlation") or {}
        for alpha_id in node.get("alpha_ids") or [""]:
            pnl = pnl_by_id.get(str(alpha_id)) or {}
            stability = pnl.get("stability") or {}
            rows.append({
                "alpha_id": str(alpha_id),
                "tag": (node.get("tags") or [""])[0] if node.get("tags") else "",
                "domain": node.get("domain"),
                "sharpe": metrics.get("sharpe"),
                "fitness": metrics.get("fitness"),
                "returns": metrics.get("returns"),
                "turnover": metrics.get("turnover"),
                "self_corr": self_corr.get("value"),
                "prod_corr": prod_corr.get("value"),
                "stability": stability.get("temporal_stability_score"),
                "positive_year_ratio": stability.get("positive_year_ratio"),
                "min_year_sharpe": stability.get("min_year_sharpe"),
                "worst_year_return": stability.get("worst_year_return"),
                "recent_2y_sharpe": stability.get("recent_2y_sharpe"),
                "warnings": pnl.get("warnings") or [],
            })
    rows.sort(key=lambda item: (-(item.get("stability") or 0), -(item.get("fitness") or 0), str(item.get("alpha_id"))))
    return rows


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build active WQ alpha factor map with yearly PnL analysis")
    parser.add_argument("--account", default="primary")
    parser.add_argument("--status", default="ACTIVE")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--obsidian-output", default="")
    parser.add_argument("--page-limit", type=int, default=100)
    parser.add_argument("--page-delay-seconds", type=float, default=0.2)
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--probe-delay-seconds", type=float, default=0.5)
    parser.add_argument("--probe-limit", type=int, default=0)
    parser.add_argument("--retry-missing-pnl", type=int, default=1)
    parser.add_argument("--refresh-probe", action="store_true")
    parser.add_argument("--no-probe", action="store_true")
    parser.add_argument("--similarity-threshold", type=float, default=0.70)
    parser.add_argument("--max-edge-nodes", type=int, default=300)
    parser.add_argument("--max-edges", type=int, default=1000)
    return parser.parse_args(argv)


def _default_obsidian_output() -> Path:
    return (
        ROOT.parents[1]
        / "doc"
        / "obsidian"
        / "exports"
        / "Quant"
        / "Stock"
        / "Factors"
        / f"WorldQuant 已提交Alpha地图与年度PnL {datetime.now():%Y%m%d}.md"
    )


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return rows
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


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
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for key, value in row.items()})
    return path


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _nested(value: dict[str, Any] | None, *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _first_text(*values: Any) -> str:
    value = _first_present(*values)
    return str(value or "")


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
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_filename(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in str(value))


def _fmt(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "-"
    return f"{number:.4f}".rstrip("0").rstrip(".")


def _fmt_pct(value: Any) -> str:
    number = _safe_float(value)
    if number is None:
        return "-"
    return f"{number * 100:.2f}%"


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _top_value_text(values: Any) -> str:
    if not values:
        return ""
    if isinstance(values, dict):
        return ", ".join(f"{key}:{value}" for key, value in values.items())
    if isinstance(values, list):
        parts = []
        for item in values:
            if isinstance(item, dict):
                parts.append(f"{item.get('value')}:{item.get('count')}")
            else:
                parts.append(str(item))
        return ", ".join(parts)
    return str(values)


def _alpha_label(node: dict[str, Any]) -> str:
    alpha_ids = node.get("alpha_ids") or []
    if alpha_ids:
        return str(alpha_ids[0])
    return str(node.get("node_id") or "")


def _log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
