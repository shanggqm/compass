"""Discover authorized document sections from the collection pinned to a Run."""

from dataclasses import replace
from datetime import date
from typing import Literal

from pydantic import Field

from compass_agent.documents.tool import Arguments, document_tool
from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import ToolDefinition, ToolFunction
from compass_agent.tools.business.search_documents.model_schema import SCHEMA
from compass_agent.tools.contracts import ToolGovernance


class Input(Arguments):
    query: str = Field(min_length=1, max_length=256)
    kind: Literal["knowledge", "history", "external"] | None = None
    entity: str | None = Field(default=None, min_length=1, max_length=240)
    businessDate: date | None = None
    historical: bool = False


SPEC = document_tool("search_documents", "search", Input)


SPEC = replace(
    SPEC, governance=ToolGovernance(channel="always_on", pack="doc_base", subagent_allowed=True)
)


def normalize_input(raw, _context):
    args = dict(raw)
    if args.get("businessDate") and "historical" not in args:
        args["historical"] = True
    return args


SPEC = replace(SPEC, normalize_input=normalize_input)


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="search_documents",
            description=tool_text("search_documents"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
