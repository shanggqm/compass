"""搜索当前发布及授权范围内的术语、指标和实体，按名称、别名和业务域检索。"""

from dataclasses import replace
from typing import Literal
from uuid import UUID

from pydantic import Field

from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import ToolDefinition, ToolFunction
from compass_agent.semantic.tool import Arguments, tool
from compass_agent.tools.business.search_semantics.model_schema import SCHEMA
from compass_agent.tools.contracts import ToolGovernance


class Input(Arguments):
    query: str = Field(max_length=256)
    kinds: list[str] | None = None
    domain: str | None = None
    scope_filter: UUID | None = None
    cursor: UUID | Literal[0] | None = None
    limit: int = Field(default=12, ge=1, le=50)


SPEC = tool("search_semantics", "search", "read", Input)


SPEC = replace(
    SPEC, governance=ToolGovernance(channel="always_on", pack="data_base", subagent_allowed=True)
)


def normalize_input(raw, _context):
    args = dict(raw)
    if "kind" in args:
        args["kinds"] = [args.pop("kind")]
    return args


SPEC = replace(SPEC, normalize_input=normalize_input)


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="search_semantics",
            description=tool_text("search_semantics"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
