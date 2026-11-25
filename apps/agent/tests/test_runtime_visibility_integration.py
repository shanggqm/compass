"""Current-view revocation on active SSE with isolated PostgreSQL and real Redis."""

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import cast
from uuid import UUID, uuid4

import pytest
from compass_agent.api.runtime_diagnostics import timeline
from compass_agent.api.sessions import events, messages
from compass_agent.models.errors import ModelError
from compass_agent.observability.observer import Observer
from compass_agent.runtime.control import RunRegistry
from compass_agent.runtime.durable_service import DurableRuntime
from compass_agent.runtime.run_scheduler import ConsumerSlot
from compass_agent.schemas import AgentResponse, PlatformScope
from compass_agent.settings import Settings
from fastapi import Response
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine

from .context_fixture import install_prompts
from .test_agent_run import CHAT
from .test_database_tools_integration import db
from .test_database_tools_integration import targets as targets
from .test_durable_runtime_integration import command
from .test_run_scheduler_integration import BoundaryRunner, advance_to_boundary_stop, claim, counts
from .test_runtime_visibility import tool_event


class AccessState:
    def __init__(self):
        self.details = True
        self.revision = "view-1"
        self.error: str | None = None
        self.checked: list[float] = []

    async def authorize(
        self,
        context,
        *,
        approval=False,
        visibility=False,
        capabilities=False,
        run_environment=False,
    ):
        assert context == CHAT
        if run_environment and not self.error:
            return {"timezone": "UTC", "tenantName": "Tenant", "workspaceName": "Workspace"}
        if visibility:
            self.checked.append(asyncio.get_running_loop().time())
        if self.error:
            raise ModelError(self.error, 403 if self.error == "forbidden" else 503)
        if capabilities:
            return {"analysis": True, "model": True, "plugins": True}
        if visibility:
            return {"details": self.details, "revision": self.revision}
        return None


def frame(chunk):
    text = chunk if isinstance(chunk, str) else bytes(chunk).decode()
    values = [line[6:] for line in text.splitlines() if line.startswith("data: ")]
    return [AgentResponse.model_validate(json.loads(value)) for value in values]


def tool_packet(response):
    return next(
        packet
        for packet in response.packets
        if packet.event.type == "view.upsert"
        and packet.event.payload.object_id == "timeline:tool-1"
    )


def identity(packet):
    value = packet.model_dump(mode="json", by_alias=True)
    return {key: value[key] for key in ("packetId", "runId", "seq", "requestId", "occurredAt")} | {
        key: value["event"]["payload"][key] for key in ("objectId", "revision", "kind")
    }


def timeline_object(packet):
    assert packet.event.type == "view.upsert"
    assert packet.event.payload.kind == "timeline"
    return packet.event.payload


@pytest.mark.parametrize(
    "change", ["forbidden", "authorization_unavailable", "revision_only", "details_restricted"]
)
def test_active_sse_rechecks_current_view_and_preserves_durable_identity(targets, change):
    url, _ = targets("visibility")
    db.migrate()
    engine = install_prompts(create_engine(url))

    async def scenario():
        runtime = DurableRuntime(engine, Settings(), RunRegistry())
        access = AccessState()
        runtime.access.authorize = access.authorize
        await runtime.start()
        assert runtime.store.ready
        try:
            receipt = await asyncio.to_thread(runtime.store.submit, command(), CHAT)
            run_id = UUID(receipt["runId"])
            await runtime.publisher.flush(run_id)
            epoch = await asyncio.to_thread(runtime.store.claim, run_id, runtime.boot_id)
            detail_id = str(uuid4())
            execution = {
                "type": "view.upsert",
                "payload": {
                    "objectId": detail_id,
                    "revision": 1,
                    "kind": "execution.node",
                    "value": {
                        "id": detail_id,
                        "revision": 1,
                        "kind": "tool",
                        "title": "查询 POS 商品明细",
                        "status": "completed",
                        "startedAtMs": 1000,
                        "endedAtMs": 2000,
                        "detail": {
                            "input": '{"channelCode":"POS"}',
                            "output": '{"totalPaidCent":"12000"}',
                        },
                    },
                },
            }
            await asyncio.to_thread(
                runtime.store.progress, run_id, epoch, [tool_event(), execution]
            )
            await runtime.publisher.flush(run_id)
            run = runtime.store.read_run(run_id, CHAT)
            raw = runtime.reader.messages(run.session_id, CHAT)
            assert "totalPaidCent" not in raw.model_dump_json(by_alias=True)
            original = await messages(run.session_id, CHAT, runtime, Response(), cursor=None)
            original_packet = tool_packet(original)
            assert timeline_object(original_packet).revision == 3
            assert "secret-984" not in original.model_dump_json(by_alias=True)
            assert "rowCount" in original.model_dump_json(by_alias=True)
            assert "totalPaidCent" in original.model_dump_json(by_alias=True)

            stream = await events(run_id, CHAT, runtime)
            iterator = cast(AsyncGenerator[bytes | str], stream.body_iterator)
            initial = frame(await anext(iterator))
            assert len(initial) == 1
            initial_packet = tool_packet(initial[0])
            assert initial_packet.seq is not None
            finished = timeline_object(initial_packet).value.event
            assert finished.type == "tool.finished"
            assert finished.payload.status == "ok"
            assert finished.payload.row_count == 7
            assert "totalPaidCent" in initial[0].model_dump_json(by_alias=True)

            started = asyncio.get_running_loop().time()
            if change in {"forbidden", "authorization_unavailable"}:
                access.error = change
            else:
                access.revision = "view-2"
                access.details = change == "revision_only"

            async def drain():
                delivered = []
                async for chunk in iterator:
                    delivered.extend(frame(chunk))
                return delivered

            assert await asyncio.wait_for(drain(), timeout=6) == []
            elapsed = asyncio.get_running_loop().time() - started
            assert 4.5 <= elapsed <= 5.5, elapsed
            assert len(access.checked) == 4
            print(f"visibility={change} closed_after_seconds={elapsed:.3f}")
            current = runtime.store.read_run(run_id, CHAT)
            assert current.status == "running"
            assert current.version == run.version
            assert current.projected_seq == run.projected_seq
            assert runtime.reader.messages(run.session_id, CHAT) == raw

            if access.error:
                with pytest.raises(ModelError, match=access.error):
                    await events(run_id, CHAT, runtime)
                with pytest.raises(ModelError, match=access.error):
                    await messages(run.session_id, CHAT, runtime, Response(), cursor=None)
                return

            latest = await messages(run.session_id, CHAT, runtime, Response(), cursor=None)
            latest_packet = tool_packet(latest)
            assert identity(latest_packet) == identity(original_packet)
            assert latest.cursors == original.cursors
            next_stream = await events(run_id, CHAT, runtime)
            next_iterator = cast(AsyncGenerator[bytes | str], next_stream.body_iterator)
            try:
                replay = frame(await anext(next_iterator))
                replay_packet = tool_packet(replay[0])
                assert identity(replay_packet) == identity(initial_packet)
                assert replay[0].cursors == initial[0].cursors
                assert replay_packet.event == latest_packet.event
                if change == "revision_only":
                    assert latest == original
                else:
                    safe = timeline_object(latest_packet).value.event
                    assert safe.type == "stream.extension"
                    assert safe.payload.name == "compass.runtime.tool_summary"
                    assert "rowCount" not in latest.model_dump_json(by_alias=True)
                    assert "rowCount" not in replay[0].model_dump_json(by_alias=True)
                    assert "totalPaidCent" not in latest.model_dump_json(by_alias=True)
                    assert "totalPaidCent" not in replay[0].model_dump_json(by_alias=True)
            finally:
                await next_iterator.aclose()
        finally:
            await runtime.close()

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_exporter_failure_during_execution_keeps_authorized_message_and_timeline(targets):
    url, _ = targets("visibility_exporter")
    db.migrate()
    engine = install_prompts(create_engine(url))

    class UnavailableExporter(InMemorySpanExporter):
        def __init__(self):
            super().__init__()
            self.trace_ids: set[str] = set()

        def export(self, spans):
            for span in spans:
                assert span.context is not None
                self.trace_ids.add(f"{span.context.trace_id:032x}")
            return SpanExportResult.FAILURE

    async def scenario():
        runtime = DurableRuntime(
            engine,
            Settings(compass_context_runtime_enabled=False),
            RunRegistry(),
            schedule_jobs=True,
        )
        exporter = UnavailableExporter()
        observer = Observer(
            Settings(compass_observability_enabled=True, compass_observability_sample_ratio=1),
            exporter=exporter,
        )
        runtime.observer = observer
        diagnostic_context = CHAT.model_copy(
            update={"purpose": "runtime_diagnostics", "scope": PlatformScope(kind="platform")}
        )
        denied = False

        async def authorize(
            context, *, approval=False, visibility=False, capabilities=False, run_environment=False
        ):
            if denied or context not in (CHAT, diagnostic_context):
                raise ModelError("forbidden", 403)
            if run_environment:
                return {"timezone": "UTC", "tenantName": "Tenant", "workspaceName": "Workspace"}
            if capabilities:
                return {"analysis": True, "model": True, "plugins": True}
            return {"details": True, "revision": "observability-view"} if visibility else None

        runtime.access.authorize = authorize
        await runtime.start()
        assert runtime.store.ready
        try:
            receipt = await asyncio.to_thread(runtime.store.submit, command(), CHAT)
            run_id = UUID(receipt["runId"])
            await runtime.publisher.flush(run_id)
            executor = BoundaryRunner()
            headers = runtime.begin_observation(run_id, CHAT, executor)
            trace_id = headers["X-Compass-Trace-Id"]
            worker = ConsumerSlot("visibility-exporter-worker", uuid4())
            grant = await claim(runtime, worker, run_id)
            driving = asyncio.create_task(
                advance_to_boundary_stop(runtime, worker, grant, executor, CHAT)
            )
            await asyncio.wait_for(executor.started.wait(), 3)
            observer.root(uuid4(), CHAT).end()
            assert observer.provider is not None and observer.pipeline is not None
            await asyncio.to_thread(observer.provider.force_flush)
            assert observer.pipeline.exporter.failures > 0
            assert runtime.store.read_run(run_id).status == "running"
            executor.release.set()
            result, checkpoints = await asyncio.wait_for(driving, 10)
            assert result.status == "done" and checkpoints == [1]
            assert executor.calls == 1
            await asyncio.to_thread(observer.provider.force_flush)
            assert trace_id in exporter.trace_ids
            run = runtime.store.read_run(run_id)
            assert run.status == "completed"
            before = counts(engine)
            history = await messages(run.session_id, CHAT, runtime, Response(), cursor=None)
            assert any(
                packet.event.type == "message.upsert"
                and packet.event.payload.role == "assistant"
                and packet.event.payload.status == "completed"
                and packet.event.payload.content.text == "计算结果为 190。"
                for packet in history.packets
            )
            diagnostic = await timeline(run_id, diagnostic_context, runtime, Response())
            assert {entry.kind for entry in diagnostic.entries} >= {
                "scheduling",
                "step",
                "attempt",
                "command",
            }
            assert len({entry.id for entry in diagnostic.entries}) == len(diagnostic.entries)
            stream = await events(run_id, CHAT, runtime)
            replayed = []
            async for chunk in stream.body_iterator:
                replayed.extend(frame(chunk))
            assert replayed and replayed[-1].cursors == history.cursors
            with pytest.raises(ModelError, match="forbidden"):
                await timeline(run_id, CHAT, runtime, Response())
            denied = True
            with pytest.raises(ModelError, match="forbidden"):
                await messages(run.session_id, CHAT, runtime, Response(), cursor=None)
            with pytest.raises(ModelError, match="forbidden"):
                await events(run_id, CHAT, runtime)
            with pytest.raises(ModelError, match="forbidden"):
                await timeline(run_id, diagnostic_context, runtime, Response())
            denied = False
            assert await messages(run.session_id, CHAT, runtime, Response(), cursor=None) == history
            assert await timeline(run_id, diagnostic_context, runtime, Response()) == diagnostic
            assert counts(engine) == before and executor.calls == 1
            assert runtime.begin_observation(run_id, CHAT, executor) == headers
            print(
                f"exporter_failures={observer.pipeline.exporter.failures} "
                f"physical_calls={executor.calls} timeline_entries={len(diagnostic.entries)}"
            )
        finally:
            await runtime.close()
            observer.shutdown()

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()
