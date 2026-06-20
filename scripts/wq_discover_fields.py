"""Discover available WorldQuant BRAIN datasets and data fields for an account.

Usage:
    python scripts/wq_discover_fields.py --regions USA CHN --universes TOP3000 --limit 50

The script writes a JSON bundle under reports/ by default. It does not simulate
or submit alphas.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantgpt.wq_brain_client import WQBrainClient


def _load_dotenv():
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value:
            os.environ.setdefault(key, value)


def _results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("results", payload.get("data", []))
    return value if isinstance(value, list) else []


def _dataset_id(dataset: dict[str, Any]) -> str | None:
    for key in ("id", "datasetId", "name"):
        value = dataset.get(key)
        if value:
            return str(value)
    return None


def discover_combo(
    client: WQBrainClient,
    *,
    region: str,
    universe: str,
    delay: int,
    limit: int,
    max_datasets: int,
) -> dict[str, Any]:
    datasets_payload = client.list_data_sets(region=region, universe=universe, delay=delay)
    datasets = _results(datasets_payload)
    if max_datasets > 0:
        datasets = datasets[:max_datasets]

    fields_by_dataset: dict[str, Any] = {}
    if datasets:
        for dataset in datasets:
            dataset_id = _dataset_id(dataset)
            if not dataset_id:
                continue
            fields_by_dataset[dataset_id] = client.list_data_fields(
                region=region,
                universe=universe,
                delay=delay,
                dataset_id=dataset_id,
                limit=limit,
            )
    else:
        fields_by_dataset["_all"] = client.list_data_fields(
            region=region,
            universe=universe,
            delay=delay,
            limit=limit,
        )

    return {
        "region": region,
        "universe": universe,
        "delay": delay,
        "datasets": datasets_payload,
        "fields_by_dataset": fields_by_dataset,
    }


def main() -> int:
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Discover WQ BRAIN datasets and fields")
    parser.add_argument("--regions", nargs="+", default=["USA"])
    parser.add_argument("--universes", nargs="+", default=["TOP3000"])
    parser.add_argument("--delays", nargs="+", type=int, default=[1])
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--max-datasets", type=int, default=25)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    client = WQBrainClient()
    if not client.authenticate():
        print("WQ authentication failed. Check WQ_BRAIN_EMAIL/WQ_BRAIN_PASSWORD.", file=sys.stderr)
        return 2

    bundle: dict[str, Any] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "user": client.get_user_info(),
        "combos": [],
    }

    for region in args.regions:
        for universe in args.universes:
            for delay in args.delays:
                bundle["combos"].append(
                    discover_combo(
                        client,
                        region=region,
                        universe=universe,
                        delay=delay,
                        limit=args.limit,
                        max_datasets=args.max_datasets,
                    )
                )

    out_path = Path(args.output) if args.output else ROOT / "reports" / f"wq_available_fields_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
