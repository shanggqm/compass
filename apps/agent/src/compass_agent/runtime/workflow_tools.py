"""Connect generic workflow execution to Run supervision, drafts and progress."""

import asyncio
from dataclasses import replace
from uuid import uuid5

from compass_agent.execution_context import current_execution, execution_scope
from compass_agent.models.errors import ModelError
from compass_agent.schemas import (
    ExecutionEnvironment,
    ExecutionSubAgent,
    ExecutionWorkflow,
    RunExtensionPayload,
    RunWorkflowProgress,
    StreamExtension,
    WorkflowDraftView,
)
from compass_agent.tools.builtin.create_workflow import NAME as CREATE_WORKFLOW
from compass_agent.tools.builtin.create_workflow import CreateWorkflowInput
from compass_agent.tools.builtin.sub_agent import NAME as SUB_AGENT
from compass_agent.tools.builtin.sub_agent import SubAgentInput
from compass_agent.tools.contracts import ExecutionReceipt, PhaseCanceled, ToolRejected, json_bytes
from compass_agent.workflows import engine
from compass_agent.workflows.assets import DYNAMIC_CONTRACT, valid_name


class WorkflowTools:
    def __init__(self, bindings, assets, drafts):
        self.bindings, self.assets, self.drafts = bindings, assets, drafts

    def start(self, prepared, identifier, **fields):
        name = prepared.tool.name
        trace = self.bindings.state.trace
        assert trace is not None
        asset = self.bindings.assets_by_tool.get(name)
        if name == SUB_AGENT:
            fields.update(
                kind="subagent",
                title="子 Agent",
                subagent=ExecutionSubAgent(name="子 Agent"),
            )
        elif name == CREATE_WORKFLOW or asset:
            fields.update(
                kind="workflow",
                title=asset.name if asset else "试跑工作流",
                executor=ExecutionEnvironment(kind="quickjs"),
                workflow=ExecutionWorkflow(
                    mode="reused" if asset else "generated",
                    assetId=str(asset.asset_id) if asset else None,
                    name=asset.name if asset else None,
                    version=asset.definition_version if asset else None,
                ),
            )
        trace.update(identifier, status="running", **fields)

    def _node(self, context):
        trace = self.bindings.state.trace
        return trace.calls.get((str(context.step_id), context.call_id)) if trace else None

    async def subagent(self, args, context):
        value = SubAgentInput.model_validate(args)
        if value.tasks:
            result = await self.bindings.subagent.tasks(
                value.tasks, context, group=self._node(context)
            )
        else:
            result = await self.bindings.subagent.run(
                value.prompt, context, identifier=self._node(context)
            )
        journal = current_execution.get()
        dependencies = None
        if (getattr(journal, "context_binding", None) or {}).get("answerProtocol") in {2, 3}:
            identifiers = {
                key
                for delivery in result.get("deliveries", [result])
                for key in delivery.get("evidenceIds", [])
            }
            from compass_agent.goal.ledger import unique_refs

            dependencies = tuple(
                unique_refs(
                    [
                        record
                        for records in journal.answer_evidence.values()
                        for key, record in records.items()
                        if key in identifiers
                    ]
                )
            )
        return ExecutionReceipt(
            context.call_id, "ok", "confirmed", result, source_deps=dependencies
        )

    def _workflow_host(self, context):
        trace, parent = self.bindings.state.trace, self._node(context)
        groups = {}

        def group_event(number, status):
            if not trace:
                return
            if status == "running":
                groups[number] = trace.start(
                    "group", "并行子任务", parentId=parent, scheduling="parallel"
                )
            else:
                trace.finish(groups[number], status)

        async def child(prompt, group, request_id):
            child_id = str(uuid5(context.step_id, f"{context.call_id}:child:{request_id}"))
            child_context = replace(context, call_id=child_id, caller="workflow")
            context.budget.start_tool()
            if isinstance(prompt, SubAgentInput) and prompt.tasks:
                return await self.bindings.subagent.tasks(
                    prompt.tasks, child_context, group=groups.get(group, parent)
                )
            return await self.bindings.subagent.run(
                prompt,
                child_context,
                group=groups.get(group, parent),
            )

        return child, group_event

    async def create(self, args, context):
        if self.drafts is None:
            raise ToolRejected("workflow_save_unavailable")
        try:
            command = CreateWorkflowInput.model_validate(args)
            name = valid_name(command.name)
        except (ModelError, ValueError):
            raise ToolRejected("invalid_workflow_definition") from None
        child, group_event = self._workflow_host(context)
        with execution_scope(f"workflow:{context.step_id}:{context.call_id}"):
            trial = await engine.trial(args, context, child, group_event=group_event)
        value = trial.result
        if value["status"] == "ok":
            definition = trial.definition
            summary = value["result"]
            summary = summary if isinstance(summary, str) else json_bytes(summary).decode()
            offer = asyncio.create_task(
                asyncio.to_thread(
                    self.drafts.offer,
                    context.trusted,
                    WorkflowDraftView(
                        name=name,
                        entryContract=DYNAMIC_CONTRACT,
                        scriptSource=definition.script_source,
                        args=definition.args,
                        summary=summary[:4000],
                        description=definition.description,
                        parameters=definition.parameters,
                    ),
                    step_id=trial.step_id,
                )
            )
            try:
                draft = await asyncio.shield(offer)
            except asyncio.CancelledError:
                await asyncio.gather(offer, return_exceptions=True)
                raise PhaseCanceled(cleanup_confirmed=True) from None
            if trial.step_id is None:
                payload = RunWorkflowProgress(
                    source="dynamic", key="save_offer", status="completed", detail=str(draft)
                )
                self.bindings.emit(
                    StreamExtension(
                        type="stream.extension",
                        payload=RunExtensionPayload(
                            name="compass.workflow.node",
                            version=1,
                            required=False,
                            data=payload.model_dump(mode="json", by_alias=True, exclude_none=True),
                        ),
                    )
                )
            value = {
                "status": value["status"],
                "result": value["result"],
                "draftId": str(draft),
                "saveStatus": "awaiting_confirmation",
            }
        return ExecutionReceipt(
            context.call_id, "ok" if value["status"] == "ok" else "empty", "confirmed", value
        )

    def reuse(self, expected):
        async def execute(args, context):
            try:
                asset = await asyncio.to_thread(
                    self.assets.get, context.trusted, expected.asset_id, active=True
                )
            except ModelError:
                raise ToolRejected("workflow_unavailable") from None
            if (
                asset.entry_contract != DYNAMIC_CONTRACT
                or asset.parameters is None
                or asset.definition_version != expected.definition_version
                or asset.parameters != expected.parameters
                or asset.script_source is None
            ):
                raise ToolRejected("workflow_unavailable")
            child, group_event = self._workflow_host(context)
            with execution_scope(f"workflow:{context.step_id}:{context.call_id}"):
                result, _step_id = await engine.execute(
                    asset.script_source,
                    asset.parameters,
                    args,
                    context,
                    child,
                    group_event=group_event,
                )
            return ExecutionReceipt(
                context.call_id,
                "ok" if result["status"] == "ok" else "empty",
                "confirmed",
                result,
            )

        return execute
