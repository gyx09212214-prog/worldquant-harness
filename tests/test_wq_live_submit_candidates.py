from scripts.wq_live_submit_candidates import _is_ready_to_submit


def test_ready_to_submit_honors_local_self_corr_cutoff():
    row = {
        "api_check_status": "api_check_readable",
        "sc_result": "PASS",
        "sc_value": 0.8324,
        "prod_corr_result": "MISSING",
    }

    assert not _is_ready_to_submit(row, 0.7)


def test_ready_to_submit_allows_platform_pass_below_cutoff():
    row = {
        "api_check_status": "api_check_readable",
        "sc_result": "PASS",
        "sc_value": "0.52",
        "prod_corr_result": "MISSING",
    }

    assert _is_ready_to_submit(row, 0.7)


def test_ready_to_submit_pending_requires_explicit_flag():
    row = {"api_check_status": "api_check_pending"}

    assert not _is_ready_to_submit(row, 0.7)
    assert _is_ready_to_submit(row, 0.7, submit_pending=True)
