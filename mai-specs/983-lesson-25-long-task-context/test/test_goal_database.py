"""983 acceptance against isolated PostgreSQL: 0018 upgrade and backup recovery.

Run from the repository root with the private AGENT_TEST_* environment.
The shared targets fixture provisions and removes only its temporary databases.
"""

from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from compass_agent.runtime.entities import AgentGoal, AgentGoalEntry, AgentRun, AgentSession
from compass_agent.runtime.private_state import PrivateState
from cryptography.fernet import Fernet
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.agent.tests.test_database_tools_integration import db, pg_tool, schema
from apps.agent.tests.test_database_tools_integration import targets as targets


def legacy_run(runtime):
    """Insert an ended 0018 Run without calling the newer Runtime submission path."""
    session_id, run_id = uuid4(), uuid4()
    moment = datetime(2026, 11, 24, tzinfo=UTC)
    engine = create_engine(runtime)
    try:
        with Session(engine) as tx, tx.begin():
            tx.add(
                AgentSession(
                    id=session_id,
                    tenant_id=uuid4(),
                    workspace_id=uuid4(),
                    owner_user_id=uuid4(),
                    title="983 升级前会话",
                    created_at=moment,
                    updated_at=moment,
                )
            )
            tx.flush()
            tx.add(
                AgentRun(
                    id=run_id,
                    session_id=session_id,
                    request_id=uuid4(),
                    ordinal=1,
                    status="completed",
                    config_snapshot={"legacy": "preserve-0018"},
                    public_view={"run": {"status": "completed"}},
                    created_at=moment,
                    started_at=moment,
                    finished_at=moment,
                )
            )
    finally:
        engine.dispose()
    return session_id, run_id


def run_row(runtime, run_id):
    with psycopg.connect(db.dsn(runtime)) as tx:
        return tx.execute("SELECT to_jsonb(r) FROM agent_run r WHERE id=%s", (run_id,)).fetchone()


@pytest.mark.parametrize("mode", ["native", "sql"])
def test_0018_upgrade_preserves_run_and_enforces_goal_contract(targets, mode):
    runtime, migration = targets("l25_upgrade_" + mode)
    assert db.migrate(target="0018") == "applied"
    session_id, run_id = legacy_run(runtime)
    before = run_row(runtime, run_id)
    assert db.migrate(mode=mode, start="0018", target="0019") == "applied"
    assert db.migrate(mode=mode, target="0019") == "already_applied"
    assert run_row(runtime, run_id) == before

    goal_id, moment = uuid4(), datetime.now(UTC)
    private = PrivateState(Fernet.generate_key().decode())
    payload = {"title": "销售阶段已核对", "sourceRefs": [], "value": "120"}
    sealed = private.seal(goal_id, "ledger", payload)
    engine = create_engine(runtime)
    try:
        with Session(engine) as tx, tx.begin():
            assert tx.query(AgentGoal).count() == 0
            assert tx.query(AgentGoalEntry).count() == 0
            tx.add(
                AgentGoal(
                    id=goal_id,
                    session_id=session_id,
                    contract={
                        "objective": "月度分析",
                        "acceptance": [{"id": "a1", "text": "销售有来源"}],
                        "constraints": ["只使用测试数据"],
                    },
                    budget={"maxRounds": 3, "tokens": 50000, "costMicrousd": 100000},
                    created_at=moment,
                    updated_at=moment,
                )
            )
            tx.flush()
            tx.add(
                AgentGoalEntry(
                    goal_id=goal_id,
                    entry_key="conclusion:sales",
                    kind="conclusion",
                    status="done",
                    private_body=sealed,
                    updated_at=moment,
                )
            )
    finally:
        engine.dispose()

    with psycopg.connect(db.dsn(runtime)) as tx:
        assert tx.execute(
            "SELECT has_table_privilege('agent_goal','SELECT'),"
            "has_table_privilege('agent_goal','INSERT'),"
            "has_table_privilege('agent_goal','UPDATE'),"
            "has_table_privilege('agent_goal','DELETE'),"
            "has_table_privilege('agent_goal_entry','DELETE')"
        ).fetchone() == (True, True, True, False, False)
        tx.execute("UPDATE agent_goal SET revision=2 WHERE id=%s", (goal_id,))
        tx.execute("UPDATE agent_goal_entry SET status='conflict' WHERE goal_id=%s", (goal_id,))
        row = tx.execute(
            "SELECT private_body FROM agent_goal_entry WHERE goal_id=%s", (goal_id,)
        ).fetchone()
        assert row is not None
        content = row[0]
        assert content == sealed and private.open(goal_id, "ledger", content) == payload
        for table in ("agent_goal", "agent_goal_entry"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), tx.transaction():
                tx.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
        with pytest.raises(psycopg.errors.CheckViolation), tx.transaction():
            tx.execute("UPDATE agent_goal SET tokens_used=-1 WHERE id=%s", (goal_id,))
        with pytest.raises(psycopg.errors.CheckViolation), tx.transaction():
            tx.execute("UPDATE agent_goal SET status='unknown' WHERE id=%s", (goal_id,))
        with pytest.raises(psycopg.errors.CheckViolation), tx.transaction():
            tx.execute("UPDATE agent_goal_entry SET kind='unknown' WHERE goal_id=%s", (goal_id,))
        with pytest.raises(psycopg.errors.UniqueViolation), tx.transaction():
            tx.execute(
                "INSERT INTO agent_goal SELECT %s, session_id, revision, status, reason_code,"
                "contract,budget,runtime_state,rounds_started,tokens_used,cost_used_microusd,"
                "ledger_revision,last_settled_ordinal,retry_after,created_at,updated_at,ended_at "
                "FROM agent_goal WHERE id=%s",
                (uuid4(), goal_id),
            )
    with psycopg.connect(db.dsn(migration)) as tx:
        assert tx.execute("SELECT version_num FROM alembic_version").fetchone() == ("0019",)


def test_upgrade_failure_keeps_0018_and_verified_backup_can_restore(targets):
    runtime, migration = targets("l25_backup")
    assert db.migrate(target="0018") == "applied"
    _, run_id = legacy_run(runtime)
    before = run_row(runtime, run_id)
    baseline = schema(migration, restored=True)
    backup = pg_tool("pg_dump", migration)
    assert len(backup) > 1000
    with pytest.raises(db.DatabaseError, match="starting"):
        db.migrate(start="0017", target="0019")
    assert run_row(runtime, run_id) == before
    with psycopg.connect(db.dsn(runtime)) as tx:
        assert tx.execute("SELECT version_num FROM alembic_version").fetchone() == ("0018",)
    assert db.migrate(start="0018", target="0019") == "applied"
    restored_runtime, restored_migration = targets("l25_restore0018")
    pg_tool("pg_restore", restored_migration, backup)
    assert db.migrate(target="0018") == "already_applied"
    assert schema(restored_migration, restored=True) == baseline
    assert run_row(restored_runtime, run_id) == before
    with psycopg.connect(db.dsn(restored_runtime)) as tx:
        assert tx.execute("SELECT to_regclass('agent_goal')").fetchone() == (None,)
    with psycopg.connect(db.dsn(runtime)) as tx:
        assert tx.execute("SELECT version_num FROM alembic_version").fetchone() == ("0019",)
