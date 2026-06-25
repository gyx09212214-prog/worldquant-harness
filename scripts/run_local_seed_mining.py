"""Run a first-pass local factor mining sweep through the worldquant-harness HTTP API."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.factor_miner import DEFAULT_SERVER, batch_evaluate, check_health


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate seed expressions against a worldquant-harness server")
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--expressions", default=str(ROOT / "scripts" / "local_seed_expressions.json"))
    parser.add_argument("--universe", default="small_scale")
    parser.add_argument("--benchmark", default="hs300")
    parser.add_argument("--start-date", default="2023-01-01")
    parser.add_argument("--end-date", default="2025-12-31")
    parser.add_argument("--holding-period", type=int, default=5)
    parser.add_argument("--n-groups", type=int, default=5)
    parser.add_argument("--max-concurrent", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    try:
        health = check_health(args.server)
    except Exception as exc:
        print(f"worldquant-harness server is not reachable at {args.server}: {exc}", file=sys.stderr)
        return 2

    expressions = json.loads(Path(args.expressions).read_text(encoding="utf-8"))
    params = {
        "universe": args.universe,
        "benchmark": args.benchmark,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "holding_period": args.holding_period,
        "n_groups": args.n_groups,
    }
    results = batch_evaluate(
        args.server,
        expressions,
        params,
        max_concurrent=args.max_concurrent,
        timeout=args.timeout,
    )

    bundle = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "server": args.server,
        "health": health,
        "params": params,
        "input_count": len(expressions),
        "result_count": len(results),
        "results": results,
    }
    output = Path(args.output) if args.output else ROOT / "reports" / f"local_seed_mining_{datetime.now():%Y%m%d_%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    if results:
        print(json.dumps(results[:10], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

