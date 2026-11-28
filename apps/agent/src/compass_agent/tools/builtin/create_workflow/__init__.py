"""Model-generated workflow definition and trial inputs."""

from dataclasses import replace

from pydantic import Field

from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import RunContractModel, ToolDefinition, ToolFunction
from compass_agent.tools.builtin.create_workflow.model_schema import SCHEMA
from compass_agent.tools.catalog_contracts import ToolSpec
from compass_agent.tools.contracts import ToolGovernance

NAME = "create_workflow"


class CreateWorkflowInput(RunContractModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    parameters: dict = Field(description=tool_text(NAME, "parameters"))
    script_source: str = Field(alias="scriptSource", min_length=1, max_length=16384)
    args: dict = Field(description=tool_text(NAME, "args"))


def definition(_context):
    parameters = CreateWorkflowInput.model_json_schema(by_alias=True)
    for field, key in (("args", "args"), ("parameters", "parameters")):
        parameters["properties"][field]["description"] = tool_text(NAME, key)
    return ToolDefinition(
        function=ToolFunction(
            name=NAME,
            description=(tool_text("create_workflow")),
            parameters=parameters,
        )
    )


SPEC = ToolSpec(
    NAME, "builtin", "workflow", definition, lambda context: context.workflow, orchestrates=True
)


SPEC = replace(SPEC, governance=ToolGovernance(channel="user_selected", pack="workflow_pack"))


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="create_workflow",
            description=tool_text("create_workflow"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
