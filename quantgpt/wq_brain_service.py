"""Shared WQ BRAIN business logic — the single source of truth.

All WQ BRAIN operations (single simulate, batch sweep, submit-by-ids,
check-alphas, list-alphas) live here as **sync** functions.

MCP tools call them via `asyncio.to_thread(service_fn, ...)`.
HTTP routes call them directly from background `threading.Thread`.

wq_brain_client.py (low-level HTTP transport) is the only dependency.
"""

from __future__ import annotations

import itertools
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helpers (no I/O)
# ---------------------------------------------------------------------------

def safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def fitness_to_grade(fitness: float | None) -> str:
    if fitness is None:
        return "D"
    if fitness >= 1.0:
        return "A"
    if fitness >= 0.5:
        return "B"
    if fitness >= 0.25:
        return "C"
    return "D"


def parse_is_metrics(is_data: dict) -> dict:
    return {
        "sharpe": safe_float(is_data.get("sharpe")),
        "fitness": safe_float(is_data.get("fitness")),
        "returns": safe_float(is_data.get("returns")),
        "turnover": safe_float(is_data.get("turnover")),
    }


def _build_wq_result_block(sharpe, fitness, returns_val, turnover, grade):
    return {
        "backtest_summary": {
            "long_short_sharpe": sharpe,
            "wq_fitness": fitness,
            "rank_ic_mean": None,
            "turnover": turnover,
            "wq_rating": grade,
        },
        "wq_brain": {
            "wq_sharpe": sharpe,
            "wq_fitness": fitness,
            "wq_returns": returns_val,
            "wq_turnover": turnover,
            "wq_rating": grade,
        },
        "interpretation": {"rating": grade},
    }


# ---------------------------------------------------------------------------
# Service functions (sync, stateless — caller manages client lifecycle)
# ---------------------------------------------------------------------------

def run_single_simulation(
    client,
    expression: str,
    region: str = "USA",
    universe: str = "TOP3000",
    delay: int = 1,
    decay: int = 0,
    neutralization: str = "SUBINDUSTRY",
    truncation: float = 0.08,
    auto_submit: bool = False,
    user_id: str | None = None,
    tag: str | None = None,
    progress_callback: Callable[[int, str], None] | None = None,
) -> dict:
    """Simulate one expression and optionally auto-submit. Returns result dict."""
    result = client.simulate(
        expression, region=region, universe=universe,
        delay=delay, decay=decay, neutralization=neutralization,
        truncation=truncation, progress_callback=progress_callback,
    )

    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "Simulation failed")}

    alpha_id = result.get("alpha_id")
    is_data = result.get("is", {})
    m = parse_is_metrics(is_data)
    grade = fitness_to_grade(m["fitness"])

    submitted = False
    if auto_submit and alpha_id and grade == "A":
        submit_result = client.submit_alpha(alpha_id)
        submitted = submit_result.get("ok", False)

    if submitted and alpha_id and user_id:
        _track_alpha(
            user_id=user_id, alpha_id=alpha_id, expression=expression,
            region=region, universe=universe, delay=delay, decay=decay,
            neutralization=neutralization, truncation=truncation,
            tag=tag, metrics=m,
        )

    out = {
        "ok": True,
        "expression": expression,
        "alpha_id": alpha_id,
        "is_metrics": is_data,
        "oos_metrics": result.get("oos", {}),
        "settings": result.get("settings", {}),
        "submitted": submitted,
        "simulation_id": result.get("simulation_id"),
    }
    out.update(_build_wq_result_block(m["sharpe"], m["fitness"], m["returns"], m["turnover"], grade))
    return out


def run_batch_simulation(
    client,
    expression: str,
    regions: list[str],
    delays: list[int],
    universes: list[str],
    neutralizations: list[str],
    decay: int = 0,
    truncation: float = 0.08,
    auto_submit: bool = False,
    user_id: str | None = None,
    tag: str | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    check_cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Sweep expression over region×delay×universe×neutralization grid. Returns result dict."""
    combos = list(itertools.product(regions, delays, universes, neutralizations))
    sub_results: dict[str, dict] = {}

    for i, (region, delay_val, universe, neut) in enumerate(combos):
        if check_cancelled and check_cancelled():
            break

        key = f"{region}_D{delay_val}_{universe}_{neut}"
        if on_progress:
            on_progress(i + 1, len(combos), key)

        sim_result = client.simulate(
            expression, region=region, universe=universe,
            delay=delay_val, decay=decay, neutralization=neut,
            truncation=truncation,
        )

        if not sim_result.get("ok"):
            sub_results[key] = {
                "key": key, "region": region, "delay": delay_val,
                "universe": universe, "neutralization": neut,
                "status": "failed", "error": sim_result.get("error", "unknown"),
            }
            continue

        alpha_id = sim_result.get("alpha_id")
        is_data = sim_result.get("is", {})
        m = parse_is_metrics(is_data)
        grade = fitness_to_grade(m["fitness"])

        submitted = False
        if auto_submit and alpha_id and grade == "A":
            submit_result = client.submit_alpha(alpha_id)
            submitted = submit_result.get("ok", False)

        if submitted and alpha_id and user_id:
            _track_alpha(
                user_id=user_id, alpha_id=alpha_id, expression=expression,
                region=region, universe=universe, delay=delay_val, decay=decay,
                neutralization=neut, truncation=truncation,
                tag=tag, metrics=m,
            )

        sub_results[key] = {
            "key": key, "region": region, "delay": delay_val,
            "universe": universe, "neutralization": neut,
            "status": "completed", "alpha_id": alpha_id,
            "sharpe": m["sharpe"], "fitness": m["fitness"],
            "returns": m["returns"], "turnover": m["turnover"],
            "submitted": submitted, "rating": grade,
        }

    return _aggregate_batch_result(expression, len(combos), sub_results)


def run_submit_by_ids(
    client,
    alpha_ids: list[str],
    on_progress: Callable[[int, int, str], None] | None = None,
    check_cancelled: Callable[[], bool] | None = None,
    on_each_done: Callable[[str, dict], None] | None = None,
) -> dict:
    """Submit a list of already-simulated alphas. Returns summary dict."""
    results: dict[str, dict] = {}
    active = sc_fail = timeout = 0

    for i, alpha_id in enumerate(alpha_ids):
        if check_cancelled and check_cancelled():
            break

        if i > 0:
            time.sleep(5)

        if on_progress:
            on_progress(i + 1, len(alpha_ids), alpha_id)

        result = client.submit_alpha(alpha_id)
        entry: dict[str, Any] = {
            "ok": result.get("ok", False),
            "detail": result.get("detail", ""),
            "platform_status": result.get("platform_status", ""),
            "status_code": result.get("status_code"),
        }
        if result.get("sc_value") is not None:
            entry["sc_value"] = result["sc_value"]
            entry["sc_limit"] = result.get("sc_limit")

        if result.get("ok"):
            active += 1
            entry["final_status"] = "ACTIVE"
        elif "SC FAIL" in result.get("detail", ""):
            sc_fail += 1
            entry["final_status"] = "SC_FAIL"
        elif result.get("platform_status") == "TIMEOUT":
            timeout += 1
            entry["final_status"] = "SC_PENDING"
        else:
            entry["final_status"] = "OTHER_FAIL"

        results[alpha_id] = entry

        if on_each_done:
            on_each_done(alpha_id, entry)

    return {
        "total": len(alpha_ids),
        "active": active,
        "sc_fail": sc_fail,
        "timeout": timeout,
        "results": results,
    }


def run_check_alphas(client, alpha_ids: list[str]) -> dict:
    """Check platform status of multiple alphas. Returns summary + per-alpha dict."""
    results: dict[str, dict] = {}

    for alpha_id in alpha_ids:
        data = client.check_alpha_status(alpha_id)
        if not data.get("ok"):
            results[alpha_id] = {"ok": False, "error": data.get("error", "not found")}
            continue

        is_data = data.get("is", {})
        checks = is_data.get("checks", [])
        sc_check = next((c for c in checks if c.get("name") == "SELF_CORRELATION"), None)

        results[alpha_id] = {
            "ok": True,
            "status": data.get("status"),
            "grade": data.get("grade"),
            "dateCreated": data.get("dateCreated"),
            "sharpe": safe_float(is_data.get("sharpe")),
            "fitness": safe_float(is_data.get("fitness")),
            "returns": safe_float(is_data.get("returns")),
            "turnover": safe_float(is_data.get("turnover")),
            "sc_result": sc_check.get("result") if sc_check else None,
            "sc_value": sc_check.get("value") if sc_check else None,
        }

    summary = {
        "total": len(alpha_ids),
        "active": sum(1 for r in results.values() if r.get("status") == "ACTIVE"),
        "unsubmitted": sum(1 for r in results.values() if r.get("status") == "UNSUBMITTED"),
        "sc_fail": sum(1 for r in results.values() if r.get("sc_result") == "FAIL"),
        "sc_pending": sum(1 for r in results.values() if r.get("sc_result") == "PENDING"),
    }
    return {"summary": summary, "alphas": results}


def run_list_alphas(
    client,
    limit: int = 100,
    offset: int = 0,
    min_fitness: float | None = None,
    status_filter: str | None = None,
) -> dict:
    """List alphas from the platform with optional filtering."""
    s = client._get_session()
    r = s.get(
        "https://api.worldquantbrain.com/users/self/alphas",
        params={"limit": min(limit, 100), "offset": offset, "order": "-dateCreated"},
    )
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}

    data = r.json()
    raw_alphas = data if isinstance(data, list) else data.get("results", [])

    alphas = []
    for a in raw_alphas:
        code = a.get("regular", {})
        expr = code.get("code", "") if isinstance(code, dict) else str(code)
        settings = a.get("settings", {})
        is_data = a.get("is", {})
        fitness = safe_float(is_data.get("fitness"))
        alpha_status = a.get("status", "")

        if min_fitness is not None and (fitness is None or fitness < min_fitness):
            continue
        if status_filter and alpha_status.upper() != status_filter.upper():
            continue

        alphas.append({
            "alpha_id": a.get("id"),
            "expression": expr,
            "status": alpha_status,
            "dateCreated": a.get("dateCreated"),
            "neutralization": settings.get("neutralization"),
            "sharpe": safe_float(is_data.get("sharpe")),
            "fitness": fitness,
            "returns": safe_float(is_data.get("returns")),
            "turnover": safe_float(is_data.get("turnover")),
        })

    return {"ok": True, "total": len(alphas), "alphas": alphas}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _aggregate_batch_result(expression: str, total_combos: int, sub_results: dict) -> dict:
    all_failed = all(s.get("status") == "failed" for s in sub_results.values())
    if all_failed:
        first_error = next(
            (s.get("error", "unknown") for s in sub_results.values()),
            "all simulations failed",
        )
        return {
            "ok": False,
            "expression": expression,
            "total_combinations": total_combos,
            "best_fitness": None, "best_key": None,
            "submittable_count": 0,
            "sub_results": sub_results,
            "error": first_error,
        }

    best_fitness = -999.0
    best_key = None
    submittable_count = 0

    for key, sub in sub_results.items():
        if sub.get("status") != "completed":
            continue
        fitness = sub.get("fitness")
        if fitness is not None and fitness >= 1.0:
            submittable_count += 1
        if fitness is not None and fitness > best_fitness:
            best_fitness = fitness
            best_key = key

    best_sub = sub_results.get(best_key, {}) if best_key else {}
    best_fit = round(best_fitness, 4) if best_fitness > -999 else None
    best_grade = fitness_to_grade(best_fit)

    out: dict[str, Any] = {
        "ok": True,
        "expression": expression,
        "total_combinations": total_combos,
        "best_fitness": best_fit,
        "best_key": best_key,
        "submittable_count": submittable_count,
        "sub_results": sub_results,
    }
    out.update(_build_wq_result_block(
        best_sub.get("sharpe"), best_fit,
        best_sub.get("returns"), best_sub.get("turnover"), best_grade,
    ))
    return out


def _track_alpha(user_id, alpha_id, expression, region, universe, delay,
                 decay, neutralization, truncation, tag, metrics):
    try:
        from .alpha_tracker import record_submitted_alpha_sync
        record_submitted_alpha_sync(
            user_id=user_id, alpha_id=alpha_id, expression=expression,
            region=region, universe=universe, delay=delay, decay=decay,
            neutralization=neutralization, truncation=truncation,
            sharpe=metrics["sharpe"], fitness=metrics["fitness"],
            returns=metrics["returns"], turnover=metrics["turnover"],
            tag=tag,
        )
    except Exception as e:
        logger.warning(f"Alpha tracking failed for {alpha_id}: {e}")
