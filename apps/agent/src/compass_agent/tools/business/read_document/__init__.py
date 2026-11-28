"""Read a previously discovered paragraph or an adjacent paragraph."""

from dataclasses import replace
from uuid import UUID

from pydantic import Field

from compass_agent.documents.tool import Arguments, document_tool
from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import ToolDefinition, ToolFunction
from compass_agent.tools.business.read_document.model_schema import SCHEMA
from compass_agent.tools.contracts import ToolGovernance


class Input(Arguments):
    ref: UUID
    offset: int = Field(default=0, ge=-1, le=1)


SPEC = document_tool("read_document", "read", Input)


SPEC = replace(
    SPEC, governance=ToolGovernance(channel="always_on", pack="doc_base", subagent_allowed=True)
)


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="read_document",
            description=tool_text("read_document"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
