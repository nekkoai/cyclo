from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


SCHEMA_VERSION = 1
MANIFEST_PATH = Path(__file__).resolve().parent / "gateway_context" / "safe-model-fields.json"
_LIST_KEYS = {
    "costFields": "cost_fields",
    "inputTypes": "input_types",
    "compatBooleanFields": "compat_boolean_fields",
    "maxTokensFields": "max_tokens_fields",
    "thinkingFormats": "thinking_formats",
    "thinkingLevels": "thinking_levels",
    "cacheControlFormats": "cache_control_formats",
}


@dataclass(frozen=True)
class SafeModelFields:
    schema_version: int
    cost_fields: frozenset[str]
    input_types: frozenset[str]
    compat_boolean_fields: frozenset[str]
    max_tokens_fields: frozenset[str]
    thinking_formats: frozenset[str]
    thinking_levels: frozenset[str]
    cache_control_formats: frozenset[str]


def load_safe_model_fields(path: Path = MANIFEST_PATH) -> SafeModelFields:
    """Load the shared model-metadata allowlist, failing closed on drift."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load safe model fields manifest {path}: {exc}") from exc

    if not isinstance(document, dict):
        raise RuntimeError(f"safe model fields manifest {path} must be a JSON object")

    expected_keys = {"schemaVersion", *_LIST_KEYS}
    actual_keys = set(document)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        unknown = sorted(actual_keys - expected_keys)
        raise RuntimeError(
            f"safe model fields manifest {path} has invalid keys; "
            f"missing={missing}, unknown={unknown}"
        )

    version = document["schemaVersion"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise RuntimeError(
            f"safe model fields manifest {path} requires schemaVersion {SCHEMA_VERSION}"
        )

    values: dict[str, object] = {"schema_version": version}
    for json_key, attribute in _LIST_KEYS.items():
        items = document[json_key]
        if not isinstance(items, list) or not items:
            raise RuntimeError(
                f"safe model fields manifest {path} field {json_key} "
                "must be a non-empty array"
            )
        if any(not isinstance(item, str) or not item for item in items):
            raise RuntimeError(
                f"safe model fields manifest {path} field {json_key} "
                "must contain only non-empty strings"
            )
        if len(items) != len(set(items)):
            raise RuntimeError(
                f"safe model fields manifest {path} field {json_key} "
                "must not contain duplicates"
            )
        values[attribute] = frozenset(items)

    return SafeModelFields(**values)  # type: ignore[arg-type]


SAFE_MODEL_FIELDS = load_safe_model_fields()
