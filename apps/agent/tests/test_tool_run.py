"""Actual model-loop integration of the frozen catalog, policy and result projection."""

import asyncio
import json
from typing import Any, cast
from uuid import uuid4

import pytest
from compass_agent.runtime.context import compass_definition
from compass_agent.runtime.control import OwnerKey, RunRegistry
from compass_agent.runtime.service import RunService
from compass_agent.schemas import TOOL_CATALOG_LIMIT, StreamExtension, ToolApprovalProposal
from compass_agent.tools.builtin.database_query import DatabaseResult
from compass_agent.tools.builtin.database_query.handler import registration as database_registration
from compass_agent.tools.contracts import ExecutionReceipt, ToolBatchPolicy
from compass_agent.tools.loader import ToolLoader
from compass_agent.tools.mcp.client import EndpointPolicy
from compass_agent.tools.mcp.service import tool_name

from .test_agent_run import (
    CHAT,
    FakeModels,
    FakeRouting,
    binding,
    model_result,
    product_call,
    run_command,
    selected_route,
)
from .test_mcp_tools import Repository, configuration, fixture_server
from .test_tool_dispatcher import call, ok, png, registration


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("approval", [False, True])
async def test_actual_run_discovers_personal_mcp_without_retail_and_waits_before_effect(approval):
    async with fixture_server() as (endpoint, invoked):
        config = configuration(endpoint, selected=not approval)
        repo = Repository(config)
        name = tool_name(config.id, "add")
        models = FakeModels(
            [
                model_result(
                    tool_calls=(call(name, "sum", '{"left":2,"right":3}'),),
                    token="continuation",
                    finish="tool_calls",
                ),
                model_result(text="计算结果是 5。"),
            ]
        )
        loader = ToolLoader(
            repository=repo, endpoints=EndpointPolicy("", endpoint.removesuffix("/mcp"))
        )
        executor = RunService(
            cast(Any, models),
            cast(Any, FakeRouting(selected_route())),
            tool_loader=loader,
            definition=compass_definition(),
        )
        control = RunRegistry().register(OwnerKey.from_context(CHAT))
        events = []
        result = await executor.run(run_command(), CHAT, control, events.append)
        assert [t.function.name for t in models.requests[0][1].tools] == ["todo_write", name]
        assert "apply_budget" not in {t.function.name for t in models.requests[0][1].tools}
        if approval:
            assert result.status == "waiting" and isinstance(result.proposal, ToolApprovalProposal)
            assert invoked == [] and len(models.requests) == 1
        else:
            assert result.status == "completed" and invoked == [(2, 3)]
            assert [t.function.name for t in models.requests[1][1].tools] == ["todo_write", name]
            payload = json.loads(models.requests[1][1].messages[-1].content)
            assert payload["structuredContent"] == {"sum": 5}
        nodes = [
            event.payload.data
            for event in events
            if isinstance(event, StreamExtension) and event.payload.name == "compass.execution.node"
        ]
        assert any(
            isinstance(origin := node.get("origin"), dict) and origin.get("kind") == "mcp"
            for node in nodes
        )
        assert loader.slots.active == 0


@pytest.mark.anyio
@pytest.mark.parametrize("row_count", [20, 192])
async def test_actual_run_registers_full_query_source_and_accepts_png_after_backend_cleanup(
    monkeypatch,
    row_count,
):
    class Models(FakeModels):
        async def run(self, prepared, request, **kwargs):
            if len(self.requests) == 2:
                source = json.loads(request.messages[-1].content)["resultId"]
                self.results.insert(
                    0,
                    model_result(
                        tool_calls=(
                            call(
                                "python_analysis",
                                "chart",
                                json.dumps(
                                    {"source_query_id": source, "code": "result = {'total': 7}"}
                                ),
                            ),
                        ),
                        token="second",
                        finish="tool_calls",
                    ),
                )
            return await super().run(prepared, request, **kwargs)

    class Backend:
        calls = 0

        async def execute(self, binding, args, ctx):
            self.calls += 1
            source = ctx.sources[args["source_query_id"]]
            assert source.owner == ctx.owner and source.run_id == ctx.run_id
            assert len(source.data["rows"]) == row_count
            assert await source.revalidate()
            return ExecutionReceipt("execution", "ok", "confirmed", {"total": 7}, file=png())

    models = Models(
        [
            model_result(tool_calls=(product_call(),), token="first", finish="tool_calls"),
            model_result(text="分析结果是 7。"),
        ]
    )

    async def query(call, bound, control):
        return DatabaseResult(
            str(uuid4()),
            ("code", "value"),
            tuple((f"商品名称-{i}" * 4, 7) for i in range(row_count)),
            False,
        )

    monkeypatch.setattr("compass_agent.tools.builtin.database_query.handler.query_database", query)
    backend = Backend()
    loader = ToolLoader(sandbox=cast(Any, backend), policy=ToolBatchPolicy(mode="parallel"))
    executor = RunService(
        cast(Any, models),
        cast(Any, FakeRouting(selected_route())),
        tools_provider=query_provider,
        tool_loader=loader,
        definition=compass_definition(),
    )
    control = RunRegistry().register(OwnerKey.from_context(CHAT))
    events = []
    result = await executor.run(run_command(), CHAT, control, events.append)
    assert result.status == "completed", result
    assert result.final_answer == "分析结果是 7。" and backend.calls == 1
    assert len(result.artifacts) == 1 and loader.slots.active == 0
    ref = result.artifacts[0]
    assert ref.tool_call_id == "chart"
    from compass_agent.tools.mcp.repository import owner_key

    assert loader.artifacts.read(ref.id, owner_key(CHAT), control.id).content == png()
    from compass_agent.tools.contracts import ToolRejected

    with pytest.raises(ToolRejected):
        loader.artifacts.read(ref.id, owner_key(CHAT), uuid4())


@pytest.mark.anyio
async def test_parallel_builtin_run_preserves_start_sequence_and_model_result_order(monkeypatch):
    models = FakeModels(
        [
            model_result(
                tool_calls=(product_call("slow"), product_call("fast")),
                token="first",
                finish="tool_calls",
            ),
            model_result(text="查询完成。"),
        ]
    )
    running = maximum = 0

    async def query(call, bound, control):
        nonlocal running, maximum
        running += 1
        maximum = max(maximum, running)
        await asyncio.sleep(0.04 if call.id == "slow" else 0.01)
        running -= 1
        return DatabaseResult(str(uuid4()), ("value",), ((7,),), False)

    monkeypatch.setattr("compass_agent.tools.builtin.database_query.handler.query_database", query)
    executor = RunService(
        cast(Any, models),
        cast(Any, FakeRouting(selected_route())),
        tools_provider=query_provider,
        tool_loader=ToolLoader(policy=ToolBatchPolicy(mode="parallel")),
        definition=compass_definition(),
    )
    control = RunRegistry().register(OwnerKey.from_context(CHAT))
    events = []
    result = await executor.run(run_command(), CHAT, control, events.append)
    assert result.status == "completed" and maximum == 2
    starts = [event.payload for event in events if event.type == "tool.started"]
    ends = [event.payload.tool_call_id for event in events if event.type == "tool.finished"]
    assert [event.tool_index for event in starts] == [1, 2]
    assert ends == ["fast", "slow"]
    assert [
        message.tool_call_id
        for message in models.requests[-1][1].messages
        if message.role == "tool"
    ] == ["slow", "fast"]


@pytest.mark.anyio
async def test_combined_catalog_limit_rejects_before_routing_or_inference():
    class Discovered:
        entries = tuple(registration(f"remote_{i}", ok) for i in range(TOOL_CATALOG_LIMIT - 1))
        closed = False

        async def close(self):
            self.closed = True

    discovered = Discovered()

    class Loader(ToolLoader):
        async def discover(
            self, trusted, control, *, defer_catalog_limit=False, selected_connection_ids=None
        ):
            return cast(Any, discovered)

    class Routing(FakeRouting):
        calls = 0

        async def select_route(self, *args, **kwargs):
            self.calls += 1
            return await super().select_route(*args, **kwargs)

    routing = Routing(selected_route())
    models = FakeModels([])
    executor = RunService(
        cast(Any, models),
        cast(Any, routing),
        tools_provider=query_provider,
        tool_loader=Loader(),
        definition=compass_definition(),
    )
    control = RunRegistry().register(OwnerKey.from_context(CHAT))
    result = await executor.run(run_command(), CHAT, control, lambda event: None)
    assert result.status == "failed" and result.error_code == "tool_catalog_limit"
    assert routing.calls == 0 and models.requests == []
    assert discovered.closed


async def query_provider(trusted, control, emit):
    return (database_registration((binding(),), emit=emit),)


def test_database_bookkeeping_cancellation_drains_local_transaction():
    from compass_agent.tools.builtin.database_query.handler import finish_bookkeeping
    from compass_agent.tools.contracts import PhaseCanceled

    async def scenario():
        started, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def transaction():
            started.set()
            await release.wait()
            settled.set()

        task = asyncio.create_task(finish_bookkeeping(transaction()))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not settled.is_set()
        release.set()
        with pytest.raises(PhaseCanceled) as caught:
            await task
        assert caught.value.cleanup_confirmed and settled.is_set()

    asyncio.run(scenario())


def test_database_bookkeeping_keeps_unsettled_transaction_unknown():
    from compass_agent.tools.builtin.database_query.handler import finish_bookkeeping
    from compass_agent.tools.contracts import PhaseCanceled

    async def scenario():
        started, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def transaction():
            started.set()
            await release.wait()
            settled.set()

        task = asyncio.create_task(finish_bookkeeping(transaction(), timeout_s=0.01))
        await started.wait()
        task.cancel()
        with pytest.raises(PhaseCanceled) as caught:
            await task
        assert not caught.value.cleanup_confirmed and not settled.is_set()
        release.set()
        await asyncio.wait_for(settled.wait(), 1)

    asyncio.run(scenario())
