"""Build equal-weight ensembles from field-dissimilar ACTIVE components."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.artifact_io import read_jsonl, utc_now, write_json, write_jsonl
from worldquant_harness.wq_auto_mining import validate_wq_expression
from worldquant_harness.wq_diversity import (
    DiversityPolicy,
    build_diversity_record,
    evaluate_diversity,
    infer_family,
    weighted_field_jaccard,
)
from worldquant_harness.wq_efficiency import annotate_candidate_identity

SETTINGS = {
    "account": "primary",
    "region": "USA",
    "universe": "TOP3000",
    "delay": 1,
    "decay": 8,
    "neutralization": "SUBINDUSTRY",
    "truncation": 0.05,
    "maxTrade": "OFF",
    "maxPosition": "OFF",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-root", default="reports/wq_agent_runs")
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--seed", type=int, default=20260805)
    parser.add_argument(
        "--component-count",
        type=int,
        choices=(4, 5),
        default=5,
        help="Number of equal-weight ACTIVE components to combine.",
    )
    args = parser.parse_args(argv)

    report_root = _resolve(args.report_root)
    output = _resolve(args.output)
    active = _active_receipts(report_root)
    legacy = [build_diversity_record(row) for row in active]
    attempted = _attempted_uids(report_root)
    components = [_component(row) for row in active]
    components = [row for row in components if row is not None]
    rng = random.Random(args.seed)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    attempts = 0
    policy = DiversityPolicy()
    minimum_family_count = 4 if args.component_count == 4 else 3

    while len(candidates) < args.limit and attempts < 30000:
        attempts += 1
        chosen = rng.sample(components, args.component_count)
        if len({row["family"] for row in chosen}) < minimum_family_count:
            continue
        if any(
            weighted_field_jaccard(left["record"]["fields"], right["record"]["fields"]) > 0.45
            for left, right in itertools.combinations(chosen, 2)
        ):
            continue
        parent_ids = sorted(str(row["source"]["alpha_id"]) for row in chosen)
        key = "|".join(parent_ids)
        if key in seen:
            continue
        seen.add(key)
        component_weight = round(1.0 / args.component_count, 2)
        expression = _ensemble_expression(chosen, component_weight)
        try:
            validate_wq_expression(expression)
        except Exception:
            continue
        row = {
            "tag": f"round5-strict-active-ensemble-{len(candidates) + 1:03d}",
            "source": "equal-weight-field-dissimilar-active-ensemble",
            "source_family": "strict-diversity-active-ensembles",
            "candidate_family": f"multi-domain-{args.component_count}-active-ensemble",
            "diversity_family": "multi_domain_ensemble",
            "diversity_core_signature": f"active-ensemble:{hashlib.sha256(key.encode()).hexdigest()[:20]}",
            "expression": expression,
            "simulation_settings": SETTINGS,
            "parent_alpha_ids": parent_ids,
            "ensemble_design": {
                "component_count": args.component_count,
                "component_weight": component_weight,
                "minimum_component_family_count": minimum_family_count,
                "maximum_component_pair_field_jaccard": 0.45,
                "component_families": [item["family"] for item in chosen],
                "component_metrics": [
                    {
                        "alpha_id": item["source"]["alpha_id"],
                        "sharpe": item["source"].get("sharpe"),
                        "fitness": item["source"].get("fitness"),
                        "self_correlation": item["source"].get("sc_value"),
                    }
                    for item in chosen
                ],
            },
            "acceptance_gate": {
                "sharpe_min": 1.25,
                "fitness_min": 1.0,
                "turnover_max": 0.7,
                "self_corr_comparison": "numeric_value < cutoff",
                "self_corr_cutoff": 0.5,
            },
            "offline_only": True,
            "requires_fresh_simulation": True,
            "requires_fresh_platform_self_check": True,
            "requires_active_reconcile": True,
            "max_submit_retries": 0,
            "pending_submission": False,
        }
        annotated = annotate_candidate_identity(row, default_settings=SETTINGS)
        if str(annotated["candidate_uid"]) in attempted:
            continue
        gate = evaluate_diversity(annotated, [], legacy, policy=policy)
        if not gate["ok"]:
            continue
        candidates.append(
            {
                **annotated,
                "field_signature": gate["record"]["field_signature"],
                "primary_fields": gate["record"]["fields"],
                "pool_legacy_weighted_jaccard": gate["max_legacy_weighted_jaccard"],
                "predicted_component_quality_floor": round(
                    min(
                        min(float(item["source"]["sharpe"]) / 1.25, float(item["source"]["fitness"]))
                        for item in chosen
                    ),
                    6,
                ),
            }
        )

    candidates.sort(
        key=lambda row: (
            -float(row["predicted_component_quality_floor"]),
            float(row["pool_legacy_weighted_jaccard"]),
            str(row["candidate_uid"]),
        )
    )
    for index, row in enumerate(candidates, start=1):
        row["candidate_rank"] = index
    write_jsonl(output, candidates)
    audit = {
        "artifact_type": "wq_strict_diverse_active_ensemble_audit",
        "schema_version": 1,
        "created_at": utc_now(),
        "offline_only": True,
        "submit_posts": 0,
        "criteria": {
            "component_count": args.component_count,
            "component_weight": round(1.0 / args.component_count, 2),
            "minimum_component_family_count": minimum_family_count,
            "maximum_component_pair_field_jaccard": 0.45,
            "self_correlation_cutoff_exclusive": 0.5,
            "legacy_weighted_field_jaccard_max": 0.75,
        },
        "counts": {
            "active_inventory": len(active),
            "eligible_components": len(components),
            "sampling_attempts": attempts,
            "selected": len(candidates),
            "unique_candidate_uids": len({row["candidate_uid"] for row in candidates}),
            "unique_field_signatures": len({row["field_signature"] for row in candidates}),
        },
        "candidate_file_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "privacy": {"formulas_omitted_from_audit": True},
    }
    write_json(output.with_suffix(".audit.json"), audit)
    print(json.dumps({"ok": bool(candidates), "output": str(output), "audit": audit}, ensure_ascii=False, indent=2))
    return 0 if candidates else 1


def _component(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        sharpe = float(row.get("sharpe"))
        fitness = float(row.get("fitness"))
    except (TypeError, ValueError):
        return None
    if sharpe < 1.40 or fitness < 1.0 or len(str(row.get("expression") or "")) > 450:
        return None
    settings = row.get("simulation_settings") if isinstance(row.get("simulation_settings"), dict) else {}
    if str(settings.get("region") or "USA").upper() != "USA":
        return None
    record = build_diversity_record(row)
    return {"source": row, "record": record, "family": infer_family(row, record["fields"])}


def _ensemble_expression(chosen: list[dict[str, Any]], component_weight: float) -> str:
    terms = [
        f"{component_weight:.2f}*rank(({row['source']['expression']}))"
        for row in chosen
    ]
    return "rank(" + "+".join(terms) + ")"


def _active_receipts(report_root: Path) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for path in report_root.rglob("submit_results.jsonl"):
        for row in read_jsonl(path):
            alpha_id = str(row.get("alpha_id") or "").strip()
            if alpha_id and row.get("expression") and str(row.get("final_status") or "").upper() == "ACTIVE":
                by_id[alpha_id] = row
    return list(by_id.values())


def _attempted_uids(report_root: Path) -> set[str]:
    attempted = {
        str(row.get("candidate_uid"))
        for path in report_root.rglob("simulation_results.jsonl")
        for row in read_jsonl(path)
        if row.get("candidate_uid")
    }
    attempted.update(
        str(row.get("candidate_uid"))
        for path in report_root.rglob("agent_events.jsonl")
        for row in read_jsonl(path)
        if row.get("candidate_uid") and str(row.get("event_type") or "") == "simulation_started"
    )
    return attempted


def _resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
