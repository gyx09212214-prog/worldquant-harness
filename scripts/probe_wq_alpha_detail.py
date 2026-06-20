"""Probe WQ alpha detail endpoints with read-only GET requests."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.wq_alpha_detail import render_probe_markdown, summarize_alpha_probe, write_probe_outputs
from quantgpt.wq_auto_mining import load_dotenv
from quantgpt.wq_brain_client import get_client, is_configured


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_dotenv(ROOT)

    output_dir = _resolve_path(args.output_dir) if args.output_dir else (
        ROOT / "reports" / f"wq_alpha_detail_{datetime.now():%Y%m%d_%H%M%S}"
    )
    alpha_ids = _load_alpha_ids(args.ids, [_resolve_path(value) for value in args.input])
    if args.limit > 0:
        alpha_ids = alpha_ids[: args.limit]
    if not alpha_ids:
        print(json.dumps({"ok": False, "error": "no alpha IDs provided"}, ensure_ascii=False), file=sys.stderr)
        return 2
    if not is_configured(args.account):
        print(
            json.dumps({"ok": False, "error": f"WQ BRAIN credentials are not configured (account={args.account})"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2

    client = get_client(args.account)
    summaries: list[dict] = []
    files_by_alpha: dict[str, dict[str, str]] = {}
    try:
        if not client.authenticate():
            print(json.dumps({"ok": False, "error": "WQ BRAIN authentication failed"}, ensure_ascii=False), file=sys.stderr)
            return 2
        for index, alpha_id in enumerate(alpha_ids):
            if args.delay_seconds > 0 and index > 0:
                time.sleep(args.delay_seconds)
            probe = client.probe_alpha_detail(alpha_id)
            summary = summarize_alpha_probe(probe)
            summaries.append(summary)
            files_by_alpha[alpha_id] = write_probe_outputs(output_dir, alpha_id, probe, summary)
    finally:
        client.close()

    markdown = render_probe_markdown(summaries, output_dir=output_dir)
    summary_payload = {
        "ok": True,
        "read_only": True,
        "output_dir": str(output_dir),
        "alpha_count": len(alpha_ids),
        "pnl_found_count": sum(1 for item in summaries if item.get("pnl_curve_found")),
        "summaries": summaries,
        "files": files_by_alpha,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (output_dir / "summary.md").write_text(markdown, encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "read_only": True,
        "output_dir": str(output_dir),
        "alpha_count": len(alpha_ids),
        "pnl_found_count": summary_payload["pnl_found_count"],
    }, ensure_ascii=False, indent=2))
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only WQ alpha detail/PnL endpoint probe")
    parser.add_argument("--ids", nargs="*", default=[], help="Explicit alpha IDs")
    parser.add_argument("--input", nargs="*", default=[], help="JSON/JSONL/text files containing alpha_id values")
    parser.add_argument("--account", default="primary")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay-seconds", type=int, default=0)
    return parser.parse_args(argv)


def _load_alpha_ids(explicit: list[str], input_paths: list[Path]) -> list[str]:
    ids: list[str] = []
    for alpha_id in explicit:
        if alpha_id and alpha_id not in ids:
            ids.append(alpha_id)
    for path in input_paths:
        for alpha_id in _ids_from_file(path):
            if alpha_id not in ids:
                ids.append(alpha_id)
    return ids


def _ids_from_file(path: Path) -> list[str]:
    if not path.is_file():
        return []
    ids: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            alpha_id = row.get("alpha_id") or (row.get("result") or {}).get("alpha_id")
            if alpha_id:
                ids.append(str(alpha_id))
        elif not line.startswith("[") and len(line) <= 80:
            ids.append(line.split()[0])
    return ids


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
