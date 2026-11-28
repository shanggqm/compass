"""Shared tool control ports; no dependency on a Run or inference implementation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from compass_agent.schemas import (
    BudgetProposal,
    ToolApprovalProposal,
    ToolDefinition,
    TrustedContext,
)

if TYPE_CHECKING:
    from compass_agent.observability.observer import Observation


class StopRequested(Exception):
    pass


class PhaseCanceled(asyncio.CancelledError):
    def __init__(self, *, cleanup_confirmed: bool):
        self.cleanup_confirmed = cleanup_confirmed
        super().__init__("run phase canceled")


class ResourceUnconfirmed(Exception):
    pass


class ExecutionError(Exception):
    """Safe execution reason carried across orchestration and tool boundaries."""


class ToolControl(Protocol):
    id: UUID
    deadline_at: float
    phase_deadline: float | None
    cleanup_confirmed: bool

    @property
    def stop_reason(self) -> str | None: ...

    @property
    def remaining(self) -> float: ...
    def require_running(self) -> None: ...
    def mark_cleanup_unconfirmed(self) -> None: ...
    async def perform[T](
        self, operation: Callable[[], Awaitable[T]], *, timeout_s: float | None = None
    ) -> T: ...


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def preview(value: str | None) -> str | None:
    if value is None:
        return None
    raw = value.encode()
    return value if len(raw) <= 900 else raw[:880].decode(errors="ignore") + "\n…（摘要已截断）"


# Tool contracts are independent of the model loop and the infrastructure clients.


Effect = Literal["read_only", "local_artifact", "external_write", "unknown"]
CleanupState = Literal["not_started", "confirmed", "remote_managed", "unconfirmed"]
ToolStatus = Literal[
    "ok", "empty", "error", "rejected", "waiting", "canceled", "timeout", "unknown"
]


class ToolBusinessStopped(Exception):
    """A trusted handler stopped after completing its resource cleanup."""


class ToolRejected(ExecutionError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ToolBudget(Protocol):
    def start_tool(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    effect: Effect = "read_only"
    approval: Literal["none", "required"] = "none"

    def __post_init__(self):
        if self.effect not in {"read_only", "local_artifact", "external_write", "unknown"}:
            raise ValueError("invalid effect")
        if self.approval not in {"none", "required"}:
            raise ValueError("invalid approval")
        if self.effect in {"external_write", "unknown"} and self.approval != "required":
            raise ValueError("external or unknown effects require approval")


@dataclass(frozen=True, slots=True)
class ToolBatchPolicy:
    mode: Literal["serial", "parallel"] = "serial"
    max_concurrency: int = 2
    failure_mode: Literal["stop", "continue"] = "stop"

    def __post_init__(self):
        if self.mode not in {"serial", "parallel"} or self.failure_mode not in {"stop", "continue"}:
            raise ValueError("invalid batch policy")
        if type(self.max_concurrency) is not int or not 1 <= self.max_concurrency <= 2:
            raise ValueError("batch concurrency must be 1 or 2")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    id: str
    name: str
    media_type: str
    run_id: UUID
    step_id: UUID
    tool_call_id: str
    byte_size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    execution_id: str
    status: ToolStatus
    cleanup: CleanupState
    payload: object = field(default=None, repr=False)
    reason_code: str | None = None
    file: bytes | None = field(default=None, repr=False)
    environment_id: str | None = None
    context_result: dict | None = field(default=None, repr=False)
    governed: bool = field(default=False, repr=False)
    source_deps: tuple[dict, ...] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    call_id: str
    status: ToolStatus
    cleanup: CleanupState = "not_started"
    reason_code: str | None = None
    payload: object = field(default=None, repr=False)
    artifacts: tuple[ArtifactRef, ...] = ()
    artifact_status: str = "none"
    proposal: BudgetProposal | ToolApprovalProposal | None = None
    context_result: dict | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ToolExecutionEvent:
    run_id: UUID
    step_id: UUID
    tool_call_id: str
    tool_name: str
    environment: str
    effect: Effect
    approval: str
    status: ToolStatus
    cleanup: CleanupState
    elapsed_ms: int
    reason_code: str | None = None
    environment_id: str | None = None
    output_json: str | None = field(default=None, repr=False)
    operation: str | None = None


@dataclass(frozen=True, slots=True)
class AuthorizedInput:
    run_id: UUID
    owner: tuple[UUID, UUID, UUID]
    data: object = field(repr=False)
    revalidate: Callable[[], Awaitable[bool]] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    trusted: TrustedContext
    run_id: UUID
    step_id: UUID
    control: ToolControl
    budget: ToolBudget
    visible_names: frozenset[str]
    caller: Literal["model", "workflow"] = "model"
    connection_ids: frozenset[str] | None = None
    sources: MutableMapping[str, AuthorizedInput] = field(default_factory=dict, repr=False)
    emit: Callable[[ToolExecutionEvent], None] = field(default=lambda _: None, repr=False)
    call_id: str = ""
    on_start: Callable[[PreparedToolCall], None] = field(default=lambda _: None, repr=False)
    observation: Observation | None = field(default=None, repr=False)
    search_dispatch: Callable[..., Awaitable[ExecutionReceipt]] | None = field(
        default=None, repr=False
    )

    sandbox_access: Callable[[list[UUID]], Awaitable[None]] | None = field(default=None, repr=False)

    @property
    def owner(self) -> tuple[UUID, UUID, UUID]:
        from compass_agent.schemas import TenantScope

        scope = self.trusted.scope
        if not isinstance(scope, TenantScope):
            raise ToolRejected("tool_scope_denied")
        return scope.tenant_id, scope.workspace_id, self.trusted.actor_user_id

    def require_running(self) -> None:
        self.control.require_running()
        if self.control.remaining <= 0:
            raise StopRequested()


@dataclass(frozen=True, slots=True)
class InProcessBinding:
    handler: Callable[[object, ToolExecutionContext], Awaitable[ExecutionReceipt]]
    environment: Literal["in_process"] = "in_process"
    orchestrates: bool = False


@dataclass(frozen=True, slots=True)
class SandboxBinding:
    runtime_profile: str = "python"
    environment: Literal["sandbox"] = "sandbox"


@dataclass(frozen=True, slots=True)
class McpBinding:
    connection_id: UUID
    revision: int
    remote_name: str
    schema_hash: str
    environment: Literal["mcp"] = "mcp"


ExecutionBinding = InProcessBinding | SandboxBinding | McpBinding


class ToolBackend(Protocol):
    async def execute(
        self, binding: ExecutionBinding, args: object, context: ToolExecutionContext
    ) -> ExecutionReceipt: ...


@dataclass(frozen=True, slots=True)
class PreparedApproval:
    proposal: ToolApprovalProposal
    private: dict


LoadingChannel = Literal["always_on", "scene_routed", "user_selected"]
CapabilityPack = Literal[
    "core_base",
    "subagent_base",
    "data_base",
    "doc_base",
    "schedule_base",
    "deep_analysis_pack",
    "long_task",
    "workflow_pack",
    "semantic_govern",
    "user_extension",
]


@dataclass(frozen=True, slots=True)
class ToolGovernance:
    """Code-owned loading metadata; user selection never grants execution access."""

    channel: LoadingChannel = "user_selected"
    pack: CapabilityPack = "user_extension"
    default_scenes: frozenset[str] = frozenset()
    default_tasks: frozenset[str] = frozenset()
    allowed_phases: frozenset[str] = frozenset({"work"})
    requires_semantic_maintainer: bool = False
    subagent_allowed: bool = False
    scheduler_allowed: bool = True
    summary: str = ""


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    name: str
    definition: ToolDefinition | None
    policy: ToolPolicy
    binding: ExecutionBinding | None
    parse_arguments: Callable[[str], object]
    authorize: Callable[[object, ToolExecutionContext], None]
    validate_payload: Callable[[object], object]
    prepare_proposal: (
        Callable[[object, ToolExecutionContext], BudgetProposal | ToolApprovalProposal] | None
    ) = None
    parallel_safe: bool = False
    timeout_s: float = 20
    revalidate: Callable[[ToolExecutionContext], Awaitable[None]] | None = None
    prepare_approval: (
        Callable[[object, ToolExecutionContext], Awaitable[PreparedApproval | None]] | None
    ) = None
    local_effect: bool = False
    # Code-owned capabilities. Remote declarations cannot opt into these ports.
    completion: bool = False
    public_projection: Literal["normal", "status_only"] = "normal"
    evidence_adapter: Callable | None = None
    fingerprint: Callable | None = None
    governance: ToolGovernance = field(default_factory=ToolGovernance)
    normalize_input: Callable | None = None
    legacy_definition: ToolDefinition | None = None
    workflow_id: UUID | None = None
    mcp_server_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PreparedToolCall:
    call_id: str
    tool: RegisteredTool
    args: object = field(repr=False)
    approval: PreparedApproval | None = None
