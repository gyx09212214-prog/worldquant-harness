import json
import shutil
import uuid
from pathlib import Path

from worldquant_harness.wq_submission_experience import WQSubmissionExperienceConfig, build_submission_experience


def _workdir() -> Path:
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / f"wq_submission_experience_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_build_submission_experience_generates_rules_and_memory():
    workdir = _workdir()
    try:
        reports = workdir / "reports"
        output = workdir / "experience"
        _write_jsonl(reports / "run_a" / "simulation_results.jsonl", [
            {
                "alpha_id": "alpha_credit",
                "expression": "rank(ts_rank(rp_css_credit, 40))",
                "tag": "credit-standalone",
                "source_family": "research_credit",
                "sharpe": 0.1,
                "fitness": 0.02,
                "turnover": 0.4,
                "failed_platform_checks": [
                    {"name": "LOW_SHARPE", "result": "FAIL"},
                    {"name": "LOW_FITNESS", "result": "FAIL"},
                    {"name": "CONCENTRATED_WEIGHT", "result": "FAIL"},
                ],
            },
            {
                "alpha_id": "alpha_news",
                "expression": "rank(ts_rank(news_max_dn_ret, 20))",
                "tag": "news-standalone",
                "source_family": "research_news",
                "sharpe": 0.2,
                "fitness": 0.03,
                "turnover": 1.3,
                "failed_platform_checks": [
                    {"name": "LOW_SHARPE", "result": "FAIL"},
                    {"name": "HIGH_TURNOVER", "result": "FAIL"},
                ],
            },
            {
                "alpha_id": "alpha_forward",
                "expression": "rank(ts_rank(forward_sales_to_price, 80))",
                "tag": "forward-near",
                "source_family": "research_forward",
                "sharpe": 0.65,
                "fitness": 0.9,
                "turnover": 0.06,
                "failed_platform_checks": [
                    {"name": "LOW_SHARPE", "result": "FAIL"},
                    {"name": "LOW_FITNESS", "result": "FAIL"},
                    {"name": "CONCENTRATED_WEIGHT", "result": "FAIL"},
                ],
            },
        ])
        _write_jsonl(reports / "run_b" / "simulation_results.jsonl", [
            {
                "alpha_id": "alpha_credit_2",
                "expression": "rank(ts_rank(rp_css_credit_ratings, 40))",
                "tag": "credit-rating",
                "source_family": "research_credit",
                "sharpe": 0.05,
                "fitness": 0.01,
                "turnover": 0.3,
                "failed_platform_checks": [{"name": "CONCENTRATED_WEIGHT", "result": "FAIL"}],
            },
            {
                "alpha_id": "alpha_submitted",
                "expression": "rank(ts_rank(cashflow_op / cap, 80))",
                "tag": "good",
                "source_family": "research_cashflow",
                "sharpe": 1.5,
                "fitness": 1.1,
                "turnover": 0.2,
                "submitted": True,
            },
        ])

        summary = build_submission_experience(WQSubmissionExperienceConfig(
            reports_dir=reports,
            output_dir=output,
            min_field_evidence=1,
        ))

        rules = json.loads((output / "experience_rules.json").read_text(encoding="utf-8"))
        memory = [
            json.loads(line)
            for line in (output / "submission_experience_memory.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        assert summary["ok"] is True
        assert summary["record_count"] == 5
        assert summary["memory_count"] == 4
        assert any(row["rule_id"] == "no_single_non_price_standalone" for row in rules["structure_rules"])
        assert any(row["rule_id"] == "near_pass_repair" for row in rules["repair_rules"])
        assert any("repair_near_pass_before_fresh_budget" in row["repair_hints"] for row in memory)
        assert (output / "submission_experience.md").is_file()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
