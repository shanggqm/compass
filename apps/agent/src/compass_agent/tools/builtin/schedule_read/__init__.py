"""Find, inspect and view execution history for owned schedules."""

from dataclasses import replace
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter, ValidationError

from compass_agent.execution_context import current_execution, execution_path
from compass_agent.models.errors import ModelError
from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import RunContractModel, ScheduleStatus, ToolDefinition, ToolFunction
from compass_agent.tools.builtin.schedule_read.model_schema import SCHEMA
from compass_agent.tools.catalog_contracts import ToolSpec
from compass_agent.tools.contracts import (
    ExecutionReceipt,
    InProcessBinding,
    RegisteredTool,
    ToolGovernance,
    ToolPolicy,
    ToolRejected,
)
from compass_agent.tools.registry import bounded_payload, parse_arguments


class ScheduleListArgs(RunContractModel):
    operation: Literal["list"]
    q: str | None = Field(default=None, max_length=120)
    status: ScheduleStatus | None = None
    cursor: UUID | None = Field(default=None, strict=False)


class ScheduleGetArgs(RunContractModel):
    operation: Literal["get"]
    schedule_id: UUID = Field(alias="scheduleId", strict=False)


class ScheduleHistoryArgs(RunContractModel):
    operation: Literal["history"]
    schedule_id: UUID = Field(alias="scheduleId", strict=False)
    cursor: UUID | None = Field(default=None, strict=False)


ARGUMENTS = TypeAdapter(
    Annotated[
        ScheduleListArgs | ScheduleGetArgs | ScheduleHistoryArgs, Field(discriminator="operation")
    ]
)
NAME = "schedule_read"


def definition(_context):
    return ToolDefinition(
        function=ToolFunction(
            name=NAME,
            description=(tool_text("schedule_read")),
            parameters={"type": "object", **ARGUMENTS.json_schema(by_alias=True)},
        )
    )


def registration(context):
    service = context.schedules
    assert service is not None

    async def execute(args, execution):
        slim = "operation" not in args
        if slim:
            args = (
                {"operation": "get", "scheduleId": args["scheduleId"]}
                if args.get("scheduleId")
                else {"operation": "list", "q": args.get("query")}
            )
        try:
            command = ARGUMENTS.validate_python(args)
        except ValidationError:
            raise ToolRejected("invalid_parameters") from None

        async def query():
            if isinstance(command, ScheduleListArgs):
                result = await service.list(
                    execution.trusted, command.q, command.status, command.cursor
                )
                data = result.model_dump(mode="json", by_alias=True)
                for item in data["items"]:
                    item.pop("question", None)
                    item.pop("constraints", None)
                    item.pop("workflowAssetRef", None)
                return bounded_payload(data)
            if isinstance(command, ScheduleGetArgs):
                result = await service.get(execution.trusted, command.schedule_id)
                if slim:
                    history = await service.history(execution.trusted, command.schedule_id, None)
                    return bounded_payload(
                        {
                            "schedule": result.model_dump(mode="json", by_alias=True),
                            "history": history.model_dump(mode="json", by_alias=True),
                        }
                    )
            else:
                result = await service.history(
                    execution.trusted, command.schedule_id, command.cursor
                )
            return bounded_payload(result.model_dump(mode="json", by_alias=True))

        try:
            journal = current_execution.get()
            data = (
                await journal.local_read(
                    f"{execution_path.get()}/schedule-read:{execution.call_id}", args, query
                )
                if journal
                else await query()
            )
        except ModelError as error:
            raise ToolRejected(error.code) from None
        return ExecutionReceipt(execution.call_id, "ok", "confirmed", data)

    return RegisteredTool(
        NAME,
        definition(context),
        ToolPolicy(),
        InProcessBinding(execute),
        parse_arguments,
        lambda _, execution: execution.require_running(),
        bounded_payload,
        parallel_safe=True,
    )


SPEC = ToolSpec(
    NAME,
    "builtin",
    NAME,
    definition,
    lambda context: context.schedules is not None,
    parallel_safe=True,
    context_factory=registration,
)


SPEC = replace(
    SPEC,
    governance=ToolGovernance(channel="always_on", pack="schedule_base", subagent_allowed=True),
)


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="schedule_read",
            description=tool_text("schedule_read"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
