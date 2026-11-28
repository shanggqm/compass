"""Expose the current owner's compatible stored assets as model tools."""

from uuid import UUID

from compass_agent.context_runtime.receipt_evidence import receipt
from compass_agent.prompt_assets import prompt_text
from compass_agent.schemas import ToolDefinition, ToolFunction
from compass_agent.tools.contracts import InProcessBinding, RegisteredTool, ToolPolicy
from compass_agent.tools.registry import bounded_payload, parse_arguments

PREFIX = "workflow_"


def tool_name(asset_id: UUID) -> str:
    return PREFIX + asset_id.hex


def is_asset_tool(name: str) -> bool:
    identifier = name.removeprefix(PREFIX)
    return (
        name.startswith(PREFIX)
        and len(identifier) == 32
        and all(char in "0123456789abcdef" for char in identifier)
    )


def registration(asset, handler, *, declaration=None):
    name = tool_name(asset.asset_id)
    definition = declaration or ToolDefinition(
        function=ToolFunction(
            name=name,
            description=(
                prompt_text("shared/tools/workflow-label.md")
                + f"「{asset.name}」 v{asset.definition_version}。{asset.description}"
            ),
            parameters=asset.parameters,
        )
    )
    return RegisteredTool(
        name,
        definition,
        ToolPolicy(),
        InProcessBinding(handler(asset), orchestrates=True),
        parse_arguments,
        lambda args, context: context.require_running(),
        bounded_payload,
        evidence_adapter=receipt,
        workflow_id=asset.asset_id,
    )
