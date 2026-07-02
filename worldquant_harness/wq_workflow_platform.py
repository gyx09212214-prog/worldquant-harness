"""Platform synchronization agent for the WQ workflow."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .artifact_io import utc_now as _now
from .artifact_io import write_json as _write_json
from .artifact_io import write_jsonl as _write_jsonl
from .wq_agent_config import WorkflowPaths, WQAgentWorkflowConfig
from .wq_agent_records import workflow_settings as _settings
from .wq_brain_client import get_client, is_configured
from .wq_brain_service import run_list_alphas
from .wq_workflow_active import _fields, _operators


class PlatformSyncAgent:
    """Fetch platform alphas and mirror active records into the local ledger."""

    def __init__(self, config: WQAgentWorkflowConfig, paths: WorkflowPaths, *, dependencies: dict[str, Any] | None = None):
        self.config = config
        self.paths = paths
        self.dependencies = dependencies or {}

    def run(self) -> dict:
        if self.config.dry_run:
            rows = self.dependencies.get("platform_rows", [])
        else:
            rows = self._fetch_platform_alphas()

        _write_jsonl(self.paths.platform_alphas, rows)
        inventory = build_active_inventory(rows)
        _write_json(self.paths.active_inventory, inventory)
        ledger_summary = self._record_active_rows(rows) if self.config.use_ledger else {"ok": True, "skipped": True}
        return {
            "ok": True,
            "total": len(rows),
            "active": inventory["active_count"],
            "output": str(self.paths.platform_alphas),
            "active_inventory": str(self.paths.active_inventory),
            "ledger": ledger_summary,
        }

    def _fetch_platform_alphas(self) -> list[dict]:
        fetcher = self.dependencies.get("list_alphas")
        if fetcher:
            return list(fetcher(self.config))
        if not is_configured(self.config.account):
            raise RuntimeError(f"WQ BRAIN credentials are not configured (account={self.config.account})")

        client = get_client(self.config.account)
        try:
            if not client.authenticate():
                raise RuntimeError("WQ BRAIN authentication failed")
            out: list[dict] = []
            page_size = 100
            for offset in range(0, max(1, self.config.platform_sync_limit), page_size):
                result = run_list_alphas(client, limit=page_size, offset=offset)
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or "list alphas failed")
                page = result.get("alphas") or []
                out.extend(page)
                if len(page) < page_size or len(out) >= self.config.platform_sync_limit:
                    break
            return out[: self.config.platform_sync_limit]
        finally:
            client.close()

    def _record_active_rows(self, rows: list[dict]) -> dict:
        records = []
        for row in rows:
            status = str(row.get("status") or "").upper()
            if status not in {"ACTIVE", "SUBMITTED"}:
                continue
            records.append({
                **row,
                "api_check_status": "platform_active_check_readable",
                "platform_status": status,
                "source_submit_eligible": True,
                "source_file": str(self.paths.platform_alphas),
            })
        if not records:
            return {"ok": True, "recorded": 0}
        try:
            from .wq_alpha_ledger import record_api_check_records_sync

            return record_api_check_records_sync(
                records,
                settings=_settings(self.config),
                source_run_id=self.paths.output_dir.name,
            )
        except Exception as exc:
            return {"ok": False, "recorded": 0, "error": str(exc)}


def build_active_inventory(rows: list[dict]) -> dict:
    active = [row for row in rows if str(row.get("status") or "").upper() in {"ACTIVE", "SUBMITTED"}]
    field_counts: Counter[str] = Counter()
    operator_counts: Counter[str] = Counter()
    for row in active:
        field_counts.update(_fields(row.get("expression") or ""))
        operator_counts.update(_operators(row.get("expression") or ""))
    return {
        "created_at": _now(),
        "active_count": len(active),
        "field_counts": dict(sorted(field_counts.items())),
        "operator_counts": dict(sorted(operator_counts.items())),
        "active": active,
    }
