"""Whole-batch preflight and bounded scheduling with original-order results."""

import asyncio
from dataclasses import replace

from compass_agent.schemas import ToolCall
from compass_agent.tools.contracts import (
    PhaseCanceled,
    PreparedToolCall,
    ToolBatchPolicy,
    ToolExecutionContext,
    ToolExecutionEvent,
    ToolExecutionResult,
    ToolRejected,
)
from compass_agent.tools.dispatcher import ToolDispatcher
from compass_agent.tools.errors import ActionableToolRejected

REVOKED = {
    "mcp_configuration_stale",
    "tool_config_changed",
    "mcp_catalog_stale",
    "mcp_scope_denied",
}


async def dispatch_batch(
    dispatcher: ToolDispatcher,
    calls: tuple[ToolCall, ...],
    context: ToolExecutionContext,
    policy: ToolBatchPolicy,
) -> tuple[ToolExecutionResult, ...]:
    if not calls or len(calls) > 16 or len({c.id for c in calls}) != len(calls):
        raise ToolRejected("invalid_batch")
    from compass_agent.execution_context import current_execution

    journal = current_execution.get()
    if (getattr(journal, "context_binding", None) or {}).get("answerProtocol") == 3 and len(
        calls
    ) > 1:
        work_calls = tuple(call for call in calls if call.function.name != "final_answer")
        if len(work_calls) != len(calls):
            executed = (
                await dispatch_batch(dispatcher, work_calls, context, policy) if work_calls else ()
            )
            by_id = {result.call_id: result for result in executed}
            for call in calls:
                if call.id not in by_id:
                    context.emit(
                        ToolExecutionEvent(
                            context.run_id,
                            context.step_id,
                            call.id,
                            call.function.name,
                            "none",
                            "read_only",
                            "none",
                            "rejected",
                            "not_started",
                            0,
                            "completion_deferred",
                        )
                    )
            return tuple(
                by_id.get(call.id)
                or ToolExecutionResult(
                    call.id,
                    "rejected",
                    reason_code="completion_deferred",
                    payload={
                        "errorCode": "completion_deferred",
                        "message": "同批工作调用已处理，请根据结果继续。",
                        "nextAction": "工作完成后单独调用 final_answer。",
                    },
                )
                for call in calls
            )
    # Older protocols retain their original whole-batch completion rule.
    if len(calls) > 1 and any(
        entry.completion
        for call in calls
        if (entry := dispatcher.registry.entries.get(call.function.name)) is not None
    ):
        rejected = tuple(
            ToolExecutionResult(call.id, "rejected", reason_code="completion_requires_single_call")
            for call in calls
        )
        for call, result in zip(calls, rejected, strict=True):
            context.emit(
                ToolExecutionEvent(
                    context.run_id,
                    context.step_id,
                    call.id,
                    call.function.name,
                    "none",
                    "unknown",
                    "required",
                    result.status,
                    result.cleanup,
                    0,
                    result.reason_code,
                )
            )
        return rejected
    emitted: set[str] = set()
    publish = context.emit

    def record(event):
        emitted.add(event.tool_call_id)
        publish(event)

    context = replace(context, emit=record)
    results: list[ToolExecutionResult | None] = [None] * len(calls)
    prepared: list[PreparedToolCall | None] = [None] * len(calls)
    try:
        for index, call in enumerate(calls):
            try:
                item = await dispatcher.prepare(call, context)
                prepared[index] = item
                if item.tool.local_effect and len(calls) != 1:
                    raise ToolRejected("tool_requires_independent_call")
                await dispatcher.revalidate(item, context)
                item = await dispatcher.prepare_approval(item, context)
                prepared[index] = item
                if item.approval is not None and len(calls) != 1 and not item.tool.local_effect:
                    raise ToolRejected("tool_requires_independent_call")
                results[index] = dispatcher.admission(item, context)
                if (
                    results[index] is None
                    and policy.mode == "parallel"
                    and not item.tool.parallel_safe
                ):
                    results[index] = ToolExecutionResult(
                        call.id, "rejected", reason_code="parallel_not_supported"
                    )
            except ToolRejected as error:
                results[index] = ToolExecutionResult(
                    call.id,
                    "rejected",
                    reason_code=error.code,
                    payload=error.error.payload()
                    if isinstance(error, ActionableToolRejected)
                    else None,
                )
    except asyncio.CancelledError:
        raise PhaseCanceled(cleanup_confirmed=context.control.cleanup_confirmed) from None
    waiting = any(result and result.status == "waiting" for result in results)
    rejected = any(result and result.status != "waiting" for result in results)
    revoked = any(result and result.reason_code in REVOKED for result in results)
    if waiting or revoked or rejected and policy.failure_mode == "stop":
        reason = "batch_waiting" if waiting else "batch_preflight_failed"
        results = [
            result or ToolExecutionResult(call.id, "rejected", reason_code=reason)
            for call, result in zip(calls, results, strict=True)
        ]
    for call, item, result in zip(calls, prepared, results, strict=True):
        if result is not None:
            context.emit(
                ToolExecutionEvent(
                    context.run_id,
                    context.step_id,
                    call.id,
                    call.function.name,
                    item.tool.binding.environment if item and item.tool.binding else "none",
                    item.tool.policy.effect if item else "unknown",
                    item.tool.policy.approval if item else "required",
                    result.status,
                    result.cleanup,
                    0,
                    result.reason_code,
                )
            )
    if all(result is not None for result in results):
        return tuple(result for result in results if result is not None)

    stopped = False
    cursor = 0
    workers: list[asyncio.Task] = []

    async def work():
        nonlocal cursor, stopped
        while cursor < len(calls) and not stopped:
            index = cursor
            cursor += 1
            if results[index] is not None:
                continue
            item = prepared[index]
            assert item is not None
            try:
                result = await dispatcher.execute(item, replace(context, call_id=item.call_id))
                results[index] = result
                must_stop = (
                    result.cleanup == "unconfirmed"
                    or result.reason_code in REVOKED
                    or result.status in {"canceled", "timeout"}
                    or (policy.failure_mode == "stop" and result.status not in {"ok", "empty"})
                )
                if must_stop:
                    stopped = True
                    for task in workers:
                        if (
                            task is not asyncio.current_task()
                            and not task.done()
                            and not task.cancelling()
                        ):
                            task.cancel()
            except BaseException:
                stopped = True
                for task in workers:
                    if (
                        task is not asyncio.current_task()
                        and not task.done()
                        and not task.cancelling()
                    ):
                        task.cancel()
                raise

    count = 1 if policy.mode == "serial" else min(policy.max_concurrency, len(calls))
    workers = [asyncio.create_task(work()) for _ in range(count)]
    settled_workers = asyncio.gather(*workers, return_exceptions=True)
    try:
        # Let this scope signal workers once and wait for their cleanup receipts.
        outcomes = await asyncio.shield(settled_workers)
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, asyncio.CancelledError
            ):
                raise outcome
    except BaseException as error:
        for task in workers:
            if not task.done() and not task.cancelling():
                task.cancel()
        await settled_workers
        for call, item, result in zip(calls, prepared, results, strict=True):
            if call.id not in emitted:
                context.emit(
                    ToolExecutionEvent(
                        context.run_id,
                        context.step_id,
                        call.id,
                        call.function.name,
                        item.tool.binding.environment if item and item.tool.binding else "none",
                        item.tool.policy.effect if item else "unknown",
                        item.tool.policy.approval if item else "required",
                        result.status if result else "rejected",
                        result.cleanup if result else "not_started",
                        0,
                        result.reason_code if result else "batch_stopped",
                    )
                )
        if isinstance(error, asyncio.CancelledError):
            raise PhaseCanceled(cleanup_confirmed=context.control.cleanup_confirmed) from None
        raise
    settled = tuple(
        result or ToolExecutionResult(call.id, "rejected", reason_code="batch_stopped")
        for call, result in zip(calls, results, strict=True)
    )
    for call, item, result in zip(calls, prepared, settled, strict=True):
        if call.id not in emitted:
            context.emit(
                ToolExecutionEvent(
                    context.run_id,
                    context.step_id,
                    call.id,
                    call.function.name,
                    item.tool.binding.environment if item and item.tool.binding else "none",
                    item.tool.policy.effect if item else "unknown",
                    item.tool.policy.approval if item else "required",
                    result.status,
                    result.cleanup,
                    0,
                    result.reason_code,
                )
            )
    return settled
