"""Legal input registry for WorldQuant BRAIN candidate generation.

The registry is an offline artifact compiled from explicit field discovery.
It keeps candidate mining from spending simulation budget on unavailable
fields, local-only operators, or malformed candidate specs.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .expression_parser import (
    _ALIAS_NORMALIZE,
    _WQ_EXTENDED_FIELDS,
    _WQ_OPERATORS,
    _WQ_REMOTE_ONLY_OPS,
    _WQ_SPECIAL_VARS,
    extract_components,
    normalize_expression,
    parse_expression,
)
from .record_utils import first_float as _safe_float

SCHEMA_VERSION = 1
DEFAULT_FORBIDDEN_FIELDS = {"short_interest", "short_ratio"}
DEFAULT_FORBIDDEN_OPERATORS = {"pasteurize"}
DEFAULT_GROUP_FIELDS = {"industry", "subindustry", "sector", "market", "none"}
CORE_FIELDS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "returns",
    "cap",
    "market_cap",
}
LOCAL_COMPATIBILITY_FIELDS = {
    "analyst_revision_rank_derivative",
    "cash_burn_rate",
    "cashflow_efficiency_rank_derivative",
    "change_in_eps_surprise",
    "composite_factor_score_derivative",
    "earnings_certainty_rank_derivative",
    "forward_earnings_yield",
    "growth_potential_rank_derivative",
    "multi_factor_acceleration_score_derivative",
    "relative_valuation_rank_derivative",
}
ALLOWED_SIMULATION_SETTING_KEYS = {
    "region",
    "universe",
    "delay",
    "decay",
    "neutralization",
    "truncation",
    "maxTrade",
    "maxPosition",
}
VECTOR_OPERATORS = {
    "vector_neut",
    "group_vector_neut",
    "vec_avg",
    "vec_sum",
    "vec_max",
    "vec_min",
    "vec_count",
    "vec_range",
    "vec_stddev",
    "vec_skewness",
    "vec_kurtosis",
    "vec_ir",
    "vec_norm",
    "vec_percentage",
    "vec_choose",
}


@dataclass(frozen=True)
class WQLegalInputConfig:
    registry_file: Path | None = None
    account: str = "primary"
    region: str = "USA"
    universe: str = "TOP3000"
    delay: int = 1
    strict: bool = True


@dataclass(frozen=True)
class WQFieldSpec:
    id: str
    type: str = "UNKNOWN"
    domain: str = "unknown"
    dataset_id: str = ""
    category: str = ""
    subcategory: str = ""
    region: str = ""
    universe: str = ""
    delay: int | None = None
    coverage: float | None = None
    source: str = "static"


@dataclass(frozen=True)
class WQOperatorSpec:
    name: str
    remote_only: bool = False
    forbidden: bool = False
    source: str = "static"


@dataclass(frozen=True)
class WQCandidateValidationResult:
    ok: bool
    errors: tuple[dict[str, Any], ...]
    warnings: tuple[dict[str, Any], ...]
    fields: tuple[str, ...]
    operators: tuple[str, ...]
    field_specs: tuple[dict[str, Any], ...]
    normalized_expression: str

    def primary_error_code(self) -> str:
        if not self.errors:
            return ""
        return str(self.errors[0].get("code") or "illegal_candidate")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "fields": list(self.fields),
            "operators": list(self.operators),
            "field_specs": list(self.field_specs),
            "normalized_expression": self.normalized_expression,
            "primary_error_code": self.primary_error_code(),
        }


class WQLegalInputRegistry:
    """Offline registry of legal WQ fields and operators."""

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.schema_version = int(payload.get("schema_version") or 1)
        self.forbidden_fields = {
            _field_id(value)
            for value in payload.get("forbidden_fields", sorted(DEFAULT_FORBIDDEN_FIELDS))
            if _field_id(value)
        }
        self.forbidden_operators = {
            _operator_id(value)
            for value in payload.get("forbidden_operators", sorted(DEFAULT_FORBIDDEN_OPERATORS))
            if _operator_id(value)
        }
        self.operators = {
            _operator_id(name): dict(spec or {})
            for name, spec in (payload.get("operators") or {}).items()
            if _operator_id(name)
        }
        self._global_fields = self._build_global_field_index()

    @classmethod
    def from_file(cls, path: Path | str) -> WQLegalInputRegistry:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(payload)

    @classmethod
    def compile_from_discovery(
        cls,
        discovery_file: Path | str,
        *,
        account: str = "primary",
    ) -> WQLegalInputRegistry:
        discovery_path = Path(discovery_file)
        raw = json.loads(discovery_path.read_text(encoding="utf-8"))
        payload = _base_payload(
            source={
                "discovery_file": discovery_path.name,
                "discovery_created_at": raw.get("created_at"),
                "sanitized": True,
            }
        )
        account_payload = payload["accounts"].setdefault(account, {"combos": {}})

        for combo in raw.get("combos") or []:
            if not isinstance(combo, dict):
                continue
            region = str(combo.get("region") or "USA").upper()
            universe = str(combo.get("universe") or "TOP3000").upper()
            delay = _safe_int(combo.get("delay"), default=1)
            key = combo_key(region, universe, delay)
            datasets = _dataset_index(combo.get("datasets"))
            fields = _static_field_map(region=region, universe=universe, delay=delay)

            for dataset_id, field_payload in (combo.get("fields_by_dataset") or {}).items():
                for field_row in _payload_results(field_payload):
                    spec = _field_spec_from_discovery(
                        field_row,
                        dataset_id=str(dataset_id or ""),
                        region=region,
                        universe=universe,
                        delay=delay,
                    )
                    if spec:
                        fields[spec.id] = asdict(spec)

            account_payload["combos"][key] = {
                "region": region,
                "universe": universe,
                "delay": delay,
                "datasets": datasets,
                "fields": dict(sorted(fields.items())),
            }

        return cls(payload)

    def to_payload(self) -> dict[str, Any]:
        return self.payload

    def write(self, path: Path | str) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        return out

    def summary(self) -> dict[str, Any]:
        combos: list[dict[str, Any]] = []
        accounts = self.payload.get("accounts") or {}
        for account, account_payload in accounts.items():
            for key, combo in (account_payload.get("combos") or {}).items():
                fields = combo.get("fields") or {}
                type_counts = Counter(str((spec or {}).get("type") or "UNKNOWN").upper() for spec in fields.values())
                source_counts = Counter(str((spec or {}).get("source") or "unknown") for spec in fields.values())
                combos.append({
                    "account": account,
                    "combo_key": key,
                    "region": combo.get("region"),
                    "universe": combo.get("universe"),
                    "delay": combo.get("delay"),
                    "field_count": len(fields),
                    "dataset_count": len(combo.get("datasets") or {}),
                    "field_type_counts": dict(sorted(type_counts.items())),
                    "field_source_counts": dict(sorted(source_counts.items())),
                })
        return {
            "schema_version": self.schema_version,
            "kind": self.payload.get("kind"),
            "created_at": self.payload.get("created_at"),
            "combo_count": len(combos),
            "operator_count": len(self.operators),
            "forbidden_fields": sorted(self.forbidden_fields),
            "forbidden_operators": sorted(self.forbidden_operators),
            "combos": sorted(combos, key=lambda row: (str(row["account"]), str(row["combo_key"]))),
        }

    def validate_candidate(
        self,
        candidate: dict[str, Any],
        *,
        account: str = "primary",
        region: str = "USA",
        universe: str = "TOP3000",
        delay: int = 1,
        strict: bool = True,
    ) -> WQCandidateValidationResult:
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        if not isinstance(candidate, dict):
            errors.append({"code": "illegal_candidate_schema", "message": "candidate must be an object"})
            return _result(errors=errors, warnings=warnings)

        expression = candidate.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            errors.append({"code": "illegal_candidate_schema", "message": "candidate.expression is required"})
            return _result(errors=errors, warnings=warnings)

        settings = candidate.get("simulation_settings")
        if settings is not None and not isinstance(settings, dict):
            errors.append({"code": "illegal_candidate_schema", "message": "simulation_settings must be an object"})
        elif isinstance(settings, dict):
            bad_keys = sorted(str(key) for key in settings if str(key) not in ALLOWED_SIMULATION_SETTING_KEYS)
            if bad_keys:
                errors.append({
                    "code": "illegal_candidate_schema",
                    "message": "simulation_settings contains unsupported keys",
                    "keys": bad_keys,
                })
            region = str(settings.get("region") or region).upper()
            universe = str(settings.get("universe") or universe).upper()
            delay = _safe_int(settings.get("delay"), default=delay)

        expression_result = self.validate_expression(
            expression,
            account=account,
            region=region,
            universe=universe,
            delay=delay,
            strict=strict,
        )
        return WQCandidateValidationResult(
            ok=not errors and expression_result.ok,
            errors=tuple([*errors, *expression_result.errors]),
            warnings=tuple([*warnings, *expression_result.warnings]),
            fields=expression_result.fields,
            operators=expression_result.operators,
            field_specs=expression_result.field_specs,
            normalized_expression=expression_result.normalized_expression,
        )

    def validate_expression(
        self,
        expression: str,
        *,
        account: str = "primary",
        region: str = "USA",
        universe: str = "TOP3000",
        delay: int = 1,
        strict: bool = True,
    ) -> WQCandidateValidationResult:
        normalized = " ".join(str(expression or "").strip().split())
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        if not normalized:
            errors.append({"code": "illegal_expression", "message": "empty expression"})
            return _result(errors=errors, warnings=warnings)

        components = _components(normalized)
        operators = tuple(sorted(_operator_id(value) for value in components["operators"] if _operator_id(value)))
        fields = tuple(sorted(_field_id(value) for value in components["fields"] if _field_id(value)))

        for operator in operators:
            spec = self.operators.get(operator)
            if operator in self.forbidden_operators or (spec and bool(spec.get("forbidden"))):
                errors.append({"code": "illegal_operator", "operator": operator, "message": "operator is forbidden"})
            elif not spec:
                errors.append({"code": "illegal_operator", "operator": operator, "message": "operator is not in WQ operator registry"})

        if not any(error.get("code") == "illegal_operator" for error in errors):
            try:
                parse_expression(normalized, mode="wq")
            except Exception as exc:
                errors.append({"code": "illegal_expression", "message": str(exc)})

        combo_fields = self._combo_fields(account=account, region=region, universe=universe, delay=delay)
        field_specs: list[dict[str, Any]] = []
        if combo_fields is None:
            message = f"no legal input combo for {account}:{combo_key(region, universe, delay)}"
            if strict:
                errors.append({"code": "unavailable_dataset_field", "message": message})
            else:
                warnings.append({"code": "missing_registry_combo", "message": message})
            combo_fields = {}

        for field in fields:
            if _is_dynamic_adv(field):
                field_specs.append(asdict(_static_field(field, "MATRIX", "core", region, universe, delay, source="static_core")))
                continue
            spec = combo_fields.get(field)
            if field in self.forbidden_fields:
                errors.append({"code": "illegal_field", "field": field, "message": "field is forbidden"})
                if spec:
                    field_specs.append(spec)
                continue
            if spec is None:
                if field in self._global_fields:
                    code = "unavailable_dataset_field"
                    message = "field exists in registry but not for this account/region/universe/delay"
                else:
                    code = "illegal_field"
                    message = "field is not in legal input registry"
                if strict:
                    errors.append({"code": code, "field": field, "message": message})
                else:
                    warnings.append({"code": code, "field": field, "message": message})
                continue
            if str(spec.get("source") or "") == "static_fallback":
                message = "field is a local compatibility fallback and was not discovered as legal for this WQ combo"
                if strict:
                    errors.append({"code": "unavailable_dataset_field", "field": field, "message": message})
                else:
                    warnings.append({"code": "static_fallback_field", "field": field, "message": message})
            field_specs.append(spec)

        vector_fields = [spec for spec in field_specs if str(spec.get("type") or "").upper() == "VECTOR"]
        has_vector_operator = any(operator in VECTOR_OPERATORS for operator in operators)
        if vector_fields and not has_vector_operator:
            errors.append({
                "code": "illegal_field_type",
                "fields": sorted(str(spec.get("id")) for spec in vector_fields),
                "message": "VECTOR fields must be reduced with vec_* or vector neutralization operators",
            })
        if has_vector_operator and not vector_fields:
            errors.append({
                "code": "illegal_field_type",
                "operators": sorted(operator for operator in operators if operator in VECTOR_OPERATORS),
                "message": "vector operators require at least one VECTOR field",
            })

        return WQCandidateValidationResult(
            ok=not errors,
            errors=tuple(errors),
            warnings=tuple(warnings),
            fields=fields,
            operators=operators,
            field_specs=tuple(sorted(field_specs, key=lambda spec: str(spec.get("id") or ""))),
            normalized_expression=normalize_expression(normalized),
        )

    def _combo_fields(
        self,
        *,
        account: str,
        region: str,
        universe: str,
        delay: int,
    ) -> dict[str, dict[str, Any]] | None:
        accounts = self.payload.get("accounts") or {}
        account_payload = accounts.get(account) or accounts.get("primary")
        if not isinstance(account_payload, dict):
            return None
        combo = (account_payload.get("combos") or {}).get(combo_key(region, universe, delay))
        if not isinstance(combo, dict):
            return None
        fields = combo.get("fields") or {}
        return {_field_id(name): dict(spec or {}) for name, spec in fields.items() if _field_id(name)}

    def _build_global_field_index(self) -> set[str]:
        fields: set[str] = set()
        for account_payload in (self.payload.get("accounts") or {}).values():
            if not isinstance(account_payload, dict):
                continue
            for combo in (account_payload.get("combos") or {}).values():
                if not isinstance(combo, dict):
                    continue
                fields.update(_field_id(name) for name in (combo.get("fields") or {}) if _field_id(name))
        return fields


def combo_key(region: str, universe: str, delay: int) -> str:
    return f"{str(region or '').upper()}|{str(universe or '').upper()}|{int(delay)}"


def load_legal_input_registry(path: Path | str) -> WQLegalInputRegistry:
    return WQLegalInputRegistry.from_file(path)


def load_optional_legal_input_registry(path: Path | str | None) -> WQLegalInputRegistry | None:
    if not path:
        return None
    registry_path = Path(path)
    if not registry_path.is_file():
        raise FileNotFoundError(f"legal input registry not found: {registry_path}")
    return WQLegalInputRegistry.from_file(registry_path)


def _base_payload(*, source: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "wq_legal_input_registry",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source or {"sanitized": True},
        "forbidden_fields": sorted(DEFAULT_FORBIDDEN_FIELDS),
        "forbidden_operators": sorted(DEFAULT_FORBIDDEN_OPERATORS),
        "operators": {
            name: asdict(WQOperatorSpec(
                name=name,
                remote_only=name in _WQ_REMOTE_ONLY_OPS,
                forbidden=name in DEFAULT_FORBIDDEN_OPERATORS,
            ))
            for name in sorted({_operator_id(name) for name in _WQ_OPERATORS if _operator_id(name)})
        },
        "accounts": {},
    }


def _dataset_index(payload: Any) -> dict[str, dict[str, Any]]:
    datasets: dict[str, dict[str, Any]] = {}
    for row in _payload_results(payload):
        if not isinstance(row, dict):
            continue
        dataset_id = str(row.get("id") or row.get("datasetId") or row.get("name") or "").strip()
        if not dataset_id:
            continue
        category = row.get("category") if isinstance(row.get("category"), dict) else {}
        subcategory = row.get("subcategory") if isinstance(row.get("subcategory"), dict) else {}
        datasets[dataset_id] = {
            "id": dataset_id,
            "name": str(row.get("name") or ""),
            "category": str(category.get("id") or row.get("category") or ""),
            "subcategory": str(subcategory.get("id") or row.get("subcategory") or ""),
        }
    return dict(sorted(datasets.items()))


def _payload_results(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        value = payload.get("results", payload.get("data", []))
        return value if isinstance(value, list) else []
    if isinstance(payload, list):
        return payload
    return []


def _field_spec_from_discovery(
    value: Any,
    *,
    dataset_id: str,
    region: str,
    universe: str,
    delay: int,
) -> WQFieldSpec | None:
    row = _coerce_field_row(value)
    if not row:
        return None
    field = _field_id(row.get("id") or row.get("field") or row.get("name"))
    if not field:
        return None
    dataset = row.get("dataset") if isinstance(row.get("dataset"), dict) else {}
    category = row.get("category") if isinstance(row.get("category"), dict) else {}
    subcategory = row.get("subcategory") if isinstance(row.get("subcategory"), dict) else {}
    dataset_value = str(dataset.get("id") or row.get("dataset_id") or dataset_id or "")
    category_value = str(category.get("id") or row.get("category") or "")
    subcategory_value = str(subcategory.get("id") or row.get("subcategory") or "")
    return WQFieldSpec(
        id=field,
        type=str(row.get("type") or "UNKNOWN").upper(),
        domain=category_value or dataset_value or "discovery",
        dataset_id=dataset_value,
        category=category_value,
        subcategory=subcategory_value,
        region=str(row.get("region") or region).upper(),
        universe=str(row.get("universe") or universe).upper(),
        delay=_safe_int(row.get("delay"), default=delay),
        coverage=_safe_float(row.get("coverage"), row.get("dateCoverage")),
        source="discovery",
    )


def _coerce_field_row(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    if text.startswith("@{") and text.endswith("}"):
        text = text[2:-1]
    out: dict[str, Any] = {}
    for item in re.split(r";\s*", text):
        if "=" not in item:
            continue
        key, raw = item.split("=", 1)
        key = key.strip()
        raw = raw.strip()
        if key:
            out[key] = raw
    return out


def _static_field_map(*, region: str, universe: str, delay: int) -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    for name in sorted(CORE_FIELDS | set(_WQ_SPECIAL_VARS)):
        spec = _static_field(name, "MATRIX", "core", region, universe, delay, source="static_core")
        fields[spec.id] = asdict(spec)
    for name in sorted(DEFAULT_GROUP_FIELDS):
        spec = _static_field(name, "GROUP", "group", region, universe, delay, source="static_group")
        fields[spec.id] = asdict(spec)
    for name in sorted(set(_WQ_EXTENDED_FIELDS) | LOCAL_COMPATIBILITY_FIELDS):
        spec = _static_field(name, "MATRIX", "compatibility", region, universe, delay, source="static_fallback")
        fields[spec.id] = asdict(spec)
    return fields


def _static_field(
    name: str,
    field_type: str,
    domain: str,
    region: str,
    universe: str,
    delay: int,
    *,
    source: str,
) -> WQFieldSpec:
    return WQFieldSpec(
        id=_field_id(name),
        type=field_type,
        domain=domain,
        region=str(region or "").upper(),
        universe=str(universe or "").upper(),
        delay=int(delay),
        source=source,
    )


def _components(expression: str) -> dict[str, set[str]]:
    try:
        parts = extract_components(expression or "")
    except Exception:
        return {"fields": set(), "operators": set()}
    return {
        "fields": {_field_id(value) for value in parts.get("fields", set()) if _field_id(value)},
        "operators": {_operator_id(value) for value in parts.get("operators", set()) if _operator_id(value)},
    }


def _result(
    *,
    errors: list[dict[str, Any]] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> WQCandidateValidationResult:
    return WQCandidateValidationResult(
        ok=not errors,
        errors=tuple(errors or []),
        warnings=tuple(warnings or []),
        fields=(),
        operators=(),
        field_specs=(),
        normalized_expression="",
    )


def _operator_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _ALIAS_NORMALIZE.get(text, _ALIAS_NORMALIZE.get(text.lower(), text))
    return text.lower()


def _field_id(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_dynamic_adv(field: str) -> bool:
    return bool(re.fullmatch(r"adv\d+", field or ""))


def _safe_int(value: Any, *, default: int) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(value)
    except Exception:
        return int(default)
