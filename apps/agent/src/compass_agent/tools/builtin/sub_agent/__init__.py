"""One prompt starts an isolated child Agent."""

from dataclasses import replace

from pydantic import Field, model_validator

from compass_agent.goal.contracts import MAX_TASKS, DelegationTask
from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import RunContractModel, ToolDefinition, ToolFunction
from compass_agent.tools.builtin.sub_agent.model_schema import SCHEMA
from compass_agent.tools.builtin.sub_agent.slim import normalize_input
from compass_agent.tools.catalog_contracts import ToolSpec
from compass_agent.tools.contracts import ToolGovernance

NAME = "sub_agent"


class SubAgentInput(RunContractModel):
    prompt: str | None = Field(default=None, min_length=1, max_length=8000, pattern=r"\S")
    tasks: list[DelegationTask] | None = Field(default=None, min_length=1, max_length=MAX_TASKS)

    @model_validator(mode="after")
    def one_of(self):
        if (self.prompt is None) == (self.tasks is None):
            raise ValueError("provide exactly one of prompt or tasks")
        if self.tasks and len({task.name for task in self.tasks}) != len(self.tasks):
            raise ValueError("task names must be unique")
        return self


def definition(_context):
    return ToolDefinition(
        function=ToolFunction(
            name=NAME,
            description=(tool_text("sub_agent")),
            parameters=SubAgentInput.model_json_schema(),
        )
    )


def evidence(args, payload, refs):
    # The child returns handles; permission records remain in private Run storage.
    identifiers = payload.get("evidenceIds", [])
    identifiers = [
        *identifiers,
        *(key for d in payload.get("deliveries", []) for key in d.get("evidenceIds", [])),
    ]
    # Earlier receipts included full records; their IDs address the same private storage.
    identifiers = [*identifiers, *(item["id"] for item in payload.get("evidence", []))]
    for identifier in dict.fromkeys(identifiers):
        yield {"importEvidenceId": identifier}


SPEC = ToolSpec(
    NAME,
    "builtin",
    "workflow",
    definition,
    lambda context: context.workflow,
    orchestrates=True,
    evidence_adapter=evidence,
    public_projection="status_only",
)


SPEC = replace(
    SPEC,
    governance=ToolGovernance(
        channel="scene_routed",
        pack="deep_analysis_pack",
        default_scenes=frozenset(
            [
                "SC-03",
                "SC-04",
                "SC-14",
                "SC-20",
                "SC-25",
                "SC-26",
                "SC-27",
                "SC-28",
                "SC-31",
                "SC-32",
                "SC-35",
                "SC-40",
            ]
        ),
    ),
)

SPEC = replace(SPEC, normalize_input=normalize_input)


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="sub_agent",
            description=tool_text("sub_agent"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
