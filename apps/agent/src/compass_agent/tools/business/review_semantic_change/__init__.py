"""检查草稿来源、声明、依赖、冲突与已登记影响，说明阻断项及修正建议。"""

from dataclasses import replace
from uuid import UUID

from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import ToolDefinition, ToolFunction
from compass_agent.semantic.tool import Arguments, tool
from compass_agent.tools.business.review_semantic_change.model_schema import SCHEMA
from compass_agent.tools.contracts import ToolGovernance


class Input(Arguments):
    version_id: UUID


SPEC = tool("review_semantic_change", "validate", "draft", Input)


SPEC = replace(
    SPEC,
    governance=ToolGovernance(
        channel="user_selected", pack="semantic_govern", requires_semantic_maintainer=True
    ),
)


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="review_semantic_change",
            description=tool_text("review_semantic_change"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
