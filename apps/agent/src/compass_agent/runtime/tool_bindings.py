"""Bind registered tools to per-Run policy, progress and model observations."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from compass_agent.harness.autonomy import AutonomyBudget, AutonomyLimits, AutonomyStopped
from compass_agent.harness.contracts import ToolResult
from compass_agent.runtime.subagent import SubAgent
from compass_agent.runtime.workflow_tools import WorkflowTools
from compass_agent.schemas import (
    TOOL_CATALOG_LIMIT,
    ExecutionEnvironment,
    ExecutionOrigin,
    RunArtifact,
    RunExtensionPayload,
    RunWaiting,
    StreamExtension,
    ToolDefinition,
    WorkflowAssetView,
)
from compass_agent.tools.batch import dispatch_batch
from compass_agent.tools.builtin.create_workflow import NAME as CREATE_WORKFLOW
from compass_agent.tools.builtin.sub_agent import NAME as SUB_AGENT
from compass_agent.tools.builtin.todo_write import execute as execute_todo
from compass_agent.tools.builtin.todo_write.plan import ExecutionPlan, TodoDefinition
from compass_agent.tools.catalog import registrations
from compass_agent.tools.catalog_contracts import CatalogContext
from compass_agent.tools.contracts import (
    McpBinding,
    PhaseCanceled,
    PreparedToolCall,
    ResourceUnconfirmed,
    SandboxBinding,
    ToolExecutionContext,
    ToolExecutionEvent,
    ToolRejected,
    json_bytes,
)
from compass_agent.tools.loader import RunTools
from compass_agent.tools.registry import has_catalog_room
from compass_agent.workflows.assets import DYNAMIC_CONTRACT
from compass_agent.workflows.catalog import PREFIX, is_asset_tool, registration

if TYPE_CHECKING:
    from compass_agent.runtime.turn import RuntimeTurn


class RunToolBindings:
    def __init__(
        self,
        executor: RuntimeTurn,
        state,
        route,
        trusted,
        control,
        controlled,
        emit,
        loader,
        discovered,
        supported=True,
        *,
        entries=(),
        workflow=False,
        catalog=None,
    ):
        self.entries = tuple(entries)
        self.assets_by_tool: dict[str, WorkflowAssetView] = {}
        self.workflow = workflow
        self.catalog = catalog
        self.subagent = SubAgent(self)
        self.workflow_tools = (
            WorkflowTools(self, executor.workflow_assets, executor.workflow_drafts)
            if workflow
            else None
        )
        self.executor, self.state, self.route = executor, state, route
        self.trusted, self.control, self.controlled, self.emit = trusted, control, controlled, emit
        self.loader, self.discovered, self.supported = loader, discovered, supported
        self.runtime: RunTools | None = None
        self.budget = controlled.budget if controlled else AutonomyBudget((), AutonomyLimits())
        from compass_agent.context_runtime.search_control import SearchDispatch, SearchProgress
        from compass_agent.execution_context import current_execution

        journal = current_execution.get()
        pin = (getattr(journal, "context_binding", None) or {}).get("search")
        self.search = SearchProgress(pin) if pin else None
        self.search_dispatch = (
            SearchDispatch(journal, pin, self.budget, self.restore_plan) if pin else None
        )
        if self.search and journal:
            journal.search_progress = self.search
            if controlled:
                controlled.search_progress = self.search
        self.artifacts: list[RunArtifact] = []
        self.todo_plan = controlled.plan if controlled else ExecutionPlan[TodoDefinition]()

    async def with_workflows(self, declarations, snapshot):
        assert self.workflow_tools is not None
        if snapshot is None:
            if len(declarations) >= TOOL_CATALOG_LIMIT:
                return
            assert self.workflow_tools.assets is not None
            assets = await asyncio.to_thread(
                self.workflow_tools.assets.active_tools,
                self.trusted,
                limit=TOOL_CATALOG_LIMIT - len(declarations),
            )
            candidates = [(asset, None) for asset in assets]
        else:
            candidates = []
            for name, (stored, metadata) in snapshot.items():
                if not is_asset_tool(name):
                    continue
                definition = ToolDefinition.model_validate(stored)
                function = definition.function
                label = metadata["workflowName"]
                asset = WorkflowAssetView(
                    assetId=UUID(hex=name.removeprefix(PREFIX)),
                    name=label,
                    status="active",
                    definitionVersion=1,
                    entryContract=DYNAMIC_CONTRACT,
                    metadataRevision=1,
                    parameters=function.parameters,
                )
                candidates.append((asset, definition))
        for asset, definition in candidates:
            entry = registration(asset, self.workflow_tools.reuse, declaration=definition)
            if entry.definition is None or not has_catalog_room([*declarations, entry.definition]):
                continue
            self.entries += (entry,)
            declarations.append(entry.definition)
            self.assets_by_tool[entry.name] = asset

    def freeze(self):
        if self.runtime is not None:
            return
        context = self.catalog or CatalogContext(
            self.executor.definition.builtin_tools,
            sandbox=self.loader.sandbox is not None,
            workflow=self.workflow,
        )
        entries = [
            *self.entries,
            *registrations(replace(context, workflow=self.workflow), self._handler),
        ]
        self.runtime = self.loader.assemble(
            entries if self.supported else (), self.discovered if self.supported else None
        )

        assert self.runtime is not None
        from compass_agent.execution_context import current_execution

        if journal := current_execution.get():
            journal.freeze_catalog(
                self.runtime.registry,
                asset_names={name: asset.name for name, asset in self.assets_by_tool.items()},
            )
        self.emit(
            StreamExtension(
                type="stream.extension",
                payload=RunExtensionPayload(
                    name="compass.tool.catalog",
                    version=1,
                    required=False,
                    data={
                        "catalogId": self.runtime.registry.catalog_id,
                        "loadedMcpTools": len(self.discovered.entries) if self.discovered else 0,
                        "skippedMcpConnections": self.discovered.summary.skipped
                        if self.discovered
                        else 0,
                    },
                ),
            )
        )

    def definitions(self):
        self.freeze()
        assert self.runtime is not None
        return self.runtime.registry.definitions()

    def restore_plan(self, prepared, context, receipt):
        from compass_agent.models.errors import ModelError
        from compass_agent.tools.builtin.todo_write.plan import TODO_ACTION

        parent = next(step for step in self.state.steps if step.id == context.step_id)
        self.todo_plan.apply(TODO_ACTION.validate_python(prepared.args), parent.index)
        if {"status": "ok", **self.todo_plan.snapshot()} != receipt.payload:
            raise ModelError("execution_replay_mismatch", 409)

    def _handler(self, name):
        if self.workflow_tools:
            if name == SUB_AGENT:
                return self.workflow_tools.subagent
            if name == CREATE_WORKFLOW:
                return self.workflow_tools.create

        async def invoke(args, context):
            if name != "todo_write":
                from compass_agent.tools.contracts import ToolRejected

                raise ToolRejected("unknown_tool")
            parent = next(step for step in self.state.steps if step.id == context.step_id)
            receipt = await execute_todo(self.todo_plan, args, context, parent.index)
            from compass_agent.runtime.session import RuntimeSession

            progress = self.controlled or RuntimeSession((), AutonomyLimits())
            progress.plan = self.todo_plan
            payload = receipt.payload if isinstance(receipt.payload, dict) else {}
            self.emit(
                progress.action_event(
                    str(parent.id),
                    next(
                        i
                        for i, call in enumerate(parent.tool_calls, 1)
                        if call.id == context.call_id
                    ),
                    args["op"],
                    payload.get("code", "ok"),
                    args.get("todoId"),
                )
            )
            return receipt

        return invoke

    def publish_execution(self, value: ToolExecutionEvent):
        assert self.runtime is not None
        record = {
            "runId": str(value.run_id),
            "stepId": str(value.step_id),
            "toolCallId": value.tool_call_id,
            "toolName": value.tool_name,
            "catalogId": self.runtime.registry.catalog_id,
            "environment": value.environment,
            "effect": value.effect,
            "approval": value.approval,
            "status": value.status,
            "cleanup": value.cleanup,
            "elapsedMs": value.elapsed_ms,
            "reasonCode": value.reason_code,
            "environmentId": value.environment_id,
        }
        if value.operation is not None:
            record["operation"] = value.operation
        logging.getLogger(__name__).info(
            "tool.execution.finished", extra={"tool_execution": record}
        )
        self.emit(
            StreamExtension(
                type="stream.extension",
                payload=RunExtensionPayload(
                    name="compass.tool.execution", version=1, required=False, data=record
                ),
            )
        )

    async def dispatch(self, step, definitions):
        self.freeze()
        assert self.runtime is not None
        runtime = self.runtime
        trace = self.state.trace
        visible = frozenset(tool.function.name for tool in definitions)
        from compass_agent.execution_context import current_execution

        journal = current_execution.get()
        if getattr(journal, "context_binding", None):
            from compass_agent.context_runtime.source_access import require_run_sources

            grants = await journal.capabilities() if journal.capabilities else {}
            if not grants.get("analysis") or not grants.get("model"):
                raise ToolRejected("permission_denied")
            if not grants.get("plugins"):
                visible = frozenset(n for n in visible if not n.startswith(("mcp_", "workflow_")))
            await asyncio.to_thread(
                require_run_sources,
                journal.store,
                journal.run_id,
                self.trusted,
                epoch=journal.epoch,
            )
        if self.controlled and step.index >= self.controlled.budget.limits.steps:
            raise AutonomyStopped("max_steps")

        def started(prepared: PreparedToolCall):
            if not trace or not (identifier := trace.calls.get((str(step.id), prepared.call_id))):
                return
            binding = prepared.tool.binding
            fields: dict[str, object] = {}
            if prepared.tool.name == "goal" and isinstance(prepared.args, dict):
                fields["title"] = {
                    "propose": "确认长任务目标",
                    "record": "记录目标进度",
                    "complete": "核对目标完成条件",
                    "block": "记录目标阻碍",
                }.get(prepared.args.get("op", ""), "更新目标")
            if (
                not self.search
                and not prepared.tool.completion
                and prepared.tool.public_projection == "normal"
            ):
                fields["detail"] = trace.tool_detail(
                    identifier, input_json=json_bytes(prepared.args).decode()
                )
            if isinstance(binding, McpBinding):
                fields["origin"] = ExecutionOrigin(
                    kind="mcp", toolName=binding.remote_name, serverId=str(binding.connection_id)
                )
                fields["title"] = binding.remote_name[:160]
            elif isinstance(binding, SandboxBinding):
                fields["executor"] = ExecutionEnvironment(kind="sandbox", runtime="Python")
            if self.workflow_tools and (
                prepared.tool.name in {SUB_AGENT, CREATE_WORKFLOW}
                or prepared.tool.name in self.assets_by_tool
            ):
                self.workflow_tools.start(prepared, identifier, **fields)
            else:
                trace.begin_call(str(step.id), prepared.call_id, **fields)

        def event(value: ToolExecutionEvent):
            if value.status in {"ok", "empty"} and (
                value.environment == "mcp" or value.tool_name in self.assets_by_tool
            ):
                from compass_agent.context_runtime.source_access import record_source
                from compass_agent.execution_context import current_execution

                journal = current_execution.get()
                if getattr(journal, "context_binding", None):
                    record_source(journal, {"kind": "transient"})
            self.publish_execution(value)
            if trace and (identifier := trace.calls.get((str(step.id), value.tool_call_id))):
                entry = runtime.registry.entries.get(value.tool_name)
                binding = entry.binding if entry else None
                fields = {}
                if isinstance(binding, McpBinding):
                    fields["origin"] = ExecutionOrigin(
                        kind="mcp",
                        toolName=binding.remote_name,
                        serverId=str(binding.connection_id),
                    )
                    fields["title"] = binding.remote_name[:160]
                if value.environment == "sandbox":
                    fields["executor"] = ExecutionEnvironment(
                        kind="sandbox", runtime="Python", environmentId=value.environment_id
                    )
                    fields["title"] = "执行 Python 分析"
                output = value.output_json
                if output is None and value.reason_code:
                    output = json_bytes(
                        {"status": value.status, "code": value.reason_code}
                    ).decode()
                if not self.search and not (
                    entry and (entry.completion or entry.public_projection == "status_only")
                ):
                    fields["detail"] = trace.tool_detail(identifier, output_json=output)
                status = {
                    "ok": "completed",
                    "empty": "empty",
                    "waiting": "waiting",
                    "unknown": "incomplete",
                    "canceled": "canceled",
                }.get(value.status, "failed")
                trace.finish(
                    identifier,
                    status=status,
                    summary=("执行环境清理未确认" if value.cleanup == "unconfirmed" else None),
                    **fields,
                )

        context = ToolExecutionContext(
            self.trusted,
            self.control.id,
            step.id,
            self.control,
            self.budget,
            visible,
            sources=runtime.sources,
            emit=event,
            on_start=started,
            search_dispatch=self.search_dispatch.invoke if self.search_dispatch else None,
            observation=getattr(self.executor.models, "root_observation", None),
        )
        policy = runtime.policy
        # Tools that declare ordered state changes run serially.
        if any(
            not runtime.registry.entries[name].parallel_safe
            for name in visible
            if name in {call.function.name for call in step.tool_calls}
        ):
            policy = replace(policy, mode="serial")
        results = await self.control.perform(
            lambda: dispatch_batch(runtime.dispatcher, step.tool_calls, context, policy)
        )
        observations = {}
        if (getattr(journal, "context_binding", None) or {}).get("answerProtocol") == 2:
            from compass_agent.context_runtime.evidence import after_tool_batch

            admitted = await asyncio.to_thread(
                after_tool_batch, journal, step, results, runtime.registry, self.trusted
            )
            observations = {item["callId"]: item for item in admitted}
        self.last_results = results
        if self.search and all(
            r.cleanup != "unconfirmed" and r.status not in {"waiting", "canceled", "timeout"}
            for r in results
        ):
            from compass_agent.context_runtime.search_verification import verify_batch

            observations = await asyncio.to_thread(
                verify_batch, journal, step, results, self.trusted
            )
            self.search.record(step, results, observations, self.budget)
        waiting = None
        for result in results:
            payload = (
                result.payload
                if result.payload is not None
                else {"status": result.status, "code": result.reason_code}
            )
            self.artifacts.extend(
                RunArtifact(
                    id=ref.id,
                    name="chart.png",
                    mediaType="image/png",
                    byteSize=ref.byte_size,
                    stepId=str(ref.step_id),
                    toolCallId=ref.tool_call_id,
                )
                for ref in result.artifacts
            )
            if result.artifacts or result.artifact_status != "none":
                payload = {
                    "result": payload,
                    "artifactStatus": result.artifact_status,
                    "artifacts": [
                        {
                            "id": ref.id,
                            "name": ref.name,
                            "mediaType": ref.media_type,
                            "byteSize": ref.byte_size,
                        }
                        for ref in result.artifacts
                    ],
                }
            observation = json_bytes(payload).decode()
            if result.call_id in observations and (
                observations[result.call_id]["evidence"]
                or observations[result.call_id].get("feedback")
            ):
                from compass_agent.context_runtime.evidence import model_observation

                observation = json_bytes(
                    model_observation(payload, observations[result.call_id])
                ).decode()
            self.state.record_tool_result(
                step,
                ToolResult(
                    result.call_id,
                    "ok" if result.status in {"ok", "empty"} else "error",
                    observation,
                ),
            )
            if result.cleanup == "unconfirmed":
                raise ResourceUnconfirmed()
            if result.status in {"canceled", "timeout"}:
                if result.status == "timeout":
                    raise AutonomyStopped("execution_timeout")
                raise PhaseCanceled(cleanup_confirmed=True)
            if result.proposal is not None:
                waiting = RunWaiting(
                    status="waiting",
                    runId=str(self.control.id),
                    waitId=str(uuid5(self.control.id, str(step.id) + result.call_id)),
                    nodeKey="tool_approval",
                    continuation="execute_tool",
                    proposal=result.proposal,
                )
        if journal and getattr(journal, "context_results", None) and waiting is None:
            from compass_agent.context_runtime.history import record_batch
            from compass_agent.execution_context import execution_path

            restored = await record_batch(
                journal, step, self.state.messages[-len(results) - 1 :], execution_path.get()
            )
            if restored is not None:
                self.state.messages[-len(results) - 1 :] = restored
        return waiting
