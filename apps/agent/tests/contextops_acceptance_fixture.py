"""Explicit isolated PostgreSQL inputs for the ContextOps browser acceptance suite."""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
from compass_agent.data_connections.connections import DataConnections
from compass_agent.models.entities import Provider
from compass_agent.models.repository import ModelRepository
from compass_agent.models.secrets import SecretStore
from compass_agent.promptops.service import bootstrap
from compass_agent.schemas import (
    GenerationProfileInput,
    ModelTokenLimits,
    PlatformScope,
    ProviderCreate,
    RetailConnectionSave,
    TenantScope,
    TrustedContext,
)
from compass_agent.settings import Settings
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

TENANT = UUID("99500000-0000-4000-8000-000000000010")
WORKSPACE = UUID("99500000-0000-4000-8000-000000000020")
OTHER_TENANT = UUID("99500000-0000-4000-8000-000000000011")
OTHER_WORKSPACE = UUID("99500000-0000-4000-8000-000000000022")
MARKER = "compass:contextops-acceptance/971"
READER = "compass_l26_reader"
OTHER_READER = "compass_l26_other_reader"


def target(name, database):
    value = os.environ[name].replace("postgresql+psycopg://", "postgresql://")
    parsed = urlsplit(value)
    if (
        os.environ.get("COMPASS_CONTEXTOPS_E2E") != "true"
        or parsed.hostname != "127.0.0.1"
        or parsed.path != "/" + database
    ):
        raise RuntimeError("Explicit isolated ContextOps acceptance targets are required")
    return value


def retail_fixture():
    database = "compass_retail_test_971"
    target_url = target("COMPASS_CONTEXTOPS_RETAIL_ADMIN_URL", database)
    admin_url = target("AGENT_TEST_ADMIN_URL", "postgres")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        existing = admin.execute(
            "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s",
            (database,),
        ).fetchone()
        if existing:
            if existing != (MARKER,):
                raise RuntimeError("Unknown retail fixture database")
            return
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        admin.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                sql.Identifier(database), sql.Literal(MARKER)
            )
        )
    with psycopg.connect(target_url) as conn:
        conn.execute("CREATE SCHEMA core; CREATE SCHEMA sec")
        conn.execute(
            "CREATE TABLE sec.grants (principal name, tenant_id smallint, region_id bigint, "
            "PRIMARY KEY(principal,tenant_id,region_id))"
        )
        conn.execute(
            "CREATE FUNCTION sec.allowed(t smallint,r bigint) RETURNS boolean "
            "LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$ "
            "SELECT EXISTS(SELECT FROM sec.grants WHERE principal=session_user "
            "AND tenant_id=t AND (r IS NULL OR region_id=r)) $$"
        )
        conn.execute("REVOKE ALL ON FUNCTION sec.allowed(smallint,bigint) FROM PUBLIC")
        for reader in (READER, OTHER_READER):
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(reader),
                    sql.Literal(os.environ["COMPASS_CONTEXTOPS_RETAIL_PASSWORD"]),
                )
            )
            conn.execute(
                sql.SQL("ALTER ROLE {} SET default_transaction_read_only=on").format(
                    sql.Identifier(reader)
                )
            )
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA core,sec TO {}").format(sql.Identifier(reader))
            )
            conn.execute(
                sql.SQL("GRANT EXECUTE ON FUNCTION sec.allowed(smallint,bigint) TO {}").format(
                    sql.Identifier(reader)
                )
            )
        conn.cursor().executemany(
            "INSERT INTO sec.grants VALUES(%s,%s,%s)",
            [(READER, 101, 1), (READER, 101, 2), (OTHER_READER, 202, 9)],
        )
        for relation in ("channel_product_daily", "channel", "sku", "spu"):
            name = sql.Identifier("core", relation)
            conn.execute(
                sql.SQL(
                    "CREATE TABLE {} (tenant_id smallint,region_id bigint,"
                    "amount integer,cost integer,note text)"
                ).format(name)
            )
            conn.execute(
                sql.SQL(
                    "INSERT INTO {} VALUES(101,1,120,50,%s),(101,2,80,30,%s),(202,9,777,99,%s)"
                ).format(name),
                ("正常配送", "正常退货", "另一租户记录"),
            )
            conn.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(name))
            conn.execute(
                sql.SQL(
                    "CREATE POLICY retail_scope ON {} FOR SELECT "
                    "USING(sec.allowed(tenant_id,region_id))"
                ).format(name)
            )
            conn.execute(
                sql.SQL("GRANT SELECT(tenant_id,region_id,amount,cost,note) ON {} TO {},{}").format(
                    name, sql.Identifier(READER), sql.Identifier(OTHER_READER)
                )
            )


def seed():
    retail_fixture()
    database = target("AGENT_TEST_DATABASE_URL", "compass_agent_test_971")
    settings = Settings()
    engine = create_engine(database.replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with Session(engine) as tx, tx.begin():
            bootstrap(tx)
        repository = ModelRepository(
            engine,
            SecretStore(settings.compass_model_encryption_key),
            settings.compass_model_allowed_origins,
        )
        with Session(engine) as tx:
            provider_id = tx.scalar(select(Provider.id).where(Provider.alias == "971 受控模型"))
        if provider_id is None:
            provider_id = repository.save(
                ProviderCreate(
                    operation_id=uuid4(),
                    catalog_id="deepseek/deepseek-v4-pro",
                    api_model="deepseek-v4-pro",
                    alias="971 受控模型",
                    base_url="https://api.deepseek.com",
                    api_key="isolated-fixture",
                    enabled=True,
                    chat_visible=True,
                    generation_profile=GenerationProfileInput(
                        limits=ModelTokenLimits(max_input_tokens=98304, max_output_tokens=4096)
                    ),
                ),
                TrustedContext(
                    actor_user_id=UUID("99500000-0000-4000-8000-000000000001"),
                    scope=PlatformScope(kind="platform"),
                    purpose="manage",
                ),
            ).provider_id
        connections = DataConnections(engine, settings)
        retail = urlsplit(os.environ["COMPASS_CONTEXTOPS_RETAIL_ADMIN_URL"])
        retail_service_host = os.environ.get("COMPASS_CONTEXTOPS_RETAIL_SERVICE_HOST", "127.0.0.1")
        retail_service_port = int(
            os.environ.get("COMPASS_CONTEXTOPS_RETAIL_SERVICE_PORT", str(retail.port or 55440))
        )
        for tenant, workspace, role, business_tenant in (
            (TENANT, WORKSPACE, READER, 101),
            (OTHER_TENANT, OTHER_WORKSPACE, OTHER_READER, 202),
        ):
            scope = TenantScope(kind="tenant", tenant_id=tenant, workspace_id=workspace)
            if connections.status(scope).connection is None:
                connections.save(
                    scope,
                    RetailConnectionSave(
                        host=retail_service_host,
                        port=retail_service_port,
                        database_name="compass_retail_test_971",
                        role=role,
                        sslmode="disable",
                        retail_tenant_id=business_tenant,
                        revision=0,
                        password=SecretStr(os.environ["COMPASS_CONTEXTOPS_RETAIL_PASSWORD"]),
                    ),
                )
        with psycopg.connect(
            target("WEB_TEST_MIGRATION_DATABASE_URL", "compass_web_test_971")
        ) as web:
            web.execute("SET ROLE compass_web_owner")
            # Give both ordinary members an explicit Chat grant for this isolated suite.
            web.execute(
                "UPDATE web.workspace_member SET permission_keys=ARRAY["
                "'module.analysis','models.invoke','module.plugins',"
                "'module.reports','workspaces.read'] "
                "WHERE id=ANY(%s::uuid[])",
                (["99500000-0000-4000-8000-000000000040", "99500000-0000-4000-8000-000000000042"],),
            )
            web.execute(
                "UPDATE web.tenant SET authz_version=authz_version+1 WHERE id=%s", (TENANT,)
            )
        result = {
            "provider_id": str(provider_id),
            "tenant_id": str(TENANT),
            "workspace_id": str(WORKSPACE),
        }
        output = Path(os.environ.get("COMPASS_CONTEXTOPS_REPORT_DIR", "work/971-contextops"))
        output.mkdir(parents=True, exist_ok=True)
        (output / "fixture.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    finally:
        engine.dispose()


def change_permission(allowed):
    with psycopg.connect(
        target("COMPASS_CONTEXTOPS_RETAIL_ADMIN_URL", "compass_retail_test_971")
    ) as conn:
        if allowed:
            conn.execute(
                "INSERT INTO sec.grants VALUES(%s,101,2) ON CONFLICT DO NOTHING", (READER,)
            )
        else:
            conn.execute(
                "DELETE FROM sec.grants WHERE principal=%s AND tenant_id=101 AND region_id=2",
                (READER,),
            )
    print(json.dumps({"source_region_allowed": allowed}))


def mcp_tokens():
    from compass_agent.context_ops.mcp_server import issue_token

    target("AGENT_TEST_DATABASE_URL", "compass_agent_test_971")
    settings = Settings()
    tokens = {}
    for name, actor, purpose, tenant, workspace in (
        ("platform", "000001", "contextops_admin", TENANT, WORKSPACE),
        ("analyst", "000003", "contextops_read", TENANT, WORKSPACE),
        ("outsider", "000006", "contextops_read", OTHER_TENANT, OTHER_WORKSPACE),
    ):
        context = TrustedContext.model_validate(
            {
                "actor_user_id": UUID("99500000-0000-4000-8000-000000" + actor),
                "purpose": purpose,
                "scope": TenantScope(kind="tenant", tenant_id=tenant, workspace_id=workspace),
            }
        )
        tokens[name] = issue_token(settings, context)
    output = Path(os.environ.get("COMPASS_CONTEXTOPS_REPORT_DIR", "work/971-contextops"))
    output.mkdir(parents=True, exist_ok=True)
    path = output / "mcp-tokens.json"
    path.write_text(json.dumps(tokens) + "\n")
    path.chmod(0o600)
    print("Created three isolated MCP credentials; expires after one hour.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("seed", "revoke-region", "restore-region", "mcp-tokens"))
    command = parser.parse_args().action
    if command == "seed":
        seed()
    elif command == "mcp-tokens":
        mcp_tokens()
    else:
        change_permission(command == "restore-region")
