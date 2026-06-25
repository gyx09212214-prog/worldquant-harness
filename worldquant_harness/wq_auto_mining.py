"""WorldQuant BRAIN-first autonomous alpha mining loop.

This module builds on the existing WQ transport/service layer. It evaluates
FASTEXPR candidates on the platform, diagnoses weak or rejected alphas, creates
next-generation candidates, and checkpoints every step for resumable long runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .community_context import CommunityContext
from .expression_parser import normalize_expression, parse_expression
from .llm_service import clean_expression
from .wq_brain_client import SUBMIT_THRESHOLDS, get_client, is_configured
from .wq_brain_service import run_single_simulation, safe_float, submit_threshold_checks
from .wq_review import parse_review_checks, primary_failure_kind, review_has_pending_correlation

logger = logging.getLogger(__name__)


TERMINAL_SUBMIT_STATUSES = {"active", "submitted"}
NON_EXPANDABLE_STATUSES = TERMINAL_SUBMIT_STATUSES | {"duplicate", "corr_pending", "submit_pending"}


@dataclass(frozen=True)
class WQCandidate:
    expression: str
    tag: str | None = None
    parent_id: str | None = None
    generation: int = 0
    strategy: str = "seed"
    diagnosis: dict | None = None
    source_index: int = 0
    id: str | None = None

    def with_id(self) -> WQCandidate:
        if self.id:
            return self
        raw = f"{normalize_expression(self.expression)}|{self.parent_id or ''}|{self.generation}|{self.source_index}"
        return WQCandidate(
            expression=self.expression,
            tag=self.tag,
            parent_id=self.parent_id,
            generation=self.generation,
            strategy=self.strategy,
            diagnosis=self.diagnosis,
            source_index=self.source_index,
            id=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12],
        )


@dataclass
class WQAutoMiningConfig:
    candidates_file: Path
    output_dir: Path
    results_file: Path
    checkpoint_file: Path
    status_file: Path
    submitted_file: Path
    summary_file: Path
    stop_file: Path
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    decay: int = 0
    neutralization: str = "SUBINDUSTRY"
    truncation: float = 0.08
    account: str = "primary"
    tag: str = "wq-auto-mine"
    max_runs: int = 200
    max_rounds: int = 30
    parents_per_round: int = 3
    children_per_parent: int = 4
    max_generation: int = 4
    max_consecutive_failures: int = 8
    target_submissions: int = 3
    direction: str | None = None
    fields_file: Path | None = None
    community_context_dir: Path | None = None
    community_context_mode: str = "auto"
    community_seed_limit: int = 12


def load_dotenv(root: Path | None = None) -> None:
    root = root or Path(__file__).resolve().parents[1]
    env_file = root / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value and not os.environ.get(key):
            os.environ[key] = value


def expression_hash(expression: str) -> str:
    return hashlib.sha256(normalize_expression(expression).encode("utf-8")).hexdigest()[:16]


def validate_wq_expression(expression: str) -> None:
    normalized = " ".join(expression.strip().split())
    if not normalized:
        raise ValueError("empty expression")
    if normalized[-1] in "+-*/^,":
        raise ValueError("expression ends with an operator")
    parse_expression(normalized, mode="wq")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    for attempt in range(8):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def load_candidates(path: Path) -> list[WQCandidate]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        rows: list[Any] = []
        for line_no, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rows.append((json.loads(line), line_no))
            except json.JSONDecodeError:
                rows.append((line, line_no))
        return [_candidate_from_value(value, idx).with_id() for value, idx in rows]

    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("candidate JSON must be an array")
    return [_candidate_from_value(value, i).with_id() for i, value in enumerate(data)]


def _candidate_from_value(value: Any, source_index: int) -> WQCandidate:
    if isinstance(value, str):
        expression = value.strip()
        if not expression:
            raise ValueError(f"empty expression at candidate #{source_index}")
        return WQCandidate(expression=expression, source_index=source_index)

    if isinstance(value, dict):
        expression = str(value.get("expression", "")).strip()
        if not expression:
            raise ValueError(f"missing expression at candidate #{source_index}")
        return WQCandidate(
            expression=expression,
            tag=str(value["tag"]) if value.get("tag") else None,
            parent_id=value.get("parent_id"),
            generation=int(value.get("generation", 0) or 0),
            strategy=str(value.get("strategy", "seed") or "seed"),
            diagnosis=value.get("diagnosis") if isinstance(value.get("diagnosis"), dict) else None,
            source_index=source_index,
            id=value.get("id"),
        )

    raise ValueError(f"unsupported candidate at #{source_index}: {type(value).__name__}")


def _first_not_none(*values):
    for value in values:
        if value is not None:
            return value
    return None


def extract_wq_metrics(result: dict | None) -> dict:
    result = result or {}
    wq = result.get("wq_brain") if isinstance(result.get("wq_brain"), dict) else {}
    is_metrics = result.get("is_metrics") if isinstance(result.get("is_metrics"), dict) else {}
    raw_is = result.get("is") if isinstance(result.get("is"), dict) else {}
    backtest = result.get("backtest_summary") if isinstance(result.get("backtest_summary"), dict) else {}

    metrics = {
        "sharpe": safe_float(_first_not_none(wq.get("wq_sharpe"), is_metrics.get("sharpe"), raw_is.get("sharpe"), backtest.get("long_short_sharpe"))),
        "fitness": safe_float(_first_not_none(wq.get("wq_fitness"), is_metrics.get("fitness"), raw_is.get("fitness"), backtest.get("wq_fitness"))),
        "returns": safe_float(_first_not_none(wq.get("wq_returns"), is_metrics.get("returns"), raw_is.get("returns"))),
        "turnover": safe_float(_first_not_none(wq.get("wq_turnover"), is_metrics.get("turnover"), raw_is.get("turnover"), backtest.get("turnover"))),
        "rating": _first_not_none(wq.get("wq_rating"), backtest.get("wq_rating"), result.get("rating")),
    }
    gate = submit_threshold_checks(metrics)
    metrics["submit_eligible"] = bool(result.get("submit_eligible", gate["eligible"]))
    metrics["submit_checks"] = result.get("submit_checks", gate["checks"])
    metrics["submit_thresholds"] = result.get("submit_thresholds", gate["thresholds"])
    return metrics


def classify_submit_result(submit_result: dict | None) -> dict:
    if submit_result is None:
        return {"status": "not_submitted", "reason": "not attempted"}

    detail = str(submit_result.get("detail", ""))
    platform_status = str(submit_result.get("platform_status", "") or "").upper()
    review_checks = submit_result.get("review_checks") or parse_review_checks(submit_result)
    failure_kind = submit_result.get("failure_kind") or primary_failure_kind(review_checks)

    if submit_result.get("ok"):
        return {
            "status": "active" if platform_status == "ACTIVE" else "submitted",
            "reason": detail or platform_status or "submitted",
            "platform_status": platform_status,
            "review_checks": review_checks,
        }

    if failure_kind == "self_correlation" or submit_result.get("sc_value") is not None or "SC FAIL" in detail.upper():
        return {
            "status": "self_corr_failed",
            "reason": detail or "self correlation failed",
            "platform_status": platform_status,
            "sc_value": submit_result.get("sc_value"),
            "sc_limit": submit_result.get("sc_limit"),
            "review_checks": review_checks,
        }
    if failure_kind == "prod_correlation" or submit_result.get("prod_value") is not None or "PROD" in detail.upper() and "CORRELATION" in detail.upper():
        return {
            "status": "prod_corr_failed",
            "reason": detail or "production correlation failed",
            "platform_status": platform_status,
            "prod_value": submit_result.get("prod_value"),
            "prod_limit": submit_result.get("prod_limit"),
            "review_checks": review_checks,
        }
    if failure_kind == "correlation_pending" or review_has_pending_correlation(review_checks):
        return {
            "status": "corr_pending",
            "reason": detail or "correlation review is pending",
            "platform_status": platform_status,
            "review_checks": review_checks,
        }
    if platform_status == "TIMEOUT":
        return {
            "status": "submit_pending",
            "reason": detail or "submission status pending",
            "platform_status": platform_status,
            "review_checks": review_checks,
        }
    return {
        "status": "failed",
        "reason": detail or submit_result.get("error", "submit failed"),
        "platform_status": platform_status,
        "review_checks": review_checks,
    }


def diagnose_wq_result(expression: str, result: dict | None, submit_result: dict | None = None) -> dict:
    if submit_result is not None:
        submit_status = classify_submit_result(submit_result)
        if submit_status["status"] in {"self_corr_failed", "sc_failed"}:
            return {
                "strategy": "avoid_self_correlation",
                "reason": "submission failed WQ self-correlation check; switch operator/field family",
                "details": submit_status,
            }
        if submit_status["status"] == "prod_corr_failed":
            return {
                "strategy": "avoid_prod_correlation",
                "reason": "submission failed WQ production-correlation check; change signal source and structure",
                "details": submit_status,
            }
        if submit_status["status"] == "corr_pending":
            return {
                "strategy": "wait_correlation_review",
                "reason": "correlation review is pending; do not expand yet",
                "details": submit_status,
            }
        if submit_status["status"] == "submit_pending":
            return {
                "strategy": "wait_submit_status",
                "reason": "submission status is pending; do not expand yet",
                "details": submit_status,
            }

    if not result or not result.get("ok", False):
        error = str((result or {}).get("error", "unknown error"))
        lower = error.lower()
        strategy = "repair_expression"
        if any(token in lower for token in ("concurrent", "rate", "timeout", "connection")):
            strategy = "platform_retry"
        elif any(token in lower for token in ("unknown", "operator", "field", "unit", "inaccessible", "unsupported")):
            strategy = "repair_expression"
        return {
            "strategy": strategy,
            "reason": error,
            "details": {"error": error},
        }

    metrics = extract_wq_metrics(result)
    thresholds = SUBMIT_THRESHOLDS
    sharpe = metrics.get("sharpe")
    fitness = metrics.get("fitness")
    returns_val = metrics.get("returns")
    turnover = metrics.get("turnover")

    if metrics.get("submit_eligible"):
        return {
            "strategy": "submit_ready",
            "reason": "Sharpe, fitness and turnover pass submit gates",
            "details": metrics,
        }
    if turnover is not None and turnover > thresholds["turnover_max"]:
        return {
            "strategy": "reduce_turnover",
            "reason": f"turnover {turnover:.4f} is above {thresholds['turnover_max']}",
            "details": metrics,
        }
    if turnover is not None and turnover < thresholds["turnover_min"]:
        return {
            "strategy": "increase_turnover",
            "reason": f"turnover {turnover:.4f} is below {thresholds['turnover_min']}",
            "details": metrics,
        }
    if sharpe is not None and sharpe < thresholds["sharpe"]:
        return {
            "strategy": "improve_sharpe",
            "reason": f"sharpe {sharpe:.4f} is below {thresholds['sharpe']}",
            "details": metrics,
        }
    if returns_val is not None and abs(returns_val) < 0.03:
        return {
            "strategy": "increase_returns",
            "reason": f"absolute returns {returns_val:.4f} are low",
            "details": metrics,
        }
    if fitness is not None and fitness < thresholds["fitness"]:
        return {
            "strategy": "improve_fitness",
            "reason": f"fitness {fitness:.4f} is below {thresholds['fitness']}",
            "details": metrics,
        }
    return {
        "strategy": "diversify",
        "reason": "metrics are usable but not submit eligible; explore adjacent families",
        "details": metrics,
    }


def expression_complexity(expression: str) -> dict:
    depth = 0
    max_depth = 0
    for char in expression:
        if char == "(":
            depth += 1
            max_depth = max(max_depth, depth)
        elif char == ")":
            depth = max(0, depth - 1)
    return {
        "length": len(expression),
        "nesting": max_depth,
    }


def ranking_key(entry: dict) -> tuple:
    metrics = entry.get("metrics") if isinstance(entry.get("metrics"), dict) else {}
    status = entry.get("status", "")
    status_score = {
        "active": 7,
        "submitted": 6,
        "eligible": 5,
        "corr_pending": 4,
        "submit_pending": 4,
        "simulated": 3,
        "self_corr_failed": 2,
        "prod_corr_failed": 2,
        "sc_failed": 2,
        "failed": 0,
        "duplicate": -1,
    }.get(status, 0)
    turnover = metrics.get("turnover")
    turnover_ok = turnover is not None and SUBMIT_THRESHOLDS["turnover_min"] <= turnover <= SUBMIT_THRESHOLDS["turnover_max"]
    complexity = expression_complexity(entry.get("expression", ""))
    return (
        status_score,
        1 if metrics.get("submit_eligible") else 0,
        _score(metrics.get("fitness")),
        _score(metrics.get("sharpe")),
        abs(_score(metrics.get("returns"), default=0.0)),
        1 if turnover_ok else 0,
        -complexity["nesting"],
        -complexity["length"],
    )


def _score(value: Any, default: float = float("-inf")) -> float:
    parsed = safe_float(value)
    return default if parsed is None else parsed


def build_wq_mutation_prompt(
    expression: str,
    diagnosis: dict,
    history: list[str],
    *,
    direction: str | None = None,
    fields_hint: list[str] | None = None,
    community_context: str | None = None,
) -> str:
    metrics = diagnosis.get("details", {})
    if isinstance(metrics, dict) and "details" in metrics:
        metrics = metrics["details"]
    parts = [
        f"Current alpha: {expression}",
        f"Diagnosis strategy: {diagnosis.get('strategy')}",
        f"Diagnosis reason: {diagnosis.get('reason')}",
        "Target: generate one different WorldQuant BRAIN FASTEXPR alpha.",
        "Hard constraints:",
        "- Use only WQ BRAIN FASTEXPR operators and fields.",
        "- Return exactly one expression, no markdown, no explanation.",
        "- Avoid pasteurize(), tanh(), sigmoid(), clip(), ema(), sma(), wma(), rsi(), macd(), atr(), boll_*.",
        "- Keep expression length under 500 chars and nesting under 10.",
        "- Do not merely change one numeric window after self-correlation failure.",
        "- After production-correlation failure, change the field family or operator family, not just parameters.",
        "",
        f"Metrics: {json.dumps(metrics, ensure_ascii=False, default=str)[:1200]}",
    ]
    if direction:
        parts.append(f"User direction: {direction}")
    if fields_hint:
        parts.append("Available field hints: " + ", ".join(fields_hint[:60]))
    if community_context:
        parts.extend([
            "",
            community_context,
            "Use the Community notes as inspiration only; do not copy suspected complete alphas verbatim.",
        ])
    if history:
        parts.append("Already used expressions:")
        for expr in history[-20:]:
            parts.append(f"- {expr}")
    parts.append("New expression:")
    return "\n".join(parts)


def generate_child_expressions(
    expression: str,
    result: dict | None,
    history: list[str],
    *,
    n_children: int,
    direction: str | None = None,
    fields_hint: list[str] | None = None,
    submit_result: dict | None = None,
    community_context: str | None = None,
    llm_call: Callable[[str], str] | None = None,
) -> list[dict]:
    diagnosis = diagnose_wq_result(expression, result, submit_result)
    if diagnosis["strategy"] in {"submit_ready", "wait_submit_status", "wait_correlation_review", "platform_retry"}:
        return []

    raw_children = _heuristic_children(expression, diagnosis)
    llm_call = llm_call or _call_llm_child
    while len(raw_children) < n_children and os.environ.get("DEEPSEEK_API_KEY"):
        try:
            prompt = build_wq_mutation_prompt(
                expression,
                diagnosis,
                history + raw_children,
                direction=direction,
                fields_hint=fields_hint,
                community_context=community_context,
            )
            raw_children.append(llm_call(prompt))
        except Exception as exc:
            logger.warning(f"LLM child generation failed: {exc}")
            break

    out: list[dict] = []
    seen = {normalize_expression(expr) for expr in history}
    for raw in raw_children:
        expr = clean_expression(str(raw))
        if not expr:
            continue
        norm = normalize_expression(expr)
        if norm in seen:
            continue
        try:
            validate_wq_expression(expr)
        except Exception as exc:
            logger.info(f"Generated child rejected by WQ parser: {expr} ({exc})")
            continue
        seen.add(norm)
        out.append({
            "expression": expr,
            "strategy": diagnosis["strategy"],
            "diagnosis": diagnosis,
        })
        if len(out) >= n_children:
            break
    return out


def _heuristic_children(expression: str, diagnosis: dict) -> list[str]:
    strategy = diagnosis.get("strategy", "")
    ranked = expression if expression.strip().startswith(("rank(", "-rank(")) else f"rank({expression})"
    base_templates = {
        "reduce_turnover": [
            f"decay_linear({ranked}, 5)",
            f"decay_linear({ranked}, 10)",
            f"rank(ts_mean(({expression}), 5))",
        ],
        "increase_turnover": [
            "rank(ts_delta(close, 3)) * rank(volume / ts_mean(volume, 20))",
            "rank(ts_delta(vwap, 3) / ts_shift(vwap, 3))",
            "rank(ts_rank(returns, 10))",
        ],
        "improve_sharpe": [
            "rank(ts_corr(close, volume, 20)) * rank(-1 * ts_std(returns, 20))",
            "rank((vwap - close) / close)",
            "-1 * rank(ts_delta(close, 5))",
        ],
        "increase_returns": [
            "rank(ts_delta(close, 5) / ts_shift(close, 5)) * rank(volume / ts_mean(volume, 20))",
            "rank(ts_rank(close, 20)) * rank(ts_corr(vwap, volume, 20))",
            "rank((close - ts_min(close, 20)) / (ts_max(close, 20) - ts_min(close, 20)))",
        ],
        "improve_fitness": [
            "decay_linear(rank((vwap - close) / close), 5)",
            "rank(ts_rank(returns, 40))",
            "-1 * ts_av_diff(close, 50) * ts_corr(close, volume, 50)",
        ],
        "avoid_self_correlation": [
            "rank(ts_corr(vwap, volume, 20))",
            "rank(volume / ts_mean(volume, 20)) * rank(-1 * ts_std(returns, 20))",
            "rank(ts_rank(returns, 40))",
            "rank((vwap - close) / close)",
        ],
        "avoid_prod_correlation": [
            "trade_when(volume > ts_mean(volume, 20), rank(ts_corr(vwap, volume, 30)) * rank(-1 * ts_std(returns, 20)), -1)",
            "group_neutralize(rank(ts_rank(returns, 60)) * rank(volume / ts_mean(volume, 40)), subindustry)",
            "rank(ts_corr(rank(vwap), rank(volume), 40)) * rank((close - vwap) / vwap)",
            "hump(decay_linear(rank(ts_delta(vwap, 7)) * rank(ts_std(returns, 30)), 8), 0.01)",
        ],
        "repair_expression": [
            _strip_unsupported(expression),
            "rank(ts_delta(close, 5))",
            "rank(ts_corr(close, volume, 20))",
        ],
        "diversify": [
            "rank(ts_corr(close, volume, 10))",
            "rank((vwap - close) / close) * rank(volume / ts_mean(volume, 20))",
            "rank(-1 * ts_std(returns, 20))",
        ],
    }
    return [item for item in base_templates.get(strategy, base_templates["diversify"]) if item]


def _strip_unsupported(expression: str) -> str:
    replacements = {
        "ts_decay_linear": "decay_linear",
        "delay": "ts_shift",
        "delta": "ts_delta",
        "stddev": "ts_std",
        "correlation": "ts_corr",
        "covariance": "ts_cov",
    }
    out = expression
    for old, new in replacements.items():
        out = re.sub(rf"\b{re.escape(old)}\b", new, out)
    return out


def _call_llm_child(prompt: str) -> str:
    from openai import OpenAI

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    client = OpenAI(api_key=api_key, base_url=base_url)
    system = (
        "You are a WorldQuant BRAIN FASTEXPR alpha improvement assistant. "
        "Return exactly one valid FASTEXPR expression and nothing else."
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        temperature=0.9,
        max_tokens=256,
        timeout=60,
    )
    return clean_expression(resp.choices[0].message.content)


def load_fields_hint(path: Path | None) -> list[str]:
    if not path or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    fields: set[str] = set()

    def visit(value: Any):
        if isinstance(value, dict):
            for key in ("id", "name", "field", "dataField"):
                item = value.get(key)
                if isinstance(item, str) and re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", item):
                    fields.add(item)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(data)
    return sorted(fields)[:200]


class WQAutoMiner:
    def __init__(
        self,
        config: WQAutoMiningConfig,
        *,
        client_factory: Callable[[str], Any] | None = None,
        configured_check: Callable[[str], bool] | None = None,
        simulation_fn: Callable[..., dict] | None = None,
        child_generator: Callable[..., list[dict]] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ):
        self.config = config
        self.client_factory = client_factory or get_client
        self.configured_check = configured_check or is_configured
        self.simulation_fn = simulation_fn or run_single_simulation
        self.child_generator = child_generator or generate_child_expressions
        self.sleep_fn = sleep_fn or time.sleep

        self.queue: list[WQCandidate] = []
        self.seen_hashes: set[str] = set()
        self.expression_history: list[str] = []
        self.counters = {
            "runs_started": 0,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "submitted": 0,
            "active": 0,
            "sc_failed": 0,
            "self_corr_failed": 0,
            "prod_corr_failed": 0,
            "corr_pending": 0,
            "children_generated": 0,
            "community_seeds_added": 0,
        }
        self.best: dict | None = None
        self.round_index = 0
        self.consecutive_failures = 0
        self.fields_hint: list[str] = []
        self.community_context: CommunityContext | None = None
        self.community_context_info: dict[str, Any] = {
            "mode": self.config.community_context_mode,
            "loaded": False,
            "context_dir": str(self.config.community_context_dir) if self.config.community_context_dir else "",
            "records": 0,
            "candidates": 0,
            "seeds_added": 0,
        }

    def run(self) -> int:
        load_dotenv()
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self.fields_hint = load_fields_hint(self.config.fields_file)
        self._load_community_context()
        self._load_state()
        self._update_status(status="STARTING", message="starting WQ auto mining")

        if self.config.max_runs <= self.counters["runs_started"]:
            self._finish("SUCCESS", "max_runs_reached")
            return 0
        if self.config.target_submissions > 0 and self.counters["submitted"] >= self.config.target_submissions:
            self._finish("SUCCESS", "target_submissions_reached")
            return 0
        if not self.configured_check(self.config.account):
            self._finish("FAILED", "WQ credentials are not configured")
            return 2

        client = self.client_factory(self.config.account)
        try:
            self._update_status(status="AUTHENTICATING", message="Authenticating to WQ BRAIN")
            if not client.authenticate():
                self._finish("FAILED", "WQ authentication failed")
                return 3

            while True:
                stop = self._stop_reason()
                if stop:
                    status = "STOPPED" if stop == "stop_file_detected" else "SUCCESS"
                    self._finish(status, stop)
                    return 0

                self.round_index += 1
                self._update_status(status="RUNNING", round=self.round_index, queue_size=len(self.queue))
                entries: list[dict] = []

                while (
                    self.queue
                    and len(entries) < self.config.parents_per_round
                    and self.counters["runs_started"] < self.config.max_runs
                ):
                    if self.config.target_submissions > 0 and self.counters["submitted"] >= self.config.target_submissions:
                        break
                    candidate = self.queue.pop(0).with_id()
                    entry = self._process_candidate(client, candidate)
                    entries.append(entry)

                if entries:
                    self._expand_from_entries(entries)
                    self._save_progress()
                else:
                    self._finish("SUCCESS", "queue_empty")
                    return 0

                if self.consecutive_failures >= self.config.max_consecutive_failures:
                    self._finish("FAILED", f"max_consecutive_failures_reached ({self.consecutive_failures})")
                    return 4
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _stop_reason(self) -> str | None:
        if self.config.stop_file.is_file():
            return "stop_file_detected"
        if self.config.target_submissions > 0 and self.counters["submitted"] >= self.config.target_submissions:
            return "target_submissions_reached"
        if self.counters["runs_started"] >= self.config.max_runs:
            return "max_runs_reached"
        if self.round_index >= self.config.max_rounds:
            return "max_rounds_reached"
        if not self.queue:
            return "queue_empty"
        return None

    def _process_candidate(self, client, candidate: WQCandidate) -> dict:
        candidate_hash = expression_hash(candidate.expression)
        if candidate_hash in self.seen_hashes:
            self.counters["skipped"] += 1
            entry = self._entry(candidate, "duplicate", candidate_hash, error="expression already processed")
            append_jsonl(self.config.results_file, entry)
            return entry

        try:
            validate_wq_expression(candidate.expression)
        except Exception as exc:
            self.seen_hashes.add(candidate_hash)
            self.expression_history.append(candidate.expression)
            self.counters["skipped"] += 1
            entry = self._entry(candidate, "failed", candidate_hash, error=f"local WQ validation failed: {exc}")
            entry["diagnosis"] = diagnose_wq_result(candidate.expression, {"ok": False, "error": str(exc)})
            append_jsonl(self.config.results_file, entry)
            return entry

        self.seen_hashes.add(candidate_hash)
        self.expression_history.append(candidate.expression)
        self.counters["runs_started"] += 1

        def on_progress(progress: int, message: str) -> None:
            self._update_status(
                status="RUNNING",
                current_expression=candidate.expression,
                current_progress=progress,
                message=_ascii_progress_message(progress, message),
            )

        try:
            result = self.simulation_fn(
                client,
                candidate.expression,
                region=self.config.region,
                universe=self.config.universe,
                delay=self.config.delay,
                decay=self.config.decay,
                neutralization=self.config.neutralization,
                truncation=self.config.truncation,
                auto_submit=False,
                tag=candidate.tag or self.config.tag,
                progress_callback=on_progress,
            )
        except Exception as exc:
            logger.error(f"WQ simulation crashed: {traceback.format_exc()}")
            result = {"ok": False, "error": str(exc)}

        if not result.get("ok"):
            self.counters["failed"] += 1
            self.consecutive_failures += 1
            entry = self._entry(candidate, "failed", candidate_hash, result=result, error=result.get("error", "simulation failed"))
            entry["diagnosis"] = diagnose_wq_result(candidate.expression, result)
            append_jsonl(self.config.results_file, entry)
            return entry

        self.counters["completed"] += 1
        self.consecutive_failures = 0
        metrics = extract_wq_metrics(result)
        status = "eligible" if metrics["submit_eligible"] else "simulated"
        submit_result = None
        submit_status = None

        if (
            metrics["submit_eligible"]
            and result.get("alpha_id")
            and self.config.target_submissions > 0
            and self.counters["submitted"] < self.config.target_submissions
        ):
            submit_result = client.submit_alpha(result["alpha_id"])
            submit_status = classify_submit_result(submit_result)
            status = submit_status["status"]
            if status in TERMINAL_SUBMIT_STATUSES:
                self.counters["submitted"] += 1
                if status == "active":
                    self.counters["active"] += 1
            elif status == "self_corr_failed":
                self.counters["self_corr_failed"] += 1
                self.counters["sc_failed"] += 1
            elif status == "prod_corr_failed":
                self.counters["prod_corr_failed"] += 1
            elif status == "corr_pending":
                self.counters["corr_pending"] += 1

        entry = self._entry(
            candidate,
            status,
            candidate_hash,
            result=result,
            metrics=metrics,
            submit_result=submit_result,
            submit_status=submit_status,
        )
        entry["diagnosis"] = diagnose_wq_result(candidate.expression, result, submit_result)
        self.best = best_entry(self.best, entry)
        append_jsonl(self.config.results_file, entry)
        if status in TERMINAL_SUBMIT_STATUSES:
            append_jsonl(self.config.submitted_file, entry)
        return entry

    def _entry(
        self,
        candidate: WQCandidate,
        status: str,
        candidate_hash: str,
        *,
        result: dict | None = None,
        metrics: dict | None = None,
        submit_result: dict | None = None,
        submit_status: dict | None = None,
        error: str | None = None,
    ) -> dict:
        entry = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "id": candidate.id,
            "hash": candidate_hash,
            "status": status,
            "expression": candidate.expression,
            "tag": candidate.tag or self.config.tag,
            "parent_id": candidate.parent_id,
            "generation": candidate.generation,
            "strategy": candidate.strategy,
            "candidate_diagnosis": candidate.diagnosis,
            "source_index": candidate.source_index,
            "params": {
                "region": self.config.region,
                "universe": self.config.universe,
                "delay": self.config.delay,
                "decay": self.config.decay,
                "neutralization": self.config.neutralization,
                "truncation": self.config.truncation,
            },
        }
        if result is not None:
            entry["result"] = result
            entry["alpha_id"] = result.get("alpha_id")
        if metrics is not None:
            entry["metrics"] = metrics
            entry["sharpe"] = metrics.get("sharpe")
            entry["fitness"] = metrics.get("fitness")
            entry["returns"] = metrics.get("returns")
            entry["turnover"] = metrics.get("turnover")
            entry["submit_eligible"] = metrics.get("submit_eligible")
        if submit_result is not None:
            entry["submit_result"] = submit_result
        if submit_status is not None:
            entry["submit_status"] = submit_status
        if error:
            entry["error"] = error
        return entry

    def _expand_from_entries(self, entries: list[dict]) -> None:
        expandable = [
            entry for entry in entries
            if entry.get("status") not in NON_EXPANDABLE_STATUSES
            and int(entry.get("generation", 0) or 0) < self.config.max_generation
        ]
        expandable.sort(key=ranking_key, reverse=True)
        queued_hashes = {expression_hash(candidate.expression) for candidate in self.queue}

        for entry in expandable[:self.config.parents_per_round]:
            if self.config.target_submissions > 0 and self.counters["submitted"] >= self.config.target_submissions:
                return
            result = entry.get("result") or {"ok": False, "error": entry.get("error", "no result")}
            community_context = self._community_context_for_entry(entry, result)
            children = self.child_generator(
                entry["expression"],
                result,
                self.expression_history + [candidate.expression for candidate in self.queue],
                n_children=self.config.children_per_parent,
                direction=self.config.direction,
                fields_hint=self.fields_hint,
                submit_result=entry.get("submit_result"),
                community_context=community_context,
            )
            for idx, child in enumerate(children):
                expr = child["expression"]
                h = expression_hash(expr)
                if h in self.seen_hashes or h in queued_hashes:
                    continue
                candidate = WQCandidate(
                    expression=expr,
                    tag=self.config.tag,
                    parent_id=entry.get("id"),
                    generation=int(entry.get("generation", 0) or 0) + 1,
                    strategy=child.get("strategy", "generated"),
                    diagnosis=child.get("diagnosis"),
                    source_index=idx,
                ).with_id()
                self.queue.append(candidate)
                queued_hashes.add(h)
                self.counters["children_generated"] += 1

    def _load_state(self) -> None:
        if self.config.checkpoint_file.is_file():
            data = json.loads(self.config.checkpoint_file.read_text(encoding="utf-8"))
            self.queue = [WQCandidate(**item).with_id() for item in data.get("queue", [])]
            self.seen_hashes = set(data.get("seen_hashes", []))
            self.expression_history = list(data.get("expression_history", []))
            self.counters.update(data.get("counters", {}))
            self.best = data.get("best")
            self.round_index = int(data.get("round", 0) or 0)
            self.consecutive_failures = int(data.get("consecutive_failures", 0) or 0)
            return

        self.queue = load_candidates(self.config.candidates_file)
        self._append_community_seeds()
        self.seen_hashes = set()
        self.expression_history = []
        self.best = None
        self.round_index = 0

    def _load_community_context(self) -> None:
        mode = (self.config.community_context_mode or "auto").lower()
        self.community_context_info["mode"] = mode
        if mode == "off":
            return
        self.community_context = CommunityContext.from_dir(self.config.community_context_dir)
        if not self.community_context:
            return
        self.community_context_info.update({
            "loaded": True,
            "context_dir": str(self.community_context.context_dir),
            "records": len(self.community_context.records),
            "candidates": len(self.community_context.candidates),
        })

    def _append_community_seeds(self) -> None:
        if not self.community_context or self.config.community_seed_limit <= 0:
            return
        existing = [candidate.expression for candidate in self.queue]
        seeds = self.community_context.seed_candidates(
            limit=self.config.community_seed_limit,
            existing_expressions=existing,
        )
        for offset, seed in enumerate(seeds, start=1):
            self.queue.append(
                WQCandidate(
                    expression=seed.expression,
                    tag=seed.tag or self.config.tag,
                    generation=0,
                    strategy=seed.strategy,
                    diagnosis=seed.diagnosis,
                    source_index=len(existing) + offset,
                ).with_id()
            )
        if seeds:
            self.counters["community_seeds_added"] = self.counters.get("community_seeds_added", 0) + len(seeds)
            self.community_context_info["seeds_added"] = int(self.community_context_info.get("seeds_added", 0) or 0) + len(seeds)

    def _community_context_for_entry(self, entry: dict, result: dict | None) -> str:
        if not self.community_context:
            return ""
        diagnosis = entry.get("diagnosis")
        if not isinstance(diagnosis, dict):
            diagnosis = diagnose_wq_result(entry.get("expression", ""), result or {})
        return self.community_context.retrieve(
            query=self.config.direction,
            expression=entry.get("expression"),
            diagnosis=diagnosis,
            fields_hint=self.fields_hint,
        )

    def _save_progress(self) -> None:
        checkpoint = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "round": self.round_index,
            "queue": [asdict(candidate) for candidate in self.queue],
            "seen_hashes": sorted(self.seen_hashes),
            "expression_history": self.expression_history[-1000:],
            "counters": self.counters,
            "best": self.best,
            "consecutive_failures": self.consecutive_failures,
            "config": config_to_dict(self.config),
            "community_context": self.community_context_info,
        }
        write_json(self.config.checkpoint_file, checkpoint)
        if self.best:
            write_json(self.config.output_dir / "best.json", self.best)
        self._write_summary("RUNNING", "")

    def _update_status(self, **updates) -> None:
        current: dict[str, Any] = {}
        if self.config.status_file.is_file():
            try:
                current = json.loads(self.config.status_file.read_text(encoding="utf-8"))
            except Exception:
                current = {}
        current.update(updates)
        current.update({
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "round": self.round_index,
            "queue_size": len(self.queue),
            "counters": self.counters,
            "best": self.best,
            "community_context": self.community_context_info,
        })
        write_json(self.config.status_file, current)

    def _finish(self, status: str, reason: str) -> None:
        self._save_progress()
        self._update_status(status=status, reason=reason, ended_at=datetime.now().isoformat(timespec="seconds"))
        self._write_summary(status, reason)

    def _write_summary(self, status: str, reason: str) -> None:
        lines = [
            "# WQ Auto Mining Summary",
            "",
            f"- Status: {status}",
            f"- Reason: {reason or '-'}",
            f"- Updated: {datetime.now().isoformat(timespec='seconds')}",
            f"- Region/Universe/Delay: {self.config.region}/{self.config.universe}/D{self.config.delay}",
            f"- Runs started: {self.counters['runs_started']}",
            f"- Completed: {self.counters['completed']}",
            f"- Failed: {self.counters['failed']}",
            f"- Submitted: {self.counters['submitted']}",
            f"- Active: {self.counters['active']}",
            f"- Self corr failed: {self.counters.get('self_corr_failed', self.counters.get('sc_failed', 0))}",
            f"- Prod corr failed: {self.counters.get('prod_corr_failed', 0)}",
            f"- Corr pending: {self.counters.get('corr_pending', 0)}",
            f"- Children generated: {self.counters['children_generated']}",
            f"- Community context: {'loaded' if self.community_context_info.get('loaded') else 'not loaded'}",
            f"- Community seeds added: {self.counters.get('community_seeds_added', 0)}",
            "",
            "## Best",
        ]
        if self.best:
            lines.extend([
                f"- Alpha ID: {self.best.get('alpha_id') or '-'}",
                f"- Status: {self.best.get('status')}",
                f"- Fitness: {self.best.get('fitness')}",
                f"- Sharpe: {self.best.get('sharpe')}",
                f"- Returns: {self.best.get('returns')}",
                f"- Turnover: {self.best.get('turnover')}",
                "",
                "```text",
                str(self.best.get("expression", "")),
                "```",
            ])
        else:
            lines.append("- None yet")
        self.config.summary_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.summary_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def best_entry(existing: dict | None, candidate: dict) -> dict:
    if not existing:
        return candidate
    return candidate if ranking_key(candidate) > ranking_key(existing) else existing


def config_to_dict(config: WQAutoMiningConfig) -> dict:
    data = asdict(config)
    return {key: str(value) if isinstance(value, Path) else value for key, value in data.items()}


def _ascii_progress_message(progress: int, message: str) -> str:
    if "并发限制" in message:
        return "Concurrent simulation limit; waiting before retry"
    if "速率限制" in message:
        return "Rate limited; waiting before retry"
    if "连接异常" in message:
        return "Connection error; waiting before retry"
    if "模拟完成" in message:
        return "Simulation completed"
    if "模拟进行中" in message:
        return f"Simulation running ({progress}%)"
    return message
