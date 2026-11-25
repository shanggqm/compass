"""Real PostgreSQL, accepted Journal manifests and shared result files."""

import asyncio
import json
import os
from dataclasses import replace
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
import pytest
from compass_agent.execution_context import current_execution
from compass_agent.runtime.control import OwnerKey, RunRegistry
from compass_agent.runtime.journal import ExecutionJournal
from compass_agent.runtime.store import RuntimeStore
from compass_agent.schemas import MainModelSelection, SessionSubmit
from compass_agent.settings import Settings
from compass_agent.tools.builtin.database_query import DatabaseResult, query_database
from compass_agent.tools.builtin.read_tool_result import ReadToolResultInput
from compass_agent.tools.contracts import ToolRejected
from compass_agent.tools.result_store import INLINE_TOKENS

from .context_fixture import capability_grants
from .test_agent_run import CHAT, PROVIDER_ID
from .test_context_runtime_integration import LocalPublisher
from .test_database_tools_integration import db
from .test_database_tools_integration import targets as targets
from .test_generic_database_integration import data_source as data_source
from .test_tool_dispatcher import call, context


def test_streamed_original_and_accepted_manifest_survive_reader_recreation(data_source, tmp_path):
    engine, _, binding = data_source
    admin = urlsplit(os.environ["AGENT_TEST_ADMIN_URL"])
    from psycopg.conninfo import conninfo_to_dict

    source = conninfo_to_dict(binding.dsn.get_secret_value())
    admin_target = urlunsplit(admin._replace(path="/" + str(source["dbname"])))
    with psycopg.connect(db.dsn(admin_target), autocommit=True) as tx:
        tx.execute(
            "INSERT INTO analytics_978.daily_fact SELECT 101, '2026-06-01'::date, "
            "'ROW-'||g, g FROM generate_series(1,10000) g"
        )
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    store = RuntimeStore(engine, key, settings=Settings(compass_context_results_root=str(tmp_path)))
    store.ready = True
    command = SessionSubmit(
        requestId=uuid4(),
        question="读取完整数据",
        selection=MainModelSelection(mode="fixed", provider_id=PROVIDER_ID),
    )
    run_id = UUID(store.submit(command, CHAT)["runId"])
    epoch = store.claim(run_id, uuid4())

    async def scenario():
        journal = ExecutionJournal(store, LocalPublisher(), run_id, epoch)
        journal.capabilities = lambda: capability_grants(CHAT)
        control = RunRegistry().register(OwnerKey.from_context(CHAT), run_id=run_id)
        token = current_execution.set(journal)
        try:
            query = call(
                "database_query",
                "large",
                json.dumps(
                    {
                        "op": "query",
                        "sql": "SELECT channel, amount_cent FROM analytics_978.daily_fact "
                        "WHERE day='2026-06-01' ORDER BY amount_cent",
                    }
                ),
            )
            result = await query_database(query, binding, control)
            assert isinstance(result, DatabaseResult)
            assert result.context_result and result.context_result["returnedCount"] == 10000
            assert len(result.rows) <= 5
            manifest = result.context_result
            # New reader/journal instance uses accepted encrypted attempts, not an in-memory map.
            restored = ExecutionJournal(store, LocalPublisher(), run_id, epoch)
            assert restored.context_results is not None
            tool_context = replace(context("read_tool_result"), run_id=run_id, control=control)
            page = await restored.context_results.read(
                ReadToolResultInput(ref=manifest["ref"], offset=8000, limit=1), tool_context
            )
            assert isinstance(page.payload, dict)
            assert page.payload["data"] == [["ROW-8001", 8001]]
            assert page.payload["sourceComplete"]
            assert len(json.dumps(page.payload, ensure_ascii=False).encode()) <= INLINE_TOKENS
            # A small SELECT remains original inline data.
            small = await query_database(
                call(
                    "database_query",
                    "small",
                    json.dumps(
                        {
                            "op": "query",
                            "sql": "SELECT channel, amount_cent FROM analytics_978.daily_fact "
                            "WHERE day='2026-06-01' ORDER BY amount_cent LIMIT 3",
                        }
                    ),
                ),
                binding,
                control,
            )
            assert (
                isinstance(small, DatabaseResult)
                and small.context_result is None
                and len(small.rows) == 3
            )
            foreign = CHAT.model_copy(update={"actor_user_id": uuid4()})
            with pytest.raises(ToolRejected, match="result_access_denied"):
                await restored.context_results.read(
                    ReadToolResultInput(ref=manifest["ref"]), replace(tool_context, trusted=foreign)
                )
        finally:
            current_execution.reset(token)

    asyncio.run(scenario())


def test_auxiliary_budget_is_counted_once_across_prepared_main_takeover(data_source, tmp_path):
    from types import SimpleNamespace

    from compass_agent.context_runtime.optimization_calls import restore_budget
    from compass_agent.runtime.entities import AgentAttempt
    from cryptography.fernet import Fernet
    from sqlalchemy.orm import Session

    engine, _, _ = data_source
    store = RuntimeStore(
        engine,
        Fernet.generate_key().decode(),
        settings=Settings(compass_context_results_root=str(tmp_path)),
    )
    store.ready = True
    run_id = UUID(
        store.submit(
            SessionSubmit(
                requestId=uuid4(),
                question="恢复费用验收",
                selection=MainModelSelection(mode="fixed", provider_id=PROVIDER_ID),
            ),
            CHAT,
        )["runId"]
    )
    epoch = store.claim(run_id, uuid4())

    def ledger():
        return SimpleNamespace(steps=0, tokens=0, cost_microusd=0, attempts=0, unknown_attempts=0)

    async def scenario():
        first = ExecutionJournal(store, LocalPublisher(), run_id, epoch)
        old = ledger()

        async def select_once():
            old.tokens += 100
            old.cost_microusd += 7
            old.attempts += 1
            return {"status": "completed"}

        await first.call(
            "ctx_select",
            "root/context-select:test",
            {},
            select_once,
            dict,
            budget=old,
            yield_boundary=False,
        )
        aux = first.last_attempt
        assert aux
        frame = {"input": {"request": {"messages": []}}}
        # Process exits after preparing main, before dispatch. No provider call happened.
        first.prepare(
            "model",
            "root/model:test",
            {},
            None,
            frame_factory=lambda _: frame,
            receive_frame=lambda *_: None,
        )
        restored = ExecutionJournal(store, LocalPublisher(), run_id, epoch)
        fresh = ledger()
        calls = []

        async def validate():
            restore_budget(restored, fresh, [str(aux)])

        async def main():
            calls.append("sent")
            fresh.tokens += 200
            fresh.cost_microusd += 11
            fresh.attempts += 1
            return {"status": "completed"}

        await restored.call(
            "model",
            "root/model:test",
            {},
            main,
            dict,
            budget=fresh,
            frame_factory=lambda _: frame,
            receive_frame=lambda *_: None,
            revalidate=validate,
            yield_boundary=False,
        )
        assert fresh.tokens == 300 and fresh.cost_microusd == 18
        with Session(engine) as tx:
            accepted = tx.get(AgentAttempt, restored.last_attempt)
            assert accepted and accepted.budget_receipt["tokens"] == 200
        replay = ExecutionJournal(store, LocalPublisher(), run_id, epoch)
        again = ledger()

        async def revalidate():
            restore_budget(replay, again, [str(aux)])

        await replay.call(
            "model",
            "root/model:test",
            {},
            main,
            dict,
            budget=again,
            frame_factory=lambda _: frame,
            receive_frame=lambda *_: None,
            revalidate=revalidate,
            yield_boundary=False,
        )
        await replay.call(
            "ctx_select",
            "root/context-select:test",
            {},
            select_once,
            dict,
            budget=again,
            yield_boundary=False,
        )
        assert calls == ["sent"] and again.tokens == 300 and again.cost_microusd == 18

    asyncio.run(scenario())


def test_expired_sources_keep_action_status_and_generic_files_remain_scoped(data_source, tmp_path):
    from dataclasses import asdict

    from compass_agent.context_runtime.history import load_history
    from compass_agent.context_runtime.source_access import record_source
    from compass_agent.tools.contracts import ExecutionReceipt
    from cryptography.fernet import Fernet

    engine, _, binding = data_source
    store = RuntimeStore(
        engine,
        Fernet.generate_key().decode(),
        settings=Settings(compass_context_results_root=str(tmp_path)),
    )
    store.ready = True
    command = SessionSubmit(
        requestId=uuid4(),
        question="PRIVATE USER QUESTION",
        selection=MainModelSelection(mode="fixed", provider_id=PROVIDER_ID),
    )
    accepted = store.submit(command, CHAT)
    run_id = UUID(accepted["runId"])
    epoch = store.claim(run_id, uuid4())

    async def scenario():
        journal = ExecutionJournal(store, LocalPublisher(), run_id, epoch)

        async def completed_action():
            return asdict(
                ExecutionReceipt("call-a", "ok", "remote_managed", {"secret": "PRIVATE OUTPUT"})
            )

        await journal.call(
            "tool",
            "root/physical:call-a",
            {
                "tool": "mcp_write_note",
                "args": {"private": "PRIVATE ARGUMENT"},
            },
            completed_action,
            dict,
            yield_boundary=False,
        )
        record_source(journal, {"kind": "transient"})
        assert journal.context_results
        durable = {
            "kind": "metadata",
            "connection": str(binding.connection_id or binding.workspace_id),
            "tenant": str(binding.tenant_id),
            "workspace": str(binding.workspace_id),
            "revision": binding.revision,
            "role": binding.role,
            "columns": {},
        }
        # Earlier durable reads must not authorize a generic MCP result.
        journal.context_results.dependencies = lambda: [durable]
        control = RunRegistry().register(OwnerKey.from_context(CHAT), run_id=run_id)
        ctx = replace(context("read_tool_result"), run_id=run_id, control=control)
        generic = await journal.context_results.govern(
            ExecutionReceipt("generic", "ok", "confirmed", {"text": "x" * (INLINE_TOKENS + 1)}), ctx
        )
        assert generic.context_result and any(
            r["kind"] == "transient" for r in generic.context_result["sourceDeps"]
        )
        certified = await journal.context_results.govern(
            ExecutionReceipt(
                "metadata",
                "ok",
                "confirmed",
                {"text": "y" * (INLINE_TOKENS + 1)},
                source_deps=(durable,),
            ),
            replace(ctx, call_id="metadata"),
        )
        assert certified.context_result and certified.context_result["sourceDeps"] == [durable]
        store.finish(run_id, epoch, "canceled", reason="user_canceled")
        from compass_agent.runtime.entities import AgentOutbox
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        with Session(engine) as tx:
            packets = [
                {
                    "packet": packet,
                    "outboxId": str(box.id),
                    "batchIndex": index,
                    "batchSize": len(box.payload["packets"]),
                }
                for box in tx.scalars(
                    select(AgentOutbox)
                    .where(AgentOutbox.run_id == run_id)
                    .order_by(AgentOutbox.run_version)
                )
                for index, packet in enumerate(box.payload["packets"])
            ]
        store.project(run_id, [(f"{i + 1}-0", packet) for i, packet in enumerate(packets)])
        following = store.submit(
            command.model_copy(
                update={
                    "request_id": uuid4(),
                    "session_id": UUID(accepted["sessionId"]),
                    "question": "继续交接",
                }
            ),
            CHAT,
        )
        next_id = UUID(following["runId"])
        next_epoch = store.claim(next_id, uuid4())
        reader = ExecutionJournal(store, LocalPublisher(), next_id, next_epoch)
        groups = load_history(reader, CHAT)
        text = json.dumps([g.view() for g in groups], ensure_ascii=False)
        assert "call-a" in text and "mcp_write_note" in text and '"ok"' in text.replace('\\"', '"')
        assert "PRIVATE" not in text
        assert reader.context_results
        with pytest.raises(ToolRejected, match="result_source_expired"):
            reader.context_results.resolve(generic.context_result["ref"], CHAT)

    asyncio.run(scenario())
