"""Prepare and execute a plan owner's confirmed schedule change."""

import asyncio
import json
from dataclasses import replace
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from pydantic import Field, TypeAdapter, ValidationError

from compass_agent.execution_context import current_execution, execution_path
from compass_agent.llm.continuation import canonical
from compass_agent.models.errors import ModelError
from compass_agent.prompt_assets import tool_text
from compass_agent.scheduler.cron import Cron
from compass_agent.schemas import (
    RunContractModel,
    ScheduleCreate,
    ScheduleDelete,
    ScheduleFields,
    SchedulePatch,
    ScheduleUpdate,
    ToolApprovalProposal,
    ToolDefinition,
    ToolFunction,
)
from compass_agent.tools.builtin.schedule_write.model_schema import SCHEMA
from compass_agent.tools.builtin.schedule_write.slim import normalizer
from compass_agent.tools.catalog_contracts import ToolSpec
from compass_agent.tools.contracts import (
    InProcessBinding,
    PreparedApproval,
    RegisteredTool,
    ToolGovernance,
    ToolPolicy,
    ToolRejected,
)
from compass_agent.tools.registry import bounded_payload, parse_arguments


class ScheduleCreateArgs(ScheduleFields):
    operation: Literal["create"]


class ScheduleUpdateArgs(RunContractModel):
    operation: Literal["update"]
    schedule_id: UUID = Field(alias="scheduleId", strict=False)
    expected_revision: int = Field(alias="expectedRevision", ge=1)
    patch: SchedulePatch


class ScheduleDeleteArgs(RunContractModel):
    operation: Literal["delete"]
    schedule_id: UUID = Field(alias="scheduleId", strict=False)
    expected_revision: int = Field(alias="expectedRevision", ge=1)


ARGUMENTS = TypeAdapter(
    Annotated[
        ScheduleCreateArgs | ScheduleUpdateArgs | ScheduleDeleteArgs,
        Field(discriminator="operation"),
    ]
)
NAME = "schedule_write"


WRITE_POLICY = ToolPolicy(effect="external_write", approval="required")


def definition(_context):
    return ToolDefinition(
        function=ToolFunction(
            name=NAME,
            description=(tool_text("schedule_write")),
            parameters={"type": "object", **ARGUMENTS.json_schema(by_alias=True)},
        )
    )


def command_for(args, request_id):
    if isinstance(args, ScheduleCreateArgs):
        return None, ScheduleCreate(
            requestId=request_id, **args.model_dump(exclude={"operation"}, exclude_unset=True)
        )
    if isinstance(args, ScheduleUpdateArgs):
        return args.schedule_id, ScheduleUpdate(
            requestId=request_id,
            expectedRevision=args.expected_revision,
            patch=args.patch,
        )
    return args.schedule_id, ScheduleDelete(
        requestId=request_id,
        expectedRevision=args.expected_revision,
    )


def describe(service, execution, command, schedule_id, operation):
    with service.store.transaction() as tx:
        plan = service.owned(tx, execution.trusted, schedule_id) if schedule_id else None
        service.check_revision(plan, command)
        fields = service.fields(service.store, plan, command)
        if fields is None:
            assert plan is not None
            return fields, (
                f"删除「{plan.name}」？执行记录会保留，已经开始交付的触发仍按原计划处理。"
            )
        next_fire = Cron.parse(fields.cron, fields.timezone).nearest(
            service.store.scheduler.clock(tx)
        )
        verb = "创建" if operation == "create" else "修改"
        rule = f"{fields.cron} · {fields.timezone}"
        lines = [f"{verb}「{fields.name}」。"]
        if plan:
            lines.append(f"原规则：{plan.cron} · {plan.timezone}")
        lines.extend((f"触发规则：{rule}", "状态：启用" if fields.enabled else "状态：停用"))
        if fields.enabled:
            local_fire = next_fire.astimezone(ZoneInfo(fields.timezone))
            lines.append(f"下次预计：{local_fire:%Y-%m-%d %H:%M}（{fields.timezone}）")
        else:
            lines.append("已开始交付的触发会继续处理。")
        question = fields.question[:600]
        if len(fields.question) > len(question):
            question += "\n\n…（此处仅展示前 600 字）"
        return fields, "\n".join([*lines, "分析内容：", question])


def registration(context):
    service = context.schedules
    execute_effect = context.schedule_effect
    assert service is not None and execute_effect is not None

    def require_chat(_, execution):
        execution.require_running()
        if execution.caller != "model" or current_execution.get() is None:
            raise ToolRejected("schedule_confirmation_required")

    async def prepare(args, execution):
        try:
            command_args = ARGUMENTS.validate_python(args)
        except ValidationError:
            raise ToolRejected("invalid_parameters") from None
        proposal_id = uuid5(execution.run_id, f"schedule:{execution.step_id}:{execution.call_id}")
        schedule_id, command = command_for(command_args, proposal_id)
        try:
            await service.write_access(execution.trusted, command)
            fields, summary = await asyncio.to_thread(
                describe, service, execution, command, schedule_id, command_args.operation
            )
            if fields and (
                command_args.operation == "create"
                or isinstance(command, ScheduleUpdate)
                and service.changes_input(command)
            ):
                await service.validate_model(fields)
        except ModelError as error:
            raise ToolRejected(error.code) from None
        return PreparedApproval(
            ToolApprovalProposal(
                proposalId=str(proposal_id),
                toolName=NAME,
                toolCallId=execution.call_id,
                argumentsDigest=sha256(canonical(args)).hexdigest(),
                summary=summary,
            ),
            {
                "kind": "schedule_owner",
                "owner": [str(v) for v in execution.owner],
                "scheduleId": str(schedule_id) if schedule_id else None,
                "operation": command_args.operation,
                "command": command.model_dump(mode="json", by_alias=True, exclude_unset=True),
            },
        )

    async def execute(args, execution):
        journal = current_execution.get()
        assert journal is not None
        grant = next(
            (
                p
                for p in journal.approved_proposals
                if p.get("kind") == "schedule_owner"
                and p.get("callId") == execution.call_id
                and p.get("stepId") == str(execution.step_id)
                and canonical(p.get("args")) == canonical(args)
            ),
            None,
        )
        if grant is None:
            raise ToolRejected("schedule_confirmation_required")
        command_class = {
            "create": ScheduleCreate,
            "update": ScheduleUpdate,
            "delete": ScheduleDelete,
        }[grant["operation"]]
        command = command_class.model_validate_json(
            json.dumps({**grant["command"], "requestId": grant["actionId"]})
        )
        schedule_id = UUID(grant["scheduleId"]) if grant["scheduleId"] else None
        return await execute_effect(
            service,
            journal,
            execution,
            grant,
            schedule_id,
            command,
            key=f"{execution_path.get()}/schedule:{execution.call_id}",
        )

    return RegisteredTool(
        NAME,
        definition(context),
        WRITE_POLICY,
        InProcessBinding(execute),
        parse_arguments,
        require_chat,
        bounded_payload,
        prepare_approval=prepare,
        normalize_input=normalizer(service),
        local_effect=True,
    )


SPEC = ToolSpec(
    NAME,
    "builtin",
    NAME,
    definition,
    lambda context: context.schedules is not None,
    context_factory=registration,
)


SPEC = replace(
    SPEC,
    governance=ToolGovernance(channel="always_on", pack="schedule_base", scheduler_allowed=False),
)


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="schedule_write",
            description=tool_text("schedule_write"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
