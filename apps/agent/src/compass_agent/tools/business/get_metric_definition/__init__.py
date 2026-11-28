"""读取已发布定义、统计粒度、期间、单位、适用维度与可见关系。"""

from dataclasses import replace
from uuid import UUID

from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import ToolDefinition, ToolFunction
from compass_agent.semantic.tool import Arguments, tool
from compass_agent.tools.business.get_metric_definition.model_schema import SCHEMA
from compass_agent.tools.contracts import ToolGovernance


class Input(Arguments):
    asset_id: UUID
    version_id: UUID | None = None


SPEC = tool("get_metric_definition", "definition", "read", Input)


SPEC = replace(
    SPEC, governance=ToolGovernance(channel="always_on", pack="data_base", subagent_allowed=True)
)


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="get_metric_definition",
            description=tool_text("get_metric_definition"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
