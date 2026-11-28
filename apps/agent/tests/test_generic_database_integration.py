"""Isolated PostgreSQL, Sandbox and durable acceptance without business tools."""

import asyncio
import hashlib
import json
import os
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4, uuid5

import psycopg
import pytest
from compass_agent.data_connections.connections import DataConnections
from compass_agent.data_connections.tools import provider
from compass_agent.runtime.context import compass_definition
from compass_agent.runtime.control import OwnerKey, RunRegistry
from compass_agent.runtime.service import RunService
from compass_agent.schemas import DataConnectionSave, SessionSubmit, ToolApprovalProposal
from compass_agent.settings import Settings
from compass_agent.tools.builtin.database_query import DatabaseResult, query_database
from compass_agent.tools.contracts import ExecutionReceipt, ToolPolicy
from compass_agent.tools.loader import ToolLoader
from cryptography.fernet import Fernet
from psycopg import sql
from sqlalchemy import create_engine

from .context_fixture import install_prompts
from .test_agent_run import CHAT, FakeModels, FakeRouting, model_result, run_command, selected_route
from .test_database_tools_integration import db
from .test_database_tools_integration import targets as targets
from .test_durable_runtime_integration import decision, execute, service
from .test_tool_dispatcher import call, registration
from .test_tool_runtime_integration import real_backend


@pytest.fixture
def data_source(targets):
    runtime, _ = targets("generic_tools")
    db.migrate()
    engine = install_prompts(create_engine(runtime))
    admin = urlsplit(os.environ["AGENT_TEST_ADMIN_URL"])
    admin_target = urlunsplit(admin._replace(path=urlsplit(runtime).path))
    role = "generic978_" + uuid4().hex[:12]
    password = "isolated-test-only-978"
    with psycopg.connect(db.dsn(admin_target), autocommit=True) as client:
        client.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        client.execute(
            sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(
                sql.Identifier(role)
            )
        )
        client.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(urlsplit(runtime).path.lstrip("/")), sql.Identifier(role)
            )
        )
        client.execute("CREATE SCHEMA analytics_978")
        client.execute("CREATE SCHEMA private_978")
        client.execute(
            "CREATE TABLE analytics_978.daily_fact "
            "(tenant_id int, day date, channel text, amount_cent bigint)"
        )
        client.execute(
            "COMMENT ON TABLE analytics_978.daily_fact IS "
            "'按日期和渠道汇总的实付金额，一行是日期渠道汇总，不代表订单或支付笔数'"
        )
        client.execute(
            "INSERT INTO analytics_978.daily_fact VALUES "
            "(101,'2026-04-01','WEB',100),(101,'2026-04-08','WEB',250),"
            "(101,'2026-05-01','STORE',700),(999,'2026-04-01','WEB',999999)"
        )
        client.execute(
            "COMMENT ON COLUMN analytics_978.daily_fact.amount_cent IS '实付金额，单位：分'"
        )
        client.execute("ALTER TABLE analytics_978.daily_fact ENABLE ROW LEVEL SECURITY")
        client.execute(
            sql.SQL(
                "CREATE POLICY scoped ON analytics_978.daily_fact "
                "FOR SELECT TO {} USING (tenant_id=101)"
            ).format(sql.Identifier(role))
        )
        client.execute(
            sql.SQL("GRANT USAGE ON SCHEMA analytics_978 TO {}").format(sql.Identifier(role))
        )
        client.execute(
            sql.SQL("GRANT SELECT ON analytics_978.daily_fact TO {}").format(sql.Identifier(role))
        )
        client.execute("CREATE TABLE private_978.secret(value text)")
    scope = CHAT.scope
    from compass_agent.schemas import TenantScope

    assert isinstance(scope, TenantScope)
    settings = Settings(
        compass_data_allowed_endpoints=f"{admin.hostname}:{admin.port or 5432}",
        compass_model_encryption_key=Fernet.generate_key().decode(),
    )
    connections = DataConnections(engine, settings)
    connections.save_connection(
        scope,
        DataConnectionSave.model_validate(
            {
                "name": "自主查询验收",
                "engine": "postgresql",
                "host": admin.hostname,
                "port": admin.port or 5432,
                "database_name": urlsplit(runtime).path.lstrip("/"),
                "role": role,
                "sslmode": "disable",
                "password": password,
                "revision": 0,
            }
        ),
        None,
    )
    binding = connections.bindings_for_run(scope)[0]
    assert binding.scope_check is None
    try:
        yield engine, connections, binding
    finally:
        engine.dispose()
        with psycopg.connect(db.dsn(admin_target), autocommit=True) as client:
            client.execute("DROP SCHEMA analytics_978 CASCADE")
            client.execute("DROP SCHEMA private_978 CASCADE")
            client.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            client.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_registered_semantic_binding_requires_unchanged_row_policy_and_contract(data_source):
    from compass_agent.semantic.execution import calculate
    from compass_agent.tools.contracts import ToolRejected

    _, _, adapter = data_source
    statement = (
        "SELECT sum(f.amount_cent)::text AS value FROM analytics_978.daily_fact f "
        "WHERE f.day>=($1::jsonb->>'start')::date AND f.day<($1::jsonb->>'end')::date "
        "AND jsonb_typeof($2::jsonb)='object'"
    )
    with psycopg.connect(adapter.dsn.get_secret_value()) as source:
        expr = source.execute(
            "SELECT pg_get_expr(polqual,polrelid) FROM pg_policy "
            "WHERE polrelid='analytics_978.daily_fact'::regclass"
        ).fetchone()
    assert expr
    plan = {
        "sql_text": statement,
        "target_hash": hashlib.sha256(statement.encode()).hexdigest(),
        "parameter_schema": {"type": "object", "required": ["start", "end"]},
        "result_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        "access_requirements": {
            "protocol": "postgres_readonly",
            "identity": "verified_session_user",
            "required_relations": ["daily_fact"],
            "namespace_profiles": {"generic": ["pg_catalog", "analytics_978", "pg_temp"]},
            "policy_expressions": {"daily_fact": [expr[0]]},
        },
    }
    args = {"start": "2026-04-01", "end": "2026-05-01"}

    async def scenario():
        control = RunRegistry().register(OwnerKey.from_context(CHAT))
        rows, witness = await calculate(adapter, plan, args, control)
        assert rows == [{"value": "350"}]
        assert witness["tables"] == ["daily_fact"]
        wrong = {
            **plan,
            "access_requirements": {
                **plan["access_requirements"],
                "policy_expressions": {"daily_fact": ["false"]},
            },
        }
        with pytest.raises(ToolRejected, match="semantic_source_unavailable"):
            await calculate(adapter, wrong, args, control)
        with pytest.raises(ToolRejected, match="semantic_binding_changed"):
            await calculate(adapter, {**plan, "target_hash": "0" * 64}, args, control)

    asyncio.run(scenario())


def test_generic_connection_discovery_readonly_and_rls(data_source):
    _, connections, binding = data_source

    async def scenario():
        control = RunRegistry().register(OwnerKey.from_context(CHAT))

        async def query(args):
            return await query_database(
                call("database_query", "query", json.dumps(args)), binding, control
            )

        tables = await query({"op": "list_tables"})
        assert isinstance(tables, DatabaseResult)
        assert any(row[0] == "analytics_978" for row in tables.rows)
        columns = await query(
            {"op": "describe_table", "schema": "analytics_978", "table": "daily_fact"}
        )
        assert isinstance(columns, DatabaseResult) and any(
            "单位：分" in str(row) for row in columns.rows
        )
        totals = await query(
            {
                "op": "query",
                "sql": (
                    "SELECT channel, sum(amount_cent) AS amount "
                    "FROM analytics_978.daily_fact GROUP BY channel ORDER BY channel"
                ),
            }
        )
        assert isinstance(totals, DatabaseResult) and totals.rows == (
            ("STORE", 1050 - 350),
            ("WEB", 350),
        )
        for unsafe in (
            "DELETE FROM analytics_978.daily_fact",
            "SELECT * FROM private_978.secret",
            "SELECT * FROM pg_catalog.pg_roles",
        ):
            assert await query({"op": "query", "sql": unsafe}) == "invalid_parameters"
        empty = await query(
            {
                "op": "query",
                "sql": "SELECT amount_cent FROM analytics_978.daily_fact WHERE day >= '2035-01-01'",
            }
        )
        assert isinstance(empty, DatabaseResult) and empty.rows == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("channel,expected", [("WEB", 350), ("STORE", 700)])
def test_same_runtime_handles_different_channels_without_case_routing(
    data_source, channel, expected
):
    _, connections, _ = data_source

    def answer(value):
        observed = json.loads(value.messages[-1].content)
        assert observed["rows"] == [[expected]]
        return model_result(text=f"{channel} 实付合计 {observed['rows'][0][0]} 分。")

    class Models(FakeModels):
        async def run(self, prepared, value, **kwargs):
            item = self.results.pop(0)
            return item(value) if callable(item) else item

    models = Models(
        [
            model_result(
                tool_calls=(
                    call(
                        "database_query",
                        "sum",
                        json.dumps(
                            {
                                "op": "query",
                                "sql": (
                                    "SELECT sum(amount_cent) FROM analytics_978.daily_fact "
                                    f"WHERE channel='{channel}'"
                                ),
                            }
                        ),
                    ),
                ),
                token="next",
                finish="tool_calls",
            ),
            answer,
        ]
    )

    async def scenario():
        worker = RunService(
            cast(Any, models),
            cast(Any, FakeRouting(selected_route())),
            definition=compass_definition(),
            tools_provider=provider(connections),
        )
        control = RunRegistry().register(OwnerKey.from_context(CHAT))
        result = await worker.run(
            run_command().model_copy(update={"question": f"汇总 {channel} 渠道实付"}),
            CHAT,
            control,
            lambda event: None,
        )
        assert result.status == "completed" and str(expected) in result.final_answer

    asyncio.run(scenario())


def test_sql_to_real_python_sandbox_without_retail(data_source):
    engine, connections, _ = data_source
    backend = real_backend(engine)

    class Models(FakeModels):
        async def run(self, prepared, value, **kwargs):
            if len(self.requests) == 2:
                source = json.loads(value.messages[-1].content)["resultId"]
                return model_result(
                    tool_calls=(
                        call(
                            "python_analysis",
                            "compute",
                            json.dumps(
                                {
                                    "source_query_id": source,
                                    "code": (
                                        "result = {'total': sum(row[0] for row in data['rows'])}"
                                    ),
                                }
                            ),
                        ),
                    ),
                    token="python",
                    finish="tool_calls",
                )
            if len(self.requests) == 3:
                assert json.loads(value.messages[-1].content) == {"total": 1050}
                return model_result(text="实付合计 1050 分。")
            return await super().run(prepared, value, **kwargs)

    models = Models(
        [
            model_result(
                tool_calls=(
                    call(
                        "database_query",
                        "read",
                        json.dumps(
                            {
                                "op": "query",
                                "sql": (
                                    "SELECT amount_cent FROM analytics_978.daily_fact ORDER BY day"
                                ),
                            }
                        ),
                    ),
                ),
                token="sql",
                finish="tool_calls",
            )
        ]
    )

    async def scenario():
        worker = RunService(
            cast(Any, models),
            cast(Any, FakeRouting(selected_route())),
            definition=compass_definition(),
            tools_provider=provider(connections),
            tool_loader=ToolLoader(sandbox=backend),
        )
        control = RunRegistry().register(OwnerKey.from_context(CHAT))
        result = await worker.run(run_command(), CHAT, control, lambda event: None)
        assert result.status == "completed" and "1050" in result.final_answer

    asyncio.run(scenario())


def test_generic_approval_survives_restart_and_executes_once(data_source):
    engine, _, _ = data_source
    effects = []

    async def write(args, context):
        effects.append(context.call_id)
        return ExecutionReceipt(context.call_id, "ok", "confirmed", {"saved": True})

    def proposal(args, context):
        return ToolApprovalProposal(
            proposalId=str(uuid5(context.run_id, str(context.step_id) + context.call_id)),
            toolName="save_note",
            toolCallId=context.call_id,
            argumentsDigest=hashlib.sha256(b"{}").hexdigest(),
            summary="保存测试记录",
        )

    entry = registration(
        "save_note", write, policy=ToolPolicy("external_write", "required"), proposal=proposal
    )
    from compass_agent.harness.context import AgentDefinition

    class ApprovalModels(FakeModels):
        async def run(self, prepared, request, **kwargs):
            if any(message.role == "tool" for message in request.messages):
                return model_result(text="已保存。")
            return model_result(
                tool_calls=(call("save_note", "save"),), token="next", finish="tool_calls"
            )

    def worker():
        models = ApprovalModels([])
        return RunService(
            cast(Any, models),
            cast(Any, FakeRouting(selected_route())),
            definition=AgentDefinition(tools=(entry,)),
        )

    async def scenario():
        async with service(engine) as first:
            run = await execute(
                first,
                SessionSubmit(
                    requestId=uuid4(), question="保存记录", selection=run_command().selection
                ),
                worker(),
            )
            assert run.status == "waiting" and effects == []
            run_id = run.id
        async with service(engine) as restored:
            run = restored.store.read_run(run_id)
            restored.hitl.decide(
                run.id, decision(restored, run), CHAT, may_approve=True, boot_id=restored.boot_id
            )
            restored.schedule(run.id, worker(), CHAT)
            await asyncio.wait_for(restored.tasks[run.id], 30)
            assert restored.store.read_run(run.id).status == "completed"
            assert effects == ["save"]
            from compass_agent.runtime.entities import AgentAction
            from sqlalchemy import select
            from sqlalchemy.orm import Session

            with Session(engine) as tx:
                action = tx.scalar(select(AgentAction).where(AgentAction.run_id == run.id))
                assert action is not None and action.state == "applied"
                assert action.receipt and action.receipt["status"] == "ok"

    asyncio.run(scenario())


def test_previously_accepted_legacy_retry_returns_receipt_without_new_execution(data_source):
    from compass_agent.models.errors import ModelError
    from compass_agent.runtime.entities import AgentCommand, AgentRun
    from compass_agent.runtime.private_state import digest
    from compass_agent.runtime.store import RuntimeStore
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    engine, _, _ = data_source
    store = RuntimeStore(engine, Settings().compass_runtime_state_key.get_secret_value())
    store.ready = True
    command = SessionSubmit(
        requestId=uuid4(), question="旧版本已受理的分析", selection=run_command().selection
    )
    receipt = store.submit(command, CHAT)
    legacy = command.model_dump(mode="json", by_alias=True)
    legacy.pop("constraints")
    legacy["showcase"] = "weekly_review"
    with Session(engine) as tx, tx.begin():
        saved = tx.get(AgentRun, UUID(receipt["runId"]))
        assert saved is not None
        saved.config_snapshot = {**saved.config_snapshot, "command": legacy}
        old = tx.scalar(select(AgentCommand).where(AgentCommand.run_id == saved.id))
        assert old is not None
        old.request_sha256 = digest(legacy)
    assert store.accepted_retry(legacy, CHAT) == receipt
    with Session(engine) as tx:
        assert tx.scalar(select(func.count()).select_from(AgentRun)) == 1
    with pytest.raises(ModelError, match="request_conflict"):
        store.accepted_retry({**legacy, "question": "另一个问题"}, CHAT)
    with pytest.raises(ModelError, match="invalid_parameters"):
        store.accepted_retry({**legacy, "requestId": str(uuid4())}, CHAT)


def test_generic_in_process_write_replays_accepted_receipt_without_repeating_effect(data_source):
    from dataclasses import replace

    from compass_agent.execution_context import current_execution
    from compass_agent.runtime.entities import AgentAttempt
    from compass_agent.runtime.journal import ExecutionJournal
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from .test_tool_dispatcher import context, dispatcher

    engine, _, _ = data_source
    effects = []

    async def write(args, execution):
        effects.append(execution.call_id)
        return ExecutionReceipt(execution.call_id, "ok", "confirmed", {"saved": True})

    def proposal(args, execution):
        return ToolApprovalProposal(
            proposalId=str(uuid5(execution.run_id, "save")),
            toolName="save_note",
            toolCallId=execution.call_id,
            argumentsDigest=hashlib.sha256(b"{}").hexdigest(),
            summary="保存测试记录",
        )

    async def scenario():
        async with service(engine) as runtime:
            receipt = runtime.store.submit(
                SessionSubmit(
                    requestId=uuid4(), question="保存记录", selection=run_command().selection
                ),
                CHAT,
            )
            identifier = UUID(receipt["runId"])
            await runtime.publisher.flush(identifier)
            epoch = runtime.store.claim(identifier, runtime.boot_id)
            execution = replace(context("save_note"), run_id=identifier)
            tool = registration(
                "save_note",
                write,
                policy=ToolPolicy("external_write", "required"),
                proposal=proposal,
            )
            for _ in range(2):
                journal = ExecutionJournal(runtime.store, runtime.publisher, identifier, epoch)
                journal.approved_proposal = {
                    "toolName": "save_note",
                    "args": {},
                    "callId": "save",
                    "stepId": str(execution.step_id),
                }
                token = current_execution.set(journal)
                try:
                    dispatch = dispatcher(tool)
                    prepared = await dispatch.prepare(call("save_note", "save"), execution)
                    result = await dispatch.execute(prepared, execution)
                    assert result.status == "ok" and result.payload == {"saved": True}
                finally:
                    current_execution.reset(token)
            assert effects == ["save"]
            with Session(engine) as tx:
                assert tx.scalar(select(func.count()).select_from(AgentAttempt)) == 1

    asyncio.run(scenario())
