"""Create strict-diversity rank-mask candidates from locally ACTIVE seeds."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.artifact_io import read_jsonl, utc_now, write_json, write_jsonl
from worldquant_harness.wq_diversity import (
    DiversityPolicy,
    build_diversity_record,
    evaluate_diversity,
    field_role,
    infer_family,
)
from worldquant_harness.wq_efficiency import annotate_candidate_identity
from worldquant_harness.wq_auto_mining import validate_wq_expression


LEGAL_INPUTS = ROOT / "configs/wq_legal_inputs.default.json"
THRESHOLDS = (0.65, 0.70, 0.75)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-root", default="reports/wq_agent_runs")
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=400)
    args = parser.parse_args(argv)

    report_root = _resolve(args.report_root)
    output = _resolve(args.output)
    active = _active_receipts(report_root)
    legacy = [build_diversity_record(row) for row in active]
    attempted = _attempted_uids(report_root)
    mask_fields = _mask_fields(active)
    candidates: list[dict[str, Any]] = []
    seen_uids: set[str] = set()
    rejected: Counter[str] = Counter()
    policy = DiversityPolicy()

    seeds = sorted(active, key=_seed_sort_key)
    for seed_index, seed in enumerate(seeds):
        base = build_diversity_record(seed)
        settings = seed.get("simulation_settings") if isinstance(seed.get("simulation_settings"), dict) else {}
        if not _same_live_scope(settings):
            rejected["outside_live_scope"] += 1
            continue
        available_masks = [field for field in mask_fields if field not in base["fields"]]
        if len(available_masks) < 6:
            continue
        family = infer_family(seed, base["fields"])
        for variant in range(3):
            offset = (seed_index * 11 + variant * 17) % len(available_masks)
            rotated = available_masks[offset:] + available_masks[:offset]
            built = None
            for mask_count in range(2, 7):
                selected_masks = rotated[:mask_count]
                mask_signal = "(" + "+".join(f"rank({field})" for field in selected_masks) + f")/{mask_count}"
                threshold = THRESHOLDS[variant]
                expression = f"({seed['expression']}) * (rank({mask_signal}) > {threshold:.2f})"
                row = {
                    "tag": f"round5-strict-seed-{seed['alpha_id']}-v{variant + 1}",
                    "source": "strict-diversity-active-seed-rank-mask",
                    "source_family": "strict-diversity-active-seed-rank-mask",
                    "candidate_family": f"{family}-seed-mask",
                    "diversity_family": family,
                    "diversity_core_signature": f"seed-alpha:{seed['alpha_id']}",
                    "expression": expression,
                    "simulation_settings": settings,
                    "parent_alpha_ids": [seed["alpha_id"]],
                    "partition_design": {
                        "design": "multi-field-composite-rank-upper-tail",
                        "seed_alpha_id": seed["alpha_id"],
                        "seed_self_correlation": seed.get("sc_value"),
                        "seed_sharpe": seed.get("sharpe"),
                        "seed_fitness": seed.get("fitness"),
                        "seed_turnover": seed.get("turnover"),
                        "mask_fields": selected_masks,
                        "mask_threshold": threshold,
                        "target_cross_section_share": round(1.0 - threshold, 6),
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
                try:
                    validate_wq_expression(expression)
                except Exception:
                    rejected["local_validation_failed"] += 1
                    continue
                annotated = annotate_candidate_identity(row, default_settings=settings)
                if str(annotated["candidate_uid"]) in attempted or str(annotated["candidate_uid"]) in seen_uids:
                    rejected["globally_attempted_or_duplicate_uid"] += 1
                    continue
                gate = evaluate_diversity(annotated, [], legacy, policy=policy)
                if gate["ok"]:
                    built = {
                        **annotated,
                        "field_signature": gate["record"]["field_signature"],
                        "primary_fields": gate["record"]["fields"],
                        "pool_legacy_weighted_jaccard": gate["max_legacy_weighted_jaccard"],
                    }
                    break
            if built is None:
                rejected["legacy_diversity_or_mask_build_failed"] += 1
                continue
            seen_uids.add(str(built["candidate_uid"]))
            candidates.append(built)

    ordered = _round_robin(candidates)
    selected = ordered[: max(0, args.limit)] if args.limit > 0 else ordered
    for index, row in enumerate(selected, start=1):
        row["candidate_rank"] = index
    write_jsonl(output, selected)
    audit = {
        "artifact_type": "wq_strict_diverse_active_seed_mask_audit",
        "schema_version": 1,
        "created_at": utc_now(),
        "offline_only": True,
        "submit_posts": 0,
        "criteria": {
            "self_correlation_operator": "<",
            "self_correlation_cutoff": 0.5,
            "legacy_weighted_field_jaccard_max": 0.75,
            "mask_fields_matrix_and_seen_in_active_inventory": True,
            "variants_per_seed": 3,
            "max_accepted_per_core": 2,
        },
        "counts": {
            "active_seeds": len(active),
            "mask_field_pool": len(mask_fields),
            "built": len(candidates),
            "selected": len(selected),
            "families": len({row["diversity_family"] for row in selected}),
            "unique_candidate_uids": len({row["candidate_uid"] for row in selected}),
        },
        "family_counts": dict(sorted(Counter(row["diversity_family"] for row in selected).items())),
        "rejected": dict(sorted(rejected.items())),
        "candidate_file_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "privacy": {"formulas_omitted_from_audit": True},
    }
    write_json(output.with_suffix(".audit.json"), audit)
    print(json.dumps({"ok": True, "output": str(output), "audit": audit}, ensure_ascii=False, indent=2))
    return 0


def _mask_fields(active: list[dict[str, Any]]) -> list[str]:
    registry = json.loads(LEGAL_INPUTS.read_text(encoding="utf-8"))
    legal = registry["accounts"]["primary"]["combos"]["USA|TOP3000|1"]["fields"]
    counts = Counter(field for row in active for field in build_diversity_record(row)["fields"])
    denied = {"actual_cashflow_per_share_value_quarterly", "rel_momentum", "rel_num_supp", "rel_ret_supp", "short_interest", "short_sale_cost"}
    return sorted(
        field
        for field, count in counts.items()
        if field_role(field) == "research"
        and field not in denied
        and not field.startswith("rp_")
        and field in legal
        and str(legal[field].get("type") or "").upper() == "MATRIX"
        and count <= 6
    )


def _round_robin(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for row in sorted(rows, key=lambda item: (_seed_sort_key(item), str(item.get("candidate_uid") or ""))):
        queues[str(row["diversity_family"])].append(row)
    families = sorted(queues)
    output: list[dict[str, Any]] = []
    while families:
        remaining = []
        for family in families:
            output.append(queues[family].popleft())
            if queues[family]:
                remaining.append(family)
        families = remaining
    return output


def _seed_sort_key(row: dict[str, Any]) -> tuple[float, float, float, str]:
    def number(key: str, default: float) -> float:
        try:
            return float(row.get(key))
        except (TypeError, ValueError):
            design = row.get("partition_design") if isinstance(row.get("partition_design"), dict) else {}
            design_key = {
                "sc_value": "seed_self_correlation",
                "sharpe": "seed_sharpe",
                "fitness": "seed_fitness",
                "turnover": "seed_turnover",
            }.get(key, f"seed_{key}")
            try:
                return float(design.get(design_key))
            except (TypeError, ValueError):
                return default
    sharpe = number("sharpe", 0.0)
    fitness = number("fitness", 0.0)
    quality_margin = min(sharpe / 1.25, fitness)
    return (-quality_margin, number("sc_value", 1.0), -sharpe, str(row.get("alpha_id") or ""))


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


def _same_live_scope(settings: dict[str, Any]) -> bool:
    return (
        str(settings.get("account") or "primary").lower() == "primary"
        and str(settings.get("region") or "USA").upper() == "USA"
        and str(settings.get("universe") or "TOP3000").upper() == "TOP3000"
        and int(settings.get("delay") or 1) == 1
    )


def _resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
