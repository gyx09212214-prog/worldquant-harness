"""Lightweight retrieval over exported WorldQuant Community triage results."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_COMMUNITY_CONTEXT_DIR = Path(r"D:\tmp\worldquant_community_full_20260513\triage")
DEFAULT_CONTEXT_LIMIT = 6
DEFAULT_SEED_LIMIT = 12


@dataclass(frozen=True)
class CommunitySeed:
    expression: str
    tag: str | None = None
    source_post_id: str | None = None
    source_comment_id: str | None = None
    relevance_score: int = 0
    strategy: str = "community_seed"
    diagnosis: dict[str, Any] | None = None


@dataclass
class CommunityContext:
    context_dir: Path
    records: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    rules: str = ""
    findings: str = ""
    failures: str = ""

    @classmethod
    def from_dir(cls, context_dir: str | Path | None = None) -> CommunityContext | None:
        path = resolve_context_dir(context_dir)
        if not path or not path.is_dir():
            return None
        records_file = path / "triage_records.jsonl"
        candidates_file = path / "community_wq_candidates.jsonl"
        if not records_file.is_file() and not candidates_file.is_file():
            return None
        cached = _read_cache(path, records_file, candidates_file)
        if cached:
            return cls(
                context_dir=path,
                records=cached.get("records", []),
                candidates=cached.get("candidates", []),
                rules=str(cached.get("rules", "")),
                findings=str(cached.get("findings", "")),
                failures=str(cached.get("failures", "")),
            )
        context = cls(
            context_dir=path,
            records=_read_jsonl(records_file),
            candidates=_read_jsonl(candidates_file),
            rules=_read_text(path / "knowledge_suggestions" / "rules.md"),
            findings=_read_text(path / "knowledge_suggestions" / "findings.md"),
            failures=_read_text(path / "knowledge_suggestions" / "failures.md"),
        )
        _write_cache(context)
        return context

    def retrieve(
        self,
        *,
        query: str | None = None,
        expression: str | None = None,
        diagnosis: dict | None = None,
        fields_hint: list[str] | None = None,
        limit: int = DEFAULT_CONTEXT_LIMIT,
    ) -> str:
        selected = retrieve_community_records(
            self.records,
            query=query,
            expression=expression,
            diagnosis=diagnosis,
            fields_hint=fields_hint,
            limit=limit,
        )
        return render_community_context(selected, self.rules, self.failures)

    def seed_candidates(self, limit: int = DEFAULT_SEED_LIMIT, existing_expressions: list[str] | None = None) -> list[CommunitySeed]:
        return select_community_seeds(self.candidates, limit=limit, existing_expressions=existing_expressions)


def resolve_context_dir(context_dir: str | Path | None = None) -> Path | None:
    raw = str(context_dir or os.environ.get("WQ_COMMUNITY_CONTEXT_DIR") or "").strip()
    if raw:
        return Path(raw)
    return DEFAULT_COMMUNITY_CONTEXT_DIR


def resolve_cache_path(context_dir: Path) -> Path:
    raw = os.environ.get("WQ_COMMUNITY_CONTEXT_DB", "").strip()
    if raw:
        return Path(raw)
    return context_dir / "community_context_cache.json"


def retrieve_community_context(
    *,
    context_dir: str | Path | None = None,
    query: str | None = None,
    expression: str | None = None,
    diagnosis: dict | None = None,
    fields_hint: list[str] | None = None,
    limit: int = DEFAULT_CONTEXT_LIMIT,
) -> str:
    context = CommunityContext.from_dir(context_dir)
    if not context:
        return ""
    return context.retrieve(
        query=query,
        expression=expression,
        diagnosis=diagnosis,
        fields_hint=fields_hint,
        limit=limit,
    )


def retrieve_community_records(
    records: list[dict[str, Any]],
    *,
    query: str | None = None,
    expression: str | None = None,
    diagnosis: dict | None = None,
    fields_hint: list[str] | None = None,
    limit: int = DEFAULT_CONTEXT_LIMIT,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    wanted = _query_tokens(query, expression, diagnosis, fields_hint)
    scored: list[tuple[tuple[float, int, str], dict[str, Any]]] = []
    for index, record in enumerate(records):
        score = _record_score(record, wanted)
        if score <= 0 and int(record.get("relevance_score", 0) or 0) < 80:
            continue
        key = (
            score + min(100, int(record.get("relevance_score", 0) or 0)) / 100.0,
            -index,
            str(record.get("post_id") or "") + "/" + str(record.get("comment_id") or ""),
        )
        scored.append((key, record))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [record for _, record in scored[:limit]]


def select_community_seeds(
    candidates: list[dict[str, Any]],
    *,
    limit: int = DEFAULT_SEED_LIMIT,
    existing_expressions: list[str] | None = None,
) -> list[CommunitySeed]:
    if limit <= 0:
        return []
    seen = {_expression_hash(expr) for expr in existing_expressions or []}
    out: list[CommunitySeed] = []
    rows = sorted(candidates, key=lambda row: int(row.get("relevance_score", 0) or 0), reverse=True)
    for row in rows:
        expression = str(row.get("expression") or "").strip()
        if not expression:
            continue
        h = _expression_hash(expression)
        if h in seen:
            continue
        seen.add(h)
        out.append(
            CommunitySeed(
                expression=expression,
                tag=str(row.get("tag") or "community-seed"),
                source_post_id=_optional_str(row.get("source_post_id")),
                source_comment_id=_optional_str(row.get("source_comment_id")),
                relevance_score=int(row.get("relevance_score", 0) or 0),
                diagnosis={
                    "source": "worldquant_community",
                    "source_post_id": row.get("source_post_id"),
                    "source_comment_id": row.get("source_comment_id"),
                    "relevance_score": row.get("relevance_score"),
                },
            )
        )
        if len(out) >= limit:
            break
    return out


def render_community_context(records: list[dict[str, Any]], rules: str = "", failures: str = "") -> str:
    if not records and not rules and not failures:
        return ""
    lines = ["Community-derived reference notes:"]
    for record in records:
        hypothesis = _clean_inline(record.get("hypothesis") or record.get("title") or "Community hint", limit=140)
        score = int(record.get("relevance_score", 0) or 0)
        fields = ", ".join(str(item) for item in (record.get("wq_fields") or [])[:8])
        operators = ", ".join(str(item) for item in (record.get("operators") or [])[:8])
        risks = ", ".join(str(item) for item in (record.get("risk_flags") or [])[:6])
        candidates = "; ".join(_clean_inline(expr, limit=120) for expr in (record.get("candidate_expressions") or [])[:3])
        lines.append(f"- [{score}] {hypothesis}")
        if fields:
            lines.append(f"  Fields: {fields}")
        if operators:
            lines.append(f"  Operators: {operators}")
        if risks:
            lines.append(f"  Risks: {risks}")
        if candidates:
            lines.append(f"  Derived templates: {candidates}")
    rules_summary = _knowledge_summary(rules, "Rules")
    failures_summary = _knowledge_summary(failures, "Failures")
    if rules_summary:
        lines.append(rules_summary)
    if failures_summary:
        lines.append(failures_summary)
    return "\n".join(lines)


def _query_tokens(
    query: str | None,
    expression: str | None,
    diagnosis: dict | None,
    fields_hint: list[str] | None,
) -> set[str]:
    parts: list[str] = []
    if query:
        parts.append(query)
    if expression:
        parts.append(expression)
    if diagnosis:
        parts.append(str(diagnosis.get("strategy") or ""))
        parts.append(str(diagnosis.get("reason") or ""))
    if fields_hint:
        parts.extend(fields_hint[:80])
    return {_normalize_token(token) for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", " ".join(parts)) if len(token) >= 3}


def _record_score(record: dict[str, Any], wanted: set[str]) -> float:
    if not wanted:
        return min(100, int(record.get("relevance_score", 0) or 0)) / 50.0
    haystack = _record_tokens(record)
    overlap = len(wanted & haystack)
    score = float(overlap * 3)
    risk_flags = set(str(item) for item in record.get("risk_flags") or [])
    if "possible_complete_alpha" in risk_flags:
        score -= 1.5
    if record.get("value_type") == "failure_case":
        score += 1.0
    if record.get("candidate_expressions"):
        score += 1.0
    return score


def _record_tokens(record: dict[str, Any]) -> set[str]:
    values: list[str] = [
        str(record.get("title") or ""),
        str(record.get("hypothesis") or ""),
        str(record.get("excerpt") or ""),
        " ".join(str(item) for item in record.get("wq_fields") or []),
        " ".join(str(item) for item in record.get("operators") or []),
        " ".join(str(item) for item in record.get("risk_flags") or []),
        " ".join(str(item) for item in record.get("candidate_expressions") or []),
    ]
    return {_normalize_token(token) for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", " ".join(values)) if len(token) >= 3}


def _knowledge_summary(text: str, title: str) -> str:
    if not text:
        return ""
    bullets = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("- "):
            bullets.append(_clean_inline(line[2:], limit=150))
        if len(bullets) >= 3:
            break
    if not bullets:
        return ""
    return f"{title}: " + " | ".join(bullets)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig")


def _read_cache(context_dir: Path, records_file: Path, candidates_file: Path) -> dict[str, Any] | None:
    cache_path = resolve_cache_path(context_dir)
    if not cache_path.is_file():
        return None
    source_mtime = max(_mtime(records_file), _mtime(candidates_file))
    if _mtime(cache_path) < source_mtime:
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("context_dir") != str(context_dir):
        return None
    return data


def _write_cache(context: CommunityContext) -> None:
    cache_path = resolve_cache_path(context.context_dir)
    payload = {
        "context_dir": str(context.context_dir),
        "records": context.records,
        "candidates": context.candidates,
        "rules": context.rules,
        "findings": context.findings,
        "failures": context.failures,
    }
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        return


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def _clean_inline(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _normalize_token(token: str) -> str:
    return token.lower().strip("_")


def _expression_hash(expression: str) -> str:
    normalized = " ".join(expression.strip().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
