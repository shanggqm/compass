"""Goal business boundaries on real PostgreSQL; model and Redis transport are controlled.

The local publisher feeds committed outbox packets into the production projection.
Browser/SSE, real model decisions and child processes are separate E2E coverage.
"""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
import pytest
from compass_agent.context_runtime.answer_verification import before_delivery, check_access
from compass_agent.context_runtime.answers import ANSWER
from compass_agent.context_runtime.evidence import after_tool_batch, user_input
from compass_agent.context_runtime.source_access import allows, read_private, view_key
from compass_agent.data_connections.entities import DataConnection
from compass_agent.data_connections.legacy import LegacyScopeCheck
from compass_agent.execution_context import current_execution, execution_path
from compass_agent.goal.anchor import anchor
from compass_agent.goal.contracts import (
    ChildReturn,
    DelegationTask,
    InputRef,
    LedgerItem,
    ProposeGoal,
)
from compass_agent.goal.controller import GoalController, accepted_usage
from compass_agent.goal.gate import complete
from compass_agent.goal.handoff import admit_delegation, delivery, merge_delivery
from compass_agent.goal.ledger import Ledger
from compass_agent.goal.resume import resume_check
from compass_agent.harness.autonomy import AutonomyBudget, AutonomyLimits, AutonomyStopped
from compass_agent.models.errors import ModelError
from compass_agent.runtime.context_results import ContextResults
from compass_agent.runtime.entities import AgentGoal, AgentGoalEntry, AgentRun, AgentStep
from compass_agent.runtime.journal import ExecutionJournal
from compass_agent.runtime.private_state import digest
from compass_agent.runtime.run_scheduler import ConsumerSlot, RunScheduler
from compass_agent.runtime.store import RuntimeStore
from compass_agent.schemas import GoalAction, RunToolError, SessionSubmit
from compass_agent.settings import Settings
from compass_agent.tools.artifacts import ArtifactStore
from compass_agent.tools.builtin.database_query import DatabaseResult, query_database_live
from compass_agent.tools.builtin.database_query.handler import registration as database_registration
from compass_agent.tools.builtin.read_tool_result import ReadToolResultInput
from compass_agent.tools.contracts import ToolRejected
from compass_agent.tools.dispatcher import ExecutionSlots, ToolDispatcher
from compass_agent.tools.registry import ToolRegistry
from cryptography.fernet import Fernet
from psycopg import sql
from sqlalchemy import create_engine, select

from .context_fixture import install_prompts
from .test_agent_run import CHAT, PROVIDER_ID
from .test_context_runtime_integration import LocalPublisher
from .test_database_tools_integration import db
from .test_database_tools_integration import targets as targets
from .test_generic_database_integration import data_source as data_source
from .test_tool_dispatcher import call, context


def publish(store, run_id):
    """A deterministic transport, retaining actual outbox and projection validation."""
    for box in store.pending(run_id):
        counter = int(store.read_run(run_id).projected_seq.split("-")[0])
        packets = box.payload["packets"]
        entries = [
            (
                f"{counter + index + 1}-0",
                {
                    "packet": packet,
                    "outboxId": str(box.id),
                    "batchIndex": index,
                    "batchSize": len(packets),
                },
            )
            for index, packet in enumerate(packets)
        ]
        store.project(run_id, entries)


def command(session_id=None):
    return SessionSubmit.model_validate(
        {
            "requestId": str(uuid4()),
            "sessionId": str(session_id) if session_id else None,
            "question": "分阶段核对月度经营数据，仅使用授权范围。",
            "selection": {"mode": "fixed", "provider_id": str(PROVIDER_ID)},
        }
    )


def create_store(engine, settings=None):
    settings = settings or Settings(compass_context_runtime_enabled=False)
    store = RuntimeStore(engine, Fernet.generate_key().decode(), settings=settings)
    store.scheduler = RunScheduler(store, settings)
    store.scheduler.owner_boot_id = uuid4()
    store.ready = True
    return store


def claim(store, run_id):
    publish(store, run_id)
    grant = store.scheduler.claim(ConsumerSlot("goal-" + uuid4().hex, uuid4()))
    assert grant and grant.run_id == run_id
    return ExecutionJournal(store, LocalPublisher(), run_id, grant.epoch)


def approved_goal(store, owner=CHAT):
    receipt = store.submit(command(), owner)
    run_id = UUID(receipt["runId"])
    first = claim(store, run_id)
    controller = GoalController(store)
    proposal = ProposeGoal.model_validate(
        {
            "op": "propose",
            "contract": {
                "objective": "核对销售与库存并交付月度结论",
                "acceptance": [
                    {"id": "a1", "text": "销售结论可核对"},
                    {"id": "a2", "text": "库存结论可核对"},
                ],
                "constraints": ["只看华东", "金额以分为单位"],
            },
            "budget": {"maxRounds": 12, "tokens": 100000, "costMicrousd": 200000},
        }
    )
    with store.transaction() as tx:
        session, run = store.lock(tx, run_id, owner)
        store.require_epoch(run, first.epoch)
        goal = controller.propose(tx, run, proposal)
        goal_id, session_id = goal.id, session.id
    first.goal_id = str(goal_id)
    return SimpleNamespace(
        store=store,
        controller=controller,
        journal=first,
        goal_id=goal_id,
        session_id=session_id,
    )


@pytest.fixture
def goal_runtime(targets):
    runtime, _ = targets("goal_runtime")
    db.migrate()
    engine = create_engine(runtime)
    try:
        yield approved_goal(create_store(engine))
    finally:
        engine.dispose()


def finish(runtime, journal=None, *, status="completed", project=True):
    journal = journal or runtime.journal
    runtime.store.finish(journal.run_id, journal.epoch, status, answer="本轮已完成。", reason=None)
    if project:
        publish(runtime.store, journal.run_id)


@pytest.mark.parametrize("pending_status", ["running", "completed"])
def test_cancel_settles_latest_undelivered_execution_revision(targets, pending_status):
    url, _ = targets("goal_cancel_outbox")
    db.migrate()
    engine = install_prompts(create_engine(url))
    try:
        runtime = approved_goal(
            create_store(engine, Settings(compass_context_runtime_enabled=True))
        )
        store, journal = runtime.store, runtime.journal
        node_id = str(uuid4())

        def event(revision, status):
            return {
                "type": "view.upsert",
                "payload": {
                    "objectId": node_id,
                    "revision": revision,
                    "kind": "execution.node",
                    "value": {
                        "id": node_id,
                        "revision": revision,
                        "kind": "tool",
                        "title": "私有执行节点标题",
                        "status": status,
                        "startedAtMs": 1000,
                        "endedAtMs": 2000 if status == "completed" else None,
                        "detail": {"input": "authorized-input", "output": f"revision-{revision}"},
                    },
                },
            }

        store.progress(journal.run_id, journal.epoch, [event(1, "running")])
        publish(store, journal.run_id)
        store.progress(journal.run_id, journal.epoch, [event(2, pending_status)])
        assert store.read_run(journal.run_id).public_view["objects"][node_id]["revision"] == 1
        with store.transaction() as tx:
            accepted = read_private(
                store, tx, journal.run_id, view_key(event(2, pending_status)["payload"])
            )
        assert accepted and accepted["payload"]["value"]["detail"]["output"] == "revision-2"

        # Cancel while revision 2 is committed but Redis has only projected revision 1.
        finish(runtime, status="canceled")
        final = store.read_run(journal.run_id)
        node = final.public_view["objects"][node_id]
        assert final.status == "canceled"
        assert node["revision"] == (3 if pending_status == "running" else 2)
        assert node["value"]["status"] == (
            "canceled" if pending_status == "running" else "completed"
        )
        with store.transaction() as tx:
            retained = read_private(
                store, tx, journal.run_id, view_key(event(2, pending_status)["payload"])
            )
            settled = read_private(store, tx, journal.run_id, view_key(node))
        assert retained == accepted
        assert settled and settled["payload"]["value"]["title"] == "私有执行节点标题"
        assert settled["payload"]["value"]["detail"]["output"] == "revision-2"
        assert store.pending(journal.run_id) == []
    finally:
        engine.dispose()


def view(runtime):
    return runtime.controller.read(runtime.session_id, CHAT)["goal"]


def action(runtime, name):
    return runtime.controller.act(
        runtime.session_id,
        GoalAction.model_validate(
            {
                "action": name,
                "requestId": str(uuid4()),
                "expectedRevision": view(runtime)["revision"],
            }
        ),
        CHAT,
    )


def latest(runtime):
    with runtime.store.transaction() as tx:
        return tx.scalar(
            select(AgentRun)
            .where(AgentRun.session_id == runtime.session_id)
            .order_by(AgentRun.ordinal.desc())
        )


def write_entry(runtime, key, kind="todo", status="open", **body):
    with runtime.store.transaction() as tx:
        _, run = runtime.store.lock(tx, runtime.journal.run_id, CHAT)
        runtime.store.require_epoch(run, runtime.journal.epoch)
        goal = tx.get(AgentGoal, runtime.goal_id, with_for_update=True)
        return Ledger(runtime.store).upsert(
            tx,
            goal,
            key,
            kind,
            status,
            {"title": key, "sourceRefs": [], "dependsOn": [], **body},
        )


def test_concurrent_scans_admit_one_round_only_after_terminal_publication(goal_runtime):
    runtime = goal_runtime
    finish(runtime, project=False)
    runtime.controller.tick()
    assert latest(runtime).id == runtime.journal.run_id
    publish(runtime.store, runtime.journal.run_id)
    barrier = Barrier(2)

    def scan():
        barrier.wait(timeout=5)
        GoalController(runtime.store).tick()

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(scan) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    with runtime.store.transaction() as tx:
        runs = tx.scalars(select(AgentRun).order_by(AgentRun.ordinal)).all()
        assert len(runs) == 2
        assert runs[1].status == "created"
        assert runs[1].config_snapshot["goal"]["round"] == 1
    assert view(runtime)["usage"]["rounds"] == 1


def test_queued_interruption_restores_counter_and_admits_fresh_run_after_user_turn(goal_runtime):
    runtime = goal_runtime
    finish(runtime)
    runtime.controller.tick()
    queued = latest(runtime)
    assert queued.status == "created"
    result = action(runtime, "interrupt")
    assert result["runId"] == str(queued.id)
    publish(runtime.store, queued.id)
    runtime.controller.tick()
    assert view(runtime)["status"] == "paused"
    assert view(runtime)["usage"]["rounds"] == 0
    interrupted = runtime.store.submit(command(runtime.session_id), CHAT)
    user = claim(runtime.store, UUID(interrupted["runId"]))
    assert runtime.store.read_run(user.run_id).config_snapshot["goal"]["round"] is None
    finish(runtime, user)
    runtime.controller.tick()
    replacement = latest(runtime)
    assert view(runtime)["status"] == "active"
    assert replacement.status == "created"
    assert replacement.id != queued.id and replacement.request_id != queued.request_id
    assert replacement.config_snapshot["goal"]["round"] == 1
    runtime.controller.tick()
    assert latest(runtime).id == replacement.id


def test_user_pause_stays_paused_and_stale_action_is_rejected(goal_runtime):
    runtime = goal_runtime
    finish(runtime)
    runtime.controller.tick()
    queued = latest(runtime)
    before = view(runtime)["revision"]
    action(runtime, "pause")
    publish(runtime.store, queued.id)
    runtime.controller.tick()
    assert view(runtime)["status"] == "paused"
    assert view(runtime)["reasonCode"] == "user_paused"
    assert view(runtime)["usage"]["rounds"] == 0
    with pytest.raises(ModelError, match="goal_stale_revision"):
        runtime.controller.act(
            runtime.session_id,
            GoalAction.model_validate(
                {"action": "resume", "requestId": str(uuid4()), "expectedRevision": before}
            ),
            CHAT,
        )
    action(runtime, "resume")
    runtime.controller.tick()
    assert latest(runtime).status == "created"
    assert latest(runtime).id != queued.id


@pytest.mark.parametrize(
    "status,reason", [("completed", "no_progress"), ("failed", "runtime_failures")]
)
def test_three_unproductive_rounds_stop_without_an_extra_run(goal_runtime, status, reason):
    runtime = goal_runtime
    journal = runtime.journal
    for index in range(3):
        finish(runtime, journal, status=status)
        runtime.controller.tick()
        if index < 2:
            journal = claim(runtime.store, latest(runtime).id)
    assert view(runtime)["status"] == "blocked"
    assert view(runtime)["reasonCode"] == reason
    assert latest(runtime).id == journal.run_id


@pytest.mark.parametrize("user_paused", [False, True])
def test_blocked_run_stops_goal_without_new_round_or_overwriting_user_pause(
    goal_runtime, user_paused
):
    runtime = goal_runtime
    if user_paused:
        action(runtime, "pause")
    prior = view(runtime)
    run_id = runtime.journal.run_id
    runtime.store.scheduler.revoke(run_id, "workflow_replay_mismatch", force=True)
    runtime.store.scheduler.settle_reclaimed(run_id)
    publish(runtime.store, run_id)
    for _ in range(3):
        runtime.controller.tick()
    result = view(runtime)
    assert result["status"] == ("paused" if user_paused else "blocked")
    assert result["reasonCode"] == ("user_paused" if user_paused else "workflow_replay_mismatch")
    assert result["revision"] == prior["revision"] + (0 if user_paused else 1)
    assert result["usage"]["rounds"] == prior["usage"]["rounds"]
    assert latest(runtime).id == run_id
    if not user_paused:
        assert "自动执行已停止" in result["reason"]
        assert action(runtime, "cancel")["goal"]["status"] == "canceled"


def test_accepted_parent_child_usage_is_settled_once_and_replay_does_not_charge(goal_runtime):
    runtime = goal_runtime
    journal = runtime.journal
    parent = AutonomyBudget((), AutonomyLimits(steps=20, tools=30))

    async def model(key, budget, tokens, cost):
        async def invoke():
            budget.steps += 1
            budget.tokens += tokens
            budget.cost_microusd += cost
            budget.attempts += 1
            return {"status": "completed"}

        return await journal.call(
            "model", key, {}, invoke, dict, budget=budget, yield_boundary=False
        )

    async def child(index, tokens, cost):
        with parent.child_budget(steps=3) as budget:

            async def invoke():
                return await model(f"child:{index}/model:1", budget, tokens, cost)

            return await journal.call(
                "subagent", f"child:{index}", {}, invoke, dict, yield_boundary=False
            )

    async def scenario():
        await model("root/model:1", parent, 100, 50)
        await asyncio.gather(child(1, 200, 70), child(2, 300, 80))
        replay_budget = AutonomyBudget((), AutonomyLimits())
        await model("root/model:1", replay_budget, 9999, 9999)

    asyncio.run(scenario())
    assert parent.tokens == 600 and parent.cost_microusd == 200
    with runtime.store.transaction() as tx:
        assert accepted_usage(tx, journal.run_id) == {
            "tokens": 600,
            "cost_microusd": 200,
            "attempts": 3,
        }
        child_steps = tx.scalars(select(AgentStep).where(AgentStep.kind == "subagent")).all()
        assert len(child_steps) == 2
        assert all(
            tx.scalar(select(AgentStep.id).where(AgentStep.parent_step_id == s.id))
            for s in child_steps
        )
    finish(runtime)
    runtime.controller.tick()
    runtime.controller.tick()
    assert view(runtime)["usage"]["tokens"] == 600
    assert view(runtime)["usage"]["costMicrousd"] == 200


def test_completion_remedies_conflicts_and_explicit_resolution(goal_runtime):
    runtime = goal_runtime
    journal = runtime.journal
    evidence_id = user_input(journal, "销售额 120 分；库存 8 件；复核销售额为 130 分。")

    def record(items):
        with runtime.store.transaction() as tx:
            _, run = runtime.store.lock(tx, journal.run_id, CHAT)
            runtime.store.require_epoch(run, journal.epoch)
            goal = tx.get(AgentGoal, runtime.goal_id, with_for_update=True)
            Ledger(runtime.store).record(
                tx, goal, [LedgerItem.model_validate(i) for i in items], journal, CHAT
            )

    def check():
        with runtime.store.transaction() as tx:
            goal = tx.get(AgentGoal, runtime.goal_id, with_for_update=True)
            return complete(runtime.store, tx, goal, goal.revision, CHAT, epoch=journal.epoch)

    record(
        [
            {
                "type": "conclusion",
                "key": "sales:one",
                "title": "销售 120",
                "evidenceIds": [evidence_id],
                "acceptanceIds": ["a1"],
                "factKey": "sales",
                "value": "120",
                "unit": "分",
            },
            {"type": "todo", "key": "stock:todo", "title": "核对库存"},
        ]
    )
    assert check()["accepted"] is False
    assert view(runtime)["status"] == "active"
    with runtime.store.transaction() as tx:
        remedies = tx.scalars(
            select(AgentGoalEntry.entry_key).where(AgentGoalEntry.entry_key.like("remedy:%"))
        ).all()
        assert len(remedies) == 1
        revision = tx.get(AgentGoal, runtime.goal_id).ledger_revision
    check()
    with runtime.store.transaction() as tx:
        assert tx.get(AgentGoal, runtime.goal_id).ledger_revision == revision
    record(
        [
            {
                "type": "conclusion",
                "key": "sales:two",
                "title": "销售 130",
                "evidenceIds": [evidence_id],
                "acceptanceIds": ["a1"],
                "factKey": "sales",
                "value": "130",
                "unit": "分",
            },
        ]
    )
    assert view(runtime)["counts"]["conflicts"] == 2
    record(
        [
            {
                "type": "conclusion",
                "key": "sales:resolved",
                "title": "确认销售 130",
                "evidenceIds": [evidence_id],
                "acceptanceIds": ["a1"],
                "factKey": "sales",
                "value": "130",
                "unit": "分",
                "resolves": ["sales:one", "sales:two"],
            },
            {
                "type": "conclusion",
                "key": "stock:done",
                "title": "库存已核对",
                "evidenceIds": [evidence_id],
                "acceptanceIds": ["a2"],
            },
            {"type": "todo_done", "key": "stock:todo", "title": "库存已核对"},
            {"type": "todo_done", "key": remedies[0], "title": "补救已完成"},
        ]
    )
    assert view(runtime)["counts"]["conflicts"] == 0
    assert check()["accepted"] is True
    assert view(runtime)["status"] == "completed"
    assert anchor(journal, CHAT, None) is not None
    final_budget = AutonomyBudget((), AutonomyLimits())

    async def final_model():
        final_budget.tokens += 70
        final_budget.cost_microusd += 29
        final_budget.attempts += 1
        return {"status": "completed"}

    asyncio.run(
        journal.call(
            "model",
            "root/completed-delivery",
            {},
            final_model,
            dict,
            budget=final_budget,
            yield_boundary=False,
        )
    )
    finish(runtime)
    runtime.controller.tick()
    runtime.controller.tick()
    with runtime.store.transaction() as tx:
        goal = tx.get(AgentGoal, runtime.goal_id)
        assert (goal.tokens_used, goal.cost_used_microusd) == (70, 29)
        assert goal.last_settled_ordinal == latest(runtime).ordinal
    assert latest(runtime).id == journal.run_id


@pytest.mark.parametrize("reason", ["token_budget", "cost_budget", "max_rounds"])
def test_budget_stop_settles_accepted_usage_and_never_admits_extra_round(goal_runtime, reason):
    runtime = goal_runtime
    journal = runtime.journal
    budget = AutonomyBudget((), AutonomyLimits())
    if reason == "max_rounds":
        with runtime.store.transaction() as tx:
            goal = tx.get(AgentGoal, runtime.goal_id)
            goal.budget = {**goal.budget, "maxRounds": 1}
        finish(runtime)
        runtime.controller.tick()
        journal = claim(runtime.store, latest(runtime).id)
    else:

        async def model():
            budget.tokens += 100005 if reason == "token_budget" else 3
            budget.cost_microusd += 200005 if reason == "cost_budget" else 2
            budget.attempts += 1
            return {"status": "completed"}

        asyncio.run(
            journal.call(
                "model", "root/budget-limit", {}, model, dict, budget=budget, yield_boundary=False
            )
        )
        with pytest.raises(AutonomyStopped, match=reason):
            anchor(journal, CHAT, budget)
    finish(runtime, journal)
    runtime.controller.tick()
    runtime.controller.tick()
    observed = view(runtime)
    assert observed["status"] == "budget_limited" and observed["reasonCode"] == reason
    assert observed["usage"]["tokens"] == budget.tokens
    assert observed["usage"]["costMicrousd"] == budget.cost_microusd
    assert observed["usage"]["overrun"] is (reason != "max_rounds")
    assert latest(runtime).id == journal.run_id


def test_anchor_preserves_contract_work_failures_and_budget_boundaries(goal_runtime):
    runtime = goal_runtime
    for index in range(32):
        write_entry(runtime, f"todo:{index}", title=f"阶段 {index}")
    for index in range(12):
        write_entry(runtime, f"failure:{index}", "failure", "done", title=f"失败路径 {index}")
    budget = SimpleNamespace(tokens=92000, cost_microusd=0)
    text = anchor(runtime.journal, CHAT, budget)
    assert isinstance(text, str)
    assert "只看华东" in text and "金额以分为单位" in text
    assert all(f"阶段 {i}" in text for i in range(32))
    assert all(f"失败路径 {i}" in text for i in range(12))
    assert "本轮收尾" in text
    before = view(runtime)
    budget.tokens = 100000
    with pytest.raises(AutonomyStopped, match="token_budget"):
        anchor(runtime.journal, CHAT, budget)
    assert view(runtime) == before


def source_runtime(data_source, **overrides):
    engine, connections, _ = data_source
    settings = Settings(
        compass_context_runtime_enabled=False,
        compass_model_encryption_key=connections.key,
        compass_data_allowed_endpoints=",".join(connections.endpoints),
        **overrides,
    )
    return approved_goal(create_store(engine, settings))


def metadata_ref(binding, table="daily_fact"):
    return {
        "kind": "metadata",
        "tenant": str(binding.tenant_id),
        "workspace": str(binding.workspace_id),
        "connection": str(binding.connection_id),
        "revision": binding.revision,
        "role": binding.role,
        "columns": {"analytics_978." + table: ["amount_cent"]},
    }


def admin_connection(engine):
    admin = urlsplit(os.environ["AGENT_TEST_ADMIN_URL"])
    return psycopg.connect(
        db.dsn(urlunsplit(admin._replace(path="/" + engine.url.database))), autocommit=True
    )


def test_resume_checks_current_revision_revocation_and_derived_content(data_source):
    runtime = source_runtime(data_source)
    engine, _, binding = data_source
    sales = metadata_ref(binding)
    with admin_connection(engine) as connection:
        connection.execute("CREATE TABLE analytics_978.inventory_fact(amount_cent bigint)")
        connection.execute(
            sql.SQL("GRANT SELECT ON analytics_978.inventory_fact TO {}").format(
                sql.Identifier(binding.role)
            )
        )
    stock = metadata_ref(binding, "inventory_fact")
    assert allows(runtime.store, CHAT, [sales, stock])
    write_entry(
        runtime,
        "query:sales",
        "query",
        "done",
        sourceRefs=[sales],
        dataVersion={str(binding.connection_id): 1},
    )
    write_entry(
        runtime,
        "sales:summary",
        "conclusion",
        "done",
        dependsOn=["query:sales"],
        sourceRefs=[sales],
    )
    write_entry(
        runtime, "stock:secret", "conclusion", "done", title="库存秘密 9876", sourceRefs=[stock]
    )
    write_entry(
        runtime,
        "stock:derived",
        "conclusion",
        "done",
        title="库存派生 9876",
        dependsOn=["stock:secret"],
    )
    write_entry(
        runtime,
        "effect:report",
        "effect",
        "done",
        sourceRefs=[sales],
        result={"artifactId": "retained-report"},
    )
    with runtime.store.transaction() as tx:
        tx.get(DataConnection, binding.connection_id).revision += 1
        tx.get(AgentGoal, runtime.goal_id).revision += 1
    with admin_connection(engine) as connection:
        connection.execute(
            sql.SQL("REVOKE SELECT ON analytics_978.inventory_fact FROM {}").format(
                sql.Identifier(binding.role)
            )
        )
    with runtime.store.transaction() as tx:
        goal = tx.get(AgentGoal, runtime.goal_id, with_for_update=True)
        resume_check(runtime.store, tx, goal, CHAT, runtime.journal.epoch)
        first_revision = goal.ledger_revision
        resume_check(runtime.store, tx, goal, CHAT, runtime.journal.epoch)
        assert goal.ledger_revision == first_revision
        rows = {
            row.entry_key: (row.status, body) for row, body in Ledger(runtime.store).rows(tx, goal)
        }
        assert rows["query:sales"][0] == "stale"
        assert rows["sales:summary"][0] == "stale"
        assert rows["stock:secret"][0] == "dropped"
        assert rows["stock:derived"][0] == "dropped"
        assert rows["effect:report"][0] == "done"
        assert rows["effect:report"][1]["result"] == {"artifactId": "retained-report"}
        assert rows["refresh:query:sales"][0] == "open"
    text = anchor(runtime.journal, CHAT, None)
    assert isinstance(text, str)
    assert "9876" not in text and "库存秘密" not in text and "库存派生" not in text
    assert view(runtime)["counts"]["stale"] == 2


def test_delegation_checks_whole_batch_budget_tools_connections_and_inputs(goal_runtime):
    runtime = goal_runtime
    write_entry(runtime, "todo:inventory", title="仅库存待办")
    user_input(runtime.journal, "父历史中的敏感旁支，不应自动加入子任务。")

    async def make_context():
        return replace(
            context("database_query"), run_id=runtime.journal.run_id, call_id="delegation"
        )

    ctx = asyncio.run(make_context())
    budget = AutonomyBudget((), AutonomyLimits(steps=20, tools=30))
    allowed = DelegationTask(
        name="库存",
        objective="核对库存",
        inputs=[InputRef(kind="ledger", id="todo:inventory")],
        tools=["database_query"],
    )
    denied = DelegationTask(name="越权", objective="修改资料", tools=["goal"])
    with pytest.raises(ToolRejected, match="delegation_out_of_scope"):
        admit_delegation(runtime.journal, [allowed, denied], ctx, {"database_query"}, budget)
    assert budget.children == set()
    tiny = AutonomyBudget((), AutonomyLimits(steps=5, tools=5))
    with pytest.raises(ToolRejected, match="delegation_budget_exceeded"):
        admit_delegation(runtime.journal, [allowed, allowed], ctx, {"database_query"}, tiny)
    foreign = allowed.model_copy(update={"connection_ids": [str(uuid4())]})
    with pytest.raises(ToolRejected, match="delegation_out_of_scope"):
        admit_delegation(runtime.journal, [foreign], ctx, {"database_query"}, budget)
    invisible = DelegationTask(
        name="未知输入", objective="核对", inputs=[InputRef(kind="evidence", id="foreign")]
    )
    with pytest.raises(ToolRejected, match="delegation_input_forbidden"):
        admit_delegation(runtime.journal, [invisible], ctx, {"database_query"}, budget)
    snapshots = admit_delegation(runtime.journal, [allowed], ctx, {"database_query"}, budget)
    assert "仅库存待办" in json.dumps(snapshots, ensure_ascii=False)
    assert "敏感旁支" not in json.dumps(snapshots, ensure_ascii=False)
    assert snapshots[0]["tools"] == ["database_query"]


def test_delegation_connection_cannot_widen_parent_scope(data_source):
    runtime = source_runtime(data_source)
    _, _, binding = data_source

    async def make_context():
        return replace(
            context("database_query"),
            run_id=runtime.journal.run_id,
            call_id="restricted-connections",
            connection_ids=frozenset(),
        )

    ctx = asyncio.run(make_context())
    task = DelegationTask.model_validate(
        {
            "name": "越过父连接边界",
            "objective": "核对销售",
            "connectionIds": [str(binding.connection_id)],
        }
    )
    with pytest.raises(ToolRejected, match="delegation_out_of_scope"):
        admit_delegation(runtime.journal, [task], ctx, {"database_query"}, None)


def test_replayed_delegation_rechecks_real_input_access(data_source):
    runtime = source_runtime(data_source)
    _, _, binding = data_source
    write_entry(
        runtime,
        "sales:available",
        "conclusion",
        "done",
        title="销售结论 350 分",
        sourceRefs=[metadata_ref(binding)],
    )

    async def make_context():
        return replace(context("database_query"), run_id=runtime.journal.run_id, call_id="replay")

    ctx = asyncio.run(make_context())
    task = DelegationTask(
        name="销售复核",
        objective="复核已有结论",
        inputs=[InputRef(kind="ledger", id="sales:available")],
    )
    initial = admit_delegation(runtime.journal, [task], ctx, {"database_query"}, None)
    repeated = admit_delegation(runtime.journal, [task], ctx, {"database_query"}, None)
    assert initial == repeated
    assert "350" in json.dumps(initial, ensure_ascii=False)
    with admin_connection(runtime.store.engine) as connection:
        connection.execute(
            sql.SQL("REVOKE SELECT ON analytics_978.daily_fact FROM {}").format(
                sql.Identifier(binding.role)
            )
        )
    with pytest.raises(ToolRejected, match="delegation_input_forbidden"):
        admit_delegation(runtime.journal, [task], ctx, {"database_query"}, None)


def test_child_delivery_filters_foreign_evidence_and_merges_only_once(goal_runtime):
    runtime = goal_runtime
    identifier = user_input(runtime.journal, "本子任务已获得的输入结论。")
    records = runtime.journal.answer_evidence[execution_path.get()]
    returned = ChildReturn.model_validate(
        {
            "status": "completed",
            "summary": "库存分支完成",
            "findings": [
                {"statement": "合法结论", "evidenceIds": [identifier, "foreign"]},
                {"statement": "全部外来", "evidenceIds": ["other-child"]},
            ],
        }
    )
    child_id = uuid4()
    result = delivery(child_id, returned, "", "returned", records)
    assert result["status"] == "partial"
    assert result["rejectedEvidenceIds"] == ["foreign", "other-child"]
    assert len(result["result"]["findings"]) == 1
    assert result["result"]["findings"][0]["evidenceIds"] == [identifier]
    merge_delivery(runtime.journal, result, CHAT)
    with runtime.store.transaction() as tx:
        revision = tx.get(AgentGoal, runtime.goal_id).ledger_revision
    merge_delivery(runtime.journal, result, CHAT)
    with runtime.store.transaction() as tx:
        assert tx.get(AgentGoal, runtime.goal_id).ledger_revision == revision
        assert (
            len(tx.scalars(select(AgentGoalEntry).where(AgentGoalEntry.kind == "delivery")).all())
            == 1
        )
    natural = delivery(uuid4(), None, "完整保留的自然结束文本", "natural_end", records)
    assert natural["status"] == "partial" and natural["answer"] == "完整保留的自然结束文本"
    assert natural["result"]["gaps"] == ["子任务未按契约返回结构化结果"]


def test_cross_run_query_reuse_imports_citable_evidence_and_preserves_authorization(data_source):
    runtime = source_runtime(data_source)
    _, _, binding = data_source
    actual_queries = []

    async def counted_query(query, connection, control):
        actual_queries.append(query.function.arguments)
        # Scheduler checkpoint/yield behavior is covered separately; execute actual SQL here.
        return await query_database_live(query, connection, control)

    args = {
        "op": "query",
        "connectionId": str(binding.connection_id),
        "sql": "SELECT sum(amount_cent)::text AS amount FROM analytics_978.daily_fact "
        "WHERE channel='WEB'",
    }

    def enable_sources(journal):
        # This test exercises the answer protocol's actual evidence path, without model assembly.
        binding = {"answerProtocol": 2, "registry": {"pins": []}}
        with runtime.store.transaction() as tx:
            _, run = runtime.store.lock(tx, journal.run_id, CHAT)
            run.config_snapshot = {**run.config_snapshot, "contextRuntime": binding}
        journal.context_binding = binding

    async def query_once(journal, identifier):
        enable_sources(journal)
        token = current_execution.set(journal)
        try:
            entry = database_registration((binding,), query=counted_query)
            registry = ToolRegistry([entry])
            dispatcher = ToolDispatcher(registry, ExecutionSlots(), ArtifactStore())
            ctx = replace(context("database_query"), run_id=journal.run_id)
            query = call("database_query", identifier, json.dumps(args))
            result = await dispatcher.execute(dispatcher.prepare(query, ctx), ctx)
            assert result.status == "ok", result.reason_code
            assert isinstance(result.payload, dict)
            observed = after_tool_batch(
                journal,
                SimpleNamespace(id=ctx.step_id, tool_calls=[query]),
                [result],
                registry,
                CHAT,
            )
            assert observed[0]["evidence"], observed
            assert result.payload["rows"] == [["350"]]
            return result, observed[0]["evidence"][0]["evidenceId"], ctx
        finally:
            current_execution.reset(token)

    first_result, original_id, _ = asyncio.run(query_once(runtime.journal, "sales-first"))
    assert isinstance(first_result.payload, dict)
    assert not first_result.payload.get("reused")
    # A successful query survives a later failed Run, and is usable when the goal continues.
    finish(runtime, status="failed")
    runtime.controller.tick()
    following = claim(runtime.store, latest(runtime).id)
    result, imported_id, query_context = asyncio.run(query_once(following, "sales-reuse"))
    assert isinstance(result.payload, dict)
    assert result.payload["reused"] is True
    assert imported_id != original_id
    assert len(actual_queries) == 1
    assert view(runtime)["counts"]["reusedCalls"] == 1
    assert query_context.sources[result.payload["resultId"]].run_id == following.run_id
    evidence = getattr(following, "answer_evidence", {})[execution_path.get()]
    assert evidence[imported_id]["originRunId"] == str(runtime.journal.run_id)
    candidate = ANSWER.validate_python(
        {
            "mode": "evidence",
            "completion": "complete",
            "coveredGoalIds": ["sales"],
            "unresolved": [],
            "claims": [
                {
                    "id": "sales",
                    "kind": "fact",
                    "text": "销售金额为 350 分。",
                    "refs": [{"evidenceId": imported_id, "row": 0, "column": "amount"}],
                }
            ],
            "blocks": [{"type": "paragraph", "claimIds": ["sales"]}],
        }
    ).model_dump(mode="json", by_alias=True)
    record = {"candidate": candidate, "digest": digest(candidate)}
    review = {
        "candidateDigest": record["digest"],
        "evidenceRevision": digest(evidence),
        "decision": "accept",
        "modeValid": True,
        "goals": [{"goalId": "sales", "status": "covered", "reason": "查询覆盖销售问题"}],
        "claims": [{"claimId": "sales", "status": "supported", "reason": "查询金额为350"}],
        "feedback": "",
    }
    decision = before_delivery(following.run_id, record, evidence, review)
    assert decision.action == "accept", decision.feedback
    assert "350" in decision.body and decision.citations["anchors"]
    assert check_access(following, CHAT)
    with admin_connection(runtime.store.engine) as connection:
        connection.execute(
            sql.SQL("REVOKE SELECT ON analytics_978.daily_fact FROM {}").format(
                sql.Identifier(binding.role)
            )
        )
    assert not check_access(following, CHAT)
    authorized = query_context.sources[result.payload["resultId"]]

    async def revalidate():
        assert authorized.revalidate is not None
        return await authorized.revalidate()

    assert not asyncio.run(revalidate())


@pytest.mark.parametrize("union_query", [False, True])
def test_large_query_reuses_original_manifest_and_rechecks_cross_run_pages(
    data_source, tmp_path, union_query
):
    class StrictDispatcher(ToolDispatcher):
        async def _invoke(self, prepared, context):
            try:
                return await super()._invoke(prepared, context)
            except Exception as error:
                pytest.fail(f"Large result adapter failed: {type(error).__name__}: {error}")

    runtime = source_runtime(data_source, compass_context_results_root=str(tmp_path))
    _, _, generic_binding = data_source
    binding = replace(generic_binding, scope_check=LegacyScopeCheck(101, generic_binding.role))
    # The real source uses the supported tenant/region policy, so the result has durable witnesses.
    with admin_connection(runtime.store.engine) as connection:
        connection.execute("CREATE SCHEMA core")
        connection.execute("CREATE SCHEMA sec")
        connection.execute(
            "CREATE FUNCTION sec.allowed(t smallint, r bigint) RETURNS boolean "
            "LANGUAGE sql STABLE AS 'SELECT t = 101'"
        )
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA core, sec TO {}").format(sql.Identifier(binding.role))
        )
        for table in ("channel_product_daily", "channel", "sku", "spu"):
            connection.execute(
                sql.SQL(
                    "CREATE TABLE core.{} (tenant_id smallint, region_id bigint, "
                    "amount bigint, note text)"
                ).format(sql.Identifier(table))
            )
            connection.execute(
                sql.SQL(
                    "INSERT INTO core.{} SELECT 101, 1, n, repeat('商品明细', 8) "
                    "FROM generate_series(1, 1500) n"
                ).format(sql.Identifier(table))
            )
            connection.execute(
                sql.SQL("ALTER TABLE core.{} ENABLE ROW LEVEL SECURITY").format(
                    sql.Identifier(table)
                )
            )
            connection.execute(
                sql.SQL(
                    "CREATE POLICY retail_scope ON core.{} FOR SELECT "
                    "USING(sec.allowed(tenant_id, region_id))"
                ).format(sql.Identifier(table))
            )
            connection.execute(
                sql.SQL("GRANT SELECT ON core.{} TO {}").format(
                    sql.Identifier(table), sql.Identifier(binding.role)
                )
            )
    actual_queries = []
    arguments = {
        "op": "query",
        "connectionId": str(binding.connection_id),
        "sql": "SELECT amount, note FROM core.channel_product_daily "
        + ("UNION ALL SELECT amount, note FROM core.sku " if union_query else "")
        + "ORDER BY amount",
    }

    async def query(journal, identifier):
        with runtime.store.transaction() as tx:
            _, run = runtime.store.lock(tx, journal.run_id, CHAT)
            run.config_snapshot = {**run.config_snapshot, "contextRuntime": {"answerProtocol": 2}}
        journal.context_binding = {"answerProtocol": 2}
        journal.context_results = ContextResults(journal)

        async def execute_sql(request, source, control):
            async def read():
                actual_queries.append(identifier)
                return await query_database_live(request, source, control)

            try:
                return await journal.call(
                    "tool",
                    f"root/large:{identifier}",
                    arguments,
                    read,
                    DatabaseResult | RunToolError,
                    yield_boundary=False,
                )
            except Exception as error:
                pytest.fail(f"Large query receipt failed: {type(error).__name__}: {error}")

        token = current_execution.set(journal)
        try:
            entry = database_registration((binding,), query=execute_sql)
            registry = ToolRegistry([entry])
            dispatcher = StrictDispatcher(registry, ExecutionSlots(), ArtifactStore())
            ctx = replace(context("database_query"), run_id=journal.run_id)
            request = call("database_query", identifier, json.dumps(arguments))
            result = await dispatcher.execute(dispatcher.prepare(request, ctx), ctx)
            assert result.status == "ok", result.reason_code
            after_tool_batch(
                journal,
                SimpleNamespace(id=ctx.step_id, tool_calls=[request]),
                [result],
                registry,
                CHAT,
            )
            return result
        finally:
            current_execution.reset(token)

    first = asyncio.run(query(runtime.journal, "large-first"))
    expected_count = 3000 if union_query else 1500
    assert first.context_result and first.context_result["returnedCount"] == expected_count
    assert first.context_result["sourceDeps"][0]["kind"] == "retail"
    if union_query:
        assert set(first.context_result["sourceDeps"][0]["columns"]) == {
            "core.channel_product_daily",
            "core.sku",
        }

        async def uncertified_leaf():
            ctx = context("database_query")
            request = call(
                "database_query",
                "untrusted-leaf",
                json.dumps(
                    {
                        "op": "query",
                        "sql": "SELECT amount, note FROM core.channel_product_daily "
                        "UNION ALL SELECT amount_cent, channel FROM analytics_978.daily_fact",
                    }
                ),
            )
            return await query_database_live(request, binding, ctx.control)

        uncertified = asyncio.run(uncertified_leaf())
        assert isinstance(uncertified, DatabaseResult)
        assert uncertified.source_ref == {"kind": "transient"}
    assert isinstance(first.payload, dict) and "rows" not in first.payload
    finish(runtime, status="failed")
    runtime.controller.tick()
    following = claim(runtime.store, latest(runtime).id)
    reused = asyncio.run(query(following, "large-reuse"))
    assert isinstance(reused.payload, dict) and reused.payload["reused"] is True
    assert reused.context_result == json.loads(json.dumps(first.context_result))
    assert reused.context_result is not None
    assert actual_queries == ["large-first"]
    assert following.context_results is not None
    ref = reused.context_result["ref"]
    assert following.context_results.resolve(ref, CHAT)["sourceRunId"] == str(
        runtime.journal.run_id
    )

    async def page():
        ctx = replace(context("read_tool_result"), run_id=following.run_id)
        assert following.context_results is not None
        return await following.context_results.read(
            ReadToolResultInput(ref=ref, offset=expected_count - 1, limit=1), ctx
        )

    result = asyncio.run(page())
    assert isinstance(result.payload, dict) and result.payload["data"][0][0] == 1500
    with admin_connection(runtime.store.engine) as connection:
        connection.execute(
            sql.SQL("REVOKE SELECT ON core.{} FROM {}").format(
                sql.Identifier("sku" if union_query else "channel_product_daily"),
                sql.Identifier(binding.role),
            )
        )
    with pytest.raises(ToolRejected, match="result_access_denied"):
        asyncio.run(page())


def test_ledger_retains_many_authorized_sources_with_bounded_storage(goal_runtime):
    runtime = goal_runtime
    refs = [
        {"kind": "semantic", "version_id": str(uuid4()), "default_term": True} for _ in range(16)
    ] + [{"kind": "semantic_open", "offer": str(uuid4())} for _ in range(32)]
    write_entry(runtime, "many-sources", "conclusion", "done", sourceRefs=refs)
    with runtime.store.transaction() as tx:
        entry = tx.get(AgentGoalEntry, (runtime.goal_id, "many-sources"))
        saved = runtime.store.private.open(runtime.goal_id, "ledger", entry.private_body)
        assert saved["sourceRefs"] == refs
    for oversized in ([{"kind": "transient"}] * 257, [{"kind": "x", "value": "x" * 524288}]):
        with pytest.raises(ModelError, match="ledger_source_limit"):
            write_entry(runtime, "oversized", sourceRefs=oversized)
    with runtime.store.transaction() as tx:
        assert tx.get(AgentGoalEntry, (runtime.goal_id, "oversized")) is None


def test_paused_goal_anchor_has_user_pause_stop_reason(goal_runtime):
    runtime = goal_runtime
    finish(runtime)
    runtime.controller.tick()
    journal = claim(runtime.store, latest(runtime).id)
    action(runtime, "pause")
    with pytest.raises(AutonomyStopped, match="goal_paused"):
        anchor(journal, CHAT, AutonomyBudget((), AutonomyLimits()))


@pytest.mark.parametrize("denied", [False, True])
def test_ledger_visibility_batches_shared_sources_without_hiding_accessible_entries(
    monkeypatch, denied
):
    from compass_agent.goal import ledger as module

    common, restricted = {"kind": "common"}, {"kind": "restricted"}
    rows = [
        (SimpleNamespace(entry_key="a", status="done"), {"sourceRefs": [common]}),
        (SimpleNamespace(entry_key="b", status="done"), {"sourceRefs": [common, restricted]}),
        (
            SimpleNamespace(entry_key="c", status="done"),
            {"sourceRefs": [common], "dependsOn": ["b"]},
        ),
    ]
    ledger = Ledger(None)
    monkeypatch.setattr(ledger, "rows", lambda tx, goal: rows)
    calls = []

    def authorize(store, context, refs, **kwargs):
        calls.append(refs)
        return not (denied and restricted in refs)

    monkeypatch.setattr(module, "allows", authorize)
    visible = ledger.visible(None, None, None)
    assert calls[0] == [common, restricted]
    assert [row.entry_key for row, body in visible] == (["a"] if denied else ["a", "b", "c"])
    assert len(calls) == (4 if denied else 1)


@pytest.mark.parametrize("returned", [False, True])
def test_settled_read_cancellation_keeps_completed_receipt_only_after_return(
    goal_runtime, returned
):
    from compass_agent.tools.contracts import PhaseCanceled

    runtime = goal_runtime
    journal = runtime.journal
    reached = asyncio.Event()
    pending = asyncio.Event()
    operation_returned = asyncio.Event()

    async def operation():
        if not returned:
            reached.set()
            await pending.wait()
        operation_returned.set()
        return {"rows": [[1]]}

    async def flush(_run_id):
        if operation_returned.is_set():
            reached.set()
            await pending.wait()

    async def scenario():
        journal.publisher = SimpleNamespace(flush=flush, require_owner=lambda: None)
        task = asyncio.create_task(
            journal.call(
                "tool",
                "root/settled-read",
                {},
                operation,
                dict,
                yield_boundary=False,
                settled_on_return=True,
            )
        )
        await asyncio.wait_for(reached.wait(), 3)
        task.cancel()
        if returned:
            with pytest.raises(PhaseCanceled) as caught:
                await task
            assert caught.value.cleanup_confirmed
        else:
            with pytest.raises(asyncio.CancelledError):
                await task
        with runtime.store.transaction() as tx:
            from compass_agent.runtime.entities import AgentAttempt

            attempt = tx.scalar(
                select(AgentAttempt)
                .join(AgentStep, AgentStep.id == AgentAttempt.step_id)
                .where(
                    AgentAttempt.run_id == journal.run_id, AgentStep.node_key == "root/settled-read"
                )
            )
            assert attempt.status == ("completed" if returned else "unknown")
        if returned:
            assert await journal.call(
                "tool",
                "root/settled-read",
                {},
                operation,
                dict,
                yield_boundary=False,
                settled_on_return=True,
            ) == {"rows": [[1]]}

    asyncio.run(scenario())
