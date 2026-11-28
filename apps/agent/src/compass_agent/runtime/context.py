"""Generic request context and model input assembly."""

from dataclasses import dataclass
from uuid import UUID

from compass_agent.harness.context import AgentDefinition, build_messages, model_input
from compass_agent.prompt_assets import prompt_text
from compass_agent.schemas import (
    RUN_STREAM_LIMITS,
    AnalysisRunInput,
    ChatMessage,
    GenerateInput,
    ModelTokenLimits,
    ToolDefinition,
)
from compass_agent.tools.catalog import definitions as catalog_definitions
from compass_agent.tools.catalog_contracts import CatalogContext


@dataclass(frozen=True, slots=True)
class RunRoute:
    provider_id: UUID
    binding_version: int
    model_label: str
    scene_id: str
    route_prompt: str
    stream: bool
    thinking: bool
    deepseek: bool = False
    tools_enabled: bool = False
    deliverable: str | None = None
    limits: ModelTokenLimits | None = None
    requires_plan: bool = False
    vendor: str = ""
    model: str = ""
    task_type: str = ""
    schema_output: bool = False
    required_tools: tuple[str, ...] = ()


def initial_messages(
    definition: AgentDefinition,
    command: AnalysisRunInput,
    route: RunRoute,
    *,
    controlled=False,
    workflow=False,
):
    instructions = route.route_prompt
    if command.constraints and command.constraints.plan == "required":
        instructions += "\n" + prompt_text("shared/fragments/plan-required.md")
    if workflow:
        instructions += "\n" + prompt_text("shared/fragments/workflow.md").replace(
            "{{tool_limit}}", str(RUN_STREAM_LIMITS["toolsPerStep"])
        )
    return build_messages(definition, command, instructions)


def builtin_definitions(definition: AgentDefinition, *, database=False, sandbox=False):
    return [
        entry.definition for entry in definition.tools if entry.definition is not None
    ] + catalog_definitions(
        CatalogContext(definition.builtin_tools, database=database, sandbox=sandbox)
    )


def generate_input(
    definition,
    messages: list[ChatMessage],
    continuation_token,
    route: RunRoute,
    *,
    controlled=False,
    extra_tools: tuple[ToolDefinition, ...] = (),
    defer_budget=False,
) -> GenerateInput:
    return model_input(
        messages,
        continuation_token,
        tools=[*builtin_definitions(definition), *extra_tools],
        stream=route.stream,
        thinking=route.thinking,
        controlled=controlled,
        deepseek=route.deepseek,
        defer_budget=defer_budget,
    )


def compass_definition() -> AgentDefinition:
    return AgentDefinition(builtin_tools=frozenset({"todo", "database", "python", "workflow"}))
