"""Managed Agent/main migrations and deterministic SQL delivery (Feature 993)."""

import argparse
import hashlib
import io
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import alembic
import psycopg
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from compass_agent.context_ops.entities import CONTEXTOPS_TABLES
from compass_agent.data_connections.entities import DataConnection, RetailConnection
from compass_agent.models import entities
from compass_agent.promptops.entities import PROMPT_TABLES
from compass_agent.runtime.entities import RUNTIME_TABLES, SCHEDULER_TABLES
from compass_agent.scheduler.entities import AgentSchedule, AgentScheduleTrigger, RuntimeControl
from compass_agent.semantic import entities as semantic_entities
from compass_agent.tools.mcp.entities import McpConnection
from compass_agent.tools.sandbox.entities import SandboxAllocation
from compass_agent.workflows.assets import UNIQUE_SCRIPT, WorkflowAsset
from psycopg import sql
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    Uuid,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

APP = Path(__file__).resolve().parents[1]
SQL_ROOT = APP / "db/main/sql"
REVISIONS = {
    "0001": "0001_demo_fixture.py",
    "0002": "0002_llm_runtime.py",
    "0003": "0003_systemone_decision.py",
    "0004": "0004_reliable_execution.py",
    "0005": "0005_retail_connection.py",
    "0006": "0006_data_connections.py",
    "0007": "0007_workflow_assets.py",
    "0008": "0008_tool_runtime.py",
    "0009": "0009_durable_runs.py",
    "0010": "0010_run_scheduler.py",
    "0011": "0011_session_management.py",
    "0012": "0012_dynamic_workflow.py",
    "0013": "0013_model_generation_profile.py",
    "0014": "0014_workflow_asset_deletion.py",
    "0015": "0015_scheduled_tasks.py",
    "0016": "0016_prompt_context.py",
    "0017": "0017_semantic_runtime.py",
    "0018": "0018_semantic_cleanup.py",
    "0019": "0019_goal_ledger.py",
    "0020": "0020_context_ops.py",
}
TARGET = "0020"
GOAL_TABLES = {"agent_goal", "agent_goal_entry"}
SEMANTIC_TABLES = {
    semantic_entities.SemanticMetricOffer.__tablename__,
}
MARKER = "compass:agent/main"
OWNER = "compass_agent_owner"
MIGRATOR = "compass_agent_migrator"
RUNTIME = "compass_agent_runtime"
SCHEDULER = "compass_agent_scheduler"
PLAN_TABLES = {
    RuntimeControl.__tablename__,
    AgentSchedule.__tablename__,
    AgentScheduleTrigger.__tablename__,
}
SUMMARY_VIEW = "agent_scheduled_run_summary"
LOCK = 993001


class DatabaseError(Exception):
    """Safe, operator-facing errors only; database exceptions are never echoed."""


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sources(target: str = TARGET) -> dict[str, str]:
    return {
        revision: digest((APP / "migrations/versions" / name).read_bytes())
        for revision, name in REVISIONS.items()
        if revision <= target
    }


def ledger_comment(target: str) -> str:
    return MARKER + ";sources=" + json.dumps(sources(target), sort_keys=True)


def connection_url(name: str) -> str:
    value = os.environ.get(name, "")
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgresql", "postgres", "postgresql+psycopg"}:
        raise DatabaseError(f"Explicit PostgreSQL {name} required")
    if name != "AGENT_PROVISION_DATABASE_URL" and not re.fullmatch(
        r"/(?:compass|compass_agent(?:_[a-z0-9_]+)?)", parsed.path
    ):
        raise DatabaseError("Refusing a non-Agent database target")
    return value


def dsn(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def config(connection=None) -> Config:
    result = Config(str(APP / "alembic.ini"))
    result.set_main_option("path_separator", "os")
    result.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline/offline")
    if connection is not None:
        result.attributes["connection"] = connection
    return result


def inspect_version(connection, *, legacy: bool = False) -> str:
    unexpected = connection.execute(
        text("""
        SELECT nspname FROM pg_namespace
        WHERE nspname NOT IN ('public','information_schema') AND nspname NOT LIKE 'pg_%'
        UNION ALL SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind NOT IN ('r','i','p','I')
        AND NOT (c.relkind='v' AND c.relname='agent_scheduled_run_summary')
        AND NOT (c.relkind='S' AND c.relname='prompt_generation_id_seq')
        UNION ALL SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public'
    """)
    ).all()
    if unexpected:
        raise DatabaseError("Unknown database objects; refusing migration")
    tables = set(
        connection.execute(
            text("""
        SELECT tablename FROM pg_tables WHERE schemaname='public'
    """)
        ).scalars()
    )
    if not tables:
        return "empty"
    if "alembic_version" not in tables:
        raise DatabaseError("Missing Agent version ledger")
    revisions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(revisions) != 1 or revisions[0] not in REVISIONS:
        raise DatabaseError("Unknown Agent revision")
    revision = revisions[0]
    sequences = set(inspect(connection).get_sequence_names(schema="public"))
    if sequences != ({"prompt_generation_id_seq"} if revision >= "0016" else set()):
        raise DatabaseError("Unexpected Agent identity sequence at current revision")
    expected = {"alembic_version", "demo_daily_sales"}
    if revision >= "0002":
        expected |= {"model_provider", "model_change", "model_call"}
    if revision == "0005":
        expected.add(RetailConnection.__tablename__)
    if revision >= "0006":
        expected.add(DataConnection.__tablename__)
    if revision >= "0007":
        expected.add("workflow_asset")
    if revision >= "0008":
        expected |= {"mcp_connection", "sandbox_allocation"}
    if revision >= "0009":
        expected |= RUNTIME_TABLES
    if revision >= "0010":
        expected |= SCHEDULER_TABLES
    if revision >= "0015":
        expected |= PLAN_TABLES
    if revision == "0017":
        expected.add("semantic_run_binding")
    if revision >= "0017":
        expected |= SEMANTIC_TABLES
    if revision >= "0016":
        expected |= PROMPT_TABLES
    if revision >= "0019":
        expected |= GOAL_TABLES
    if revision >= "0020":
        expected |= CONTEXTOPS_TABLES
    views = inspect(connection).get_view_names(schema="public")
    if views != ([SUMMARY_VIEW] if revision >= "0015" else []):
        raise DatabaseError("Agent view drift at current revision")
    if tables != expected:
        raise DatabaseError("Unknown database tables; refusing migration")
    actual = connection.execute(
        text("SELECT obj_description('public.alembic_version'::regclass,'pg_class')")
    ).scalar_one()
    if actual != ledger_comment(revision):
        if not legacy or revision != "0001" or actual is not None:
            raise DatabaseError("Agent migration source checksum mismatch")
    if revision == "0001":
        columns = connection.execute(
            text("""
            SELECT column_name,udt_name,is_nullable FROM information_schema.columns
            WHERE table_schema='public' AND table_name='demo_daily_sales'
            ORDER BY ordinal_position
        """)
        ).all()
        if [tuple(row) for row in columns] != [
            ("day", "date", "NO"),
            ("revenue_minor", "int4", "NO"),
            ("orders", "int4", "NO"),
        ]:
            raise DatabaseError("Legacy Agent schema does not match revision 0001")
        checks = connection.execute(
            text("""
            SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conrelid='public.demo_daily_sales'::regclass ORDER BY conname
        """)
        ).all()
        if [tuple(row) for row in checks] != [
            ("ck_demo_orders_nonnegative", "CHECK ((orders >= 0))"),
            ("ck_demo_revenue_nonnegative", "CHECK ((revenue_minor >= 0))"),
            ("demo_daily_sales_pkey", "PRIMARY KEY (day)"),
        ]:
            raise DatabaseError("Legacy Agent constraints do not match revision 0001")
    elif revision >= "0002":
        # 0002 and 0003 share columns, indexes and keys. Their adapter CHECK differs;
        # verify its version-specific definition separately below.
        metadata = entities.Base.metadata
        if revision < TARGET:
            metadata = MetaData()
            for table in entities.Base.metadata.sorted_tables:
                if revision < "0020" and table.name in CONTEXTOPS_TABLES:
                    continue
                if (revision < "0010" and table.name in SCHEDULER_TABLES) or (
                    revision < "0009" and table.name in RUNTIME_TABLES
                ):
                    continue
                if revision < "0008" and table.name in {
                    McpConnection.__tablename__,
                    SandboxAllocation.__tablename__,
                }:
                    continue
                if (revision < "0007" and table.name == WorkflowAsset.__tablename__) or (
                    revision < "0006" and table.name == DataConnection.__tablename__
                ):
                    continue
                if revision < "0015" and table.name in PLAN_TABLES:
                    continue
                if revision < "0017" and table.name in SEMANTIC_TABLES:
                    continue
                if revision < "0019" and table.name in GOAL_TABLES:
                    continue
                if revision < "0016" and table.name in PROMPT_TABLES:
                    continue
                copied = table.to_metadata(metadata)
                if revision < "0016":
                    if table.name == "agent_step":
                        copied._columns.remove(copied.c.prepared_context)
                    if table.name == "agent_message":
                        copied._columns.remove(copied.c.source_refs)
                if revision < "0015":
                    if table.name == "agent_run":
                        for name in (
                            "publish_token",
                            "publish_expires_at",
                            "publish_generation",
                            "publish_initialized",
                        ):
                            copied._columns.remove(copied.c[name])
                    if table.name == "agent_run_job":
                        for name in ("recovery_token", "recovery_expires_at", "active_since"):
                            copied._columns.remove(copied.c[name])
                    if table.name == "agent_command":
                        copied.c.session_id.nullable = False
                        for constraint in list(copied.constraints):
                            if constraint.name == "ck_agent_command_schedule_scope":
                                copied.constraints.remove(constraint)
                if table.name == "model_provider" and revision < "0013":
                    copied._columns.remove(copied.c.generation_profile)
                if table.name == "workflow_asset" and revision < "0014":
                    index = next(i for i in copied.indexes if i.name == UNIQUE_SCRIPT)
                    copied.append_constraint(UniqueConstraint(*index.columns, name=UNIQUE_SCRIPT))
                    copied.indexes.remove(index)
                    copied._columns.remove(copied.c.deleted_at)
                if table.name == "workflow_asset" and revision < "0012":
                    for constraint in list(copied.constraints):
                        if constraint.name == "ck_workflow_asset_tool_definition":
                            copied.constraints.remove(constraint)
                    for name in ("description", "parameters"):
                        copied._columns.remove(copied.c[name])
                if table.name == "agent_session" and revision < "0011":
                    for constraint in list(copied.constraints):
                        if constraint.name in {
                            "ck_agent_session_title_status",
                            "ck_agent_session_title_job",
                        }:
                            copied.constraints.remove(constraint)
                    for index in list(copied.indexes):
                        if index.name == "ix_agent_session_title_ready":
                            copied.indexes.remove(index)
                    for name in ("title_status", "title_attempts", "title_next_at", "deleted_at"):
                        copied._columns.remove(copied.c[name])
                if table.name == "model_call" and revision < "0009":
                    for constraint in list(copied.constraints):
                        if constraint.name in {"fk_model_call_agent_attempt", "ck_model_call_run"}:
                            copied.constraints.remove(constraint)
                            for foreign_key in getattr(constraint, "elements", ()):
                                copied.foreign_keys.discard(foreign_key)
                                foreign_key.parent.foreign_keys.discard(foreign_key)
                    for index in list(copied.indexes):
                        if index.name == "ix_model_call_run":
                            copied.indexes.remove(index)
                    for name in (
                        "run_id",
                        "agent_attempt_id",
                        "input_message_refs",
                        "request_content",
                        "output_content",
                        "content_status",
                    ):
                        copied._columns.remove(copied.c[name])
                if table.name == "model_call" and revision < "0004":
                    copied._columns.remove(copied.c.execution)
            if revision == "0017":
                # Validate the retired table only when checking the upgrade's starting schema.
                Table(
                    "semantic_run_binding",
                    metadata,
                    Column("run_id", Uuid, ForeignKey("agent_run.id"), primary_key=True),
                    Column("tenant_id", Uuid, nullable=False),
                    Column("workspace_id", Uuid, nullable=False),
                    Column("catalog_kind", String(20), nullable=False),
                    Column("manifest_hash", String(64), nullable=False),
                    Column("partition_releases", JSONB, nullable=False),
                    Column("bound_at", DateTime(timezone=True), nullable=False),
                )
            if revision == "0005":
                RetailConnection.__table__.to_metadata(metadata)
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        if compare_metadata(context, metadata):
            raise DatabaseError("Agent schema drift at current revision")
        expected_keys = {
            table.name: [column.name for column in table.primary_key.columns]
            for table in metadata.tables.values()
        }
        expected_keys["alembic_version"] = ["version_num"]
        inspector = inspect(connection)
        for table_name, columns in expected_keys.items():
            if inspector.get_pk_constraint(table_name).get("constrained_columns") != columns:
                raise DatabaseError("Agent primary key drift at current revision")
        expected_checks = sorted(
            (table.name, str(constraint.name), True)
            for table in metadata.tables.values()
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        )
        actual_checks = connection.execute(
            text("""
            SELECT c.relname,t.conname,t.convalidated FROM pg_constraint t
            JOIN pg_class c ON c.oid=t.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND t.contype='c' ORDER BY 1,2
            """)
        ).all()
        if [tuple(row) for row in actual_checks] != expected_checks:
            raise DatabaseError("Agent check constraints drift at current revision")
        adapter_check = next(
            item["sqltext"]
            for item in inspector.get_check_constraints("model_provider")
            if item["name"] == "ck_model_provider_adapter"
        )
        expected_adapter = "aliyun_decision" if revision == "0002" else "systemone_decision"
        # PostgreSQL renders IN as = ANY(ARRAY[...]); literals are stable across both.
        if re.findall(r"'([^']*)'", adapter_check) != ["litellm_chat", expected_adapter]:
            raise DatabaseError("Agent adapter constraint drift at registered revision")
    return revision


def role_grants(target: str) -> str:
    grants = f"""REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON demo_daily_sales TO {RUNTIME};
GRANT SELECT ON alembic_version TO {RUNTIME};
GRANT SELECT, INSERT, UPDATE ON model_provider, model_call TO {RUNTIME};
GRANT SELECT, INSERT ON model_change TO {RUNTIME};"""
    if target >= "0005":
        table = "data_connection" if target >= "0006" else "retail_connection"
        grants += f"\nGRANT SELECT, INSERT, UPDATE ON {table} TO {RUNTIME};"
    if target >= "0007":
        deletion_column = ", deleted_at" if target >= "0014" else ""
        grants += (
            f"\nGRANT SELECT, INSERT ON workflow_asset TO {RUNTIME};"
            f"\nGRANT UPDATE (name, status, metadata_revision, updated_at{deletion_column}) "
            f"ON workflow_asset TO {RUNTIME};"
        )
    if target >= "0008":
        grants += f"\nGRANT SELECT, INSERT, UPDATE, DELETE ON mcp_connection TO {RUNTIME};"
        grants += f"\nGRANT SELECT, INSERT, UPDATE ON sandbox_allocation TO {RUNTIME};"
    if target >= "0009":
        grants += (
            "\nGRANT SELECT, INSERT, UPDATE ON "
            + ", ".join(sorted(RUNTIME_TABLES))
            + f" TO {RUNTIME};"
        )
    if target >= "0010":
        grants += (
            "\nGRANT SELECT, INSERT, UPDATE ON "
            + ", ".join(sorted(SCHEDULER_TABLES))
            + f" TO {RUNTIME};"
        )
    if target >= "0015":
        grants += f"""
GRANT SELECT, INSERT, UPDATE ON agent_schedule, agent_schedule_trigger TO {RUNTIME};
GRANT SELECT, UPDATE (policy_sha256, holder_id) ON agent_runtime_control TO {RUNTIME};
GRANT SELECT ON alembic_version, agent_runtime_control, agent_schedule,
    agent_schedule_trigger, agent_scheduled_run_summary TO {SCHEDULER};
GRANT UPDATE (holder_id, generation, expires_at) ON agent_runtime_control TO {SCHEDULER};
GRANT UPDATE (next_fire_at) ON agent_schedule TO {SCHEDULER};
GRANT INSERT (id, schedule_id, schedule_revision, scheduled_at, detected_at, coalesced_from_at,
    input_ciphertext, delivery_status, reason_code, delivery_deadline_at, next_attempt_at),
    UPDATE (delivery_status, reason_code, delivery_attempts, next_attempt_at, input_ciphertext,
    delivery_token, delivery_generation, delivery_expires_at, observed_run_status,
    observed_run_version, observed_reason_code, observed_at, finished_at)
    ON agent_schedule_trigger TO {SCHEDULER};"""
    if target >= "0016":
        grants += f"""
GRANT SELECT, INSERT ON prompt_file, prompt_version, prompt_generation, prompt_command TO {RUNTIME};
GRANT UPDATE (origin,edit_rev,max_version,active_epoch,draft_body,draft_base_version,
    draft_base_epoch,last_import_body,last_import_hash,updated_by,updated_at)
    ON prompt_file TO {RUNTIME};
GRANT SELECT, UPDATE (generation,last_snapshot_no) ON prompt_head TO {RUNTIME};
GRANT USAGE ON SEQUENCE prompt_generation_id_seq TO {RUNTIME};"""
    if target >= "0017":
        tables = (
            "semantic_run_binding, semantic_metric_offer"
            if target == "0017"
            else "semantic_metric_offer"
        )
        grants += f"\nGRANT SELECT, INSERT ON {tables} TO {RUNTIME};"
        grants += f"\nGRANT UPDATE(status,private_payload) ON semantic_metric_offer TO {RUNTIME};"
    if target >= "0019":
        grants += f"\nGRANT SELECT, INSERT, UPDATE ON agent_goal, agent_goal_entry TO {RUNTIME};"
    if target >= "0020":
        grants += (
            "\nGRANT SELECT, INSERT ON "
            + ", ".join(sorted(CONTEXTOPS_TABLES))
            + f" TO {RUNTIME};"
            + f"\nGRANT SELECT ON context_snapshot, context_badcase TO {SCHEDULER};"
            + f"\nGRANT DELETE ON context_snapshot TO {SCHEDULER};"
        )
    return grants


def sql_delivery(start: str, target: str = TARGET) -> str:
    buffer = io.StringIO()
    cfg = config()
    cfg.output_buffer = buffer
    command.upgrade(cfg, f"{'base' if start == 'empty' else start}:{target}", sql=True)
    generated = buffer.getvalue().replace("BEGIN;\n", "").replace("COMMIT;\n", "").strip()
    if target >= "0004":
        # Normalize new deliveries while preserving immutable historical SQL bytes.
        generated = "\n".join(line.rstrip() for line in generated.splitlines())
    comment = ledger_comment(target).replace("'", "''")
    feature = {
        "0002": "993",
        "0003": "993",
        "0004": "991",
        "0005": "982",
        "0006": "982",
        "0007": "987",
        "0008": "986",
        "0009": "985",
        "0010": "984",
        "0011": "981",
        "0012": "977",
        "0013": "976",
        "0014": "977",
        "0015": "984",
        "0016": "975",
        "0017": "974",
        "0018": "974",
        "0019": "983",
        "0020": "971",
    }[target]
    change = "transactional Retail copy" if target == "0006" else "additive schema"
    if target == "0018":
        change = "remove verified duplicate manifest rows"
    return (
        f"-- Generated from Agent Alembic migrations; do not edit.\n"
        f"-- owner=agent/main; feature={feature}; "
        f"PostgreSQL 17; from={start}; to={target}\n"
        "-- Apply through db:apply-sql after provision and ownership/version/checksum checks.\n"
        f"-- Single transaction; {change}; restore verified backup to a new owned database.\n"
        "BEGIN;\n" + generated + "\n" + role_grants(target) + "\n"
        f"COMMENT ON TABLE alembic_version IS '{comment}';\nCOMMIT;\n"
    )


def write_sql(*, check: bool = False) -> None:
    files = {
        "bootstrap.sql": sql_delivery("empty"),
        "upgrades/0001__0002.sql": sql_delivery("0001", "0002"),
        "upgrades/0001__0003.sql": sql_delivery("0001", "0003"),
        "upgrades/0002__0003.sql": sql_delivery("0002", "0003"),
        "upgrades/0001__0004.sql": sql_delivery("0001", "0004"),
        "upgrades/0002__0004.sql": sql_delivery("0002", "0004"),
        "upgrades/0003__0004.sql": sql_delivery("0003", "0004"),
    }
    for start in ("0001", "0002", "0003", "0004"):
        files[f"upgrades/{start}__0005.sql"] = sql_delivery(start, "0005")
    for start in ("0001", "0002", "0003", "0004", "0005"):
        files[f"upgrades/{start}__0006.sql"] = sql_delivery(start, "0006")
    for target in (
        "0007",
        "0008",
        "0010",
        "0011",
        "0012",
        "0013",
        "0014",
        "0015",
        "0017",
        "0018",
        TARGET,
    ):
        for start in REVISIONS:
            if start < target:
                files[f"upgrades/{start}__{target}.sql"] = sql_delivery(start, target)
    manifest = {
        "owner": "agent",
        "databaseKey": "main",
        "feature": "983",
        "from": "empty",
        "to": TARGET,
        "engine": "PostgreSQL 17",
        "tool": "Alembic " + alembic.__version__,
        "sourceRevision": "working-tree; fixed by source SHA-256",
        "sources": [
            {"source": "../../migrations/versions/" + REVISIONS[rev], "sha256": sha}
            for rev, sha in sources().items()
        ],
        "files": {name: digest(content.encode()) for name, content in files.items()},
        "generate": "pnpm db:sql -F agent",
        "apply": f"pnpm db:apply-sql -F agent -- --from empty --to {TARGET}",
        "transaction": "single transaction with advisory lock",
        "prerequisites": "Provisioned Agent-owned database; expected start and source checksums",
        "dataImpact": (
            "Removes duplicated registry bindings after checking the retained Run snapshots; "
            "adds model configuration and metadata; renames decision adapter; "
            "adds execution summaries and multi-engine workspace connections; "
            "adds durable runs, checkpoints, HITL and immutable artifacts; "
            "adds asynchronous session titles, session deletion metadata "
            "and workflow tool contracts and soft deletion; "
            "adds plans, triggers and replica execution and publication leases; "
            "backfills historical model attempt metadata without raw content; "
            "preserves existing Retail configuration, credentials and revisions"
        ),
        "recovery": "Restore verified backup to new owned database with original encryption key",
    }
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    for name, content in files.items():
        path = SQL_ROOT / name
        if check:
            if not path.exists() or path.read_text() != content:
                raise DatabaseError("SQL delivery drift; run pnpm db:sql -F agent")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)


def migrate(*, mode: str = "native", start: str | None = None, target: str = TARGET) -> str:
    if target not in REVISIONS or start not in {None, "empty", *REVISIONS}:
        raise DatabaseError("Unknown migration range")
    if mode == "sql":
        if target != TARGET:
            raise DatabaseError(f"SQL delivery targets {TARGET}")
        write_sql(check=True)
    url = connection_url("AGENT_MIGRATION_DATABASE_URL")
    engine = create_engine(dsn(url).replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with engine.begin() as connection:
            marker = connection.execute(
                text("""
                SELECT shobj_description(oid,'pg_database'),pg_get_userbyid(datdba)
                FROM pg_database WHERE datname=current_database()
            """)
            ).one()
            if tuple(marker) != (MARKER, OWNER):
                raise DatabaseError("Unregistered Agent database")
            connection.execute(text(f"SET ROLE {OWNER}"))
            connection.execute(text("SET LOCAL lock_timeout='5s'"))
            connection.execute(text("SET LOCAL statement_timeout='30s'"))
            connection.execute(text("SET LOCAL search_path=public,pg_catalog"))
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK})
            current = inspect_version(connection)
            if current == target:
                return "already_applied"
            if current > target or start is not None and current != start:
                if current != "empty":
                    raise DatabaseError("Unexpected starting revision")
                if start not in {None, "empty"}:
                    raise DatabaseError("Unexpected starting revision")
            if mode == "sql":
                name = (
                    "bootstrap.sql" if current == "empty" else f"upgrades/{current}__{target}.sql"
                )
                content = (SQL_ROOT / name).read_text()
                # The surrounding managed transaction owns BEGIN/COMMIT.
                content = content.replace("BEGIN;\n", "").replace("COMMIT;\n", "")
                connection.exec_driver_sql(content, execution_options={"no_parameters": True})
            else:
                command.upgrade(config(connection), target)
                grants = (
                    role_grants(target)
                    if target >= "0002"
                    else (
                        f"GRANT SELECT, INSERT, UPDATE ON demo_daily_sales TO {RUNTIME};"
                        f"GRANT SELECT ON alembic_version TO {RUNTIME};"
                    )
                )
                connection.exec_driver_sql(grants)
                connection.exec_driver_sql(
                    "COMMENT ON TABLE alembic_version IS '"
                    + ledger_comment(target).replace("'", "''")
                    + "'"
                )
            if inspect_version(connection) != target:
                raise DatabaseError("Migration did not reach expected target")
        return "applied"
    finally:
        engine.dispose()


def provision() -> str:
    admin = urlsplit(connection_url("AGENT_PROVISION_DATABASE_URL"))
    migration = urlsplit(connection_url("AGENT_MIGRATION_DATABASE_URL"))
    runtime = urlsplit(connection_url("DATABASE_URL"))
    scheduler = (
        urlsplit(connection_url("COMPASS_SCHEDULER_DATABASE_URL"))
        if os.environ.get("COMPASS_SCHEDULER_DATABASE_URL")
        else None
    )
    if scheduler and (
        scheduler.username != SCHEDULER
        or scheduler.path != runtime.path
        or (scheduler.hostname, scheduler.port) != (runtime.hostname, runtime.port)
        or not scheduler.password
    ):
        raise DatabaseError("Invalid dedicated Scheduler database target or credential")
    if admin.hostname not in {"localhost", "127.0.0.1", "::1", "postgres"}:
        raise DatabaseError("Provision is limited to local teaching deployments")
    if (
        migration.username != MIGRATOR
        or runtime.username != RUNTIME
        or migration.path != runtime.path
        or (admin.hostname, admin.port) != (migration.hostname, migration.port)
        or (admin.hostname, admin.port) != (runtime.hostname, runtime.port)
        or not migration.password
        or not runtime.password
    ):
        raise DatabaseError("Inconsistent Agent targets or role credentials")
    database = migration.path[1:]
    with psycopg.connect(dsn(admin.geturl()), autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (LOCK - 1,))
        existing = connection.execute(
            "SELECT shobj_description(oid,'pg_database'),pg_get_userbyid(datdba) "
            "FROM pg_database WHERE datname=%s",
            (database,),
        ).fetchone()
        legacy_revision = "empty"
        if existing:
            if existing[0] not in {None, MARKER} or existing[0] == MARKER and existing[1] != OWNER:
                raise DatabaseError("Refusing an unknown existing database")
            inspect_url = urlunsplit(admin._replace(path=migration.path))
            probe = create_engine(
                dsn(inspect_url).replace("postgresql://", "postgresql+psycopg://", 1)
            )
            try:
                with probe.connect() as target_connection:
                    legacy_revision = inspect_version(target_connection, legacy=existing[0] is None)
            finally:
                probe.dispose()
            if existing[0] is None and legacy_revision not in {"empty", "0001"}:
                raise DatabaseError("Only empty or original 0001 databases can be adopted")
        for name, password in [
            (OWNER, None),
            (MIGRATOR, migration.password),
            (RUNTIME, runtime.password),
            (SCHEDULER, scheduler.password if scheduler else None),
        ]:
            role = connection.execute(
                "SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls,rolreplication,"
                "shobj_description(oid,'pg_authid'),rolcanlogin FROM pg_roles WHERE rolname=%s",
                (name,),
            ).fetchone()
            if role:
                if any(role[:5]) or role[5] != MARKER:
                    raise DatabaseError("Refusing an unknown or privileged existing role")
                if name == SCHEDULER and password and not role[6]:
                    connection.execute(
                        sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                            sql.Identifier(SCHEDULER), sql.Literal(unquote(password))
                        )
                    )
            else:
                login = (
                    sql.SQL("NOLOGIN")
                    if password is None
                    else sql.SQL("LOGIN PASSWORD {}").format(sql.Literal(unquote(password)))
                )
                connection.execute(
                    sql.SQL(
                        "CREATE ROLE {} {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOBYPASSRLS"
                    ).format(sql.Identifier(name), login)
                )
                connection.execute(
                    sql.SQL("COMMENT ON ROLE {} IS {}").format(
                        sql.Identifier(name), sql.Literal(MARKER)
                    )
                )
        connection.execute(
            sql.SQL("GRANT {} TO {}").format(sql.Identifier(OWNER), sql.Identifier(MIGRATOR))
        )
        if not existing:
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(OWNER)
                )
            )
        else:
            connection.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                    sql.Identifier(database), sql.Identifier(OWNER)
                )
            )
        connection.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                sql.Identifier(database), sql.Literal(MARKER)
            )
        )
        connection.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {},{},{}").format(
                sql.Identifier(database),
                sql.Identifier(MIGRATOR),
                sql.Identifier(RUNTIME),
                sql.Identifier(SCHEDULER),
            )
        )
        target_url = urlunsplit(admin._replace(path=migration.path))
        with psycopg.connect(dsn(target_url)) as target_connection:
            target_connection.execute(
                sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(OWNER))
            )
            target_connection.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            target_connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {},{}").format(
                    sql.Identifier(RUNTIME), sql.Identifier(SCHEDULER)
                )
            )
            if legacy_revision != "empty":
                tables = target_connection.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public'"
                ).fetchall()
                for (table,) in tables:
                    target_connection.execute(
                        sql.SQL("ALTER TABLE {} OWNER TO {}").format(
                            sql.Identifier(table), sql.Identifier(OWNER)
                        )
                    )
                target_connection.execute(
                    sql.SQL("COMMENT ON TABLE alembic_version IS {}").format(
                        sql.Literal(ledger_comment(legacy_revision))
                    )
                )
    return "already_provisioned" if existing and existing[0] == MARKER else "provisioned"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["provision", "migrate", "sql", "apply-sql"])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="target", default=TARGET)
    args = parser.parse_args()
    try:
        if args.action == "provision":
            print(provision())
        elif args.action == "sql":
            write_sql(check=args.check)
            print("SQL delivery matches" if args.check else "SQL delivery generated")
        else:
            print(
                migrate(
                    mode="sql" if args.action == "apply-sql" else "native",
                    start=args.start,
                    target=args.target,
                )
            )
    except DatabaseError as error:
        parser.exit(1, str(error) + "\n")
    except Exception:
        parser.exit(
            1, "Agent database operation failed; check target, credentials and database state.\n"
        )


if __name__ == "__main__":
    main()
