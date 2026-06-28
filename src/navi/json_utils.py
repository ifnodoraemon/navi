from __future__ import annotations

import json
from typing import Any


def json_object(value: str | bytes | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def json_list(value: str | bytes | None) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


JSON_SCHEMA_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: (isinstance(value, (int, float)) and not isinstance(value, bool)),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def json_schema_errors(value: Any, schema: dict[str, Any], *, path: str = "$") -> list[str]:
    if not isinstance(schema, dict):
        return []
    errors: list[str] = []
    expected_types = _schema_types(schema)
    if expected_types:
        if not any(_json_type_matches(value, item) for item in expected_types):
            errors.append(f"{path} expected {'|'.join(expected_types)}")
            return errors

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and value not in enum_values:
        errors.append(f"{path} must be one of {enum_values}")

    object_like = "object" in expected_types or (
        not expected_types and ("properties" in schema or "required" in schema)
    )
    if object_like and isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    errors.append(f"{path}.{key} is required")
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, subschema in properties.items():
                if key in value and isinstance(subschema, dict):
                    errors.extend(json_schema_errors(value[key], subschema, path=f"{path}.{key}"))
        additional = schema.get("additionalProperties")
        if additional is False and isinstance(properties, dict):
            allowed = set(properties)
            for key in value:
                if key not in allowed:
                    errors.append(f"{path}.{key} is not declared")

    array_like = "array" in expected_types or (not expected_types and "items" in schema)
    if array_like and isinstance(value, list):
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for index, item in enumerate(value):
                errors.extend(json_schema_errors(item, items_schema, path=f"{path}[{index}]"))

    if "string" in expected_types and isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            errors.append(f"{path} length must be at least {min_length}")

    return errors


def _schema_types(schema: dict[str, Any]) -> tuple[str, ...]:
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        return (raw_type,)
    if isinstance(raw_type, list):
        return tuple(item for item in raw_type if isinstance(item, str))
    return ()


def _json_type_matches(value: Any, expected_type: str) -> bool:
    check = JSON_SCHEMA_TYPE_CHECKS.get(expected_type)
    return bool(check and check(value))

