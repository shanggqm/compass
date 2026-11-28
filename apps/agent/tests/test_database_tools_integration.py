"""PostgreSQL acceptance: native/SQL, 0001 upgrade, roles, drift and restore."""

import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import LiteralString, cast
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from compass_agent.data_connections.connections import DataConnections
from compass_agent.database import Base
from compass_agent.models.entities import Provider
from compass_agent.models.errors import ModelError
from compass_agent.models.secrets import SecretStore
from compass_agent.schemas import (
    DataConnectionSave,
    ProviderCreate,
    RetailConnectionSave,
    TenantScope,
    TrustedContext,
    WorkflowAssetPatch,
)
from compass_agent.settings import Settings
from compass_agent.workflows.assets import DYNAMIC_CONTRACT, WorkflowAsset, WorkflowAssets
from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

SPEC = importlib.util.spec_from_file_location(
    "agent_database_acceptance", Path(__file__).parents[1] / "scripts/database.py"
)
assert SPEC is not None and SPEC.loader is not None
db = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(db)


@pytest.fixture
def targets(monkeypatch):
    names = ["AGENT_TEST_ADMIN_URL", "AGENT_TEST_DATABASE_URL", "AGENT_TEST_MIGRATION_DATABASE_URL"]
    values = [os.environ.get(name, "") for name in names]
    if not all(values):
        pytest.fail("Explicit AGENT_TEST_* PostgreSQL credentials required")
    admin, runtime, migration = map(urlsplit, values)
    if not runtime.path.startswith("/compass_agent_test_") or runtime.path != migration.path:
        pytest.fail("Dedicated Agent test databases required")
    created = []

    def make(label):
        name = "compass_agent_test_" + uuid4().hex[:10] + "_" + label
        created.append(name)
        run = urlunsplit(runtime._replace(path="/" + name))
        mig = urlunsplit(migration._replace(path="/" + name))
        monkeypatch.setenv("AGENT_PROVISION_DATABASE_URL", admin.geturl())
        monkeypatch.setenv("AGENT_MIGRATION_DATABASE_URL", mig)
        monkeypatch.setenv("DATABASE_URL", run)
        monkeypatch.setenv("COMPASS_SCHEDULER_DATABASE_URL", "")
        db.provision()
        return run, mig

    yield make
    with psycopg.connect(db.dsn(admin.geturl()), autocommit=True) as connection:
        for name in created:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def schema(url, *, restored=False):
    with psycopg.connect(db.dsn(url)) as connection:
        snapshot = [
            connection.execute(sql.SQL(cast(LiteralString, query))).fetchall()
            for query in [
                "SELECT table_name,column_name,data_type,is_nullable,column_default "
                "FROM information_schema.columns WHERE table_schema='public' "
                "ORDER BY table_name,ordinal_position",
                "SELECT c.relname,t.conname,pg_get_constraintdef(t.oid) FROM pg_constraint t "
                "JOIN pg_class c ON c.oid=t.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public' ORDER BY 1,2",
                "SELECT tablename,indexname,indexdef FROM pg_indexes "
                "WHERE schemaname='public' ORDER BY 1,2",
                "SELECT table_name,grantee,privilege_type FROM information_schema.table_privileges "
                "WHERE table_schema='public' ORDER BY 1,2,3",
                "SELECT version_num FROM alembic_version",
            ]
        ]
        if restored:
            # pg_dump/reparse rewrites IN casts while preserving check semantics.
            # Compare identities/validation here and exercise CHECK behavior below.
            snapshot[1] = connection.execute(
                "SELECT c.relname,t.conname,t.contype,t.convalidated FROM pg_constraint t "
                "JOIN pg_class c ON c.oid=t.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public' ORDER BY 1,2"
            ).fetchall()
        return snapshot


def test_native_and_sql_upgrade_preserve_rows_and_runtime_permissions(targets, monkeypatch):
    db.write_sql(check=True)
    snapshots = []
    for mode in ["native", "sql"]:
        runtime, migration = targets("upgrade_" + mode)
        assert db.migrate(target="0001") == "applied"
        with psycopg.connect(db.dsn(runtime)) as connection:
            connection.execute("INSERT INTO demo_daily_sales VALUES ('2026-10-01',12345,7)")
        assert db.migrate(mode=mode, start="0001") == "applied"
        assert db.migrate(mode=mode) == "already_applied"
        with psycopg.connect(db.dsn(runtime)) as connection:
            assert connection.execute("SELECT revenue_minor FROM demo_daily_sales").fetchone() == (
                12345,
            )
            assert connection.execute("SELECT count(*) FROM model_provider").fetchone() == (0,)
            for action in [
                "CREATE TABLE forbidden_table(id int)",
                "DELETE FROM model_change",
                "UPDATE alembic_version SET version_num='0001'",
            ]:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with connection.transaction():
                        connection.execute(sql.SQL(cast(LiteralString, action)))
        snapshots.append(schema(migration))
    assert snapshots[0] == snapshots[1]
    for mode in ["native", "sql"]:
        _, migration = targets("empty_" + mode)
        assert db.migrate(mode=mode, start="empty") == "applied"
        assert schema(migration) == snapshots[0]
    _, migration = targets("wrong")
    with pytest.raises(db.DatabaseError, match="starting"):
        db.migrate(start="0001")
    with pytest.raises(db.DatabaseError, match="range"):
        db.migrate(target="9999")
    db.migrate()
    engine = create_engine(db.dsn(migration).replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with engine.begin() as connection:
            connection.execute(text("SET ROLE compass_agent_owner"))
            connection.execute(text("COMMENT ON TABLE alembic_version IS 'bad-checksum'"))
        with pytest.raises(db.DatabaseError, match="checksum"):
            db.migrate()
    finally:
        engine.dispose()


def test_legacy_0001_adoption_and_unknown_object_refusal(targets):
    runtime, migration = targets("legacy")
    db.migrate(target="0001")
    with psycopg.connect(db.dsn(migration)) as connection:
        connection.execute("SET ROLE compass_agent_owner")
        connection.execute("INSERT INTO demo_daily_sales VALUES ('2026-10-01',100,2)")
        connection.execute("COMMENT ON TABLE alembic_version IS NULL")
        connection.execute(
            sql.SQL("COMMENT ON DATABASE {} IS NULL").format(
                sql.Identifier(urlsplit(migration).path[1:])
            )
        )
    assert db.provision() == "provisioned"
    assert db.migrate(start="0001") == "applied"
    with psycopg.connect(db.dsn(runtime)) as connection:
        assert connection.execute("SELECT revenue_minor FROM demo_daily_sales").fetchone() == (100,)
    _, unknown = targets("unknown")
    with psycopg.connect(db.dsn(unknown)) as connection:
        connection.execute("SET ROLE compass_agent_owner")
        connection.execute("CREATE TABLE private_history (id int)")
    with pytest.raises(db.DatabaseError, match="ledger"):
        db.provision()
    with psycopg.connect(db.dsn(unknown)) as connection:
        assert connection.execute("SELECT to_regclass('private_history')").fetchone() == (
            "private_history",
        )


def test_migration_matches_runtime_metadata(targets):
    _, migration = targets("metadata")
    db.migrate()
    engine = create_engine(db.dsn(migration).replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    finally:
        engine.dispose()


def test_unknown_sequence_refused_before_adopting_database(targets):
    _, migration = targets("sequence")
    with psycopg.connect(db.dsn(migration)) as connection:
        connection.execute("SET ROLE compass_agent_owner")
        connection.execute("CREATE SEQUENCE unrelated_sequence START 42")
        connection.execute(
            sql.SQL("COMMENT ON DATABASE {} IS NULL").format(
                sql.Identifier(urlsplit(migration).path[1:])
            )
        )
        before = connection.execute(
            "SELECT pg_get_userbyid(datdba),datacl,shobj_description(oid,'pg_database') "
            "FROM pg_database WHERE datname=current_database()"
        ).fetchone()
    with pytest.raises(db.DatabaseError, match="Unknown database objects"):
        db.provision()
    with psycopg.connect(db.dsn(migration)) as connection:
        assert (
            connection.execute(
                "SELECT pg_get_userbyid(datdba),datacl,shobj_description(oid,'pg_database') "
                "FROM pg_database WHERE datname=current_database()"
            ).fetchone()
            == before
        )
        assert connection.execute("SELECT last_value FROM unrelated_sequence").fetchone() == (42,)


@pytest.mark.parametrize(
    "mutation",
    [
        "ALTER TABLE model_provider DROP COLUMN api_key_ciphertext",
        "ALTER TABLE model_provider ALTER COLUMN api_key_ciphertext TYPE integer USING 0",
        "ALTER TABLE model_provider DROP CONSTRAINT ck_model_provider_chat_kind",
        "ALTER TABLE model_call DROP CONSTRAINT model_call_pkey CASCADE",
        "ALTER TABLE model_provider DROP CONSTRAINT ck_model_provider_adapter; "
        "ALTER TABLE model_provider ADD CONSTRAINT ck_model_provider_adapter "
        "CHECK (adapter IN ('litellm_chat','aliyun_decision'))",
    ],
)
def test_rejects_current_schema_drift_before_reporting_noop(targets, mutation):
    _, migration = targets("drift")
    db.migrate()
    with psycopg.connect(db.dsn(migration)) as connection:
        connection.execute("SET ROLE compass_agent_owner")
        connection.execute(sql.SQL(cast(LiteralString, mutation)))
    with pytest.raises(db.DatabaseError, match="drift"):
        db.migrate()


def pg_tool(tool, url, data=None):
    parsed = urlsplit(db.dsn(url))
    container = os.environ.get("AGENT_TEST_PG_CONTAINER")
    settings = {
        "PGHOST": "127.0.0.1" if container else (parsed.hostname or "localhost"),
        "PGPORT": "5432" if container else str(parsed.port or 5432),
        "PGUSER": unquote(parsed.username or ""),
        "PGPASSWORD": unquote(parsed.password or ""),
        "PGDATABASE": parsed.path[1:],
    }
    args = (
        ["--format=custom"]
        if tool == "pg_dump"
        else [
            "--exit-on-error",
            "--no-owner",
            "--role=compass_agent_owner",
            "--dbname=" + parsed.path[1:],
        ]
    )
    if container:
        command = ["docker", "exec", "-i"]
        for key in settings:
            command += ["-e", key]
        command += [container, tool, *args]
    else:
        command = [tool, *args]
    result = subprocess.run(
        command, input=data, capture_output=True, env={**os.environ, **settings}
    )
    assert result.returncode == 0, "PostgreSQL backup/restore tool failed"
    return result.stdout


@pytest.mark.parametrize("mode", ["native", "sql"])
def test_0004_retail_upgrade_and_idempotent_default(targets, mode):
    runtime, _ = targets("retail_" + mode)
    assert db.migrate(target="0004") == "applied"
    with psycopg.connect(db.dsn(runtime)) as connection:
        connection.execute("INSERT INTO demo_daily_sales VALUES ('2026-10-01',12345,7)")
    assert db.migrate(mode=mode, start="0004") == "applied"
    scope = TenantScope(kind="tenant", tenant_id=uuid4(), workspace_id=uuid4())
    key = Fernet.generate_key().decode()
    settings = Settings(
        compass_model_encryption_key=key,
        compass_retail_run_scope=json.dumps(
            {
                "tenantId": str(scope.tenant_id),
                "workspaceId": str(scope.workspace_id),
                "release": "compatible-retail-release",
                "contentHash": "a" * 64,
                "role": "fixture_a_east",
                "retailTenantId": 987,
                "schema": "core",
            }
        ),
        compass_retail_run_readonly_dsn=SecretStr(
            "postgresql://fixture_a_east:dummy@127.0.0.1:55432/retail"
        ),
    )
    engine = create_engine(runtime)
    try:
        service = DataConnections(engine, settings)
        assert service.status(scope).connection is None
        assert service.seed_default(settings)
        initial = service.status(scope).connection
        assert initial is not None and initial.revision == 1 and initial.retail_tenant_id == 987
        assert "dummy" not in initial.model_dump_json()
        edited = RetailConnectionSave.model_validate(
            {**initial.model_dump(exclude={"password_configured"}), "retail_tenant_id": 654}
        )
        assert service.save(scope, edited).revision == 2
        with pytest.raises(ModelError, match="version_conflict"):
            service.save(scope, edited)
        with pytest.raises(ModelError, match="validation_failed"):
            service.save(scope, edited.model_copy(update={"revision": 2, "database_name": "other"}))
        assert not service.seed_default(settings)
        saved = service.status(scope).connection
        assert saved is not None and saved.retail_tenant_id == 654
        assert service.status(scope.model_copy(update={"tenant_id": uuid4()})).connection is None
        assert service.status(scope.model_copy(update={"workspace_id": uuid4()})).connection is None
        with psycopg.connect(db.dsn(runtime)) as connection:
            assert connection.execute("SELECT revenue_minor FROM demo_daily_sales").fetchone() == (
                12345,
            )
            secret = connection.execute(
                "SELECT password_ciphertext FROM data_connection"
            ).fetchone()
            assert secret is not None and secret[0] != "dummy"
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("DELETE FROM data_connection")
    finally:
        engine.dispose()


@pytest.mark.parametrize("mode", ["native", "sql"])
def test_0005_default_migrates_and_coexists_with_other_engines(targets, mode):
    runtime, _ = targets("data_connections_" + mode)
    assert db.migrate(target="0005") == "applied"
    scope = TenantScope(kind="tenant", tenant_id=uuid4(), workspace_id=uuid4())
    key = Fernet.generate_key().decode()
    cipher = SecretStore(key).seal(
        f"retail_connection:{scope.tenant_id}:{scope.workspace_id}", {"password": "retail-fixture"}
    )
    with psycopg.connect(db.dsn(runtime)) as connection:
        connection.execute(
            """
            INSERT INTO retail_connection (
                workspace_id, tenant_id, host, port, database_name, role, sslmode,
                retail_tenant_id, password_ciphertext, revision
            ) VALUES (%s, %s, '127.0.0.1', 55432, 'retail', 'fixture_a_east', 'prefer',
                      101, %s, 7)
            """,
            (scope.workspace_id, scope.tenant_id, cipher),
        )
    assert db.migrate(mode=mode, start="0005") == "applied"
    engine = create_engine(runtime)
    try:
        service = DataConnections(engine, Settings(compass_model_encryption_key=key))
        listing = service.list(scope)
        assert len(listing.items) == 1 and listing.items[0].revision == 7
        assert listing.items[0].id == scope.workspace_id
        binding = service.binding_for_run(scope)
        assert binding and conninfo_to_dict(binding.dsn.get_secret_value())["password"] == (
            "retail-fixture"
        )
        values = {
            "name": "Business MySQL",
            "engine": "mysql",
            "host": "127.0.0.1",
            "port": 3306,
            "database_name": "business",
            "role": "reader",
            "sslmode": "disable",
            "revision": 0,
            "password": "mysql-fixture",
        }
        command = DataConnectionSave.model_validate(values)
        mysql = service.save_connection(scope, command, None)
        pg = service.save_connection(
            scope,
            DataConnectionSave.model_validate(
                {**values, "name": "Warehouse", "engine": "postgresql", "port": 5432}
            ),
            None,
        )
        assert {item.id for item in service.list(scope).items} == {
            scope.workspace_id,
            mysql.id,
            pg.id,
        }
        assert "mysql-fixture" not in service.list(scope).model_dump_json()
        change = command.model_copy(update={"name": "Updated", "revision": 1, "password": None})
        assert service.save_connection(scope, change, mysql.id).revision == 2
        with pytest.raises(ModelError, match="version_conflict"):
            service.save_connection(scope, change, mysql.id)
        other = scope.model_copy(update={"workspace_id": uuid4()})
        with pytest.raises(ModelError, match="not_found"):
            service.test_connection_target(other, mysql.id)
        assert service.list(other).items == []
        assert service.test_connection_target(scope, pg.id)[0].revision == 1
        assert service.test_connection_target(scope, scope.workspace_id)[0].revision == 7
    finally:
        engine.dispose()


def test_restores_data_and_encrypted_values_into_new_owned_database(targets):
    from compass_agent.promptops.service import bootstrap

    runtime, source = targets("backup")
    db.migrate()
    provider_id = uuid4()
    key = Fernet.generate_key().decode()
    secrets = SecretStore(key)
    retail_settings = Settings(compass_model_encryption_key=key)
    retail_scope = TenantScope(kind="tenant", tenant_id=uuid4(), workspace_id=uuid4())
    now = datetime.now(UTC)
    engine = create_engine(db.dsn(runtime).replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with Session(engine) as tx, tx.begin():
            baseline = bootstrap(tx)
        DataConnections(engine, retail_settings).save(
            retail_scope,
            RetailConnectionSave.model_validate(
                {
                    "host": "127.0.0.1",
                    "port": 55432,
                    "database_name": "retail",
                    "role": "fixture_a_east",
                    "sslmode": "prefer",
                    "retail_tenant_id": 101,
                    "revision": 0,
                    "password": "fixture-retail-password",
                }
            ),
        )
        with Session(engine) as session:
            session.add(
                Provider(
                    id=provider_id,
                    catalog_id="deepseek/deepseek-v4-pro",
                    vendor="deepseek",
                    api_model="deepseek-v4-pro",
                    sdk_model="deepseek/deepseek-v4-pro",
                    alias="restore fixture",
                    generation_profile={"defaults": {"max_tokens": 1024}},
                    kind="llm",
                    adapter="litellm_chat",
                    base_url="https://api.deepseek.com",
                    api_key_ciphertext=secrets.seal_key(provider_id, "fixture-restore-key"),
                    enabled=False,
                    chat_visible=False,
                    version=1,
                    binding_version=1,
                    created_at=now,
                    updated_at=now,
                    updated_by=uuid4(),
                )
            )
            session.commit()
    finally:
        engine.dispose()
    with psycopg.connect(db.dsn(source)) as connection:
        connection.execute("SET ROLE compass_agent_owner")
        connection.execute("INSERT INTO demo_daily_sales VALUES ('2026-10-01',9999,9)")
    backup = pg_tool("pg_dump", source)
    assert len(backup) > 1000
    restored_runtime, destination = targets("restore")
    pg_tool("pg_restore", destination, backup)
    assert schema(source, restored=True) == schema(destination, restored=True)
    with psycopg.connect(db.dsn(destination)) as restored_db:
        published = restored_db.execute(
            "SELECT h.generation, g.snapshot_no, v.body FROM prompt_head h "
            "JOIN prompt_generation g ON h.generation=g.id JOIN prompt_version v ON "
            "v.path='shared/fragments/base.md' AND v.version=(g.manifest->>v.path)::bigint"
        ).fetchone()
        assert published and published[:2] == (baseline, 1) and published[2].strip()
    assert db.migrate() == "already_applied"
    restored_engine = create_engine(restored_runtime)
    try:
        revision, binding = DataConnections(restored_engine, retail_settings).test_target(
            retail_scope
        )
        assert revision == 1 and binding is not None
        assert conninfo_to_dict(binding.dsn.get_secret_value())["password"] == (
            "fixture-retail-password"
        )
    finally:
        restored_engine.dispose()
    with psycopg.connect(db.dsn(destination)) as connection:
        assert connection.execute("SELECT revenue_minor FROM demo_daily_sales").fetchone() == (
            9999,
        )
        restored = connection.execute(
            "SELECT api_key_ciphertext, generation_profile FROM model_provider WHERE id=%s",
            (provider_id,),
        ).fetchone()
        assert restored is not None
        assert secrets.open_key(provider_id, restored[0]) == "fixture-restore-key"
        assert restored[1] == {"defaults": {"max_tokens": 1024}}
        for statement in [
            "UPDATE model_provider SET kind='unknown'",
            "UPDATE model_provider SET adapter='unknown'",
            "UPDATE model_provider SET kind='decision',chat_visible=true",
            "UPDATE model_provider SET version=0",
        ]:
            with pytest.raises(psycopg.errors.CheckViolation):
                with connection.transaction():
                    connection.execute(sql.SQL(cast(LiteralString, statement)))


def seed_legacy_provider(runtime, secrets, actor_id):
    body = ProviderCreate(
        operation_id=uuid4(),
        catalog_id="deepseek/deepseek-v4-pro",
        api_model="deepseek-v4-pro",
        alias="Pro",
        base_url="https://api.deepseek.com",
        api_key="legacy-fixture-key",
        enabled=True,
        chat_visible=True,
    )
    provider_id = uuid4()
    fingerprint = secrets.fingerprint(
        {
            "provider_id": "None",
            "command": body.model_dump(mode="json", exclude={"generation_profile"}),
        }
    )
    with psycopg.connect(db.dsn(runtime)) as connection:
        connection.execute(
            """
            INSERT INTO model_provider (
                id, catalog_id, vendor, api_model, sdk_model, alias, kind, adapter,
                base_url, api_key_ciphertext, enabled, chat_visible,
                version, binding_version, created_at, updated_at, updated_by
            ) VALUES (
                %s, %s, 'deepseek', %s, %s, %s, 'llm', 'litellm_chat',
                %s, %s, true, true, 1, 1, now(), now(), %s
            )
            """,
            (
                provider_id,
                body.catalog_id,
                body.api_model,
                body.catalog_id,
                body.alias,
                body.base_url,
                secrets.seal_key(provider_id, body.api_key),
                actor_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO model_change (
                operation_id, provider_id, actor_user_id, request_mac, after_version,
                changed_fields, created_at
            ) VALUES (%s,%s,%s,%s,1,'[]',now())
            """,
            (body.operation_id, provider_id, actor_id, fingerprint),
        )
    return body, provider_id


@pytest.mark.parametrize("mode", ["native", "sql"])
def test_upgrades_0002_decision_preserving_provider_secrets_and_history(targets, mode):
    import asyncio

    from compass_agent.llm.admission import ExecutionGate
    from compass_agent.models.repository import ModelRepository
    from compass_agent.models.service import ModelService, create_runtime
    from compass_agent.schemas import DecisionInput, DecisionQuestion

    from .provider_fixture import ProviderFixture
    from .test_models import MANAGE, ORIGINS, TEST, invoke

    runtime, migration = targets("legacydecision")
    assert db.migrate(target="0002") == "applied"
    engine = create_engine(db.dsn(runtime).replace("postgresql://", "postgresql+psycopg://", 1))
    secrets = SecretStore(Fernet.generate_key().decode())
    repo = ModelRepository(engine, secrets, ORIGINS)
    upstream = ProviderFixture()
    service = ModelService(repo, create_runtime(secrets, upstream.transport), gate=ExecutionGate())
    try:
        body, provider_id = seed_legacy_provider(runtime, secrets, MANAGE.actor_user_id)
        # Seed the historical layout with its own column list before upgrading.
        legacy_call_id = uuid4()
        with psycopg.connect(db.dsn(migration)) as connection:
            connection.execute("SET ROLE compass_agent_owner")
            connection.execute(
                "INSERT INTO model_call (call_id,provider_id,config_version,binding_version,"
                "requested_model,purpose,actor_user_id,capability_summary,status,started_at,"
                "deadline_at,upstream_outcome_unknown) VALUES (%s,%s,1,1,%s,'test',%s,'[]',"
                "'completed',now(),now()+interval '180 seconds',false)",
                (legacy_call_id, provider_id, body.api_model, TEST.actor_user_id),
            )
        with psycopg.connect(db.dsn(migration)) as connection:
            connection.execute("SET ROLE compass_agent_owner")
            connection.execute(
                "UPDATE model_provider SET kind='decision',adapter='aliyun_decision', "
                "catalog_id='aliyun/decision-model-preview',vendor='aliyun', "
                "api_model='decision-model-preview',sdk_model=NULL,chat_visible=false, "
                "base_url='https://decision.example.com/compatible-mode/v1' WHERE id=%s",
                (provider_id,),
            )
            before = connection.execute(
                "SELECT * FROM model_provider WHERE id=%s", (provider_id,)
            ).fetchone()
        assert db.migrate(mode=mode, start="0002") == "applied"
        assert db.migrate(mode=mode) == "already_applied"
        with Session(engine) as session:
            row = session.get(Provider, provider_id)
            assert row is not None and row.adapter == "systemone_decision"
            assert row.version == row.binding_version == 1
            assert secrets.open_key(row.id, row.api_key_ciphertext) == body.api_key
        with psycopg.connect(db.dsn(migration)) as connection:
            after = connection.execute(
                "SELECT * FROM model_provider WHERE id=%s", (provider_id,)
            ).fetchone()
        assert before is not None and after is not None
        assert ["systemone_decision" if v == "aliyun_decision" else v for v in before] == list(
            after[:-1]
        )
        assert after[-1] is None
        assert repo.save(body, MANAGE).provider_id == provider_id
        assert repo.operation(body.operation_id).provider_id == provider_id
        assert repo.call(legacy_call_id, TEST).status == "completed"
        result = asyncio.run(
            invoke(
                service,
                provider_id,
                DecisionInput(
                    state="保留配置测试",
                    questions={"ok": DecisionQuestion(type="noul", instructions="是否可用？")},
                ),
            )
        )
        assert result.status == "completed" and result.answers["ok"].noul == 0.8
    finally:
        engine.dispose()


@pytest.mark.parametrize("mode", ["native", "sql"])
def test_0003_upgrade_preserves_call_history_and_adds_nullable_execution(targets, mode):
    from compass_agent.models.repository import ModelRepository

    from .test_models import MANAGE, ORIGINS, TEST

    runtime, migration = targets("execution")
    assert db.migrate(target="0003") == "applied"
    engine = create_engine(db.dsn(runtime).replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        secrets = SecretStore(Fernet.generate_key().decode())
        repo = ModelRepository(engine, secrets, ORIGINS)
        body, provider_id = seed_legacy_provider(runtime, secrets, MANAGE.actor_user_id)
        call_id = uuid4()
        with psycopg.connect(db.dsn(migration)) as connection:
            connection.execute("SET ROLE compass_agent_owner")
            connection.execute(
                "INSERT INTO model_call (call_id,provider_id,config_version,binding_version,"
                "requested_model,purpose,actor_user_id,capability_summary,status,started_at,"
                "deadline_at,upstream_outcome_unknown) VALUES (%s,%s,1,1,%s,'test',%s,'[]',"
                "'completed',now(),now()+interval '180 seconds',false)",
                (call_id, provider_id, body.api_model, TEST.actor_user_id),
            )
        assert db.migrate(mode=mode, start="0003") == "applied"
        assert repo.call(call_id, TEST).execution is None
        assert repo.call(call_id, TEST).status == "completed"
        assert repo.operation(body.operation_id).provider_id == provider_id
        with psycopg.connect(db.dsn(runtime)) as connection:
            connection.execute(
                "UPDATE model_call SET execution=%s WHERE call_id=%s", ('{"version": 1}', call_id)
            )
            assert connection.execute(
                "SELECT execution->>'version' FROM model_call WHERE call_id=%s", (call_id,)
            ).fetchone() == ("1",)
    finally:
        engine.dispose()


@pytest.mark.parametrize("mode", ["native", "sql"])
def test_0006_upgrade_adds_workflow_assets_without_broadening_runtime_grants(targets, mode):
    runtime, _ = targets("workflow_" + mode)
    assert db.migrate(target="0006") == "applied"
    with psycopg.connect(db.dsn(runtime)) as connection:
        connection.execute("INSERT INTO demo_daily_sales VALUES ('2026-11-11',12345,7)")
    assert db.migrate(mode=mode, start="0006") == "applied"
    assert db.migrate(mode=mode) == "already_applied"
    with psycopg.connect(db.dsn(runtime)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            db.TARGET,
        )
        assert connection.execute("SELECT revenue_minor FROM demo_daily_sales").fetchone() == (
            12345,
        )
        assert connection.execute(
            """
            SELECT has_table_privilege('workflow_asset','INSERT'),
                   has_column_privilege('workflow_asset','name','UPDATE'),
                   has_column_privilege('workflow_asset','script_source','UPDATE')
            """
        ).fetchone() == (True, True, False)


@pytest.mark.parametrize("mode", ["native", "sql"])
def test_0010_session_upgrade_keeps_historical_titles(targets, mode):
    runtime_url, _ = targets("session_upgrade_" + mode)
    assert db.migrate(target="0010") == "applied"
    ids = [uuid4() for _ in range(4)]
    with psycopg.connect(db.dsn(runtime_url)) as connection:
        connection.execute(
            "INSERT INTO agent_session "
            "(id, tenant_id, workspace_id, owner_user_id, title, session_type, "
            "version, context_start_ordinal, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,'已有标题','chat',1,1,now(),now())",
            ids,
        )
    assert db.migrate(mode=mode, start="0010") == "applied"
    assert db.migrate(mode=mode, start="0010") == "already_applied"
    with psycopg.connect(db.dsn(runtime_url)) as connection:
        assert connection.execute(
            "SELECT title,title_status,title_attempts,title_next_at,deleted_at "
            "FROM agent_session WHERE id=%s",
            (ids[0],),
        ).fetchone() == ("已有标题", "fallback", 0, None, None)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("DELETE FROM agent_session WHERE id=%s", (ids[0],))


@pytest.mark.parametrize("mode", ["native", "sql"])
def test_0011_workflow_upgrade_preserves_old_assets_and_immutable_contracts(targets, mode):
    runtime, _ = targets("dynamic_workflow_" + mode)
    assert db.migrate(target="0011") == "applied"
    old_id, new_id, tenant, workspace, owner = [uuid4() for _ in range(5)]
    with psycopg.connect(db.dsn(runtime)) as connection:
        connection.execute(
            """
            INSERT INTO workflow_asset
                (asset_id, tenant_id, workspace_id, owner_user_id, name,
                 entry_contract, script_source, script_sha256)
            VALUES (%s,%s,%s,%s,'旧工作流','channel_attribution@1','return 1',%s)
            """,
            (old_id, tenant, workspace, owner, b"0" * 32),
        )
    assert db.migrate(mode=mode, start="0011") == "applied"
    assert db.migrate(mode=mode, start="0011") == "already_applied"
    with psycopg.connect(db.dsn(runtime)) as connection:
        assert connection.execute(
            "SELECT entry_contract, script_source, description, parameters "
            "FROM workflow_asset WHERE asset_id = %s",
            (old_id,),
        ).fetchone() == ("channel_attribution@1", "return 1", None, None)
        connection.execute(
            """
            INSERT INTO workflow_asset
                (asset_id, tenant_id, workspace_id, owner_user_id, name,
                 entry_contract, script_source, script_sha256, description, parameters)
            VALUES (%s,%s,%s,%s,'新工作流','agent_workflow@1','return 2',%s,
                    '复盘经营数据','{"type":"object"}')
            """,
            (new_id, tenant, workspace, owner, b"1" * 32),
        )
        assert connection.execute(
            """
            SELECT has_column_privilege('workflow_asset','name','UPDATE'),
                   has_column_privilege('workflow_asset','description','UPDATE'),
                   has_column_privilege('workflow_asset','parameters','UPDATE')
            """
        ).fetchone() == (True, False, False)


@pytest.mark.parametrize("mode", ["native", "sql"])
def test_workflow_deletion_keeps_history_and_allows_saving_the_same_script(targets, mode):
    runtime, _ = targets("workflow_delete_" + mode)
    assert db.migrate(target="0013") == "applied"
    context = TrustedContext(
        actor_user_id=uuid4(),
        scope=TenantScope(kind="tenant", tenant_id=uuid4(), workspace_id=uuid4()),
        purpose="chat",
    )
    assert isinstance(context.scope, TenantScope)
    asset_id = uuid4()
    with psycopg.connect(db.dsn(runtime)) as connection:
        connection.execute(
            """
            INSERT INTO workflow_asset
                (asset_id, tenant_id, workspace_id, owner_user_id, name, entry_contract,
                 script_source, script_sha256, description, parameters)
            VALUES (%s,%s,%s,%s,'旧资产','agent_workflow@1','return 1',
                    sha256(convert_to('return 1','UTF8')),'原说明','{"type":"object"}')
            """,
            (asset_id, context.scope.tenant_id, context.scope.workspace_id, context.actor_user_id),
        )
    assert db.migrate(mode=mode, start="0013") == "applied"
    engine = create_engine(runtime)
    assets = WorkflowAssets(engine)
    try:
        assert [item.asset_id for item in assets.active_tools(context, limit=10)] == [asset_id]
        other = context.model_copy(update={"actor_user_id": uuid4()})
        with pytest.raises(ModelError, match="not_found"):
            assets.delete(other, asset_id, 1)
        with pytest.raises(ModelError, match="version_conflict"):
            assets.delete(context, asset_id, 2)
        assets.delete(context, asset_id, 1)
        assets.delete(context, asset_id, 1)
        assert assets.list(context).items == assets.active_tools(context, limit=10) == []
        with pytest.raises(ModelError, match="not_found"):
            assets.get(context, asset_id, active=True)
        with pytest.raises(ModelError, match="not_found"):
            assets.patch(
                context,
                asset_id,
                WorkflowAssetPatch(name="恢复", status="active", metadataRevision=2),
            )
        with Session(engine) as session:
            deleted = session.get(WorkflowAsset, asset_id)
            assert deleted and deleted.deleted_at and deleted.script_source == "return 1"
        with psycopg.connect(db.dsn(runtime)) as connection:
            assert connection.execute(
                "SELECT has_table_privilege('workflow_asset','DELETE')"
            ).fetchone() == (False,)
        parameters = {"type": "object"}
        new_asset = assets.save(
            context,
            "重新保存",
            "return 1",
            DYNAMIC_CONTRACT,
            description="新说明",
            parameters=parameters,
        )
        assert new_asset.asset_id != asset_id
        assert assets.get(context, new_asset.asset_id, active=True).name == "重新保存"
        assert (
            assets.save(
                context,
                "再次保存",
                "return 1",
                DYNAMIC_CONTRACT,
                description="新说明",
                parameters=parameters,
            ).asset_id
            == new_asset.asset_id
        )
    finally:
        engine.dispose()


def test_session_title_jobs_and_soft_delete(targets):
    import asyncio
    from uuid import UUID

    from compass_agent.llm.admission import ExecutionGate
    from compass_agent.models.repository import ModelRepository
    from compass_agent.models.service import ModelService, create_runtime
    from compass_agent.promptops.service import bootstrap
    from compass_agent.runtime.entities import AgentRun
    from compass_agent.runtime.message_reader import MessageReader
    from compass_agent.runtime.session_management import (
        claim_title,
        finish_title,
        process_titles,
        update_session,
    )
    from compass_agent.runtime.store import RuntimeStore, now, public_run
    from compass_agent.schemas import (
        MainModelSelection,
        SessionMutation,
        SessionRename,
        SessionSubmit,
    )

    from .provider_fixture import ProviderFixture
    from .test_agent_run import CHAT
    from .test_models import ORIGINS, create

    url, _ = targets("session_metadata")
    assert db.migrate() == "applied"
    engine = create_engine(url)
    with Session(engine) as tx, tx.begin():
        bootstrap(tx)
    store = RuntimeStore(engine, Fernet.generate_key().decode())
    store.ready = True
    reader = MessageReader(store)
    try:
        first = SessionSubmit(
            requestId=uuid4(),
            question="比较两周\n门店渠道实付的变化",
            selection=MainModelSelection(mode="auto"),
        )
        created = store.submit(first, CHAT)
        assert store.submit(first, CHAT) == created
        session_id, run_id = UUID(created["sessionId"]), UUID(created["runId"])
        assert created["session"]["title"] == "比较两周 门店渠道实付的变化"
        assert created["session"]["titleStatus"] == "pending"
        original_time = created["session"]["updatedAt"]
        job = claim_title(store)
        assert job is not None and job.question == first.question and job.attempt == 1
        assert claim_title(store) is None
        foreign = CHAT.model_copy(update={"actor_user_id": uuid4()})
        rename = SessionRename(requestId=uuid4(), title="  门店复盘  ")
        with pytest.raises(ModelError, match="not_found"):
            update_session(store, session_id, rename, foreign)
        manual = update_session(store, session_id, rename, CHAT)
        assert manual == update_session(store, session_id, rename, CHAT)
        finish_title(store, job, "迟到的自动标题")
        assert reader.session(session_id, CHAT)["title"] == "门店复盘"
        assert reader.session(session_id, CHAT)["updatedAt"] == original_time
        deletion = SessionMutation(requestId=uuid4())
        with pytest.raises(ModelError, match="session_busy"):
            update_session(store, session_id, deletion, CHAT)
        with store.transaction() as tx:
            run = tx.get(AgentRun, run_id)
            assert run is not None
            run.status = "completed"
            run.finished_at = now()
            run.public_view = {**run.public_view, "run": public_run(run)}
        assert update_session(store, session_id, deletion, CHAT) == {"sessionId": str(session_id)}
        assert update_session(store, session_id, deletion, CHAT) == {"sessionId": str(session_id)}
        assert update_session(store, session_id, SessionMutation(requestId=uuid4()), CHAT) == {
            "sessionId": str(session_id)
        }
        assert not reader.sessions(CHAT).items
        with pytest.raises(ModelError, match="not_found"):
            reader.messages(session_id, CHAT)
        with pytest.raises(ModelError, match="not_found"):
            store.artifact(run_id, uuid4(), CHAT)
        with pytest.raises(ModelError, match="not_found"):
            store.submit(first, CHAT)
        repository = ModelRepository(engine, SecretStore(Fernet.generate_key().decode()), ORIGINS)
        _, provider = create(repository)
        fixture = ProviderFixture()
        models = ModelService(
            repository, create_runtime(repository.secrets, fixture.transport), gate=ExecutionGate()
        )
        second = store.submit(
            first.model_copy(
                update={
                    "request_id": uuid4(),
                    "selection": MainModelSelection(mode="fixed", provider_id=provider.provider_id),
                }
            ),
            CHAT,
        )

        async def wait_for_generated_title():
            async def allowed(context):
                assert context == CHAT

            task = asyncio.create_task(process_titles(store, allowed, lambda: models, lambda: True))
            try:
                async with asyncio.timeout(5):
                    while True:
                        item = await asyncio.to_thread(
                            reader.session, UUID(second["sessionId"]), CHAT
                        )
                        if item["titleStatus"] == "generated":
                            return item["title"]
                        await asyncio.sleep(0.05)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        generated = asyncio.run(wait_for_generated_title())
        assert generated == "这是模型测试回复。"
        sent = json.loads(fixture.requests[0].content)
        assert sent["messages"][-1]["content"] == first.question
        assert sent["stream"] is False and not sent.get("tools")
        item = reader.sessions(CHAT).items[0]
        assert item.title == generated and item.title_status == "generated"
        assert item.updated_at == second["session"]["updatedAt"]
    finally:
        engine.dispose()


@pytest.mark.parametrize("mode", ["native", "sql"])
def test_0008_upgrade_backfills_attempt_summaries_without_inventing_content(targets, mode):
    from .test_models import MANAGE, TEST

    runtime, migration = targets("attempts_" + mode)
    assert db.migrate(target="0008") == "applied"
    engine = create_engine(runtime)
    try:
        secrets = SecretStore(Fernet.generate_key().decode())
        body, provider_id = seed_legacy_provider(runtime, secrets, MANAGE.actor_user_id)
        call_id, first_attempt = uuid4(), uuid4()
        historical = {
            "version": 1,
            "attempts": [
                {
                    "attempt_id": str(first_attempt),
                    "status": "failed",
                    "sent": True,
                    "cleanup_confirmed": True,
                    "upstream_outcome_unknown": False,
                    "error_code": "upstream_unavailable",
                    "usage": {"input_tokens": 12},
                },
                {"status": "completed", "sent": True, "duration_ms": 10},
            ],
        }
        with psycopg.connect(db.dsn(migration)) as connection:
            connection.execute("SET ROLE compass_agent_owner")
            connection.execute(
                "INSERT INTO model_call (call_id,provider_id,config_version,binding_version,"
                "requested_model,purpose,actor_user_id,capability_summary,status,started_at,"
                "deadline_at,upstream_outcome_unknown,execution) VALUES (%s,%s,1,1,%s,'test',"
                "%s,'[]','completed',now(),now()+interval '180 seconds',false,%s)",
                (
                    call_id,
                    provider_id,
                    body.api_model,
                    TEST.actor_user_id,
                    json.dumps(historical),
                ),
            )
        assert db.migrate(mode=mode, start="0008") == "applied"
        assert db.migrate(mode=mode) == "already_applied"
        with psycopg.connect(db.dsn(runtime)) as connection:
            rows = connection.execute(
                "SELECT id,number,status,origin,request_content,output_content,"
                "actual_cost_microusd,usage FROM model_attempt WHERE call_id=%s ORDER BY number",
                (call_id,),
            ).fetchall()
            assert len(rows) == 2 and rows[0][0] == first_attempt
            assert [row[1:4] for row in rows] == [
                (1, "failed", "legacy_summary"),
                (2, "completed", "legacy_summary"),
            ]
            assert all(row[4:7] == (None, None, None) for row in rows)
            assert rows[0][7] == {"input_tokens": 12} and rows[1][7] == {}
            assert connection.execute(
                "SELECT request_content,output_content FROM model_call WHERE call_id=%s",
                (call_id,),
            ).fetchone() == (None, None)
    finally:
        engine.dispose()


@pytest.mark.parametrize("mode", ["native", "sql"])
def test_0012_provider_upgrade_preserves_binding_and_defaults_to_null(targets, mode):
    runtime, _ = targets("model_profile_" + mode)
    assert db.migrate(target="0012") == "applied"
    provider_id, actor_id = uuid4(), uuid4()
    with psycopg.connect(db.dsn(runtime)) as connection:
        connection.execute(
            """
            INSERT INTO model_provider (
                id, catalog_id, vendor, api_model, sdk_model, alias, kind, adapter,
                base_url, api_key_ciphertext, enabled, chat_visible,
                version, binding_version, created_at, updated_at, updated_by
            ) VALUES (
                %s, 'deepseek/deepseek-chat', 'deepseek', 'deepseek-chat',
                'deepseek/deepseek-chat', 'Legacy profile', 'llm', 'litellm_chat',
                'https://api.deepseek.com', 'legacy-ciphertext', true, true, 2, 1, now(), now(), %s
            )
            """,
            (provider_id, actor_id),
        )
    assert db.migrate(mode=mode, start="0012") == "applied"
    assert db.migrate(mode=mode, start="0012") == "already_applied"
    with psycopg.connect(db.dsn(runtime)) as connection:
        assert connection.execute(
            """
            SELECT generation_profile, api_key_ciphertext, version, binding_version
            FROM model_provider WHERE id=%s
            """,
            (provider_id,),
        ).fetchone() == (None, "legacy-ciphertext", 2, 1)


@pytest.mark.parametrize("mode", ["native", "sql"])
def test_0017_reopens_drained_control_and_protects_semantic_receipts(targets, mode, monkeypatch):
    runtime, migration = targets("registry_" + mode)
    assert db.migrate(target="0016") == "applied"
    with psycopg.connect(db.dsn(runtime)) as old:
        old.execute(
            "UPDATE agent_runtime_control SET policy_sha256=%s WHERE key='execution'", ("b" * 64,)
        )
    assert db.migrate(start="0016", target="0017") == "applied"
    with psycopg.connect(db.dsn(runtime)) as upgraded:
        assert upgraded.execute(
            "SELECT policy_sha256 FROM agent_runtime_control WHERE key='execution'"
        ).fetchone() == (None,)
        assert upgraded.execute(
            """SELECT has_table_privilege('semantic_run_binding','INSERT'),
            has_table_privilege('semantic_run_binding','UPDATE'),
            has_table_privilege('semantic_metric_offer','DELETE'),
            has_column_privilege('semantic_metric_offer','private_payload','UPDATE'),
            has_column_privilege('semantic_metric_offer','owner_id','UPDATE')"""
        ).fetchone() == (True, False, False, True, False)
    from compass_agent.runtime.store import RuntimeStore

    from .test_agent_run import CHAT
    from .test_durable_runtime_integration import command

    engine = create_engine(runtime)
    try:
        store = RuntimeStore(
            engine,
            Fernet.generate_key().decode(),
            settings=Settings(
                compass_context_runtime_enabled=False,
                compass_semantic_workspaces="",
            ),
        )
        store.ready = True
        # Seed a pre-0019 Run; goals did not exist in this historical schema.
        with monkeypatch.context() as historical:
            historical.setattr("compass_agent.goal.controller.open_goal", lambda *a, **k: None)
            run_id = store.submit(command(), CHAT)["runId"]
        assert CHAT.scope.kind == "tenant"
        with psycopg.connect(db.dsn(migration)) as c:
            c.execute("SET ROLE compass_agent_owner")
            c.execute(
                """INSERT INTO semantic_run_binding
                VALUES (%s,%s,%s,'registry',repeat('b',64),'[]',now())""",
                (run_id, CHAT.scope.tenant_id, CHAT.scope.workspace_id),
            )
        with pytest.raises(DBAPIError, match="Registry binding differs"):
            db.migrate(mode=mode, start="0017")
        registry = {"registry": {"manifest": "b" * 64, "pins": []}}
        with psycopg.connect(db.dsn(migration)) as c:
            c.execute("SET ROLE compass_agent_owner")
            c.execute(
                """UPDATE agent_run SET config_snapshot=jsonb_set(
                config_snapshot,'{contextRuntime}',%s::jsonb) WHERE id=%s""",
                (json.dumps(registry), run_id),
            )
        assert db.migrate(mode=mode, start="0017") == "applied"
        assert store.read_run(UUID(run_id)).config_snapshot["contextRuntime"] == registry
    finally:
        engine.dispose()
    with psycopg.connect(db.dsn(runtime)) as upgraded:
        assert upgraded.execute("SELECT to_regclass('semantic_run_binding')").fetchone() == (None,)


@pytest.mark.parametrize("mode", ["native", "sql"])
def test_0016_prompt_publication_keeps_readers_and_serializes_publishers(targets, mode):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from compass_agent import schemas as dto
    from compass_agent.promptops import service
    from compass_agent.promptops.reader import PromptReader

    runtime, _ = targets("prompt_" + mode)
    assert db.migrate(target="0015") == "applied"
    with psycopg.connect(db.dsn(runtime)) as old_db:
        old_db.execute(
            "UPDATE agent_runtime_control SET policy_sha256=%s WHERE key='execution'",
            ("a" * 64,),
        )
    assert db.migrate(mode=mode, start="0015") == "applied"
    with psycopg.connect(db.dsn(runtime)) as upgraded:
        assert upgraded.execute(
            "SELECT policy_sha256 FROM agent_runtime_control WHERE key='execution'"
        ).fetchone() == (None,)
    engine = create_engine(db.dsn(runtime).replace("postgresql://", "postgresql+psycopg://", 1))
    paths = (
        "tasks/business_report/scenes/weekly/finance.md",
        "tasks/business_report/scenes/weekly/report.md",
    )
    actor = uuid4()
    gate = Barrier(2)
    try:
        with Session(engine) as tx, tx.begin():
            old = service.bootstrap(tx, actor)
            for path in paths:
                detail = dto.PromptFileDetail.model_validate(service.file_detail(tx, path))
                data = dto.PromptDraft(
                    path=path,
                    body="发布 " + path,
                    expected_edit_rev=detail.edit_rev,
                    base_epoch=detail.active_epoch,
                    request_id=uuid4(),
                )
                service.mutate(tx, actor, "file-draft", data, service.save_draft)

        def publish(path):
            data = dto.PromptReleaseTarget(action="file", path=path)
            with Session(engine) as tx:
                view = service.release_plan(tx, service.head(tx), data).preview
            gate.wait(timeout=10)
            cmd = dto.PromptRelease(
                action="file",
                path=path,
                confirmation_hash=view.confirmation_hash,
                note="并发测试",
                request_id=uuid4(),
            )
            with Session(engine) as tx, tx.begin():
                return service.mutate(tx, actor, "releases", cmd, service.commit_release)[
                    "generation"
                ]

        with ThreadPoolExecutor(max_workers=2) as executor:
            generations = list(executor.map(publish, paths))
        with Session(engine) as tx:
            current = PromptReader.pin_current(tx)
            assert current == max(generations)
            assert [r.version for r in PromptReader.load_exact(tx, current, paths)] == [2, 2]
            assert [r.version for r in PromptReader.load_exact(tx, old, paths)] == [1, 1]
        with psycopg.connect(db.dsn(runtime)) as connection:
            for table in ("prompt_version", "prompt_generation", "prompt_command"):
                assert connection.execute(
                    "SELECT has_table_privilege(current_user,%s,'UPDATE')", (table,)
                ).fetchone() == (False,)
            assert connection.execute(
                "SELECT has_table_privilege('compass_agent_scheduler','prompt_file','INSERT')"
            ).fetchone() == (False,)
    finally:
        engine.dispose()


@pytest.mark.parametrize("mode", ["native", "sql"])
def test_0020_sandbox_upgrade_preserves_legacy_leases_and_limits_runtime_grants(targets, mode):
    runtime, _ = targets("sandbox_0021_" + mode)
    db.migrate(target="0020")
    identifier = uuid4()
    with psycopg.connect(db.dsn(runtime)) as conn:
        conn.execute(
            """INSERT INTO sandbox_allocation
            (id,tenant_id,workspace_id,actor_user_id,run_id,step_id,tool_call_id,attempt_id,
             host,slot,request_digest,state,resource_phase,resource_seq,created_at,resource_deadline)
            VALUES (%s,%s,%s,%s,%s,%s,'legacy',%s,'upgrade-test',0,'digest','unknown','clean',2,
                    now(),now()+interval '1 minute')""",
            (identifier, uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4()),
        )
    assert db.migrate(mode=mode, start="0020") == "applied"
    with psycopg.connect(db.dsn(runtime)) as conn:
        assert conn.execute(
            "SELECT state,resource_phase,shared FROM sandbox_allocation WHERE id=%s", (identifier,)
        ).fetchone() == ("unknown", "clean", False)
        assert conn.execute("SELECT count(*) FROM sandbox_execution").fetchone() == (0,)
        for action in (
            "DELETE FROM sandbox_execution",
            "ALTER TABLE sandbox_execution ADD bad int",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(sql.SQL(cast(LiteralString, action)))
