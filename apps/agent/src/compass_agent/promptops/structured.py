"""Validate editable asset content against code-owned execution contracts."""

import copy
import hashlib
import json
from functools import lru_cache

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from compass_agent.promptops.errors import PromptError
from compass_agent.promptops.layout import MEMBERS, dumps, extract


def parse(body: str) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"重复字段 {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            body,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("无效 JSON 数值")),
        )
        if not isinstance(value, dict):
            raise ValueError("JSON 资产须为对象")
        return value
    except (ValueError, RecursionError) as exc:
        raise PromptError("invalid_asset", f"JSON 资产格式无效：{exc}") from exc


def schema_structure(schema):
    """Remove description annotations without dropping properties named description."""
    if not isinstance(schema, dict):
        return schema
    maps = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
    singles = {
        "items",
        "additionalProperties",
        "additionalItems",
        "contains",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
    lists = {"allOf", "anyOf", "oneOf", "prefixItems"}
    result = {}
    for key, value in schema.items():
        if key == "description":
            continue
        if key in maps and isinstance(value, dict):
            value = {name: schema_structure(child) for name, child in value.items()}
        elif key in singles:
            value = schema_structure(value)
        elif key in lists and isinstance(value, list):
            value = [schema_structure(child) for child in value]
        result[key] = value
    return result


@lru_cache(maxsize=64)
def code_definition(name: str) -> dict:
    from compass_agent.prompt_assets import raw_factory_text, use_prompts
    from compass_agent.tools.catalog import TOOLS
    from compass_agent.tools.catalog_contracts import CatalogContext

    if name == "report_to_parent":
        from compass_agent.goal.report_schema import SCHEMA

        value = parse(raw_factory_text("support/subagent/report-to-parent.json"))
        value["function"]["parameters"] = SCHEMA
        value.pop("examples", None)
        return value
    spec = next((spec for spec in TOOLS if spec.name == name), None)
    if spec is None:
        raise PromptError("unknown_tool", "工具未在当前代码中注册。")
    context = CatalogContext(enabled=frozenset(), database=True, sandbox=True, workflow=True)
    with use_prompts(raw_factory_text):
        definition = spec.definition(context)
    if definition is None:
        raise PromptError("unknown_tool", "当前代码未提供工具声明。")
    return definition.model_dump(mode="json", exclude_none=True)


def validate_tool(path: str, document: dict, *, expected=None, historical=False) -> None:
    from compass_agent.tools.contracts import ToolRejected
    from compass_agent.tools.registry import parse_arguments, validate_schema

    name = path.removeprefix("tools/").removesuffix(".json")
    expected = expected or code_definition(name)
    candidate = copy.deepcopy(document)
    examples = candidate.pop("examples", [])
    try:
        try:
            validate_schema(candidate["function"]["parameters"])
        except ToolRejected as exc:
            raise ValueError("参数说明超出 Runtime 支持的 Schema 大小或结构限制。") from exc
        if not historical:

            def descriptions(schema):
                if not isinstance(schema, dict):
                    return
                for parameter in schema.get("properties", {}).values():
                    if (
                        not isinstance(parameter.get("description"), str)
                        or not parameter["description"].strip()
                    ):
                        raise ValueError("每个参数须有非空说明")
                    descriptions(parameter)
                descriptions(schema.get("items"))

            descriptions(candidate["function"]["parameters"])
        description = candidate["function"].pop("description")
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > (2000 if historical else 1200)
        ):
            raise ValueError("工具描述须为 1–1200 个字符")
        candidate["function"]["parameters"] = schema_structure(candidate["function"]["parameters"])
        baseline = copy.deepcopy(expected)
        baseline["function"].pop("description", None)
        # Pinned Runs can resume with declarations predating these optional fields.
        if name == "discover_semantic_candidates":
            for optional in ("revise_metric", "reuse_reason"):
                if optional not in candidate["function"]["parameters"]["properties"]:
                    baseline["function"]["parameters"]["properties"].pop(optional, None)
        baseline["function"]["parameters"] = schema_structure(baseline["function"]["parameters"])
        if json.dumps(candidate, sort_keys=True, allow_nan=False) != json.dumps(
            baseline, sort_keys=True, allow_nan=False
        ):
            raise ValueError("工具名称和参数 Schema 由代码维护，后台只允许编辑描述与示例")
        if not isinstance(examples, list) or len(examples) > (10 if historical else 3):
            raise ValueError("调用示例须为数组，最多 3 条")
        validator = Draft202012Validator(
            expected["function"]["parameters"], format_checker=Draft202012Validator.FORMAT_CHECKER
        )
        for example in examples:
            if (
                not isinstance(example, dict)
                or set(example) != {"description", "arguments"}
                or not isinstance(example["description"], str)
                or len(example["description"]) > 1000
                or not validator.is_valid(example["arguments"])
            ):
                raise ValueError("每条示例需包含 description 和符合代码参数 Schema 的 arguments")
            try:
                parse_arguments(
                    json.dumps(example["arguments"], ensure_ascii=False, allow_nan=False)
                )
            except ToolRejected as exc:
                raise ValueError("示例参数超出 Runtime 的 JSON 大小或嵌套限制。") from exc
        if len(model_description(document)) > (2000 if historical else 1200):
            raise ValueError("工具描述和示例合计超过模型工具声明的 1200 字符限制。")
    except (KeyError, TypeError, ValueError, RecursionError, SchemaError) as exc:
        raise PromptError("tool_contract_readonly", str(exc)) from exc


def contract_digest(path: str) -> str | None:
    if path == "support/subagent/report-to-parent.json":
        path = "tools/report_to_parent.json"
    if not path.startswith("tools/") or not path.endswith(".json"):
        return None
    value = copy.deepcopy(code_definition(path[6:-5]))
    value["function"].pop("description", None)
    value["function"]["parameters"] = schema_structure(value["function"]["parameters"])
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def validate_document(path: str, body: str) -> dict:
    document = parse(body)
    if path.startswith("tools/"):
        validate_tool(path, document)
    elif path == "support/subagent/report-to-parent.json":
        validate_tool("tools/report_to_parent.json", document)
    elif path == "support/routing/scenes.json":
        from compass_agent.routing.dictionary import catalog

        scenes = document.get("scenes")
        expected = catalog()[1]
        if (
            set(document) != {"scenes"}
            or not isinstance(scenes, dict)
            or scenes.keys() != expected.keys()
        ):
            raise PromptError("invalid_scenes", "须保留代码登记的完整场景清单及场景 ID。")
        for key, row in scenes.items():
            if not isinstance(row, dict):
                raise PromptError("invalid_scenes", "场景须为对象。")
            fixed = {k: v for k, v in row.items() if k not in {"description", "examples", "sop"}}
            examples = row.get("examples")
            if (
                fixed != expected[key]
                or (
                    "sop" in row
                    and (not isinstance(row["sop"], str) or not 0 < len(row["sop"].strip()) <= 4000)
                )
                or not isinstance(row.get("description"), str)
                or not row["description"].strip()
                or not isinstance(examples, list)
                or len(examples) > 10
                or not all(isinstance(v, str) and 0 < len(v) <= 2000 for v in examples)
            ):
                raise PromptError("invalid_scenes", f"{key} 可编辑识别描述、示例及场景 SOP。")
    else:
        from compass_agent.prompt_assets import factory_text

        members = [p for p, member in MEMBERS.items() if member["path"] == path]
        if members:
            expected = parse(factory_text(path))

            def shape(value):
                if isinstance(value, dict):
                    return {key: shape(child) for key, child in value.items()}
                return "text" if isinstance(value, str) else "invalid"

            if shape(document) != shape(expected):
                raise PromptError("invalid_asset", "须保留已登记的字段，内容使用字符串。")
        elif path.endswith(".json"):
            raise PromptError("unregistered_asset", "结构化资产须先在代码中登记。")
    return document


def validate_members(path: str, body: str, allowed: dict) -> None:
    from compass_agent.promptops.starter_catalog import validate_starter
    from compass_agent.promptops.template import validate_template

    validate_document(path, body)
    for legacy, member in MEMBERS.items():
        if member["path"] == path:
            text = extract(legacy, body)
            validate_template(legacy, text, allowed)
            validate_starter(legacy, text)


def refresh_tool_schema(path: str, body: str) -> str:
    document = parse(body)
    definition = copy.deepcopy(code_definition(path[6:-5]))
    definition["function"]["description"] = document["function"]["description"]

    def descriptions(code, online):
        if not isinstance(code, dict) or not isinstance(online, dict):
            return
        if isinstance(online.get("description"), str):
            code["description"] = online["description"]
        for key in ("properties", "$defs", "definitions"):
            for name, child in code.get(key, {}).items():
                descriptions(child, online.get(key, {}).get(name))
        if "items" in code:
            descriptions(code["items"], online.get("items"))

    descriptions(definition["function"]["parameters"], document["function"]["parameters"])
    definition["examples"] = document.get("examples", [])
    return dumps(definition)


def enrich_definition(definition):
    from compass_agent.prompt_assets import prompt_text
    from compass_agent.schemas import ToolDefinition

    path = f"tools/{definition.function.name}.json"
    from compass_agent.tools.catalog import TOOLS

    if definition.function.name not in {spec.name for spec in TOOLS}:
        return definition
    document = parse(prompt_text(path))
    from compass_agent.execution_context import current_execution

    journal = current_execution.get()
    historical = (
        journal is not None
        and (getattr(journal, "context_binding", None) or {}).get("answerProtocol", 1) < 3
    )
    expected = definition.model_dump(mode="json", exclude_none=True)
    if historical:
        # Frozen declarations are restored by RunToolBindings. A new-format bundled
        # prompt must never replace the internal contract of an old protocol.
        if schema_structure(document["function"]["parameters"]) != schema_structure(
            expected["function"]["parameters"]
        ):
            return definition
        validate_tool(path, document, expected=expected, historical=True)
    else:
        validate_tool(path, document)
    value = {key: child for key, child in document.items() if key != "examples"}
    value = copy.deepcopy(value)
    value["function"]["description"] = model_description(document)
    return ToolDefinition.model_validate(value)


def model_description(document):
    value = document["function"]["description"]
    if document.get("examples"):
        value += "\n调用示例：\n" + dumps(document["examples"])
    return value
