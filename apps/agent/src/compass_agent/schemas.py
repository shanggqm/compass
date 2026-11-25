from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PrivateAttr,
    RootModel,
    SecretStr,
    StringConstraints,
    field_serializer,
    model_validator,
)


class Health(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    service: Literal["compass-agent"] = "compass-agent"


class DemoOverview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"] = "1"
    source: Literal["synthetic-fixture"] = "synthetic-fixture"
    currency: Literal["CNY"] = "CNY"
    start_date: date
    end_date: date
    revenue_minor: int = Field(ge=0)
    orders: int = Field(ge=0)
    days: int = Field(ge=1)


# Feature 993 HTTP contracts. Identity is carried separately by the trusted BFF.


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlatformScope(StrictModel):
    kind: Literal["platform"]


class TenantScope(StrictModel):
    kind: Literal["tenant"]
    tenant_id: UUID
    workspace_id: UUID


class TrustedContext(StrictModel):
    actor_user_id: UUID
    scope: Annotated[PlatformScope | TenantScope, Field(discriminator="kind")]
    purpose: Literal[
        "manage",
        "test",
        "chat",
        "module_read",
        "knowledge_manage",
        "data_manage",
        "mcp_manage",
        "runtime_diagnostics",
        "runtime_usage",
        "schedule_manage",
        "schedule_owner",
        "schedule_run",
        "promptops_manage",
    ]


class RetailConnectionFields(StrictModel):
    host: str = Field(min_length=1, max_length=253, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.-]*$")
    port: int = Field(strict=True, ge=1, le=65535)
    database_name: str = Field(pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    role: str = Field(pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    sslmode: Literal["disable", "prefer", "require", "verify-full"]
    retail_tenant_id: int = Field(strict=True, ge=1, le=9007199254740991)


class RetailConnectionSave(RetailConnectionFields):
    revision: int = Field(strict=True, ge=0, lt=2147483647)
    password: SecretStr | None = Field(default=None, min_length=1, max_length=1024)


class RetailConnectionView(RetailConnectionFields):
    revision: int = Field(ge=1)
    password_configured: Literal[True] = True


class RetailConnectionStatus(StrictModel):
    connection: RetailConnectionView | None


class RetailConnectionTest(StrictModel):
    revision: int = Field(ge=1)
    status: Literal["ok", "connection_failed", "identity_mismatch", "unavailable"]


class DataConnectionFields(StrictModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    engine: Literal["postgresql", "mysql"]
    host: str = Field(min_length=1, max_length=253, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.-]*$")
    port: int = Field(strict=True, ge=1, le=65535)
    database_name: str = Field(min_length=1, max_length=64, pattern=r"^[^\x00-\x1f\x7f]+$")
    role: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")
    sslmode: Literal["disable", "prefer", "require", "verify-full"]
    retail_tenant_id: int | None = Field(default=None, strict=True, ge=1, le=9007199254740991)


class DataConnectionSave(DataConnectionFields):
    revision: int = Field(strict=True, ge=0, lt=2147483647)
    password: SecretStr | None = Field(default=None, min_length=1, max_length=1024)


class DataConnectionView(DataConnectionFields):
    id: UUID
    revision: int = Field(ge=1)
    password_configured: Literal[True] = True


class DataConnectionList(StrictModel):
    items: list[DataConnectionView]


class Capability(StrictModel):
    state: Literal["supported", "unsupported", "unknown"]
    reason: str


class ModelCapabilities(StrictModel):
    sync: Capability
    stream: Capability
    tools: Capability
    thinking: Capability
    json_output: Capability
    schema_output: Capability
    reasoning_efforts: list[str]


class ModelTokenLimits(StrictModel):
    model_config = ConfigDict(frozen=True)

    context_window_tokens: int | None = Field(default=None, gt=0, le=9007199254740991)
    max_input_tokens: int | None = Field(default=None, gt=0, le=9007199254740991)
    max_output_tokens: int | None = Field(default=None, gt=0, le=9007199254740991)


class CatalogTokenLimits(StrictModel):
    model_config = ConfigDict(frozen=True)

    catalog_key: str
    sdk_version: str
    limits: ModelTokenLimits


class GenerationDefaults(StrictModel):
    model_config = ConfigDict(frozen=True)

    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    top_p: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class GenerationProfileInput(StrictModel):
    limits: ModelTokenLimits = Field(default_factory=ModelTokenLimits)
    defaults: GenerationDefaults = Field(default_factory=GenerationDefaults)
    import_catalog: bool = False


class GenerationProfile(StrictModel):
    model_config = ConfigDict(frozen=True)

    limits: ModelTokenLimits = Field(default_factory=ModelTokenLimits)
    defaults: GenerationDefaults = Field(default_factory=GenerationDefaults)
    catalog_snapshot: CatalogTokenLimits | None = None

    def resolve_limits(self) -> ModelTokenLimits:
        base = self.catalog_snapshot.limits if self.catalog_snapshot else ModelTokenLimits()
        return base.model_copy(update=self.limits.model_dump(exclude_unset=True))


class GenerationProfileView(GenerationProfile):
    effective_limits: ModelTokenLimits
    limit_sources: dict[str, Literal["override", "litellm", "unknown"]]

    @field_serializer("limits")
    def serialize_overrides(self, limits: ModelTokenLimits) -> dict:
        return limits.model_dump(exclude_unset=True)


class AvailableGeneration(StrictModel):
    binding_version: int
    limits: ModelTokenLimits
    default_max_tokens: int
    application_max_output_tokens: int = 32768
    supported_parameters: list[str] | None = None


class CatalogItem(StrictModel):
    id: str
    vendor: str
    api_model: str
    kind: Literal["llm", "decision", "unsupported"]
    adapter_supported: bool
    chat_supported: bool = False
    source: Literal["litellm", "project_verified", "systemone"]
    base_url: str | None = None
    capabilities: ModelCapabilities
    token_limits: CatalogTokenLimits | None = None


class CatalogPage(StrictModel):
    items: list[CatalogItem]
    total: int
    page: int
    page_size: int
    sdk_version: str


class ProviderCreate(StrictModel):
    operation_id: UUID
    catalog_id: str = Field(min_length=1, max_length=200)
    api_model: str = Field(min_length=1, max_length=200)
    alias: str = Field(min_length=1, max_length=80)
    base_url: str = Field(default="", max_length=2048)
    api_key: str = Field(default="", max_length=4096, repr=False)
    connection_params: dict | None = Field(default=None, repr=False)
    generation_profile: GenerationProfileInput | None = None
    enabled: bool = False
    chat_visible: bool = False


class ProviderPatch(StrictModel):
    operation_id: UUID
    expected_version: int = Field(ge=1)
    catalog_id: str | None = Field(default=None, min_length=1, max_length=200)
    api_model: str | None = Field(default=None, min_length=1, max_length=200)
    alias: str | None = Field(default=None, min_length=1, max_length=80)
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)
    connection_params: dict | None = Field(default=None, repr=False)
    generation_profile: GenerationProfileInput | None = None
    enabled: bool | None = None
    chat_visible: bool | None = None


class SavedProvider(StrictModel):
    provider_id: UUID
    version: int
    operation_id: UUID


class TestSummary(StrictModel):
    call_id: UUID
    status: str
    binding_version: int
    started_at: datetime
    capabilities: list[str]
    stale: bool


class ProviderView(StrictModel):
    id: UUID
    catalog_id: str
    vendor: str
    api_model: str
    alias: str
    kind: Literal["llm", "decision"]
    base_url: str
    has_api_key: bool
    has_connection_params: bool = False
    enabled: bool
    chat_visible: bool
    chat_supported: bool
    version: int
    binding_version: int
    capabilities: ModelCapabilities
    generation_profile: GenerationProfileView | None = None
    latest_test: TestSummary | None = None
    created_at: datetime
    updated_at: datetime


class ProviderPage(StrictModel):
    items: list[ProviderView]
    total: int
    page: int
    page_size: int


class AvailableModel(StrictModel):
    id: UUID
    alias: str
    api_model: str
    kind: Literal["llm"]
    capabilities: ModelCapabilities
    generation: AvailableGeneration | None = None


class AvailableModels(StrictModel):
    items: list[AvailableModel]


class ModelChangeView(StrictModel):
    operation_id: UUID
    provider_id: UUID
    actor_user_id: UUID
    before_version: int | None
    after_version: int
    changed_fields: list[str]
    created_at: datetime


class ModelChangePage(StrictModel):
    items: list[ModelChangeView]
    total: int
    page: int
    page_size: int


class ToolFunction(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
    description: str = Field(default="", max_length=2000)
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})


class ToolDefinition(StrictModel):
    type: Literal["function"] = "function"
    function: ToolFunction


class ToolCallFunction(StrictModel):
    name: str
    arguments: str


class ToolCall(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    type: Literal["function"] = "function"
    function: ToolCallFunction


class ChatMessage(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def validate_role_fields(self):
        if self.role == "tool" and (not self.tool_call_id or self.content is None):
            raise ValueError("tool message requires content and tool_call_id")
        if self.role != "tool" and self.tool_call_id is not None:
            raise ValueError("tool_call_id requires tool role")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("tool_calls requires assistant role")
        if self.content is None and not self.tool_calls:
            raise ValueError("message requires content or tool calls")
        return self


RUN_CONTEXT_MESSAGES = 512
TOOL_CATALOG_LIMIT = 32


class GenerateInput(StrictModel):
    kind: Literal["generate"] = "generate"
    messages: list[ChatMessage] = Field(min_length=1, max_length=RUN_CONTEXT_MESSAGES)
    stream: bool = False
    thinking: bool = False
    reasoning_effort: Literal["low", "medium", "high", "max"] | None = None
    tools: list[ToolDefinition] = Field(default_factory=list, max_length=TOOL_CATALOG_LIMIT)
    output_format: Literal["text", "json", "schema"] = "text"
    output_schema: dict | None = None
    max_tokens: int = Field(default=8192, ge=1, le=32768)
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    top_p: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    continuation_token: str | None = Field(default=None, max_length=1048576, repr=False)

    @model_validator(mode="after")
    def validate_options(self):
        if not self.thinking and self.reasoning_effort is not None:
            raise ValueError("reasoning_effort requires thinking")
        if (self.output_format == "schema") != (self.output_schema is not None):
            raise ValueError("output_schema is required only for schema mode")
        return self


class DecisionQuestion(StrictModel):
    type: Literal["choice", "noul", "score"]
    instructions: str | dict | list = ""
    criteria: dict[str, str | dict | list | None] | list[str | dict | list] | None = None

    @model_validator(mode="after")
    def validate_criteria(self):
        if self.type == "choice" and (
            not isinstance(self.criteria, dict) or not 1 <= len(self.criteria) <= 255
        ):
            raise ValueError("choice requires 1..255 named criteria")
        if self.type == "score" and (
            not isinstance(self.criteria, list) or not 2 <= len(self.criteria) <= 255
        ):
            raise ValueError("score requires 2..255 ordered criteria")
        if (
            self.type == "noul"
            and self.criteria is not None
            and (not isinstance(self.criteria, dict) or set(self.criteria) != {"true", "false"})
        ):
            raise ValueError("noul criteria must contain true and false")
        return self


class DecisionInput(StrictModel):
    kind: Literal["decision"] = "decision"
    state: str | dict | list
    questions: dict[str, DecisionQuestion] = Field(min_length=1, max_length=16)
    stream: Literal[False] = False


class TestCallInput(StrictModel):
    call_id: UUID
    input: Annotated[GenerateInput | DecisionInput, Field(discriminator="kind")]


class MainModelSelection(StrictModel):
    mode: Literal["fixed", "auto"]
    provider_id: UUID | None = None

    @model_validator(mode="after")
    def validate_selection(self):
        if (self.mode == "fixed") != (self.provider_id is not None):
            raise ValueError("fixed requires provider_id; auto must omit it")
        return self


# Feature 989: a single, versioned Agent Run stream contract.  These models stay
# top-level so the repository contract exporter can discover every named branch.

RUN_STREAM_PROTOCOL: Literal["compass.run"] = "compass.run"
RUN_STREAM_VERSION: Literal[1] = 1
DEFAULT_RUN_TIMEOUT_SECONDS = 600
RUN_STREAM_LIMITS = {
    "frameBytes": 256 * 1024,
    "streamBytes": 4 * 1024 * 1024,
    "events": 4096,
    "batchItems": 16,
    "batchBytes": 32 * 1024,
    "batchWaitMs": 24,
    "heartbeatMs": 15_000,
    "extensionBytes": 4 * 1024,
    "toolInputBytes": 8 * 1024,
    "toolResultBytes": 4 * 1024,
    "jsonDepth": 20,
    "answerBytes": 32 * 1024,
    "fileBytes": 128 * 1024,
    "fileRows": 200,
    "steps": 100,
    "timeoutSeconds": DEFAULT_RUN_TIMEOUT_SECONDS,
    "toolsPerStep": 4,
    "executionNodes": 768,
    "executionDepth": 12,
}

RunUUID = Annotated[str, Field(pattern=r"^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$")]
RunToolId = Annotated[str, Field(min_length=1, max_length=200)]
RunDay = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
RunRowCount = Annotated[int, Field(ge=1, le=RUN_STREAM_LIMITS["fileRows"])]

RunToolError = Literal[
    "unknown_tool",
    "invalid_parameters",
    "retail_unavailable",
    "retail_query_failed",
    "retail_query_timeout",
    "database_query_failed",
    "database_unavailable",
    "database_query_timeout",
    "result_limit",
]
type ModelInputError = Literal[
    "input_token_limit_exceeded", "output_token_limit_exceeded", "invalid_model_input"
]
RunError = Literal[
    ModelInputError,
    "model_failed",
    "model_rate_limited",
    "runtime_overloaded",
    "run_budget_exhausted",
    "model_incomplete",
    "model_unavailable",
    "run_timeout",
    "context_limit",
    "workflow_unavailable",
    "workflow_failed",
    "tool_catalog_limit",
    "tool_setup_failed",
    "step_limit",
    "internal_error",
]
RunStopCause = Literal["user", "disconnected", "shutdown"]
RunCleanupCause = RunStopCause | Literal["deadline", "execution_error"]
RunTerminalStatus = Literal["completed", "waiting", "failed", "canceled", "cleanup_unconfirmed"]


class RunContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=True)


class AnalysisHistoryTurn(RunContractModel):
    question: str = Field(min_length=1, max_length=8000)
    answer: str = Field(min_length=1, max_length=RUN_STREAM_LIMITS["answerBytes"])


class RunPeriod(RunContractModel):
    start: RunDay
    end: RunDay


class WorkflowPeriods(RunContractModel):
    baseline: RunPeriod
    current: RunPeriod


class WorkflowAssetRef(WorkflowPeriods):
    asset_id: UUID = Field(alias="assetId", strict=False)
    definition_version: Literal[1] = Field(alias="definitionVersion")


type WorkflowEntryContract = Literal[
    "channel_attribution@1", "channel_overview@1", "agent_workflow@1"
]


class WorkflowAssetView(RunContractModel):
    asset_id: UUID = Field(alias="assetId", strict=False)
    name: str = Field(min_length=1, max_length=80)
    status: Literal["active", "disabled"]
    definition_version: Literal[1] = Field(alias="definitionVersion")
    entry_contract: WorkflowEntryContract = Field(alias="entryContract")
    metadata_revision: int = Field(alias="metadataRevision", ge=1)
    script_source: str | None = Field(default=None, alias="scriptSource", max_length=16384)
    description: str | None = Field(default=None, max_length=500)
    parameters: dict | None = None


class WorkflowAssetList(RunContractModel):
    items: list[WorkflowAssetView]
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class WorkflowAssetPatch(RunContractModel):
    metadata_revision: int = Field(alias="metadataRevision", ge=1, lt=2147483647)
    name: str = Field(min_length=1, max_length=80)
    status: Literal["active", "disabled"]


class WorkflowAssetDelete(RunContractModel):
    metadata_revision: int = Field(alias="metadataRevision", ge=1, lt=2147483647)


class WorkflowDraftConfirm(RunContractModel):
    name: str = Field(min_length=1, max_length=80)


class WorkflowDraftView(RunContractModel):
    name: str = Field(min_length=1, max_length=80)
    entry_contract: WorkflowEntryContract = Field(alias="entryContract")
    script_source: str = Field(alias="scriptSource", max_length=16384)
    args: WorkflowPeriods | dict
    summary: str
    description: str | None = Field(default=None, max_length=500)
    parameters: dict | None = None


class RunConstraints(RunContractModel):
    plan: Literal["required"] | None = None
    max_steps: int | None = Field(
        default=None, alias="maxSteps", ge=1, le=RUN_STREAM_LIMITS["steps"]
    )
    timeout_seconds: int | None = Field(
        default=None, alias="timeoutSeconds", ge=1, le=DEFAULT_RUN_TIMEOUT_SECONDS
    )


class AnalysisRunInput(RunContractModel):
    # JSON carries UUID text; keep the rest of the Run contract strictly typed.
    request_id: UUID = Field(alias="requestId", strict=False)
    question: str = Field(min_length=1, max_length=8000)
    history: list[AnalysisHistoryTurn] = Field(default_factory=list, max_length=12)
    selection: MainModelSelection
    workflow_asset_ref: WorkflowAssetRef | None = Field(default=None, alias="workflowAssetRef")
    constraints: RunConstraints | None = None

    @model_validator(mode="after")
    def validate_visible_context_bytes(self):
        if self.constraints and self.workflow_asset_ref:
            raise ValueError("analysis constraints require a direct task")
        # Match the BFF's newline-separated byte accounting before any Run can
        # route, invoke a model, or open a tool connection.
        visible = [
            self.question,
            *(value for turn in self.history for value in (turn.question, turn.answer)),
        ]
        if len("\n".join(visible).encode()) > 32 * 1024:
            raise ValueError("visible context exceeds 32 KiB")
        return self


class ChannelRunParams(RunContractModel):
    dimension: Literal["channel"]
    period: RunPeriod
    compare_to: RunPeriod | None = Field(alias="compareTo")


class ProductRunParams(RunContractModel):
    dimension: Literal["product"]
    period: RunPeriod
    compare_to: RunPeriod | None = Field(alias="compareTo")
    channel_code: Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,24}$")] = Field(alias="channelCode")


RetailRunParams = Annotated[
    ChannelRunParams | ProductRunParams,
    Field(discriminator="dimension"),
]


class RunEmpty(RunContractModel):
    pass


class RunRoutedPayload(RunContractModel):
    provider_id: RunUUID = Field(alias="providerId")
    model_label: Annotated[str, Field(min_length=1, max_length=160)] = Field(alias="modelLabel")
    scene_id: Annotated[str, Field(pattern=r"^SC-[0-9]{2}$")] = Field(alias="sceneId")
    tools_enabled: bool = Field(alias="toolsEnabled")


class StepStartedPayload(RunContractModel):
    step_id: RunUUID = Field(alias="stepId")
    step_index: Annotated[int, Field(ge=1, le=RUN_STREAM_LIMITS["steps"])] = Field(
        alias="stepIndex"
    )
    model_call_id: RunUUID = Field(alias="modelCallId")


class ToolEventKey(RunContractModel):
    step_id: RunUUID = Field(alias="stepId")
    tool_call_id: RunToolId = Field(alias="toolCallId")
    tool_index: Annotated[int, Field(ge=1, le=RUN_STREAM_LIMITS["toolsPerStep"])] = Field(
        alias="toolIndex"
    )


class ToolStartedPayload(ToolEventKey):
    tool_kind: Literal["retail_sales", "database_query", "unavailable"] = Field(alias="toolKind")
    params: RetailRunParams | None
    input_preview: str | None = Field(
        default_factory=lambda: None,
        alias="inputPreview",
        max_length=RUN_STREAM_LIMITS["toolInputBytes"],
    )


class ToolFinishedKey(ToolEventKey):
    result_preview: str | None = Field(
        default_factory=lambda: None,
        alias="resultPreview",
        max_length=RUN_STREAM_LIMITS["toolResultBytes"],
    )


class ToolSucceededPayload(ToolFinishedKey):
    status: Literal["ok"]
    # This counts all returned rows, including a separately retained full result.
    # Inline previews and downloadable demo files keep their own smaller bounds.
    row_count: int = Field(alias="rowCount", ge=1, le=9_007_199_254_740_991)


class ToolEmptyPayload(ToolFinishedKey):
    status: Literal["empty"]
    row_count: Literal[0] = Field(alias="rowCount")


class ToolFailedPayload(ToolFinishedKey):
    status: Literal["error"]
    error_code: RunToolError = Field(alias="errorCode")


ToolFinishedPayload = Annotated[
    ToolSucceededPayload | ToolEmptyPayload | ToolFailedPayload,
    Field(discriminator="status"),
]


class StepFinishedPayload(RunContractModel):
    step_id: RunUUID = Field(alias="stepId")
    status: Literal["completed", "failed", "canceled"]


class AnalysisFileNone(RunContractModel):
    status: Literal["none"]


class AnalysisFileUnavailable(RunContractModel):
    status: Literal["unavailable"]
    code: Literal["file_limit", "file_generation_failed"]


class AnalysisFileReady(RunContractModel):
    status: Literal["ready"]
    step_id: RunUUID = Field(alias="stepId")
    tool_call_id: RunToolId = Field(alias="toolCallId")
    filename: Annotated[
        str,
        Field(pattern=r"^retail-products-[a-f0-9-]{36}\.csv$"),
    ]
    media_type: Literal["text/csv; charset=utf-8"] = Field(alias="mediaType")
    row_count: RunRowCount = Field(alias="rowCount")
    byte_length: Annotated[int, Field(ge=1, le=RUN_STREAM_LIMITS["fileBytes"])] = Field(
        alias="byteLength"
    )
    content_base64: Annotated[
        str,
        Field(
            min_length=4,
            max_length=4 * ((RUN_STREAM_LIMITS["fileBytes"] + 2) // 3),
        ),
    ] = Field(alias="contentBase64")


AnalysisFile = Annotated[
    AnalysisFileNone | AnalysisFileUnavailable | AnalysisFileReady,
    Field(discriminator="status"),
]


class RunArtifact(RunContractModel):
    id: RunUUID
    name: Literal["chart.png"]
    media_type: Literal["image/png"] = Field(alias="mediaType")
    byte_size: int = Field(alias="byteSize", ge=1, le=512 * 1024)
    step_id: RunUUID = Field(alias="stepId")
    tool_call_id: RunToolId = Field(alias="toolCallId")


class RunCompleted(RunContractModel):
    _delivery: Any = PrivateAttr(default=None)
    _citations: Any = PrivateAttr(default=None)
    _answer_acceptance: Any = PrivateAttr(default=None)
    status: Literal["completed"]
    final_answer: Annotated[
        str,
        Field(min_length=1, max_length=RUN_STREAM_LIMITS["answerBytes"]),
    ] = Field(alias="finalAnswer")
    file: AnalysisFile
    artifacts: list[RunArtifact] = Field(default_factory=list, max_length=40)


class BudgetProposal(RunContractModel):
    proposal_id: RunUUID = Field(alias="proposalId")
    revision: Literal[1] = 1
    campaign_key: Literal["classroom-retail"] = Field(alias="campaignKey")
    tenant_id: RunUUID = Field(alias="tenantId")
    workspace_id: RunUUID = Field(alias="workspaceId")
    delta_cent: str = Field(alias="deltaCent", pattern=r"^(?:0|-?[1-9][0-9]{0,19})$")
    summary: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[RunUUID] = Field(alias="evidenceRefs", min_length=1, max_length=2)


class GoalAcceptance(RunContractModel):
    id: str = Field(pattern=r"^a[0-9]{1,2}$")
    text: str = Field(min_length=1, max_length=300)


class GoalContract(RunContractModel):
    objective: str = Field(min_length=1, max_length=2000)
    acceptance: list[GoalAcceptance] = Field(min_length=1, max_length=10)
    constraints: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=10
    )

    @model_validator(mode="after")
    def unique_acceptance(self):
        if len({item.id for item in self.acceptance}) != len(self.acceptance):
            raise ValueError("acceptance IDs must be unique")
        return self


class GoalBudget(RunContractModel):
    max_rounds: int = Field(default=12, alias="maxRounds", ge=1, le=32)
    tokens: int = Field(default=2_000_000, ge=10_000, le=10_000_000)
    cost_microusd: int = Field(default=20_000_000, alias="costMicrousd", ge=1, le=20_000_000)


class GoalProposal(RunContractModel):
    goal_id: RunUUID | None = Field(default=None, alias="goalId")
    expected_revision: int | None = Field(default=None, alias="expectedRevision", ge=1)
    contract: GoalContract
    budget: GoalBudget = Field(default_factory=GoalBudget)


class GoalUsage(RunContractModel):
    rounds: int = Field(ge=0)
    tokens: int = Field(ge=0)
    cost_microusd: int = Field(alias="costMicrousd", ge=0)
    tokens_pct: float = Field(alias="tokensPct", ge=0)
    cost_pct: float = Field(alias="costPct", ge=0)
    overrun: bool


class GoalCounts(RunContractModel):
    open_todos: int = Field(alias="openTodos", ge=0)
    conflicts: int = Field(ge=0)
    stale: int = Field(ge=0)
    reused_calls: int = Field(alias="reusedCalls", ge=0)


class GoalView(RunContractModel):
    goal_id: RunUUID = Field(alias="goalId")
    revision: int = Field(ge=1)
    status: Literal["active", "paused", "blocked", "budget_limited", "completed", "canceled"]
    reason_code: str | None = Field(default=None, alias="reasonCode", max_length=80)
    reason: str | None = Field(default=None, max_length=60)
    contract: GoalContract
    satisfied: list[str] = Field(default_factory=list)
    budget: GoalBudget
    usage: GoalUsage
    counts: GoalCounts


class SessionGoal(RunContractModel):
    goal: GoalView | None
    run_id: RunUUID | None = Field(default=None, alias="runId")


class GoalAction(RunContractModel):
    action: Literal["pause", "resume", "cancel", "interrupt"]
    expected_revision: int = Field(alias="expectedRevision", ge=1)
    request_id: UUID = Field(alias="requestId", strict=False)


class ToolApprovalProposal(RunContractModel):
    goal: GoalProposal | None = None
    proposal_id: RunUUID = Field(alias="proposalId")
    tool_name: str = Field(alias="toolName", min_length=1, max_length=128)
    tool_call_id: RunToolId = Field(alias="toolCallId")
    arguments_digest: str = Field(alias="argumentsDigest", pattern=r"^[0-9a-f]{64}$")
    summary: str = Field(min_length=1, max_length=2000)
    connection_id: RunUUID | None = Field(default=None, alias="connectionId")
    connection_revision: int | None = Field(default=None, alias="connectionRevision", ge=1)
    schema_hash: str | None = Field(default=None, alias="schemaHash", pattern=r"^[0-9a-f]{64}$")


class RunWaiting(RunContractModel):
    status: Literal["waiting"]
    run_id: RunUUID = Field(alias="runId")
    wait_id: RunUUID = Field(alias="waitId")
    node_key: Literal["tool_approval"] = Field(default="tool_approval", alias="nodeKey")
    continuation: Literal["execute_tool"] = "execute_tool"
    proposal: ToolApprovalProposal


class RunFailed(RunContractModel):
    status: Literal["failed"]
    error_code: RunError = Field(alias="errorCode")


class RunCanceled(RunContractModel):
    status: Literal["canceled"]
    reason: RunStopCause
    cleanup: Literal["confirmed"]


class RunCleanupUnconfirmed(RunContractModel):
    status: Literal["cleanup_unconfirmed"]
    reason: RunCleanupCause
    cleanup: Literal["unconfirmed"]


RunResult = Annotated[
    RunCompleted | RunWaiting | RunFailed | RunCanceled | RunCleanupUnconfirmed,
    Field(discriminator="status"),
]


RunTodoId = Annotated[str, Field(pattern=r"^t[1-9][0-9]{0,2}$")]
RunAutonomyCode = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")]
RunMoney = Annotated[str, Field(pattern=r"^(?:0|-?[1-9][0-9]{0,19})$")]


class RunTodoItem(RunContractModel):
    id: RunTodoId
    goal: str = Field(min_length=1, max_length=200)
    status: Literal["pending", "failed", "verified", "skipped"]
    depends_on: list[RunTodoId] = Field(alias="dependsOn", max_length=5)
    optional: bool
    period: RunPeriod | None = None
    compare_to: RunPeriod | None = Field(default=None, alias="compareTo")
    channel_code: str | None = Field(
        default=None, alias="channelCode", pattern=r"^[A-Z0-9_]{1,24}$"
    )
    reason: Literal["out_of_scope", "duplicate_optional"] | None = None


class RunAutonomyAction(RunContractModel):
    step_id: RunUUID = Field(alias="stepId")
    action_index: int = Field(alias="actionIndex", ge=1, le=4)
    action: Literal[
        "replace", "select", "complete", "skip", "retail", "database", "submit", "unknown"
    ]
    code: RunAutonomyCode
    todo_id: RunTodoId | None = Field(default=None, alias="todoId")
    level: Literal["step", "run"] | None = None
    layer: Literal["rule", "schema", "tool_result"] | None = None
    plan_version: int = Field(
        alias="planVersion", ge=0, le=RUN_STREAM_LIMITS["steps"] * RUN_STREAM_LIMITS["toolsPerStep"]
    )
    selected_todo_id: RunTodoId | None = Field(alias="selectedTodoId")
    items: list[RunTodoItem] = Field(max_length=6)


class RunAutonomyUsage(RunContractModel):
    limit: int = Field(ge=0)
    used: int = Field(ge=0)
    remaining: int = Field(ge=0)


class RunAutonomyBudget(RunContractModel):
    steps: RunAutonomyUsage
    tools: RunAutonomyUsage
    tokens: RunAutonomyUsage
    cost_microusd: RunAutonomyUsage = Field(alias="costMicrousd")
    attempts: int = Field(ge=0)
    unknown_attempts: int = Field(alias="unknownAttempts", ge=0)


class RunVerifiedSummary(RunContractModel):
    query_id: RunUUID = Field(alias="queryId")
    step_id: RunUUID = Field(alias="stepId")
    tool_call_id: RunToolId = Field(alias="toolCallId")
    dimension: Literal["channel", "product"]
    period: RunPeriod
    compare_to: RunPeriod | None = Field(alias="compareTo")
    channel_code: str | None = Field(alias="channelCode", pattern=r"^[A-Z0-9_]{1,24}$")
    row_count: RunRowCount = Field(alias="rowCount")
    paid_cent: RunMoney = Field(alias="paidCent")
    compare_paid_cent: RunMoney | None = Field(alias="comparePaidCent")


class RunAutonomyResult(RunContractModel):
    outcome: Literal[
        "completed",
        "waiting",
        "failed",
        "budget_exhausted",
        "human_required",
        "canceled",
        "cleanup_unconfirmed",
    ]
    reason: RunAutonomyCode
    plan_version: int = Field(
        alias="planVersion", ge=0, le=RUN_STREAM_LIMITS["steps"] * RUN_STREAM_LIMITS["toolsPerStep"]
    )
    first_plan_step: int | None = Field(alias="firstPlanStep", ge=1, le=RUN_STREAM_LIMITS["steps"])
    budget: RunAutonomyBudget
    partial: list[RunVerifiedSummary] = Field(max_length=2)


class RunWorkflowAsset(RunContractModel):
    asset_id: RunUUID = Field(alias="assetId")
    name: str = Field(min_length=1, max_length=80)
    definition_version: Literal[1] = Field(alias="definitionVersion")


class RunWorkflowProgress(RunContractModel):
    source: Literal["dynamic", "fixed", "skill"]
    key: str = Field(pattern=r"^[a-z0-9_:@-]{1,72}$")
    status: Literal["running", "completed", "failed", "canceled", "waiting"]
    detail: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,80}$")
    asset: RunWorkflowAsset | None = None


class RunToolPreparation(RunContractModel):
    step_id: RunUUID = Field(alias="stepId")
    tool_index: int = Field(alias="toolIndex", ge=1, le=RUN_STREAM_LIMITS["toolsPerStep"])
    status: Literal["preparing", "prepared"]


class ExecutionOrigin(RunContractModel):
    kind: Literal["builtin", "mcp"]
    tool_name: str | None = Field(default_factory=lambda: None, alias="toolName", max_length=200)
    server_id: str | None = Field(default_factory=lambda: None, alias="serverId", max_length=120)
    server_name: str | None = Field(
        default_factory=lambda: None, alias="serverName", max_length=120
    )


class ExecutionEnvironment(RunContractModel):
    kind: Literal["service", "quickjs", "sandbox"]
    runtime: str | None = Field(default_factory=lambda: None, max_length=80)
    environment_id: str | None = Field(
        default_factory=lambda: None, alias="environmentId", max_length=120
    )


class ExecutionWorkflow(RunContractModel):
    mode: Literal["generated", "reused", "fixed"]
    asset_id: RunUUID | None = Field(default_factory=lambda: None, alias="assetId")
    name: str | None = Field(default_factory=lambda: None, max_length=80)
    version: int | None = Field(default_factory=lambda: None, ge=1)


class ExecutionSkill(RunContractModel):
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=40)
    stage: Literal["load", "script"]


class ExecutionSubAgent(RunContractModel):
    task_name: str | None = Field(default_factory=lambda: None, alias="taskName", max_length=40)
    delivery_status: Literal["completed", "partial", "blocked", "failed"] | None = Field(
        default_factory=lambda: None, alias="deliveryStatus"
    )
    name: str = Field(min_length=1, max_length=120)
    child_run_id: RunUUID | None = Field(default_factory=lambda: None, alias="childRunId")


class ExecutionDisplay(RunContractModel):
    policy: Literal["summary", "preview"] = Field(default_factory=lambda: "summary")
    input_preview: str | None = Field(
        default_factory=lambda: None, alias="inputPreview", max_length=1024
    )
    output_preview: str | None = Field(
        default_factory=lambda: None, alias="outputPreview", max_length=1024
    )
    language: Literal["text", "json", "sql", "javascript", "python"] = Field(
        default_factory=lambda: "text"
    )


class ExecutionDetail(RunContractModel):
    thinking: str | None = Field(default_factory=lambda: None, max_length=32 * 1024)
    narration: str | None = Field(default_factory=lambda: None, max_length=32 * 1024)
    input: str | None = Field(default_factory=lambda: None, max_length=32 * 1024)
    output: str | None = Field(default_factory=lambda: None, max_length=32 * 1024)


class RunExecutionNode(RunContractModel):
    """Execution snapshot with optional detail for currently authorized viewers."""

    id: RunUUID
    parent_id: RunUUID | None = Field(default_factory=lambda: None, alias="parentId")
    revision: int = Field(ge=1, le=1024)
    kind: Literal["model", "narration", "tool", "workflow", "skill", "subagent", "group"]
    status: Literal[
        "preparing", "running", "completed", "empty", "failed", "canceled", "waiting", "incomplete"
    ]
    title: str = Field(min_length=1, max_length=160)
    summary: str | None = Field(default_factory=lambda: None, max_length=512)
    started_at_ms: int = Field(alias="startedAtMs", ge=0)
    ended_at_ms: int | None = Field(default_factory=lambda: None, alias="endedAtMs", ge=0)
    step_id: RunUUID | None = Field(default_factory=lambda: None, alias="stepId")
    model_call_id: RunUUID | None = Field(default_factory=lambda: None, alias="modelCallId")
    tool_call_id: RunToolId | None = Field(default_factory=lambda: None, alias="toolCallId")
    origin: ExecutionOrigin | None = Field(default_factory=lambda: None)
    executor: ExecutionEnvironment | None = Field(default_factory=lambda: None)
    workflow: ExecutionWorkflow | None = Field(default_factory=lambda: None)
    skill: ExecutionSkill | None = Field(default_factory=lambda: None)
    subagent: ExecutionSubAgent | None = Field(default_factory=lambda: None)
    scheduling: Literal["sequential", "parallel"] | None = Field(default_factory=lambda: None)
    depends_on: list[RunUUID] = Field(default_factory=list, alias="dependsOn", max_length=16)
    display: ExecutionDisplay = Field(default_factory=ExecutionDisplay)
    detail: ExecutionDetail | None = Field(default_factory=lambda: None)

    @model_validator(mode="after")
    def validate_node(self):
        if self.parent_id == self.id or self.id in self.depends_on:
            raise ValueError("execution node cannot reference itself")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("duplicate execution dependency")
        if (
            self.origin
            and self.origin.kind == "mcp"
            and not (self.kind == "tool" and self.origin.server_id and self.origin.tool_name)
        ):
            raise ValueError("MCP calls require server and tool identity")
        if self.scheduling is not None and self.kind != "group":
            raise ValueError("scheduling belongs to an execution group")
        for kind in ("workflow", "skill", "subagent"):
            if getattr(self, kind) is not None and self.kind != kind:
                raise ValueError("execution metadata must match kind")
        if self.display.policy == "summary" and (
            self.display.input_preview is not None or self.display.output_preview is not None
        ):
            raise ValueError("summary policy forbids previews")
        if self.ended_at_ms is not None and self.ended_at_ms < self.started_at_ms:
            raise ValueError("execution time order")
        if (self.status in {"preparing", "running"}) != (self.ended_at_ms is None):
            raise ValueError("execution end time must match status")
        return self


class RunExtensionPayload(RunContractModel):
    name: Annotated[
        str,
        Field(max_length=96, pattern=r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+$"),
    ]
    version: int = Field(ge=1, le=65535)
    required: bool
    data: dict[str, JsonValue]


class RunStarted(RunContractModel):
    type: Literal["run.started"]
    payload: RunEmpty


class RunRouted(RunContractModel):
    type: Literal["run.routed"]
    payload: RunRoutedPayload


class StepStarted(RunContractModel):
    type: Literal["step.started"]
    payload: StepStartedPayload


class ToolStarted(RunContractModel):
    type: Literal["tool.started"]
    payload: ToolStartedPayload


class ToolFinished(RunContractModel):
    type: Literal["tool.finished"]
    payload: ToolFinishedPayload


class StepFinished(RunContractModel):
    type: Literal["step.finished"]
    payload: StepFinishedPayload


class StreamExtension(RunContractModel):
    type: Literal["stream.extension"]
    payload: RunExtensionPayload


RunProgressEvent = Annotated[
    RunRouted | StepStarted | ToolStarted | ToolFinished | StepFinished | StreamExtension,
    Field(discriminator="type"),
]


class StreamBatch(RunContractModel):
    type: Literal["stream.batch"]
    payload: list[RunProgressEvent] = Field(
        min_length=1,
        max_length=RUN_STREAM_LIMITS["batchItems"],
    )


class RunFinished(RunContractModel):
    type: Literal["run.finished"]
    payload: RunResult
    duration_ms: int | None = Field(default_factory=lambda: None, alias="durationMs", ge=0)


RunStreamEvent = Annotated[
    RunStarted
    | RunRouted
    | StepStarted
    | ToolStarted
    | ToolFinished
    | StepFinished
    | StreamExtension
    | StreamBatch
    | RunFinished,
    Field(discriminator="type"),
]


class RunPacket(RunContractModel):
    protocol: Literal["compass.run"]
    version: Literal[1]
    request_id: RunUUID = Field(alias="requestId")
    run_id: RunUUID = Field(alias="runId")
    seq: int = Field(ge=1, le=RUN_STREAM_LIMITS["events"])
    event: RunStreamEvent


class AnalysisCancelAccepted(RunContractModel):
    run_id: RunUUID = Field(alias="runId")
    status: Literal["accepted"]


class AnalysisCancelAlreadyTerminal(RunContractModel):
    run_id: RunUUID = Field(alias="runId")
    status: Literal["already_terminal"]
    terminal_status: RunTerminalStatus = Field(alias="terminalStatus")


class AnalysisCancelAck(
    RootModel[
        Annotated[
            AnalysisCancelAccepted | AnalysisCancelAlreadyTerminal,
            Field(discriminator="status"),
        ]
    ]
):
    pass


class ChatCallInput(StrictModel):
    call_id: UUID
    provider_id: UUID | None = None
    main_model: MainModelSelection | None = None
    input: GenerateInput

    @model_validator(mode="after")
    def validate_selection(self):
        if (self.provider_id is None) == (self.main_model is None):
            raise ValueError("provide either provider_id or main_model")
        return self

    def selection(self):
        return self.main_model or MainModelSelection(mode="fixed", provider_id=self.provider_id)


class ModelUsage(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class OutputValidation(StrictModel):
    status: Literal["not_requested", "valid", "invalid"] = "not_requested"
    mode: Literal["text", "json", "schema", "decision"] = "text"
    paths: list[str] = Field(default_factory=list)


class DecisionAnswer(StrictModel):
    type: Literal["choice", "noul", "score"]
    choice: str | None = None
    noul: float | None = Field(default=None, ge=0, le=1)
    score: float | None = None
    probabilities: dict[str, float] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    legend: dict[str, str | dict | list] | None = None


class AttemptSummary(StrictModel):
    attempt_id: UUID
    number: int = Field(ge=1, le=2)
    provider_id: UUID
    config_version: int = Field(ge=1)
    binding_version: int = Field(ge=1)
    started_at: datetime
    duration_ms: int = Field(ge=0)
    status: Literal["completed", "failed", "canceled", "incomplete"]
    error_code: str | None = Field(default=None, max_length=80)
    upstream_request_id: str | None = Field(default=None, max_length=200)
    upstream_outcome_unknown: bool = False
    sent: bool = False
    cleanup_confirmed: bool = True
    usage: ModelUsage = Field(default_factory=ModelUsage)


class ExecutionSummary(StrictModel):
    version: Literal[1] = 1
    max_attempts: Literal[1, 2]
    queue_wait_ms: int = Field(ge=0)
    stop_reason: Literal[
        "completed",
        "non_retryable",
        "attempts_exhausted",
        "deadline_exceeded",
        "canceled",
        "overloaded",
        "cleanup_unconfirmed",
    ]
    attempts: list[AttemptSummary] = Field(default_factory=list, max_length=2)


class CallOutcome(StrictModel):
    execution: ExecutionSummary | None = None
    call_id: UUID
    provider_id: UUID
    config_version: int
    status: Literal["completed", "failed", "canceled", "incomplete"]
    duration_ms: int
    usage: ModelUsage = Field(default_factory=ModelUsage)
    error_code: str | None = None
    upstream_outcome_unknown: bool = False
    requested_model: str
    response_model: str | None = None
    upstream_request_id: str | None = None
    finish_reason: str | None = None
    validation: OutputValidation = Field(default_factory=OutputValidation)


class CallResponse(CallOutcome):
    kind: Literal["generate", "decision"]
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    answers: dict[str, DecisionAnswer] | None = None
    raw_response: dict | None = Field(default=None, repr=False)
    continuation_token: str | None = Field(default=None, repr=False)


class CallRecordView(StrictModel):
    execution: ExecutionSummary | None = None
    call_id: UUID
    provider_id: UUID
    config_version: int
    binding_version: int
    purpose: Literal["test", "chat"]
    status: Literal["running", "completed", "failed", "canceled", "incomplete"]
    requested_model: str
    response_model: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    usage: ModelUsage
    error_code: str | None
    upstream_outcome_unknown: bool


class StartEvent(StrictModel):
    type: Literal["start"] = "start"
    call_id: UUID
    provider_id: UUID
    config_version: int


class TextDeltaEvent(StrictModel):
    type: Literal["text_delta"] = "text_delta"
    call_id: UUID
    text: str


class ToolDeltaEvent(StrictModel):
    type: Literal["tool_delta"] = "tool_delta"
    call_id: UUID
    index: int
    id: str | None = None
    name: str | None = None
    arguments: str = ""


class ReasoningMetaEvent(StrictModel):
    type: Literal["reasoning_meta"] = "reasoning_meta"
    call_id: UUID
    thinking: bool
    reasoning_effort: str | None = None


class FinalEvent(CallOutcome):
    type: Literal["final"] = "final"
    continuation_token: str | None = Field(default=None, repr=False)


class ModelEvent(
    RootModel[
        Annotated[
            StartEvent | TextDeltaEvent | ToolDeltaEvent | ReasoningMetaEvent | FinalEvent,
            Field(discriminator="type"),
        ]
    ]
):
    pass


class ModelErrorDetail(StrictModel):
    code: str
    message: str
    requestId: str
    currentVersion: int | None = None
    reasonCode: str | None = None


class ModelErrorResponse(StrictModel):
    error: ModelErrorDetail


# Feature 992: classification has exactly two business fields.
class Intent(StrictModel):
    category_id: str = Field(max_length=80)
    scene_id: str = Field(max_length=20)


class RoutingFeatures(StrictModel):
    task_type: str
    difficulty: Literal["L1", "L2", "L3", "undetermined"]
    response_mode: Literal["answer", "clarify", "refuse", "handoff"]
    required_capabilities: list[str] = Field(default_factory=list)


class ModelRoute(StrictModel):
    mode: Literal["fixed", "auto"]
    provider_id: UUID
    model: str
    reason: str
    dictionary_version: str
    routing_version: str


class RoutingMetrics(StrictModel):
    classifier_source: Literal["jev", "flash"] | None = None
    fallback_reason: str | None = None
    classification_calls: list[CallOutcome] = Field(default_factory=list, max_length=2)
    duration_ms: int = 0
    usage_complete: bool = False


class RoutedCallResponse(StrictModel):
    call_id: UUID
    status: Literal["completed", "failed", "canceled", "incomplete"]
    error_code: str | None = None
    intent: Intent | None = None
    features: RoutingFeatures | None = None
    routing: ModelRoute | None = None
    result: CallResponse | None = None
    metrics: RoutingMetrics = Field(default_factory=RoutingMetrics)


class ClassificationEvent(StrictModel):
    type: Literal["classification_status"] = "classification_status"
    call_id: UUID
    stage: Literal["jev", "flash"]


class RouteSelectedEvent(StrictModel):
    type: Literal["route_selected"] = "route_selected"
    call_id: UUID
    intent: Intent
    features: RoutingFeatures
    routing: ModelRoute


type ReliabilityScenario = Literal[
    "success",
    "rate_limit",
    "unavailable",
    "exhausted",
    "authentication",
    "timeout",
    "interrupted",
    "schema_invalid",
    "cancel",
    "concurrency",
    "cleanup_failed",
]


class ShowcaseScenario(StrictModel):
    id: ReliabilityScenario
    title: str
    description: str
    expected: str


class ShowcaseCatalog(StrictModel):
    items: list[ShowcaseScenario]


class ShowcaseInput(StrictModel):
    call_id: UUID
    scenario: ReliabilityScenario


class GateSnapshot(StrictModel):
    capacity: int = Field(ge=1, le=64)
    max_inflight: int = Field(ge=1, le=256)
    active: int = Field(ge=0)
    inflight: int = Field(ge=0)
    quarantined: int = Field(ge=0)
    available: int = Field(ge=0)
    peak_active: int = Field(ge=0)
    peak_inflight: int = Field(ge=0)


class ShowcaseCall(StrictModel):
    number: int = Field(ge=1, le=20)
    result: CallResponse
    preview: str = Field(max_length=8192)
    upstream_requests: int = Field(ge=0, le=2)
    accepted: bool


class ShowcaseReport(StrictModel):
    call_id: UUID
    scenario: ReliabilityScenario
    simulation: Literal[True] = True
    passed: bool
    duration_ms: int = Field(ge=0)
    calls: list[ShowcaseCall] = Field(max_length=20)
    capacity: GateSnapshot


class ShowcaseStart(StrictModel):
    type: Literal["start"] = "start"
    call_id: UUID


class ShowcaseProgress(StrictModel):
    type: Literal["progress"] = "progress"
    call_id: UUID
    number: int = Field(ge=1, le=20)
    message: str = Field(max_length=200)
    text: str = Field(default="", max_length=8192)
    capacity: GateSnapshot


class ShowcaseFinal(ShowcaseReport):
    type: Literal["final"] = "final"


class ShowcaseEvent(
    RootModel[
        Annotated[ShowcaseStart | ShowcaseProgress | ShowcaseFinal, Field(discriminator="type")]
    ]
):
    pass


class ObservabilityStatus(StrictModel):
    instance_id: str
    environment: str
    enabled: bool
    configured: bool
    sample_ratio: float
    queue_size: int = Field(ge=0)
    queue_capacity: int = Field(ge=1)
    dropped: int = Field(ge=0)
    export_failures: int = Field(ge=0)
    instrumentation_errors: int = Field(ge=0)
    last_export: Literal["never", "success", "failure"]


class ModelComparisonRow(StrictModel):
    provider_id: UUID
    model: str
    data_state: Literal["observed", "empty", "unavailable"]
    logical_count: int | None = None
    completed: int | None = None
    failed: int | None = None
    canceled: int | None = None
    incomplete: int | None = None
    success_rate: float | None = None
    mean_duration_ms: float | None = None
    attempt_count: int | None = None
    known_input_tokens: int | None = None
    known_output_tokens: int | None = None
    unknown_usage_attempts: int | None = None
    known_estimated_usd: float | None = None
    estimated_usd: float | None = None
    unknown_cost_attempts: int | None = None


class ModelComparison(StrictModel):
    from_timestamp: datetime
    to_timestamp: datetime
    environment: str
    sample_ratio: float
    items: list[ModelComparisonRow] = Field(max_length=2)


class McpToolSelection(StrictModel):
    name: str = Field(min_length=1, max_length=256)
    effect: Literal["read_only", "local_artifact", "external_write", "unknown"] = "unknown"
    approval: Literal["none", "required"] = "required"

    @model_validator(mode="after")
    def safe_policy(self):
        if self.effect in {"external_write", "unknown"} and self.approval != "required":
            raise ValueError("external and unknown effects require approval")
        return self


class McpConnectionSave(StrictModel):
    revision: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=80)
    endpoint: str = Field(min_length=1, max_length=2048)
    token: SecretStr | None = None
    enabled: bool = True
    required: bool = False
    max_inflight: int = Field(default=1, ge=1, le=2)
    selected_tools: Literal["all"] | list[str] = "all"
    tool_policies: list[McpToolSelection] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def unique_tools(self):
        if len({tool.name for tool in self.tool_policies}) != len(self.tool_policies):
            raise ValueError("duplicate tool policy")
        if isinstance(self.selected_tools, list) and (
            len(self.selected_tools) > 16
            or len(set(self.selected_tools)) != len(self.selected_tools)
            or any(not name or len(name) > 256 for name in self.selected_tools)
        ):
            raise ValueError("invalid tool selection")
        return self


class McpConnectionView(StrictModel):
    id: UUID
    revision: int
    name: str
    endpoint: str
    has_token: bool
    enabled: bool
    required: bool
    max_inflight: int
    selected_tools: Literal["all"] | list[str]
    tool_policies: list[McpToolSelection]


class McpConnectionList(StrictModel):
    items: list[McpConnectionView]


class McpConnectionDelete(StrictModel):
    revision: int = Field(ge=1)


class McpDiscoveredTool(StrictModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


class McpDiscoveryResult(StrictModel):
    revision: int
    tools: list[McpDiscoveredTool]


class SandboxInspection(RunContractModel):
    allocation_id: RunUUID = Field(alias="allocationId")
    run_id: RunUUID = Field(alias="runId")
    step_id: RunUUID = Field(alias="stepId")
    tool_call_id: RunToolId = Field(alias="toolCallId")
    status: Literal["ok", "error", "unknown"]
    cleanup: Literal["confirmed", "unconfirmed"]
    result: JsonValue = None
    chart_sha256: str | None = Field(default=None, alias="chartSha256", pattern=r"^[0-9a-f]{64}$")


# Message snapshots and Run streams share this single v2 business protocol.
def valid_stream_id(value: str) -> str:
    if any(int(part) > 2**64 - 1 for part in value.split("-")):
        raise ValueError("stream ID overflow")
    return value


StreamId = Annotated[
    str,
    Field(pattern=r"^(0|[1-9][0-9]{0,19})-(0|[1-9][0-9]{0,19})$"),
    AfterValidator(valid_stream_id),
]
DurableStatus = Literal[
    "created", "running", "waiting", "paused", "cancelling", "completed", "failed", "canceled"
]


class SessionSubmit(RunContractModel):
    request_id: UUID = Field(alias="requestId", strict=False)
    session_id: UUID | None = Field(default=None, alias="sessionId", strict=False)
    question: str = Field(min_length=1, max_length=8000)
    selection: MainModelSelection
    workflow_asset_ref: WorkflowAssetRef | None = Field(default=None, alias="workflowAssetRef")
    constraints: RunConstraints | None = None

    @model_validator(mode="after")
    def valid_submission(self):
        if not self.question.strip():
            raise ValueError("invalid submission")
        if self.constraints and self.workflow_asset_ref:
            raise ValueError("analysis constraints require a direct task")
        return self


class ControlCommand(RunContractModel):
    request_id: UUID = Field(alias="requestId", strict=False)
    expected_version: int = Field(alias="expectedVersion", ge=1)


class DecisionCommand(ControlCommand):
    wait_id: UUID = Field(alias="waitId", strict=False)
    proposal_id: UUID = Field(alias="proposalId", strict=False)
    proposal_revision: int = Field(alias="proposalRevision", ge=1)
    proposal_digest: str = Field(alias="proposalDigest", pattern=r"^[a-f0-9]{64}$")
    decision_id: UUID = Field(alias="decisionId", strict=False)
    decision: Literal["approve", "reject"]
    choice_id: UUID | None = Field(default=None, alias="choiceId", strict=False)
    custom_definition: str | None = Field(
        default=None, alias="customDefinition", min_length=1, max_length=1600
    )
    draft_edits: dict[str, str] | None = Field(default=None, alias="draftEdits", max_length=16)

    @model_validator(mode="after")
    def validate_custom_definition(self):
        if self.custom_definition is not None and (
            self.decision != "approve"
            or self.choice_id
            or self.draft_edits
            or not self.custom_definition.strip()
        ):
            raise ValueError("submit one clarification for the selected metric")
        return self


class ReconcileCommand(ControlCommand):
    wait_id: UUID = Field(alias="waitId", strict=False)
    action_id: UUID = Field(alias="actionId", strict=False)


class ExternalRunEvent(RunContractModel):
    event_id: UUID = Field(alias="eventId", strict=False)
    wait_id: UUID = Field(alias="waitId", strict=False)
    event_type: str = Field(alias="eventType", min_length=1, max_length=80)
    correlation_key: str = Field(alias="correlationKey", min_length=1, max_length=200)
    payload: dict[str, JsonValue]


class ControlReceipt(RunContractModel):
    run_id: RunUUID | None = Field(alias="runId")
    session_id: RunUUID = Field(alias="sessionId")
    version: int = Field(ge=1)
    status: str = Field(max_length=32)
    code: str | None = Field(default=None, max_length=80)


class RunPublicSnapshot(RunContractModel):
    ordinal: int = Field(ge=1)
    version: int = Field(ge=1)
    status: DurableStatus
    reason_code: str | None = Field(alias="reasonCode", max_length=80)
    recovery_status: Literal["ready", "required", "blocked"] = Field(alias="recoveryStatus")
    cleanup_status: Literal["none", "pending", "confirmed", "unconfirmed"] = Field(
        alias="cleanupStatus"
    )


class MessageContent(RunContractModel):
    text: str = Field(max_length=RUN_STREAM_LIMITS["answerBytes"])
    answer_mode: Literal["plain", "evidence"] | None = Field(
        default_factory=lambda: None, alias="answerMode", exclude_if=lambda value: value is None
    )
    completion: Literal["complete", "partial"] | None = Field(
        default_factory=lambda: None, exclude_if=lambda value: value is None
    )
    artifact_ids: list[RunUUID] = Field(alias="artifactIds", default_factory=list, max_length=40)


class MessageSnapshot(RunContractModel):
    message_id: RunUUID = Field(alias="messageId")
    ordinal: int = Field(ge=1)
    role: Literal["user", "assistant"]
    revision: int = Field(ge=1)
    status: Literal["streaming", "completed", "incomplete"]
    content: MessageContent


class MessageUpsert(RunContractModel):
    type: Literal["message.upsert"] = "message.upsert"
    payload: MessageSnapshot


class MessageDeltaValue(RunContractModel):
    message_id: RunUUID = Field(alias="messageId")
    revision: int = Field(ge=2)
    text: str = Field(min_length=1, max_length=8192)


class MessageDelta(RunContractModel):
    type: Literal["message.delta"] = "message.delta"
    payload: MessageDeltaValue


class RunUpsert(RunContractModel):
    type: Literal["run.upsert"] = "run.upsert"
    payload: RunPublicSnapshot


class PublicApproval(RunContractModel):
    goal: GoalProposal | None = None
    wait_id: RunUUID = Field(alias="waitId")
    proposal_id: RunUUID = Field(alias="proposalId")
    proposal_revision: int = Field(alias="proposalRevision", ge=1)
    proposal_digest: str = Field(alias="proposalDigest", pattern=r"^[a-f0-9]{64}$")
    action_id: RunUUID = Field(alias="actionId")
    kind: Literal["approval", "external_event", "reconciliation"]
    status: Literal["pending", "approved", "rejected", "expired", "canceled"]
    summary: str = Field(max_length=2000)
    expires_at: str | None = Field(alias="expiresAt")
    decision_id: RunUUID | None = Field(alias="decisionId")
    reason_code: str | None = Field(alias="reasonCode", max_length=80)


class DashboardWidget(RunContractModel):
    kind: Literal["metric", "table", "bar", "line"]
    title: str = Field(min_length=1, max_length=160)
    fact_ref: str = Field(alias="factRef", min_length=1, max_length=200)
    columns: list[str] = Field(min_length=1, max_length=50)
    rows: list[list[str | int | float | bool | None]] = Field(max_length=50)


class DashboardDocumentV1(RunContractModel):
    schema_version: Literal[1] = Field(alias="schemaVersion")
    title: str = Field(min_length=1, max_length=160)
    scope: str = Field(min_length=1, max_length=500)
    as_of: str = Field(alias="asOf", min_length=1, max_length=64)
    widgets: list[DashboardWidget] = Field(min_length=1, max_length=24)


class PublicArtifact(RunContractModel):
    id: RunUUID
    name: str = Field(min_length=1, max_length=160)
    media_type: Literal["image/png", "text/csv", "application/json", "text/markdown"] = Field(
        alias="mediaType"
    )
    byte_size: int = Field(alias="byteSize", ge=1, le=2097152)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    step_id: RunUUID = Field(alias="stepId")
    tool_call_id: str = Field(alias="toolCallId", max_length=200)


class SemanticOption(RunContractModel):
    id: RunUUID
    version_id: RunUUID = Field(alias="versionId")
    name: str
    description: str
    unit: str
    version: int


class SemanticFieldChange(RunContractModel):
    field: str
    before: str
    after: str


class SemanticRevisionPreview(RunContractModel):
    name: str
    code: str
    changes: list[SemanticFieldChange]


class WaitView(PublicApproval):
    revision_preview: SemanticRevisionPreview | None = Field(default=None, alias="revisionPreview")
    semantic_kind: Literal["choice", "result", "draft", "submit", "revision"] | None = Field(
        default=None, alias="semanticKind"
    )
    options: list[SemanticOption] | None = None
    fields: dict[str, JsonValue] | None = None
    scopes: list[dict[str, JsonValue]] | None = None
    run_version: int = Field(alias="runVersion", ge=1)
    allowed_actions: list[Literal["approve", "reject", "reconcile"]] = Field(alias="allowedActions")


class SemanticOpenResult(RunContractModel):
    proposal: dict[str, JsonValue] = Field(default_factory=dict)
    result_ref: UUID
    created_at: datetime
    columns: list[str]
    status: Literal["active", "confirmed", "dismissed", "saved"]
    reusable: bool
    draft_id: UUID | None = None


class CitationAnchor(RunContractModel):
    citation_id: RunUUID = Field(alias="citationId")
    ordinal: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=1)


class RunCitations(RunContractModel):
    message_id: RunUUID = Field(alias="messageId")
    revision: int = Field(ge=1)
    text_digest: str = Field(alias="textDigest", pattern=r"^[a-f0-9]{64}$")
    anchors: list[CitationAnchor] = Field(max_length=96)


class CitationSource(RunContractModel):
    kind: Literal["metric", "definition", "detail", "knowledge", "history", "external"]
    title: str
    version: str
    locator: str
    scope: str
    excerpt: str


class CitationDetail(RunContractModel):
    citation_id: RunUUID = Field(alias="citationId")
    claim_id: RunUUID = Field(alias="claimId")
    revision: int = Field(ge=1)
    status: Literal["supported", "refuted", "insufficient", "contested"]
    statement: str
    calculation: str | None = None
    sources: list[CitationSource] = Field(max_length=64)


class SemanticResults(RunContractModel):
    items: list[SemanticOpenResult]


class SemanticResultDecision(RunContractModel):
    result_ref: UUID = Field(strict=False)
    action: Literal["confirm", "dismiss", "save"]
    fields: dict[str, JsonValue] | None = Field(default=None, max_length=32)


class PublicWorkflow(RunContractModel):
    draft_id: RunUUID = Field(alias="draftId")
    draft: WorkflowDraftView
    status: Literal["offered", "saved", "expired"]
    asset_id: RunUUID | None = Field(alias="assetId")
    expires_at: str = Field(alias="expiresAt")


class ViewObject(RunContractModel):
    object_id: str = Field(alias="objectId", min_length=1, max_length=200)
    revision: int = Field(ge=1)


class ExecutionView(ViewObject):
    kind: Literal["execution.node"] = "execution.node"
    value: RunExecutionNode


class TimelineValue(RunContractModel):
    ordinal: int = Field(ge=1)
    event: RunProgressEvent


class TimelineView(ViewObject):
    kind: Literal["timeline"] = "timeline"
    value: TimelineValue


class PlanView(ViewObject):
    kind: Literal["plan"] = "plan"
    value: RunAutonomyResult


class NarrationView(ViewObject):
    kind: Literal["narration"] = "narration"
    value: RunRoutedPayload


class WorkflowView(ViewObject):
    kind: Literal["workflow"] = "workflow"
    value: PublicWorkflow


class ApprovalView(ViewObject):
    kind: Literal["approval"] = "approval"
    value: PublicApproval


class ArtifactView(ViewObject):
    kind: Literal["artifact"] = "artifact"
    value: PublicArtifact


PublicViewObject = Annotated[
    ExecutionView
    | TimelineView
    | PlanView
    | NarrationView
    | WorkflowView
    | ApprovalView
    | ArtifactView,
    Field(discriminator="kind"),
]


class ViewUpsert(RunContractModel):
    type: Literal["view.upsert"] = "view.upsert"
    payload: PublicViewObject


AgentEvent = Annotated[
    MessageUpsert | MessageDelta | RunUpsert | ViewUpsert, Field(discriminator="type")
]


class AgentPacket(RunContractModel):
    packet_id: str = Field(alias="packetId", min_length=1, max_length=240)
    run_id: RunUUID = Field(alias="runId")
    seq: StreamId | None
    request_id: RunUUID = Field(alias="requestId")
    occurred_at: str = Field(alias="occurredAt", max_length=40)
    event: AgentEvent


class RunCursor(RunContractModel):
    run_id: RunUUID = Field(alias="runId")
    seq: StreamId


class MessagePage(RunContractModel):
    next_cursor: str | None = Field(alias="nextCursor", max_length=500)


class AgentResponse(RunContractModel):
    protocol: Literal["compass.run"] = "compass.run"
    version: Literal[2] = 2
    session_id: RunUUID = Field(alias="sessionId")
    mode: Literal["snapshot", "delta"]
    packets: list[AgentPacket] = Field(max_length=8192)
    cursors: list[RunCursor] = Field(max_length=50)
    page: MessagePage | None

    @model_validator(mode="after")
    def envelope(self):
        runs = {cursor.run_id for cursor in self.cursors}
        if len(runs) != len(self.cursors) or any(p.run_id not in runs for p in self.packets):
            raise ValueError("run cursor mismatch")
        if self.mode == "snapshot":
            if self.page is None or any(
                p.seq is not None or p.event.type == "message.delta" for p in self.packets
            ):
                raise ValueError("invalid snapshot")
        else:
            if self.page is not None or len(self.cursors) != 1 or not self.packets:
                raise ValueError("invalid delta")
            previous = (0, 0)
            for packet in self.packets:
                if packet.seq is None:
                    raise ValueError("missing stream ID")
                current = tuple(map(int, packet.seq.split("-")))
                if current <= previous:
                    raise ValueError("invalid stream order")
                previous = current
            if self.cursors[0].seq != self.packets[-1].seq:
                raise ValueError("invalid envelope cursor")
        return self


class SessionListItem(RunContractModel):
    session_id: RunUUID = Field(alias="sessionId")
    title: str
    title_status: Literal["fallback", "pending", "generated", "manual"] = Field(
        default="fallback", alias="titleStatus"
    )
    session_type: Literal["chat", "workflow"] = Field(alias="sessionType")
    icon_key: Literal["chat", "workflow"] = Field(alias="iconKey")
    version: int = Field(ge=1)
    updated_at: str = Field(alias="updatedAt")
    latest_run_id: RunUUID | None = Field(alias="latestRunId")
    execution_status: DurableStatus | None = Field(alias="executionStatus")


class SessionList(RunContractModel):
    items: list[SessionListItem] = Field(max_length=50)
    next_cursor: str | None = Field(alias="nextCursor")


class SessionAccepted(RunContractModel):
    session_id: RunUUID = Field(alias="sessionId")
    run_id: RunUUID = Field(alias="runId")
    stream_url: str = Field(alias="streamUrl")
    session: SessionListItem | None = None


class SessionMutation(RunContractModel):
    request_id: UUID = Field(alias="requestId", strict=False)


class SessionRename(SessionMutation):
    title: str = Field(min_length=1, max_length=80)


class SessionDeleted(RunContractModel):
    session_id: RunUUID = Field(alias="sessionId")


class SchedulingPublic(RunContractModel):
    phase: Literal["scheduling"] = "scheduling"
    status: Literal["ready", "leased", "reclaiming", "parked", "done"]
    reason_code: str = Field(alias="reasonCode", max_length=80)
    claim_count: int = Field(alias="claimCount", ge=0)
    occurred_at: Annotated[datetime, Field(strict=False)] = Field(alias="occurredAt")
    summary: str = Field(max_length=200)


class RuntimeUsage(RunContractModel):
    tenant_id: Annotated[UUID, Field(strict=False)] = Field(alias="tenantId")
    limits: dict[str, int]
    jobs: dict[str, int]
    backlog: int = Field(ge=0)
    occupied: int = Field(ge=0)
    settled_tokens: int = Field(alias="settledTokens", ge=0)
    settled_cost_microusd: int | None = Field(alias="settledCostMicrousd", ge=0)
    unknown_attempts: int = Field(alias="unknownAttempts", ge=0)
    reserved_tokens: int = Field(alias="reservedTokens", ge=0)
    reserved_cost_microusd: int = Field(alias="reservedCostMicrousd", ge=0)


class RuntimeTimelineEntry(RunContractModel):
    id: str = Field(max_length=200)
    kind: Literal["scheduling", "step", "attempt", "model", "command"]
    status: str = Field(max_length=80)
    occurred_at: Annotated[datetime, Field(strict=False)] = Field(alias="occurredAt")
    reason_code: str | None = Field(alias="reasonCode", default=None, max_length=80)
    step_id: Annotated[UUID, Field(strict=False)] | None = Field(alias="stepId", default=None)
    attempt_id: Annotated[UUID, Field(strict=False)] | None = Field(alias="attemptId", default=None)
    model_call_id: Annotated[UUID, Field(strict=False)] | None = Field(
        alias="modelCallId", default=None
    )
    worker_id: str | None = Field(alias="workerId", default=None, max_length=64)
    epoch: Annotated[UUID, Field(strict=False)] | None = None


class RuntimeTimeline(RunContractModel):
    run_id: Annotated[UUID, Field(strict=False)] = Field(alias="runId")
    schedule_id: Annotated[UUID, Field(strict=False)] | None = Field(
        default=None, alias="scheduleId"
    )
    trigger_id: Annotated[UUID, Field(strict=False)] | None = Field(default=None, alias="triggerId")
    entries: list[RuntimeTimelineEntry] = Field(max_length=4096)


ScheduleStatus = Literal["enabled", "disabled", "deleted"]
DeliveryStatus = Literal[
    "pending",
    "dispatching",
    "retry_wait",
    "reconcile_pending",
    "submitted",
    "delivery_failed",
    "skipped",
]


class ScheduleInput(RunContractModel):
    question: str = Field(min_length=1, max_length=8000)
    selection: MainModelSelection
    workflow_asset_ref: WorkflowAssetRef | None = Field(default=None, alias="workflowAssetRef")
    constraints: RunConstraints | None = None


class ScheduleFields(ScheduleInput):
    name: str = Field(min_length=1, max_length=120)
    cron: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=128)
    enabled: bool = True


class ScheduleCreate(ScheduleFields):
    request_id: UUID = Field(alias="requestId", strict=False)


class SchedulePatch(RunContractModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    question: str | None = Field(default=None, min_length=1, max_length=8000)
    selection: MainModelSelection | None = None
    cron: str | None = Field(default=None, min_length=1, max_length=120)
    timezone: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    workflow_asset_ref: WorkflowAssetRef | None = Field(default=None, alias="workflowAssetRef")
    constraints: RunConstraints | None = None

    @model_validator(mode="after")
    def validate_patch(self):
        if not self.model_fields_set or any(
            getattr(self, field) is None
            for field in self.model_fields_set - {"workflow_asset_ref", "constraints"}
        ):
            raise ValueError("Provide at least one valid editable field")
        return self


class ScheduleDelete(RunContractModel):
    request_id: UUID = Field(alias="requestId", strict=False)
    expected_revision: int = Field(alias="expectedRevision", ge=1)


class ScheduleUpdate(ScheduleDelete):
    patch: SchedulePatch


class SchedulePreview(RunContractModel):
    selection: MainModelSelection | None = None
    cron: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=128)


class ScheduleAutoRouting(RunContractModel):
    flash: bool
    pro: bool


class SchedulePreviewResult(RunContractModel):
    times: list[datetime]
    warning_code: Literal["capability_not_supported"] | None = Field(
        default=None, alias="warningCode"
    )
    auto_routing: ScheduleAutoRouting | None = Field(default=None, alias="autoRouting")


class ScheduleWriteReceipt(RunContractModel):
    id: UUID
    revision: int
    status: ScheduleStatus
    next_fire_at: datetime | None = Field(alias="nextFireAt")


class TriggerView(RunContractModel):
    id: UUID
    schedule_id: UUID = Field(alias="scheduleId")
    schedule_revision: int = Field(alias="scheduleRevision")
    scheduled_at: datetime = Field(alias="scheduledAt")
    detected_at: datetime = Field(alias="detectedAt")
    coalesced_from_at: datetime | None = Field(alias="coalescedFromAt")
    delivery_status: DeliveryStatus = Field(alias="deliveryStatus")
    reason_code: str | None = Field(alias="reasonCode")
    delivery_attempts: int = Field(alias="deliveryAttempts")
    next_attempt_at: datetime | None = Field(alias="nextAttemptAt")
    run_id: UUID | None = Field(alias="runId")
    session_id: UUID | None = Field(alias="sessionId")
    run_status: DurableStatus | None = Field(alias="runStatus")
    run_reason_code: str | None = Field(alias="runReasonCode")
    observed_at: datetime | None = Field(alias="observedAt")
    finished_at: datetime | None = Field(alias="finishedAt")


class ScheduleView(ScheduleWriteReceipt, ScheduleInput):
    name: str
    cron: str
    timezone: str
    updated_at: datetime = Field(alias="updatedAt")
    latest_trigger: TriggerView | None = Field(alias="latestTrigger")


class SchedulePage(RunContractModel):
    items: list[ScheduleView]
    next_cursor: str | None = Field(alias="nextCursor")


class TriggerPage(RunContractModel):
    items: list[TriggerView]
    next_cursor: str | None = Field(alias="nextCursor")


class TriggerDelivery(RunContractModel):
    delivery_token: UUID = Field(alias="deliveryToken", strict=False)


class TriggerAdmission(RunContractModel):
    outcome: Literal["accepted", "closed", "ready"]
    run_id: UUID | None = Field(default=None, alias="runId")


# PromptOps management contracts for the authenticated Web BFF.
PromptPath = Annotated[str, Field(min_length=1, max_length=512)]
PromptBody = Annotated[str, Field(max_length=262144)]
PromptChoice = Literal["local", "online", "merge"]


class PromptFileEntry(StrictModel):
    path: str
    origin: Literal["bundled", "online"]
    current_version: int | None
    edit_rev: int
    active_epoch: int
    has_draft: bool
    used: bool


class PromptTree(StrictModel):
    generation: int | None
    snapshot_no: int
    items: list[PromptFileEntry]
    next_cursor: str | None


class PromptFileDetail(PromptFileEntry):
    body: str | None
    draft_body: str | None
    draft_base_version: int | None
    draft_base_epoch: int | None
    usages: list[str]
    variables: list[str]
    version: int | None
    kind: Literal["markdown", "json", "tool"] = "markdown"
    contract_sha256: str | None = None
    retired: bool = False


class PromptHistoricalVersion(StrictModel):
    path: str
    version: int
    sha256: str
    created_at: datetime
    created_by: UUID
    note: str


class PromptHistory(StrictModel):
    items: list[PromptHistoricalVersion]
    next_cursor: str | None


class PromptSnapshotEntry(StrictModel):
    snapshot_no: int
    generation: int
    policy_version: str
    policy_digest: str
    manifest_digest: str
    created_at: datetime
    created_by: UUID
    note: str


class PromptSnapshots(StrictModel):
    items: list[PromptSnapshotEntry]
    next_cursor: str | None


class PromptSnapshotFile(StrictModel):
    path: str
    version: int


class PromptSnapshot(PromptSnapshotEntry):
    items: list[PromptSnapshotFile]
    next_cursor: str | None


class PromptDraftBase(StrictModel):
    path: PromptPath
    expected_edit_rev: int = Field(ge=0)
    base_epoch: int = Field(ge=0)
    request_id: UUID


class PromptDraft(PromptDraftBase):
    body: PromptBody


class PromptDraftFromVersion(PromptDraftBase):
    version: int = Field(gt=0)


class PromptRenderInput(StrictModel):
    path: PromptPath
    body: PromptBody
    variables: dict[str, str] = Field(default_factory=dict, max_length=64)


class PromptRendered(StrictModel):
    rendered: str
    variables: list[str]


class PromptImportPreviewInput(StrictModel):
    path: PromptPath | None = None
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=100, ge=1, le=200)


class PromptImportEntry(StrictModel):
    path: str
    state: Literal["new", "unchanged", "local", "online", "conflict"]
    edit_rev: int
    base_body: str | None = None
    local_body: str | None = None
    online_body: str | None = None


class PromptImportPreview(StrictModel):
    package_digest: str
    confirmation_hash: str
    items: list[PromptImportEntry]
    next_cursor: str | None
    total: int


class PromptImportChoice(StrictModel):
    path: PromptPath
    choice: PromptChoice
    body: PromptBody | None = None


class PromptImport(StrictModel):
    request_id: UUID
    confirmation_hash: str = Field(min_length=64, max_length=64)
    package_digest: str = Field(min_length=64, max_length=64)
    choices: list[PromptImportChoice] = Field(default_factory=list, max_length=2048)


class PromptReleaseTarget(StrictModel):
    action: Literal["file", "all", "restore"]
    path: PromptPath | None = None
    snapshot_no: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_target(self):
        if (self.action == "file") != (self.path is not None) or (
            (self.action == "restore") != (self.snapshot_no is not None)
        ):
            raise ValueError("Choose the target for this release action")
        return self


class PromptRelease(PromptReleaseTarget):
    confirmation_hash: str = Field(min_length=64, max_length=64)
    note: str = Field(min_length=1, max_length=500)
    request_id: UUID


class PromptChange(StrictModel):
    path: str
    old_version: int | None
    new_version: int | None
    usages: list[str]


class PromptReleasePreview(StrictModel):
    generation: int | None
    snapshot_no: int | None
    changes: list[PromptChange]
    since_snapshot: list[PromptChange]
    errors: list[str]
    confirmation_hash: str


class PromptReceipt(StrictModel):
    action: str
    generation: int | None = None
    snapshot_no: int | None = None
    path: str | None = None
    version: int | None = None
    edit_rev: int | None = None
    adopted: int = 0
    changed_count: int = 0
    retained: int = 0
    skipped: int = 0


class StarterModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class StarterSuggestion(StarterModel):
    label: str
    question: str = Field(min_length=1, max_length=8000)
    constraints: RunConstraints | None = None


class StarterGroup(StarterModel):
    title: str
    suggestions: list[StarterSuggestion]


class StarterQuestion(StarterModel):
    id: str
    type: Literal["choice", "noul", "score"]
    instructions: str
    criteria: str


class StarterDecision(StarterModel):
    id: str
    title: str
    description: str
    state_format: Literal["text", "json"] = Field(alias="stateFormat")
    state: str
    questions: list[StarterQuestion]


class PromptStarterCards(StarterModel):
    generation: int
    analysis: list[StarterGroup] = Field(default_factory=list)
    models: list[StarterDecision] = Field(default_factory=list)
    default_choice: str | None = None
    tool_description: str | None = None
    workflow_parts: list[str] = Field(default_factory=list)


class KnowledgeEmbeddingBatch(StrictModel):
    texts: list[Annotated[str, Field(min_length=1, max_length=10000)]] = Field(
        min_length=1, max_length=32
    )


class KnowledgeEmbeddings(StrictModel):
    vectors: list[list[float]]


class ChildTraceSnapshot(RunContractModel):
    child_run_id: RunUUID = Field(alias="childRunId")
    seq: str = Field(pattern=r"^[0-9]+-[0-9]+$")
    nodes: list[RunExecutionNode]
    answer: str | None = None
