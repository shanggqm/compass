"""M6 scheduling acceptance against isolated PostgreSQL and real Redis Streams.

The supervisor is driven explicitly after startup so lock and reclamation windows
remain observable. No database, lease, projection, or command receipt is mocked.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from compass_agent.context_runtime.source_access import project_sources
from compass_agent.data_connections.transport import AuthorizedDatabaseBinding
from compass_agent.execution_context import current_execution
from compass_agent.models.attempts import AttemptRecorder
from compass_agent.models.entities import ModelCall
from compass_agent.models.errors import ModelError
from compass_agent.models.repository import ModelRepository
from compass_agent.models.secrets import SecretStore
from compass_agent.runtime.commands import control
from compass_agent.runtime.control import RunRegistry
from compass_agent.runtime.diagnostics import RuntimeDiagnostics
from compass_agent.runtime.durable_service import DurableRuntime
from compass_agent.runtime.entities import (
    AgentAction,
    AgentAttempt,
    AgentCheckpoint,
    AgentCommand,
    AgentMessage,
    AgentOutbox,
    AgentRun,
    AgentRunJob,
    AgentSession,
    AgentStep,
    AgentTenantSchedule,
    AgentWait,
    ModelAttempt,
)
from compass_agent.runtime.hitl import HumanDecisions
from compass_agent.runtime.journal import BoundaryYield, ExecutionJournal, RecoveryStop
from compass_agent.runtime.legacy import start_legacy
from compass_agent.runtime.resources import RuntimeResources
from compass_agent.runtime.run_scheduler import AdvanceCommand, ConsumerSlot
from compass_agent.runtime.store import RuntimeStore
from compass_agent.schemas import (
    AnalysisFileNone,
    AnalysisRunInput,
    ControlCommand,
    DecisionCommand,
    ExternalRunEvent,
    RunCompleted,
    RunWaiting,
    TenantScope,
)
from compass_agent.settings import Settings
from compass_agent.tools.builtin.database_query.handler import registration
from compass_agent.tools.contracts import PhaseCanceled
from compass_agent.tools.sandbox.allocations import Allocations
from compass_agent.workflows.assets import WorkflowAssets
from compass_agent.workflows.drafts import WorkflowDrafts
from pydantic import SecretStr
from sqlalchemy import MetaData, Table, create_engine, event, func, select, text
from sqlalchemy.orm import Session

from .context_fixture import capability_grants, install_prompts
from .test_agent_run import CHAT, model_result
from .test_database_tools_integration import db
from .test_database_tools_integration import targets as targets
from .test_durable_runtime_integration import DYNAMIC, command
from .test_durable_runtime_integration import service as legacy_service
from .test_dynamic_workflow import ParentAndChildModels, create_call, response, service_for
from .test_models import ORIGINS, generate
from .test_models import create as create_provider

TENANT_A = UUID("20000000-0000-0000-0000-000000000001")
TENANT_B = UUID("20000000-0000-0000-0000-000000000002")
TENANT_C = UUID("20000000-0000-0000-0000-000000000003")


def owner(tenant=TENANT_A):
    assert isinstance(CHAT.scope, TenantScope)
    return CHAT.model_copy(update={"scope": CHAT.scope.model_copy(update={"tenant_id": tenant})})


@pytest.fixture
def engine(targets):
    url, _ = targets("scheduler")
    db.migrate()
    value = install_prompts(create_engine(url))
    try:
        yield value
    finally:
        value.dispose()


@asynccontextmanager
async def runtime_service(engine, **settings):
    distributed = settings.pop("distributed", False)
    small = {
        "compass_runtime_max_active": 2,
        "compass_runtime_tenant_active": 1,
        "compass_runtime_max_backlog": 12,
        "compass_runtime_tenant_backlog": 4,
    }
    runtime = DurableRuntime(
        engine,
        Settings().model_copy(update={**small, **settings}),
        RunRegistry(),
        schedule_jobs=True,
        distributed=distributed,
    )

    async def allowed(context, **kwargs):
        if kwargs.get("run_environment"):
            return {
                "tenantName": "定时租户",
                "workspaceName": "调度组",
                "timezone": "Asia/Shanghai",
            }
        return None

    runtime.access.capabilities = capability_grants
    runtime.access.check = allowed
    await runtime.start()
    assert runtime.store.ready, "The isolated PostgreSQL/Redis runtime must be ready"
    assert runtime.store.scheduler is not None
    assert runtime.ticker is not None
    runtime.ticker.cancel()
    await asyncio.gather(runtime.ticker, return_exceptions=True)
    try:
        yield runtime
    finally:
        await runtime.close()


def scheduler(runtime):
    value = runtime.store.scheduler
    assert value is not None
    return value


async def submit(runtime, tenant=TENANT_A):
    receipt = await asyncio.to_thread(runtime.store.submit, command(), owner(tenant))
    run_id = UUID(receipt["runId"])
    await runtime.publisher.flush(run_id)
    return run_id


async def claim(runtime, worker, expected=None):
    grant = await asyncio.to_thread(scheduler(runtime).claim, worker)
    assert grant is not None
    if expected is not None:
        assert grant.run_id == expected
    await runtime.publisher.flush(grant.run_id)
    return grant


def job(engine, run_id):
    with Session(engine, expire_on_commit=False) as tx:
        value = tx.get(AgentRunJob, run_id)
        assert value is not None
        return value


def counts(engine):
    with Session(engine) as tx:
        return {
            table.__tablename__: tx.scalar(select(func.count()).select_from(table))
            for table in (
                AgentSession,
                AgentRun,
                AgentRunJob,
                AgentTenantSchedule,
                AgentMessage,
                AgentOutbox,
                AgentCommand,
                AgentAttempt,
                AgentAction,
            )
        }


def control_command(runtime, run_id):
    return ControlCommand(requestId=uuid4(), expectedVersion=runtime.store.read_run(run_id).version)


async def checkpoint(runtime, run_id, epoch, *, store=None, calls=None):
    journal = ExecutionJournal(store or runtime.store, runtime.publisher, run_id, epoch)

    async def calculate():
        if calls is not None:
            calls.append(run_id)
        return {"sum": sum(range(20))}

    if journal.store.scheduler:
        with pytest.raises(BoundaryYield):
            await journal.call("tool", "local-sum", {}, calculate, dict)
    else:
        assert await journal.call("tool", "local-sum", {}, calculate, dict) == {"sum": 190}
    assert runtime.store.read_run(run_id).checkpoint_no == 1
    return journal


def expire_lease(engine, run_id):
    with Session(engine) as tx, tx.begin():
        value = tx.get(AgentRunJob, run_id)
        assert value is not None
        clock = tx.scalar(select(func.clock_timestamp()))
        assert clock is not None
        value.lease_expires_at = clock - timedelta(seconds=1)


def test_acceptance_is_atomic_idempotent_and_rolls_back_every_record(engine, monkeypatch):
    async def scenario():
        async with runtime_service(engine) as runtime:
            request = command()
            results = await asyncio.gather(
                *(asyncio.to_thread(runtime.store.submit, request, owner()) for _ in range(8))
            )
            assert results == [results[0]] * 8
            before = counts(engine)
            assert before == {
                "agent_session": 1,
                "agent_run": 1,
                "agent_run_job": 1,
                "agent_tenant_schedule": 1,
                "agent_message": 3,
                "agent_outbox": 1,
                "agent_command": 1,
                "agent_attempt": 0,
                "agent_action": 0,
            }
            with Session(engine) as tx:
                assert sorted(tx.scalars(select(AgentMessage.channel))) == [
                    "context",
                    "conversation",
                    "conversation",
                ]
            identifier = UUID(results[0]["runId"])
            run = runtime.store.read_run(identifier)
            assert run.status == "created" and run.execution_epoch is None
            assert job(engine, identifier).status == "ready"
            assert runtime.tasks == {}
            with pytest.raises(ModelError, match="request_conflict"):
                runtime.store.submit(request.model_copy(update={"question": "changed"}), owner())

            def fail_receipt(*args, **kwargs):
                raise RuntimeError("injected failure after Run/job/Outbox creation")

            with monkeypatch.context() as scoped:
                scoped.setattr(runtime.store, "record_command", fail_receipt)
                with pytest.raises(RuntimeError, match="injected failure"):
                    runtime.store.submit(command(), owner(TENANT_B))
            assert counts(engine) == before
            await runtime.publisher.flush(identifier)
            snapshot = project_sources(
                runtime.store, runtime.reader.messages(run.session_id, owner()), owner()
            )
            assert any(
                packet.event.type == "view.upsert" and packet.event.payload.kind == "timeline"
                for packet in snapshot.packets
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", ["tenant", "global"])
def test_concurrent_admission_enforces_tenant_and_global_backlog(engine, limit):
    async def scenario():
        settings = (
            {"compass_runtime_tenant_backlog": 4}
            if limit == "tenant"
            else {"compass_runtime_max_backlog": 3}
        )
        async with runtime_service(engine, **settings) as runtime:
            capacity = 4 if limit == "tenant" else 3
            scopes = [TENANT_A] * 9 if limit == "tenant" else [TENANT_A, TENANT_B, TENANT_C] * 3
            requests = [command() for _ in scopes]
            outcomes = await asyncio.gather(
                *(
                    asyncio.to_thread(runtime.store.submit, request, owner(tenant))
                    for request, tenant in zip(requests, scopes, strict=True)
                ),
                return_exceptions=True,
            )
            accepted = [value for value in outcomes if isinstance(value, dict)]
            rejected = [value for value in outcomes if isinstance(value, ModelError)]
            assert len(accepted) == capacity
            assert len(rejected) == len(scopes) - capacity
            assert all(e.code == "runtime_backpressure" and e.status == 429 for e in rejected)
            result = counts(engine)
            assert result["agent_run"] == result["agent_run_job"] == capacity
            assert result["agent_message"] == capacity * 3
            assert result["agent_outbox"] == result["agent_command"] == capacity
            assert runtime.tasks == {}
            first = next(
                index for index, outcome in enumerate(outcomes) if isinstance(outcome, dict)
            )
            assert runtime.store.submit(requests[first], owner(scopes[first])) == outcomes[first]
            assert counts(engine) == result
            if limit == "tenant":
                assert await submit(runtime, TENANT_B)

    asyncio.run(scenario())


def test_blocked_parked_task_releases_backlog_capacity(engine):
    async def scenario():
        async with runtime_service(engine, compass_runtime_tenant_backlog=1) as runtime:
            first = await submit(runtime)
            with runtime.store.transaction() as tx:
                session, run = runtime.store.lock(tx, first)
                run.status, run.recovery_status = "paused", "blocked"
                run.reason_code = "execution_unknown"
                scheduler(runtime).job(tx, first).status = "parked"
                runtime.store.outbox(tx, session, run, [])
            second = await submit(runtime)
            assert runtime.store.read_run(first).recovery_status == "blocked"
            assert job(engine, second).status == "ready"
            with pytest.raises(ModelError, match="runtime_backpressure"):
                await submit(runtime)

    asyncio.run(scenario())


def test_parked_task_keeps_backlog_and_resume_reuses_reserved_position(engine):
    async def scenario():
        async with runtime_service(engine, compass_runtime_tenant_backlog=2) as runtime:
            first = await submit(runtime)
            worker = ConsumerSlot("worker-a", uuid4())
            grant = await claim(runtime, worker, first)
            await checkpoint(runtime, first, grant.epoch)
            control(runtime.store, first, "pause", control_command(runtime, first), owner())
            await runtime.recover_scheduled()
            await runtime.publisher.flush(first)
            assert job(engine, first).status == "parked"
            parked = runtime.store.read_run(first)
            assert parked.status == "paused" and parked.control_intent == "pause"
            second = await submit(runtime)
            with pytest.raises(ModelError, match="runtime_backpressure"):
                runtime.store.submit(command(), owner())
            resumed = control_command(runtime, first)
            receipt = control(runtime.store, first, "resume", resumed, owner())
            assert control(runtime.store, first, "resume", resumed, owner()) == receipt
            assert job(engine, first).status == "ready"
            run = runtime.store.read_run(first)
            assert run.execution_epoch is None and run.control_intent is None
            assert run.status == "paused" and run.reason_code == "queued"
            with pytest.raises(ModelError, match="state_conflict"):
                control(runtime.store, first, "resume", control_command(runtime, first), owner())
            await runtime.publisher.flush(first)
            await claim(runtime, worker, second)
            assert scheduler(runtime).claim(ConsumerSlot("worker-b", uuid4())) is None
            assert counts(engine)["agent_run_job"] == 2

    asyncio.run(scenario())


def test_resume_reuses_original_constraints_and_remaining_active_time(engine):
    async def scenario():
        async with runtime_service(engine, compass_context_runtime_enabled=False) as runtime:
            constraints = {
                "plan": "required",
                "maxSteps": 2,
                "timeoutSeconds": 5,
            }
            submitted = command(question=DYNAMIC, constraints=constraints)
            receipt = runtime.store.submit(submitted, owner())
            run_id = UUID(receipt["runId"])
            await runtime.publisher.flush(run_id)
            worker = ConsumerSlot("worker-a", uuid4())
            first = await claim(runtime, worker, run_id)
            await checkpoint(runtime, run_id, first.epoch)
            runtime.store.record_active_time(run_id, first.epoch, 4000)
            control(runtime.store, run_id, "pause", control_command(runtime, run_id), owner())
            await runtime.recover_scheduled()
            await runtime.publisher.flush(run_id)
            assert runtime.store.read_run(run_id).status == "paused"
            control(runtime.store, run_id, "resume", control_command(runtime, run_id), owner())
            await runtime.publisher.flush(run_id)
            resumed = await claim(runtime, worker, run_id)

            observed = []

            async def run(restored, _context, control, _sink):
                observed.append((restored.constraints, control.remaining))
                return control.commit(
                    RunCompleted(
                        status="completed", finalAnswer="完成", file=AnalysisFileNone(status="none")
                    )
                )

            await advance_to_boundary_stop(
                runtime, worker, resumed, SimpleNamespace(run=run, models=object()), owner()
            )
            assert len(observed) == 1
            stored, remaining = observed[0]
            assert stored == submitted.constraints and 0 < remaining <= 1
            assert runtime.store.read_run(run_id).status == "completed"

    asyncio.run(scenario())


@pytest.mark.parametrize("locked_table", [AgentSession, AgentRun])
def test_claim_skips_locked_candidate_without_changing_its_fairness_position(engine, locked_table):
    async def scenario():
        async with runtime_service(engine) as runtime:
            first, second = await submit(runtime), await submit(runtime, TENANT_B)
            run = runtime.store.read_run(first)
            with Session(engine) as tx, tx.begin():
                identifier = run.session_id if locked_table is AgentSession else first
                tx.scalar(
                    select(locked_table).where(locked_table.id == identifier).with_for_update()
                )
                granted = await asyncio.wait_for(
                    asyncio.to_thread(scheduler(runtime).claim, ConsumerSlot("worker-a", uuid4())),
                    2,
                )
                assert granted is not None and granted.run_id == second
                with Session(engine) as observer:
                    a = observer.get(AgentTenantSchedule, TENANT_A)
                    b = observer.get(AgentTenantSchedule, TENANT_B)
                    assert a is not None and b is not None
                    assert a.last_claimed_at is None and b.last_claimed_at is not None
            await runtime.publisher.flush(second)
            await claim(runtime, ConsumerSlot("worker-b", uuid4()), first)

    asyncio.run(scenario())


def test_claim_recovers_lost_response_and_enforces_boot_worker_and_active_slots(engine):
    async def scenario():
        async with runtime_service(engine) as runtime:
            first = await submit(runtime)
            await submit(runtime)
            second = await submit(runtime, TENANT_B)
            third = await submit(runtime, TENANT_C)
            worker = ConsumerSlot("worker-a", uuid4())
            results = await asyncio.gather(
                *(asyncio.to_thread(scheduler(runtime).claim, worker) for _ in range(6))
            )
            assert all(value == results[0] for value in results)
            assert results[0] is not None and results[0].run_id == first
            assert job(engine, first).claim_count == 1
            assert scheduler(runtime).claim(ConsumerSlot(worker.worker_id, uuid4())) is None
            grant_b = await claim(runtime, ConsumerSlot("worker-b", uuid4()), second)
            assert scheduler(runtime).claim(ConsumerSlot("worker-c", uuid4())) is None
            assert scheduler(runtime).heartbeat(ConsumerSlot("worker-b", grant_b.boot_id), grant_b)
            with pytest.raises(ModelError, match="lease_lost"):
                scheduler(runtime).heartbeat(ConsumerSlot("worker-c", grant_b.boot_id), grant_b)
            scheduler(runtime).release(worker, results[0])
            assert job(engine, first).status == "reclaiming"
            assert scheduler(runtime).claim(ConsumerSlot("worker-c", uuid4())) is None
            await runtime.recover_scheduled()
            await runtime.publisher.flush(first)
            await claim(runtime, ConsumerSlot("worker-c", uuid4()), third)

    asyncio.run(scenario())


def test_heartbeat_changes_only_lease_expiry_and_rejects_expired_grant(engine):
    async def scenario():
        async with runtime_service(engine) as runtime:
            run_id = await submit(runtime)
            worker = ConsumerSlot("worker-a", uuid4())
            grant = await claim(runtime, worker, run_id)
            before = runtime.store.read_run(run_id)
            before_counts = counts(engine)
            await asyncio.sleep(0.01)
            renewed = scheduler(runtime).heartbeat(worker, grant)
            assert renewed.expires_at > grant.expires_at
            after = runtime.store.read_run(run_id)
            assert (after.version, after.projected_seq, after.public_view) == (
                before.version,
                before.projected_seq,
                before.public_view,
            )
            assert counts(engine) == before_counts
            expire_lease(engine, run_id)
            expired = job(engine, run_id).lease_expires_at
            with pytest.raises(ModelError, match="lease_lost"):
                scheduler(runtime).heartbeat(worker, renewed)
            assert job(engine, run_id).lease_expires_at == expired

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["heartbeat", "advance", "progress"])
def test_waiting_for_row_lock_past_ttl_uses_current_database_clock(engine, operation):
    async def scenario():
        async with runtime_service(engine) as runtime:
            run_id = await submit(runtime)
            worker = ConsumerSlot("worker-a", uuid4())
            grant = await claim(runtime, worker, run_id)
            with Session(engine) as tx, tx.begin():
                lease = tx.get(AgentRunJob, run_id)
                assert lease is not None
                clock = tx.scalar(select(func.clock_timestamp()))
                assert clock is not None
                lease.lease_expires_at = clock + timedelta(seconds=1)
            before = counts(engine)
            entered = Event()

            def entering_lock(connection, cursor, statement, parameters, context, executemany):
                if "agent_session" in statement and "FOR UPDATE" in statement:
                    entered.set()

            def invoke():
                if operation == "heartbeat":
                    return scheduler(runtime).heartbeat(worker, grant)
                if operation == "advance":
                    request = AdvanceCommand(grant=grant, expectedCheckpoint=0)
                    return scheduler(runtime).prepare_advance(worker, request)
                return runtime.store.progress(run_id, grant.epoch, [])

            with Session(engine) as locked, locked.begin():
                session_id = runtime.store.read_run(run_id).session_id
                locked.scalar(
                    select(AgentSession).where(AgentSession.id == session_id).with_for_update()
                )
                event.listen(engine, "before_cursor_execute", entering_lock)
                pending = asyncio.create_task(asyncio.to_thread(invoke))
                try:
                    assert await asyncio.to_thread(entered.wait, 0.5)
                    assert not pending.done()
                    await asyncio.sleep(1.1)
                finally:
                    event.remove(engine, "before_cursor_execute", entering_lock)
            with pytest.raises(ModelError, match="lease_lost"):
                await asyncio.wait_for(pending, 2)
            assert counts(engine) == before

    asyncio.run(scenario())


def test_advance_polling_and_old_committed_receipt_do_not_repeat_an_attempt(engine):
    async def scenario():
        async with runtime_service(engine) as runtime:
            run_id = await submit(runtime)
            worker = ConsumerSlot("worker-a", uuid4())
            grant = await claim(runtime, worker, run_id)
            request = AdvanceCommand(grant=grant, expectedCheckpoint=0)
            accepted = await asyncio.gather(
                *(
                    asyncio.to_thread(scheduler(runtime).prepare_advance, worker, request)
                    for _ in range(6)
                )
            )
            assert sum(start for _, start in accepted) == 1
            assert all(ack == accepted[0][0] for ack, _ in accepted)
            assert accepted[0][0].status == "running"
            refreshed = scheduler(runtime).heartbeat(worker, grant)
            assert scheduler(runtime).prepare_advance(
                worker, request.model_copy(update={"grant": refreshed})
            ) == (accepted[0][0], False)
            calls = []
            await checkpoint(runtime, run_id, grant.epoch, calls=calls)
            committed, start = scheduler(runtime).prepare_advance(worker, request)
            assert not start and committed.status == "continue" and committed.checkpoint_no == 1
            assert calls == [run_id]
            runtime.store.ready = False
            before_unavailable = counts(engine)
            assert scheduler(runtime).prepare_advance(worker, request) == (committed, False)
            with pytest.raises(ModelError, match="runtime_unavailable") as unavailable:
                scheduler(runtime).prepare_advance(
                    worker, AdvanceCommand(grant=refreshed, expectedCheckpoint=1)
                )
            assert unavailable.value.status == 503
            assert counts(engine) == before_unavailable
            runtime.store.ready = True
            scheduler(runtime).release(worker, refreshed)
            await runtime.recover_scheduled()
            await runtime.publisher.flush(run_id)
            successor = await claim(runtime, ConsumerSlot("worker-b", uuid4()), run_id)
            assert successor.epoch != grant.epoch and successor.checkpoint_no == 1
            before = counts(engine)
            assert scheduler(runtime).prepare_advance(worker, request) == (committed, False)
            with pytest.raises(ModelError, match="lease_lost"):
                scheduler(runtime).prepare_advance(
                    worker, AdvanceCommand(grant=grant, expectedCheckpoint=1)
                )
            with pytest.raises(ModelError, match="execution_fenced"):
                runtime.store.progress(run_id, grant.epoch, [])
            assert counts(engine) == before
            with Session(engine) as tx:
                attempts = tx.scalars(
                    select(AgentAttempt).where(AgentAttempt.run_id == run_id)
                ).all()
                assert len(attempts) == 1 and attempts[0].status == "completed"
                assert attempts[0].execution_epoch == grant.epoch

    asyncio.run(scenario())


@pytest.mark.parametrize("distributed", [False, True])
@pytest.mark.parametrize(
    "reason",
    [
        "workflow_replay_mismatch",
        "checkpoint_invalid",
        "checkpoint_corrupt",
        "runtime_state_invalid",
        "budget_policy_mismatch",
    ],
)
def test_deterministic_recovery_failure_parks_without_reclaim_loop(engine, reason, distributed):
    async def scenario():
        async with runtime_service(
            engine, distributed=distributed, compass_context_runtime_enabled=False
        ) as runtime:
            run_id = await submit(runtime)
            worker = ConsumerSlot("deterministic-failure", uuid4())
            grant = await claim(runtime, worker, run_id)
            scheduler(runtime).prepare_advance(
                worker, AdvanceCommand(grant=grant, expectedCheckpoint=0)
            )
            await checkpoint(runtime, run_id, grant.epoch)
            await runtime.pause_interrupted(run_id, grant.epoch, reason)
            for _ in range(3):
                await runtime.recover()
                await runtime.publisher.flush(run_id)
                assert scheduler(runtime).claim(worker) is None
                run = runtime.store.read_run(run_id)
                assert (run.status, run.recovery_status, run.reason_code) == (
                    "paused",
                    "blocked",
                    reason,
                )
                assert job(engine, run_id).status == "parked"
                assert job(engine, run_id).claim_count == 1
            # Parking the failed Run releases the slot for unrelated work.
            other = await submit(runtime, TENANT_B)
            await claim(runtime, worker, other)
            # The user can still cancel a parked Run.
            control(runtime.store, run_id, "cancel", control_command(runtime, run_id), owner())
            await runtime.recover()
            assert runtime.store.read_run(run_id).status == "canceled"

    asyncio.run(scenario())


def test_agent_restart_reclaims_unstarted_advance_and_preserves_original_attempt(engine):
    async def scenario():
        worker = ConsumerSlot("worker-a", uuid4())
        async with runtime_service(engine) as runtime:
            run_id = await submit(runtime)
            grant = await claim(runtime, worker, run_id)
            await checkpoint(runtime, run_id, grant.epoch)
            request = AdvanceCommand(grant=grant, expectedCheckpoint=1)
            ack, start = scheduler(runtime).prepare_advance(worker, request)
            assert start and ack.status == "running"
            with Session(engine) as tx:
                original_attempt = tx.scalars(select(AgentAttempt.id)).one()
        async with runtime_service(engine) as restarted:
            assert job(engine, run_id).status == "ready"
            assert restarted.store.read_run(run_id).execution_epoch is None
            with Session(engine) as tx:
                receipt = tx.scalars(
                    select(AgentCommand).where(AgentCommand.request_id == ack.command_id)
                ).one()
                assert receipt.response["state"] == "interrupted"
                assert tx.scalars(select(AgentAttempt.id)).all() == [original_attempt]
            fresh = await claim(restarted, ConsumerSlot("worker-a", uuid4()), run_id)
            assert fresh.epoch != grant.epoch and fresh.checkpoint_no == 1
            with pytest.raises(ModelError, match="lease_lost"):
                scheduler(restarted).prepare_advance(worker, request)

    asyncio.run(scenario())


async def legacy_continuation(runtime, kind):
    """Create the published M5 state through its original store and journal APIs."""
    old = RuntimeStore(
        runtime.store.engine, runtime.settings.compass_runtime_state_key.get_secret_value()
    )
    old.ready = True
    run_id = UUID(old.submit(command(), owner())["runId"])
    await runtime.publisher.flush(run_id)
    if kind == "created":
        return run_id, control_command(runtime, run_id)
    epoch = old.claim(run_id, runtime.boot_id)
    journal = await checkpoint(runtime, run_id, epoch, store=old)
    if kind == "resume":
        old.finish(run_id, epoch, "paused", reason="user_paused")
        await runtime.publisher.flush(run_id)
        return run_id, control_command(runtime, run_id)
    if kind == "approval":
        waiting = RunWaiting.model_validate(
            {
                "status": "waiting",
                "runId": str(run_id),
                "waitId": str(uuid4()),
                "nodeKey": "tool_approval",
                "continuation": "execute_tool",
                "proposal": {
                    "proposalId": str(uuid4()),
                    "toolName": "scheduler_fixture_write",
                    "toolCallId": "scheduler_fixture_call",
                    "argumentsDigest": "a" * 64,
                    "summary": "确认调度边界测试动作。",
                },
            }
        )
        HumanDecisions(old).wait(run_id, epoch, waiting)
        await runtime.publisher.flush(run_id)
        with Session(runtime.store.engine) as tx:
            wait = tx.get(AgentWait, UUID(waiting.wait_id))
            assert wait is not None
            value = DecisionCommand(
                requestId=uuid4(),
                expectedVersion=old.read_run(run_id).version,
                waitId=wait.id,
                proposalId=wait.proposal_id,
                proposalRevision=wait.proposal_revision,
                proposalDigest=wait.proposal_sha256,
                decisionId=uuid4(),
                decision="approve",
            )
        return run_id, value
    with pytest.raises(RecoveryStop):
        await journal.external_event(
            "delivery", "delivery.confirmed", "scheduler-order", {"type": "object"}
        )
    with Session(runtime.store.engine) as tx:
        wait = tx.scalars(select(AgentWait).where(AgentWait.run_id == run_id)).one()
        value = ExternalRunEvent(
            eventId=uuid4(),
            waitId=wait.id,
            eventType="delivery.confirmed",
            correlationKey="scheduler-order",
            payload={"confirmed": True},
        )
    return run_id, value


@pytest.mark.parametrize("entry", ["created", "resume", "approval", "external"])
def test_legacy_control_admission_is_atomic_and_every_success_only_enqueues(engine, entry):
    async def scenario():
        async with runtime_service(engine, compass_runtime_tenant_backlog=1) as runtime:
            run_id, request = await legacy_continuation(runtime, entry)
            occupied = await submit(runtime)

            def invoke():
                if entry in {"created", "resume"}:
                    return control(runtime.store, run_id, "resume", request, owner())
                if entry == "approval":
                    return runtime.hitl.decide(
                        run_id, request, owner(), may_approve=True, boot_id=runtime.boot_id
                    )
                return runtime.hitl.external_event(run_id, request, boot_id=runtime.boot_id)

            before = counts(engine)
            original = runtime.store.read_run(run_id)
            with pytest.raises(ModelError, match="runtime_backpressure") as rejected:
                invoke()
            assert rejected.value.status == 429
            assert counts(engine) == before
            unchanged = runtime.store.read_run(run_id)
            assert (unchanged.status, unchanged.version, unchanged.execution_epoch) == (
                original.status,
                original.version,
                original.execution_epoch,
            )
            with Session(engine) as tx:
                assert tx.get(AgentRunJob, run_id) is None
                if entry not in {"created", "resume"}:
                    wait = tx.scalars(select(AgentWait).where(AgentWait.run_id == run_id)).one()
                    assert wait.status == "pending" and wait.decision_id is None
            control(runtime.store, occupied, "cancel", control_command(runtime, occupied), owner())
            receipt = invoke()
            assert invoke() == receipt
            run = runtime.store.read_run(run_id)
            assert run.status == "paused" and run.execution_epoch is None
            assert run.reason_code == "queued" and job(engine, run_id).status == "ready"
            assert runtime.tasks == {}
            with pytest.raises(ModelError, match="worker_required"):
                runtime.store.claim(run_id, runtime.boot_id)
            await runtime.publisher.flush(run_id)
            grant = await claim(runtime, ConsumerSlot("worker-a", uuid4()), run_id)
            assert grant.checkpoint_no == (0 if entry == "created" else 1)

    asyncio.run(scenario())


def test_v1_adapter_uses_same_backlog_and_does_not_start_execution(engine):
    async def scenario():
        async with runtime_service(engine, compass_runtime_tenant_backlog=1) as runtime:
            body = AnalysisRunInput.model_validate(
                command().model_dump(mode="json", by_alias=True, exclude={"session_id"})
            )
            executor = SimpleNamespace(models=SimpleNamespace())
            response = await start_legacy(runtime, body, owner(), executor)
            assert response.status_code == 200
            assert runtime.tasks == {}
            with Session(engine) as tx:
                run = tx.scalars(select(AgentRun)).one()
                assert run.status == "created" and run.execution_epoch is None
                assert tx.scalars(select(AgentRunJob.status)).all() == ["ready"]
            before = counts(engine)
            with pytest.raises(ModelError, match="runtime_backpressure"):
                await start_legacy(
                    runtime, body.model_copy(update={"request_id": uuid4()}), owner(), executor
                )
            assert counts(engine) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("claimed", [False, True])
def test_cancel_and_reclaim_block_new_dispatch_until_resources_are_settled(engine, claimed):
    async def scenario():
        async with runtime_service(engine, compass_runtime_tenant_backlog=1) as runtime:
            run_id = await submit(runtime)
            worker = ConsumerSlot("worker-a", uuid4())
            grant = await claim(runtime, worker, run_id) if claimed else None
            request = control_command(runtime, run_id)
            receipt = control(runtime.store, run_id, "cancel", request, owner())
            assert control(runtime.store, run_id, "cancel", request, owner()) == receipt
            if grant:
                assert job(engine, run_id).status == "reclaiming"
                assert job(engine, run_id).worker_id == worker.worker_id
                repeated = control(
                    runtime.store, run_id, "cancel", control_command(runtime, run_id), owner()
                )
                assert repeated["status"] == "cancelling"
                with pytest.raises(ModelError, match="runtime_backpressure"):
                    runtime.store.submit(command(), owner())
                with pytest.raises(ModelError, match="lease_lost"):
                    scheduler(runtime).prepare_advance(
                        worker, AdvanceCommand(grant=grant, expectedCheckpoint=0)
                    )
                with pytest.raises(ModelError, match="lease_lost"):
                    scheduler(runtime).heartbeat(worker, grant)
                await runtime.recover_scheduled()
            assert runtime.store.read_run(run_id).status == "canceled"
            final_job = job(engine, run_id)
            assert final_job.status == "done" and final_job.worker_id is None
            assert counts(engine)["agent_attempt"] == 0
            await runtime.publisher.flush(run_id)
            snapshot = runtime.reader.messages(runtime.store.read_run(run_id).session_id, owner())
            assert any(
                packet.event.type == "message.upsert"
                and packet.event.payload.role == "assistant"
                and packet.event.payload.status == "incomplete"
                for packet in snapshot.packets
            )
            next_run = await submit(runtime)
            await claim(runtime, ConsumerSlot("worker-b", uuid4()), next_run)

    asyncio.run(scenario())


def test_reclaim_waits_for_cleanup_without_canceling_it_again(engine):
    async def scenario():
        async with runtime_service(engine) as runtime:
            run_id = await submit(runtime)
            await claim(runtime, ConsumerSlot("worker-draining", uuid4()), run_id)
            entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def active():
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()

            task = asyncio.create_task(active())
            runtime.tasks[run_id] = task
            await entered.wait()
            control(runtime.store, run_id, "cancel", control_command(runtime, run_id), owner())
            try:
                await runtime.recover_scheduled()
                assert cleaning.is_set() and not task.done()
                await runtime.recover_scheduled()
                assert not task.done() and task.cancelling() == 1
                assert job(engine, run_id).status == "reclaiming"
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            await runtime.recover_scheduled()
            assert runtime.store.read_run(run_id).status == "canceled"

    asyncio.run(scenario())


def test_cancel_after_model_commit_keeps_its_checkpoint_and_frees_the_slot(engine):
    async def scenario():
        async with runtime_service(engine) as runtime:
            run_id = await submit(runtime)
            grant = await claim(runtime, ConsumerSlot("worker-returned", uuid4()), run_id)
            entered = asyncio.Event()
            model_returned = asyncio.Event()

            class DelayedPublisher:
                def require_owner(self):
                    runtime.publisher.require_owner()

                async def flush(self, key):
                    if model_returned.is_set():
                        entered.set()
                        await asyncio.Event().wait()
                    else:
                        await runtime.publisher.flush(key)

            journal = ExecutionJournal(runtime.store, DelayedPublisher(), run_id, grant.epoch)

            async def completed():
                model_returned.set()
                return {"status": "completed", "text": "settled"}

            task = asyncio.create_task(
                journal.call(
                    "model", "root/model:returned", {}, completed, dict, yield_boundary=False
                )
            )
            await asyncio.wait_for(entered.wait(), 10)
            assert journal.has_step("root/model:returned")
            control(runtime.store, run_id, "cancel", control_command(runtime, run_id), owner())
            task.cancel()
            with pytest.raises(PhaseCanceled) as canceled:
                await task
            assert canceled.value.cleanup_confirmed
            await runtime.recover_scheduled()
            assert runtime.store.read_run(run_id).status == "canceled"
            assert job(engine, run_id).status == "done"
            with Session(engine) as tx:
                attempt = tx.scalars(
                    select(AgentAttempt).where(AgentAttempt.run_id == run_id)
                ).one()
                assert attempt.status == "completed"

    asyncio.run(scenario())


@pytest.mark.parametrize("receipt_state", ["confirmed", "late", "prepared", "absent"])
def test_cancel_reclaims_fenced_model_with_confirmed_cleanup(engine, receipt_state):
    async def scenario():
        async with runtime_service(engine) as runtime:
            repository = ModelRepository(
                engine, SecretStore(os.environ["COMPASS_MODEL_ENCRYPTION_KEY"]), ORIGINS
            )
            _, provider = create_provider(repository, alias="Cancel model fixture")
            run_id = await submit(runtime)
            grant = await claim(runtime, ConsumerSlot("worker-model-cancel", uuid4()), run_id)
            journal = ExecutionJournal(runtime.store, runtime.publisher, run_id, grant.epoch)
            step_id, attempt_id, _ = journal.prepare("model", "root/model:1", {}, None)
            journal.dispatched(attempt_id)
            physical_id = None
            if receipt_state != "absent":
                _, physical_id = ledger_attempt(
                    runtime,
                    repository,
                    provider.provider_id,
                    owner(),
                    usage={},
                    actual_cost=None,
                    status="dispatched",
                    reserved_tokens=120,
                    run_id=run_id,
                    agent_attempt_id=attempt_id,
                )
            control(runtime.store, run_id, "cancel", control_command(runtime, run_id), owner())
            journal.unknown(attempt_id)
            if receipt_state == "late":
                await runtime.recover_scheduled()
                assert job(engine, run_id).status == "parked"
                await runtime.publisher.flush(run_id)
                view = runtime.store.read_run(run_id).public_view["run"]
                assert (view["status"], view["recoveryStatus"]) == ("cancelling", "blocked")
            if physical_id:
                with Session(engine) as tx, tx.begin():
                    physical = tx.get(ModelAttempt, physical_id)
                    assert physical is not None
                    if receipt_state == "prepared":
                        physical.status, physical.sent = "prepared", False
                        physical.dispatched_at, physical.outcome_unknown = None, False
                    else:
                        physical.status, physical.cleanup_confirmed = "canceled", True
                        physical.finished_at = tx.scalar(select(func.clock_timestamp()))
            await runtime.recover_scheduled()
            await runtime.publisher.flush(run_id)

            run = runtime.store.read_run(run_id)
            assert (run.status, run.cleanup_status, run.recovery_status) == (
                "canceled",
                "confirmed",
                "ready",
            )
            assert run.public_view["run"]["status"] == "canceled"
            assert job(engine, run_id).status == "done"
            with Session(engine) as tx:
                logical = tx.get(AgentAttempt, attempt_id)
                step = tx.get(AgentStep, step_id)
                assert logical and logical.status == "canceled"
                assert step and step.status == "canceled"
                if physical_id:
                    physical = tx.get(ModelAttempt, physical_id)
                    assert physical and physical.status == "canceled"
                    assert physical.cleanup_confirmed and physical.reserved_tokens == 120
                    assert physical.outcome_unknown == (receipt_state != "prepared")
            assert runtime.store.submit(command(sessionId=str(run.session_id)), owner())

    asyncio.run(scenario())


def test_cancel_during_model_dispatch_records_that_provider_was_never_called(engine):
    async def scenario():
        async with runtime_service(engine) as runtime:
            repository = ModelRepository(
                engine, SecretStore(os.environ["COMPASS_MODEL_ENCRYPTION_KEY"]), ORIGINS
            )
            _, provider = create_provider(repository, alias="Cancel before model send")
            run_id = await submit(runtime)
            grant = await claim(runtime, ConsumerSlot("worker-before-send", uuid4()), run_id)
            journal = ExecutionJournal(runtime.store, runtime.publisher, run_id, grant.epoch)
            _, attempt_id, _ = journal.prepare("model", "root/model:1", {}, None)
            journal.dispatched(attempt_id)
            _, physical_id = ledger_attempt(
                runtime,
                repository,
                provider.provider_id,
                owner(),
                usage={},
                actual_cost=None,
                status="prepared",
                run_id=run_id,
                agent_attempt_id=attempt_id,
            )
            with Session(engine) as tx, tx.begin():
                physical = tx.get(ModelAttempt, physical_id)
                assert physical is not None
                physical.sent = physical.outcome_unknown = False
                physical.dispatched_at = None

            entered, release = Event(), Event()

            def slow_dispatch(connection, cursor, statement, parameters, context, executemany):
                if statement.startswith("UPDATE model_attempt") and not entered.is_set():
                    entered.set()
                    assert release.wait(5)

            token = current_execution.set(journal)
            try:
                recorder = AttemptRecorder(repository)
            finally:
                current_execution.reset(token)
            event.listen(engine, "before_cursor_execute", slow_dispatch)
            try:
                dispatch = asyncio.create_task(recorder.dispatched(physical_id))
                assert await asyncio.to_thread(entered.wait, 5)
                dispatch.cancel()
                await asyncio.sleep(0)
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await dispatch
            finally:
                release.set()
                event.remove(engine, "before_cursor_execute", slow_dispatch)
            with Session(engine) as tx:
                physical = tx.get(ModelAttempt, physical_id)
                assert physical and physical.status == "canceled" and physical.cleanup_confirmed
                assert not physical.sent and not physical.outcome_unknown

            control(runtime.store, run_id, "cancel", control_command(runtime, run_id), owner())
            await runtime.recover_scheduled()
            assert runtime.store.read_run(run_id).status == "canceled"

    asyncio.run(scenario())


@pytest.mark.parametrize("receipt", ["before_call", "closed", "returned", "unknown"])
def test_cancel_fences_tool_output_but_accepts_confirmed_cleanup(engine, receipt):
    async def scenario():
        async with runtime_service(engine) as runtime:
            run_id = await submit(runtime)
            grant = await claim(runtime, ConsumerSlot("worker-tool-cancel", uuid4()), run_id)
            journal = ExecutionJournal(runtime.store, runtime.publisher, run_id, grant.epoch)
            dispatched, release = Event(), Event()
            invoked = asyncio.Event()
            record_dispatch = journal.dispatched

            def delayed_dispatch(attempt_id):
                record_dispatch(attempt_id)
                dispatched.set()
                assert release.wait(5)

            async def operation():
                invoked.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    if receipt == "returned":
                        return {"status": "canceled", "cleanup": "confirmed"}
                    raise PhaseCanceled(cleanup_confirmed=receipt == "closed") from None

            if receipt == "before_call":
                journal.dispatched = delayed_dispatch
            task = asyncio.create_task(journal.call("tool", "root/test-tool", {}, operation, dict))
            try:
                if receipt == "before_call":
                    assert await asyncio.to_thread(dispatched.wait, 5)
                else:
                    await asyncio.wait_for(invoked.wait(), 5)
                control(runtime.store, run_id, "cancel", control_command(runtime, run_id), owner())
                task.cancel()
            finally:
                release.set()
            if receipt == "returned":
                with pytest.raises(ModelError, match="execution_fenced"):
                    await task
            else:
                with pytest.raises(PhaseCanceled) as canceled:
                    await task
                assert canceled.value.cleanup_confirmed == (receipt != "unknown")
            assert invoked.is_set() == (receipt != "before_call")
            await runtime.recover_scheduled()
            await runtime.publisher.flush(run_id)
            run = runtime.store.read_run(run_id)
            if receipt == "unknown":
                assert run.public_view["run"]["recoveryStatus"] == "blocked"
                assert run.status == "cancelling" and job(engine, run_id).status == "reclaiming"
            else:
                assert run.status == "canceled" and job(engine, run_id).status == "done"
            with Session(engine) as tx:
                attempt = tx.scalars(
                    select(AgentAttempt).where(AgentAttempt.run_id == run_id)
                ).one()
                assert attempt.output_content is None and run.checkpoint_no is None
                assert attempt.status == ("dispatched" if receipt == "unknown" else "canceled")

    asyncio.run(scenario())


def test_cancellation_finishes_after_sandbox_reconciliation(engine):
    async def scenario():
        async with runtime_service(engine) as runtime:
            run_id = await submit(runtime)
            grant = await claim(runtime, ConsumerSlot("worker-sandbox-cancel", uuid4()), run_id)
            journal = ExecutionJournal(runtime.store, runtime.publisher, run_id, grant.epoch)
            step_id, attempt_id, _ = journal.prepare("tool", "root/sandbox", {}, None)
            journal.dispatched(attempt_id)
            scope = owner().scope
            assert isinstance(scope, TenantScope)
            allocations = Allocations(engine, "cancel-test")
            allocation = allocations.reserve(
                (scope.tenant_id, scope.workspace_id, owner().actor_user_id),
                run_id,
                step_id,
                "test-call",
                uuid4(),
                "fixture-digest",
            )
            control(runtime.store, run_id, "cancel", control_command(runtime, run_id), owner())
            journal.unknown(attempt_id, sandbox_execution_id=str(allocation.id))
            await runtime.recover_scheduled()
            assert runtime.store.read_run(run_id).reason_code == "cleanup_unconfirmed"
            assert job(engine, run_id).status == "reclaiming"

            allocations.confirmed(allocation)
            await runtime.recover_scheduled()
            assert runtime.store.read_run(run_id).status == "canceled"
            assert job(engine, run_id).status == "done"

    asyncio.run(scenario())


def test_unknown_dispatched_attempt_remains_blocked_and_is_never_replayed(engine):
    async def scenario():
        async with runtime_service(engine) as runtime:
            run_id = await submit(runtime)
            worker = ConsumerSlot("worker-a", uuid4())
            grant = await claim(runtime, worker, run_id)
            journal = ExecutionJournal(runtime.store, runtime.publisher, run_id, grant.epoch)
            step_id, attempt_id, _ = journal.prepare("tool", "unresolved-write", {}, None)
            journal.dispatched(attempt_id)
            expire_lease(engine, run_id)
            await runtime.recover_scheduled()
            assert scheduler(runtime).claim(ConsumerSlot("worker-b", uuid4())) is None
            run = runtime.store.read_run(run_id)
            assert run.recovery_status == "blocked"
            assert run.execution_epoch is None
            retained = job(engine, run_id)
            assert retained.status == "reclaiming" and retained.worker_id == worker.worker_id
            assert retained.worker_boot_id == worker.boot_id
            assert scheduler(runtime).claim(ConsumerSlot(worker.worker_id, uuid4())) is None
            with Session(engine) as tx:
                attempts = tx.scalars(
                    select(AgentAttempt).where(AgentAttempt.step_id == step_id)
                ).all()
                assert len(attempts) == 1 and attempts[0].id == attempt_id
                assert attempts[0].status in {"dispatched", "unknown"}
            with pytest.raises(ModelError, match="resume_blocked"):
                control(runtime.store, run_id, "resume", control_command(runtime, run_id), owner())

    asyncio.run(scenario())


def test_unconfirmed_resources_retain_worker_tenant_and_global_slots(engine):
    async def scenario():
        async with runtime_service(engine, compass_runtime_tenant_backlog=1) as runtime:
            first = await submit(runtime)
            worker = ConsumerSlot("worker-a", uuid4())
            grant = await claim(runtime, worker, first)
            runtime.store.finish(
                first, grant.epoch, "paused", reason="cleanup_unconfirmed", cleanup="unconfirmed"
            )
            await runtime.recover_scheduled()
            run = runtime.store.read_run(first)
            assert run.recovery_status == "blocked" and run.cleanup_status == "unconfirmed"
            held = job(engine, first)
            assert held.status == "reclaiming" and held.worker_id == worker.worker_id
            assert held.worker_boot_id == worker.boot_id
            with pytest.raises(ModelError, match="runtime_backpressure"):
                runtime.store.submit(command(), owner())
            assert scheduler(runtime).claim(ConsumerSlot("worker-a", uuid4())) is None
            second = await submit(runtime, TENANT_B)
            await claim(runtime, ConsumerSlot("worker-b", uuid4()), second)
            await submit(runtime, TENANT_C)
            assert scheduler(runtime).claim(ConsumerSlot("worker-c", uuid4())) is None
            control(runtime.store, first, "cancel", control_command(runtime, first), owner())
            await runtime.recover_scheduled()
            assert runtime.store.read_run(first).status == "cancelling"
            assert job(engine, first).status == "reclaiming"
            assert counts(engine)["agent_attempt"] == 0

    asyncio.run(scenario())


class BoundaryRunner:
    """A real deterministic local calculation held at an observable dispatch boundary."""

    def __init__(self):
        self.models = SimpleNamespace()
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0

    async def run(self, command, context, control, emit):
        journal = current_execution.get()
        assert journal is not None

        async def calculate():
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return {"sum": sum(range(20))}

        result = await journal.call("tool", "local-sum", {}, calculate, dict)
        return RunCompleted(
            status="completed",
            finalAnswer=f"计算结果为 {result['sum']}。",
            file=AnalysisFileNone(status="none"),
        )


async def advance_to_boundary_stop(runtime, worker, grant, executor, context):
    checkpoint_no = grant.checkpoint_no
    continuations = []
    for _ in range(50):
        grant = scheduler(runtime).heartbeat(worker, grant)
        request = AdvanceCommand(grant=grant, expectedCheckpoint=checkpoint_no)
        ack, start = scheduler(runtime).prepare_advance(worker, request)
        assert start and ack.status == "running"
        runtime.start_boundary(grant.run_id, executor, context)
        await asyncio.wait_for(runtime.tasks[grant.run_id], 30)
        result, repeated_start = scheduler(runtime).prepare_advance(worker, request)
        assert not repeated_start
        if result.status != "continue":
            run = runtime.store.read_run(grant.run_id)
            assert result.status in {"parked", "done"}, (result, run.status, run.reason_code)
            return result, continuations
        persisted = runtime.store.read_run(grant.run_id)
        if result.checkpoint_no != persisted.checkpoint_no:
            with Session(runtime.store.engine) as tx:
                boundaries = list(
                    tx.execute(
                        select(AgentCheckpoint.number, AgentCheckpoint.position).where(
                            AgentCheckpoint.run_id == grant.run_id,
                            AgentCheckpoint.number > checkpoint_no,
                        )
                    )
                )
                details = []
                for number, position in boundaries:
                    step = tx.get(AgentStep, UUID(position["lastStepId"]))
                    assert step is not None
                    details.append(
                        {
                            "number": number,
                            "kind": step.kind,
                            "node": step.node_key,
                            "parent": str(step.parent_step_id),
                        }
                    )
            pytest.fail(
                json.dumps(
                    {
                        "workerCheckpoint": result.checkpoint_no,
                        "persistedCheckpoint": persisted.checkpoint_no,
                        "previousCheckpoint": checkpoint_no,
                        "runStatus": persisted.status,
                        "boundaries": details,
                    }
                )
            )
        assert result.checkpoint_no > checkpoint_no
        checkpoint_no = result.checkpoint_no
        continuations.append(checkpoint_no)
    pytest.fail("The workflow did not settle within 50 committed boundaries")


def test_workflow_tool_finishes_across_worker_boundaries(engine, monkeypatch):
    from compass_agent.workflows import script

    original = script.shutil.which
    runner_lookups = 0

    def which(name):
        nonlocal runner_lookups
        if name == "node":
            runner_lookups += 1
        return original(name)

    monkeypatch.setattr(script.shutil, "which", which)

    async def scenario():
        async with runtime_service(
            engine, compass_runtime_lease_seconds=60, compass_context_runtime_enabled=False
        ) as runtime:
            run_id = await submit(runtime)
            worker = ConsumerSlot("worker-workflow", uuid4())
            grant = await claim(runtime, worker, run_id)
            models = ParentAndChildModels([create_call(), model_result(text="试跑完成。")])
            executor = service_for(
                models, WorkflowAssets(engine), WorkflowDrafts(RuntimeResources(runtime.store))
            )
            result, checkpoints = await advance_to_boundary_stop(
                runtime, worker, grant, executor, owner()
            )
            run = runtime.store.read_run(run_id)
            assert result.status == "done" and run.status == "completed", run.reason_code
            assert checkpoints
            assert len(models.children) == 6
            assert runner_lookups == 1
            snapshot = project_sources(
                runtime.store, runtime.reader.messages(run.session_id, owner()), owner()
            )
            assert any(
                packet.event.type == "view.upsert" and packet.event.payload.kind == "workflow"
                for packet in snapshot.packets
            )

    asyncio.run(scenario())


def test_workflow_rejection_and_child_query_replay_across_worker_boundaries(engine, monkeypatch):
    from compass_agent.workflows import script

    original = script.shutil.which
    runner_lookups = 0

    def which(name):
        nonlocal runner_lookups
        if name == "node":
            runner_lookups += 1
        return original(name)

    monkeypatch.setattr(script.shutil, "which", which)

    class Models(ParentAndChildModels):
        async def run(self, prepared, request, **kwargs):
            if "你是独立的分析子 Agent" not in request.messages[0].content:
                return await super().run(prepared, request, **kwargs)
            self.children.append(request)
            if len(request.messages) == 2:
                return response("database_query", {"op": "query", "sql": "SELECT 17 AS amount"})
            assert json.loads(request.messages[-1].content)["rows"] == [[17]]
            return model_result(text="17")

    async def scenario():
        # Replay a pre-registry workflow whose SQL binding is supplied by the legacy fixture.
        async with runtime_service(
            engine, compass_runtime_lease_seconds=60, compass_context_runtime_enabled=False
        ) as runtime:
            run_id = await submit(runtime)
            worker = ConsumerSlot("worker-workflow-replay", uuid4())
            grant = await claim(runtime, worker, run_id)
            models = Models(
                [
                    create_call("const value = ;"),
                    response("sub_agent", {"prompt": "Query amount"}),
                    create_call(
                        'async function (args) { const child = await agent("sub_agent", '
                        '{prompt: "Query amount " + args.window}); '
                        'return {status: "ok", result: child.answer}; }'
                    ),
                    model_result(text="17，已试跑，等待保存。"),
                ]
            )
            executor = service_for(
                models, WorkflowAssets(engine), WorkflowDrafts(RuntimeResources(runtime.store))
            )
            url = engine.url
            assert url.username
            scope = owner().scope
            assert isinstance(scope, TenantScope)
            binding = AuthorizedDatabaseBinding(
                scope.tenant_id,
                scope.workspace_id,
                url.username,
                SecretStr(url.set(drivername="postgresql").render_as_string(hide_password=False)),
            )

            async def provider(_context, _control, emit):
                return (registration((binding,), emit=emit),)

            executor.tools_provider = provider
            result, checkpoints = await advance_to_boundary_stop(
                runtime, worker, grant, executor, owner()
            )
            run = runtime.store.read_run(run_id)
            assert result.status == "done" and run.status == "completed", run.reason_code
            assert len(checkpoints) >= 4
            assert runner_lookups == 2
            assert len(models.children) == 4
            parent_requests = [
                request
                for _, request, _ in models.requests
                if not (request.messages[0].content or "").startswith("你是独立的分析子 Agent")
            ]
            assert json.loads(parent_requests[1].messages[-1].content)["code"] == (
                "invalid_workflow_script"
            )
            assert json.loads(parent_requests[-1].messages[-1].content)["saveStatus"] == (
                "awaiting_confirmation"
            )
            with Session(engine) as tx:
                assert tx.scalars(
                    select(AgentStep.status)
                    .where(AgentStep.run_id == run_id, AgentStep.kind == "workflow")
                    .order_by(AgentStep.ordinal)
                ).all() == ["failed", "completed"]

    asyncio.run(scenario())


def ledger_attempt(
    runtime,
    repository,
    provider_id,
    context,
    *,
    usage,
    actual_cost,
    status="completed",
    reserved_tokens=0,
    reserved_cost=0,
    origin="live",
    run_id=None,
    agent_attempt_id=None,
):
    """Persist measured and unresolved accounting fixtures through real owner tables."""
    private_marker = "diagnostic-private-prompt-and-provider-secret"
    call_id, attempt_id = uuid4(), uuid4()
    repository.begin(call_id, provider_id, generate(private_marker), context)
    with Session(runtime.store.engine) as tx, tx.begin():
        clock = tx.scalar(select(func.clock_timestamp()))
        assert clock is not None
        call = tx.get(ModelCall, call_id)
        assert call is not None
        call.run_id, call.agent_attempt_id = run_id, agent_attempt_id
        call.status = "completed" if status == "completed" else "incomplete"
        call.execution = {"private": private_marker}
        content = (
            repository.secrets.seal("diagnostics-fixture", {"private": private_marker}).encode()
            if origin == "live"
            else None
        )
        call.request_content = call.output_content = content
        call.content_status = "captured" if content else "not_captured"
        tx.add(
            ModelAttempt(
                id=attempt_id,
                call_id=call_id,
                number=1,
                provider_id=provider_id,
                config_version=1,
                binding_version=1,
                requested_model=private_marker,
                status=status,
                origin=origin,
                request_content=content,
                output_content=content,
                sent=True,
                outcome_unknown=status != "completed" or origin != "live",
                cleanup_confirmed=status == "completed" and origin == "live",
                usage=usage,
                reserved_tokens=reserved_tokens,
                reserved_cost_microusd=reserved_cost,
                actual_cost_microusd=actual_cost,
                upstream_request_id=private_marker,
                created_at=clock,
                dispatched_at=clock,
                finished_at=clock if status == "completed" else None,
            )
        )
    return call_id, attempt_id


def test_diagnostics_tenant_usage_unknown_reservations_and_private_timeline(engine):
    async def scenario():
        async with runtime_service(engine) as runtime:
            repository = ModelRepository(
                engine, SecretStore(os.environ["COMPASS_MODEL_ENCRYPTION_KEY"]), ORIGINS
            )
            _, provider = create_provider(repository, alias="Scheduler diagnostics")
            run_a = await submit(runtime)
            await submit(runtime)
            run_b = await submit(runtime, TENANT_B)
            worker = ConsumerSlot("diagnostic-worker-a", uuid4())
            grant = await claim(runtime, worker, run_a)
            await claim(runtime, ConsumerSlot("diagnostic-worker-b", uuid4()), run_b)
            request = AdvanceCommand(grant=grant, expectedCheckpoint=0)
            advance, start = scheduler(runtime).prepare_advance(worker, request)
            assert start
            journal = ExecutionJournal(runtime.store, runtime.publisher, run_a, grant.epoch)
            private_marker = "diagnostic-private-prompt-and-provider-secret"

            async def fixture_result():
                return {"private": private_marker, "value": sum(range(20))}

            with pytest.raises(BoundaryYield):
                await journal.call(
                    "model", "diagnostic-model", {"private": private_marker}, fixture_result, dict
                )
            with Session(engine) as tx:
                logical = tx.scalars(select(AgentAttempt).where(AgentAttempt.run_id == run_a)).one()
                logical_id, step_id = logical.id, logical.step_id
                command_id = tx.scalars(
                    select(AgentCommand.id).where(AgentCommand.request_id == advance.command_id)
                ).one()
            call_a, physical_a = ledger_attempt(
                runtime,
                repository,
                provider.provider_id,
                owner(),
                usage={"input_tokens": 10, "output_tokens": 4, "reasoning_tokens": 6},
                actual_cost=7,
                run_id=run_a,
                agent_attempt_id=logical_id,
            )
            ledger_attempt(
                runtime,
                repository,
                provider.provider_id,
                owner(),
                usage={"input_tokens": 8, "output_tokens": 3, "reasoning_tokens": 1},
                actual_cost=11,
            )
            for status, tokens, cost in (("unknown", 100, 200), ("incomplete", 80, 150)):
                ledger_attempt(
                    runtime,
                    repository,
                    provider.provider_id,
                    owner(),
                    usage={},
                    actual_cost=None,
                    status=status,
                    reserved_tokens=tokens,
                    reserved_cost=cost,
                )
            historical_call, historical_attempt = ledger_attempt(
                runtime,
                repository,
                provider.provider_id,
                owner(),
                usage={"input_tokens": 999999, "output_tokens": 999999},
                actual_cost=None,
                status="unknown",
                origin="legacy_summary",
            )
            ledger_attempt(
                runtime,
                repository,
                provider.provider_id,
                owner(TENANT_B),
                usage={"input_tokens": 99, "output_tokens": 1},
                actual_cost=900,
            )
            ledger_attempt(
                runtime,
                repository,
                provider.provider_id,
                owner(TENANT_B),
                usage={},
                actual_cost=None,
                status="dispatched",
                reserved_tokens=1000,
                reserved_cost=2000,
            )
            before = counts(engine)
            diagnostics = RuntimeDiagnostics(runtime.store)
            a = diagnostics.usage(TENANT_A)
            assert (a.tenant_id, a.backlog, a.occupied) == (TENANT_A, 2, 1)
            assert a.jobs == {"ready": 1, "leased": 1, "reclaiming": 0, "parked": 0, "done": 0}
            assert (a.settled_tokens, a.settled_cost_microusd) == (27, 18)
            assert (a.unknown_attempts, a.reserved_tokens, a.reserved_cost_microusd) == (
                2,
                180,
                350,
            )
            b = diagnostics.usage(TENANT_B)
            assert (b.tenant_id, b.backlog, b.occupied) == (TENANT_B, 1, 1)
            assert (b.settled_tokens, b.settled_cost_microusd) == (100, 900)
            assert (b.unknown_attempts, b.reserved_tokens, b.reserved_cost_microusd) == (
                1,
                1000,
                2000,
            )
            empty = diagnostics.usage(TENANT_C)
            assert (
                empty.backlog,
                empty.occupied,
                empty.unknown_attempts,
                empty.settled_tokens,
            ) == (0, 0, 0, 0)
            assert empty.settled_cost_microusd == 0
            timeline = diagnostics.timeline(run_a)
            encoded = timeline.model_dump_json(by_alias=True)
            assert private_marker not in encoded and "unit-key-never-return" not in encoded
            assert str(run_b) not in encoded and str(historical_call) not in encoded
            entries = {entry.id: entry for entry in timeline.entries}
            assert len(entries) == len(timeline.entries)
            assert entries[str(step_id)].kind == "step"
            assert entries[str(logical_id)].step_id == step_id
            assert entries[str(logical_id)].epoch == grant.epoch
            assert entries[str(physical_a)].kind == "model"
            assert entries[str(physical_a)].model_call_id == call_a
            assert entries[str(physical_a)].attempt_id == logical_id
            assert entries[str(command_id)].kind == "command"
            assert entries[str(command_id)].worker_id == worker.worker_id
            assert entries[str(command_id)].epoch == grant.epoch
            assert entries[str(command_id)].reason_code == "continue"
            assert str(historical_attempt) not in entries
            assert counts(engine) == before
            with Session(engine) as tx:
                legacy_call = tx.get(ModelCall, historical_call)
                legacy_attempt = tx.get(ModelAttempt, historical_attempt)
                assert legacy_call is not None and legacy_attempt is not None
                assert legacy_call.run_id is None and legacy_call.agent_attempt_id is None
                assert legacy_attempt.origin == "legacy_summary"
                assert (
                    legacy_attempt.request_content is None and legacy_attempt.output_content is None
                )
            ledger_attempt(
                runtime,
                repository,
                provider.provider_id,
                owner(),
                usage={"input_tokens": 4, "output_tokens": 2},
                actual_cost=None,
            )
            unpriced = diagnostics.usage(TENANT_A)
            assert unpriced.settled_tokens == 33 and unpriced.settled_cost_microusd is None
            assert (
                unpriced.unknown_attempts,
                unpriced.reserved_tokens,
                unpriced.reserved_cost_microusd,
            ) == (2, 180, 350)
            assert diagnostics.usage(TENANT_B) == b

    asyncio.run(scenario())


def legacy_records(engine):
    with Session(engine) as tx:
        session_columns = [
            column
            for column in AgentSession.__table__.columns
            if column.name not in {"title_status", "title_attempts", "title_next_at", "deleted_at"}
        ]
        return {
            model.__tablename__: [
                dict(row._mapping)
                for row in tx.execute(
                    (
                        select(*session_columns)
                        if model is AgentSession
                        else select(model.__table__)
                    ).order_by(*model.__table__.primary_key)
                )
            ]
            for model in (
                AgentSession,
                AgentRun,
                AgentMessage,
                AgentCommand,
                AgentOutbox,
                AgentStep,
                AgentAttempt,
                AgentCheckpoint,
            )
        }


@pytest.mark.parametrize("mode", ["native", "sql"])
def test_0009_upgrade_preserves_completed_runtime_records_and_runtime_permissions(targets, mode):
    source_url, _ = targets("scheduler_upgrade_source")
    assert db.migrate() == "applied"
    source_engine = create_engine(source_url)
    runtime_url, _ = targets("scheduler_upgrade_" + mode)
    assert db.migrate(target="0009") == "applied"
    engine = create_engine(runtime_url)

    async def legacy_run():
        async with legacy_service(source_engine) as runtime:
            # 0009 predates managed context frames and encrypted source projections.
            runtime.store.context_enabled = False
            assert runtime.store.scheduler is None
            executor = BoundaryRunner()
            executor.release.set()
            receipt = runtime.store.submit(command(question="计算 0 至 19 的整数总和。"), owner())
            run_id = UUID(receipt["runId"])
            runtime.schedule(run_id, executor, owner())
            await asyncio.wait_for(runtime.tasks[run_id], 10)
            result = runtime.store.read_run(run_id)
            assert result.status == "completed" and executor.calls == 1
            assert any(
                packet.event.type == "message.upsert"
                and packet.event.payload.status == "completed"
                and packet.event.payload.content.text == "计算结果为 190。"
                for packet in runtime.reader.messages(result.session_id, owner()).packets
            )
            return run_id

    try:
        run_id = asyncio.run(legacy_run())
        before = legacy_records(source_engine)
        with engine.begin() as tx:
            for model in (
                AgentSession,
                AgentRun,
                AgentStep,
                AgentAttempt,
                AgentMessage,
                AgentCommand,
                AgentOutbox,
                AgentCheckpoint,
            ):
                table_name = model.__tablename__
                if before[table_name]:
                    table = Table(table_name, MetaData(), autoload_with=tx)
                    tx.execute(
                        table.insert(),
                        [
                            {col.name: row[col.name] for col in table.columns}
                            for row in before[table_name]
                        ],
                    )
        assert len(before["agent_session"]) == len(before["agent_run"]) == 1
        assert before["agent_command"] and before["agent_outbox"] and before["agent_checkpoint"]
        assert db.migrate(mode=mode, start="0009") == "applied"
        assert db.migrate(mode=mode, start="0009") == "already_applied"
        assert legacy_records(engine) == before
        with Session(engine) as tx:
            assert tx.scalar(text("SELECT version_num FROM alembic_version")) == db.TARGET
            assert tx.scalar(select(func.count()).select_from(AgentRunJob)) == 0
            assert tx.scalar(select(func.count()).select_from(AgentTenantSchedule)) == 0
            run = tx.get(AgentRun, run_id)
            assert run is not None and run.status == "completed"
            assert run.execution_epoch is None
        with psycopg.connect(db.dsn(runtime_url)) as connection:
            for table in ("agent_run_job", "agent_tenant_schedule"):
                assert connection.execute(
                    "SELECT has_table_privilege(current_user, %s, 'SELECT,INSERT,UPDATE,DELETE')",
                    (table,),
                ).fetchone() == (True,)
            for statement in (
                "CREATE TABLE unauthorized_scheduler_ddl(id int)",
                "UPDATE alembic_version SET version_num='0009'",
                "DROP TABLE agent_run_job",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with connection.transaction():
                        connection.execute(statement)
        assert legacy_records(engine) == before
    finally:
        engine.dispose()
        source_engine.dispose()


def test_old_epoch_boundary_cannot_adopt_a_new_worker_grant(engine):
    async def scenario():
        async with runtime_service(engine, compass_context_runtime_enabled=False) as runtime:
            run_id = await submit(runtime)
            previous_worker = ConsumerSlot("previous-worker", uuid4())
            previous = await claim(runtime, previous_worker, run_id)
            scheduler(runtime).release(previous_worker, previous)
            await runtime.recover_scheduled()
            assert runtime.store.read_run(run_id).execution_epoch is None
            with pytest.raises(ModelError, match="execution_fenced"):
                runtime.store.progress(run_id, None, [])
            await runtime.publisher.flush(run_id)
            worker = ConsumerSlot("replacement-worker", uuid4())
            replacement = await claim(runtime, worker, run_id)
            assert replacement.epoch != previous.epoch
            before = counts(engine)
            current = runtime.store.read_run(run_id)
            executor = BoundaryRunner()
            executor.release.set()
            runtime.start_boundary(run_id, executor, owner(), previous.epoch)
            await asyncio.wait_for(runtime.tasks[run_id], 5)
            assert executor.calls == 0
            assert counts(engine) == before
            after = runtime.store.read_run(run_id)
            assert (after.status, after.version, after.execution_epoch, after.projected_seq) == (
                current.status,
                current.version,
                replacement.epoch,
                current.projected_seq,
            )
            assert job(engine, run_id).worker_id == worker.worker_id
            result, checkpoints = await advance_to_boundary_stop(
                runtime, worker, replacement, executor, owner()
            )
            assert checkpoints == [1] and result.status == "done"
            assert executor.calls == 1
            assert runtime.store.read_run(run_id).status == "completed"

    asyncio.run(scenario())


def test_due_triggers_keep_published_context_through_worker_takeover(engine, monkeypatch):
    """Cross the publication boundary while a real scheduled Run has an accepted model step."""
    from compass_agent.api import promptops
    from compass_agent.context_runtime.model_step import call_model
    from compass_agent.llm.contracts import AgentCallResponse
    from compass_agent.main import app
    from compass_agent.runtime.context import RunRoute
    from compass_agent.scheduler.admission import TriggerAdmissionService
    from compass_agent.scheduler.entities import AgentSchedule, AgentScheduleTrigger
    from compass_agent.scheduler.lease import SchedulerLeaseStore
    from compass_agent.scheduler.repository import ScheduleRepository
    from compass_agent.scheduler.service import ScheduleService
    from compass_agent.schemas import ChatMessage, GenerateInput, ModelTokenLimits, ScheduleCreate
    from compass_agent.settings import get_settings
    from fastapi.testclient import TestClient
    from sqlalchemy.engine import make_url

    from .test_promptops import headers, publish, save

    admin_url = make_url(os.environ["AGENT_TEST_ADMIN_URL"]).set(
        database=engine.url.database, drivername="postgresql+psycopg"
    )
    scan_engine = create_engine(
        admin_url, connect_args={"options": "-c role=compass_agent_scheduler"}
    )
    monkeypatch.setattr(promptops, "get_engine", lambda: engine)
    monkeypatch.setattr(get_settings(), "compass_agent_service_token", "promptops-test-credential")
    editor = TestClient(app, base_url="http://testserver/v1/promptops/")
    editor.headers.update(headers(uuid4()))
    path = "tasks/business_qa/system.md"
    marker = "本次定时发布验收的版本标记"

    async def scenario():
        async with runtime_service(
            engine, compass_runtime_tenant_active=3, compass_runtime_max_active=3
        ) as runtime:
            context = owner()
            plans = ScheduleService(runtime.store, runtime.access, None, runtime.settings)
            scans = ScheduleRepository(
                scan_engine, runtime.settings.compass_runtime_state_key.get_secret_value()
            )
            leases = SchedulerLeaseStore(scan_engine)
            lease = leases.acquire(uuid4())
            assert lease is not None
            admission = TriggerAdmissionService(runtime)
            audience = command()
            route = RunRoute(
                uuid4(),
                1,
                "fixture",
                "SC-01",
                "",
                False,
                False,
                deliverable="business_qa",
                limits=ModelTokenLimits(max_input_tokens=65536),
            )
            draft = GenerateInput(messages=[ChatMessage(role="user", content=audience.question)])
            invocations = []

            async def invoke(step, value, *args, **kwargs):
                invocations.append(step.id)
                return AgentCallResponse.model_validate(model_result(text="定时问答").model_dump())

            target = SimpleNamespace(
                models=SimpleNamespace(root_observation=None), _invoke_model_live=invoke
            )

            async def deliver(name):
                with Session(engine) as tx, tx.begin():
                    value = ScheduleCreate(
                        requestId=uuid4(),
                        name=name,
                        question=audience.question,
                        cron="0 0 * * *",
                        timezone="UTC",
                        selection=audience.selection,
                    )
                    receipt = plans.apply(tx, context, None, value)
                    plan = tx.get(AgentSchedule, receipt.id)
                    assert plan is not None
                    plan.next_fire_at = scheduler(runtime).clock(tx) - timedelta(days=1)
                leases.renew(lease)
                assert scans.scan(lease) == 1 and scans.scan(lease) == 0
                [delivery] = scans.claim(lease, reconciliation=False)
                receipt = await admission.submit(delivery.trigger_id, delivery.token)
                assert receipt.run_id is not None
                assert await admission.submit(delivery.trigger_id, delivery.token) == receipt
                assert await admission.reconcile(delivery.trigger_id, delivery.token) == receipt
                # This controlled publication fixture models a persisted v1 answer Run.
                # Keep its real published Context pin while bypassing the separate v2 review loop.
                with runtime.store.transaction() as tx:
                    _, run = runtime.store.lock(tx, receipt.run_id)
                    binding = {**run.config_snapshot["contextRuntime"], "answerProtocol": 1}
                    run.config_snapshot = {**run.config_snapshot, "contextRuntime": binding}
                await runtime.publisher.flush(receipt.run_id)
                return receipt.run_id

            async def model_step(grant, step_id, *, replay=False):
                journal = ExecutionJournal(
                    runtime.store, runtime.publisher, grant.run_id, grant.epoch
                )
                journal.context_phase = "generate"
                journal.capabilities = lambda: capability_grants(context)
                step = SimpleNamespace(id=step_id)
                invocation = call_model(
                    journal, target, step, draft, route, context, None, None, None
                )
                if replay:
                    result = await invocation
                    assert result.text == "定时问答"
                else:
                    with pytest.raises(BoundaryYield):
                        await invocation
                journal.validate_checkpoint()

            def assert_checkpoint(run_id, generation, version, steps):
                with Session(engine) as tx:
                    run = tx.get(AgentRun, run_id)
                    assert (
                        run is not None
                        and run.config_snapshot["contextRuntime"]["generation"] == generation
                    )
                    checkpoint_row = tx.get(AgentCheckpoint, (run_id, run.checkpoint_no))
                    assert (
                        checkpoint_row is not None and len(checkpoint_row.completed_refs) == steps
                    )
                    for ref in checkpoint_row.completed_refs:
                        step = tx.get(AgentStep, UUID(ref["stepId"]))
                        assert step is not None and step.accepted_attempt_id == UUID(
                            ref["attemptId"]
                        )
                        assert step.prepared_context is not None
                        frame = runtime.store.private.open(
                            run_id, "model-frame", step.prepared_context
                        )
                        assert frame["receipt"]["generation"] == generation
                        assert [
                            (f["path"], f["version"])
                            for f in frame["receipt"]["files"]
                            if f["path"] == path
                        ] == [(path, version)]
                        system = frame["input"]["request"]["messages"][0]["content"]
                        assert "Asia/Shanghai" in system
                        organization = frame["input"]["request"]["messages"][1]
                        from .test_context_runtime_integration import model_json

                        assert model_json(organization["content"])["workspaceName"] == "调度组"
                        assert (marker in system) == (version == 2)

            original = editor.get("tree").json()
            old_id = await deliver("发布前")
            worker = ConsumerSlot("before-release", uuid4())
            old = await claim(runtime, worker, old_id)
            first_step = uuid4()
            await model_step(old, first_step)
            assert_checkpoint(old_id, original["generation"], 1, 1)
            save(editor, path, marker)
            updated = publish(editor, "file", path=path)
            during_id = await deliver("单文件发布后")
            restored = publish(editor, "restore", snapshot_no=original["snapshot_no"])
            after_id = await deliver("恢复后")
            for rid, expected_generation, expected_version in (
                (during_id, updated["generation"], 2),
                (after_id, restored["generation"], 1),
            ):
                grant = await claim(runtime, ConsumerSlot("next-run", uuid4()), rid)
                await model_step(grant, uuid4())
                assert_checkpoint(rid, expected_generation, expected_version, 1)
                runtime.store.finish(rid, grant.epoch, "completed", answer="定时问答")
                await runtime.publisher.flush(rid)
            expire_lease(engine, old_id)
            await runtime.recover_scheduled()
            await runtime.publisher.flush(old_id)
            assert job(engine, old_id).status == "ready", runtime.store.read_run(old_id).reason_code
            resumed = await claim(runtime, ConsumerSlot("replacement", uuid4()), old_id)
            assert resumed.epoch != old.epoch and resumed.checkpoint_no == 1
            await model_step(resumed, first_step, replay=True)
            await model_step(resumed, uuid4())
            assert_checkpoint(old_id, original["generation"], 1, 2)
            runtime.store.finish(old_id, resumed.epoch, "completed", answer="定时问答")
            await runtime.publisher.flush(old_id)
            leases.renew(lease)
            assert scans.sync(lease) == 3
            with Session(engine) as tx:
                rows = tx.scalars(select(AgentScheduleTrigger)).all()
                expected_runs = {old_id, during_id, after_id}
                assert {t.run_id for t in rows} == expected_runs
                assert set(tx.scalars(select(AgentRun.id))) == expected_runs
                assert set(tx.scalars(select(AgentRunJob.run_id))) == expected_runs
                assert len(rows) == 3 and all(
                    t.finished_at and t.delivery_attempts == 1 for t in rows
                )
                assert (
                    tx.scalar(select(func.count()).select_from(AgentAttempt))
                    == len(set(invocations))
                    == len(invocations)
                    == 4
                )
            leases.release(lease)

    try:
        asyncio.run(scenario())
    finally:
        editor.close()
        scan_engine.dispose()
