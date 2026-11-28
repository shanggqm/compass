"""Lesson 27 real PostgreSQL contracts, frozen assembly and answer-v3 replay."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

import pytest
from compass_agent.context_runtime.citations import read_citations
from compass_agent.execution_context import current_execution
from compass_agent.harness.context import AgentDefinition
from compass_agent.llm.contracts import AgentCallResponse
from compass_agent.models.secrets import SecretStore
from compass_agent.runtime.context import RunRoute
from compass_agent.runtime.control import OwnerKey, RunRegistry
from compass_agent.runtime.execution_trace import ExecutionTrace
from compass_agent.runtime.journal import ExecutionJournal
from compass_agent.runtime.store import RuntimeStore
from compass_agent.runtime.task_state import TaskState
from compass_agent.runtime.tool_bindings import RunToolBindings
from compass_agent.runtime.turn import RuntimeTurn
from compass_agent.schemas import ChatMessage, SessionSubmit
from compass_agent.semantic.evidence import result as query_evidence
from compass_agent.settings import Settings
from compass_agent.tools.catalog_contracts import CatalogContext
from compass_agent.tools.contracts import ExecutionReceipt, ToolGovernance
from compass_agent.tools.loader import ToolLoader
from cryptography.fernet import Fernet
from sqlalchemy import create_engine

from .context_fixture import capability_grants, install_prompts
from .test_agent_run import CHAT, PROVIDER_ID, model_result
from .test_context_runtime_integration import LIMITS, LocalPublisher
from .test_database_tools_integration import db
from .test_database_tools_integration import targets as targets
from .test_generic_database_integration import data_source as data_source
from .test_goal_runtime_integration import publish as publish_pending
from .test_tool_dispatcher import call, registration
from .test_tool_dispatcher import context as tool_context


def test_goal_next_round_inherits_frozen_tool_selection(targets):
    from compass_agent.schemas import RunConstraints, ToolSelection

    from .test_goal_runtime_integration import approved_goal, claim, create_store, finish, latest

    url, _ = targets("lesson27_goal_selection")
    db.migrate()
    engine = install_prompts(create_engine(url))
    selection = ToolSelection(
        packs=["long_task", "python_sandbox"],
        workflowIds=[uuid4()],
        mcpServerIds=[uuid4()],
    )
    try:
        runtime = approved_goal(
            create_store(engine, Settings(compass_context_runtime_enabled=True)),
            constraints=RunConstraints(toolSelection=selection),
        )
        expected = selection.model_dump(mode="json", by_alias=True)
        assert runtime.journal.tool_selection == expected
        initial_command = runtime.store.history_command(runtime.journal.run_id)
        assert (
            initial_command.constraints and initial_command.constraints.tool_selection == selection
        )
        # The controller retries a failed round without changing the user's tool choice.
        finish(runtime, status="failed")
        runtime.controller.tick()
        successor = latest(runtime)
        assert successor.id != runtime.journal.run_id
        assert successor.config_snapshot["command"]["constraints"]["toolSelection"] == expected
        assert successor.config_snapshot["contextRuntime"]["answerProtocol"] == 3
        next_journal = claim(runtime.store, successor.id)
        assert next_journal.tool_selection == expected
        assert next_journal.goal_id == str(runtime.goal_id)
        continued_command = runtime.store.history_command(successor.id)
        assert (
            continued_command.constraints
            and continued_command.constraints.tool_selection == selection
        )
        finish(runtime, next_journal, status="failed")
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mode",
    [
        "plain",
        "evidence",
        "markdown",
        "csv",
        "dashboard",
        "mixed",
        "direct",
        "forced",
        "child",
        "revoked_before",
        "revoked_during",
        "canceled",
        "review_missing",
        "partial",
        "legacy",
    ],
)
def test_v3_stream_delivery_and_replay(targets, mode, monkeypatch):
    url, _ = targets("lesson27")
    db.migrate()
    engine = install_prompts(create_engine(url))
    key = Fernet.generate_key().decode()
    store = RuntimeStore(engine, key, settings=Settings(compass_context_runtime_enabled=True))
    store.ready = True
    command = SessionSubmit(
        requestId=uuid4(),
        question="说明本次查询结果",
        selection=cast(Any, {"mode": "fixed", "provider_id": PROVIDER_ID}),
    )
    run_id = UUID(store.submit(command, CHAT)["runId"])
    if mode == "legacy":
        with store.transaction() as tx:
            _, run = store.lock(tx, run_id)
            config = dict(run.config_snapshot)
            config["contextRuntime"] = {**config["contextRuntime"], "completionReview": 0}
            run.config_snapshot = config
    assert store.read_run(run_id).config_snapshot["contextRuntime"]["answerProtocol"] == 3
    epoch = store.claim(run_id, uuid4())
    secrets = SecretStore(key)
    invocations, effects, events, states = [], [], [], []
    revoked = mode == "revoked_before"
    if mode.startswith("revoked_"):
        from compass_agent.runtime import stream_answer_turn

        original_access = stream_answer_turn.check_access
        monkeypatch.setattr(
            stream_answer_turn, "check_access", lambda *a: not revoked and original_access(*a)
        )
    evidential = mode not in {"plain", "direct", "forced"}

    async def lookup(args, context):
        from compass_agent.execution_context import recorded

        async def read():
            effects.append(context.call_id)
            return {"resultId": "stock", "columns": ["quantity"], "rows": [[17]]}

        payload = await recorded("tool", "lookup:" + context.call_id, {}, read, dict)
        return ExecutionReceipt(context.call_id, "ok", "confirmed", payload, source_deps=())

    entry = replace(
        registration("lookup_stock", lookup),
        governance=ToolGovernance(channel="always_on", pack="data_base", subagent_allowed=True),
        evidence_adapter=query_evidence,
    )

    class Turn(RuntimeTurn):
        definition = AgentDefinition()

        async def _invoke_model_live(
            self, step, request, route, context, control, budget, emit=None, *, publish_text=True
        ):
            invocations.append(request)
            body = ""
            from compass_agent.execution_context import execution_path

            if "subagent:" in execution_path.get():
                assert {t.function.name for t in request.tools} == {
                    "lookup_stock",
                    "report_to_parent",
                }
                child_evidence = getattr(current_execution.get(), "answer_evidence", {}).get(
                    execution_path.get().rsplit("/model:", 1)[0], {}
                )
                ids = [key for key, item in child_evidence.items() if item["kind"] == "query"]
                calls = (
                    [
                        call(
                            "report_to_parent",
                            "report",
                            args=json.dumps(
                                {
                                    "summary": "库存17件",
                                    "evidenceIds": ids,
                                    "gaps": [],
                                    "status": "completed",
                                }
                            ),
                        )
                    ]
                    if ids
                    else [call("lookup_stock", "lookup")]
                )
                return AgentCallResponse.model_validate(
                    model_result(
                        tool_calls=calls,
                        token=secrets.seal("tool_continuation", {"fixture": True}),
                        finish="tool_calls",
                    ).model_dump()
                )
            if not request.tools:
                assert publish_text and request.stream
                assert "不调用工具" in (request.messages[0].content or "")
                if evidential:
                    assert "已排序的材料" in (request.messages[0].content or "")
                    from compass_agent.context_runtime.stream_answer import ordered_evidence

                    items = ordered_evidence(current_execution.get().answer_evidence["root"])
                    ordinal = next(i for i, item in enumerate(items, 1) if item["kind"] == "query")
                    body = f"库存为 17 件 ^[{ordinal}]^。"
                else:
                    body = "本次说明已完成。"
                out = model_result(text=body)
            else:
                assert not publish_text and not request.stream
                assert [t.function.name for t in request.tools] == (
                    ["final_answer"]
                    if mode == "forced"
                    else ["lookup_stock", "delegate_lookup", "final_answer"]
                    if mode == "child"
                    else ["lookup_stock", "final_answer"]
                )
                prior = [m for m in request.messages if m.role == "tool"]
                if mode == "review_missing" and not prior:
                    calls = [call("final_answer", "early", args='{"mode":"evidence"}')]
                elif evidential and (
                    not prior
                    or (
                        mode == "review_missing"
                        and not any(m.tool_call_id == "lookup" for m in prior)
                    )
                ):
                    calls = [
                        call("delegate_lookup" if mode == "child" else "lookup_stock", "lookup")
                    ]
                    if mode == "mixed":
                        calls.append(call("final_answer", "early", args='{"mode":"evidence"}'))
                else:
                    if mode == "mixed":
                        assert "completion_deferred" in str(prior)
                    args = {"mode": "evidence" if evidential else "plain"}
                    if mode == "partial":
                        args["completion"] = "partial"
                    if mode in {"markdown", "csv", "dashboard"}:
                        args["artifactFormat"] = mode
                    calls = [call("final_answer", "complete", args=json.dumps(args))]
                out = model_result(
                    tool_calls=calls,
                    token=secrets.seal("tool_continuation", {"fixture": True}),
                    finish="tool_calls",
                )
            response = AgentCallResponse.model_validate(out.model_dump())
            if request.stream:
                from compass_agent.schemas import TextDeltaEvent

                async def prepare(*args, **kwargs):
                    return None

                async def run(*args, emit=None, **kwargs):
                    nonlocal revoked
                    assert emit is not None
                    await emit(
                        TextDeltaEvent(call_id=step.model_call_id, type="text_delta", text=body[:5])
                    )
                    if mode == "revoked_during":
                        revoked = True
                    if mode == "canceled":
                        from compass_agent.runtime.control import RunControl

                        assert isinstance(control, RunControl)
                        control.request_stop("user")
                        await asyncio.sleep(0)
                    await emit(
                        TextDeltaEvent(call_id=step.model_call_id, type="text_delta", text=body[5:])
                    )
                    return response

                self.models = cast(
                    Any, SimpleNamespace(root_observation=None, prepare=prepare, run=run)
                )
                return await super()._invoke_model_live(
                    step, request, route, context, control, budget, emit, publish_text=publish_text
                )
            return response

    class Publisher(LocalPublisher):
        def notify(self, run_id):
            from compass_agent.runtime.answer_publication import hydrate_chunk, marker_data

            with store.transaction() as tx:
                for box in store.pending(run_id):
                    for packet in box.payload["packets"]:
                        if data := marker_data(packet["event"]):
                            events.append(
                                {**packet, "event": hydrate_chunk(store, tx, run_id, data)}
                            )
            publish_pending(store, run_id)

    async def perform(*, replay=False):
        journal = ExecutionJournal(store, Publisher(), run_id, epoch)
        journal.model_secrets = secrets
        journal.capabilities = lambda: capability_grants(CHAT)
        state = TaskState([ChatMessage(role="user", content=command.question)])
        states.append(state)
        state.trace = ExecutionTrace(events.append)
        turn = Turn(cast(Any, SimpleNamespace(root_observation=None)))
        route = RunRoute(
            PROVIDER_ID,
            1,
            "fixture",
            "SC-33" if mode == "direct" else "SC-01",
            "",
            True,
            False,
            tools_enabled=True,
            deliverable="business_qa",
            limits=LIMITS,
        )
        control = RunRegistry().register(OwnerKey.from_context(CHAT), run_id=run_id)
        if replay:
            control.deadline_at = asyncio.get_running_loop().time() + 20
        token = current_execution.set(journal)
        try:

            async def delegate(_args, context):
                assert state.tools is not None
                result = await state.tools.subagent.run("查询库存", context)
                assert result["status"] == "completed" and result["evidenceIds"], result
                return ExecutionReceipt(context.call_id, "ok", "confirmed", result, source_deps=())

            from compass_agent.tools.builtin.sub_agent import evidence as child_evidence
            from compass_agent.tools.contracts import InProcessBinding

            child_entry = replace(
                registration("delegate_lookup", delegate),
                binding=InProcessBinding(delegate, orchestrates=True),
                governance=ToolGovernance(channel="always_on"),
                evidence_adapter=child_evidence,
            )
            state.tools = RunToolBindings(
                turn,
                state,
                route,
                CHAT,
                control,
                None,
                state.trace.emit,
                ToolLoader(),
                None,
                entries=(entry, child_entry) if mode == "child" else (entry,),
                catalog=CatalogContext(frozenset({"completion"})),
            )
            return await turn.execute_turn(
                state,
                route,
                CHAT,
                control,
                None,
                state.trace.emit,
                max_steps=2 if mode == "forced" else 12,
            )
        finally:
            current_execution.reset(token)

    try:
        if mode.startswith("revoked_") or mode == "canceled":
            from compass_agent.harness.contracts import RunFailure
            from compass_agent.runtime.control import StopRequested

            expected = StopRequested if mode == "canceled" else RunFailure
            with pytest.raises(expected):
                asyncio.run(perform())
            with store.transaction() as tx:
                from compass_agent.context_runtime.source_access import read_private

                accepted = uuid5(run_id, f"answer-v3:root:{states[-1].steps[-1].id}")
                assert read_private(store, tx, run_id, f"answer/v3/accepted/{accepted}") is None
            if mode == "revoked_before":
                assert not any(r.stream for r in invocations)
            return
        done = asyncio.run(perform())
        assert done.status == "completed"
        assert done._answer_acceptance["completion"] == (
            "partial" if mode in {"partial", "forced"} else "complete"
        )
        reviewed = sum(
            any("交付前做一次完成度复核" in (m.content or "") for m in request.messages)
            for request in invocations
            if request.tools
        )
        if mode in {"legacy", "direct", "forced"}:
            assert reviewed == 0
        else:
            assert reviewed >= 1
        assert any(
            isinstance(event, dict) and event.get("event", {}).get("type") == "message.delta"
            for event in events
        )
        count = len(invocations)
        store.recover_boot(uuid4())
        epoch = store.claim(run_id, uuid4())
        repeated = asyncio.run(perform(replay=True))
        assert repeated == done and len(invocations) == count
        assert effects == (["lookup"] if evidential else [])
        if mode in {"markdown", "csv", "dashboard"}:
            assert done._delivery is not None and b"17" in done._delivery.data
        store.finish(
            run_id,
            epoch,
            "completed",
            answer=done.final_answer,
            citations=done._citations,
            delivery=done._delivery,
            answer_acceptance=done._answer_acceptance,
        )
        publish_pending(store, run_id)
        index = read_citations(store, run_id, CHAT)
        assert bool(index["anchors"]) == evidential, (done._citations, index)
        if done._delivery is not None:
            from compass_agent.runtime.entities import AgentArtifact
            from sqlalchemy import select

            with store.transaction() as tx:
                artifacts = tx.scalars(
                    select(AgentArtifact.id).where(AgentArtifact.run_id == run_id)
                ).all()
            assert len(artifacts) == 1
            assert store.artifact(run_id, artifacts[0], CHAT).content == done._delivery.data
            from compass_agent.models.errors import ModelError

            with pytest.raises(ModelError, match="not_found"):
                store.artifact(
                    run_id, artifacts[0], CHAT.model_copy(update={"actor_user_id": uuid4()})
                )
    finally:
        engine.dispose()


def test_sql_metadata_and_undefined_column_repair(data_source):
    from compass_agent.tools.builtin.database_query import DatabaseResult, query_database
    from compass_agent.tools.errors import ActionableToolError

    _, _, binding = data_source

    async def scenario():
        control = RunRegistry().register(OwnerKey.from_context(CHAT))

        async def query(sql):
            return await query_database(
                call("database_query", "q", args=json.dumps({"op": "query", "sql": sql})),
                binding,
                control,
            )

        metadata = await query(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema='analytics_978'"
        )
        assert isinstance(metadata, DatabaseResult) and metadata.rows
        assert metadata.source_ref is not None and metadata.source_ref["kind"] == "metadata"
        wrong = await query("SELECT unknown_amount FROM analytics_978.daily_fact")
        assert isinstance(wrong, ActionableToolError) and "amount_cent" in wrong.candidates
        assert "private_978" not in str(wrong.payload())
        repaired = await query("SELECT sum(amount_cent) FROM analytics_978.daily_fact")
        assert isinstance(repaired, DatabaseResult) and repaired.rows == ((1050,),)

    asyncio.run(scenario())


@pytest.fixture
def runtime27(targets):
    url, _ = targets("lesson27_state")
    db.migrate()
    engine = install_prompts(create_engine(url))
    store = RuntimeStore(
        engine,
        Fernet.generate_key().decode(),
        settings=Settings(compass_context_runtime_enabled=True),
    )
    store.ready = True
    wf = uuid4()
    command = SessionSubmit.model_validate_json(
        json.dumps(
            {
                "requestId": str(uuid4()),
                "question": "核对数据",
                "selection": {"mode": "fixed", "provider_id": str(PROVIDER_ID)},
                "constraints": {
                    "toolSelection": {"packs": ["workflow_pack"], "workflowIds": [str(wf)]}
                },
            }
        )
    )
    run_id = UUID(store.submit(command, CHAT, run_environment={"timezone": "Asia/Tokyo"})["runId"])
    epoch = store.claim(run_id, uuid4())
    try:
        yield store, run_id, epoch, wf
    finally:
        engine.dispose()


def test_49_tool_frozen_pool_and_work_prefix_survive_another_owner(runtime27):
    from hashlib import sha256

    from compass_agent.context_runtime.contracts import ContextRequest
    from compass_agent.context_runtime.tool_assembly import assemble, freeze_work_entry
    from compass_agent.schemas import GenerateInput
    from compass_agent.tools.contracts import json_bytes
    from compass_agent.tools.registry import ToolRegistry

    from .test_tool_assembly import entries, extension

    store, run_id, epoch, wf = runtime27
    assert CHAT.scope.kind == "tenant"
    pool = entries()
    for i in range(30):
        name = f"workflow_{(wf if i == 0 else UUID(int=i)).hex}"
        pool[name] = extension(name, workflow_id=wf if i == 0 else UUID(int=i))
    registry = ToolRegistry(pool.values())
    assert len(registry.entries) == 49
    first = ExecutionJournal(store, LocalPublisher(), run_id, epoch)
    first.tool_entries = dict(registry.entries)
    first.freeze_catalog(registry)
    freeze_work_entry(first, required_tools=("python_analysis",))

    def visible(journal):
        assert CHAT.scope.kind == "tenant"
        with store.transaction() as tx:
            draft = GenerateInput(messages=[ChatMessage(role="user", content="核对")])
            draft = draft.model_copy(update={"tools": registry.definitions()})
            request = ContextRequest(
                str(run_id),
                "model:1",
                str(CHAT.scope.tenant_id),
                str(CHAT.scope.workspace_id),
                "platform",
                "business_qa",
                "SC-01",
                "query",
                journal.context_binding["generation"],
                1,
            )
            result, trace = assemble(
                journal, tx, request, draft, {"plugins": True, "semantic_maintainer": True}
            )
            assert trace["authorized_count"] == 49 and trace["exposed_count"] == 14
            assert "python_analysis" in {tool.function.name for tool in result.tools}
            return sha256(json_bytes([d.model_dump() for d in result.tools])).hexdigest()

    before = visible(first)
    store.recover_boot(uuid4())
    second_epoch = store.claim(run_id, uuid4())
    second = ExecutionJournal(store, LocalPublisher(), run_id, second_epoch)
    second.tool_entries = dict(registry.entries)
    second.entry_plan_active = True
    second.freeze_catalog(registry)
    freeze_work_entry(second)
    assert second.tool_selection == first.tool_selection
    assert visible(second) == before
    with pytest.raises(Exception, match="execution_fenced"):
        store.require_epoch(store.read_run(run_id), epoch)


def test_backend_admission_refusal_survives_worker_replay(runtime27):
    from compass_agent.tools.artifacts import ArtifactStore
    from compass_agent.tools.batch import dispatch_batch
    from compass_agent.tools.contracts import SandboxBinding, ToolBatchPolicy, ToolRejected
    from compass_agent.tools.dispatcher import ExecutionSlots, ToolDispatcher
    from compass_agent.tools.registry import ToolRegistry

    store, run_id, epoch, _ = runtime27
    calls = []

    class Backend:
        async def execute(self, binding, args, context):
            calls.append(context.call_id)
            raise ToolRejected("sandbox_capacity")

    async def unused(args, context):
        raise AssertionError("external binding must use its backend")

    entry = replace(registration("backend_read", unused), binding=SandboxBinding())
    dispatcher = ToolDispatcher(
        ToolRegistry([entry]), ExecutionSlots(), ArtifactStore(), sandbox=Backend()
    )
    step_id = uuid4()

    async def perform(owner_epoch):
        ctx = replace(tool_context("backend_read"), run_id=run_id, step_id=step_id)
        journal = ExecutionJournal(store, LocalPublisher(), run_id, owner_epoch)
        token = current_execution.set(journal)
        try:
            result = await dispatch_batch(
                dispatcher, (call("backend_read", "read"),), ctx, ToolBatchPolicy()
            )
            assert result[0].status == "rejected" and result[0].cleanup == "not_started"
            assert result[0].reason_code == "sandbox_capacity"
            assert ctx.control.cleanup_confirmed
            return result[0]
        finally:
            current_execution.reset(token)

    first = asyncio.run(perform(epoch))
    store.recover_boot(uuid4())
    next_epoch = store.claim(run_id, uuid4())
    assert asyncio.run(perform(next_epoch)) == first
    assert calls == ["read"]


def test_legacy_static_declaration_restored_without_using_new_adapter(runtime27):
    from pathlib import Path

    from compass_agent.schemas import ToolDefinition
    from compass_agent.tools.builtin.database_query.handler import registration as database
    from compass_agent.tools.registry import ToolRegistry

    store, run_id, epoch, _ = runtime27
    with store.transaction() as tx:
        _, run = store.lock(tx, run_id)
        run.config_snapshot = {
            **run.config_snapshot,
            "contextRuntime": {**run.config_snapshot["contextRuntime"], "answerProtocol": 2},
        }
    journal = ExecutionJournal(store, LocalPublisher(), run_id, epoch)
    baseline = json.loads(
        Path(__file__).with_name("tool_eval").joinpath("baseline_tools.json").read_text()
    )
    declarations = baseline.values()
    old = next(item for item in declarations if item["function"]["name"] == "database_query")
    entry = database(())
    journal.freeze_catalog(
        ToolRegistry([replace(entry, definition=ToolDefinition.model_validate(old))])
    )

    async def restored():
        control = RunRegistry().register(OwnerKey.from_context(CHAT), run_id=run_id)
        token = current_execution.set(journal)
        try:
            state = TaskState([])
            bindings = RunToolBindings(
                RuntimeTurn(cast(Any, SimpleNamespace(root_observation=None))),
                state,
                None,
                CHAT,
                control,
                None,
                lambda _: None,
                ToolLoader(),
                None,
                entries=(entry,),
                catalog=CatalogContext(frozenset()),
            )
            definitions = bindings.definitions()
            assert definitions[0].model_dump(mode="json") == old
            assert bindings.runtime is not None
            restored_entry = bindings.runtime.registry.entries["database_query"]
            assert restored_entry.normalize_input is None
            prepared = await bindings.runtime.dispatcher.prepare(
                call("database_query", "old", args='{"op":"list_connections"}'),
                replace(tool_context("database_query"), run_id=run_id),
            )
            assert prepared.args == {"op": "list_connections"}
        finally:
            current_execution.reset(token)

    asyncio.run(restored())


def test_schedule_revision_and_server_values_frozen_across_approval_replay(runtime27):
    from compass_agent.models.errors import ModelError
    from compass_agent.runtime.store import now
    from compass_agent.scheduler.entities import AgentSchedule
    from compass_agent.scheduler.service import ScheduleService
    from compass_agent.tools.builtin.schedule_write import ARGUMENTS, command_for
    from compass_agent.tools.builtin.schedule_write.slim import normalizer

    from .test_tool_dispatcher import context

    store, run_id, epoch, _ = runtime27
    journal = ExecutionJournal(store, LocalPublisher(), run_id, epoch)
    identifier = uuid4()
    assert CHAT.scope.kind == "tenant"
    with store.transaction() as tx:
        tx.add(
            AgentSchedule(
                id=identifier,
                tenant_id=CHAT.scope.tenant_id,
                workspace_id=CHAT.scope.workspace_id,
                owner_user_id=CHAT.actor_user_id,
                name="周报",
                cron="0 9 * * 1",
                timezone="Asia/Tokyo",
                input_ciphertext=b"unused",
                status="disabled",
                revision=1,
                next_fire_at=None,
                created_at=now(),
                updated_at=now(),
            )
        )
    normalize = normalizer(ScheduleService)

    async def scenario():
        token = current_execution.set(journal)
        try:
            execution = replace(context("schedule_write"), run_id=run_id, call_id="update")
            raw = {"scheduleId": str(identifier), "name": "新周报"}
            original = normalize(raw, execution)
            assert original["expectedRevision"] == 1
            with store.transaction() as tx:
                plan = tx.get(AgentSchedule, identifier)
                plan.revision = 2
            assert normalize(raw, execution) == original
            with store.transaction() as tx:
                plan = tx.get(AgentSchedule, identifier)
                _, command = command_for(ARGUMENTS.validate_python(original), uuid4())
                with pytest.raises(ModelError, match="schedule_revision_changed"):
                    ScheduleService.check_revision(plan, command)
            create = normalize(
                {"name": "每日汇总", "question": "汇总销售", "schedule": "daily 09:00"},
                replace(execution, call_id="create"),
            )
            assert create["timezone"] == "Asia/Tokyo" and create["cron"] == "0 9 * * *"
            selection = create["selection"]
            assert isinstance(selection, dict)
            assert selection["provider_id"] == str(PROVIDER_ID)
        finally:
            current_execution.reset(token)

    asyncio.run(scenario())


def test_slim_schedule_confirmation_survives_new_journal_and_applies_once(runtime27):
    from unittest.mock import AsyncMock

    from compass_agent.runtime.hitl import HumanDecisions
    from compass_agent.runtime.schedule_effect import execute_effect
    from compass_agent.scheduler.entities import AgentSchedule
    from compass_agent.scheduler.service import ScheduleService, plan_input
    from compass_agent.schemas import DecisionCommand, RunWaiting, ToolApprovalProposal
    from compass_agent.tools.builtin.schedule_write import SPEC
    from sqlalchemy import func, select

    from .test_tool_dispatcher import dispatcher

    store, run_id, epoch, _ = runtime27
    decisions = HumanDecisions(store)

    class ScheduleStore:
        # Chat effects use the same DB clock without starting a queue worker in this fixture.
        scheduler = SimpleNamespace(clock=lambda tx: tx.scalar(select(func.clock_timestamp())))

        def __getattr__(self, name):
            return getattr(store, name)

    service = ScheduleService(ScheduleStore(), SimpleNamespace(check=AsyncMock()), None, Settings())
    service.validate_model = AsyncMock()
    raw = {"name": "每周经营摘要", "question": "汇总销售情况", "schedule": "weekly MON 09:00"}
    invocation = call("schedule_write", "weekly", json.dumps(raw))

    async def scenario():
        nonlocal epoch
        journal = ExecutionJournal(store, LocalPublisher(), run_id, epoch)
        token = current_execution.set(journal)
        try:
            await journal.local_read(
                "initial",
                {},
                lambda: asyncio.sleep(0, result={"ready": True}),
                yield_boundary=False,
            )
            entry = SPEC.register(
                CatalogContext(frozenset(), schedules=service, schedule_effect=execute_effect),
                lambda _: None,
            )
            runtime = dispatcher(entry)
            execution = replace(tool_context("schedule_write"), run_id=run_id)
            prepared = await runtime.prepare(invocation, execution)
            waiting = await runtime.execute(prepared, execution)
            assert waiting.status == "waiting" and isinstance(
                waiting.proposal, ToolApprovalProposal
            )
            assert "Asia/Tokyo" in waiting.proposal.summary
            wait_id = uuid4()
            decisions.wait(
                run_id,
                epoch,
                RunWaiting(
                    status="waiting",
                    runId=str(run_id),
                    waitId=str(wait_id),
                    nodeKey="tool_approval",
                    continuation="execute_tool",
                    proposal=waiting.proposal,
                ),
                journal.pending_proposal,
            )
        finally:
            current_execution.reset(token)
        view = decisions.get(run_id, wait_id, CHAT, may_approve=True)
        decisions.decide(
            run_id,
            DecisionCommand.model_validate_json(
                json.dumps(
                    {
                        "requestId": str(uuid4()),
                        "expectedVersion": view.run_version,
                        "waitId": str(wait_id),
                        "proposalId": view.proposal_id,
                        "proposalRevision": view.proposal_revision,
                        "proposalDigest": view.proposal_digest,
                        "decisionId": str(uuid4()),
                        "decision": "approve",
                    }
                )
            ),
            CHAT,
            may_approve=True,
            boot_id=uuid4(),
        )
        epoch = store.read_run(run_id).execution_epoch
        resumed = ExecutionJournal(store, LocalPublisher(), run_id, epoch)
        resumed.approved_proposals = [
            {
                **store.private.open(run_id, "proposal", w.private_proposal),
                "actionId": str(uuid5(w.id, f"action:{w.proposal_revision}")),
            }
            for w in decisions.approvals(run_id)
        ]
        token = current_execution.set(resumed)
        try:
            restored = await runtime.prepare(invocation, execution)
            assert restored.args == prepared.args
            first = await runtime.execute(restored, execution)
            second = await runtime.execute(restored, execution)
            assert first.status == second.status == "ok", (first, second)
            assert first.payload == second.payload
            with store.transaction() as tx:
                plans = tx.scalars(select(AgentSchedule)).all()
                assert len(plans) == 1 and plans[0].revision == 1
                assert plans[0].cron == "0 9 * * 1" and plans[0].timezone == "Asia/Tokyo"
                assert plan_input(store, plans[0])["selection"]["provider_id"] == str(PROVIDER_ID)
        finally:
            current_execution.reset(token)

    asyncio.run(scenario())
