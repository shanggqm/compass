"""One stable tool; only propose requests an explicit owner's confirmation."""

import asyncio
from uuid import uuid5

from pydantic import ValidationError

from compass_agent.execution_context import current_execution, execution_path
from compass_agent.goal.contracts import GoalInput, ProposeGoal
from compass_agent.models.errors import ModelError
from compass_agent.prompt_assets import tool_text
from compass_agent.schemas import GoalProposal, ToolApprovalProposal, ToolDefinition, ToolFunction
from compass_agent.tools.catalog_contracts import ToolSpec
from compass_agent.tools.contracts import (
    ExecutionReceipt,
    InProcessBinding,
    PreparedApproval,
    RegisteredTool,
    ToolPolicy,
    ToolRejected,
)
from compass_agent.tools.registry import bounded_payload, parse_arguments


def definition(_context):
    return ToolDefinition(
        function=ToolFunction(
            name="goal",
            description=tool_text("goal"),
            parameters={"type": "object", **GoalInput.model_json_schema(by_alias=True)},
        )
    )


def parse(value):
    try:
        return GoalInput.model_validate(value).root
    except ValidationError:
        raise ToolRejected("invalid_parameters") from None


def registration(catalog):
    def authorize(args, context):
        context.require_running()
        if (
            context.caller != "model"
            or execution_path.get() != "root"
            or current_execution.get() is None
        ):
            raise ToolRejected("goal_owner_required")
        parse(args)

    async def prepare(args, context):
        value = parse(args)
        if not isinstance(value, ProposeGoal):
            return None
        from hashlib import sha256

        from compass_agent.llm.continuation import canonical

        proposal = GoalProposal.model_validate(value.model_dump(exclude={"op"}))
        identifier = uuid5(context.run_id, f"goal:{context.step_id}:{context.call_id}")
        return PreparedApproval(
            ToolApprovalProposal(
                proposalId=str(identifier),
                toolName="goal",
                toolCallId=context.call_id,
                argumentsDigest=sha256(canonical(args)).hexdigest(),
                summary=proposal.contract.objective,
                goal=proposal,
            ),
            {"kind": "goal_owner"},
        )

    async def execute(args, context):
        value = parse(args)
        if isinstance(value, ProposeGoal):
            journal = current_execution.get()
            if not any(
                p.get("kind") == "goal_owner"
                and p.get("callId") == context.call_id
                and p.get("stepId") == str(context.step_id)
                and p.get("args") == args
                for p in journal.approved_proposals
                + ([journal.approved_proposal] if journal.approved_proposal else [])
            ):
                raise ToolRejected("goal_confirmation_required")
        try:
            result = await asyncio.to_thread(catalog.goals.execute, value, context)
        except ModelError as error:
            raise ToolRejected(error.code) from None
        # The payload contains only the owner's contract and a currently authorized
        # state projection. It remains readable after this execution epoch ends.
        return ExecutionReceipt(context.call_id, "ok", "confirmed", result, source_deps=())

    return RegisteredTool(
        "goal",
        definition(catalog),
        ToolPolicy("local_artifact", "none"),
        InProcessBinding(execute),
        parse_arguments,
        authorize,
        bounded_payload,
        prepare_approval=prepare,
        local_effect=True,
        public_projection="status_only",
    )


SPEC = ToolSpec(
    "goal",
    "builtin",
    "goal",
    definition,
    lambda context: context.goals is not None,
    context_factory=registration,
    public_projection="status_only",
)
