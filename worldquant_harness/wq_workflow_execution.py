"""Simulation and review agents for the WQ workflow."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .artifact_io import append_jsonl as _append_jsonl
from .artifact_io import read_jsonl as _read_jsonl
from .artifact_io import utc_now as _now
from .artifact_io import write_json as _write_json
from .artifact_io import write_jsonl as _write_jsonl
from .record_utils import first_float as _first_float
from .record_utils import first_text as _first_text
from .wq_agent_config import WorkflowPaths, WQAgentWorkflowConfig
from .wq_agent_records import simulation_setting_mismatches as _simulation_setting_mismatches
from .wq_agent_records import simulation_settings_for_candidate as _simulation_settings_for_candidate
from .wq_alpha_detail import summarize_alpha_probe
from .wq_brain_client import get_client, is_configured
from .wq_brain_service import run_check_submissions, run_single_simulation, submit_threshold_checks
from .wq_efficiency import annotate_candidate_identity
from .wq_pnl_analysis import (
    analyze_alpha_probe_summary,
    build_pnl_analysis_report,
    write_pnl_analysis_artifacts,
)
from .wq_workflow_constants import (
    ACTIVE_OR_SUBMITTED,
    CONFIRMED_READY,
    HARD_FAIL,
    INFRA_TIMEOUT,
    NEAR_MISS_REPAIR,
    SUBMIT_PROBE_NEEDED,
)
from .wq_workflow_context import _community_repair_annotations
from .wq_workflow_scoring import (
    _api_check_status,
    _check_result,
    _chunks,
    _failed_platform_checks,
    _is_metric_near_miss,
    _is_repairable_platform_fail,
    _is_simulation_timeout_result,
    _metrics_from_result,
    _needs_check,
    _review_check,
    _score,
    review_sort_key,
)


class SimulationAgent:
    """Run WQ simulations with auto_submit disabled."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths, *, dependencies: dict[str, Any] | None = None):
        self.config = config
        self.paths = paths
        self.dependencies = dependencies or {}

    def run(self) -> dict:
        candidates = _read_jsonl(self.paths.candidate_pool)[: self.config.max_simulations]
        if self.config.dry_run:
            rows = [self._dry_run_row(candidate) for candidate in candidates]
        else:
            _write_jsonl(self.paths.simulation_results, [])
            rows = self._simulate_candidates(candidates)
        _write_jsonl(self.paths.simulation_results, rows)
        counts = Counter(row.get("status") for row in rows)
        return {"ok": True, "simulated": len(rows), "counts": dict(sorted(counts.items())), "output": str(self.paths.simulation_results)}

    def _simulate_candidates(self, candidates: list[dict]) -> list[dict]:
        simulator = self.dependencies.get("simulate")
        if simulator:
            rows = []
            total = len(candidates)
            for index, candidate in enumerate(candidates, start=1):
                self._write_progress(index, total, candidate, status="started")
                effective_settings = _simulation_settings_for_candidate(candidate, self.config)
                sim_candidate = {**candidate, "effective_simulation_settings": effective_settings}
                row = classify_simulation_result(sim_candidate, simulator(sim_candidate, self.config))
                rows.append(row)
                _append_jsonl(self.paths.simulation_results, row)
                self._write_progress(index, total, candidate, status=row.get("status"), alpha_id=row.get("alpha_id"))
            return rows
        if not is_configured(self.config.account):
            raise RuntimeError(f"WQ BRAIN credentials are not configured (account={self.config.account})")
        client = get_client(self.config.account)
        try:
            if not client.authenticate():
                raise RuntimeError("WQ BRAIN authentication failed")
            rows = []
            total = len(candidates)
            for index, candidate in enumerate(candidates, start=1):
                self._write_progress(index, total, candidate, status="started")
                def _progress(percent: int, message: str, *, candidate=candidate, index=index) -> None:
                    self._write_progress(index, total, candidate, status="running", percent=percent, message=message)

                settings = _simulation_settings_for_candidate(candidate, self.config)
                result = run_single_simulation(
                    client,
                    candidate["expression"],
                    region=settings["region"],
                    universe=settings["universe"],
                    delay=settings["delay"],
                    decay=settings["decay"],
                    neutralization=settings["neutralization"],
                    truncation=settings["truncation"],
                    max_trade=settings["maxTrade"],
                    max_position=settings["maxPosition"],
                    auto_submit=False,
                    tag=candidate.get("tag"),
                    progress_callback=_progress,
                )
                sim_candidate = {**candidate, "effective_simulation_settings": settings}
                row = classify_simulation_result(sim_candidate, result)
                rows.append(row)
                _append_jsonl(self.paths.simulation_results, row)
                self._write_progress(index, total, candidate, status=row.get("status"), alpha_id=row.get("alpha_id"))
            return rows
        finally:
            client.close()

    def _write_progress(
        self,
        index: int,
        total: int,
        candidate: dict,
        *,
        status: str,
        percent: int | None = None,
        message: str | None = None,
        alpha_id: str | None = None,
    ) -> None:
        _write_json(self.paths.simulation_progress, {
            "updated_at": _now(),
            "current_index": index,
            "total": total,
            "status": status,
            "percent": percent,
            "message": message,
            "alpha_id": alpha_id,
            "candidate_rank": candidate.get("candidate_rank"),
            "tag": candidate.get("tag"),
            "expression": candidate.get("expression"),
        })

    def _dry_run_row(self, candidate: dict) -> dict:
        return {
            **candidate,
            "status": "dry_run",
            "submit_eligible": False,
            "submitted": False,
            "result": {"ok": True, "dry_run": True},
        }


class ReviewAgent:
    """Check and bucket simulated candidates into actionable queues."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths, *, dependencies: dict[str, Any] | None = None):
        self.config = config
        self.paths = paths
        self.dependencies = dependencies or {}

    def run(self) -> dict:
        rows = _read_jsonl(self.paths.simulation_results)
        check_results = self._check_rows(rows) if self.config.run_checks and not self.config.dry_run else {}
        reviewed = [classify_review_row(row, check_results.get(str(row.get("alpha_id") or ""))) for row in rows]
        pnl_summary = self._enrich_pnl(reviewed) if self.config.enrich_pnl and not self.config.dry_run else {
            "ok": True,
            "skipped": True,
            "reason": "pnl enrichment disabled",
        }
        reviewed.sort(key=review_sort_key)
        _write_jsonl(self.paths.review_queue, reviewed)
        counts = Counter(row.get("triage_bucket") for row in reviewed)
        return {
            "ok": True,
            "reviewed": len(reviewed),
            "counts": dict(sorted(counts.items())),
            "output": str(self.paths.review_queue),
            "pnl_enrichment": pnl_summary,
        }

    def _check_rows(self, rows: list[dict]) -> dict[str, dict]:
        ids = [str(row.get("alpha_id") or "") for row in rows if _needs_check(row)]
        ids = [alpha_id for alpha_id in dict.fromkeys(ids) if alpha_id]
        if not ids:
            return {}
        checker = self.dependencies.get("check_submissions") or self.dependencies.get("check_alphas")
        if checker:
            return checker(ids, self.config)
        client = get_client(self.config.account)
        try:
            if not client.authenticate():
                raise RuntimeError("WQ BRAIN authentication failed")
            out: dict[str, dict] = {}
            for chunk in _chunks(ids, max(1, self.config.check_chunk_size)):
                result = run_check_submissions(client, chunk)
                out.update(result.get("alphas") or {})
            return out
        finally:
            client.close()

    def _enrich_pnl(self, reviewed: list[dict]) -> dict:
        targets = _pnl_enrichment_targets(reviewed, limit=self.config.pnl_enrichment_limit)
        if not targets:
            _write_jsonl(self.paths.pnl_alpha_metrics, [])
            _write_jsonl(self.paths.pnl_yearly_metrics, [])
            return {"ok": True, "skipped": True, "reason": "no eligible alpha ids"}

        reports: list[dict] = []
        enricher = self.dependencies.get("pnl_enrichment")
        if enricher:
            result = enricher(targets, self.config)
            if isinstance(result, dict) and "alpha_reports" in result:
                reports = [row for row in result.get("alpha_reports") or [] if isinstance(row, dict)]
            elif isinstance(result, dict):
                reports = [row for row in result.values() if isinstance(row, dict)]
            elif isinstance(result, list):
                reports = [row for row in result if isinstance(row, dict)]
        else:
            reports = self._probe_pnl_reports(targets)

        by_id = {str(report.get("alpha_id") or ""): report for report in reports if report.get("alpha_id")}
        enriched = 0
        for row in reviewed:
            alpha_id = str(row.get("alpha_id") or "")
            report = by_id.get(alpha_id)
            if not report:
                continue
            _apply_pnl_report_to_review_row(row, report, min_score=self.config.pnl_min_stability_score)
            enriched += 1

        report_payload = build_pnl_analysis_report(reports, probe_dir=self.paths.output_dir)
        files = write_pnl_analysis_artifacts(report_payload, self.paths.output_dir)
        return {
            "ok": True,
            "requested": len(targets),
            "reported": len(reports),
            "enriched": enriched,
            "pnl_found": sum(1 for report in reports if report.get("pnl_curve_found") and report.get("yearly")),
            "files": files,
        }

    def _probe_pnl_reports(self, targets: list[dict]) -> list[dict]:
        if not is_configured(self.config.account):
            return []
        client = get_client(self.config.account)
        reports: list[dict] = []
        try:
            if not client.authenticate():
                raise RuntimeError("WQ BRAIN authentication failed")
            for row in targets:
                alpha_id = str(row.get("alpha_id") or "")
                if not alpha_id:
                    continue
                probe = client.probe_alpha_detail(alpha_id)
                summary = summarize_alpha_probe(probe)
                reports.append(
                    analyze_alpha_probe_summary(
                        summary,
                        probe=probe,
                        tag=str(row.get("tag") or ""),
                    )
                )
        finally:
            client.close()
        return reports


def classify_simulation_result(candidate: dict, result: dict) -> dict:
    metrics = _metrics_from_result(result)
    submit_gate = submit_threshold_checks(metrics)
    submit_eligible = bool(result.get("submit_eligible", submit_gate["eligible"]))
    checks = result.get("is_metrics", {}).get("checks") or []
    failed_platform_checks = _failed_platform_checks(checks)
    sc = _review_check(checks, "SELF_CORRELATION")
    prod = _review_check(checks, "PROD_CORRELATION")

    status = "simulated"
    if _is_simulation_timeout_result(result):
        status = "simulation_timeout"
    elif not result.get("ok", False):
        status = "failed"
    elif submit_eligible and failed_platform_checks:
        status = "failed_platform_check"
    elif _check_result(sc) == "FAIL" or _check_result(prod) == "FAIL":
        status = "failed_correlation_check"
    elif submit_eligible and (_check_result(sc) == "PENDING" or _check_result(prod) == "PENDING"):
        status = "pending_correlation_check"
    elif submit_eligible or submit_gate["eligible"]:
        status = "eligible"

    row = {
        **candidate,
        "created_at": _now(),
        "status": status,
        "alpha_id": result.get("alpha_id"),
        "sharpe": metrics.get("sharpe"),
        "fitness": metrics.get("fitness"),
        "returns": metrics.get("returns"),
        "turnover": metrics.get("turnover"),
        "submit_eligible": submit_eligible,
        "submitted": bool(result.get("submitted")),
        "submit_checks": result.get("submit_checks") or submit_gate["checks"],
        "is_checks": checks,
        "failed_platform_checks": failed_platform_checks,
        "self_correlation": sc,
        "prod_correlation": prod,
        "result": result,
    }
    actual_settings = result.get("settings") if isinstance(result.get("settings"), dict) else {}
    if actual_settings:
        row["actual_simulation_settings"] = actual_settings
    requested_settings = candidate.get("effective_simulation_settings") if isinstance(candidate.get("effective_simulation_settings"), dict) else {}
    mismatches = _simulation_setting_mismatches(requested_settings, actual_settings)
    if mismatches:
        row["simulation_setting_mismatches"] = mismatches
    return annotate_candidate_identity(row)


def classify_review_row(source_row: dict, check_result: dict | None = None) -> dict:
    check_result = check_result or {}
    row = {**source_row}
    metrics = {
        "sharpe": _first_float(check_result.get("sharpe"), source_row.get("sharpe")),
        "fitness": _first_float(check_result.get("fitness"), source_row.get("fitness")),
        "returns": _first_float(check_result.get("returns"), source_row.get("returns")),
        "turnover": _first_float(check_result.get("turnover"), source_row.get("turnover")),
    }
    gate = submit_threshold_checks(metrics)
    review_checks = check_result.get("review_checks") or {}
    sc_result = _first_text(check_result.get("sc_result"), source_row.get("sc_result"), (source_row.get("self_correlation") or {}).get("result"))
    prod_result = _first_text(check_result.get("prod_corr_result"), source_row.get("prod_corr_result"), (source_row.get("prod_correlation") or {}).get("result"))
    sc_value = _first_float(check_result.get("sc_value"), source_row.get("sc_value"), (source_row.get("self_correlation") or {}).get("value"))
    prod_value = _first_float(check_result.get("prod_corr_value"), source_row.get("prod_corr_value"), (source_row.get("prod_correlation") or {}).get("value"))
    api_status = _api_check_status(check_result, sc_result=sc_result, prod_result=prod_result)
    platform_status = str(check_result.get("status") or check_result.get("platform_status") or source_row.get("platform_status") or "").upper()
    failed_platform = source_row.get("failed_platform_checks") or []
    base_ok = bool(source_row.get("submit_eligible") or gate["eligible"])

    bucket = HARD_FAIL
    reason = "not submit eligible"
    if source_row.get("status") == "simulation_timeout" or _is_simulation_timeout_result(source_row.get("result") or {}):
        bucket = INFRA_TIMEOUT
        reason = "simulation polling timeout; retry with longer polling budget"
    elif platform_status in {"ACTIVE", "SUBMITTED"}:
        bucket = ACTIVE_OR_SUBMITTED
        reason = f"platform status is {platform_status}"
    elif failed_platform:
        bucket = NEAR_MISS_REPAIR if _is_repairable_platform_fail(source_row, failed_platform) else HARD_FAIL
        reason = "platform check failed"
    elif str(sc_result).upper() == "FAIL":
        bucket = NEAR_MISS_REPAIR if sc_value is not None and sc_value <= 0.85 else HARD_FAIL
        reason = f"self-correlation failed ({sc_value})"
    elif str(prod_result).upper() == "FAIL":
        bucket = HARD_FAIL
        reason = f"prod-correlation failed ({prod_value})"
    elif base_ok and api_status == "api_check_readable":
        bucket = CONFIRMED_READY
        reason = "check-only readable and no failed review checks"
    elif base_ok and api_status in {"api_check_pending", "api_check_missing"}:
        bucket = SUBMIT_PROBE_NEEDED
        reason = "base checks pass but correlation review is pending or missing"
    elif _is_metric_near_miss(metrics):
        bucket = NEAR_MISS_REPAIR
        reason = "metrics are near submit thresholds"

    row.update({
        "agent_stage": "review",
        "triage_bucket": bucket,
        "triage_reason": reason,
        "api_check_status": api_status,
        "platform_status": platform_status or None,
        "sharpe": metrics["sharpe"],
        "fitness": metrics["fitness"],
        "returns": metrics["returns"],
        "turnover": metrics["turnover"],
        "sc_result": sc_result,
        "sc_value": sc_value,
        "prod_corr_result": prod_result,
        "prod_corr_value": prod_value,
        "review_checks": review_checks,
        "submit_probe_reason": reason if bucket == SUBMIT_PROBE_NEEDED else None,
    })
    row.update(_community_repair_annotations(row))
    return annotate_candidate_identity(row)


def _pnl_enrichment_targets(reviewed: list[dict], *, limit: int) -> list[dict]:
    if limit <= 0:
        return []
    eligible_buckets = {CONFIRMED_READY, SUBMIT_PROBE_NEEDED, NEAR_MISS_REPAIR}
    targets: list[dict] = []
    seen: set[str] = set()
    for row in sorted(reviewed, key=review_sort_key):
        alpha_id = str(row.get("alpha_id") or "")
        if not alpha_id or alpha_id in seen:
            continue
        if row.get("triage_bucket") not in eligible_buckets:
            continue
        if not (row.get("submit_eligible") or _is_metric_near_miss({
            "sharpe": row.get("sharpe"),
            "fitness": row.get("fitness"),
            "turnover": row.get("turnover"),
        })):
            continue
        targets.append(row)
        seen.add(alpha_id)
        if len(targets) >= limit:
            break
    return targets


def _apply_pnl_report_to_review_row(row: dict, report: dict, *, min_score: float = 0.0) -> None:
    stability = report.get("stability") if isinstance(report.get("stability"), dict) else {}
    yearly = report.get("yearly") if isinstance(report.get("yearly"), list) else []
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    score = _score(stability.get("temporal_stability_score"), default=0.0)
    row["pnl_curve_found"] = bool(report.get("pnl_curve_found"))
    row["pnl_points"] = report.get("pnl_points")
    row["pnl_curve_path"] = report.get("pnl_curve_path") or ""
    row["temporal_stability"] = stability
    row["temporal_stability_score"] = score if yearly else None
    row["yearly_metrics"] = yearly
    row["pnl_warnings"] = warnings
    row["pnl_enrichment_status"] = "ok" if yearly else "missing_pnl_curve"
    if min_score > 0 and yearly and score < min_score:
        row["temporal_stability_warning"] = "below_min_stability_score"
