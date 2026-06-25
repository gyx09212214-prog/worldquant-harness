"""Export WorldQuant BRAIN Community posts and comments to JSONL.

The Community surface is less stable than the documented BRAIN alpha API, so
this module keeps endpoint paths configurable and normalizes several common
response shapes into the JSONL schema consumed by ``community_triage``.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import requests

from .wq_brain_client import API_BASE

DEFAULT_POST_PATHS = [
    "https://support.worldquantbrain.com/api/v2/community/posts.json",
    "/community/posts",
    "/posts",
    "/forum/posts",
    "/threads",
]

DEFAULT_COMMENT_PATH_TEMPLATES = [
    "https://support.worldquantbrain.com/api/v2/community/posts/{post_id}/comments.json",
    "/community/posts/{post_id}/comments",
    "/posts/{post_id}/comments",
    "/forum/posts/{post_id}/comments",
    "/threads/{post_id}/comments",
]

COLLECTION_KEYS = (
    "results",
    "data",
    "items",
    "records",
    "content",
    "posts",
    "threads",
    "comments",
    "replies",
)

POST_ID_KEYS = ("post_id", "postId", "thread_id", "threadId", "id", "_id", "uuid", "slug")
COMMENT_ID_KEYS = ("comment_id", "commentId", "reply_id", "replyId", "id", "_id", "uuid")
TITLE_KEYS = ("title", "subject", "name", "headline")
TEXT_KEYS = (
    "body_text",
    "bodyText",
    "body",
    "body.text",
    "body.html",
    "text",
    "content",
    "content.text",
    "content.html",
    "details",
    "message",
    "description",
    "snippet",
    "summary",
)
TIME_KEYS = ("created_at", "createdAt", "dateCreated", "timestamp", "time", "date", "updated_at", "updatedAt")
URL_KEYS = ("html_url", "url", "link", "permalink", "href")


class WQCommunityFetchError(RuntimeError):
    """Raised when a Community endpoint responds but cannot be exported."""


class WQCommunityEndpointNotFound(WQCommunityFetchError):
    """Raised when all candidate endpoint paths return 404 or 405."""


@dataclass
class CollectionResult:
    rows: list[dict[str, Any]]
    path: str
    pages: int


@dataclass
class WQCommunityExportConfig:
    output_dir: Path
    post_paths: list[str] = field(default_factory=lambda: list(DEFAULT_POST_PATHS))
    comment_path_templates: list[str] = field(default_factory=lambda: list(DEFAULT_COMMENT_PATH_TEMPLATES))
    include_comments: bool = True
    limit: int = 100
    max_pages: int = 20
    max_posts: int = 500
    comments_max_pages: int = 5
    max_comments_per_post: int = 500
    sleep_seconds: float = 0.5
    posts_params: dict[str, str] = field(default_factory=dict)
    comments_params: dict[str, str] = field(default_factory=dict)
    include_raw: bool = False
    api_base: str = API_BASE
    request_timeout: tuple[int, int] = (10, 60)


def export_community(session: requests.Session, config: WQCommunityExportConfig) -> dict[str, Any]:
    """Fetch posts/comments and write posts.jsonl, comments.jsonl, and manifest.json."""

    posts_result = fetch_collection(
        session,
        config.post_paths,
        api_base=config.api_base,
        limit=config.limit,
        max_pages=config.max_pages,
        max_items=config.max_posts,
        params=config.posts_params,
        sleep_seconds=config.sleep_seconds,
        request_timeout=config.request_timeout,
    )

    normalized_posts = [normalize_post(row, include_raw=config.include_raw) for row in posts_result.rows]
    normalized_posts = _dedupe_rows(normalized_posts, ("post_id",))

    comments: list[dict[str, Any]] = []
    if config.include_comments:
        comments.extend(_embedded_comments(posts_result.rows, normalized_posts, include_raw=config.include_raw))
        comments.extend(
            fetch_comments_for_posts(
                session,
                normalized_posts,
                config.comment_path_templates,
                api_base=config.api_base,
                limit=config.limit,
                max_pages=config.comments_max_pages,
                max_items_per_post=config.max_comments_per_post,
                params=config.comments_params,
                sleep_seconds=config.sleep_seconds,
                request_timeout=config.request_timeout,
                include_raw=config.include_raw,
            )
        )
        comments = _dedupe_rows(comments, ("post_id", "comment_id", "body_text"))

    config.output_dir.mkdir(parents=True, exist_ok=True)
    posts_file = config.output_dir / "posts.jsonl"
    comments_file = config.output_dir / "comments.jsonl"
    manifest_file = config.output_dir / "manifest.json"
    write_jsonl(posts_file, normalized_posts)
    write_jsonl(comments_file, comments)

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "posts_file": str(posts_file),
        "comments_file": str(comments_file),
        "input_post_endpoint": posts_result.path,
        "post_pages": posts_result.pages,
        "posts": len(normalized_posts),
        "comments": len(comments),
        "include_raw": config.include_raw,
        "privacy_note": "Credentials, cookies, and authorization headers are not written to disk.",
    }
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return manifest


def fetch_collection(
    session: requests.Session,
    paths: list[str],
    *,
    api_base: str = API_BASE,
    limit: int = 100,
    max_pages: int = 20,
    max_items: int | None = None,
    params: dict[str, str] | None = None,
    sleep_seconds: float = 0.5,
    request_timeout: tuple[int, int] = (10, 60),
) -> CollectionResult:
    """Fetch a paginated list from the first working endpoint path."""

    failures: list[str] = []
    for path in paths:
        try:
            return _fetch_collection_from_path(
                session,
                path,
                api_base=api_base,
                limit=limit,
                max_pages=max_pages,
                max_items=max_items,
                params=params or {},
                sleep_seconds=sleep_seconds,
                request_timeout=request_timeout,
            )
        except WQCommunityEndpointNotFound as exc:
            failures.append(str(exc))
            continue

    joined = "; ".join(failures) if failures else "no endpoint paths were provided"
    raise WQCommunityEndpointNotFound(joined)


def fetch_comments_for_posts(
    session: requests.Session,
    posts: list[dict[str, Any]],
    templates: list[str],
    *,
    api_base: str = API_BASE,
    limit: int = 100,
    max_pages: int = 5,
    max_items_per_post: int = 500,
    params: dict[str, str] | None = None,
    sleep_seconds: float = 0.5,
    request_timeout: tuple[int, int] = (10, 60),
    include_raw: bool = False,
) -> list[dict[str, Any]]:
    """Fetch comments for each post using configurable path templates."""

    out: list[dict[str, Any]] = []
    for post in posts:
        post_id = str(post.get("post_id") or "").strip()
        if not post_id:
            continue
        post_templates = [_format_comment_template(template, post_id) for template in templates]
        try:
            result = fetch_collection(
                session,
                post_templates,
                api_base=api_base,
                limit=limit,
                max_pages=max_pages,
                max_items=max_items_per_post,
                params=params or {},
                sleep_seconds=sleep_seconds,
                request_timeout=request_timeout,
            )
        except WQCommunityEndpointNotFound:
            continue
        for row in result.rows:
            out.append(normalize_comment(row, post, include_raw=include_raw))
    return out


def normalize_post(row: dict[str, Any], *, include_raw: bool = False) -> dict[str, Any]:
    post_id = _first_text(row, *POST_ID_KEYS) or _stable_id("post", row)
    normalized = {
        "post_id": post_id,
        "title": _first_text(row, *TITLE_KEYS),
        "url": _first_text(row, *URL_KEYS),
        "body_text": _first_text(row, *TEXT_KEYS),
        "created_at": _first_text(row, *TIME_KEYS),
    }
    if include_raw:
        normalized["raw"] = row
    return normalized


def normalize_comment(row: dict[str, Any], post: dict[str, Any], *, include_raw: bool = False) -> dict[str, Any]:
    post_id = _first_text(row, "post_id", "postId", "thread_id", "threadId", "parent_post_id", "parentPostId")
    post_id = post_id or str(post.get("post_id") or "")
    normalized = {
        "comment_id": _first_text(row, *COMMENT_ID_KEYS) or _stable_id("comment", row),
        "post_id": post_id,
        "title": _first_text(row, *TITLE_KEYS) or str(post.get("title") or ""),
        "url": _first_text(row, *URL_KEYS) or str(post.get("url") or ""),
        "body_text": _first_text(row, *TEXT_KEYS),
        "created_at": _first_text(row, *TIME_KEYS),
    }
    if include_raw:
        normalized["raw"] = row
    return normalized


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _fetch_collection_from_path(
    session: requests.Session,
    path: str,
    *,
    api_base: str,
    limit: int,
    max_pages: int,
    max_items: int | None,
    params: dict[str, str],
    sleep_seconds: float,
    request_timeout: tuple[int, int],
) -> CollectionResult:
    url = _resolve_url(path, api_base)
    request_params: dict[str, Any] = {"limit": limit, "offset": 0}
    request_params.update(params)
    rows: list[dict[str, Any]] = []
    pages = 0

    while pages < max_pages:
        response = _get_with_retry(session, url, request_params, request_timeout=request_timeout)
        if response.status_code in (404, 405):
            raise WQCommunityEndpointNotFound(f"{path}: HTTP {response.status_code}")
        if response.status_code in (401, 403):
            raise WQCommunityFetchError(f"{path}: HTTP {response.status_code}; check WQ credentials or cookies")
        if response.status_code < 200 or response.status_code >= 300:
            raise WQCommunityFetchError(f"{path}: HTTP {response.status_code}; {response.text[:300]}")

        payload = _json_payload(response, path)
        page_rows = _collection_rows(payload)
        rows.extend(page_rows)
        pages += 1

        if max_items is not None and len(rows) >= max_items:
            rows = rows[:max_items]
            break

        next_url = _next_url(payload, api_base)
        if next_url:
            url = next_url
            request_params = {}
        elif _has_more_offset_pages(payload, len(rows)) and page_rows:
            request_params["offset"] = int(request_params.get("offset") or 0) + len(page_rows)
        else:
            break

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return CollectionResult(rows=rows, path=path, pages=pages)


def _get_with_retry(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    *,
    request_timeout: tuple[int, int],
) -> requests.Response:
    response = session.get(url, params=params or None, timeout=request_timeout)
    if response.status_code != 429:
        return response
    retry_after = response.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        time.sleep(int(retry_after) + 1)
        return session.get(url, params=params or None, timeout=request_timeout)
    return response


def _json_payload(response: requests.Response, path: str) -> Any:
    try:
        return response.json()
    except ValueError:
        raise WQCommunityFetchError(f"{path}: response is not JSON") from None


def _collection_rows(payload: Any) -> list[dict[str, Any]]:
    value = _collection_value(payload)
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _collection_value(payload: Any) -> Any:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in COLLECTION_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _collection_value(value)
            if isinstance(nested, list):
                return nested
    return []


def _next_url(payload: Any, api_base: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("next"),
        payload.get("next_url"),
        payload.get("nextUrl"),
        payload.get("next_page"),
        _nested_value(payload, "links.next"),
        _nested_value(payload, "pagination.next"),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return _resolve_url(value.strip(), api_base)
    return None


def _has_more_offset_pages(payload: Any, current_count: int) -> bool:
    if not isinstance(payload, dict):
        return False
    for key in ("count", "total", "totalCount", "total_count"):
        value = payload.get(key)
        if isinstance(value, int):
            return current_count < value
        if isinstance(value, str) and value.isdigit():
            return current_count < int(value)
    return False


def _embedded_comments(
    raw_posts: list[dict[str, Any]],
    normalized_posts: list[dict[str, Any]],
    *,
    include_raw: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw_post, post in zip(raw_posts, normalized_posts):
        for key in ("comments", "replies", "responses", "children"):
            value = raw_post.get(key)
            if isinstance(value, dict):
                value = _collection_value(value)
            if not isinstance(value, list):
                continue
            for row in value:
                if isinstance(row, dict):
                    out.append(normalize_comment(row, post, include_raw=include_raw))
    return out


def _dedupe_rows(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key_values = [str(row.get(key) or "") for key in keys]
        key = "\0".join(key_values) if any(key_values) else _stable_id("row", row)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _format_comment_template(template: str, post_id: str) -> str:
    safe_post_id = quote(post_id, safe="")
    return template.format(post_id=safe_post_id, raw_post_id=post_id, id=safe_post_id)


def _resolve_url(path: str, api_base: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return urljoin(api_base.rstrip("/") + "/", path.lstrip("/"))


def _first_text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _nested_value(row, key)
        text = _clean_text(value)
        if text:
            return text
    return ""


def _nested_value(row: dict[str, Any], key: str) -> Any:
    value: Any = row
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(_clean_text(item) for item in value if _clean_text(item)).strip()
    if isinstance(value, dict):
        for key in ("text", "html", "value", "body", "content"):
            text = _clean_text(value.get(key))
            if text:
                return text
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _stable_id(prefix: str, row: dict[str, Any]) -> str:
    encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:16]}"
