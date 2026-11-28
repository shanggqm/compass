"""Validate and execute generated JS through the same ABI on trial and reuse."""

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from compass_agent.tools.builtin.create_workflow import CreateWorkflowInput
from compass_agent.tools.builtin.sub_agent import NAME as SUB_AGENT
from compass_agent.tools.builtin.sub_agent import SubAgentInput
from compass_agent.tools.contracts import ToolRejected, json_bytes
from compass_agent.tools.errors import (
    ActionableToolError,
    ActionableToolRejected,
    invalid_arguments,
)
from compass_agent.tools.registry import bounded_payload, validate_schema
from compass_agent.workflows.assets import normalize_source
from compass_agent.workflows.script import WorkflowError, execute_script


@dataclass(frozen=True, slots=True)
class WorkflowTrial:
    definition: CreateWorkflowInput
    result: dict
    step_id: UUID | None


async def execute(source, parameters, args, context, child, *, group_event=None):
    validate_schema(parameters)
    if len(json_bytes(parameters)) > 8192:
        raise ActionableToolRejected(
            ActionableToolError(
                "invalid_workflow_parameters",
                "fix_arguments",
                "工作流参数声明超过 8 KiB。",
                field="parameters",
                next_action="缩小参数声明后重新试跑。",
            )
        )
    if error := next(Draft202012Validator(parameters).iter_errors(args), None):
        from dataclasses import replace

        detail = invalid_arguments(error).error
        raise ActionableToolRejected(
            replace(
                detail,
                code="invalid_workflow_parameters",
                field="args" + ("." + detail.field if detail.field else ""),
                next_action="让 args 与 parameters 中的字段和类型一致，再重新试跑。",
            )
        )

    async def dispatch(profile, value, group_id, request_id):
        nonlocal dispatched
        if profile != SUB_AGENT:
            raise ToolRejected("invalid_workflow_child")
        from compass_agent.tools.builtin.sub_agent.slim import normalize_input
        from compass_agent.tools.input_adapter import normalize_arguments

        try:
            normalized = await normalize_arguments(normalize_input, value, context)
            task = SubAgentInput.model_validate(normalized)
        except ValidationError as error:
            field = ".".join(str(part) for part in error.errors()[0]["loc"])
            raise ActionableToolRejected(
                ActionableToolError(
                    "invalid_workflow_child",
                    "fix_arguments",
                    "工作流子任务参数不符合声明。",
                    field="scriptSource.agent.input" + ("." + field if field else ""),
                    next_action=(
                        "使用 agent('sub_agent', "
                        "{tasks:[{name:'子任务名称', objective:'调查目标'}]})；"
                        "单任务也可使用 {prompt:'调查目标'}。"
                    ),
                )
            ) from None
        result = await child(task if task.tasks else task.prompt, group_id, request_id)
        dispatched = True
        return result

    dispatched = False
    step_id = None

    def checkpoint(identifier):
        nonlocal step_id
        step_id = identifier

    def validate_result(result):
        if (
            not dispatched
            or not isinstance(result, dict)
            or result.get("status") not in ("ok", "needs_data")
            or "result" not in result
            or set(result) != {"status", "result"}
        ):
            raise ToolRejected("invalid_workflow_result")
        bounded_payload(result)

    try:
        result = await execute_script(
            normalize_source(source),
            args,
            child_dispatch=dispatch,
            group_event=group_event,
            checkpoint=checkpoint,
            validate_result=validate_result,
            parent_model_step=context.step_id,
            remaining=context.control.remaining,
        )
    except ToolRejected:
        raise
    except (WorkflowError, ValueError, OSError):
        raise ToolRejected("workflow_execution_failed") from None
    return cast(dict, result), step_id


async def trial(command, context, child, *, group_event=None):
    try:
        definition = CreateWorkflowInput.model_validate(command)
        source = normalize_source(definition.script_source)
        if not definition.description.strip():
            raise ValueError("blank description")
    except ValueError:
        raise ToolRejected("invalid_workflow_definition") from None
    result, step = await execute(
        source, definition.parameters, definition.args, context, child, group_event=group_event
    )
    return WorkflowTrial(definition.model_copy(update={"script_source": source}), result, step)
