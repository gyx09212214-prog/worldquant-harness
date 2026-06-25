"""CLI for compiling and validating WQ legal input registries."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from worldquant_harness.wq_legal_inputs import WQLegalInputRegistry, load_legal_input_registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile and validate WQ legal input registries")
    sub = parser.add_subparsers(dest="mode", required=True)

    compile_parser = sub.add_parser("compile", help="Compile a sanitized registry from wq_discover_fields.py output")
    compile_parser.add_argument("--discover-file", required=True)
    compile_parser.add_argument("--output", required=True)
    compile_parser.add_argument("--account", default="primary")

    validate = sub.add_parser("validate-file", help="Validate a candidate JSON/JSONL file against a registry")
    validate.add_argument("--registry", required=True)
    validate.add_argument("--candidate-file", required=True)
    validate.add_argument("--account", default="primary")
    validate.add_argument("--region", default="USA")
    validate.add_argument("--universe", default="TOP3000")
    validate.add_argument("--delay", type=int, default=1)
    validate.add_argument("--no-strict", action="store_true")

    summarize = sub.add_parser("summarize", help="Summarize a compiled registry")
    summarize.add_argument("--registry", required=True)

    args = parser.parse_args(argv)
    if args.mode == "compile":
        registry = WQLegalInputRegistry.compile_from_discovery(
            _resolve(args.discover_file),
            account=args.account,
        )
        out = registry.write(_resolve(args.output))
        result = {"ok": True, "output": str(out), "summary": registry.summary()}
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.mode == "validate-file":
        registry = load_legal_input_registry(_resolve(args.registry))
        rows = _load_candidates(_resolve(args.candidate_file))
        results = []
        invalid = 0
        for index, row in enumerate(rows, start=1):
            candidate = row if isinstance(row, dict) else {"expression": str(row)}
            validation = registry.validate_candidate(
                candidate,
                account=args.account,
                region=args.region,
                universe=args.universe,
                delay=args.delay,
                strict=not args.no_strict,
            )
            if not validation.ok:
                invalid += 1
            results.append({
                "index": index,
                "ok": validation.ok,
                "primary_error_code": validation.primary_error_code(),
                "expression": candidate.get("expression"),
                "validation": validation.to_dict(),
            })
        summary = {
            "ok": invalid == 0,
            "input": len(rows),
            "valid": len(rows) - invalid,
            "invalid": invalid,
            "results": results,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0 if invalid == 0 else 1

    if args.mode == "summarize":
        registry = load_legal_input_registry(_resolve(args.registry))
        print(json.dumps({"ok": True, "summary": registry.summary()}, ensure_ascii=False, indent=2, default=str))
        return 0

    parser.error(f"unsupported mode: {args.mode}")
    return 2


def _load_candidates(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        rows = []
        for line_no, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_no}: {exc}") from exc
        return rows
    data = json.loads(text)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("candidates"), list):
        return list(data["candidates"])
    if isinstance(data, dict):
        return [data]
    raise ValueError("candidate file must be JSON object, JSON array, or JSONL")


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
