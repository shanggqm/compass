"""M6 boundary replay preserves real tool budgets and timed-out model usage."""

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import compass_agent.tools.builtin.database_query as database_query
from compass_agent.execution_context import current_execution
from compass_agent.harness.autonomy import AutonomyBudget, AutonomyLimits
from compass_agent.llm.admission import ExecutionGate
from compass_agent.llm.policy import LogicalPolicy
from compass_agent.models.entities import ModelCall
from compass_agent.models.repository import ModelRepository
from compass_agent.models.secrets import SecretStore
from compass_agent.models.service import ModelService, create_runtime
from compass_agent.runtime.diagnostics import RuntimeDiagnostics
from compass_agent.runtime.entities import AgentAttempt, AgentCheckpoint, AgentStep, ModelAttempt
from compass_agent.runtime.run_scheduler import ConsumerSlot
from compass_agent.schemas import CallResponse, RunAutonomyResult, RunFailed, TenantScope
from compass_agent.settings import AutonomyModelProfile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .provider_fixture import ProviderFixture
from .test_database_tools_integration import targets as targets
from .test_durable_case_matrix import generic_case
from .test_durable_runtime_integration import command
from .test_models import ORIGINS, create, generate
from .test_run_scheduler_integration import (
    BoundaryRunner,
    advance_to_boundary_stop,
    claim,
    job,
    owner,
    runtime_service,
    submit,
)
from .test_run_scheduler_integration import engine as engine


def record_case(name, value):
    target = Path(__file__).resolve().parents[3] / "work/lesson-20/limits-acceptance.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    report = json.loads(target.read_text()) if target.exists() else {}
    report[name] = {"result": "passed", **value}
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    target.chmod(0o600)


async def complete_successor(runtime, worker, context):
    successor = await submit(runtime)
    grant = await claim(runtime, worker, successor)
    executor = BoundaryRunner()
    executor.release.set()
    result, checkpoints = await advance_to_boundary_stop(runtime, worker, grant, executor, context)
    assert result.status == "done" and checkpoints == [1]
    assert runtime.store.read_run(successor).status == "completed" and executor.calls == 1
    return str(successor)


def test_m6_tool_budget_stops_after_one_real_sql_and_releases_slot(engine, monkeypatch):
    physical_reads = []
    actual_query = database_query.query_database_live

    async def audited_query(prepared, binding, control, **kwargs):
        physical_reads.append(prepared.id)
        return await actual_query(prepared, binding, control, **kwargs)

    monkeypatch.setattr(database_query, "query_database_live", audited_query)

    async def scenario():
        context = owner()
        assert isinstance(context.scope, TenantScope)
        # This M6 fixture supplies its database directly, as pre-registry Runs did.
        async with runtime_service(
            engine, compass_runtime_max_active=1, compass_context_runtime_enabled=False
        ) as runtime:
            executor, models, request = generic_case("tool-budget", trusted=context)
            run_id = UUID(runtime.store.submit(request, context)["runId"])
            await runtime.publisher.flush(run_id)
            worker = ConsumerSlot("budget-worker", uuid4())
            grant = await claim(runtime, worker, run_id)
            result, checkpoints = await advance_to_boundary_stop(
                runtime, worker, grant, executor, context
            )
            run = runtime.store.read_run(run_id)
            assert result.status == "done" and run.status == "failed"
            assert job(engine, run_id).status == "done"
            assert len(checkpoints) >= 3 and len(models.requests) == 2
            assert physical_reads == ["c"]
            snapshot = runtime.reader.messages(run.session_id, context)
            results = [
                RunAutonomyResult.model_validate(packet.event.payload.value.event.payload.data)
                for packet in snapshot.packets
                if packet.event.type == "view.upsert"
                and packet.event.payload.kind == "timeline"
                and packet.event.payload.value.event.type == "stream.extension"
                and packet.event.payload.value.event.payload.name == "compass.autonomy.result"
            ]
            assert len(results) == 1
            outcome = results[0]
            assert outcome.outcome == "budget_exhausted" and outcome.reason == "max_tool_calls"
            assert outcome.budget.tools.used == outcome.budget.tools.limit == 1
            observed = [
                packet.event.payload.value.event
                for packet in snapshot.packets
                if packet.event.type == "view.upsert"
                and packet.event.payload.kind == "timeline"
                and packet.event.payload.value.event.type == "tool.finished"
            ]
            assert len(observed) == 1 and observed[0].payload.tool_call_id == "c"
            with Session(engine) as tx:
                attempts = tx.scalars(
                    select(AgentAttempt).where(AgentAttempt.run_id == run_id)
                ).all()
                steps = tx.scalars(select(AgentStep).where(AgentStep.run_id == run_id)).all()
                assert all(row.number == 1 and row.status == "completed" for row in attempts)
                assert sum(row.kind == "model" for row in steps) == 2
                assert len(attempts) == len(steps)
                saved = tx.get(AgentCheckpoint, (run_id, run.checkpoint_no))
                assert saved and saved.budget_state["states"]
                assert max(state["tools"] for state in saved.budget_state["states"].values()) == 1
                attempt_ids = [str(row.id) for row in attempts]
            usage = RuntimeDiagnostics(runtime.store).usage(context.scope.tenant_id)
            assert usage.occupied == 0 and usage.jobs["done"] == 1
            next_id = await complete_successor(runtime, worker, context)
            assert physical_reads == ["c"] and len(models.requests) == 2
            record_case(
                "toolBudget",
                {
                    "runId": str(run_id),
                    "checkpoints": checkpoints,
                    "attemptIds": attempt_ids,
                    "physicalSqlCalls": len(physical_reads),
                    "physicalModelCalls": len(models.requests),
                    "resultView": outcome.model_dump(mode="json", by_alias=True),
                    "job": "done",
                    "occupiedAfterFailure": usage.occupied,
                    "successorCompleted": next_id,
                },
            )

    asyncio.run(scenario())


def test_m6_model_timeout_retains_unknown_usage_without_retry_and_releases_clean_slot(engine):
    repository = ModelRepository(
        engine, SecretStore(os.environ["COMPASS_MODEL_ENCRYPTION_KEY"]), ORIGINS
    )
    upstream = ProviderFixture()
    canceled = asyncio.Event()

    async def delayed(request):
        upstream.requests.append(request)
        try:
            await asyncio.sleep(3)
            raise AssertionError("The real logical attempt deadline must cancel this provider")
        except asyncio.CancelledError:
            canceled.set()
            raise

    upstream.handle = delayed
    models = ModelService(
        repository, create_runtime(repository.secrets, upstream.transport), gate=ExecutionGate()
    )
    _, provider = create(repository, alias="M6 timeout " + uuid4().hex)
    request = generate("M6 controlled upstream timeout", max_tokens=64)
    call_id = uuid4()

    class TimeoutRunner:
        def __init__(self):
            self.models = models

        async def run(self, command, context, control, emit):
            journal = current_execution.get()
            assert journal is not None

            async def invoke():
                prepared = await models.prepare(call_id, provider.provider_id, request, context)
                spec = prepared.spec
                budget = AutonomyBudget(
                    (
                        AutonomyModelProfile(
                            provider_id=spec.provider_id,
                            binding_version=spec.binding_version,
                            sdk_model=spec.sdk_model,
                            api_base=spec.api_base,
                            input_tokens=128,
                            output_tokens=64,
                            input_microusd=2,
                            output_microusd=8,
                        ),
                    ),
                    AutonomyLimits(),
                )
                return await models.run(
                    prepared,
                    request,
                    logical_policy=LogicalPolicy(max_attempts=2, attempt_timeout_s=1),
                    attempt_budget=budget,
                )

            result = await journal.call(
                "model",
                "root/model:1",
                {"request": request.model_dump(mode="json")},
                invoke,
                CallResponse,
            )
            assert result.status == "failed" and result.error_code == "model_timeout"
            assert result.execution and len(result.execution.attempts) == 1
            return RunFailed(status="failed", errorCode="model_failed")

    async def scenario():
        context = owner()
        assert isinstance(context.scope, TenantScope)
        async with runtime_service(
            engine, compass_runtime_max_active=1, compass_context_runtime_enabled=False
        ) as runtime:
            run_id = UUID(runtime.store.submit(command(), context)["runId"])
            await runtime.publisher.flush(run_id)
            worker = ConsumerSlot("timeout-worker", uuid4())
            grant = await claim(runtime, worker, run_id)
            result, checkpoints = await advance_to_boundary_stop(
                runtime, worker, grant, TimeoutRunner(), context
            )
            assert result.status == "done" and checkpoints == [1]
            assert runtime.store.read_run(run_id).status == "failed"
            assert job(engine, run_id).status == "done"
            assert canceled.is_set() and len(upstream.requests) == 1
            with Session(engine) as tx:
                call = tx.get(ModelCall, call_id)
                assert call and call.run_id == run_id and call.agent_attempt_id
                logical = tx.get(AgentAttempt, call.agent_attempt_id)
                assert logical and logical.status == "completed" and logical.number == 1
                attempts = tx.scalars(
                    select(ModelAttempt).where(ModelAttempt.call_id == call_id)
                ).all()
                assert len(attempts) == 1
                attempt = attempts[0]
                assert attempt.status == "incomplete" and attempt.error_code == "model_timeout"
                assert attempt.sent and attempt.outcome_unknown and attempt.cleanup_confirmed
                assert attempt.usage["input_tokens"] is None
                assert attempt.usage["output_tokens"] is None
                assert attempt.actual_cost_microusd is None
                assert attempt.reserved_tokens == 192 and attempt.reserved_cost_microusd == 768
                attempt_id = str(attempt.id)
            usage = RuntimeDiagnostics(runtime.store).usage(context.scope.tenant_id)
            assert usage.occupied == 0 and usage.jobs["done"] == 1
            assert usage.unknown_attempts == 1 and usage.reserved_tokens == 192
            assert usage.reserved_cost_microusd == 768
            assert models.gate.snapshot()["available"] == models.gate.capacity
            next_id = await complete_successor(runtime, worker, context)
            assert len(upstream.requests) == 1
            record_case(
                "modelTimeout",
                {
                    "runId": str(run_id),
                    "modelCallId": str(call_id),
                    "physicalAttemptId": attempt_id,
                    "attemptTimeoutSeconds": 1,
                    "providerDelaySeconds": 3,
                    "maxAttempts": 2,
                    "actualRequests": 1,
                    "attemptStatus": "incomplete",
                    "errorCode": "model_timeout",
                    "actualCostMicrousd": None,
                    "cleanupConfirmed": True,
                    "usage": usage.model_dump(mode="json", by_alias=True),
                    "successorCompleted": next_id,
                },
            )

    asyncio.run(scenario())
