"""Per-Run database registration and authorized query-result inputs."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import cast

from compass_agent.data_connections.transport import AuthorizedDatabaseBinding
from compass_agent.schemas import (
    RUN_STREAM_LIMITS,
    RunToolError,
    ToolCall,
    ToolCallFunction,
    ToolEmptyPayload,
    ToolFailedPayload,
    ToolFinished,
    ToolStarted,
    ToolStartedPayload,
    ToolSucceededPayload,
)
from compass_agent.semantic.evidence import result as evidence_result
from compass_agent.tools.builtin.database_query import (
    _FUNCTIONS,
    DATABASE_ACTION,
    DatabaseResult,
    ListConnections,
    definition,
    fingerprint,
    query_database,
)
from compass_agent.tools.contracts import (
    AuthorizedInput,
    ExecutionReceipt,
    InProcessBinding,
    PhaseCanceled,
    RegisteredTool,
    ToolPolicy,
    ToolRejected,
    json_bytes,
)
from compass_agent.tools.contracts import (
    preview as bounded_preview,
)
from compass_agent.tools.registry import bounded_payload, parse_arguments


async def finish_bookkeeping(operation, *, timeout_s: float = 8):
    """Drain local source-recording transactions after the physical query has settled."""
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        done, _ = await asyncio.wait({task}, timeout=timeout_s)
        if done:
            task.exception()
        else:
            task.add_done_callback(
                lambda finished: None if finished.cancelled() else finished.exception()
            )
        raise PhaseCanceled(cleanup_confirmed=bool(done)) from None


def registration(
    bindings: tuple[AuthorizedDatabaseBinding, ...],
    *,
    emit=lambda event: None,
    query=None,
    current: Callable[[], Awaitable[tuple[AuthorizedDatabaseBinding, ...]]] | None = None,
) -> RegisteredTool:
    indices: dict[object, int] = {}

    def authorize(args, context):
        DATABASE_ACTION.validate_python(args)
        if any(
            (binding.tenant_id, binding.workspace_id) != context.owner[:2] for binding in bindings
        ):
            raise ToolRejected("tool_scope_denied")

    async def execute(args, context):
        from compass_agent.execution_context import current_execution

        journal = current_execution.get()
        action = DATABASE_ACTION.validate_python(args)
        if isinstance(action, ListConnections):
            visible = tuple(
                b
                for b in bindings
                if context.connection_ids is None
                or str(b.connection_id or b.workspace_id) in context.connection_ids
            )
            if getattr(journal, "search_snapshot", None):
                from compass_agent.context_runtime.source_access import record_sources

                authorized = await current() if current else ()
                visible = tuple(b for b in visible if b in authorized)
                await finish_bookkeeping(
                    asyncio.to_thread(
                        record_sources,
                        journal,
                        [
                            {
                                "kind": "metadata",
                                "connection": str(b.connection_id or b.workspace_id),
                                "tenant": str(b.tenant_id),
                                "workspace": str(b.workspace_id),
                                "revision": b.revision,
                                "role": b.role,
                                "columns": {},
                            }
                            for b in visible
                        ],
                    )
                )
            return ExecutionReceipt(
                context.call_id,
                "ok",
                "confirmed",
                {
                    "connections": [
                        {
                            "id": str(item.connection_id or item.workspace_id),
                            "name": item.name,
                            "engine": "postgresql",
                        }
                        for item in visible
                    ]
                },
            )
        allowed_bindings = tuple(
            b
            for b in bindings
            if context.connection_ids is None
            or str(b.connection_id or b.workspace_id) in context.connection_ids
        )
        available = {
            str(item.connection_id or item.workspace_id): item for item in allowed_bindings
        }
        binding = (
            available.get(action.connection_id)
            if action.connection_id
            else (
                available.get(str(context.owner[1]))
                or (allowed_bindings[0] if len(allowed_bindings) == 1 else None)
            )
        )
        if binding is None:
            return ExecutionReceipt(
                context.call_id,
                "error",
                "confirmed",
                {
                    "status": "error",
                    "code": "connection_required" if bindings else "database_unavailable",
                },
            )

        async def revalidate():
            return current is None or binding in await current()

        if not await revalidate():
            raise ToolRejected("database_source_stale")
        call = ToolCall(
            id=context.call_id,
            function=ToolCallFunction(
                name="database_query",
                arguments=json_bytes(args).decode(),
            ),
        )
        index = indices.get(context.step_id, 0) + 1
        indices[context.step_id] = index
        common = {"stepId": str(context.step_id), "toolCallId": context.call_id, "toolIndex": index}
        emit(
            ToolStarted(
                type="tool.started",
                payload=ToolStartedPayload(
                    **common,
                    toolKind="database_query",
                    params=None,
                    inputPreview=call.function.arguments,
                ),
            )
        )
        result = await context.control.perform(
            lambda: (query or query_database)(call, binding, context.control), timeout_s=8
        )
        if not isinstance(result, DatabaseResult):
            failure = {"status": "error", "code": result}
            if result == "invalid_parameters":
                failure.update(
                    repair=(
                        "请依据 list_tables / describe_table 返回的业务表和字段修正单条 SELECT。"
                        "元信息使用上述工具操作读取；普通函数调用支持 supportedFunctions 中的名字，"
                        "也可直接选择原始列。"
                    ),
                    supportedFunctions=sorted(_FUNCTIONS),
                )
            emit(
                ToolFinished(
                    type="tool.finished",
                    payload=ToolFailedPayload(
                        **common,
                        status="error",
                        errorCode=cast(RunToolError, result),
                        resultPreview=json_bytes(failure).decode(),
                    ),
                )
            )
            return ExecutionReceipt(context.call_id, "error", "confirmed", failure)
        payload = json.loads(result.observation())
        from compass_agent.context_runtime.source_access import record_source

        witness = result.source_ref or {"kind": "transient"}
        if journal and (
            (getattr(journal, "context_binding", None) or {}).get("registry")
            or getattr(journal, "search_snapshot", None)
        ):
            from compass_agent.semantic.service import remember_open

            retained = await finish_bookkeeping(
                asyncio.to_thread(remember_open, journal, context, args, result, binding)
            )
            if retained:
                ref, witness = retained
                payload.update(
                    {"calculation_mode": "open_sql", "resultId": ref, "semantic_result_ref": ref}
                )
        if journal and getattr(journal, "context_binding", None):
            await finish_bookkeeping(asyncio.to_thread(record_source, journal, witness))
        context.sources[payload["resultId"]] = AuthorizedInput(
            context.run_id, context.owner, payload, revalidate
        )
        manifest = result.context_result
        if manifest is not None and journal and journal.context_results:
            payload = journal.context_results.files.receipt(manifest, metadata=payload)
        preview = (
            json_bytes(payload).decode()
            if manifest
            else result.observation(include_result_id=False)
        )
        if len(preview.encode()) > RUN_STREAM_LIMITS["toolResultBytes"]:
            preview = bounded_preview(preview)
        payload_event = (
            ToolSucceededPayload(
                **common,
                status="ok",
                rowCount=manifest["returnedCount"] if manifest else len(result.rows),
                resultPreview=preview,
            )
            if (manifest["returnedCount"] if manifest else len(result.rows))
            else ToolEmptyPayload(**common, status="empty", rowCount=0, resultPreview=preview)
        )
        emit(ToolFinished(type="tool.finished", payload=payload_event))
        return ExecutionReceipt(
            context.call_id,
            "ok",
            "confirmed",
            payload,
            context_result=manifest,
            source_deps=(witness,),
        )

    return RegisteredTool(
        "database_query",
        definition(),
        ToolPolicy(),
        InProcessBinding(execute),
        parse_arguments,
        authorize,
        bounded_payload,
        parallel_safe=True,
        evidence_adapter=evidence_result,
        fingerprint=fingerprint,
    )
