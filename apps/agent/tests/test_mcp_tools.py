"""Real Streamable HTTP protocol coverage plus owner, revision and policy fencing."""

import asyncio
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
import uvicorn
from compass_agent.schemas import McpToolSelection, ToolApprovalProposal
from compass_agent.tools.batch import dispatch_batch
from compass_agent.tools.contracts import ToolBatchPolicy, ToolRejected
from compass_agent.tools.mcp.client import EndpointPolicy, McpSession
from compass_agent.tools.mcp.repository import ConnectionConfig
from compass_agent.tools.mcp.service import McpRun, tool_name
from mcp.server import MCPServer
from pydantic import BaseModel, SecretStr

from tests.test_tool_dispatcher import Budget, call, context, dispatcher


@pytest.fixture
def anyio_backend():
    return "asyncio"


class SumResult(BaseModel):
    sum: int


@asynccontextmanager
async def fixture_server():
    server = MCPServer("Lesson18 fixture")
    invoked = []

    @server.tool()
    async def add(left: int, right: int) -> SumResult:
        """Return a sum with no external effects."""
        invoked.append((left, right))
        if left == -1:
            await asyncio.sleep(30)
        return SumResult(sum=left + right)

    app = server.streamable_http_app(json_response=True)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    endpoint = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
    process = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(process.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not process.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        yield endpoint, invoked
    finally:
        process.should_exit = True
        await task
        sock.close()


def configuration(endpoint, *, selected=True):
    c = context()
    return ConnectionConfig(
        uuid4(),
        c.owner,
        1,
        "个人 MCP",
        endpoint,
        SecretStr(""),
        False,
        1,
        (McpToolSelection(name="add", effect="read_only", approval="none"),) if selected else (),
    )


class Repository:
    valid = True

    def __init__(self, config):
        self.config = config

    def load(self, owner):
        assert owner == self.config.owner
        return (self.config,)

    def current(self, config):
        return self.valid


@pytest.mark.anyio
async def test_real_initialize_discovery_call_and_close():
    async with fixture_server() as (endpoint, invoked):
        config = configuration(endpoint)
        policy = EndpointPolicy("", endpoint.removesuffix("/mcp"))
        session = McpSession(config, policy)
        try:
            await session.start(asyncio.Semaphore(1))
            assert [t.name for t in session.tools] == ["add"]
            assert session.client is not None and session.client.initialize_result is not None
            assert session.client.initialize_result.protocol_version == "2025-11-25"
            result = await session.call("add", {"left": 2, "right": 3}, 3)
            assert result.structured_content == {"sum": 5}
            assert invoked == [(2, 3)]
        finally:
            await session.close()
        assert session.client is None and session._task is not None and session._task.done()


@pytest.mark.anyio
async def test_frozen_catalog_revision_fence_and_single_registry_dispatch():
    async with fixture_server() as (endpoint, invoked):
        config = configuration(endpoint)
        repo = Repository(config)
        run = await McpRun(
            repo, EndpointPolicy("", endpoint.removesuffix("/mcp")), config.owner
        ).open()
        try:
            name = tool_name(config.id, "add")
            assert len(name) == 61 and run.summary.loaded == 1
            d = dispatcher(*run.entries)
            d.mcp = run
            c = context(name)
            assert c.owner == config.owner
            result = await dispatch_batch(
                d, (call(name, args='{"left":4,"right":5}'),), c, ToolBatchPolicy()
            )
            assert result[0].status == "ok" and result[0].cleanup == "remote_managed"
            assert isinstance(result[0].payload, dict)
            assert result[0].payload["structuredContent"] == {"sum": 9}
            repo.valid = False
            result = await dispatch_batch(
                d, (call(name, args='{"left":1,"right":1}'),), c, ToolBatchPolicy()
            )
            assert result[0].reason_code == "mcp_configuration_stale"
            assert invoked == [(4, 5)]
        finally:
            await run.close()


@pytest.mark.anyio
async def test_default_unknown_effect_waits_without_remote_call():
    async with fixture_server() as (endpoint, invoked):
        config = replace(configuration(endpoint), tool_policies=(McpToolSelection(name="add"),))
        run = await McpRun(
            Repository(config), EndpointPolicy("", endpoint.removesuffix("/mcp")), config.owner
        ).open()
        try:
            name = run.entries[0].name
            d = dispatcher(*run.entries)
            d.mcp = run
            result = await dispatch_batch(
                d, (call(name, args='{"left":1,"right":2}'),), context(name), ToolBatchPolicy()
            )
            assert isinstance(result[0].proposal, ToolApprovalProposal)
            assert result[0].status == "waiting" and result[0].proposal.connection_revision == 1
            assert invoked == []
        finally:
            await run.close()


@pytest.mark.anyio
async def test_revoked_waiting_tool_stops_entire_batch_even_with_continue():
    from tests.test_tool_dispatcher import ok, registration

    async with fixture_server() as (endpoint, invoked):
        config = configuration(endpoint, selected=False)
        repo = Repository(config)
        run = await McpRun(
            repo, EndpointPolicy("", endpoint.removesuffix("/mcp")), config.owner
        ).open()
        try:
            name = run.entries[0].name
            repo.valid = False
            d = dispatcher(*run.entries, registration("builtin", ok))
            c = context(name, "builtin")
            results = await dispatch_batch(
                d,
                (call("builtin"), call(name, args='{"left":1,"right":2}')),
                c,
                ToolBatchPolicy(failure_mode="continue"),
            )
            assert all(r.status == "rejected" and r.cleanup == "not_started" for r in results)
            assert results[1].reason_code == "mcp_configuration_stale"
            assert isinstance(c.budget, Budget)
            assert c.budget.calls == 0 and invoked == []
        finally:
            await run.close()


@pytest.mark.anyio
async def test_changed_catalog_rejects_new_calls_without_refresh():
    async with fixture_server() as (endpoint, invoked):
        config = configuration(endpoint)
        run = await McpRun(
            Repository(config), EndpointPolicy("", endpoint.removesuffix("/mcp")), config.owner
        ).open()
        try:
            from mcp.types import ToolListChangedNotification

            await run.sessions[config.id]._message(ToolListChangedNotification())
            name = run.entries[0].name
            result = await dispatch_batch(
                dispatcher(*run.entries),
                (call(name, args='{"left":1,"right":2}'),),
                context(name),
                ToolBatchPolicy(),
            )
            assert result[0].reason_code == "mcp_catalog_stale" and invoked == []
        finally:
            await run.close()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com/mcp",
        "https://user:pass@example.com/mcp",
        "https://example.com/mcp?token=bad",
        "https://elsewhere.example/mcp",
        "https://example.com/../secret",
        "https://example.com/mcp#fragment",
    ],
)
def test_endpoint_rejects_unapproved_or_credential_bearing_urls(endpoint):
    with pytest.raises(ToolRejected):
        EndpointPolicy("https://example.com").validate(endpoint)


@pytest.mark.anyio
async def test_private_dns_and_metadata_are_blocked_even_when_allowlisted():
    with pytest.raises(ToolRejected):
        await EndpointPolicy("https://127.0.0.1").resolve("https://127.0.0.1/mcp")
    with pytest.raises(ToolRejected):
        await EndpointPolicy("", "http://169.254.169.254").resolve("http://169.254.169.254/mcp")


@pytest.mark.anyio
async def test_configuration_revocation_during_real_call_quarantines_remote_outcome():
    async with fixture_server() as (endpoint, invoked):
        config = configuration(endpoint)
        repo = Repository(config)
        run = await McpRun(
            repo, EndpointPolicy("", endpoint.removesuffix("/mcp")), config.owner
        ).open()
        try:
            name = run.entries[0].name
            d = dispatcher(*run.entries)
            d.mcp = run
            task = asyncio.create_task(
                dispatch_batch(
                    d, (call(name, args='{"left":-1,"right":3}'),), context(name), ToolBatchPolicy()
                )
            )
            async with asyncio.timeout(4):
                while not invoked:
                    await asyncio.sleep(0.01)
            repo.valid = False
            (result,) = await asyncio.wait_for(task, 4)
            assert result.status == "unknown" and result.cleanup == "unconfirmed"
            assert result.reason_code == "tool_config_changed" and result.payload is None
            assert invoked == [(-1, 3)] and d.slots.quarantined == 1
        finally:
            await run.close()


@asynccontextmanager
async def catalog_server(mode):
    """A real stateless Streamable HTTP endpoint that controls pagination and protocol faults."""
    from fastapi import FastAPI, Request, Response
    from fastapi.responses import JSONResponse

    app = FastAPI()
    cursors = []

    @app.post("/mcp")
    async def rpc(request: Request):
        message = await request.json()
        method = message["method"]
        value: dict[str, object]
        if "id" not in message:
            return Response(status_code=202)
        if method == "initialize":
            value = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "catalog fixture", "version": "1"},
            }
        elif method == "tools/list":
            cursor = message.get("params", {}).get("cursor")
            cursors.append(cursor)
            if mode == "timeout":
                await asyncio.sleep(8.2)
            schema = {"type": "array"} if mode == "schema" else {"type": "object"}
            count = 257 if mode == "oversized" else 33 if mode == "combined" else 1
            value = {
                "tools": [
                    {"name": f"tool_{cursor}_{i}", "inputSchema": schema} for i in range(count)
                ]
            }
            if mode in {"pages", "loop"} and (cursor is None or mode == "loop"):
                value["nextCursor"] = "page2"
        else:
            value = {}
        return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": value})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    process = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(process.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not process.started:
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/mcp", cursors
    finally:
        process.should_exit = True
        await task
        sock.close()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["pages", "loop", "schema", "oversized", "timeout", "combined"])
async def test_real_discovery_pages_optional_failure_required_failure_and_catalog_limit(mode):
    async with catalog_server(mode) as (endpoint, cursors):
        config = configuration(endpoint)
        run = McpRun(
            Repository(config), EndpointPolicy("", endpoint.removesuffix("/mcp")), config.owner
        )
        try:
            if mode == "combined":
                with pytest.raises(ToolRejected, match="tool_catalog_limit"):
                    await run.open()
            else:
                await run.open()
                if mode == "pages":
                    assert cursors == [None, "page2"] and len(run.entries) == 2
                else:
                    assert run.entries == () and run.summary.skipped == 1
        finally:
            await run.close()
        assert all(s.client is None and s._task.done() for s in run.sessions.values())
        if mode in {"loop", "schema"}:
            config = replace(config, required=True)
            required = McpRun(
                Repository(config), EndpointPolicy("", endpoint.removesuffix("/mcp")), config.owner
            )
            with pytest.raises(ToolRejected, match="mcp_required_unavailable"):
                await required.open()
            assert all(s.client is None and s._task.done() for s in required.sessions.values())


@pytest.mark.anyio
async def test_unselected_mcp_connections_are_never_probed(monkeypatch):
    from compass_agent.execution_context import current_execution

    config = configuration("http://127.0.0.1:1/mcp")
    calls = []

    async def start(*_):
        calls.append(True)
        raise AssertionError("unselected connections must not be probed")

    monkeypatch.setattr(McpSession, "start", start)
    c = context()
    run = McpRun(
        Repository(config),
        EndpointPolicy("", "http://127.0.0.1:1"),
        c.owner,
        selected_connection_ids=frozenset(),
        defer_catalog_limit=True,
    )
    token = current_execution.set(None)
    try:
        await run.open()
        assert not run.entries and not calls and not run.sessions
    finally:
        await run.close()
        current_execution.reset(token)
