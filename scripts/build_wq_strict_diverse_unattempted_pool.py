"""Build a globally unattempted pool for the strict SELF/field-diversity run."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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
    infer_family,
)
from worldquant_harness.wq_efficiency import annotate_candidate_identity
from worldquant_harness.wq_expression_utils import expression_fields, expression_operators


DEFAULT_SETTINGS = {
    "account": "primary",
    "region": "USA",
    "universe": "TOP3000",
    "delay": 1,
    "decay": 8,
    "neutralization": "SUBINDUSTRY",
    "truncation": 0.08,
    "maxTrade": "OFF",
    "maxPosition": "OFF",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-root", default="reports/wq_agent_runs")
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=600)
    args = parser.parse_args(argv)

    report_root = _resolve(args.report_root)
    output = _resolve(args.output)
    attempted = _attempted_uids(report_root)
    denied_operators, denied_fields = _failure_blacklists(report_root)
    legacy_rows = _active_receipts(report_root)
    legacy = [build_diversity_record(row) for row in legacy_rows]
    policy = DiversityPolicy()

    eligible_by_uid: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    rejection_reasons: Counter[str] = Counter()
    for path in report_root.rglob("candidates.jsonl"):
        if path.resolve() == output.resolve():
            continue
        for source_index, raw in enumerate(read_jsonl(path), start=1):
            if not raw.get("expression"):
                counts["missing_expression"] += 1
                continue
            settings = raw.get("simulation_settings") if isinstance(raw.get("simulation_settings"), dict) else DEFAULT_SETTINGS
            if not _same_live_scope(settings):
                counts["outside_live_scope"] += 1
                continue
            row = annotate_candidate_identity(raw, default_settings=settings)
            uid = str(row.get("candidate_uid") or "")
            if not uid:
                counts["missing_candidate_uid"] += 1
                continue
            if uid in attempted:
                counts["globally_attempted"] += 1
                continue
            if uid in eligible_by_uid:
                counts["duplicate_unattempted_uid"] += 1
                continue
            operators = set(expression_operators(str(row.get("expression") or "")))
            fields = set(expression_fields(str(row.get("expression") or "")))
            if operators & denied_operators:
                counts["known_inaccessible_operator"] += 1
                continue
            if fields & denied_fields:
                counts["known_unknown_field"] += 1
                continue
            if any(field.startswith("rp_") for field in fields):
                counts["unsupported_event_input_family"] += 1
                continue
            gate = evaluate_diversity(row, [], legacy, policy=policy)
            if not gate["ok"]:
                counts["legacy_diversity_blocked"] += 1
                rejection_reasons.update(gate["reasons"])
                continue
            record = gate["record"]
            family = infer_family(row, record["fields"])
            eligible_by_uid[uid] = {
                **row,
                "diversity_family": family,
                "diversity_core_signature": record["core_signature"],
                "field_signature": record["field_signature"],
                "primary_fields": record["fields"],
                "strict_self_correlation_cutoff": 0.5,
                "max_submit_retries": 0,
                "pool_source_path": str(path.relative_to(ROOT)),
                "pool_source_index": source_index,
                "pool_legacy_weighted_jaccard": gate["max_legacy_weighted_jaccard"],
            }

    ordered = _round_robin_candidates(list(eligible_by_uid.values()))
    selected = ordered[: max(0, args.limit)] if args.limit > 0 else ordered
    for index, row in enumerate(selected, start=1):
        row["candidate_rank"] = index

    write_jsonl(output, selected)
    payload_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
    audit = {
        "artifact_type": "wq_strict_diverse_unattempted_pool_audit",
        "schema_version": 1,
        "created_at": utc_now(),
        "offline_only": True,
        "submit_posts": 0,
        "criteria": {
            "globally_unattempted_candidate_uid": True,
            "self_correlation_operator": "<",
            "self_correlation_cutoff": 0.5,
            "max_submit_retries": 0,
            "legacy_weighted_field_jaccard_max": policy.max_legacy_weighted_jaccard,
            "family_round_robin": True,
            "field_signature_round_robin": True,
            "known_platform_failures_excluded": True,
        },
        "counts": {
            **dict(sorted(counts.items())),
            "attempted_uid_inventory": len(attempted),
            "legacy_active_inventory": len(legacy),
            "eligible_unique_uid": len(eligible_by_uid),
            "selected": len(selected),
            "selected_families": len({row["diversity_family"] for row in selected}),
            "selected_field_signatures": len({row["field_signature"] for row in selected}),
        },
        "rejection_reason_counts": dict(sorted(rejection_reasons.items())),
        "failure_blacklists": {
            "operators": sorted(denied_operators),
            "fields": sorted(denied_fields),
            "event_prefixes": ["rp_"],
        },
        "selected_family_counts": dict(sorted(Counter(row["diversity_family"] for row in selected).items())),
        "selected_source_counts": dict(sorted(Counter(row["pool_source_path"] for row in selected).items())),
        "candidate_file_sha256": payload_sha256,
        "privacy": {"formulas_omitted_from_audit": True},
    }
    write_json(output.with_suffix(".audit.json"), audit)
    print(json.dumps({"ok": True, "output": str(output), "audit": audit}, ensure_ascii=False, indent=2))
    return 0


def _round_robin_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_family_signature: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        by_family_signature[str(row["diversity_family"])][str(row["field_signature"])].append(row)
    family_queues: dict[str, deque[dict[str, Any]]] = {}
    for family, by_signature in by_family_signature.items():
        for variants in by_signature.values():
            variants.sort(key=_candidate_sort_key)
        signature_order = sorted(
            by_signature,
            key=lambda signature: (_candidate_sort_key(by_signature[signature][0]), signature),
        )
        interleaved: list[dict[str, Any]] = []
        depth = 0
        while True:
            added = False
            for signature in signature_order:
                variants = by_signature[signature]
                if depth < len(variants):
                    interleaved.append(variants[depth])
                    added = True
            if not added:
                break
            depth += 1
        family_queues[family] = deque(interleaved)

    output: list[dict[str, Any]] = []
    family_order = sorted(family_queues, key=lambda family: (_candidate_sort_key(family_queues[family][0]), family))
    while family_order:
        next_order: list[str] = []
        for family in family_order:
            queue = family_queues[family]
            if queue:
                output.append(queue.popleft())
            if queue:
                next_order.append(family)
        family_order = next_order
    return output


def _candidate_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    text = " ".join(
        str(row.get(key) or "").lower()
        for key in ("tag", "source", "source_family", "candidate_family", "pool_source_path")
    )
    bonus = 0
    for token, value in (
        ("strict06", -7),
        ("selfcorr", -6),
        ("lowself", -6),
        ("highmargin", -5),
        ("nearline", -4),
        ("quality", -3),
        ("strong", -3),
        ("repair", -2),
        ("frontier", -1),
        ("standalone", 2),
        ("simple", 2),
    ):
        if token in text:
            bonus += value
    legacy_overlap = float(row.get("pool_legacy_weighted_jaccard") or 0.0)
    field_count = len(row.get("primary_fields") or [])
    return (bonus, legacy_overlap, field_count, str(row.get("candidate_uid") or ""))


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


def _failure_blacklists(report_root: Path) -> tuple[set[str], set[str]]:
    operators: set[str] = set()
    fields: set[str] = set()
    for path in report_root.rglob("simulation_results.jsonl"):
        for row in read_jsonl(path):
            error = str(row.get("error") or "")
            if not error:
                continue
            operator = re.search(
                r'Attempted to use (?:inaccessible or )?unknown operator "([^"]+)"',
                error,
                re.IGNORECASE,
            )
            if operator:
                operators.add(operator.group(1).lower())
            field = re.search(
                r'Attempted to use unknown variable "([^"]+)"',
                error,
                re.IGNORECASE,
            )
            if field:
                fields.add(field.group(1).lower())
    return operators, fields


def _same_live_scope(settings: dict[str, Any]) -> bool:
    return (
        str(settings.get("account") or "primary").lower() == "primary"
        and str(settings.get("region") or "USA").upper() == "USA"
        and str(settings.get("universe") or "TOP3000").upper() == "TOP3000"
        and int(settings.get("delay") or 1) == 1
    )


def _active_receipts(report_root: Path) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for path in report_root.rglob("submit_results.jsonl"):
        for row in read_jsonl(path):
            alpha_id = str(row.get("alpha_id") or "").strip()
            if alpha_id and str(row.get("final_status") or "").upper() == "ACTIVE" and row.get("expression"):
                by_id[alpha_id] = row
    return list(by_id.values())


def _resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
