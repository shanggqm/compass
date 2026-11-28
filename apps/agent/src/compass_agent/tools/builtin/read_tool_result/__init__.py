"""Read an authorized page from a previously accepted tool result."""

from dataclasses import replace
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field, model_validator

from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import ToolDefinition, ToolFunction
from compass_agent.tools.builtin.read_tool_result.model_schema import SCHEMA
from compass_agent.tools.catalog_contracts import ToolSpec
from compass_agent.tools.contracts import (
    InProcessBinding,
    RegisteredTool,
    ToolGovernance,
    ToolPolicy,
    ToolRejected,
    json_bytes,
)
from compass_agent.tools.registry import bounded_payload, parse_arguments


class ReadToolResultInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=1, max_length=200)
    offset: int | None = Field(default=None, ge=0)
    limit: int | None = Field(default=None, ge=1)
    cursor: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def check_position(self):
        if self.cursor is not None and (self.offset is not None or self.limit is not None):
            raise ValueError("cursor 与 offset/limit 互斥")
        return self


def definition(_context=None):
    return ToolDefinition(
        function=ToolFunction(
            name="read_tool_result",
            description=tool_text("read_tool_result"),
            parameters=ReadToolResultInput.model_json_schema(),
        )
    )


def registration(catalog):
    async def execute(args, context):
        if catalog.result_reader is None:
            raise ToolRejected("result_reader_unavailable")
        return await catalog.result_reader(ReadToolResultInput.model_validate(args), context)

    def authorize(args, context):
        context.require_running()
        ReadToolResultInput.model_validate(args)

    return RegisteredTool(
        "read_tool_result",
        definition(),
        ToolPolicy(),
        InProcessBinding(execute),
        parse_arguments,
        authorize,
        bounded_payload,
        parallel_safe=True,
    )


def evidence(args, payload, refs):
    if not isinstance(payload, dict) or not all(key in payload for key in ("ref", "range", "data")):
        return
    yield {
        "kind": "document",
        "title": "工具结果原件分页",
        "version": sha256(json_bytes(payload)).hexdigest(),
        "locator": payload["ref"],
        "record": payload,
        "sourceRefs": refs,
    }


SPEC = ToolSpec(
    "read_tool_result",
    "builtin",
    "context_results",
    definition,
    lambda context: context.result_reader is not None,
    parallel_safe=True,
    context_factory=registration,
    evidence_adapter=evidence,
)


SPEC = replace(
    SPEC, governance=ToolGovernance(channel="always_on", pack="core_base", subagent_allowed=True)
)


def normalize_input(raw, _context):
    args = dict(raw)
    if args.get("cursor"):
        args.pop("offset", None)
        args.pop("limit", None)
    return args


SPEC = replace(SPEC, normalize_input=normalize_input)


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="read_tool_result",
            description=tool_text("read_tool_result"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
