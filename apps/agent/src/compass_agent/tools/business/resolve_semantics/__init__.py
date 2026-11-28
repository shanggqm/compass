"""结合上下文解析可见指标，歧义由原会话用户选择。"""

from dataclasses import replace
from uuid import UUID

from pydantic import Field

from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import ToolDefinition, ToolFunction
from compass_agent.semantic.tool import Arguments, Parameters, tool
from compass_agent.tools.business.resolve_semantics.model_schema import SCHEMA
from compass_agent.tools.contracts import ToolGovernance


class Input(Arguments):
    mention: str = Field(min_length=1, max_length=160)
    metric_id: UUID | None = None
    dimensions: list[str] = Field(default_factory=list, max_length=8)
    parameters: Parameters | None = None
    historical_as_of: str | None = Field(
        default=None,
        max_length=40,
        description="User-specified historical data cutoff; omit to use the Run start.",
    )


SPEC = tool("resolve_semantics", "resolve", "read", Input)


SPEC = replace(
    SPEC, governance=ToolGovernance(channel="always_on", pack="data_base", subagent_allowed=True)
)


def normalize_input(raw, _context):
    from datetime import date, timedelta

    args = dict(raw)
    params = {key: args.pop(key) for key in ("start", "end", "group_by", "filters") if key in args}
    if params:
        if "end" in params:
            params["end"] = (date.fromisoformat(params["end"]) + timedelta(days=1)).isoformat()
        args["parameters"] = params
    return args


SPEC = replace(SPEC, normalize_input=normalize_input)


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="resolve_semantics",
            description=tool_text("resolve_semantics"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
