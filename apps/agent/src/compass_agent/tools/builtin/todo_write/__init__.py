"""Generic task planning tool; domain adapters can supply a stricter action schema."""

from dataclasses import replace
from typing import Any

from pydantic import TypeAdapter

from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import ToolDefinition, ToolFunction
from compass_agent.tools.builtin.todo_write.model_schema import SCHEMA
from compass_agent.tools.builtin.todo_write.plan import (
    TODO_ACTION,
    ExecutionPlan,
    TodoDefinition,
    TodoRejected,
)
from compass_agent.tools.catalog_contracts import ToolSpec
from compass_agent.tools.contracts import ExecutionReceipt, ToolGovernance


def definition(
    adapter: TypeAdapter[Any] = TODO_ACTION,
    description=None,
):
    return ToolDefinition(
        function=ToolFunction(
            name="todo_write",
            description=description if description is not None else tool_text("todo_write"),
            parameters={"type": "object", **adapter.json_schema(by_alias=True)},
        )
    )


async def execute(plan: ExecutionPlan[TodoDefinition], args, context, index: int):
    try:
        if "todos" in args:
            from compass_agent.tools.builtin.todo_write.slim import apply_slim

            apply_slim(plan, args, index)
        else:
            plan.apply(TODO_ACTION.validate_python(args), index)
    except TodoRejected as error:
        return ExecutionReceipt(
            context.call_id, "error", "confirmed", {"status": "error", "code": error.code}
        )
    return ExecutionReceipt(context.call_id, "ok", "confirmed", {"status": "ok", **plan.snapshot()})


SPEC = ToolSpec("todo_write", "builtin", "todo", lambda context: definition(), lambda context: True)


SPEC = replace(SPEC, governance=ToolGovernance(channel="scene_routed", pack="deep_analysis_pack"))


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="todo_write",
            description=tool_text("todo_write"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
