"""Submit a private answer candidate through the ordinary tool dispatcher."""

from dataclasses import replace

from pydantic import ValidationError

from compass_agent.context_runtime.answer_candidates import submit
from compass_agent.context_runtime.answers import ANSWER, answer_schema
from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import ToolDefinition, ToolFunction
from compass_agent.tools.builtin.final_answer.model_schema import SCHEMA
from compass_agent.tools.catalog_contracts import ToolSpec
from compass_agent.tools.contracts import (
    InProcessBinding,
    RegisteredTool,
    ToolGovernance,
    ToolPolicy,
    ToolRejected,
)
from compass_agent.tools.registry import bounded_payload, parse_arguments


def definition(_):
    return ToolDefinition(
        function=ToolFunction(
            name="final_answer",
            description=tool_text("final_answer"),
            parameters=answer_schema(),
        )
    )


def authorize(args, context):
    context.require_running()
    if context.caller != "model":
        raise ToolRejected("tool_scope_denied")
    from compass_agent.execution_context import current_execution

    journal = current_execution.get()
    if (getattr(journal, "context_binding", None) or {}).get("answerProtocol") == 3:
        return
    try:
        ANSWER.validate_python(args)
    except ValidationError:
        raise ToolRejected("invalid_answer_candidate") from None


async def complete(args, context):
    from compass_agent.execution_context import current_execution, execution_path
    from compass_agent.tools.contracts import ExecutionReceipt
    from compass_agent.tools.errors import ActionableToolError, ActionableToolRejected

    journal = current_execution.get()
    if (getattr(journal, "context_binding", None) or {}).get("answerProtocol") != 3:
        return await submit(args, context)
    if args.get("artifactFormat") in {"csv", "dashboard"}:
        evidence = getattr(journal, "answer_evidence", {}).get(execution_path.get(), {})
        if not any(item["kind"] == "query" for item in evidence.values()):
            raise ActionableToolRejected(
                ActionableToolError(
                    "artifact_source_required",
                    "fix_arguments",
                    "附件缺少查询结果。",
                    field="artifactFormat",
                    next_action="先执行查询，或选择 markdown。",
                )
            )
        queries = [item for item in evidence.values() if item["kind"] == "query"][-24:]
        if any("rows" not in item["record"] for item in queries):
            raise ActionableToolRejected(
                ActionableToolError(
                    "artifact_requires_bounded_rows",
                    "fix_arguments",
                    "附件包含仅有原件引用的大结果，当前附件生成需要有界表格。",
                    field="artifactFormat",
                    next_action="改用 markdown 交付当前结果与说明。",
                )
            )
    return ExecutionReceipt("", "ok", "confirmed", payload={"status": "ready", **args})


def registration():
    return RegisteredTool(
        "final_answer",
        definition(None),
        ToolPolicy(),
        InProcessBinding(complete),
        parse_arguments,
        authorize,
        bounded_payload,
        completion=True,
        public_projection="status_only",
    )


SPEC = ToolSpec(
    "final_answer", "builtin", "completion", definition, lambda context: True, factory=registration
)


SPEC = replace(SPEC, governance=ToolGovernance(channel="always_on", pack="core_base"))


def model_definition(context):
    return ToolDefinition(
        function=ToolFunction(
            name="final_answer",
            description=tool_text("final_answer"),
            parameters=SCHEMA,
        )
    )


SPEC = replace(SPEC, legacy_definition=SPEC.definition, definition=model_definition)
