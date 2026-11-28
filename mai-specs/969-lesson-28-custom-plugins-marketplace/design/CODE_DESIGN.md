# 代码设计附录：第 28 课｜轻量插件市场、云端 MCP 接入与 Tool Search

> 关联主方案：[TECH_DESIGN.md](TECH_DESIGN.md) | 状态：定稿（已吸纳第 2 轮评审结论、衔接 `0021 → 0022` 迁移并补齐 ContextOps 全链路观测）

本附录展示 6 个核心模块的关键数据结构、安全边界、双轨机审、Tool Search 引擎与 ContextOps/OTel/Timeline 可观测契约。

---

## 模块 1：`EndpointPolicy` 与 `McpSession` 轻量适配

- **对应文件**：`apps/agent/src/compass_agent/tools/mcp/client.py`
- **核心语义**：引入 `enforce_allowlist: bool = False` 与 `allow_private_network: bool = True` 两个显式开关，默认允许接入标准云端 HTTPS 与公司内部 HTTP/HTTPS Streamable HTTP MCP；同时**无条件硬性阻断云元数据（`169.254.169.254`）、链路本地、组播、未指定地址、公网明文 HTTP 及 3xx 重定向**；协议版本兼容 `{"2025-03-26", "2025-06-18", "2025-11-25"}`。

```python
# apps/agent/src/compass_agent/tools/mcp/client.py（核心增量）
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from compass_agent.tools.contracts import ToolRejected

SUPPORTED_MCP_PROTOCOL_VERSIONS = frozenset({"2025-03-26", "2025-06-18", "2025-11-25"})


class EndpointPolicy:
    def __init__(
        self,
        allowed_origins: str,
        local_fixture_origins: str = "",
        *,
        enforce_allowlist: bool = False,
        allow_private_network: bool = True,
    ):
        self.allowed = frozenset(
            filter(None, (s.strip().rstrip("/") for s in allowed_origins.split(",")))
        )
        self.local = frozenset(
            filter(None, (s.strip().rstrip("/") for s in local_fixture_origins.split(",")))
        )
        self.enforce_allowlist = enforce_allowlist
        self.allow_private_network = allow_private_network

    def validate(self, endpoint: str):
        try:
            url = urlsplit(endpoint)
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError()
            origin = f"{url.scheme}://{url.hostname}"
            if url.port and url.port != (443 if url.scheme == "https" else 80):
                origin += f":{url.port}"
            if (
                url.username
                or url.password
                or url.query
                or url.fragment
                or "%" in url.netloc
                or any(ord(c) < 33 for c in endpoint)
                or not url.path.startswith("/")
                or ".." in url.path
            ):
                raise ValueError()
            is_local_fixture = origin in self.local
            if self.enforce_allowlist and origin not in (self.allowed | self.local):
                raise ValueError()
            if (
                url.scheme != "https"
                and not is_local_fixture
                and not self.allow_private_network
            ):
                raise ValueError()
            return url, is_local_fixture
        except ValueError:
            raise ToolRejected("mcp_endpoint_denied") from None

    async def resolve(self, endpoint: str) -> str:
        url, local = self.validate(endpoint)
        addresses = await asyncio.get_running_loop().getaddrinfo(
            url.hostname,
            url.port or (443 if url.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        values = {str(item[4][0]) for item in addresses}
        if not values:
            raise ToolRejected("mcp_endpoint_denied")
        for address in values:
            ip = ipaddress.ip_address(address)
            if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                raise ToolRejected("mcp_endpoint_denied")
            if not local and not self.allow_private_network and not ip.is_global:
                raise ToolRejected("mcp_endpoint_denied")
            if url.scheme != "https" and not local and ip.is_global:
                raise ToolRejected("mcp_endpoint_denied")
        return sorted(values)[0]
```

---

## 模块 2：开启即自动纳入工具池、默认 `deferred` 与闲聊直出保护

- **对应文件**：`apps/agent/src/compass_agent/tools/mcp/repository.py`、`apps/agent/src/compass_agent/runtime/service.py`
- **核心语义**：
  1. 未显式传 `mcpServerIds` 时，自动拉取当前用户全部 `enabled=True`、`check_status != 'failed'` 且关联市场条目未下架（`status != 'disabled'`）的连接及其 `loading_mode`（新接入默认 `'deferred'`）；
  2. 后台默认加载的连接 ID 仅存 `journal.default_mcp_server_ids`（严禁回写 `journal.tool_selection`），确保 `stream_answer_turn.py` 与 `assembler.should_direct_stream()` 对 `SC-39` 等纯闲聊场景保持零步直出；
  3. 市场条目下架后，`McpRepository.current()` 实时返回 `False`，阻断在途 Run 的 `revalidate` 与 `watch_revision`。

```python
# apps/agent/src/compass_agent/tools/mcp/repository.py（核心过滤与加载）
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from compass_agent.plugins.entities import PluginCatalogItem
from compass_agent.tools.mcp.entities import McpConnection


def _active_catalog_filter():
    """自定义连接（catalog_item_id IS NULL）或所关联市场条目未下架的连接有效。"""
    return or_(
        McpConnection.catalog_item_id.is_(None),
        PluginCatalogItem.status != "disabled",
    )


def load_enabled_connection_modes(engine, owner) -> dict[UUID, str]:
    """读取用户当前工作区所有已开启、质检未失败且未被市场下架的个人 MCP 连接及 loading_mode（最多 8 条）。"""
    stmt = (
        select(McpConnection.id, McpConnection.loading_mode)
        .outerjoin(PluginCatalogItem, McpConnection.catalog_item_id == PluginCatalogItem.id)
        .where(
            McpConnection.tenant_id == owner[0],
            McpConnection.workspace_id == owner[1],
            McpConnection.actor_user_id == owner[2],
            McpConnection.enabled.is_(True),
            McpConnection.check_status != "failed",
            _active_catalog_filter(),
        )
        .order_by(McpConnection.id)
        .limit(8)
    )
    with Session(engine) as session:
        return {conn_id: mode for conn_id, mode in session.execute(stmt).all()}
```

```python
# apps/agent/src/compass_agent/runtime/service.py（_discover_tools v3 分支）
async def discover_for_v3_run(self, context, control, journal):
    from uuid import UUID

    from compass_agent.tools.mcp.repository import owner_key

    snapshot = await asyncio.to_thread(journal.catalog_declarations)
    explicit = frozenset(UUID(v) for v in journal.tool_selection.get("mcpServerIds", []))
    repo = (
        self.tool_loader.repository()
        if callable(self.tool_loader.repository)
        else self.tool_loader.repository
    )
    enabled_modes = (
        await asyncio.to_thread(repo.load_enabled_modes, owner_key(context)) if repo else {}
    )
    if snapshot is not None:
        selected = frozenset(
            UUID(meta["connection_id"])
            for _, meta in snapshot.values()
            if meta.get("environment") == "mcp" and meta.get("connection_id")
        )
        journal.default_mcp_server_ids = selected - explicit
    elif explicit:
        selected = explicit
        journal.default_mcp_server_ids = frozenset()
    else:
        # 仅写入 default_mcp_server_ids，不污染 journal.tool_selection，保护 SC-39 纯闲聊直出
        selected = frozenset(enabled_modes.keys())
        journal.default_mcp_server_ids = selected
    journal.mcp_loading_modes = {
        cid: enabled_modes.get(cid, "deferred") for cid in selected
    }
    if not selected:
        return None
    return await control.perform(
        lambda: self.tool_loader.discover(
            context,
            control,
            defer_catalog_limit=True,
            selected_connection_ids=selected,
        )
    )
```

---

## 模块 3：市场目录实体（含 `asset_type`）与双轨统一自动化机审门禁

- **对应文件**：`apps/agent/src/compass_agent/plugins/entities.py`、`apps/agent/src/compass_agent/plugins/admission.py`
- **核心语义**：
  1. `PluginCatalogItem` 包含 `asset_type = 'mcp_remote'` 演进锚点与 4 态生命周期；`McpConnection` 增补 `catalog_item_id`、`source`、`loading_mode`（默认 `'deferred'`）、`check_status`、`last_check_json`；
  2. 抽出统一机审引擎 `run_plugin_quality_gate()`，同时服务**个人自用接入**与**市场上架提交**，固化 5 项确定性检查并预留 `AdmissionHook`。

```python
# apps/agent/src/compass_agent/plugins/entities.py
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, DateTime, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from compass_agent.database import Base


class PluginCatalogItem(Base):
    __tablename__ = "plugin_catalog_item"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z][a-z0-9_]{2,39}$'", name="ck_plugin_cat_slug"),
        CheckConstraint("asset_type IN ('mcp_remote')", name="ck_plugin_cat_asset_type"),
        CheckConstraint("source IN ('builtin_preset', 'submitted')", name="ck_plugin_cat_source"),
        CheckConstraint(
            "visibility IN ('tenant_private', 'platform_public')", name="ck_plugin_cat_visibility"
        ),
        CheckConstraint("auth_type IN ('none', 'bearer')", name="ck_plugin_cat_auth"),
        CheckConstraint(
            "status IN ('draft', 'pending_review', 'published', 'disabled')",
            name="ck_plugin_cat_status",
        ),
        CheckConstraint("revision >= 1", name="ck_plugin_cat_revision"),
        Index(
            "uq_plugin_catalog_public_slug",
            "slug",
            unique=True,
            postgresql_where=text("visibility = 'platform_public'"),
        ),
        Index(
            "uq_plugin_catalog_tenant_slug",
            "owner_tenant_id",
            "slug",
            unique=True,
            postgresql_where=text("visibility = 'tenant_private'"),
        ),
        Index("ix_plugin_catalog_status_vis", "status", "visibility", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(40))
    asset_type: Mapped[str] = mapped_column(String(24), server_default=text("'mcp_remote'"))
    source: Mapped[str] = mapped_column(String(24), server_default=text("'submitted'"))
    visibility: Mapped[str] = mapped_column(String(24), server_default=text("'tenant_private'"))
    owner_tenant_id: Mapped[UUID | None] = mapped_column(nullable=True)
    name: Mapped[str] = mapped_column(String(80))
    summary: Mapped[str] = mapped_column(String(300))
    endpoint: Mapped[str] = mapped_column(String(2048))
    auth_type: Mapped[str] = mapped_column(String(12), server_default=text("'bearer'"))
    status: Mapped[str] = mapped_column(String(24), server_default=text("'draft'"))
    discovered_tools_json: Mapped[list] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), server_default=text("'[]'::jsonb")
    )
    last_check_json: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
        nullable=True,
    )
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(server_default=text("1"))
    created_by_user_id: Mapped[UUID | None] = mapped_column(nullable=True)
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

```python
# apps/agent/src/compass_agent/plugins/admission.py
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from time import monotonic
from typing import Protocol
from uuid import uuid4

from pydantic import SecretStr

from compass_agent.tools.catalog import TOOLS as BUILTIN_TOOLS
from compass_agent.tools.contracts import ToolRejected
from compass_agent.tools.mcp.client import EndpointPolicy, McpSession
from compass_agent.tools.mcp.repository import ConnectionConfig
from compass_agent.tools.registry import validate_schema

MAX_TOOLS_PER_CONNECTION = 64
_PROMPT_OVERRIDE_RE = re.compile(
    r"(ignore\s+(all\s+)?previous\s+instructions|<\s*system\s*>|system\s*prompt\s*:|忽略(此前|之前|所有)指令)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CheckFinding:
    code: str
    severity: str  # "error" (硬阻断) | "warning" (提示放行)
    message: str


class AdmissionHook(Protocol):
    """为第 31 课深度安全网关预留的统一扩展位。"""

    def inspect(self, endpoint: str, discovered_tools: list[dict]) -> list[CheckFinding]: ...


async def run_plugin_quality_gate(
    *,
    endpoint: str,
    auth_type: str,
    ephemeral_token: str,
    policy: EndpointPolicy,
    allow_deferred_auth: bool = False,
    extra_hooks: tuple[AdmissionHook, ...] = (),
) -> tuple[dict, list[dict]]:
    """自用接入与市场上架共用的 5 项最小化自动化机审质检门禁。"""
    started = monotonic()
    findings: list[CheckFinding] = []
    discovered: list[dict] = []

    # 规则 1：URL / 协议 / 防云元数据与公网明文 HTTP 安全底线
    try:
        policy.validate(endpoint)
        await policy.resolve(endpoint)
    except ToolRejected as exc:
        findings.append(CheckFinding("endpoint_denied", "error", f"端点未通过安全底线校验：{exc.code}"))
        return _pack_result(findings, started, probe_status="failed"), discovered

    if auth_type == "bearer" and not ephemeral_token:
        if allow_deferred_auth:
            findings.append(
                CheckFinding(
                    "auth_probe_skipped",
                    "warning",
                    "该模板需个人 Bearer Token，提交阶段跳过在线握手，个人接入时强制执行握手质检。",
                )
            )
            return _pack_result(findings, started, probe_status="skipped_auth"), discovered
        findings.append(CheckFinding("token_required", "error", "Bearer 认证模式必须提供有效访问令牌。"))
        return _pack_result(findings, started, probe_status="failed"), discovered

    # 规则 2–5：真实 Streamable HTTP 握手、Schema 校验、规模上限、重名提示与注入拦截
    probe_cfg = ConnectionConfig(
        id=uuid4(),
        owner=(uuid4(), uuid4(), uuid4()),
        revision=1,
        name="quality-gate-probe",
        endpoint=endpoint,
        token=SecretStr(ephemeral_token),
        required=False,
        max_inflight=1,
        tool_policies=(),
        selected_tools="all",
    )
    session = McpSession(probe_cfg, policy)
    try:
        await session.start(asyncio.Semaphore(1))
        if len(session.tools) > MAX_TOOLS_PER_CONNECTION:
            findings.append(
                CheckFinding(
                    "tool_count_exceeded",
                    "error",
                    f"单连接暴露工具数 {len(session.tools)} 超过上限 {MAX_TOOLS_PER_CONNECTION}。",
                )
            )
        builtin_names = {spec.name for spec in BUILTIN_TOOLS}
        for remote in session.tools[:MAX_TOOLS_PER_CONNECTION]:
            desc = remote.description or ""
            try:
                validate_schema(remote.input_schema)
                if remote.output_schema is not None:
                    validate_schema(remote.output_schema)
            except ToolRejected:
                findings.append(
                    CheckFinding("invalid_tool_schema", "error", f"工具 {remote.name} JSON Schema 不合法或超过 32 KiB。")
                )
            if len(desc) > 2000:
                findings.append(
                    CheckFinding("description_too_long", "error", f"工具 {remote.name} 描述超过 2000 字符上限。")
                )
            if _PROMPT_OVERRIDE_RE.search(desc):
                findings.append(
                    CheckFinding("prompt_injection_risk", "error", f"工具 {remote.name} 描述命中高危系统指令覆盖模式。")
                )
            if remote.name in builtin_names:
                findings.append(
                    CheckFinding("builtin_name_hint", "warning", f"工具 {remote.name} 与内建工具重名（运行时自动隔离命名空间）。")
                )
            discovered.append(
                {
                    "name": remote.name,
                    "description": desc[:300],
                    "inputSchema": remote.input_schema,
                }
            )
        for hook in extra_hooks:
            findings.extend(hook.inspect(endpoint, discovered))
        probe_status = "passed"
    except ToolRejected as exc:
        findings.append(CheckFinding("mcp_handshake_failed", "error", f"Streamable HTTP 握手探测失败：{exc.code}"))
        probe_status = "failed"
    finally:
        await session.close()

    return _pack_result(findings, started, probe_status=probe_status), discovered


def _pack_result(findings: list[CheckFinding], started: float, *, probe_status: str) -> dict:
    has_error = any(item.severity == "error" for item in findings)
    return {
        "passed": not has_error,
        "probeStatus": probe_status,
        "elapsedMs": int((monotonic() - started) * 1000),
        "findings": [
            {"code": item.code, "severity": item.severity, "message": item.message}
            for item in findings
        ],
    }
```

---

## 模块 4：Web BFF 极简授权用途

- **对应文件**：`apps/web/src/server/modules/models/application/authorize.ts`

```ts
export type PluginPurpose = "plugin_use" | "plugin_review";
// plugin_use -> requirePermission(access, "module.plugins", scope)
// plugin_review -> if (access.role !== "admin" && !actor.isPlatformAdmin) reject("forbidden")
```

---

## 模块 5：Claude Code 风格 Tool Search 引擎、元工具与前缀稳定增量激活

- **对应文件**：`apps/agent/src/compass_agent/tools/tool_search_engine.py`、`tools/builtin/tool_search/__init__.py`、`tools/assembler.py`、`context_runtime/tool_assembly.py`
- **核心语义**：
  1. BM25（`k1=1.2, b=0.75`）+ 蛇形/驼峰分词 + 中文 2-gram + `select:` 精确选择；
  2. 完备处理 `activated`、`no_match`（返回 `status="empty"` 与 `availableServers`）及 `budget_exceeded`（返回剩余槽位与降级建议）；
  3. Work 入口冻结 `deferred_catalog_hint`，后续多步激活仅在 `ordered_plan` 末尾追加 `("tool_search_activated")` 渠道工具，前序工具序列与系统提示前缀字节级 100% 恒定。

```python
# apps/agent/src/compass_agent/tools/tool_search_engine.py（核心检索接口）
def search_deferred_pool(
    deferred_pool: Mapping[str, RegisteredTool],
    query: str,
    *,
    max_results: int = 5,
) -> list[ToolSearchHit]:
    """仅在已授权且未下架的 deferred_pool 内执行 select: 精确选择或本地加权 BM25 检索 (<5ms)。"""
    ...
```

```python
# apps/agent/src/compass_agent/tools/builtin/tool_search/__init__.py（元工具契约骨架）
class ToolSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(
        min_length=1,
        max_length=200,
        description="关键词/业务问题，或使用 'select:工具名1,工具名2' 精确激活延迟工具。",
    )
    max_results: int = Field(default=5, ge=1, le=10, description="单次最多激活的工具数（1..10）。")
```

```python
# apps/agent/src/compass_agent/tools/assembler.py（分层与前缀指纹计算）
def partition_mcp_tools(
    mcp_entries: list[RegisteredTool],
    *,
    mode: str,  # "auto" | "always" | "off"
    loading_modes: Mapping[UUID, str],
    eager_limit: int,
    eager_bytes: int,
) -> tuple[list[RegisteredTool], dict[str, RegisteredTool]]:
    """默认 loading_mode='deferred' 直入延迟池；always_load 直出；auto 按阈值自动切换。"""
    always_load: list[RegisteredTool] = []
    auto_group: list[RegisteredTool] = []
    deferred_universe: dict[str, RegisteredTool] = {}

    for entry in sorted(mcp_entries, key=lambda e: e.name):
        conn_mode = loading_modes.get(entry.mcp_server_id, "auto")
        if mode == "off":
            always_load.append(entry)
        elif conn_mode == "always_load":
            always_load.append(entry)
        elif conn_mode == "deferred" or mode == "always":
            deferred_universe[entry.name] = entry
        else:
            auto_group.append(entry)

    if auto_group:
        combined = always_load + auto_group
        total_bytes = sum(len(json_bytes(e.definition.model_dump())) for e in combined if e.definition)
        if len(combined) <= eager_limit and total_bytes <= eager_bytes:
            always_load = combined
        else:
            for entry in auto_group:
                deferred_universe[entry.name] = entry
    return always_load, deferred_universe
```

---

## 模块 6：ContextOps、OTel 与 `UiExecutionTimeline` 全链路观测契约

- **对应文件**：`apps/agent/src/compass_agent/schemas.py`、`context_ops/analytics.py`、`observability/privacy.py`、`observability/context_spans.py`、`runtime/execution_trace.py`、`apps/web/src/features/contextops/{flow.tsx,page.tsx}`

```python
# apps/agent/src/compass_agent/schemas.py（ContextOps 工具装配与总览观测扩展）
class ToolAssemblyItemView(StrictModel):
    name: str
    label: str  # MCP 工具脱敏展示为 "<serverName>/<remoteName>"
    channel: Literal["always_on", "scene_routed", "user_selected", "tool_search_activated"]
    pack: str


class ToolAssemblyTraceView(StrictModel):
    authorized_count: int = Field(ge=0)
    exposed_count: int = Field(ge=0)
    deferred_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    permission_filtered_count: int = Field(ge=0)
    declaration_bytes: int = Field(ge=0)
    declaration_tokens: int = Field(ge=0)
    tool_search_active: bool = False
    tool_search_mode: Literal["auto", "always", "off"] = "auto"
    deferred_universe_count: int = Field(default=0, ge=0)
    activated_deferred_count: int = Field(default=0, ge=0)
    full_declaration_tokens: int = Field(default=0, ge=0)
    saved_declaration_tokens: int = Field(default=0, ge=0)
    token_saving_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    prefix_digest: str = ""
    exposed: list[ToolAssemblyItemView]
    deferred: list[str] = Field(max_length=20)
    excluded: list[ToolExcludedItemView]
```
