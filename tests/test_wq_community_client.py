import json
from pathlib import Path
from unittest.mock import MagicMock

from quantgpt.wq_community_client import (
    WQCommunityExportConfig,
    export_community,
    fetch_collection,
    normalize_comment,
    normalize_post,
)


def _response(status_code: int, payload):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    resp.text = json.dumps(payload)
    resp.json.return_value = payload
    return resp


def _read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_fetch_collection_follows_next_url():
    session = MagicMock()
    session.get.side_effect = [
        _response(200, {"results": [{"id": "p1"}], "next": "/community/posts?offset=1"}),
        _response(200, {"results": [{"id": "p2"}]}),
    ]

    result = fetch_collection(
        session,
        ["/community/posts"],
        api_base="https://api.test",
        limit=1,
        max_pages=3,
        sleep_seconds=0,
    )

    assert result.rows == [{"id": "p1"}, {"id": "p2"}]
    assert session.get.call_args_list[0].args[0] == "https://api.test/community/posts"
    assert session.get.call_args_list[1].args[0] == "https://api.test/community/posts?offset=1"


def test_fetch_collection_accepts_zendesk_next_page():
    session = MagicMock()
    session.get.side_effect = [
        _response(200, {"posts": [{"id": "p1"}], "next_page": "https://support.test/api/v2/community/posts.json?page=2"}),
        _response(200, {"posts": [{"id": "p2"}], "next_page": None}),
    ]

    result = fetch_collection(
        session,
        ["https://support.test/api/v2/community/posts.json"],
        limit=100,
        max_pages=3,
        sleep_seconds=0,
    )

    assert result.rows == [{"id": "p1"}, {"id": "p2"}]
    assert session.get.call_args_list[1].args[0] == "https://support.test/api/v2/community/posts.json?page=2"


def test_normalizers_accept_nested_html_and_inherit_post_context():
    post = normalize_post({
        "id": "p1",
        "subject": "Volume thread",
        "details": "<p>Use volume &amp; adv20</p>",
        "html_url": "https://support.test/post/p1",
        "createdAt": "2026-05-13T00:00:00Z",
    })
    comment = normalize_comment({"id": "c1", "body": {"text": "Try ts_corr(close, volume, 10)"}}, post)

    assert post["post_id"] == "p1"
    assert post["body_text"] == "Use volume & adv20"
    assert post["url"] == "https://support.test/post/p1"
    assert comment["post_id"] == "p1"
    assert comment["title"] == "Volume thread"
    assert comment["body_text"] == "Try ts_corr(close, volume, 10)"


def test_export_community_writes_posts_and_comments(tmp_path):
    session = MagicMock()
    session.get.side_effect = [
        _response(200, {
            "results": [{
                "id": "p1",
                "title": "VWAP idea",
                "body": "close/vwap",
                "comments": [{"id": "embedded-c1", "body": "embedded reply"}],
            }]
        }),
        _response(200, {"results": [{"id": "c2", "body": "fetched reply"}]}),
    ]

    manifest = export_community(
        session,
        WQCommunityExportConfig(
            output_dir=tmp_path,
            post_paths=["/community/posts"],
            comment_path_templates=["/community/posts/{post_id}/comments"],
            sleep_seconds=0,
        ),
    )

    posts = _read_jsonl(tmp_path / "posts.jsonl")
    comments = _read_jsonl(tmp_path / "comments.jsonl")

    assert manifest["posts"] == 1
    assert manifest["comments"] == 2
    assert posts[0]["post_id"] == "p1"
    assert {row["comment_id"] for row in comments} == {"embedded-c1", "c2"}
