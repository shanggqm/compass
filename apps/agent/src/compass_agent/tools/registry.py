"""Immutable per-Run catalog; declaration and dispatch use the same registration."""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

from compass_agent.schemas import TOOL_CATALOG_LIMIT, TOOL_REGISTRY_POOL_LIMIT, ToolDefinition
from compass_agent.tools.contracts import (
    McpBinding,
    RegisteredTool,
    SandboxBinding,
    ToolRejected,
    json_bytes,
)

TOOL_DECLARATION_BYTES_LIMIT = 64 * 1024


def parse_arguments(raw: str) -> object:
    if len(raw.encode()) > 32 * 1024:
        raise ToolRejected("invalid_parameters")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate parameter")
            value[key] = item
        return value

    try:
        value = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite parameter")),
        )
        check_depth(value)
        if not isinstance(value, dict):
            raise ValueError("arguments must be an object")
        return value
    except (ValueError, RecursionError, TypeError) as error:
        raise ToolRejected("invalid_parameters") from error


def check_depth(value: object, *, maximum: int = 20, maximum_nodes: int | None = 8192) -> None:
    pending = [(value, 0)]
    count = 0
    while pending:
        current, depth = pending.pop()
        count += 1
        if depth > maximum or (maximum_nodes is not None and count > maximum_nodes):
            raise ToolRejected("structure_limit")
        if isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend((item, depth + 1) for item in current)


def validate_schema(schema: dict) -> None:
    check_depth(schema)
    if len(json_bytes(schema)) > 32 * 1024 or schema.get("type") != "object":
        raise ToolRejected("unsupported_tool_schema")
    pending: list[Any] = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key in ("$ref", "$dynamicRef"):
                if key in value and (
                    not isinstance(value[key], str) or not value[key].startswith("#")
                ):
                    raise ToolRejected("unsupported_tool_schema")
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as error:
        raise ToolRejected("unsupported_tool_schema") from error


def bounded_payload(value: object) -> object:
    check_depth(value)
    if len(json_bytes(value)) > 64 * 1024:
        raise ToolRejected("result_limit")
    return value


def has_catalog_room(definitions: Iterable[ToolDefinition]) -> bool:
    values = list(definitions)
    return (
        len(values) <= TOOL_CATALOG_LIMIT
        and len(json_bytes([d.model_dump() for d in values])) <= TOOL_DECLARATION_BYTES_LIMIT
    )


def validate_catalog(definitions: Iterable[ToolDefinition]) -> None:
    if not has_catalog_room(definitions):
        raise ToolRejected("tool_catalog_limit")


class ToolRegistry:
    def __init__(self, entries: Iterable[RegisteredTool]):
        tools: dict[str, RegisteredTool] = {}
        for entry in entries:
            if entry.name in tools:
                raise ToolRejected("tool_name_conflict")
            if entry.policy.approval == "required" and (
                entry.prepare_proposal is None and entry.prepare_approval is None
            ):
                raise ToolRejected("invalid_tool_policy")
            if entry.policy.approval == "none" and entry.binding is None:
                raise ToolRejected("invalid_tool_binding")
            if not 0 < entry.timeout_s <= 20:
                raise ToolRejected("invalid_tool_timeout")
            definition = entry.definition
            if definition:
                if definition.function.name != entry.name:
                    raise ToolRejected("tool_name_conflict")
                validate_schema(definition.function.parameters)
                entry = replace(entry, definition=definition.model_copy(deep=True))
            tools[entry.name] = entry
        self._entries = MappingProxyType(tools)
        if len(tools) > TOOL_REGISTRY_POOL_LIMIT:
            raise ToolRejected("tool_registry_pool_limit")
        self.catalog_id = hashlib.sha256(
            json_bytes(
                [
                    {
                        "name": t.name,
                        "policy": [t.policy.effect, t.policy.approval],
                        "binding": self._binding_key(t),
                        "schema": t.definition.model_dump() if t.definition else None,
                    }
                    for t in tools.values()
                ]
            )
        ).hexdigest()

    @staticmethod
    def _binding_key(tool):
        binding = tool.binding
        if isinstance(binding, McpBinding):
            return [
                binding.environment,
                str(binding.connection_id),
                binding.revision,
                binding.remote_name,
                binding.schema_hash,
            ]
        if isinstance(binding, SandboxBinding):
            return [binding.environment, binding.runtime_profile]
        return [binding.environment] if binding else None

    @staticmethod
    def _copy(tool):
        return (
            replace(tool, definition=tool.definition.model_copy(deep=True))
            if tool.definition
            else tool
        )

    @property
    def entries(self):
        return MappingProxyType({name: self._copy(tool) for name, tool in self._entries.items()})

    def definitions(self, names: frozenset[str] | None = None) -> list[ToolDefinition]:
        return [
            t.definition.model_copy(deep=True)
            for t in self._entries.values()
            if t.definition and (names is None or t.name in names)
        ]

    def resolve(self, name: str) -> RegisteredTool:
        try:
            return self._copy(self._entries[name])
        except KeyError as error:
            raise ToolRejected("unknown_tool") from error


# Compatibility names remain for callers that validate a model request.
validate_visible_catalog = validate_catalog
has_visible_room = has_catalog_room
