"""Real PostgreSQL/Redis answer-v3 publication, fencing and authorized replay."""

import asyncio
import json
import multiprocessing
import os
import socket
import time
from pathlib import Path
from uuid import uuid4

import pytest
from compass_agent.context_runtime.source_access import project_sources, save_private
from compass_agent.models.errors import ModelError
from compass_agent.runtime.answer_publication import append_chunk
from compass_agent.runtime.entities import AgentAttempt, AgentOutbox
from compass_agent.runtime.journal import ExecutionJournal
from compass_agent.runtime.private_state import digest
from compass_agent.runtime.run_scheduler import ConsumerSlot
from compass_agent.runtime.stream_publication import VALIDATE, publication_keys
from compass_agent.schemas import AgentResponse
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .test_database_tools_integration import targets as targets
from .test_run_scheduler_integration import (
    claim,
    expire_lease,
    owner,
    runtime_service,
    submit,
)
from .test_run_scheduler_integration import (
    engine as engine,
)


async def started(runtime):
    identifier = await submit(runtime)
    with runtime.store.transaction() as tx:
        _, run = runtime.store.lock(tx, identifier)
        run.config_snapshot = {**run.config_snapshot, "contextRuntime": {"answerProtocol": 3}}
    grant = await claim(
        runtime, ConsumerSlot("answer-" + uuid4().hex[:12], runtime.boot_id), identifier
    )
    journal = ExecutionJournal(runtime.store, runtime.publisher, identifier, grant.epoch)
    return identifier, journal


def assistant(runtime, identifier):
    run = runtime.store.read_run(identifier)
    response = project_sources(
        runtime.store, runtime.reader.messages(run.session_id, owner()), owner()
    )
    return next(
        p.event.payload
        for p in response.packets
        if p.event.type == "message.upsert" and p.event.payload.role == "assistant"
    )


async def check_integrity(runtime, identifier):
    keys = publication_keys(runtime.streams.key(identifier))
    assert await runtime.streams.reader.eval(VALIDATE, len(keys), *keys) == 1
    await runtime.streams.validate(runtime.store.read_run(identifier))


def prepare_attempt(journal):
    _, attempt_id, _ = journal.prepare("model", "answer", {}, None)
    journal.dispatched(attempt_id)
    return attempt_id


def finish(runtime, identifier, journal, content):
    acceptance = {
        "candidateId": str(uuid4()),
        "executionPath": "root",
        "textDigest": digest(content),
        "citationsDigest": digest(None),
        "artifactDigest": None,
        "mode": "plain",
        "completion": "complete",
    }
    with runtime.store.transaction() as tx:
        _, run = runtime.store.lock(tx, identifier)
        save_private(
            runtime.store,
            tx,
            run,
            f"answer/v3/accepted/{acceptance['candidateId']}",
            acceptance,
            [],
        )
    runtime.store.finish(
        identifier, journal.epoch, "completed", answer=content, answer_acceptance=acceptance
    )


@pytest.mark.parametrize("distributed", [False, True])
def test_private_chunks_replay_and_finish_with_unprojected_tail(engine, distributed):
    async def scenario():
        async with runtime_service(
            engine, distributed=distributed, compass_context_runtime_enabled=False
        ) as runtime:
            identifier, journal = await started(runtime)
            attempt_id = prepare_attempt(journal)
            operation = uuid4()
            first = append_chunk(
                runtime.store, identifier, journal.epoch, attempt_id, operation, "**第一段**"
            )
            assert (
                append_chunk(
                    runtime.store, identifier, journal.epoch, attempt_id, operation, "**第一段**"
                )
                == first
            )
            with pytest.raises(ModelError, match="packet_conflict"):
                append_chunk(
                    runtime.store, identifier, journal.epoch, attempt_id, operation, "不同文本"
                )
            assert assistant(runtime, identifier).content.text == ""
            await runtime.publisher.flush(identifier)
            assert assistant(runtime, identifier).content.text == "**第一段**"
            append_chunk(runtime.store, identifier, journal.epoch, attempt_id, uuid4(), "\n第二段")
            finish(runtime, identifier, journal, "**第一段**\n第二段")
            await runtime.publisher.flush(identifier)
            saved = assistant(runtime, identifier)
            assert saved.content.text == "**第一段**\n第二段"
            assert saved.status == "completed" and saved.revision == 4
            rows = await runtime.streams.rows(identifier)
            assert "第一段" not in json.dumps(rows, ensure_ascii=False)
            with Session(engine) as tx:
                boxes = tx.scalars(
                    select(AgentOutbox).where(AgentOutbox.run_id == identifier)
                ).all()
                assert all(box.delivered_at for box in boxes)
                assert "第一段" not in json.dumps(
                    [box.payload for box in boxes], ensure_ascii=False
                )
            if distributed:
                await check_integrity(runtime, identifier)

    asyncio.run(scenario())


def test_model_result_survives_publication_failure_and_replays_without_reexecution(
    engine, monkeypatch
):
    async def scenario():
        async with runtime_service(
            engine, distributed=True, compass_context_runtime_enabled=False
        ) as runtime:
            identifier, journal = await started(runtime)
            original = runtime.publisher.flush

            async def unavailable(_):
                raise ModelError("runtime_unavailable", 503)

            monkeypatch.setattr(runtime.publisher, "flush", unavailable)
            calls = []

            async def generate():
                calls.append(1)
                for _ in range(100):
                    await journal.text_delta("输出")
                return {"status": "completed", "text": "输出" * 100}

            result = await journal.call("model", "answer", {}, generate, dict, yield_boundary=False)
            await runtime.publisher.close()
            with Session(engine) as tx:
                attempt = tx.scalars(
                    select(AgentAttempt).where(AgentAttempt.run_id == identifier)
                ).one()
                assert attempt.status == "completed"
                assert runtime.store.pending(identifier)
            monkeypatch.setattr(runtime.publisher, "flush", original)
            await runtime.publisher.flush(identifier)
            rebuilt = ExecutionJournal(runtime.store, runtime.publisher, identifier, journal.epoch)
            assert (
                await rebuilt.call("model", "answer", {}, generate, dict, yield_boundary=False)
                == result
            )
            assert calls == [1]
            assert assistant(runtime, identifier).content.text == "输出" * 100
            await check_integrity(runtime, identifier)

    asyncio.run(scenario())


def test_publish_reply_loss_lease_competition_and_old_worker_fencing(engine):
    async def scenario():
        async with runtime_service(
            engine, distributed=True, compass_context_runtime_enabled=False
        ) as first:
            identifier, journal = await started(first)
            attempt_id = prepare_attempt(journal)
            append_chunk(first.store, identifier, journal.epoch, attempt_id, uuid4(), "保留的增量")
            lease = first.store.publication(identifier)
            assert lease is not None
            async with runtime_service(
                engine, distributed=True, compass_context_runtime_enabled=False
            ) as second:
                await second.publisher.flush(identifier)
                assert assistant(second, identifier).content.text == ""
                first.store.end_publication(lease)

                async def fault(stage, run_id):
                    raise ModelError("runtime_unavailable", 503)

                first.publisher.fault = fault
                with pytest.raises(ModelError, match="runtime_unavailable"):
                    await first.publisher.flush(identifier)
                before = await first.streams.rows(identifier)
                await second.publisher.flush(identifier)
                after = await second.streams.rows(identifier)
                assert before == after
                assert assistant(second, identifier).content.text == "保留的增量"
                expire_lease(engine, identifier)
                with pytest.raises(ModelError, match="lease_lost|execution_fenced"):
                    append_chunk(
                        first.store, identifier, journal.epoch, attempt_id, uuid4(), "过期写入"
                    )
                await check_integrity(second, identifier)

    asyncio.run(scenario())


def test_authorized_delta_and_snapshot_redact_revoked_sources(engine, monkeypatch):
    async def scenario():
        async with runtime_service(
            engine, distributed=True, compass_context_runtime_enabled=False
        ) as runtime:
            identifier, journal = await started(runtime)
            attempt_id = prepare_attempt(journal)
            cursor = runtime.store.read_run(identifier).projected_seq
            append_chunk(runtime.store, identifier, journal.epoch, attempt_id, uuid4(), "私有正文")
            await runtime.publisher.flush(identifier)
            run = runtime.store.read_run(identifier)
            rows = await runtime.streams.rows(identifier, cursor)
            response = AgentResponse.model_validate(
                {
                    "protocol": "compass.run",
                    "version": 2,
                    "sessionId": str(run.session_id),
                    "mode": "delta",
                    "packets": [{**entry["packet"], "seq": seq} for seq, entry in rows],
                    "cursors": [{"runId": str(identifier), "seq": rows[-1][0]}],
                    "page": None,
                }
            )
            visible = project_sources(runtime.store, response, owner())
            assert (
                "".join(
                    p.event.payload.text for p in visible.packets if p.event.type == "message.delta"
                )
                == "私有正文"
            )
            monkeypatch.setattr(
                "compass_agent.context_runtime.source_access.allows", lambda *args, **kwargs: False
            )
            hidden = project_sources(runtime.store, response, owner())
            assert "私有正文" not in hidden.model_dump_json()
            assert hidden.cursors == visible.cursors
            assert "私有正文" not in assistant(runtime, identifier).content.text

    asyncio.run(scenario())


def test_cancel_orders_after_committed_but_unpublished_text(engine):
    async def scenario():
        async with runtime_service(
            engine, distributed=True, compass_context_runtime_enabled=False
        ) as runtime:
            identifier, journal = await started(runtime)
            attempt_id = prepare_attempt(journal)
            append_chunk(
                runtime.store, identifier, journal.epoch, attempt_id, uuid4(), "取消前已提交"
            )
            runtime.store.finish(identifier, journal.epoch, "canceled")
            with pytest.raises(ModelError, match="execution_fenced"):
                append_chunk(
                    runtime.store, identifier, journal.epoch, attempt_id, uuid4(), "迟到文本"
                )
            await runtime.publisher.flush(identifier)
            message = assistant(runtime, identifier)
            assert message.content.text == "取消前已提交"
            assert message.status == "incomplete" and message.revision == 3
            await check_integrity(runtime, identifier)

    asyncio.run(scenario())


def publisher_process(url, identifier, crash, connection):
    from unittest.mock import patch

    from compass_agent.runtime.durable_service import DurableRuntime

    original = DurableRuntime.publish_fault

    async def fault(self, stage, run_id):
        if crash:
            connection.send("appended")
            os._exit(86)
        await original(self, stage, run_id)

    async def scenario():
        database = create_engine(url)
        try:
            async with runtime_service(
                database,
                distributed=True,
                compass_context_runtime_enabled=False,
                compass_runtime_lease_seconds=60,
            ) as runtime:
                async with asyncio.timeout(20):
                    while runtime.store.pending(identifier):
                        await runtime.publisher.flush(identifier)
                        await asyncio.sleep(0.05)
                connection.send(assistant(runtime, identifier).content.text)
        finally:
            database.dispose()

    with patch.object(DurableRuntime, "publish_fault", fault):
        asyncio.run(scenario())


def test_separate_process_takes_over_after_redis_append_crash(engine, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))

    async def scenario():
        async with runtime_service(
            engine,
            distributed=True,
            compass_context_runtime_enabled=False,
            compass_runtime_lease_seconds=60,
        ) as runtime:
            identifier, journal = await started(runtime)
            attempt_id = prepare_attempt(journal)
            append_chunk(
                runtime.store, identifier, journal.epoch, attempt_id, uuid4(), "跨进程保留"
            )
            context = multiprocessing.get_context("spawn")
            received, sent = context.Pipe(duplex=False)
            url = engine.url.render_as_string(hide_password=False)
            first = context.Process(target=publisher_process, args=(url, identifier, True, sent))
            second = context.Process(target=publisher_process, args=(url, identifier, False, sent))
            try:
                first.start()
                assert await asyncio.to_thread(received.poll, 15)
                assert received.recv() == "appended"
                await asyncio.to_thread(first.join, 5)
                assert first.exitcode == 86
                before = await runtime.streams.rows(identifier)
                second.start()
                assert await asyncio.to_thread(received.poll, 25)
                assert received.recv() == "跨进程保留"
                await asyncio.to_thread(second.join, 5)
                assert second.exitcode == 0
                assert await runtime.streams.rows(identifier) == before
                await check_integrity(runtime, identifier)
            finally:
                for process in (first, second):
                    if process.is_alive():
                        process.terminate()
                        await asyncio.to_thread(process.join, 5)
                received.close()
                sent.close()

    asyncio.run(scenario())


def test_http_sse_latency_and_refresh_during_generation(engine):
    import httpx
    import uvicorn
    from compass_agent.api.sessions import events
    from fastapi import FastAPI

    async def scenario():
        async with runtime_service(
            engine, distributed=True, compass_context_runtime_enabled=False
        ) as runtime:
            identifier, journal = await started(runtime)
            cursor = runtime.store.read_run(identifier).projected_seq

            async def visibility(context):
                return {"details": True, "revision": "answer-test"}

            runtime.access.visibility = visibility
            app = FastAPI()

            @app.get("/events")
            async def stream():
                return await events(identifier, owner(), runtime, cursor)

            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            emitted, delays = {}, []
            visible = asyncio.Event()

            async def receive():
                async with httpx.AsyncClient(timeout=10) as client:
                    async with client.stream(
                        "GET", f"http://127.0.0.1:{listener.getsockname()[1]}/events"
                    ) as response:
                        assert response.status_code == 200
                        async for line in response.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            for packet in json.loads(line[6:])["packets"]:
                                event = packet["event"]
                                if event["type"] == "message.delta":
                                    for word in event["payload"]["text"].splitlines():
                                        delays.append(time.monotonic() - emitted[word])
                                    visible.set()
                            if len(delays) == 30:
                                return

            async def generate():
                for index in range(30):
                    word = f"文本{index}"
                    emitted[word] = time.monotonic()
                    await journal.text_delta(word + "\n")
                    await asyncio.sleep(0.04)
                return {"status": "completed", "text": "".join(word + "\n" for word in emitted)}

            reader = None
            generation = None
            try:
                while not server.started:
                    await asyncio.sleep(0.01)
                reader = asyncio.create_task(receive())
                generation = asyncio.create_task(
                    journal.call("model", "answer", {}, generate, dict, yield_boundary=False)
                )
                await asyncio.wait_for(visible.wait(), 5)
                assert "文本0" in assistant(runtime, identifier).content.text
                result = await generation
                await asyncio.wait_for(reader, 5)
                p95 = sorted(delays)[int(len(delays) * 0.95) - 1]
                print(f"answer SSE latency p95={p95:.3f}s samples={len(delays)}")
                assert p95 < 0.3
                finish(runtime, identifier, journal, result["text"])
                await runtime.publisher.flush(identifier)
                assert assistant(runtime, identifier).content.text == result["text"]
                await check_integrity(runtime, identifier)
            finally:
                for task in (reader, generation):
                    if task and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (reader, generation) if task), return_exceptions=True
                )
                server.should_exit = True
                await serving

    asyncio.run(scenario())


def test_two_streams_keep_heartbeat_and_cancellation_responsive(engine):
    from compass_agent.runtime.run_scheduler import ExecutionGrant

    async def scenario():
        async with runtime_service(
            engine,
            distributed=True,
            compass_context_runtime_enabled=False,
            compass_runtime_tenant_active=2,
            compass_runtime_lease_seconds=3,
        ) as runtime:
            first, first_journal = await started(runtime)
            second, second_journal = await started(runtime)
            scheduler = runtime.store.scheduler
            assert scheduler is not None
            grants = []
            with runtime.store.transaction() as tx:
                for identifier, journal in ((first, first_journal), (second, second_journal)):
                    job = scheduler.job(tx, identifier)
                    grants.append(
                        ExecutionGrant(
                            runId=identifier,
                            workerId=job.worker_id,
                            bootId=job.worker_boot_id,
                            epoch=journal.epoch,
                            expiresAt=job.lease_expires_at,
                            checkpointNo=0,
                        )
                    )
            finished = asyncio.Event()
            timings = []

            async def heartbeat():
                while not finished.is_set():
                    started_at = time.monotonic()
                    renewed = await asyncio.to_thread(
                        scheduler.heartbeat,
                        ConsumerSlot(grants[1].worker_id, grants[1].boot_id),
                        grants[1],
                    )
                    assert renewed is not None
                    timings.append(time.monotonic() - started_at)
                    await asyncio.sleep(0.2)

            async def generate(journal):
                for _ in range(80):
                    await journal.text_delta("并发文本")
                    await asyncio.sleep(0.04)
                return {"text": "并发文本" * 80}

            tasks = [
                asyncio.create_task(
                    journal.call(
                        "model",
                        "answer",
                        {},
                        lambda journal=journal: generate(journal),
                        dict,
                        yield_boundary=False,
                    )
                )
                for journal in (first_journal, second_journal)
            ]
            renew = asyncio.create_task(heartbeat())
            try:
                await asyncio.sleep(0.3)
                canceled_at = time.monotonic()
                tasks[0].cancel()
                await asyncio.gather(tasks[0], return_exceptions=True)
                runtime.store.finish(first, first_journal.epoch, "canceled")
                assert time.monotonic() - canceled_at < 0.5
                result = await tasks[1]
                finished.set()
                await renew
                assert len(timings) >= 5 and max(timings) < 0.5
                finish(runtime, second, second_journal, result["text"])
                await runtime.publisher.close()
                await runtime.publisher.flush(first)
                await runtime.publisher.flush(second)
                assert assistant(runtime, first).status == "incomplete"
                assert assistant(runtime, second).content.text == "并发文本" * 80
                await check_integrity(runtime, first)
                await check_integrity(runtime, second)
            finally:
                finished.set()
                for task in [*tasks, renew]:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, renew, return_exceptions=True)

    asyncio.run(scenario())
