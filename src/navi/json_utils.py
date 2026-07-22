from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any


def json_object(value: str | bytes | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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

    const_value = schema.get("const", _MISSING)
    if const_value is not _MISSING and value != const_value:
        errors.append(f"{path} must equal {const_value!r}")

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for subschema in all_of:
            if isinstance(subschema, dict):
                errors.extend(json_schema_errors(value, subschema, path=path))

    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and any_of:
        any_matches = [
            not json_schema_errors(value, subschema, path=path)
            for subschema in any_of
            if isinstance(subschema, dict)
        ]
        if any_matches and not any(any_matches):
            errors.append(f"{path} must match at least one declared schema")

    one_of = schema.get("oneOf")
    if isinstance(one_of, list) and one_of:
        match_count = sum(
            not json_schema_errors(value, subschema, path=path)
            for subschema in one_of
            if isinstance(subschema, dict)
        )
        if match_count != 1:
            errors.append(f"{path} must match exactly one declared schema")

    conditional = schema.get("if")
    if isinstance(conditional, dict):
        matched = not json_schema_errors(value, conditional, path=path)
        branch = schema.get("then" if matched else "else")
        if isinstance(branch, dict):
            errors.extend(json_schema_errors(value, branch, path=path))
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
        elif isinstance(additional, dict) and isinstance(properties, dict):
            for key, item in value.items():
                if key not in properties:
                    errors.extend(json_schema_errors(item, additional, path=f"{path}.{key}"))

        min_properties = schema.get("minProperties")
        if isinstance(min_properties, int) and len(value) < min_properties:
            errors.append(f"{path} must contain at least {min_properties} properties")
        max_properties = schema.get("maxProperties")
        if isinstance(max_properties, int) and len(value) > max_properties:
            errors.append(f"{path} must contain at most {max_properties} properties")

    array_like = "array" in expected_types or (not expected_types and "items" in schema)
    if array_like and isinstance(value, list):
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for index, item in enumerate(value):
                errors.extend(json_schema_errors(item, items_schema, path=f"{path}[{index}]"))
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"{path} must contain at least {min_items} items")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            errors.append(f"{path} must contain at most {max_items} items")
        if schema.get("uniqueItems") is True:
            serialized = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value]
            if len(serialized) != len(set(serialized)):
                errors.append(f"{path} items must be unique")

    if "string" in expected_types and isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            errors.append(f"{path} length must be at least {min_length}")
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            errors.append(f"{path} length must be at most {max_length}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            errors.append(f"{path} must match pattern {pattern!r}")

    if any(item in expected_types for item in ("integer", "number")) and isinstance(
        value, (int, float)
    ) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path} must be greater than or equal to {minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path} must be less than or equal to {maximum}")
        exclusive_minimum = schema.get("exclusiveMinimum")
        if isinstance(exclusive_minimum, (int, float)) and value <= exclusive_minimum:
            errors.append(f"{path} must be greater than {exclusive_minimum}")
        exclusive_maximum = schema.get("exclusiveMaximum")
        if isinstance(exclusive_maximum, (int, float)) and value >= exclusive_maximum:
            errors.append(f"{path} must be less than {exclusive_maximum}")

    return errors


def normalize_json_schema(schema: dict[str, Any], *, output: bool = False) -> dict[str, Any]:
    """Return a closed, self-describing copy of a capability JSON schema.

    Objects with declared properties reject undeclared public fields. Required
    output facts remain an explicit part of each capability contract: inferring
    one from property order would turn declaration order into runtime policy.
    """

    normalized = deepcopy(schema) if isinstance(schema, dict) else {}

    def visit(node: dict[str, Any], *, root: bool = False) -> None:
        properties = node.get("properties")
        if isinstance(properties, dict):
            node.setdefault("additionalProperties", False)
            for child in properties.values():
                if isinstance(child, dict):
                    visit(child)
        items = node.get("items")
        if isinstance(items, dict):
            visit(items)
        additional = node.get("additionalProperties")
        if isinstance(additional, dict):
            visit(additional)
        for keyword in ("allOf", "anyOf", "oneOf"):
            branches = node.get(keyword)
            if isinstance(branches, list):
                for branch in branches:
                    if isinstance(branch, dict):
                        visit(branch)
        for keyword in ("if", "then", "else"):
            branch = node.get(keyword)
            if isinstance(branch, dict):
                visit(branch)
    visit(normalized, root=True)
    return normalized


_MISSING = object()


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
