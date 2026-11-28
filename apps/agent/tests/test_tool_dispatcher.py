"""Policy and lifecycle behavior shared by all tool execution environments."""

import asyncio
import hashlib
import struct
import zlib
from dataclasses import replace
from uuid import uuid4

import pytest
from compass_agent.runtime.control import OwnerKey, RunRegistry
from compass_agent.schemas import (
    ToolApprovalProposal,
    ToolCall,
    ToolCallFunction,
    ToolDefinition,
    ToolFunction,
)
from compass_agent.tools.artifacts import ArtifactStore
from compass_agent.tools.batch import dispatch_batch
from compass_agent.tools.contracts import (
    ExecutionReceipt,
    InProcessBinding,
    RegisteredTool,
    ToolBatchPolicy,
    ToolExecutionContext,
    ToolPolicy,
    ToolRejected,
)
from compass_agent.tools.dispatcher import ExecutionSlots, ToolDispatcher
from compass_agent.tools.registry import ToolRegistry, bounded_payload, parse_arguments

from tests.test_agent_run import CHAT


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Budget:
    calls = 0

    def start_tool(self):
        self.calls += 1


def context(*names, trusted=CHAT):
    control = RunRegistry().register(OwnerKey.from_context(trusted))
    return ToolExecutionContext(trusted, control.id, uuid4(), control, Budget(), frozenset(names))


def call(name, identifier=None, args="{}"):
    return ToolCall(
        id=identifier or str(uuid4()), function=ToolCallFunction(name=name, arguments=args)
    )


def registration(name, handler, *, policy=None, proposal=None):
    policy = policy or ToolPolicy()

    def authorize(a, c):
        _ = c.owner

    return RegisteredTool(
        name,
        ToolDefinition(
            type="function",
            function=ToolFunction(
                name=name,
                description="test",
                parameters={"type": "object", "additionalProperties": False},
            ),
        ),
        policy,
        InProcessBinding(handler),
        parse_arguments,
        authorize,
        bounded_payload,
        prepare_proposal=proposal,
        parallel_safe=True,
    )


async def ok(args, ctx):
    return ExecutionReceipt(ctx.call_id, "ok", "confirmed", {"value": 1})


@pytest.mark.anyio
async def test_read_sql_preflight_rejection_keeps_worker_usable():
    from compass_agent.tools.builtin.database_query.slim import query_relations

    async def rejected_sql(args, ctx):
        query_relations("DELETE FROM orders")
        raise AssertionError("rejected SQL reached execution")

    d = dispatcher(registration("query", rejected_sql), registration("read", ok))
    c = context("query", "read")
    result = await dispatch_batch(d, (call("query"),), c, ToolBatchPolicy())
    assert result[0].status == "rejected"
    assert result[0].cleanup == "not_started"
    assert isinstance(result[0].payload, dict)
    assert result[0].payload["category"] == "fix_arguments"
    recovered = await dispatch_batch(d, (call("read"),), c, ToolBatchPolicy())
    assert recovered[0].status == "ok"


def dispatcher(*entries):
    return ToolDispatcher(ToolRegistry(entries), ExecutionSlots(), ArtifactStore())


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        '{"a":1,"a":2}',
        '{"a": NaN}',
        '{"a": Infinity}',
        "{",
        '{"a":' + "[" * 22 + "1" + "]" * 22 + "}",
    ],
)
def test_arguments_reject_ambiguous_or_unbounded_input(raw):
    with pytest.raises(ToolRejected):
        parse_arguments(raw)


def test_external_effect_requires_approval_independently_of_binding():
    with pytest.raises(ValueError):
        ToolPolicy("external_write", "none")
    assert ToolPolicy("read_only", "required").approval == "required"
    definition = registration("read", ok)
    registry = ToolRegistry([definition])
    assert definition.definition is not None
    definition.definition.function.parameters["type"] = "array"
    assert registry.definitions()[0].function.parameters["type"] == "object"
    with pytest.raises(ToolRejected, match="tool_name_conflict"):
        ToolRegistry([registration("read", ok), registration("read", ok)])


@pytest.mark.anyio
async def test_unexposed_and_invalid_tool_fail_before_execution():
    d = dispatcher(registration("read", ok))
    c = context()
    result = await dispatch_batch(d, (call("read"),), c, ToolBatchPolicy())
    assert result[0].reason_code == "tool_not_exposed"
    assert isinstance(c.budget, Budget)
    assert c.budget.calls == 0
    c = context("read")
    result = await dispatch_batch(d, (call("read", args='{"extra":1}'),), c, ToolBatchPolicy())
    assert result[0].reason_code == "invalid_parameters"
    assert isinstance(c.budget, Budget)
    assert c.budget.calls == 0


@pytest.mark.anyio
async def test_approval_pauses_entire_batch_before_any_dispatch():
    def proposal(a, c):
        return ToolApprovalProposal(
            proposalId=str(uuid4()),
            toolName="write",
            toolCallId=c.call_id,
            argumentsDigest=hashlib.sha256(b"{}").hexdigest(),
            summary="待审批",
        )

    d = dispatcher(
        registration("read", ok),
        registration(
            "write", ok, policy=ToolPolicy("external_write", "required"), proposal=proposal
        ),
    )
    c = context("read", "write")
    result = await dispatch_batch(
        d, (call("read"), call("write")), c, ToolBatchPolicy(mode="parallel")
    )
    assert [r.status for r in result] == ["rejected", "waiting"]
    assert isinstance(c.budget, Budget)
    assert c.budget.calls == 0 and d.slots.active == 0


@pytest.mark.anyio
async def test_parallel_overlap_is_bounded_and_results_keep_model_order():
    active = maximum = 0
    reached = asyncio.Event()
    events = []

    async def handler(a, c):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            reached.set()
        await reached.wait()
        if c.call_id == "first":
            await asyncio.sleep(0.025)
        active -= 1
        return ExecutionReceipt(c.call_id, "ok", "confirmed", {"call": c.call_id})

    d = dispatcher(registration("read", handler))
    c = replace(context("read"), emit=events.append)
    calls = tuple(call("read", name) for name in ["first", "second", "third"])
    results = await dispatch_batch(d, calls, c, ToolBatchPolicy(mode="parallel"))
    assert maximum == 2 and d.slots.active == 0
    assert [r.call_id for r in results] == ["first", "second", "third"]
    assert events[0].tool_call_id == "second"
    assert isinstance(c.budget, Budget)
    assert c.budget.calls == 3


@pytest.mark.anyio
async def test_serial_stop_and_continue_are_explicit_policies():
    invoked = []

    async def handler(a, c):
        invoked.append(c.call_id)
        return ExecutionReceipt(c.call_id, "error" if c.call_id == "bad" else "ok", "confirmed", {})

    d = dispatcher(registration("read", handler))
    calls = (call("read", "bad"), call("read", "good"))
    results = await dispatch_batch(d, calls, context("read"), ToolBatchPolicy())
    assert invoked == ["bad"] and results[1].reason_code == "batch_stopped"
    invoked.clear()
    results = await dispatch_batch(
        d, calls, context("read"), ToolBatchPolicy(failure_mode="continue")
    )
    assert invoked == ["bad", "good"] and results[1].status == "ok"


@pytest.mark.anyio
async def test_unknown_execution_quarantines_capacity_even_with_continue():
    async def unknown(a, c):
        return ExecutionReceipt("resource-1", "unknown", "unconfirmed")

    d = dispatcher(registration("read", unknown))
    c = context("read")
    results = await dispatch_batch(
        d, (call("read"), call("read")), c, ToolBatchPolicy(failure_mode="continue")
    )
    assert results[0].cleanup == "unconfirmed" and results[1].status == "rejected"
    assert not c.control.cleanup_confirmed
    assert d.slots.active == d.slots.quarantined == 1
    await d.slots.confirm_cleanup("resource-1")
    assert d.slots.active == d.slots.quarantined == 0


@pytest.mark.anyio
async def test_cancel_waits_for_backend_cleanup_and_drops_late_payload():
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def slow(a, c):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)
            cleaned.set()
            return ExecutionReceipt(c.call_id, "canceled", "confirmed")

    d = dispatcher(registration("read", slow))
    c = context("read")
    task = asyncio.create_task(dispatch_batch(d, (call("read"),), c, ToolBatchPolicy()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set() and d.slots.active == 0 and c.control.cleanup_confirmed


@pytest.mark.anyio
async def test_cancel_during_preflight_has_no_dispatch_or_quarantined_slot():
    from compass_agent.tools.contracts import PhaseCanceled

    checking = asyncio.Event()

    async def revalidate(ctx):
        checking.set()
        await asyncio.Event().wait()

    d = dispatcher(replace(registration("read", ok), revalidate=revalidate))
    c = context("read")
    task = asyncio.create_task(dispatch_batch(d, (call("read"),), c, ToolBatchPolicy()))
    await checking.wait()
    task.cancel()
    with pytest.raises(PhaseCanceled) as canceled:
        await task
    assert isinstance(c.budget, Budget)
    assert canceled.value.cleanup_confirmed and c.budget.calls == 0 and d.slots.active == 0


@pytest.mark.anyio
async def test_unbounded_failure_payload_is_rejected_with_confirmed_cleanup():
    async def failure(args, ctx):
        return ExecutionReceipt(ctx.call_id, "error", "confirmed", {"detail": "x" * 70000})

    d = dispatcher(registration("read", failure))
    (result,) = await dispatch_batch(d, (call("read"),), context("read"), ToolBatchPolicy())
    assert result.status == "error" and result.reason_code == "invalid_tool_result"
    assert result.cleanup == "confirmed" and result.payload is None and d.slots.active == 0


def png():
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )


@pytest.mark.anyio
async def test_artifact_acceptance_requires_valid_png_and_full_owner():
    c = context("read")
    store = ArtifactStore()
    ref = store.register(png(), c)
    assert store.read(ref.id, c.owner, c.run_id).content == png()
    with pytest.raises(ToolRejected):
        store.read(ref.id, (uuid4(), *c.owner[1:]), c.run_id)
    with pytest.raises(ToolRejected):
        store.register(png() + b"extra", replace(c, call_id="other"))


@pytest.mark.anyio
async def test_input_adapter_keeps_blocking_reads_off_loop_and_preserves_scope():
    from contextvars import ContextVar
    from threading import get_ident

    marker = ContextVar[str | None]("adapter_test_scope", default=None)
    marker.set("current-run")
    loop_thread = get_ident()

    async def authorized_read():
        assert marker.get() == "current-run"
        return {}

    def normalize(args, ctx):
        assert get_ident() != loop_thread
        return asyncio.run(authorized_read())

    entry = replace(registration("read", ok), normalize_input=normalize)
    d = dispatcher(entry)
    c = context("read")
    result = await dispatch_batch(d, (call("read"),), c, ToolBatchPolicy())
    assert result[0].status == "ok"


@pytest.mark.anyio
async def test_canceling_input_adapter_joins_pending_read_before_return():
    from threading import Event

    from compass_agent.tools.contracts import PhaseCanceled
    from compass_agent.tools.input_adapter import normalize_arguments

    started, release, finished = Event(), Event(), Event()

    def normalize(args, ctx):
        started.set()
        assert release.wait(5)
        finished.set()
        return args

    task = asyncio.create_task(normalize_arguments(normalize, {}, None))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not finished.is_set()
    release.set()
    with pytest.raises(PhaseCanceled) as canceled:
        await task
    assert canceled.value.cleanup_confirmed and finished.is_set()
