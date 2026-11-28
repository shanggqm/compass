"""Lossless schema annotation compaction; business input adapters live with their tools."""

from copy import deepcopy
from typing import Any


def compact_json_schema(schema: dict) -> dict:
    """Inline local refs and remove generator annotations without dropping validation rules."""
    definitions = schema.get("$defs", {})

    def visit(value, seen=frozenset()) -> Any:
        if isinstance(value, list):
            return [visit(v, seen) for v in value]
        if not isinstance(value, dict):
            return value
        value = deepcopy(value)
        ref = value.pop("$ref", None)
        if ref:
            name = ref.removeprefix("#/$defs/")
            if not ref.startswith("#/$defs/") or name not in definitions or name in seen:
                raise ValueError("unsupported or recursive schema reference")
            value = {**visit(definitions[name], seen | {name}), **value}
        branches = value.get("anyOf")
        if isinstance(branches, list):
            non_null = [v for v in branches if v != {"type": "null"}]
            if len(non_null) == 1 and len(non_null) != len(branches):
                value.pop("anyOf")
                return visit({**non_null[0], **value}, seen)
        result = {}
        for key, child in value.items():
            if key in {"title", "$defs", "discriminator"} or key == "default" and child is None:
                continue
            if key in {"properties", "patternProperties"}:
                result[key] = {name: visit(v, seen) for name, v in child.items()}
            else:
                result[key] = visit(child, seen)
        return result

    return visit(schema)
