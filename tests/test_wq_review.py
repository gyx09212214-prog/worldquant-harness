from quantgpt.wq_review import (
    correlation_failure_detail,
    parse_review_checks,
    primary_failure_kind,
    review_checks_passed,
)


def test_parse_self_correlation_fail():
    review = parse_review_checks({
        "is": {
            "checks": [
                {"name": "SELF_CORRELATION", "result": "FAIL", "value": 0.83, "limit": 0.7}
            ]
        }
    })

    assert review["self_correlation"]["result"] == "FAIL"
    assert review["self_correlation"]["value"] == 0.83
    assert review["failed"] == ["self_correlation"]
    assert primary_failure_kind(review) == "self_correlation"
    assert correlation_failure_detail(review, "self_correlation") == "SELF_CORRELATION FAIL: value=0.83 > limit=0.7"


def test_parse_prod_correlation_name_variants():
    review = parse_review_checks({
        "is": {
            "checks": [
                {"name": "PROD_CORRELATION_SHARPE", "result": "FAIL", "value": 0.76, "limit": 0.7},
                {"name": "PROD_CORRELATION", "result": "PASS", "value": 0.11, "limit": 0.7},
            ]
        }
    })

    assert review["prod_correlation"]["name"] == "PROD_CORRELATION_SHARPE"
    assert review["prod_correlation"]["result"] == "FAIL"
    assert review["failed"] == ["prod_correlation"]
    assert primary_failure_kind(review) == "prod_correlation"


def test_parse_pending_and_scalar_correlation_fields():
    review = parse_review_checks({
        "is": {
            "selfCorrelation": 0.12,
            "prodCorrelation": 0.2,
            "checks": [{"name": "SELF_CORRELATION", "result": "PENDING"}],
        }
    })

    assert review["self_correlation"]["result"] == "PENDING"
    assert review["self_correlation"]["value"] is None
    assert review["prod_correlation"]["result"] == "MISSING"
    assert review["prod_correlation"]["value"] == 0.2
    assert review["pending"] == ["self_correlation"]


def test_missing_checks_are_stable_and_pass_requires_known_pass():
    missing = parse_review_checks({})
    passed = parse_review_checks({
        "is": {"checks": [{"name": "SELF_CORRELATION", "result": "PASS", "value": 0.0, "limit": 0.7}]}
    })

    assert missing["self_correlation"]["result"] == "MISSING"
    assert missing["prod_correlation"]["result"] == "MISSING"
    assert review_checks_passed(missing) is False
    assert review_checks_passed(passed) is True
