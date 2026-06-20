from unittest.mock import MagicMock

from quantgpt.wq_brain_client import WQBrainClient


def _response(status_code: int, payload: dict):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = str(payload)
    resp.json.return_value = payload
    return resp


def test_submit_alpha_classifies_prod_correlation_403():
    client = WQBrainClient(email="a@b.com", password="pw")
    session = MagicMock()
    session.post.return_value = _response(403, {
        "is": {
            "checks": [
                {"name": "PROD_CORRELATION", "result": "FAIL", "value": 0.81, "limit": 0.7}
            ]
        }
    })
    client._session = session

    result = client.submit_alpha("alpha1")

    assert result["ok"] is False
    assert result["failure_kind"] == "prod_correlation"
    assert result["prod_value"] == 0.81
    assert result["review_checks"]["prod_correlation"]["result"] == "FAIL"


def test_poll_alpha_submission_returns_active_with_review_checks():
    client = WQBrainClient(email="a@b.com", password="pw")
    session = MagicMock()
    session.get.return_value = _response(200, {
        "status": "ACTIVE",
        "is": {
            "checks": [
                {"name": "SELF_CORRELATION", "result": "PASS", "value": 0.0, "limit": 0.7}
            ]
        },
    })
    client._session = session

    result = client._poll_alpha_submission("alpha1", max_polls=1, interval=0)

    assert result["ok"] is True
    assert result["platform_status"] == "ACTIVE"
    assert result["review_checks"]["self_correlation"]["result"] == "PASS"


def test_poll_alpha_submission_stops_on_prod_correlation_fail():
    client = WQBrainClient(email="a@b.com", password="pw")
    session = MagicMock()
    session.get.return_value = _response(200, {
        "status": "UNSUBMITTED",
        "is": {
            "checks": [
                {"name": "PROD_CORRELATION_SHARPE", "result": "FAIL", "value": 0.9, "limit": 0.7}
            ]
        },
    })
    client._session = session

    result = client._poll_alpha_submission("alpha1", max_polls=1, interval=0)

    assert result["ok"] is False
    assert result["failure_kind"] == "prod_correlation"
    assert result["prod_value"] == 0.9
    assert "PROD_CORRELATION_SHARPE FAIL" in result["detail"]


def test_check_alpha_submission_uses_check_endpoint_without_submit():
    client = WQBrainClient(email="a@b.com", password="pw")
    session = MagicMock()
    session.get.return_value = _response(200, {
        "status": "UNSUBMITTED",
        "is": {
            "sharpe": 2.0,
            "fitness": 1.35,
            "turnover": 0.3095,
            "checks": [
                {"name": "SELF_CORRELATION", "result": "PASS", "value": 0.7961, "limit": 0.7}
            ],
        },
    })
    session.post.side_effect = AssertionError("check submission must not submit")
    client._session = session

    result = client.check_alpha_submission("alpha1", max_polls=1, interval=0)

    assert result["ok"] is True
    assert result["status"] == "UNSUBMITTED"
    assert result["review_checks"]["self_correlation"]["result"] == "PASS"
    assert result["review_checks"]["self_correlation"]["value"] == 0.7961
    session.get.assert_called_once()
    assert session.get.call_args.args[0].endswith("/alphas/alpha1/check")
    session.post.assert_not_called()


def test_check_alpha_submission_classifies_self_correlation_fail():
    client = WQBrainClient(email="a@b.com", password="pw")
    session = MagicMock()
    session.get.return_value = _response(200, {
        "status": "UNSUBMITTED",
        "is": {
            "checks": [
                {"name": "SELF_CORRELATION", "result": "FAIL", "value": 0.8457, "limit": 0.7}
            ],
        },
    })
    client._session = session

    result = client.check_alpha_submission("alpha1", max_polls=1, interval=0)

    assert result["ok"] is False
    assert result["failure_kind"] == "self_correlation"
    assert result["sc_value"] == 0.8457
    assert "SELF_CORRELATION FAIL" in result["detail"]


def test_check_alpha_submission_empty_review_times_out_as_pending():
    client = WQBrainClient(email="a@b.com", password="pw")
    session = MagicMock()
    session.get.return_value = _response(200, {
        "status": "UNSUBMITTED",
        "is": {"checks": []},
    })
    client._session = session

    result = client.check_alpha_submission("alpha1", max_polls=2, interval=0)

    assert result["ok"] is False
    assert result["failure_kind"] == "correlation_pending"
    assert session.get.call_count == 2


def test_get_alpha_raw_uses_get_only():
    client = WQBrainClient(email="a@b.com", password="pw")
    session = MagicMock()
    session.get.return_value = _response(200, {"id": "alpha1", "status": "ACTIVE"})
    session.post.side_effect = AssertionError("raw alpha read must not post")
    client._session = session

    result = client.get_alpha_raw("alpha1")

    assert result["ok"] is True
    assert result["alpha_id"] == "alpha1"
    assert result["data"]["status"] == "ACTIVE"
    session.get.assert_called_once()
    assert session.get.call_args.args[0].endswith("/alphas/alpha1")
    session.post.assert_not_called()


def test_simulate_sends_position_controls():
    client = WQBrainClient(email="a@b.com", password="pw")
    post_response = _response(201, {})
    post_response.headers = {"Location": "/simulations/sim1"}
    session = MagicMock()
    session.post.return_value = post_response
    session.get.return_value = _response(200, {
        "id": "sim1",
        "status": "DONE",
        "progress": 1,
        "alpha": "/alphas/alpha1",
        "is": {"sharpe": 1.0, "fitness": 1.0, "turnover": 0.1, "returns": 0.05, "checks": []},
        "settings": {"maxPosition": "ON", "maxTrade": "OFF"},
    })
    client._session = session

    result = client.simulate("rank(open)", max_position="ON", max_trade="OFF")

    payload = session.post.call_args.kwargs["json"]
    assert result["ok"] is True
    assert payload["settings"]["maxPosition"] == "ON"
    assert payload["settings"]["maxTrade"] == "OFF"


def test_probe_alpha_detail_only_calls_allowlisted_get_paths():
    client = WQBrainClient(email="a@b.com", password="pw")
    session = MagicMock()
    session.get.return_value = _response(404, {"detail": "not found"})
    session.post.side_effect = AssertionError("detail probe must not post")
    session.delete.side_effect = AssertionError("detail probe must not delete")
    client._session = session

    result = client.probe_alpha_detail("alpha1", paths=["/alphas/{alpha_id}", "/users/self"])

    assert result["read_only"] is True
    assert len(result["endpoints"]) == 2
    assert result["endpoints"][1]["error"] == "path is not in READ_ONLY_ALPHA_DETAIL_PATHS"
    session.get.assert_called_once()
    assert session.get.call_args.args[0].endswith("/alphas/alpha1")
    session.post.assert_not_called()
    session.delete.assert_not_called()
