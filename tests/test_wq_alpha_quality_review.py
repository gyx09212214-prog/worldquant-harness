import json
from pathlib import Path

from worldquant_harness.wq_alpha_quality_review import (
    WQAlphaQualityReviewConfig,
    build_alpha_quality_review,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def test_quality_review_checks_window_unsubmitted_and_writes_outputs(tmp_path):
    reports = tmp_path / "reports"
    output = tmp_path / "out"
    obsidian = tmp_path / "obsidian" / "review.md"
    profile_dir = tmp_path / "profiles"
    _write_map_files(reports)

    class FakeClient:
        checked: list[str] = []

        def authenticate(self):
            return True

        def close(self):
            return None

        def get_json(self, path, params=None):
            assert path == "/users/self/alphas"
            return {
                "ok": True,
                "count": 3,
                "results": [
                    {
                        "id": "active1",
                        "status": "ACTIVE",
                        "dateCreated": "2026-06-10T00:00:00+00:00",
                        "dateSubmitted": "2026-06-12T00:00:00+00:00",
                        "regular": {"code": "rank(close)"},
                        "is": {"sharpe": 1.6, "fitness": 1.2, "returns": 0.08, "turnover": 0.2},
                        "settings": {"region": "USA", "universe": "TOP3000", "delay": 1},
                    },
                    {
                        "id": "unsub1",
                        "status": "UNSUBMITTED",
                        "dateCreated": "2026-06-15T00:00:00+00:00",
                        "regular": {"code": "rank(ts_corr(vwap, volume, 20))"},
                        "is": {"sharpe": 1.4, "fitness": 1.05, "returns": 0.05, "turnover": 0.18},
                        "settings": {"region": "USA", "universe": "TOP3000", "delay": 1},
                    },
                    {
                        "id": "old_unsub",
                        "status": "UNSUBMITTED",
                        "dateCreated": "2025-01-01T00:00:00+00:00",
                        "regular": {"code": "rank(open)"},
                        "is": {"sharpe": 1.1, "fitness": 0.8, "turnover": 0.2},
                        "settings": {"region": "USA", "universe": "TOP3000", "delay": 1},
                    },
                ],
            }

        def check_alpha_submission(self, alpha_id, max_polls=1, interval=0):
            self.checked.append(alpha_id)
            assert alpha_id == "unsub1"
            return {
                "ok": False,
                "status": "UNSUBMITTED",
                "review_checks": {
                    "self_correlation": {"name": "SELF_CORRELATION", "result": "FAIL", "value": 0.82, "limit": 0.7},
                    "prod_correlation": {"name": "PROD_CORRELATION", "result": "PASS", "value": 0.2, "limit": 0.7},
                },
                "failure_kind": "self_correlation",
            }

    fake = FakeClient()
    report = build_alpha_quality_review(
        WQAlphaQualityReviewConfig(
            reports_dir=reports,
            output_dir=output,
            since="2026-06-01",
            until="2026-06-30",
            check_interval=0,
            obsidian_output=obsidian,
            profile_dir=profile_dir,
        ),
        client_factory=lambda account: fake,
    )

    assert report["ok"] is True
    assert fake.checked == ["unsub1"]
    assert report["platform"]["check_count"] == 1
    assert report["metrics"]["submitted_count"] == 1
    assert report["metrics"]["generated_self_correlation_fail_share"] == 1.0
    assert any(row["group_type"] == "field_signature" and row["self_correlation_fail_count"] == 1 for row in report["self_correlation_pressure"])
    assert report["recommended_directions"]
    assert Path(report["files"]["profile_candidate"]).is_file()
    assert obsidian.is_file()
    records = _read_jsonl(Path(report["files"]["quality_alpha_events"]))
    assert {row["alpha_id"] for row in records} == {"active1", "unsub1"}


def test_quality_review_local_only_uses_presubmit_artifacts_and_reweights_score(tmp_path):
    reports = tmp_path / "reports"
    output = tmp_path / "out"
    _write_jsonl(
        reports / "run" / "presubmit_ready_sequential.jsonl",
        [
            {
                "created_at": "2026-06-20T00:00:00+00:00",
                "alpha_id": "ready1",
                "expression": "rank(ts_rank(close, 20))",
                "status": "eligible",
                "sharpe": 1.7,
                "fitness": 1.2,
                "turnover": 0.2,
            }
        ],
    )
    _write_jsonl(
        reports / "run" / "presubmit_rejected.jsonl",
        [
            {
                "created_at": "2026-06-21T00:00:00+00:00",
                "alpha_id": "reject1",
                "expression": "rank(ts_rank(close, 10) + ts_rank(returns, 5))",
                "presubmit_reject_reason": "self_correlation_value_above_strict_cutoff",
                "sc_value": 0.86,
                "sharpe": 1.5,
                "fitness": 1.05,
                "turnover": 0.16,
            }
        ],
    )

    report = build_alpha_quality_review(
        WQAlphaQualityReviewConfig(
            reports_dir=reports,
            output_dir=output,
            since="2026-06-01",
            until="2026-06-30",
            platform_enabled=False,
            write_profile_candidate=False,
        )
    )

    assert report["platform"]["enabled"] is False
    assert report["metrics"]["submitted_quality_score"] is None
    assert report["metrics"]["generated_quality_score"] is not None
    assert report["metrics"]["period_quality_score"] is not None
    assert report["metrics"]["generated_count"] == 2
    assert report["metrics"]["generated_ready_rate"] == 0.5
    assert Path(report["files"]["markdown"]).is_file()


def _write_map_files(reports: Path) -> None:
    map_dir = reports / "wq_active_alpha_map"
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / "active_domain_summary.csv").write_text(
        "domain,node_count,active_or_submitted_count,self_corr_fail_count,opportunity_score,crowded_score,top_fields\n"
        "liquidity_microstructure,1,0,0,3.5,0.8,\"vwap,volume\"\n",
        encoding="utf-8",
    )
    (map_dir / "active_field_summary.csv").write_text(
        "field,node_count,active_or_submitted_count,self_corr_fail_count,avg_fitness\n"
        "vwap,1,0,0,1.0\n"
        "volume,1,0,0,1.0\n",
        encoding="utf-8",
    )
