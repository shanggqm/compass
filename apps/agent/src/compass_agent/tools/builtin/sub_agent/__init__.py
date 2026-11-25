"""One prompt starts an isolated child Agent."""

from pydantic import Field, model_validator

from compass_agent.goal.contracts import MAX_TASKS, DelegationTask
from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import RunContractModel, ToolDefinition, ToolFunction
from compass_agent.tools.catalog_contracts import ToolSpec

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
