"""CLI for the role-based WQ agent workflow."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_agent_workflow import WQAgentWorkflowConfig, run_workflow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the role-based WorldQuant alpha workflow")
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("sync", "forum", "run", "postmortem", "submit", "run-submit", "presubmit-sequential"):
        _add_common_args(sub.add_parser(mode, help=f"{mode} workflow mode"))

    args = parser.parse_args(argv)
    config = _config_from_args(args)
    try:
        summary = run_workflow(config, mode=args.mode)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary.get("ok") else 1


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--candidate-files", nargs="*", default=[])
    parser.add_argument("--seed-ready-files", nargs="*", default=[])
    parser.add_argument("--seed-rejected-files", nargs="*", default=[])
    parser.add_argument("--community-context-dir", default="")
    parser.add_argument("--account", default="primary")
    parser.add_argument("--region", default="USA")
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--decay", type=int, default=8)
    parser.add_argument("--neutralization", default="SUBINDUSTRY")
    parser.add_argument("--truncation", type=float, default=0.08)
    parser.add_argument("--target-candidates", type=int, default=20)
    parser.add_argument("--max-simulations", type=int, default=40)
    parser.add_argument("--platform-sync-limit", type=int, default=2000)
    parser.add_argument("--check-chunk-size", type=int, default=1)
    parser.add_argument("--no-checks", action="store_true")
    parser.add_argument("--no-ledger", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-submit-probe", action="store_true")
    parser.add_argument("--submit-count", type=int, default=0)
    parser.add_argument("--alpha-ids", nargs="*", default=[])
    parser.add_argument(
        "--generation-mode",
        choices=["model-primary", "mixed", "template-fallback", "evolutionary", "mixed-evolutionary"],
        default="model-primary",
    )
    parser.add_argument("--model-candidates", type=int, default=0)
    parser.add_argument("--evolutionary-candidates", type=int, default=0)
    parser.add_argument("--model-retries", type=int, default=2)
    parser.add_argument("--fallback-template-limit", type=int, default=3)
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--no-platform-candidates", action="store_true", help="Do not fill candidate pools from UNSUBMITTED platform memory")
    parser.add_argument("--target-submissions", type=int, default=0)
    parser.add_argument("--target-ready", type=int, default=0)
    parser.add_argument("--max-total-simulations", type=int, default=2000)
    parser.add_argument("--cycle-candidate-count", type=int, default=40)
    parser.add_argument("--max-cycles", type=int, default=50)
    parser.add_argument("--max-consecutive-empty-cycles", type=int, default=3)
    parser.add_argument("--max-consecutive-submit-failures", type=int, default=5)
    parser.add_argument("--virtual-similarity-cutoff", type=float, default=0.65)
    parser.add_argument(
        "--presubmit-self-correlation-cutoff",
        type=float,
        default=None,
        help="Optional local self-correlation value cutoff for presubmit ready selection; by default platform PASS is trusted.",
    )
    parser.add_argument("--max-virtual-family-count", type=int, default=2)
    parser.add_argument("--max-virtual-field-signature-count", type=int, default=2)
    parser.add_argument("--submission-policy-file", default="")
    parser.add_argument("--legal-inputs", default="", help="Compiled WQ legal input registry JSON")
    parser.add_argument("--no-strict-legal-inputs", action="store_true", help="Warn instead of rejecting unknown registry fields")
    parser.add_argument("--enrich-pnl", action="store_true", help="Fetch read-only PnL detail and yearly stability metrics during review")
    parser.add_argument("--no-pnl-enrichment", action="store_true", help="Disable default PnL enrichment for run-submit")
    parser.add_argument("--pnl-enrichment-limit", type=int, default=8)
    parser.add_argument("--pnl-min-stability-score", type=float, default=0.0)
    parser.add_argument("--no-post-submit-review", action="store_true", help="Disable automatic post-submit local review artifacts")
    parser.add_argument("--post-submit-baseline-roots", nargs="*", default=[], help="Run roots used as baseline for post-submit review")
    parser.add_argument("--post-submit-profile-dir", default="", help="Research profile dir used for post-submit profile candidate context")
    parser.add_argument("--post-submit-window-days", type=int, default=14)
    parser.add_argument("--no-iteration-audit", action="store_true", help="Disable detailed iteration audit artifacts")
    parser.add_argument("--audit-history-limit", type=int, default=20, help="Number of sibling run audit summaries used for lightweight history baseline")
    parser.add_argument("--audit-include-expressions", action="store_true", help="Include full expressions in iteration_audit.jsonl; Markdown remains hash/field based")


def _config_from_args(args: argparse.Namespace) -> WQAgentWorkflowConfig:
    output_dir = _resolve_output_dir(args.output_dir, args.run_id)
    return WQAgentWorkflowConfig(
        output_dir=output_dir,
        candidate_files=[_resolve_path(path) for path in args.candidate_files],
        seed_ready_files=[_resolve_path(path) for path in args.seed_ready_files],
        seed_rejected_files=[_resolve_path(path) for path in args.seed_rejected_files],
        community_context_dir=_resolve_path(args.community_context_dir) if args.community_context_dir else None,
        account=args.account,
        region=args.region,
        universe=args.universe,
        delay=args.delay,
        decay=args.decay,
        neutralization=args.neutralization,
        truncation=args.truncation,
        target_candidates=args.target_candidates,
        max_simulations=args.max_simulations,
        platform_sync_limit=args.platform_sync_limit,
        check_chunk_size=args.check_chunk_size,
        run_checks=not args.no_checks,
        use_ledger=not args.no_ledger,
        dry_run=args.dry_run,
        allow_submit_probe=args.allow_submit_probe,
        submit_count=args.submit_count,
        submit_alpha_ids=args.alpha_ids,
        generation_mode=args.generation_mode,
        model_candidates=args.model_candidates,
        evolutionary_candidates=args.evolutionary_candidates,
        model_retries=args.model_retries,
        fallback_template_limit=args.fallback_template_limit,
        no_model=args.no_model,
        include_platform_candidates=not args.no_platform_candidates,
        target_submissions=args.target_submissions,
        target_ready=args.target_ready,
        max_total_simulations=args.max_total_simulations,
        cycle_candidate_count=args.cycle_candidate_count,
        max_cycles=args.max_cycles,
        max_consecutive_empty_cycles=args.max_consecutive_empty_cycles,
        max_consecutive_submit_failures=args.max_consecutive_submit_failures,
        virtual_similarity_cutoff=args.virtual_similarity_cutoff,
        presubmit_self_correlation_cutoff=args.presubmit_self_correlation_cutoff,
        max_virtual_family_count=args.max_virtual_family_count,
        max_virtual_field_signature_count=args.max_virtual_field_signature_count,
        submission_policy_file=_resolve_path(args.submission_policy_file) if args.submission_policy_file else None,
        legal_inputs_file=_resolve_path(args.legal_inputs) if args.legal_inputs else None,
        strict_legal_inputs=not args.no_strict_legal_inputs,
        enrich_pnl=bool(args.enrich_pnl or (args.mode == "run-submit" and not args.no_pnl_enrichment)),
        pnl_enrichment_limit=args.pnl_enrichment_limit,
        pnl_min_stability_score=args.pnl_min_stability_score,
        post_submit_review_enabled=not args.no_post_submit_review,
        post_submit_baseline_roots=[_resolve_path(path) for path in args.post_submit_baseline_roots],
        post_submit_profile_dir=_resolve_path(args.post_submit_profile_dir) if args.post_submit_profile_dir else None,
        post_submit_window_days=max(1, args.post_submit_window_days),
        iteration_audit_enabled=not args.no_iteration_audit,
        audit_history_limit=max(0, args.audit_history_limit),
        audit_include_expressions=bool(args.audit_include_expressions),
    )


def _resolve_output_dir(value: str, run_id: str) -> Path:
    if value:
        return _resolve_path(value)
    if run_id:
        return ROOT / "reports" / "wq_agent_runs" / run_id
    return ROOT / "reports" / "wq_agent_runs" / f"{datetime.now():%Y%m%d_%H%M%S}"


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
