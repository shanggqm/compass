from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from compass_agent.context_runtime.profiles import ScopeRule


class RetailRunScopeConfig(BaseModel):
    """Legacy deployment input accepted by the one-time seed importer."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True, frozen=True)

    tenant_id: UUID = Field(alias="tenantId")
    workspace_id: UUID = Field(alias="workspaceId")
    release: str = Field(min_length=1, max_length=120)
    content_hash: str = Field(
        alias="contentHash",
        pattern=r"^[0-9a-f]{64}$",
    )
    role: str = Field(
        min_length=8,
        max_length=63,
        pattern=r"^[a-z_][a-z0-9_]{0,55}_a_east$",
    )
    retail_tenant_id: int = Field(alias="retailTenantId", ge=1, le=9007199254740991)
    schema_name: Literal["core"] = Field(alias="schema")


class AutonomyModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: UUID
    binding_version: int = Field(ge=1)
    sdk_model: str | None = Field(min_length=1, max_length=200)
    api_base: str = Field(max_length=2048)
    input_tokens: int = Field(ge=1, le=1_050_000)
    output_tokens: int = Field(ge=1, le=32768)
    input_microusd: int = Field(ge=0, le=100_000)
    output_microusd: int = Field(ge=0, le=100_000)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    database_url: str = "postgresql+psycopg://compass:compass_local_only@127.0.0.1:55432/compass"
    compass_routing_jev_provider_id: UUID | None = None
    compass_routing_flash_provider_id: UUID | None = None
    compass_routing_pro_provider_id: UUID | None = None

    @field_validator(
        "compass_routing_jev_provider_id",
        "compass_routing_flash_provider_id",
        "compass_routing_pro_provider_id",
        mode="before",
    )
    @classmethod
    def optional_routing_provider(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    compass_routing_mode: str = "jev_with_flash"
    compass_controlled_autonomy_enabled: bool = False
    compass_autonomy_model_profiles: list[AutonomyModelProfile] = Field(
        default_factory=list, max_length=32
    )
    compass_autonomy_token_budget: int = Field(default=10_000_000, ge=1, le=10_000_000)
    compass_autonomy_cost_budget_microusd: int = Field(default=10_000_000, ge=1, le=10_000_000)
    compass_model_max_concurrency: int = Field(default=8, ge=1, le=64)
    compass_model_max_inflight: int = Field(default=32, ge=1, le=256)
    compass_observability_enabled: bool = False
    compass_context_runtime_enabled: bool = True
    compass_context_optimization_enabled: bool = True
    compass_context_snapshot_ttl_days: int = Field(default=14, ge=1, le=365)
    compass_context_security_mode: Literal["enforce", "audit_only"] = "enforce"
    compass_contextops_mcp_enabled: bool = True
    compass_context_results_root: str = "work/runtime/tool-results"
    compass_context_scope_profiles: list[ScopeRule] = Field(default_factory=list, max_length=512)

    @model_validator(mode="after")
    def context_security_environment(self):
        if (
            self.compass_context_security_mode == "audit_only"
            and self.compass_observability_environment not in {"local", "test"}
        ):
            raise ValueError("context audit_only requires a local or test environment")
        return self

    compass_metric_dictionary_path: str = ""
    compass_observability_sample_ratio: float = Field(default=1, ge=0, le=1, allow_inf_nan=False)
    compass_observability_instance_id: str = Field(
        default="", max_length=128, pattern=r"^[a-zA-Z0-9_.:-]*$"
    )
    compass_observability_environment: Literal[
        "local", "test", "staging", "production", "showcase"
    ] = "local"
    compass_observability_capture_content: bool | None = None
    compass_observability_content_max_bytes: int = Field(default=16384, ge=1024, le=65536)
    compass_langfuse_base_url: str = ""
    compass_langfuse_public_key: str = ""
    compass_langfuse_secret_key: SecretStr = SecretStr("")
    compass_redis_url: str = "redis://127.0.0.1:56379/0"
    compass_redis_password: SecretStr = SecretStr("")
    compass_runtime_state_key: SecretStr = SecretStr("")
    compass_runtime_event_token: SecretStr = SecretStr("")
    compass_runtime_consumer_slots: int = Field(default=1, ge=0, le=64)
    compass_runtime_key_version: str = Field(default="local-v1", min_length=1, max_length=64)
    compass_scheduler_database_url: SecretStr = SecretStr("")
    compass_scheduler_agent_url: str = "http://127.0.0.1:8102"
    compass_scheduler_token: SecretStr = SecretStr("")
    compass_runtime_max_active: int = Field(default=1000, ge=1, le=1000)
    compass_runtime_max_backlog: int = Field(default=3000, ge=1, le=3000)
    compass_runtime_tenant_active: int = Field(default=200, ge=1, le=200)
    compass_runtime_tenant_backlog: int = Field(default=20, ge=1, le=20)
    compass_runtime_tenant_overrides: str = "{}"
    compass_runtime_lease_seconds: int = Field(default=15, ge=3, le=300)
    compass_semantic_workspaces: str = ""
    compass_embedding_provider_id: str = ""
    compass_web_authz_url: str = "http://127.0.0.1:8100/api/internal/analysis/authorize"
    compass_demo_enabled: bool = False
    compass_agent_service_token: str = ""
    compass_model_encryption_key: str = ""
    compass_model_allowed_origins: str = "https://api.deepseek.com"
    compass_retail_allowed_endpoints: str = "127.0.0.1:55432,localhost:55432,postgres:5432"
    compass_data_allowed_endpoints: str = (
        "127.0.0.1:5432,localhost:5432,127.0.0.1:3306,localhost:3306,mysql:3306"
    )
    compass_sandbox_enabled: bool = False
    compass_sandbox_domain: str = "127.0.0.1:8112"
    compass_sandbox_api_key: SecretStr = SecretStr("")
    compass_sandbox_image: str = "compass-sandbox-python:lesson18"
    compass_sandbox_host_id: str = "lesson18-local"
    compass_sandbox_volume_prefix: str = "compass-sandbox-slot"
    compass_sandbox_slots: int = Field(default=16, ge=1, le=256)
    compass_mcp_allowed_origins: str = ""
    compass_mcp_local_fixture_origins: str = ""
    compass_tool_batch_mode: Literal["serial", "parallel"] = "serial"
    compass_tool_failure_mode: Literal["stop", "continue"] = "stop"
    compass_retail_run_scope: str = ""
    compass_retail_run_readonly_dsn: SecretStr = SecretStr("")

    def retail_run_configuration(
        self,
    ) -> tuple[RetailRunScopeConfig | None, SecretStr | None, str | None]:
        """Parse the optional default connection used during seed."""

        raw_scope = self.compass_retail_run_scope.strip()
        raw_dsn = self.compass_retail_run_readonly_dsn.get_secret_value().strip()
        if not raw_scope and not raw_dsn:
            return None, None, None
        if not raw_scope or not raw_dsn:
            return None, None, "partial_configuration"
        try:
            scope = RetailRunScopeConfig.model_validate_json(raw_scope)
        except ValidationError:
            return None, None, "invalid_scope"
        return scope, SecretStr(raw_dsn), None

    @property
    def capture_observability_content(self) -> bool:
        configured = self.compass_observability_capture_content
        return (
            self.compass_observability_environment == "local" if configured is None else configured
        )

    @field_validator("compass_context_scope_profiles")
    @classmethod
    def unique_context_scopes(cls, rules):
        if len({(r.tenant_id, r.workspace_id) for r in rules}) != len(rules):
            raise ValueError("duplicate context scope rule")
        return rules

    @field_validator("compass_semantic_workspaces")
    @classmethod
    def semantic_workspace_ids(cls, value: str) -> str:
        return ",".join(
            sorted({str(UUID(item.strip())) for item in value.split(",") if item.strip()})
        )

    @model_validator(mode="after")
    def execution_limits(self):
        if self.compass_model_max_inflight < self.compass_model_max_concurrency:
            raise ValueError("inflight limit must cover execution capacity")
        if self.compass_langfuse_base_url:
            url = urlsplit(self.compass_langfuse_base_url)
            local = url.hostname in {"localhost", "127.0.0.1", "langfuse-web"}
            if (
                url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in {"", "/"}
                or not (url.scheme == "https" or url.scheme == "http" and local)
            ):
                raise ValueError("Invalid observability service URL")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
