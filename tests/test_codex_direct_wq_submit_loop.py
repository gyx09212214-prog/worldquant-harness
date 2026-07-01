import argparse
import importlib.util
from pathlib import Path


def _load_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "codex_direct_wq_submit_loop.py"
    spec = importlib.util.spec_from_file_location("codex_direct_wq_submit_loop_for_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_candidate_key_is_stable_and_settings_sensitive():
    module = _load_module()
    candidate = {
        "tag": "alpha-a",
        "expression": "rank(close)",
        "simulation_settings": {"neutralization": "INDUSTRY", "decay": 4, "truncation": 0.03},
    }

    assert module.candidate_key(candidate) == module.candidate_key(dict(candidate))

    changed = dict(candidate)
    changed["simulation_settings"] = {"neutralization": "SUBINDUSTRY", "decay": 4, "truncation": 0.03}
    assert module.candidate_key(candidate) != module.candidate_key(changed)


def test_candidate_settings_prefers_candidate_over_cli_defaults():
    module = _load_module()
    args = argparse.Namespace(neutralization="SUBINDUSTRY", decay=0, truncation=0.08)

    assert module.candidate_settings({}, args) == {
        "neutralization": "SUBINDUSTRY",
        "decay": 0,
        "truncation": 0.08,
    }
    assert module.candidate_settings(
        {"simulation_settings": {"neutralization": "INDUSTRY", "decay": 6, "truncation": 0.02}},
        args,
    ) == {
        "neutralization": "INDUSTRY",
        "decay": 6,
        "truncation": 0.02,
    }


def test_final_status_from_submit_maps_platform_failures():
    module = _load_module()

    assert module.final_status_from_submit({"ok": True, "platform_status": "ACTIVE"}) == "ACTIVE"
    assert module.final_status_from_submit({"ok": False, "failure_kind": "self_correlation"}) == "SC_FAIL"
    assert module.final_status_from_submit({"ok": False, "failure_kind": "prod_correlation"}) == "PROD_FAIL"
    assert module.final_status_from_submit({"ok": False, "failure_kind": "timeout"}) == "TIMEOUT"
    assert module.final_status_from_submit({"ok": False}) == "FAIL"
