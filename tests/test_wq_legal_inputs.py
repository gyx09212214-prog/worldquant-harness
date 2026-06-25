import json
import shutil
import uuid
from pathlib import Path

import pytest

from worldquant_harness.wq_legal_inputs import WQLegalInputRegistry, load_legal_input_registry


@pytest.fixture
def workdir():
    path = Path(__file__).resolve().parents[1] / ".test_tmp" / f"wq_legal_inputs_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _discovery_payload() -> dict:
    return {
        "created_at": "2026-06-21T00:00:00",
        "user": {"email": "private@example.com", "name": "Private User"},
        "combos": [
            {
                "region": "USA",
                "universe": "TOP3000",
                "delay": 1,
                "datasets": {
                    "results": [
                        {
                            "id": "sentiment12",
                            "name": "Sentiment",
                            "category": {"id": "sentiment"},
                            "subcategory": {"id": "sentiment-news"},
                        }
                    ]
                },
                "fields_by_dataset": {
                    "sentiment12": {
                        "results": [
                            {
                                "id": "snt1_d1_netearningsrevision",
                                "type": "MATRIX",
                                "dataset": {"id": "sentiment12"},
                                "category": {"id": "sentiment"},
                                "subcategory": {"id": "sentiment-news"},
                                "region": "USA",
                                "universe": "TOP3000",
                                "delay": 1,
                                "coverage": 0.42,
                                "userCount": 999,
                            },
                            {
                                "id": "scl12_buzzvec",
                                "type": "VECTOR",
                                "dataset": {"id": "sentiment12"},
                                "category": {"id": "sentiment"},
                                "region": "USA",
                                "universe": "TOP3000",
                                "delay": 1,
                                "coverage": 0.72,
                            },
                            "@{id=string_field; type=MATRIX; coverage=0.5}",
                        ]
                    }
                },
            }
        ],
    }


def _registry_file(workdir: Path) -> Path:
    discovery = workdir / "raw_discovery.json"
    registry_file = workdir / "wq_legal_inputs.json"
    _write_json(discovery, _discovery_payload())
    registry = WQLegalInputRegistry.compile_from_discovery(discovery, account="primary")
    registry.write(registry_file)
    return registry_file


def test_compile_sanitizes_discovery_and_summarizes_fields(workdir):
    registry_file = _registry_file(workdir)
    text = registry_file.read_text(encoding="utf-8")

    assert "private@example.com" not in text
    assert "Private User" not in text

    registry = load_legal_input_registry(registry_file)
    summary = registry.summary()
    combo = summary["combos"][0]
    assert summary["combo_count"] == 1
    assert combo["field_count"] >= 4
    assert combo["field_type_counts"]["VECTOR"] == 1
    assert "sentiment12" in registry.to_payload()["accounts"]["primary"]["combos"]["USA|TOP3000|1"]["datasets"]


def test_validate_core_and_discovered_fields(workdir):
    registry = load_legal_input_registry(_registry_file(workdir))

    assert registry.validate_expression("rank(close)", region="USA", universe="TOP3000", delay=1).ok
    assert registry.validate_expression("rank(snt1_d1_netearningsrevision)", region="USA", universe="TOP3000", delay=1).ok
    assert registry.validate_expression("rank(string_field)", region="USA", universe="TOP3000", delay=1).ok
    assert registry.validate_expression("group_neutralize(rank(close), subindustry)", region="USA", universe="TOP3000", delay=1).ok


def test_validate_rejects_static_fallback_fields_in_strict_mode(workdir):
    registry = load_legal_input_registry(_registry_file(workdir))

    strict = registry.validate_expression("rank(implied_volatility_skew)", region="USA", universe="TOP3000", delay=1)
    loose = registry.validate_expression(
        "rank(implied_volatility_skew)",
        region="USA",
        universe="TOP3000",
        delay=1,
        strict=False,
    )

    assert strict.ok is False
    assert strict.primary_error_code() == "unavailable_dataset_field"
    assert loose.ok is True
    assert loose.warnings[0]["code"] == "static_fallback_field"


def test_validate_rejects_forbidden_operator_unknown_field_and_vector_misuse(workdir):
    registry = load_legal_input_registry(_registry_file(workdir))

    forbidden = registry.validate_expression("pasteurize(rank(close))", region="USA", universe="TOP3000", delay=1)
    assert forbidden.ok is False
    assert forbidden.primary_error_code() == "illegal_operator"

    unknown = registry.validate_expression("rank(not_a_real_field)", region="USA", universe="TOP3000", delay=1)
    assert unknown.ok is False
    assert unknown.primary_error_code() == "illegal_field"

    vector_without_vec = registry.validate_expression("rank(scl12_buzzvec)", region="USA", universe="TOP3000", delay=1)
    assert vector_without_vec.ok is False
    assert vector_without_vec.primary_error_code() == "illegal_field_type"

    vector_ok = registry.validate_expression("vec_avg(scl12_buzzvec)", region="USA", universe="TOP3000", delay=1)
    assert vector_ok.ok is True


def test_validate_candidate_schema_and_settings(workdir):
    registry = load_legal_input_registry(_registry_file(workdir))

    missing = registry.validate_candidate({}, region="USA", universe="TOP3000", delay=1)
    assert missing.ok is False
    assert missing.primary_error_code() == "illegal_candidate_schema"

    bad_settings = registry.validate_candidate(
        {"expression": "rank(close)", "simulation_settings": {"badKey": True}},
        region="USA",
        universe="TOP3000",
        delay=1,
    )
    assert bad_settings.ok is False
    assert bad_settings.primary_error_code() == "illegal_candidate_schema"
