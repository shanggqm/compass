"""把已保存和校验的版本提交双人评审，需要维护者确认。"""

from dataclasses import replace
from uuid import UUID

from pydantic import Field

from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import ToolDefinition, ToolFunction
from compass_agent.semantic.tool import Arguments, tool
from compass_agent.tools.business.submit_semantic_change.model_schema import SCHEMA
from compass_agent.tools.contracts import ToolGovernance


class Input(Arguments):
    version_id: UUID
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    note: str = Field(min_length=1, max_length=160)
    request_key: UUID
    binding_id: UUID | None = None


SPEC = tool("submit_semantic_change", "submit", "govern", Input)


SPEC = replace(
    SPEC,
    governance=ToolGovernance(
        channel="user_selected", pack="semantic_govern", requires_semantic_maintainer=True
    ),
)


def normalize_input(raw, context):
    from uuid import uuid5

    return {
        "request_key": str(uuid5(context.run_id, f"submit:{context.step_id}:{context.call_id}")),
        **raw,
    }


SPEC = replace(SPEC, normalize_input=normalize_input)


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="submit_semantic_change",
            description=tool_text("submit_semantic_change"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
