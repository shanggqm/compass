"""Shared protocol and policy checks for the Agent and Scheduler roles."""

from urllib.parse import urlsplit

from sqlalchemy import select, text

from compass_agent.models.errors import ModelError
from compass_agent.runtime.private_state import digest
from compass_agent.scheduler.entities import RuntimeControl

REVISION = "0019"
MODE_LOCK = 985002
LEGACY_OWNER_LOCK = 985001


def require_database_revision(tx):
    if tx.scalars(text("SELECT version_num FROM alembic_version")).all() != [REVISION]:
        raise ModelError("runtime_version_mismatch", 503)


def require_internal_mode(tx):
    tx.execute(text(f"SELECT pg_advisory_xact_lock_shared({MODE_LOCK})"))
    row = tx.get(RuntimeControl, "execution")
    if row is None or row.mode != "internal":
        raise ModelError("runtime_mode_mismatch", 503)
    return row


def budget_snapshot(settings):
    return {
        "version": 1,
        "controlled": settings.compass_controlled_autonomy_enabled,
        "profiles": [
            item.model_dump(mode="json") for item in settings.compass_autonomy_model_profiles
        ],
        "tokens": settings.compass_autonomy_token_budget,
        "costMicrousd": settings.compass_autonomy_cost_budget_microusd,
    }


def endpoint_identity(value):
    endpoint = urlsplit(value)
    return [endpoint.scheme, endpoint.hostname, endpoint.port, endpoint.path]


def policy_fingerprint(settings, overrides, metric_catalog=None):
    from compass_agent.context_runtime.profiles import POLICY_DIGEST
    from compass_agent.context_runtime.semantics import MetricCatalog

    catalog = metric_catalog or MetricCatalog(settings.compass_metric_dictionary_path)
    return digest(
        {
            "budget": budget_snapshot(settings),
            "context": {
                "enabled": settings.compass_context_runtime_enabled,
                "optimization": settings.compass_context_optimization_enabled,
                "resultsRoot": settings.compass_context_results_root,
                "policy": POLICY_DIGEST,
                "scopes": sorted(
                    (str(r.tenant_id), str(r.workspace_id or ""), r.profile)
                    for r in settings.compass_context_scope_profiles
                ),
                "activeMetricDictionary": catalog.active,
                "supportedMetricDictionaries": sorted(catalog.by_digest),
                **(
                    {"semanticWorkspaces": settings.compass_semantic_workspaces.split(",")}
                    if settings.compass_semantic_workspaces
                    else {}
                ),
            },
            "capacity": [
                settings.compass_runtime_max_active,
                settings.compass_runtime_tenant_active,
                settings.compass_runtime_max_backlog,
                settings.compass_runtime_tenant_backlog,
                overrides,
            ],
            "runtime": [
                settings.compass_runtime_lease_seconds,
                settings.compass_runtime_key_version,
                settings.compass_tool_batch_mode,
                settings.compass_tool_failure_mode,
                endpoint_identity(settings.compass_redis_url),
                endpoint_identity(settings.compass_web_authz_url),
            ],
            "sandbox": [
                settings.compass_sandbox_enabled,
                settings.compass_sandbox_domain,
                settings.compass_sandbox_host_id,
                settings.compass_sandbox_image,
                settings.compass_sandbox_volume_prefix,
                settings.compass_sandbox_slots,
            ],
            "routing": [
                settings.compass_routing_mode,
                str(settings.compass_routing_jev_provider_id or ""),
                str(settings.compass_routing_flash_provider_id or ""),
                str(settings.compass_routing_pro_provider_id or ""),
            ],
            "model_origins": settings.compass_model_allowed_origins,
            "mcp_origins": settings.compass_mcp_allowed_origins,
            "mcp_fixture_origins": settings.compass_mcp_local_fixture_origins,
            "data_endpoints": settings.compass_data_allowed_endpoints,
            "retail_endpoints": settings.compass_retail_allowed_endpoints,
        }
    )


def verify_policy(tx, fingerprint, *, register=False):
    row = require_internal_mode(tx)
    if row.policy_sha256 is None and register:
        row = tx.scalar(
            select(RuntimeControl)
            .where(RuntimeControl.key == "execution")
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row.policy_sha256 is None:
            row.policy_sha256 = fingerprint
    if row.policy_sha256 != fingerprint:
        raise ModelError("runtime_policy_mismatch", 503)
