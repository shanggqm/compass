"""Deletion response loss must still allow bounded remote-state confirmation."""

import asyncio
from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import pytest
from compass_agent.tools.contracts import PhaseCanceled
from compass_agent.tools.sandbox.client import SandboxBackend
from opensandbox import SandboxManager
from opensandbox.exceptions import SandboxApiException


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_canceled_local_reservation_releases_the_uncreated_sandbox():
    entered, release = Event(), Event()
    allocation = object()
    returned = []

    def reserve():
        entered.set()
        assert release.wait(5)
        return allocation

    store = SimpleNamespace(cancel_before_create=returned.append)
    backend = SandboxBackend(cast(Any, store), domain="unused", api_key="unit", image="unit")
    task = asyncio.create_task(backend._begin_phase(reserve))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(PhaseCanceled) as stopped:
        await task
    assert stopped.value.cleanup_confirmed and returned == [allocation]


@pytest.mark.anyio
async def test_lost_delete_response_polls_until_remote_absence(monkeypatch):
    class Manager:
        reads = 0
        closed = False

        async def kill_sandbox(self, identifier):
            raise TimeoutError("delete response lost")

        async def get_sandbox_info(self, identifier):
            self.reads += 1
            if self.reads == 1:
                raise TimeoutError("status temporarily unavailable")
            if self.reads == 3:
                raise SandboxApiException(status_code=404)
            return SimpleNamespace(id=identifier)

        async def close(self):
            self.closed = True

    manager = Manager()

    async def create(connection):
        return manager

    monkeypatch.setattr(SandboxManager, "create", create)
    backend = SandboxBackend(cast(Any, None), domain="unused", api_key="unit", image="unit")
    assert await backend._delete_confirmed(cast(Any, SimpleNamespace(sandbox_id="execution")))
    assert manager.reads == 3 and manager.closed


@pytest.mark.anyio
async def test_interrupted_confirmation_does_not_release_unknown_resource(monkeypatch):
    entered = asyncio.Event()

    class Manager:
        closed = False

        async def kill_sandbox(self, identifier):
            pass

        async def get_sandbox_info(self, identifier):
            entered.set()
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    manager = Manager()

    async def create(connection):
        return manager

    monkeypatch.setattr(SandboxManager, "create", create)
    backend = SandboxBackend(cast(Any, None), domain="unused", api_key="unit", image="unit")
    task = asyncio.create_task(
        backend._delete_confirmed(cast(Any, SimpleNamespace(sandbox_id="execution")))
    )
    await entered.wait()
    task.cancel()
    assert await task is False
    assert manager.closed


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("known_id", "matches", "released"),
    [(None, [], False), ("known", [], True), (None, ["late"], True)],
)
async def test_reconcile_keeps_unacknowledged_creates_until_a_resource_is_identified(
    monkeypatch, known_id, matches, released
):
    from datetime import UTC, datetime
    from uuid import uuid4

    from compass_agent.tools.sandbox.allocations import Allocation

    allocation = Allocation(
        uuid4(),
        (uuid4(), uuid4(), uuid4()),
        uuid4(),
        uuid4(),
        "call",
        uuid4(),
        "host",
        0,
        "digest",
        known_id,
        "unknown",
        "compute",
        1,
        datetime.now(UTC),
        None,
    )
    confirmed = []
    deleted = []

    class Manager:
        closed = False

        async def list_sandbox_infos(self, filters):
            return SimpleNamespace(sandbox_infos=[SimpleNamespace(id=item) for item in matches])

        async def close(self):
            self.closed = True

    manager = Manager()

    async def create(connection):
        return manager

    async def delete(value):
        deleted.append(value.sandbox_id)
        return True

    store = SimpleNamespace(
        claim_due=lambda: [allocation], confirmed=lambda value: confirmed.append(value.id)
    )
    backend = SandboxBackend(cast(Any, store), domain="unused", api_key="unit", image="unit")
    monkeypatch.setattr(SandboxManager, "create", create)
    monkeypatch.setattr(backend, "_delete_confirmed", delete)
    result = await backend.reconcile()
    assert result == ([str(allocation.id)] if released else [])
    assert confirmed == ([allocation.id] if released else [])
    assert deleted == (matches or ([known_id] if known_id else []))
    assert manager.closed


@pytest.mark.anyio
async def test_shared_cleanup_confirmation_releases_only_sandbox_quarantine():
    from compass_agent.tools.dispatcher import ExecutionSlots
    from compass_agent.tools.loader import ToolLoader

    from .test_tool_dispatcher import context

    identifier = "same-id"
    reads = []

    class Backend:
        async def reconcile(self):
            return []  # another replica already completed reconciliation

        async def confirmed_executions(self, identifiers):
            reads.append(identifiers)
            return [identifier]

    slots = ExecutionSlots()
    loader = ToolLoader(sandbox=cast(Any, Backend()), slots=slots)
    await loader.reconcile()  # confirmation may precede the dispatcher's quarantine handoff
    ctx = context()
    async with slots.acquire(ctx) as sandbox, slots.acquire(ctx) as mcp:
        slots.quarantine(sandbox, identifier, "sandbox")
        slots.quarantine(mcp, identifier, "mcp")
    assert slots.active == 2
    await loader.reconcile()
    assert reads == [{identifier}]
    assert slots.active == 1 and slots.quarantined == 1
    assert slots.pending("sandbox") == set() and slots.pending("mcp") == {identifier}
    await slots.confirm_cleanup(identifier, environment="mcp")
    assert slots.active == 0


@pytest.mark.anyio
async def test_unknown_input_returns_durable_rejection_before_allocating():
    from dataclasses import asdict
    from uuid import uuid4

    from compass_agent.tools.contracts import SandboxBinding

    backend = SandboxBackend(cast(Any, None), domain="unused", api_key="unit", image="unit")
    context = SimpleNamespace(
        call_id="chart", sources={}, owner=(uuid4(), uuid4(), uuid4()), run_id=uuid4()
    )
    receipt = await backend.execute(
        SandboxBinding(environment="sandbox", runtime_profile="python"),
        {"source_query_id": "unknown-result", "code": "result = {}"},
        cast(Any, context),
    )
    assert receipt.status == "rejected"
    assert receipt.cleanup == "not_started"
    assert receipt.reason_code == "sandbox_source_denied"
    assert asdict(receipt)["cleanup"] == "not_started"
