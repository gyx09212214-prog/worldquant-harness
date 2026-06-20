"""WorldQuant BRAIN API client.

Wraps the WQ BRAIN REST API for alpha simulation, quality checks, and
formal submission. Credentials are read from environment variables
WQ_BRAIN_EMAIL and WQ_BRAIN_PASSWORD.
"""

import logging
import os
import time
from typing import Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .wq_review import (
    correlation_failure_detail,
    correlation_result_label,
    parse_review_checks,
    primary_failure_kind,
    review_checks_passed,
)

logger = logging.getLogger(__name__)

API_BASE = "https://api.worldquantbrain.com"

READ_ONLY_ALPHA_DETAIL_PATHS = (
    "/alphas/{alpha_id}",
    "/alphas/{alpha_id}/pnl",
    "/alphas/{alpha_id}/pnl-chart",
    "/alphas/{alpha_id}/performance",
    "/alphas/{alpha_id}/performance-chart",
    "/alphas/{alpha_id}/stats",
    "/alphas/{alpha_id}/simulations",
    "/alphas/{alpha_id}/recordsets",
    "/alphas/{alpha_id}/recordsets/pnl",
    "/alphas/{alpha_id}/recordsets/daily-pnl",
    "/alphas/{alpha_id}/history",
)

SUBMIT_THRESHOLDS = {
    "sharpe": 1.25,
    "fitness": 1.0,
    "turnover_max": 0.7,
    "turnover_min": 0.01,
}

_ACCOUNT_ENV = {
    "primary": ("WQ_BRAIN_EMAIL", "WQ_BRAIN_PASSWORD"),
    "alt": ("WQ_BRAIN_ALT_EMAIL", "WQ_BRAIN_ALT_PASSWORD"),
}


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _validate_alpha_id(alpha_id: str) -> str:
    text = str(alpha_id or "").strip()
    if not text:
        raise ValueError("alpha_id is required")
    if any(token in text for token in ("/", "\\", "?", "#", "..")):
        raise ValueError(f"invalid alpha_id: {alpha_id}")
    return text


_POLL_INTERVAL = _env_int("WQ_POLL_INTERVAL", 10)
_POLL_MAX_ATTEMPTS = _env_int("WQ_POLL_MAX_ATTEMPTS", 90)
_CONCURRENT_BACKOFF = _env_int("WQ_CONCURRENT_BACKOFF", 30)
_MAX_RETRIES = _env_int("WQ_MAX_RETRIES", 5)
_REQUEST_TIMEOUT = (_env_int("WQ_CONNECT_TIMEOUT", 10), _env_int("WQ_READ_TIMEOUT", 60))


def _has_known_review_value(review_checks: dict) -> bool:
    return any(
        (review_checks.get(kind) or {}).get("result") in {"PASS", "FAIL", "WARNING", "MISSING"}
        and (review_checks.get(kind) or {}).get("value") is not None
        for kind in ("self_correlation", "prod_correlation")
    )


def _has_known_review_result(review_checks: dict) -> bool:
    return any(
        (review_checks.get(kind) or {}).get("result") in {"PASS", "FAIL", "WARNING"}
        for kind in ("self_correlation", "prod_correlation")
    )


def is_configured(account: str | None = None) -> bool:
    if account:
        env_email, env_pwd = _ACCOUNT_ENV.get(account, _ACCOUNT_ENV["primary"])
        return bool(os.environ.get(env_email) and os.environ.get(env_pwd))
    return any(
        bool(os.environ.get(e) and os.environ.get(p))
        for e, p in _ACCOUNT_ENV.values()
    )


def configured_accounts() -> list[str]:
    return [
        name for name, (e, p) in _ACCOUNT_ENV.items()
        if os.environ.get(e) and os.environ.get(p)
    ]


def get_client(account: str = "primary") -> "WQBrainClient":
    env_email, env_pwd = _ACCOUNT_ENV.get(account, _ACCOUNT_ENV["primary"])
    return WQBrainClient(
        email=os.environ.get(env_email, ""),
        password=os.environ.get(env_pwd, ""),
    )


class WQBrainClient:
    def __init__(self, email: str | None = None, password: str | None = None):
        self.email = email or os.environ.get("WQ_BRAIN_EMAIL", "")
        self.password = password or os.environ.get("WQ_BRAIN_PASSWORD", "")
        self._session: requests.Session | None = None

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.trust_env = False
            retry = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
            adapter = HTTPAdapter(max_retries=retry)
            self._session.mount("https://", adapter)
            self._session.mount("http://", adapter)
        return self._session

    def close(self):
        if self._session:
            self._session.close()
            self._session = None

    def authenticate(self, _max_retries: int = 5) -> bool:
        s = self._get_session()
        for attempt in range(_max_retries):
            r = s.post(
                f"{API_BASE}/authentication",
                auth=(self.email, self.password),
                timeout=_REQUEST_TIMEOUT,
            )
            if r.status_code == 429:
                retry = int(r.headers.get("Retry-After", "60"))
                logger.info(f"WQ auth rate-limited, waiting {retry}s (attempt {attempt + 1}/{_max_retries})")
                time.sleep(retry + 1)
                continue

            if r.status_code not in (200, 201):
                logger.error(f"WQ auth failed: HTTP {r.status_code}")
                return False

            data = r.json()
            if "inquiry" in data:
                logger.error("WQ auth requires biometric verification — log in via browser first")
                return False

            logger.info("WQ BRAIN authenticated")
            return True

        logger.error(f"WQ auth failed: rate-limited {_max_retries} times")
        return False

    def get_user_info(self) -> dict:
        r = self._get_session().get(f"{API_BASE}/users/self", timeout=_REQUEST_TIMEOUT)
        return r.json() if r.status_code == 200 else {}

    def get_json(self, path: str, params: dict | None = None) -> dict:
        """GET a WQ API path and return a JSON object with HTTP metadata on failure."""
        url = path if path.startswith("http") else f"{API_BASE}/{path.lstrip('/')}"
        r = self._get_session().get(url, params=params, timeout=_REQUEST_TIMEOUT)
        try:
            data = r.json()
        except Exception:
            data = {"text": r.text[:500]}
        if r.status_code != 200:
            return {"ok": False, "status_code": r.status_code, "error": data}
        if isinstance(data, dict):
            data.setdefault("ok", True)
            return data
        return {"ok": True, "data": data}

    def get_alpha_raw(self, alpha_id: str) -> dict:
        """Read raw platform alpha detail via GET /alphas/{id}; never submits."""
        alpha_id = _validate_alpha_id(alpha_id)
        return self._get_json_with_status(f"/alphas/{alpha_id}", alpha_id=alpha_id)

    def probe_alpha_detail(self, alpha_id: str, paths: list[str] | tuple[str, ...] | None = None) -> dict:
        """Probe allowlisted read-only alpha detail endpoints with GET requests only."""
        alpha_id = _validate_alpha_id(alpha_id)
        requested = list(paths or READ_ONLY_ALPHA_DETAIL_PATHS)
        endpoints: list[dict] = []
        seen: set[str] = set()
        for template in requested:
            if template not in READ_ONLY_ALPHA_DETAIL_PATHS:
                endpoints.append({
                    "ok": False,
                    "alpha_id": alpha_id,
                    "path": template,
                    "status_code": 0,
                    "error": "path is not in READ_ONLY_ALPHA_DETAIL_PATHS",
                })
                continue
            path = template.format(alpha_id=alpha_id)
            if path in seen:
                continue
            seen.add(path)
            endpoints.append(self._get_json_with_status(path, alpha_id=alpha_id))
        return {
            "ok": any(item.get("ok") for item in endpoints),
            "alpha_id": alpha_id,
            "read_only": True,
            "endpoints": endpoints,
        }

    def _get_json_with_status(self, path: str, *, alpha_id: str | None = None) -> dict:
        url = path if path.startswith("http") else f"{API_BASE}/{path.lstrip('/')}"
        try:
            r = self._get_session().get(url, timeout=_REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            return {
                "ok": False,
                "alpha_id": alpha_id,
                "path": path,
                "status_code": 0,
                "error": str(exc),
            }
        try:
            data = r.json()
        except Exception:
            data = {"text": r.text[:1000]}
        return {
            "ok": r.status_code == 200,
            "alpha_id": alpha_id,
            "path": path,
            "status_code": r.status_code,
            "data": data,
        }

    def list_data_sets(
        self,
        region: str = "USA",
        universe: str = "TOP3000",
        delay: int = 1,
        instrument_type: str = "EQUITY",
    ) -> dict:
        return self.get_json(
            "/data-sets",
            params={
                "instrumentType": instrument_type,
                "region": region,
                "universe": universe,
                "delay": delay,
            },
        )

    def list_data_fields(
        self,
        region: str = "USA",
        universe: str = "TOP3000",
        delay: int = 1,
        dataset_id: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
        instrument_type: str = "EQUITY",
    ) -> dict:
        params = {
            "instrumentType": instrument_type,
            "region": region,
            "universe": universe,
            "delay": delay,
            "limit": limit,
            "offset": offset,
        }
        if dataset_id:
            params["dataset.id"] = dataset_id
        if search:
            params["search"] = search
        return self.get_json("/data-fields", params=params)

    def simulate(
        self,
        expression: str,
        region: str = "USA",
        universe: str = "TOP3000",
        delay: int = 1,
        decay: int = 0,
        neutralization: str = "SUBINDUSTRY",
        truncation: float = 0.08,
        max_trade: str = "OFF",
        max_position: str = "OFF",
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> dict:
        s = self._get_session()
        payload = {
            "type": "REGULAR",
            "settings": {
                "instrumentType": "EQUITY",
                "region": region,
                "universe": universe,
                "delay": delay,
                "decay": decay,
                "neutralization": neutralization,
                "truncation": truncation,
                "pasteurization": "ON",
                "unitHandling": "VERIFY",
                "nanHandling": "OFF",
                "maxTrade": max_trade,
                "maxPosition": max_position,
                "language": "FASTEXPR",
                "visualization": False,
            },
            "regular": expression,
        }

        for attempt in range(_MAX_RETRIES):
            try:
                r = s.post(f"{API_BASE}/simulations", json=payload, timeout=_REQUEST_TIMEOUT)
            except requests.RequestException as e:
                wait = _CONCURRENT_BACKOFF * (attempt + 1)
                logger.warning(f"WQ connection error (attempt {attempt+1}/{_MAX_RETRIES}): {e}, retrying in {wait}s")
                if progress_callback:
                    progress_callback(0, f"连接异常，等待 {wait}s（第 {attempt+1} 次重试）")
                time.sleep(wait)
                continue

            if r.status_code in (200, 201, 202):
                break

            if r.status_code == 429:
                detail = ""
                try:
                    detail = r.json().get("detail", "")
                except Exception:
                    pass

                if "CONCURRENT_SIMULATION_LIMIT" in detail:
                    wait = _CONCURRENT_BACKOFF * (attempt + 1)
                    logger.info(f"WQ concurrent limit, waiting {wait}s (attempt {attempt+1}/{_MAX_RETRIES})")
                    if progress_callback:
                        progress_callback(0, f"并发限制，等待 {wait}s（第 {attempt+1} 次重试）")
                    time.sleep(wait)
                    continue

                retry = int(r.headers.get("Retry-After", "60"))
                logger.info(f"WQ rate-limited, waiting {retry}s")
                if progress_callback:
                    progress_callback(0, f"速率限制，等待 {retry}s")
                time.sleep(retry + 1)
                continue

            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}
        else:
            return {"ok": False, "error": "WQ concurrent retry limit exceeded"}

        location = r.headers.get("Location", "")
        if not location:
            return {"ok": False, "error": "No Location header in response"}

        url = location if location.startswith("http") else f"{API_BASE}{location}"

        for i in range(_POLL_MAX_ATTEMPTS):
            try:
                r = s.get(url, timeout=_REQUEST_TIMEOUT)
            except requests.RequestException as e:
                logger.warning(f"WQ poll request error (attempt {i+1}): {e}, retrying...")
                time.sleep(_POLL_INTERVAL)
                continue
            if r.status_code != 200:
                time.sleep(_POLL_INTERVAL)
                continue

            try:
                data = r.json()
            except Exception:
                time.sleep(_POLL_INTERVAL)
                continue
            status = data.get("status", "").upper()
            progress = data.get("progress", 0)

            if progress_callback:
                pct = int(progress * 100) if isinstance(progress, float) and progress <= 1 else int(progress)
                progress_callback(min(pct, 99), f"模拟进行中 ({pct}%)")

            if status in ("DONE", "COMPLETE"):
                alpha_raw = data.get("alpha", "")
                alpha_id = alpha_raw.split("/")[-1] if alpha_raw else None

                is_data = data.get("is", {})
                oos_data = data.get("oos", {})

                if alpha_id and not is_data:
                    alpha_detail = self._fetch_alpha(alpha_id)
                    is_data = alpha_detail.get("is", {})
                    oos_data = alpha_detail.get("oos", {})

                if progress_callback:
                    progress_callback(100, "模拟完成")

                return {
                    "ok": True,
                    "expression": expression,
                    "is": is_data,
                    "oos": oos_data,
                    "settings": data.get("settings", {}),
                    "alpha_id": alpha_id,
                    "simulation_id": data.get("id", ""),
                }
            elif status in ("ERROR", "FAILED"):
                return {"ok": False, "error": f"WQ simulation failed: {data.get('message', status)}"}

            time.sleep(_POLL_INTERVAL)

        timeout_minutes = (_POLL_MAX_ATTEMPTS * _POLL_INTERVAL) // 60
        return {"ok": False, "error": f"WQ simulation polling timeout ({timeout_minutes}min)"}

    def _fetch_alpha(self, alpha_id: str) -> dict:
        try:
            r = self._get_session().get(f"{API_BASE}/alphas/{alpha_id}", timeout=_REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning(f"Fetch alpha {alpha_id}: request error: {exc}")
            return {}
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                logger.warning(f"Empty/invalid JSON from /alphas/{alpha_id}")
                return {}
        return {}

    def check_alpha_status(self, alpha_id: str) -> dict:
        """Fetch actual platform-side alpha status including submission state."""
        data = self._fetch_alpha(alpha_id)
        if not data:
            return {"ok": False, "error": f"Alpha {alpha_id} not found"}
        return {
            "ok": True,
            "alpha_id": alpha_id,
            "status": data.get("status"),
            "dateSubmitted": data.get("dateSubmitted"),
            "dateCreated": data.get("dateCreated"),
            "grade": data.get("grade"),
            "color": data.get("color"),
            "hidden": data.get("hidden"),
            "is": data.get("is", {}),
            "checks": data.get("checks", {}),
            "review_checks": parse_review_checks(data),
        }

    def check_alpha_submission(self, alpha_id: str, max_polls: int = 6, interval: int = 10) -> dict:
        """Run the platform's check-only submission review for an alpha.

        This mirrors the web UI's "Check Submission" action. It deliberately
        never calls /submit; callers must use submit_alpha() for real submission.
        """
        s = self._get_session()
        last_payload: dict = {}
        last_status_code = None

        for attempt in range(max(1, max_polls)):
            try:
                r = s.get(f"{API_BASE}/alphas/{alpha_id}/check", timeout=_REQUEST_TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as e:
                logger.warning(f"Check submission {alpha_id}: connection error at poll #{attempt}: {e}")
                if attempt + 1 < max_polls:
                    time.sleep(interval)
                    continue
                return {"ok": False, "alpha_id": alpha_id, "error": f"connection error: {e}"}

            last_status_code = r.status_code
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", str(interval)))
                logger.warning(f"Check submission {alpha_id}: rate limited, waiting {wait}s")
                time.sleep(wait)
                continue
            if r.status_code == 404:
                return {"ok": False, "alpha_id": alpha_id, "status_code": 404, "error": "alpha not found"}
            if r.status_code not in (200, 201, 202):
                return {
                    "ok": False,
                    "alpha_id": alpha_id,
                    "status_code": r.status_code,
                    "error": f"check submission failed: {r.text[:300]}",
                }

            try:
                payload = r.json()
            except Exception:
                payload = {}
            if isinstance(payload, list):
                payload = {"checks": payload}
            if not isinstance(payload, dict):
                payload = {"raw": payload}
            last_payload = payload

            normalized = self._normalize_check_submission_payload(alpha_id, payload, r.status_code)
            review_checks = normalized.get("review_checks", {})
            if review_checks.get("failed") or _has_known_review_value(review_checks) or (
                _has_known_review_result(review_checks) and not review_checks.get("pending")
            ):
                return normalized

            if attempt + 1 < max_polls:
                time.sleep(interval)

        normalized = self._normalize_check_submission_payload(alpha_id, last_payload, last_status_code or 0)
        normalized["ok"] = False
        normalized["failure_kind"] = normalized.get("failure_kind") or "correlation_pending"
        normalized["detail"] = normalized.get("detail") or f"check submission timeout ({max_polls * interval}s)"
        return normalized

    def _normalize_check_submission_payload(self, alpha_id: str, payload: dict, status_code: int) -> dict:
        review_checks = parse_review_checks(payload)
        failure_kind = primary_failure_kind(review_checks)
        is_data = payload.get("is") if isinstance(payload.get("is"), dict) else {}
        out = {
            "ok": True,
            "alpha_id": alpha_id,
            "status_code": status_code,
            "status": payload.get("status"),
            "dateSubmitted": payload.get("dateSubmitted"),
            "dateCreated": payload.get("dateCreated"),
            "grade": payload.get("grade"),
            "color": payload.get("color"),
            "hidden": payload.get("hidden"),
            "is": is_data,
            "checks": payload.get("checks", {}),
            "review_checks": review_checks,
            "raw_check": payload,
        }
        if failure_kind:
            check = review_checks.get(failure_kind, {})
            out["ok"] = False
            out["failure_kind"] = failure_kind
            out["detail"] = correlation_failure_detail(review_checks, failure_kind)
            if failure_kind == "self_correlation":
                out["sc_value"] = check.get("value")
                out["sc_limit"] = check.get("limit")
            elif failure_kind == "prod_correlation":
                out["prod_value"] = check.get("value")
                out["prod_limit"] = check.get("limit")
        elif review_checks.get("pending"):
            out["failure_kind"] = "correlation_pending"
            out["detail"] = correlation_result_label(review_checks)
        else:
            out["detail"] = correlation_result_label(review_checks)
        return out

    def submit_alpha(self, alpha_id: str) -> dict:
        s = self._get_session()
        last_unexpected = None
        last_rate_limit = None

        for submit_try in range(3):
            r = None
            for attempt in range(3):
                try:
                    r = s.post(f"{API_BASE}/alphas/{alpha_id}/submit", timeout=_REQUEST_TIMEOUT)
                    body = r.text[:500]
                    logger.info(f"Submit {alpha_id}: HTTP {r.status_code}, body={body}")
                    break
                except (requests.ConnectionError, requests.Timeout) as e:
                    logger.warning(f"Submit {alpha_id}: connection error (attempt {attempt+1}): {e}")
                    time.sleep(5 * (attempt + 1))
            else:
                return {"status_code": 0, "ok": False, "detail": "connection failed after retries"}

            if r.status_code == 403:
                try:
                    resp = r.json()
                    review_checks = parse_review_checks(resp)
                    failure_kind = primary_failure_kind(review_checks)
                    if failure_kind:
                        check = review_checks.get(failure_kind, {})
                        detail = correlation_failure_detail(review_checks, failure_kind)
                        logger.warning(f"Submit {alpha_id}: {detail}")
                        out = {
                            "status_code": 403,
                            "ok": False,
                            "detail": detail,
                            "platform_status": "UNSUBMITTED",
                            "failure_kind": failure_kind,
                            "review_checks": review_checks,
                            "checks": resp.get("is", {}).get("checks", []),
                        }
                        if failure_kind == "self_correlation":
                            out["sc_value"] = check.get("value")
                            out["sc_limit"] = check.get("limit")
                        elif failure_kind == "prod_correlation":
                            out["prod_value"] = check.get("value")
                            out["prod_limit"] = check.get("limit")
                        return out
                except Exception:
                    pass
                return {"status_code": 403, "ok": False, "detail": body}

            if r.status_code == 429:
                wait = 30 * (submit_try + 1)
                last_rate_limit = {"status_code": 429, "body": body, "wait_seconds": wait}
                logger.warning(f"Submit {alpha_id}: rate limited (429), waiting {wait}s before retry")
                time.sleep(wait)
                continue

            if r.status_code not in (200, 201, 202):
                last_unexpected = {"status_code": r.status_code, "body": body}
                logger.warning(f"Submit {alpha_id}: unexpected HTTP {r.status_code}, body={body}, waiting 15s before retry")
                time.sleep(15)
                continue

            poll_result = self._poll_alpha_submission(alpha_id)

            if poll_result.get("ok"):
                return poll_result

            if poll_result.get("platform_status") == "TIMEOUT":
                alpha_data = self._fetch_alpha(alpha_id)
                actual_status = (alpha_data.get("status") or "").upper()
                if actual_status == "UNSUBMITTED":
                    logger.warning(f"Submit {alpha_id}: platform still UNSUBMITTED after poll, retrying submit (try {submit_try+1})")
                    time.sleep(10)
                    continue
                logger.info(f"Submit {alpha_id}: poll timeout but platform status={actual_status}, treating as submitted")
                poll_result["ok"] = True
                poll_result["detail"] = f"poll timeout but platform accepted (status={actual_status})"
                return poll_result

            return poll_result

        out = {"status_code": 200, "ok": False, "detail": "submit failed after 3 outer retries, alpha still UNSUBMITTED"}
        if last_rate_limit:
            out.update({
                "status_code": 429,
                "detail": "submit rate limited after retries",
                "failure_kind": "rate_limited",
                "last_rate_limit_response": last_rate_limit,
            })
        if last_unexpected:
            out["last_unexpected_response"] = last_unexpected
        return out

    def _poll_alpha_submission(self, alpha_id: str, max_polls: int = 12, interval: int = 10) -> dict:
        """Poll alpha status until platform confirms submission or correlation checks settle."""
        s = self._get_session()
        status = "UNKNOWN"
        review_checks = parse_review_checks({})
        for i in range(max_polls):
            time.sleep(interval)
            try:
                r = s.get(f"{API_BASE}/alphas/{alpha_id}", timeout=_REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                logger.warning(f"Submit poll {alpha_id}: request error at poll #{i}: {exc}")
                continue
            if r.status_code != 200:
                continue
            try:
                data = r.json()
            except Exception:
                continue

            status = data.get("status", "").upper()
            review_checks = parse_review_checks(data)
            failure_kind = primary_failure_kind(review_checks)
            review_label = correlation_result_label(review_checks)

            logger.info(f"Submit poll {alpha_id} #{i}: status={status}, {review_label}")

            if status == "ACTIVE":
                logger.info(f"Submit {alpha_id}: confirmed ACTIVE on platform")
                return {
                    "status_code": 200,
                    "ok": True,
                    "detail": f"submitted and ACTIVE, {review_label}",
                    "platform_status": status,
                    "review_checks": review_checks,
                }
            elif failure_kind:
                check = review_checks.get(failure_kind, {})
                detail = correlation_failure_detail(review_checks, failure_kind)
                logger.warning(f"Submit {alpha_id}: {detail}")
                out = {
                    "status_code": 200,
                    "ok": False,
                    "detail": detail,
                    "platform_status": status,
                    "failure_kind": failure_kind,
                    "review_checks": review_checks,
                }
                if failure_kind == "self_correlation":
                    out["sc_value"] = check.get("value")
                    out["sc_limit"] = check.get("limit")
                elif failure_kind == "prod_correlation":
                    out["prod_value"] = check.get("value")
                    out["prod_limit"] = check.get("limit")
                return out
            elif review_checks_passed(review_checks) and status == "UNSUBMITTED":
                logger.info(f"Submit {alpha_id}: correlation checks passed but still UNSUBMITTED, retrying submit...")
                try:
                    s.post(f"{API_BASE}/alphas/{alpha_id}/submit", timeout=_REQUEST_TIMEOUT)
                except Exception:
                    pass

        return {
            "status_code": 200,
            "ok": False,
            "detail": f"submission polling timeout ({max_polls * interval}s), last status={status}, {correlation_result_label(review_checks)}",
            "platform_status": "TIMEOUT",
            "failure_kind": "correlation_pending",
            "review_checks": review_checks,
        }

    def delete_alpha(self, alpha_id: str) -> dict:
        """Delete/retire an alpha from the platform."""
        s = self._get_session()
        r = s.delete(f"{API_BASE}/alphas/{alpha_id}", timeout=_REQUEST_TIMEOUT)
        if r.status_code in (200, 204):
            return {"ok": True, "detail": f"Alpha {alpha_id} deleted"}
        if r.status_code == 405:
            r2 = s.patch(f"{API_BASE}/alphas/{alpha_id}", json={"hidden": True}, timeout=_REQUEST_TIMEOUT)
            if r2.status_code in (200, 204):
                return {"ok": True, "detail": f"Alpha {alpha_id} hidden via PATCH"}
            return {"ok": False, "detail": f"DELETE 405, PATCH also failed: {r2.status_code} {r2.text[:200]}"}
        return {"ok": False, "detail": f"DELETE failed: {r.status_code} {r.text[:200]}"}

    def unhide_alpha(self, alpha_id: str) -> dict:
        """Restore a hidden alpha."""
        s = self._get_session()
        r = s.patch(f"{API_BASE}/alphas/{alpha_id}", json={"hidden": False}, timeout=_REQUEST_TIMEOUT)
        if r.status_code in (200, 204):
            return {"ok": True, "detail": f"Alpha {alpha_id} restored"}
        return {"ok": False, "detail": f"Unhide failed: {r.status_code} {r.text[:200]}"}

