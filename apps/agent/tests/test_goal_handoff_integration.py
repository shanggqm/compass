"""Real PostgreSQL handoff ownership and child subprocess acceptance for feature 983."""

import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from compass_agent.context_runtime.answer_verification import before_delivery
from compass_agent.context_runtime.answers import ANSWER
from compass_agent.context_runtime.evidence import after_tool_batch, user_input
from compass_agent.data_connections.entities import DataConnection
from compass_agent.execution_context import current_execution, execution_path
from compass_agent.goal.contracts import DelegationTask, InputRef, LedgerItem
from compass_agent.goal.handoff import admit_delegation
from compass_agent.goal.ledger import Ledger
from compass_agent.goal.resume import resume_check
from compass_agent.goal.service import GoalTools
from compass_agent.harness.autonomy import AutonomyBudget, AutonomyLimits
from compass_agent.harness.executor import AgentLoop
from compass_agent.runtime.context_results import ContextResults
from compass_agent.runtime.entities import AgentGoal, AgentGoalEntry, AgentStep
from compass_agent.runtime.execution_trace import ExecutionTrace
from compass_agent.runtime.journal import ExecutionJournal
from compass_agent.runtime.private_state import digest
from compass_agent.runtime.session_management import update_session
from compass_agent.runtime.subagent import SubAgent
from compass_agent.schemas import CallResponse, RunToolError, SessionMutation, TenantScope
from compass_agent.tools.artifacts import ArtifactStore
from compass_agent.tools.builtin.database_query import DatabaseResult, query_database_live
from compass_agent.tools.builtin.database_query.handler import registration as database_registration
from compass_agent.tools.builtin.goal import registration as goal_registration
from compass_agent.tools.builtin.python_analysis import python_analysis_tool
from compass_agent.tools.builtin.read_tool_result import ReadToolResultInput
from compass_agent.tools.contracts import ExecutionReceipt, InProcessBinding, ToolRejected
from compass_agent.tools.dispatcher import ExecutionSlots, ToolDispatcher
from compass_agent.tools.registry import ToolRegistry
from sqlalchemy import select

from .test_agent_run import CHAT, model_result
from .test_context_runtime_integration import LocalPublisher
from .test_database_tools_integration import targets as targets
from .test_generic_database_integration import data_source as data_source
from .test_goal_runtime_integration import (
    approved_goal,
    claim,
    command,
    finish,
    latest,
    source_runtime,
)
from .test_goal_runtime_integration import (
    goal_runtime as goal_runtime,
)
from .test_tool_dispatcher import call, context, png


def test_tenant_backpressure_delays_only_affected_goal_and_keeps_other_goal_running(goal_runtime):
    runtime = goal_runtime
    assert isinstance(CHAT.scope, TenantScope)
    finish(runtime)
    peer = CHAT.model_copy(
        update={
            "scope": CHAT.scope.model_copy(update={"tenant_id": uuid4(), "workspace_id": uuid4()})
        }
    )
    other = approved_goal(runtime.store, peer)
    finish(other)
    runtime.store.scheduler.overrides[str(CHAT.scope.tenant_id)] = {"backlog": 1}
    # A real queued request consumes the first tenant's backlog slot.
    runtime.store.submit(command(), CHAT)
    runtime.controller.tick()
    with runtime.store.transaction() as tx:
        blocked = tx.get(AgentGoal, runtime.goal_id)
        allowed = tx.get(AgentGoal, other.goal_id)
        assert blocked.status == "active" and blocked.retry_after is not None
        assert blocked.rounds_started == 0
        assert allowed.status == "active" and allowed.rounds_started == 1
    assert latest(runtime).id == runtime.journal.run_id
    assert latest(other).status == "created"
    assert runtime.store.ready


def test_deleted_session_cancels_its_goal_and_does_not_stop_another_goal(goal_runtime):
    runtime = goal_runtime
    finish(runtime)
    other = approved_goal(runtime.store)
    finish(other)
    receipt = update_session(
        runtime.store, runtime.session_id, SessionMutation(requestId=uuid4()), CHAT
    )
    assert receipt["sessionId"] == str(runtime.session_id)
    runtime.controller.tick()
    with runtime.store.transaction() as tx:
        deleted = tx.get(AgentGoal, runtime.goal_id)
        assert deleted.status == "canceled" and deleted.reason_code == "session_deleted"
        assert tx.get(AgentGoal, other.goal_id).rounds_started == 1
    assert latest(runtime).id == runtime.journal.run_id
    assert latest(other).status == "created"
    assert runtime.store.ready


def test_frozen_catalog_and_search_scope_limit_delegation(data_source, monkeypatch):
    runtime = source_runtime(data_source)
    _, _, binding = data_source
    runtime.journal.freeze_catalog(ToolRegistry([database_registration((binding,))]))

    async def make_context():
        return replace(context("database_query", "python_analysis"), run_id=runtime.journal.run_id)

    ctx = asyncio.run(make_context())
    extra = DelegationTask(name="未绑定工具", objective="计算", tools=["python_analysis"])
    with pytest.raises(ToolRejected, match="delegation_out_of_scope"):
        admit_delegation(runtime.journal, [extra], ctx, set(ctx.visible_names), None)
    task = DelegationTask.model_validate(
        {"name": "查询", "objective": "核对", "connectionIds": [str(binding.connection_id)]}
    )
    snapshot = {
        "tenantId": str(binding.tenant_id),
        "workspaceId": str(binding.workspace_id),
        "kinds": ["detail"],
        "connectionId": str(uuid4()),
        "connectionRevision": binding.revision,
    }
    runtime.journal.search_snapshot = snapshot
    # The BFF authorization transport is controlled; scope filtering and database binding are real.
    calls = []
    monkeypatch.setattr(
        "compass_agent.goal.handoff.verify", lambda *a, **kw: calls.append(kw["kinds"]) or True
    )
    with pytest.raises(ToolRejected, match="delegation_out_of_scope"):
        admit_delegation(runtime.journal, [task], ctx, {"database_query"}, None)
    assert calls == [["detail"]]
    runtime.journal.search_snapshot = {**snapshot, "connectionId": str(binding.connection_id)}
    assert admit_delegation(runtime.journal, [task], ctx, {"database_query"}, None)
    monkeypatch.setattr("compass_agent.goal.handoff.verify", lambda *a, **kw: False)
    with pytest.raises(ToolRejected, match="delegation_out_of_scope"):
        admit_delegation(runtime.journal, [task], ctx, {"database_query"}, None)


def test_identical_todo_and_failure_across_runs_do_not_create_progress(goal_runtime):
    runtime = goal_runtime
    items = [
        LedgerItem(type="todo", key="pending", title="核对库存"),
        LedgerItem(type="failure", key="known-failure", title="当前来源暂不可用"),
    ]

    def record(journal):
        with runtime.store.transaction() as tx:
            goal = tx.get(AgentGoal, runtime.goal_id, with_for_update=True)
            Ledger(runtime.store).record(tx, goal, items, journal, CHAT)
            return goal.ledger_revision

    revision = record(runtime.journal)
    finish(runtime)
    runtime.controller.tick()
    following = claim(runtime.store, latest(runtime).id)
    assert record(following) == revision
    assert record(following) == revision


def test_parallel_children_import_only_explicit_inputs_and_replay_final_once(goal_runtime):
    runtime = goal_runtime
    journal = runtime.journal
    journal.context_binding = {"answerProtocol": 2}
    shared = user_input(journal, "获准销售金额为 350 分。")
    user_input(journal, "获准销售金额为 350 分。")
    physical_calls = []
    starts = set()

    async def scenario():
        both_started = asyncio.Event()
        ctx = replace(context(), run_id=journal.run_id, call_id="batch")
        # subagent_begin attaches children to the real accepted parent model Step.
        await journal.call(
            "model",
            "root/model:" + str(ctx.step_id),
            {},
            lambda: asyncio.sleep(0, result=model_result(text="准备委派")),
            CallResponse,
            yield_boundary=False,
        )

        class Executor:
            async def _invoke_model(self, step, request, *args, **kwargs):
                current = current_execution.get()
                task = json.loads(request.messages[1].content)
                key = execution_path.get() + "/model:" + str(step.index)

                async def invoke():
                    physical_calls.append((task["name"], step.index))
                    if step.index == 1:
                        starts.add(task["name"])
                        if len(starts) == 2:
                            both_started.set()
                        await asyncio.wait_for(both_started.wait(), 5)
                        payload = {"kind": "progress", "summary": task["name"] + "正在核对"}
                    else:
                        assert step.index == 2
                        imported = task["inputs"][0]["content"]["id"]
                        assert imported != shared
                        payload = {
                            "kind": "final",
                            "result": {
                                "status": "completed",
                                "summary": task["name"] + "完成",
                                "findings": [
                                    {"statement": "销售金额350分", "evidenceIds": [imported]}
                                ],
                            },
                        }
                    return model_result(
                        tool_calls=(
                            call("report_to_parent", f"report-{step.index}", json.dumps(payload)),
                        ),
                        finish="tool_calls",
                        token="next",
                    )

                return await current.call(
                    "model",
                    key,
                    request.model_dump(mode="json"),
                    invoke,
                    CallResponse,
                    yield_boundary=False,
                )

            def _require_complete(self, response):
                assert response.status == "completed"

            def _model_error(self, code):
                return code

            def _final_answer(self, answer):
                return answer

        bindings = SimpleNamespace(
            runtime=SimpleNamespace(registry=ToolRegistry([])),
            state=SimpleNamespace(trace=None),
            executor=Executor(),
            controlled=None,
            route=None,
        )
        tasks = [
            DelegationTask(
                name=name, objective="复核销售", inputs=[InputRef(kind="evidence", id=shared)]
            )
            for name in ("销售核对", "交叉检查")
        ]
        token = current_execution.set(journal)
        try:
            first = await SubAgent(bindings).tasks(tasks, ctx)
            assert len(physical_calls) == 4
            assert len(starts) == 2
            assert all(d["status"] == "completed" for d in first["deliveries"])
            assert len({d["deliveryId"] for d in first["deliveries"]}) == 2
            owned = [set(d["evidenceIds"]) for d in first["deliveries"]]
            assert all(owned) and not owned[0].intersection(owned[1])
            assert all(shared not in ids for ids in owned)
        finally:
            current_execution.reset(token)
        # Recreated coordinator + Journal reads accepted model and delivery records.
        restored = ExecutionJournal(runtime.store, LocalPublisher(), journal.run_id, journal.epoch)
        restored.context_binding = {"answerProtocol": 2}
        vars(restored)["answer_evidence"] = {
            "root": getattr(journal, "answer_evidence", {})["root"]
        }
        token = current_execution.set(restored)
        try:
            again = await SubAgent(bindings).tasks(tasks, ctx)
            assert again == first
            assert len(physical_calls) == 4
        finally:
            current_execution.reset(token)
        with runtime.store.transaction() as tx:
            children = tx.scalars(select(AgentStep).where(AgentStep.kind == "subagent")).all()
            assert len(children) == 2 and len({c.id for c in children}) == 2
            assert all(
                c.status == "completed" and c.accepted_attempt_id and c.parent_step_id
                for c in children
            )
            for child in children:
                descendants = tx.scalars(
                    select(AgentStep).where(AgentStep.parent_step_id == child.id)
                ).all()
                assert len(descendants) == 2
            assert (
                len(
                    tx.scalars(
                        select(AgentGoalEntry).where(AgentGoalEntry.kind == "delivery")
                    ).all()
                )
                == 2
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("legacy_receipt", [False, True])
def test_budget_denied_children_replay_identically_and_parent_records_progress(
    goal_runtime, legacy_receipt
):
    runtime = goal_runtime
    journal = runtime.journal
    journal.context_binding = {"answerProtocol": 2}
    physical_calls = []

    async def scenario():
        ctx = replace(context("goal"), run_id=journal.run_id, call_id="denied-batch")
        await journal.call(
            "model",
            "root/model:" + str(ctx.step_id),
            {},
            lambda: asyncio.sleep(0, result=model_result(text="准备委派")),
            CallResponse,
            yield_boundary=False,
        )

        class Executor(AgentLoop):
            async def _invoke_model(
                self,
                step,
                request,
                route,
                context,
                control,
                budget,
                emit=None,
                *,
                publish_text=True,
            ):
                current = current_execution.get()
                key = execution_path.get() + "/model:" + str(step.index)

                async def invoke():
                    physical_calls.append(key)
                    budget.reason = "cost_budget"
                    return model_result().model_copy(
                        update={"status": "failed", "error_code": "autonomy_admission_denied"}
                    )

                # Emulate the old zero-spend receipt without a persisted stop reason.
                recording_budget = None if legacy_receipt and current is journal else budget
                return await current.call(
                    "model",
                    key,
                    request.model_dump(mode="json"),
                    invoke,
                    CallResponse,
                    budget=recording_budget,
                    yield_boundary=False,
                )

        bindings = SimpleNamespace(
            runtime=SimpleNamespace(registry=ToolRegistry([])),
            state=SimpleNamespace(trace=ExecutionTrace(lambda event: None)),
            executor=Executor(),
            controlled=True,
            budget=AutonomyBudget((), AutonomyLimits()),
            route=None,
        )
        tasks = [DelegationTask(name=name, objective="核对") for name in ("整体", "库存")]
        token = current_execution.set(journal)
        try:
            first = await SubAgent(bindings).tasks(tasks, ctx)
        finally:
            current_execution.reset(token)
        assert len(physical_calls) == 2
        assert all(d["reason"] == "budget" and d["status"] == "failed" for d in first["deliveries"])
        assert all(n.status == "failed" for n in bindings.state.trace.nodes.values())

        restored = journal
        for _ in range(2):
            restored = ExecutionJournal(
                runtime.store, LocalPublisher(), journal.run_id, journal.epoch
            )
            restored.context_binding = {"answerProtocol": 2}
            bindings.budget = AutonomyBudget((), AutonomyLimits())
            bindings.state.trace = ExecutionTrace(lambda event: None)
            token = current_execution.set(restored)
            try:
                again = await SubAgent(bindings).tasks(tasks, ctx)
                assert again == first
                assert len(physical_calls) == 2
                assert bindings.budget.tokens == bindings.budget.cost_microusd == 0
                assert bindings.budget.reason is None
                assert all(n.status == "failed" for n in bindings.state.trace.nodes.values())
            finally:
                current_execution.reset(token)

        from compass_agent.goal.contracts import RecordProgress

        result = GoalTools(restored).execute(
            RecordProgress.model_validate(
                {
                    "op": "record",
                    "items": [{"type": "failure", "key": "parallel.failed", "title": "主任务补查"}],
                }
            ),
            ctx,
        )
        assert result["goal"]["status"] == "active"
        with runtime.store.transaction() as tx:
            entries = tx.scalars(select(AgentGoalEntry)).all()
            assert len([e for e in entries if e.kind == "delivery"]) == 2
            assert any(e.entry_key == "parallel.failed" for e in entries)

    asyncio.run(scenario())


def test_large_goal_receipt_has_persistent_owner_sources_and_readable_original(
    goal_runtime, tmp_path
):
    runtime = goal_runtime
    journal = runtime.journal
    runtime.store.source_settings.compass_context_results_root = str(tmp_path)
    journal.context_binding = {"answerProtocol": 2}
    journal.context_results = ContextResults(journal)
    with runtime.store.transaction() as tx:
        goal = tx.get(AgentGoal, runtime.goal_id, with_for_update=True)
        goal.contract = {
            "objective": "月度经营复盘" * 300,
            "acceptance": [{"id": f"a{i}", "text": "验收要求" * 70} for i in range(1, 11)],
            "constraints": ["授权数据范围" * 80 for _ in range(10)],
        }
    entry = goal_registration(SimpleNamespace(goals=GoalTools(journal)))

    async def scenario():
        ctx = replace(context("goal"), run_id=journal.run_id)
        request = call(
            "goal",
            "long-read",
            '{"op":"record","items":[{"type":"todo","key":"long","title":"长回执"}]}',
        )
        registry = ToolRegistry([entry])
        dispatcher = ToolDispatcher(registry, ExecutionSlots(), ArtifactStore())
        token = current_execution.set(journal)
        try:
            result = await dispatcher.execute(await dispatcher.prepare(request, ctx), ctx)
            assert result.status == "ok", result.reason_code
            assert result.context_result and result.context_result["sourceDeps"] == []
            return result.context_result
        finally:
            current_execution.reset(token)

    manifest = asyncio.run(scenario())
    finish(runtime)
    runtime.controller.tick()
    following = claim(runtime.store, latest(runtime).id)
    following.context_results = ContextResults(following)
    assert following.context_results.resolve(manifest["ref"], CHAT)["sha256"] == manifest["sha256"]

    async def read():
        assert following.context_results is not None
        return await following.context_results.read(
            ReadToolResultInput(ref=manifest["ref"]),
            replace(context("read_tool_result"), run_id=following.run_id),
        )

    assert asyncio.run(read()).status == "ok"


@pytest.mark.parametrize("revision_changes", [False, True])
def test_effect_keeps_original_file_and_imports_complete_evidence_without_reexecution(
    data_source, tmp_path, revision_changes
):
    runtime = source_runtime(data_source)
    _, connections, initial_binding = data_source
    artifact_file = tmp_path / "chart.png"
    physical_writes = []

    async def execute(journal, binding, suffix):
        with runtime.store.transaction() as tx:
            _, run = runtime.store.lock(tx, journal.run_id, CHAT)
            run.config_snapshot = {**run.config_snapshot, "contextRuntime": {"answerProtocol": 2}}
        journal.context_binding = {"answerProtocol": 2}
        ctx = replace(context("database_query", "python_analysis"), run_id=journal.run_id)

        async def sql_read(request, source, control):
            return await journal.call(
                "tool",
                "root/metadata:" + suffix,
                {},
                lambda: query_database_live(request, source, control),
                DatabaseResult | RunToolError,
                yield_boundary=False,
            )

        async def write_chart(args, child_context):
            # The sandbox adapter is controlled; file write, production policy, fingerprint,
            # dispatcher, persisted artifact and ledger are exercised for real.
            async def run():
                physical_writes.append(suffix)
                artifact_file.write_bytes(png())
                source = child_context.sources[args["source_query_id"]].data
                assert isinstance(source, dict)
                return {
                    "resultId": "derived-" + suffix,
                    "columns": ["column_count"],
                    "rows": [[len(source["rows"])]],
                }

            payload = await journal.call(
                "tool", "root/python:" + suffix, args, run, dict, yield_boundary=False
            )
            return ExecutionReceipt(
                child_context.call_id, "ok", "confirmed", payload, file=artifact_file.read_bytes()
            )

        python = replace(python_analysis_tool(), binding=InProcessBinding(write_chart))
        registry = ToolRegistry([database_registration((binding,), query=sql_read), python])
        dispatcher = ToolDispatcher(registry, ExecutionSlots(), ArtifactStore())
        token = current_execution.set(journal)
        try:
            request = call(
                "database_query",
                "metadata-" + suffix,
                json.dumps(
                    {
                        "op": "describe_table",
                        "connectionId": str(binding.connection_id),
                        "schema": "analytics_978",
                        "table": "daily_fact",
                    }
                ),
            )
            source = await dispatcher.execute(await dispatcher.prepare(request, ctx), ctx)
            assert source.status == "ok" and isinstance(source.payload, dict)
            after_tool_batch(
                journal,
                SimpleNamespace(id=ctx.step_id, tool_calls=[request]),
                [source],
                registry,
                CHAT,
            )
            py_ctx = replace(ctx, step_id=uuid4())
            request = call(
                "python_analysis",
                "chart-" + suffix,
                json.dumps(
                    {
                        "source_query_id": source.payload["resultId"],
                        "code": "render_schema_column_count(input)",
                    }
                ),
            )
            result = await dispatcher.execute(await dispatcher.prepare(request, py_ctx), py_ctx)
            assert result.status == "ok" and isinstance(result.payload, dict)
            observed = after_tool_batch(
                journal,
                SimpleNamespace(id=py_ctx.step_id, tool_calls=[request]),
                [result],
                registry,
                CHAT,
            )
            return result, observed
        finally:
            current_execution.reset(token)

    first, _ = asyncio.run(execute(runtime.journal, initial_binding, "first"))
    assert first.artifacts
    original_hash = hashlib.sha256(artifact_file.read_bytes()).hexdigest()
    assert first.artifacts[0].sha256 == original_hash
    finish(runtime, status="failed")
    if revision_changes:
        with runtime.store.transaction() as tx:
            tx.get(DataConnection, initial_binding.connection_id).revision += 1
            tx.get(AgentGoal, runtime.goal_id).revision += 1
    runtime.controller.tick()
    following = claim(runtime.store, latest(runtime).id)
    with runtime.store.transaction() as tx:
        goal = tx.get(AgentGoal, runtime.goal_id, with_for_update=True)
        resume_check(runtime.store, tx, goal, CHAT, following.epoch)
        revision = goal.ledger_revision
        resume_check(runtime.store, tx, goal, CHAT, following.epoch)
        assert goal.ledger_revision == revision
        effects = [
            (row, body)
            for row, body in Ledger(runtime.store).rows(tx, goal)
            if row.kind == "effect"
        ]
        assert len(effects) == 1 and effects[0][0].status == "done"
        assert (
            effects[0][1]["dataVersion"][str(initial_binding.connection_id)]
            == initial_binding.revision
        )
    binding = connections.bindings_for_run(CHAT.scope)[0]
    reused, observed = asyncio.run(execute(following, binding, "reused"))
    assert isinstance(reused.payload, dict) and reused.payload["reused"] is True
    assert physical_writes == ["first"]
    assert hashlib.sha256(artifact_file.read_bytes()).hexdigest() == original_hash
    assert reused.payload["artifacts"][0]["sha256"] == original_hash
    assert len(observed[0]["evidence"]) >= 2, observed
    evidence = getattr(following, "answer_evidence", {})["root"]
    derived = [
        evidence[item["evidenceId"]]
        for item in observed[0]["evidence"]
        if evidence[item["evidenceId"]].get("parentEvidenceIds")
    ]
    assert len(derived) == 1
    assert set(derived[0]["parentEvidenceIds"]) <= set(evidence)
    assert all(ref["revision"] == binding.revision for ref in derived[0]["sourceRefs"])
    candidate = ANSWER.validate_python(
        {
            "mode": "evidence",
            "completion": "complete",
            "coveredGoalIds": ["root"],
            "unresolved": [],
            "claims": [
                {
                    "id": "columns",
                    "kind": "fact",
                    "text": "可读取字段数为4。",
                    "refs": [{"evidenceId": derived[0]["id"], "row": 0, "column": "column_count"}],
                }
            ],
            "blocks": [{"type": "paragraph", "claimIds": ["columns"]}],
        }
    ).model_dump(mode="json", by_alias=True)
    record = {"candidate": candidate, "digest": digest(candidate)}
    review = {
        "candidateDigest": record["digest"],
        "evidenceRevision": digest(evidence),
        "decision": "accept",
        "modeValid": True,
        "goals": [{"goalId": "root", "status": "covered", "reason": "完成字段核对"}],
        "claims": [{"claimId": "columns", "status": "supported", "reason": "实际元信息为4个字段"}],
        "feedback": "",
    }
    assert before_delivery(following.run_id, record, evidence, review).action == "accept"


def test_child_drawer_reads_full_accepted_answer_and_rechecks_sources(goal_runtime, monkeypatch):
    from compass_agent.goal import handoff
    from compass_agent.models.errors import ModelError
    from compass_agent.runtime.entities import AgentAttempt

    journal = goal_runtime.journal
    store = goal_runtime.store
    answer = "完整交付内容。" * 150 + "最后一段也必须可见。"

    async def prepare():
        ctx = context()
        await journal.call(
            "model",
            "root/model:" + str(ctx.step_id),
            {},
            lambda: asyncio.sleep(0, result=model_result(text="准备委派")),
            CallResponse,
            yield_boundary=False,
        )
        pending = await journal.subagent_begin("drawer-child", "复核", ctx.step_id)
        assert handoff.read_child_answer(store, journal.run_id, pending[0], CHAT) is None
        result = handoff.delivery(pending[0], None, answer, "natural_end", {})
        await journal.host_result(pending, result)
        return pending

    child_id, attempt_id, _ = asyncio.run(prepare())
    assert handoff.read_child_answer(store, journal.run_id, child_id, CHAT) == answer
    assert handoff.read_child_answer(store, uuid4(), child_id, CHAT) is None
    assert handoff.read_child_answer(store, journal.run_id, uuid4(), CHAT) is None

    with monkeypatch.context() as patch:

        def deny(*args, **kwargs):
            raise ModelError("source_access_denied", 403)

        patch.setattr(handoff, "require_run_sources", deny)
        with pytest.raises(ModelError, match="source_access_denied"):
            handoff.read_child_answer(store, journal.run_id, child_id, CHAT)

    with store.transaction() as tx:
        tx.get(AgentAttempt, attempt_id).output_sha256 = "0" * 64
    with pytest.raises(ModelError, match="service_unavailable"):
        handoff.read_child_answer(store, journal.run_id, child_id, CHAT)
