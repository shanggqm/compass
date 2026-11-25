"""Real PostgreSQL/Redis acceptance for decisions, authority and recovery boundaries."""

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
from compass_agent.api.models import ModelBoundaryMiddleware, error_response
from compass_agent.api.sessions import internal_router
from compass_agent.context_runtime.source_access import project_sources
from compass_agent.database import DemoSale
from compass_agent.execution_context import current_execution
from compass_agent.llm.admission import ExecutionGate
from compass_agent.llm.policy import LogicalPolicy
from compass_agent.models.entities import ModelCall
from compass_agent.models.errors import ModelError
from compass_agent.models.repository import ModelRepository
from compass_agent.models.secrets import SecretStore
from compass_agent.models.service import ModelService, create_runtime
from compass_agent.runtime.commands import control
from compass_agent.runtime.context import compass_definition
from compass_agent.runtime.entities import (
    AgentAttempt,
    AgentStep,
    AgentWait,
    ModelAttempt,
)
from compass_agent.runtime.journal import RecoveryStop
from compass_agent.schemas import AnalysisFileNone, CallResponse, ControlCommand, RunCompleted
from compass_agent.tools.builtin.python_analysis import python_analysis_tool
from compass_agent.tools.registry import ToolRegistry
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .provider_fixture import ProviderFixture
from .query_fixtures import provider_for
from .test_agent_run import CHAT
from .test_database_tools_integration import targets as targets
from .test_durable_runtime_integration import (
    command,
    execute,
    prepare,
    service,
)
from .test_models import ORIGINS, create, generate


def message_run(runtime, run):
    return next(
        packet.event.payload
        for packet in runtime.reader.messages(run.session_id, CHAT).packets
        if packet.event.type == "run.upsert" and packet.run_id == str(run.id)
    )


class EventRunner:
    def __init__(self):
        self.models = SimpleNamespace()
        self.deliveries = 0

    async def run(self, command, context, control, emit):
        journal = current_execution.get()
        assert journal is not None

        async def ready():
            return {"ready": True}

        await journal.call("database", "root/readiness", {}, ready, dict)
        payload = await journal.external_event(
            "receipt",
            "receipt.confirmed",
            "synthetic-receipt-19",
            {
                "type": "object",
                "properties": {"amount": {"type": "integer", "minimum": 1}},
                "required": ["amount"],
                "additionalProperties": False,
            },
        )
        self.deliveries += 1
        return RunCompleted(
            status="completed",
            finalAnswer=f"已确认金额 {payload['amount']}。",
            file=AnalysisFileNone(status="none"),
        )


def test_trusted_event_http_requires_separate_identity_and_consumes_once(targets, monkeypatch):
    engine = prepare(targets)

    async def scenario():
        async with service(engine) as runtime:
            runtime.settings = runtime.settings.model_copy(
                update={"compass_runtime_event_token": SecretStr("isolated-event-identity-19")}
            )
            worker = EventRunner()
            app = FastAPI()
            app.include_router(internal_router)
            app.add_middleware(ModelBoundaryMiddleware)
            app.state.durable_runtime = runtime

            @app.exception_handler(ModelError)
            async def errors(request, error):
                return error_response(error)

            monkeypatch.setattr("compass_agent.api.models.get_model_service", lambda request: None)
            monkeypatch.setattr(
                "compass_agent.api.runs.get_agent_executor",
                lambda request, context, models: worker,
            )
            run = await execute(runtime, command(question="等待真实受信回执"), worker)
            assert run.status == "waiting"
            with Session(engine) as tx:
                wait = tx.scalars(select(AgentWait).where(AgentWait.run_id == run.id)).one()
                identifier, step_id = wait.id, wait.step_id
            body = {
                "eventId": str(uuid4()),
                "waitId": str(identifier),
                "eventType": "receipt.confirmed",
                "correlationKey": "synthetic-receipt-19",
                "payload": {"amount": 19},
            }
            path = f"/v1/internal/analysis/runs/{run.id}/events"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://isolated.test"
            ) as client:
                for headers in [
                    {},
                    {"Authorization": "Bearer " + runtime.settings.compass_agent_service_token},
                ]:
                    response = await client.post(path, json=body, headers=headers)
                    assert response.status_code == 401
                    assert "no-store" in response.headers["cache-control"]
                    assert runtime.store.read_run(run.id).status == "waiting"
                    assert worker.deliveries == 0
                headers = {"Authorization": "Bearer isolated-event-identity-19"}
                accepted = await client.post(path, json=body, headers=headers)
                assert accepted.status_code == 200, accepted.text
                await runtime.tasks[run.id]
                repeated = await client.post(path, json=body, headers=headers)
                assert repeated.status_code == 200 and repeated.json() == accepted.json()
                await runtime.tasks[run.id]
                assert worker.deliveries == 1
                final = runtime.store.read_run(run.id)
                assert final.status == "completed"
                assert any(
                    packet.event.type == "message.upsert"
                    and packet.event.payload.content.text == "已确认金额 19。"
                    for packet in project_sources(
                        runtime.store, runtime.reader.messages(final.session_id, CHAT), CHAT
                    ).packets
                )
                with Session(engine) as tx:
                    attempts = tx.scalars(
                        select(AgentAttempt).where(AgentAttempt.step_id == step_id)
                    ).all()
                    assert len(attempts) == 1 and attempts[0].status == "completed"
                # Empty dedicated credentials disable this ingress even with a valid service token.
                runtime.settings = runtime.settings.model_copy(
                    update={"compass_runtime_event_token": SecretStr("")}
                )
                assert (await client.post(path, json=body, headers=headers)).status_code == 401

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_catalog_change_blocks_recovery_before_any_repeated_database_call(targets):
    engine = prepare(targets)
    calls = []

    class CatalogRunner:
        def __init__(self, changed=False):
            self.models = SimpleNamespace()
            registered = python_analysis_tool()
            if changed:
                assert registered.definition is not None
                definition = registered.definition.model_copy(deep=True)
                definition.function.description += " Changed revision."
                registered = replace(registered, definition=definition)
            self.catalog = ToolRegistry([registered])

        async def run(self, command, context, control, emit):
            journal = current_execution.get()
            assert journal is not None
            await asyncio.to_thread(journal.freeze_catalog, self.catalog)

            async def count_rows():
                with engine.connect() as connection:
                    result = connection.scalar(select(func.count()).select_from(DemoSale))
                calls.append(result)
                return {"rows": result}

            await journal.call("database", "root/read", {}, count_rows, dict)
            raise RecoveryStop("injected_after_database_checkpoint")

    async def scenario():
        async with service(engine) as runtime:
            run = await execute(runtime, command(question="目录版本恢复检查"), CatalogRunner())
            assert run.status == "paused" and len(calls) == 1
            with Session(engine) as tx:
                before = tx.scalars(select(AgentAttempt).where(AgentAttempt.run_id == run.id)).all()
                identity = [(item.id, item.status, item.output_sha256) for item in before]
        async with service(engine) as restarted:
            current = restarted.store.read_run(run.id)
            control(
                restarted.store,
                run.id,
                "resume",
                ControlCommand(requestId=uuid4(), expectedVersion=current.version),
                CHAT,
            )
            restarted.schedule(run.id, CatalogRunner(changed=True), CHAT)
            await restarted.tasks[run.id]
            current = restarted.store.read_run(run.id)
            assert current.status == "paused" and current.reason_code == "tool_catalog_changed"
            assert len(calls) == 1
            with Session(engine) as tx:
                after = tx.scalars(select(AgentAttempt).where(AgentAttempt.run_id == run.id)).all()
                assert [(item.id, item.status, item.output_sha256) for item in after] == identity

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_real_model_retry_attempts_remain_linked_to_durable_run_step_and_checkpoint(targets):
    engine = prepare(targets)
    repository = ModelRepository(
        engine, SecretStore(os.environ["COMPASS_MODEL_ENCRYPTION_KEY"]), ORIGINS
    )
    upstream = ProviderFixture()
    models = ModelService(
        repository, create_runtime(repository.secrets, upstream.transport), gate=ExecutionGate()
    )
    _, provider = create(repository, alias="Durable retry " + uuid4().hex)
    call_id = uuid4()
    request = generate("E2E_RETRY_429_ONCE 实际重试关联")

    class RetryRunner:
        def __init__(self):
            self.models = models

        async def run(self, command, context, control, emit):
            journal = current_execution.get()
            assert journal is not None

            async def invoke():
                prepared = await models.prepare(call_id, provider.provider_id, request, CHAT)
                return await models.run(
                    prepared, request, logical_policy=LogicalPolicy(max_attempts=2)
                )

            result = await journal.call(
                "model",
                "root/model:1",
                {"request": request.model_dump(mode="json")},
                invoke,
                CallResponse,
            )
            assert result.status == "completed" and result.execution is not None
            assert len(result.execution.attempts) == 2
            return RunCompleted(
                status="completed", finalAnswer=result.text, file=AnalysisFileNone(status="none")
            )

    async def scenario():
        async with service(engine) as runtime:
            run = await execute(
                runtime, command(question=request.messages[0].content), RetryRunner()
            )
            assert run.status == "completed" and len(upstream.requests) == 2
            with Session(engine) as tx:
                call = tx.get(ModelCall, call_id)
                assert call is not None and call.run_id == run.id and call.agent_attempt_id
                logical = tx.get(AgentAttempt, call.agent_attempt_id)
                assert logical is not None and logical.run_id == run.id
                step = tx.get(AgentStep, logical.step_id)
                assert step is not None and step.kind == "model" and step.run_id == run.id
                attempts = tx.scalars(
                    select(ModelAttempt)
                    .where(ModelAttempt.call_id == call_id)
                    .order_by(ModelAttempt.number)
                ).all()
                assert [item.status for item in attempts] == ["failed", "completed"]
                assert all(item.sent and item.origin == "live" for item in attempts)
                assert all(item.output_content and item.output_sha256 for item in attempts)
                assert all(item.reserved_tokens > 0 for item in attempts)
                assert call.input_message_refs
                logical_identity = logical.id, logical.output_sha256
            snapshot = runtime.reader.messages(run.session_id, CHAT)
        async with service(engine) as restarted:
            after = restarted.reader.messages(run.session_id, CHAT)
            assert after == snapshot and len(upstream.requests) == 2
            with Session(engine) as tx:
                logical = tx.get(AgentAttempt, logical_identity[0])
                assert logical is not None and logical.output_sha256 == logical_identity[1]
                assert tx.scalar(select(func.count()).select_from(ModelAttempt)) == 2

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()


def test_real_classifier_fallback_preserves_each_attempt_under_original_run(targets):
    from compass_agent.runtime.service import RunService

    from .test_intent_routing import configured

    engine = prepare(targets)
    repository = ModelRepository(
        engine, SecretStore(os.environ["COMPASS_MODEL_ENCRYPTION_KEY"]), ORIGINS
    )
    upstream = ProviderFixture()
    models = ModelService(
        repository, create_runtime(repository.secrets, upstream.transport), gate=ExecutionGate()
    )
    _, _, routing, _, flash, jev = configured((repository, upstream, models))
    original = upstream.handle

    async def unavailable_jev(request):
        if request.url.path.endswith("/systemone"):
            upstream.requests.append(request)
            return httpx.Response(503, json={"error": {"message": "controlled unavailable"}})
        return await original(request)

    upstream.handle = unavailable_jev

    async def scenario():
        async with service(engine) as runtime:
            worker = RunService(
                models,
                routing,
                tools_provider=provider_for(lambda _: None),
                definition=compass_definition(),
            )
            # Classifier Steps require a published Context pin. This fixture keeps
            # the historical v1 final-answer contract used by its provider responses.
            runtime.store.context_enabled = True
            submitted = command(
                question="你好呀，讲个笑话", selection={"mode": "auto", "provider_id": None}
            )
            receipt = runtime.store.submit(submitted, CHAT)
            run_id = UUID(receipt["runId"])
            with runtime.store.transaction() as tx:
                _, pinned = runtime.store.lock(tx, run_id)
                binding = {**pinned.config_snapshot["contextRuntime"], "answerProtocol": 1}
                pinned.config_snapshot = {**pinned.config_snapshot, "contextRuntime": binding}
            runtime.schedule(run_id, worker, CHAT)
            await asyncio.wait_for(runtime.tasks[run_id], 35)
            run = runtime.store.read_run(run_id)
            assert run.status == "completed", run.reason_code
            expected_calls = len(upstream.requests)
            assert expected_calls >= 3
            with Session(engine) as tx:
                rows = tx.execute(
                    select(ModelAttempt, ModelCall, AgentStep)
                    .join(ModelCall, ModelAttempt.call_id == ModelCall.call_id)
                    .join(AgentAttempt, ModelCall.agent_attempt_id == AgentAttempt.id)
                    .join(AgentStep, AgentAttempt.step_id == AgentStep.id)
                    .where(ModelCall.run_id == run.id)
                ).all()
                assert len(rows) == expected_calls
                assert {(item.provider_id, item.status) for item, _, _ in rows} == {
                    (jev, "failed"),
                    (flash, "completed"),
                }
                assert sum(step.kind == "classifier" for _, _, step in rows) == 2
                assert sum(step.kind == "model" for _, _, step in rows) == expected_calls - 2
                assert all(
                    call.run_id == run.id and step.run_id == run.id for _, call, step in rows
                )
                assert all(item.sent and item.reserved_tokens > 0 for item, _, _ in rows)
                assert all(item.output_content and item.output_sha256 for item, _, _ in rows)
                ids = {item.id for item, _, _ in rows}
        async with service(engine) as restarted:
            assert restarted.store.read_run(run.id).status == "completed"
            assert len(upstream.requests) == expected_calls
            with Session(engine) as tx:
                assert set(tx.scalars(select(ModelAttempt.id)).all()) == ids

    try:
        asyncio.run(scenario())
    finally:
        engine.dispose()
