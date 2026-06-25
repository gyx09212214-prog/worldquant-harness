import json
from pathlib import Path

from worldquant_harness.community_triage import (
    CommunityTriageConfig,
    build_community_items,
    triage_community,
    triage_item,
)


def _read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_triage_generates_wq_candidate_rows(tmp_path):
    posts = tmp_path / "posts.jsonl"
    comments = tmp_path / "comments.jsonl"
    out = tmp_path / "triage"
    posts.write_text(
        json.dumps({
            "post_id": "p1",
            "title": "VWAP reversal plus fundamentals",
            "url": "https://example.test/p1",
            "body_text": (
                "The useful idea is close/vwap mean reversion. "
                "Try a decay window 10 and add sales/assets. "
                "Sharpe and Fitness improved. `rank(ts_decay_linear(close / vwap, 10))`"
            ),
        })
        + "\n",
        encoding="utf-8",
    )
    comments.write_text("", encoding="utf-8")

    manifest = triage_community(CommunityTriageConfig(posts_file=posts, comments_file=comments, output_dir=out))
    records = _read_jsonl(out / "triage_records.jsonl")
    candidates = _read_jsonl(out / "community_wq_candidates.jsonl")

    assert manifest["triage_records"] == 1
    assert records[0]["value_type"] == "candidate_seed"
    assert "possible_complete_alpha" in records[0]["risk_flags"]
    assert "-1 * rank(ts_decay_linear(close / vwap, 10))" in records[0]["candidate_expressions"]
    assert any("actual_sales_value_quarterly / assets" in row["expression"] for row in candidates)
    assert (out / "community_factor_triage.md").is_file()
    assert (out / "knowledge_suggestions" / "findings.md").is_file()


def test_triage_classifies_platform_and_correlation_failures_without_candidate():
    item = build_community_items(
        [{
            "post_id": "p2",
            "title": "SC fail notes",
            "body_text": (
                "pasteurize is unavailable on my tier and this family gets SC FAIL. "
                "Changing only the window does not fix self correlation."
            ),
        }],
        [],
    )[0]

    record = triage_item(item)

    assert record["value_type"] == "failure_case"
    assert "platform_limit" in record["risk_flags"]
    assert "correlation_risk" in record["risk_flags"]
    assert record["candidate_expressions"] == []


def test_comment_records_inherit_post_context_and_generate_volume_seed(tmp_path):
    posts = [{"post_id": "p3", "title": "Volume ideas", "url": "https://example.test/p3", "body_text": "Thread"}]
    comments = [{
        "comment_id": "c1",
        "post_id": "p3",
        "body_text": "Abnormal volume shock and turnover hint: use volume/adv20 style structures.",
    }]

    item = build_community_items(posts, comments)[1]
    record = triage_item(item)

    assert item.url == "https://example.test/p3"
    assert item.title == "Volume ideas"
    assert record["source_type"] == "comment"
    assert record["value_type"] == "candidate_seed"
    assert "rank(volume / ts_mean(volume, 20))" in record["candidate_expressions"]


def test_sentiment_hint_uses_platform_field():
    item = build_community_items(
        [{
            "post_id": "p4",
            "title": "Sentiment idea",
            "body_text": "A short-term sentiment delta looks useful as an alpha seed.",
        }],
        [],
    )[0]

    record = triage_item(item)

    assert "rank(ts_delta(scl12_sentiment_fast_d1, 5))" in record["candidate_expressions"]
