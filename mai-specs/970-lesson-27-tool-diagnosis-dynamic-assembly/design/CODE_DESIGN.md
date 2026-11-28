# 第 27 课｜内建工具体系诊断与场景化动态组装 代码设计附录（供评审）

> 状态：**设计示意（尚未写入生产源码，待方案评审确认后按任务分解实施）** | 关联主方案：[TECH_DESIGN.md](./TECH_DESIGN.md) | 需求来源：[spec/PRD.md](../spec/PRD.md)

[返回技术方案主文](./TECH_DESIGN.md) · [返回需求简报](../BRIEF.md)

本附录展开 [`TECH_DESIGN.md` §2.4](./TECH_DESIGN.md#24-供评审的代码设计for-review-check) 登记的 6 个核心模块代码骨架、状态不变量与关键分支，供研发评审三通道边界正交性、权限前置性、单次 Run 恒定前缀缓存及持久恢复兼容性。所有决定正确性与安全性的分支均完整写出。

---

## 1. 三层 `ToolSpec` 契约与容量解耦注册表

- **目标文件**：
  - `apps/agent/src/compass_agent/tools/catalog_contracts.py`（修改）
  - `apps/agent/src/compass_agent/tools/contracts.py`（修改：在 `RegisteredTool` 挂载 `governance` 与可选扩展来源 ID）
  - `apps/agent/src/compass_agent/tools/registry.py`（修改：拆分注册池上限 `128` 与单步可见上限 `32` / `64 KiB`）
- **变更语义**：
  1. `ToolSpec` 新增不可变的三通道 `ToolGovernance(channel, pack, ...)` 元数据层，保持 `runtime/` 与 `harness/` 零业务工具硬编码导入。
  2. `ToolRegistry` 构造函数允许装载最多 `TOOL_REGISTRY_POOL_LIMIT = 128` 个已授权工具以支持 48+ 工具池与历史回执核验；单步暴露给大模型的 `tools[]` 由 `validate_visible_catalog()` 强制守卫 `<= 32` 个且 `<= 64 KiB`。
- **重点审查问题**：
  - 写入、需人工审批或具备本地副作用的工具是否在注册入口被禁止标记 `subagent_allowed=True`？

```python
# 设计示意：apps/agent/src/compass_agent/tools/catalog_contracts.py
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal

from compass_agent.schemas import ToolDefinition
from compass_agent.tools.contracts import InProcessBinding, RegisteredTool, ToolPolicy
from compass_agent.tools.registry import bounded_payload, parse_arguments

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
AssemblyPhase = Literal["work", "delivery_only"]


@dataclass(frozen=True, slots=True)
class ToolGovernance:
    """Server-owned three-channel loading & governance metadata."""

    channel: LoadingChannel
    pack: CapabilityPack
    default_scenes: frozenset[str] = frozenset()
    default_tasks: frozenset[str] = frozenset()
    allowed_phases: frozenset[AssemblyPhase] = frozenset({"work"})
    requires_semantic_maintainer: bool = False
    subagent_allowed: bool = False
    scheduler_allowed: bool = True
    summary: str = ""

    def __post_init__(self) -> None:
        if self.subagent_allowed and not self.scheduler_allowed:
            raise ValueError("subagent-allowed read-only tools must remain scheduler-compatible")
        if self.requires_semantic_maintainer and self.channel != "user_selected":
            raise ValueError("semantic maintainer tools must live in the user_selected channel")


DEFAULT_EXTENSION_GOVERNANCE = ToolGovernance(
    channel="user_selected",
    pack="user_extension",
    subagent_allowed=False,
    scheduler_allowed=True,
)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    category: Literal["builtin", "business"]
    capability: str
    definition: Callable[[object], ToolDefinition | None]
    available: Callable[[object], bool]
    governance: ToolGovernance = field(
        default_factory=lambda: ToolGovernance(channel="always_on", pack="core_base")
    )
    parallel_safe: bool = False
    orchestrates: bool = False
    factory: Callable[[], RegisteredTool] | None = None
    context_factory: Callable[[object], RegisteredTool] | None = None
    evidence_adapter: Callable | None = None
    fingerprint: Callable | None = None
    public_projection: Literal["normal", "status_only"] = "normal"

    def register(self, context: object, handler: Callable) -> RegisteredTool:
        if self.context_factory is not None:
            entry = self.context_factory(context)
        elif self.factory is not None:
            entry = self.factory()
        else:
            entry = RegisteredTool(
                self.name,
                self.definition(context),
                ToolPolicy(),
                InProcessBinding(handler(self.name), orchestrates=self.orchestrates),
                parse_arguments,
                lambda args, execution: execution.require_running(),
                bounded_payload,
                parallel_safe=self.parallel_safe,
            )
        if entry.policy.effect != "read_only" or entry.policy.approval == "required" or entry.local_effect:
            if self.governance.subagent_allowed:
                raise ValueError(f"{self.name}: mutating or approval tool cannot be subagent_allowed")
        entry = replace(
            entry,
            governance=self.governance,
            fingerprint=self.fingerprint if self.fingerprint is not None else entry.fingerprint,
            evidence_adapter=(
                self.evidence_adapter if self.evidence_adapter is not None else entry.evidence_adapter
            ),
            public_projection=(
                self.public_projection if self.public_projection != "normal" else entry.public_projection
            ),
        )
        if entry.definition:
            from compass_agent.promptops.structured import enrich_definition

            entry = replace(entry, definition=enrich_definition(entry.definition))
        return entry
```

```python
# 设计示意：apps/agent/src/compass_agent/tools/registry.py（容量拆分核心变更）
from collections.abc import Iterable, Mapping
from compass_agent.schemas import TOOL_CATALOG_LIMIT, ToolDefinition
from compass_agent.tools.contracts import RegisteredTool, ToolRejected, json_bytes

TOOL_REGISTRY_POOL_LIMIT = 128
TOOL_DECLARATION_BYTES_LIMIT = 64 * 1024


def visible_catalog_bytes(definitions: Iterable[ToolDefinition]) -> int:
    return len(json_bytes([d.model_dump() for d in definitions]))


def has_visible_room(definitions: Iterable[ToolDefinition]) -> bool:
    values = list(definitions)
    return (
        len(values) <= TOOL_CATALOG_LIMIT
        and visible_catalog_bytes(values) <= TOOL_DECLARATION_BYTES_LIMIT
    )


def validate_visible_catalog(definitions: Iterable[ToolDefinition]) -> None:
    """Enforced on the per-step model-visible tools[] list."""
    if not has_visible_room(definitions):
        raise ToolRejected("tool_catalog_limit")


def validate_registry_pool(entries: Mapping[str, RegisteredTool]) -> None:
    """Enforced on the Run-level authorized pool (supports 48+ tools with bounded memory)."""
    if len(entries) > TOOL_REGISTRY_POOL_LIMIT:
        raise ToolRejected("tool_registry_pool_limit")
```

---

## 2. 三通道确定性组装器 `ToolAssembler`

- **目标文件**：`apps/agent/src/compass_agent/tools/assembler.py`（新增）
- **变更语义**：
  1. 严格按 `Step 1 硬权限与身份裁剪 -> Step 2 阶段状态门禁 -> Step 3 AlwaysOn + UserSelected + SceneRouted 三通道合流 -> Step 4 32 槽位 / 64 KiB 预算守卫` 执行。
  2. **防越权不变量**：前端传入的 `selected_packs`、`selected_workflow_ids` 与 `selected_mcp_server_ids` 仅能从已通过 Step 1 权限校验的 `authorized` 池中提取工具，绝无越权可能。
  3. **100% 恒定前缀与第 28 课衔接不变量**：单次 Run 在 `work` 阶段内输入恒定，产出的 `definitions` 字节级 100% 恒定；未装载的获准工具自动汇总为 `deferred_pool`，供第 28 课 `ToolSearch` 直接复用。

```python
# 设计示意：apps/agent/src/compass_agent/tools/assembler.py
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from compass_agent.schemas import TOOL_CATALOG_LIMIT, ToolDefinition
from compass_agent.tools.contracts import RegisteredTool, json_bytes
from compass_agent.tools.registry import TOOL_DECLARATION_BYTES_LIMIT, validate_visible_catalog

USER_SELECTED_EXTENSION_LIMIT = 12
L1_LIGHT_SCENES = frozenset({"SC-01", "SC-08", "SC-16", "SC-21", "SC-33", "SC-37", "SC-39"})
DIRECT_STREAM_SCENES = frozenset({"SC-33", "SC-37", "SC-39"})
MULTI_TURN_READ_SCENES = frozenset({"SC-34"})
AUTOMATION_SCENES = frozenset({"SC-29", "SC-30"})
ADMIN_CONFIG_SCENES = frozenset({"SC-36", "SC-38"})


@dataclass(frozen=True, slots=True)
class AssemblyInput:
    """Immutable per-Run work-entry snapshot (session flags frozen at identify -> work)."""

    scope_profile: str
    task: str
    scene_id: str
    phase: str  # "identify" | "plan" | "query" | "analyze" | "answer_stream"
    turn_purpose: str = "work"  # "work" | "verify"
    delivery_only: bool = False
    caller_role: str = "root"  # "root" | "subagent" | "scheduler"
    plugins_permitted: bool = True
    is_semantic_maintainer: bool = False
    requires_plan: bool = False
    has_active_plan: bool = False  # Frozen at work entry to keep prefix 100% stable
    has_active_goal: bool = False  # Frozen at work entry to keep prefix 100% stable
    has_semantic_draft: bool = False  # Frozen at work entry to keep prefix 100% stable
    selected_packs: frozenset[str] = frozenset()
    selected_workflow_ids: frozenset[UUID] = frozenset()
    selected_mcp_server_ids: frozenset[UUID] = frozenset()
    scope_ceiling: frozenset[str] | None = None
    scope_plugin_prefixes: tuple[str, ...] = ()
    skill_recommended: frozenset[str] = frozenset()  # Reserved boundary for Lesson 29-30


@dataclass(frozen=True, slots=True)
class ExposedToolTrace:
    name: str
    channel: str  # "always_on" | "scene_routed" | "user_selected"
    pack: str


@dataclass(frozen=True, slots=True)
class ToolAssemblyTrace:
    authorized_count: int
    exposed_count: int
    deferred_count: int
    excluded_count: int
    permission_filtered_count: int
    declaration_bytes: int
    declaration_tokens: int
    exposed: tuple[ExposedToolTrace, ...]
    deferred: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]  # Non-permission reasons: stage / scene / role / budget


@dataclass(frozen=True, slots=True)
class ToolAssemblyResult:
    definitions: list[ToolDefinition]
    authorized_pool: Mapping[str, RegisteredTool]
    deferred_pool: Mapping[str, RegisteredTool]  # Plug-in boundary reserved for Lesson 28 ToolSearch
    trace: ToolAssemblyTrace


def eligible_for_subagent(entry: RegisteredTool) -> bool:
    if entry.governance.pack == "subagent_base":
        return True
    return (
        entry.governance.subagent_allowed
        and entry.policy.effect == "read_only"
        and entry.policy.approval == "none"
        and not entry.local_effect
        and not entry.completion
        and not getattr(entry.binding, "orchestrates", False)
    )


def _matches_user_selected(entry: RegisteredTool, ctx: AssemblyInput) -> bool:
    gov = entry.governance
    if ctx.caller_role == "subagent":
        return False
    if entry.name == "python_analysis" and "python_sandbox" in ctx.selected_packs:
        return True
    if gov.channel != "user_selected":
        return False
    if gov.pack == "long_task":
        return (
            "long_task" in ctx.selected_packs
            or ctx.has_active_goal
            or ctx.scene_id == "SC-40"
        )
    if gov.pack == "workflow_pack":
        return "workflow_pack" in ctx.selected_packs or ctx.scene_id in AUTOMATION_SCENES
    if gov.pack == "semantic_govern":
        return (
            "semantic_govern" in ctx.selected_packs
            or ctx.scene_id in ADMIN_CONFIG_SCENES
            or ctx.has_semantic_draft
        )
    if gov.pack == "user_extension":
        wf_id = getattr(entry, "workflow_id", None)
        mcp_id = getattr(entry, "mcp_server_id", None)
        if wf_id is not None and wf_id in ctx.selected_workflow_ids:
            return True
        if mcp_id is not None and mcp_id in ctx.selected_mcp_server_ids:
            return True
    return False


def _matches_scene_routed(entry: RegisteredTool, ctx: AssemblyInput) -> bool:
    gov = entry.governance
    if entry.name in ctx.skill_recommended:
        return True
    if ctx.scene_id in MULTI_TURN_READ_SCENES and entry.name == "python_analysis":
        return True
    if entry.name == "todo_write":
        return (
            ctx.scene_id not in L1_LIGHT_SCENES
            or ctx.requires_plan
            or ctx.has_active_plan
        )
    if gov.default_scenes and ctx.scene_id in gov.default_scenes:
        return True
    if gov.default_tasks and ctx.task in gov.default_tasks and not gov.default_scenes:
        return True
    return False


def should_direct_stream(ctx: AssemblyInput) -> bool:
    """Pure clarification / out-of-scope / restricted scenes bypass the work tool turn."""
    return (
        ctx.caller_role == "root"
        and ctx.scene_id in DIRECT_STREAM_SCENES
        and not ctx.selected_packs
        and not ctx.selected_workflow_ids
        and not ctx.selected_mcp_server_ids
        and not ctx.has_active_goal
    )


def assemble_step(
    entries: Mapping[str, RegisteredTool],
    ctx: AssemblyInput,
) -> ToolAssemblyResult:
    # Step 1: Hard permission, maintainer capability, scope ceiling & caller-role filtering
    authorized: dict[str, RegisteredTool] = {}
    excluded: list[tuple[str, str]] = []
    permission_filtered = 0

    for name, entry in entries.items():
        if entry.definition is None:
            continue
        if not ctx.plugins_permitted and name.startswith(("mcp_", "workflow_")):
            permission_filtered += 1
            continue
        if entry.governance.requires_semantic_maintainer and not ctx.is_semantic_maintainer:
            permission_filtered += 1
            continue
        if ctx.scope_ceiling is not None:
            if name not in ctx.scope_ceiling and not name.startswith(ctx.scope_plugin_prefixes):
                permission_filtered += 1
                continue
        if ctx.caller_role == "subagent":
            if entry.name == "final_answer" or not eligible_for_subagent(entry):
                excluded.append((name, "role"))
                continue
        else:
            if entry.governance.pack == "subagent_base":
                excluded.append((name, "role"))
                continue
        if ctx.caller_role == "scheduler" and not entry.governance.scheduler_allowed:
            excluded.append((name, "role"))
            continue
        authorized[name] = entry

    # Step 2: Phase convergence gate (identify / verify / answer_stream / delivery_only)
    if ctx.phase in {"identify", "answer_stream"} or ctx.turn_purpose == "verify":
        for name in authorized:
            excluded.append((name, "stage"))
        trace = ToolAssemblyTrace(
            authorized_count=len(authorized),
            exposed_count=0,
            deferred_count=0,
            excluded_count=len(excluded),
            permission_filtered_count=permission_filtered,
            declaration_bytes=2,
            declaration_tokens=1,
            exposed=(),
            deferred=(),
            excluded=tuple(excluded),
        )
        return ToolAssemblyResult([], authorized, {}, trace)

    if ctx.delivery_only:
        completion_defs: list[ToolDefinition] = []
        exposed_items: list[ExposedToolTrace] = []
        for name, entry in authorized.items():
            if entry.completion and entry.definition is not None:
                completion_defs.append(entry.definition.model_copy(deep=True))
                exposed_items.append(ExposedToolTrace(name, "always_on", entry.governance.pack))
            else:
                excluded.append((name, "stage"))
        raw_bytes = len(json_bytes([d.model_dump() for d in completion_defs]))
        trace = ToolAssemblyTrace(
            authorized_count=len(authorized),
            exposed_count=len(completion_defs),
            deferred_count=0,
            excluded_count=len(excluded),
            permission_filtered_count=permission_filtered,
            declaration_bytes=raw_bytes,
            declaration_tokens=max(1, raw_bytes // 4),
            exposed=tuple(exposed_items),
            deferred=(),
            excluded=tuple(excluded),
        )
        return ToolAssemblyResult(completion_defs, authorized, {}, trace)

    # Step 3: Three-channel deterministic partition (AlwaysOn -> UserSelected -> SceneRouted)
    always_on_list: list[RegisteredTool] = []
    user_static_list: list[RegisteredTool] = []
    user_ext_list: list[RegisteredTool] = []
    scene_routed_list: list[RegisteredTool] = []
    deferred_candidates: dict[str, RegisteredTool] = {}
    auto_workflow_fallback: list[RegisteredTool] = []

    for name, entry in authorized.items():
        gov = entry.governance
        if gov.channel == "always_on":
            always_on_list.append(entry)
        elif _matches_user_selected(entry, ctx):
            if gov.pack == "user_extension":
                user_ext_list.append(entry)
            else:
                user_static_list.append(entry)
        elif gov.channel == "scene_routed" and _matches_scene_routed(entry, ctx):
            scene_routed_list.append(entry)
        elif (
            name.startswith("workflow_")
            and ctx.scene_id in AUTOMATION_SCENES
            and not ctx.selected_workflow_ids
            and len(auto_workflow_fallback) < 4
        ):
            auto_workflow_fallback.append(entry)
        else:
            deferred_candidates[name] = entry
            if gov.channel == "scene_routed":
                excluded.append((name, "scene"))

    user_ext_sorted = sorted(user_ext_list, key=lambda e: e.name)
    for overflow_ext in user_ext_sorted[USER_SELECTED_EXTENSION_LIMIT:]:
        deferred_candidates[overflow_ext.name] = overflow_ext
        excluded.append((overflow_ext.name, "budget"))
    capped_ext = user_ext_sorted[:USER_SELECTED_EXTENSION_LIMIT]

    ordered_plan: list[tuple[RegisteredTool, str]] = [
        *((entry, "always_on") for entry in always_on_list),
        *((entry, "user_selected") for entry in user_static_list),
        *((entry, "user_selected") for entry in capped_ext),
        *((entry, "scene_routed") for entry in auto_workflow_fallback),
        *((entry, "scene_routed") for entry in scene_routed_list),
    ]

    # Step 4: Enforce <= 32 slots and <= 64 KiB declaration budget
    selected_defs: list[ToolDefinition] = []
    exposed_trace: list[ExposedToolTrace] = []
    seen_names: set[str] = set()
    for entry, channel_source in ordered_plan:
        if entry.name in seen_names:
            continue
        seen_names.add(entry.name)
        assert entry.definition is not None
        candidate_def = entry.definition.model_copy(deep=True)
        trial = [*selected_defs, candidate_def]
        if len(trial) <= TOOL_CATALOG_LIMIT and len(
            json_bytes([d.model_dump() for d in trial])
        ) <= TOOL_DECLARATION_BYTES_LIMIT:
            selected_defs.append(candidate_def)
            exposed_trace.append(ExposedToolTrace(entry.name, channel_source, entry.governance.pack))
        else:
            deferred_candidates[entry.name] = entry
            excluded.append((entry.name, "budget"))

    validate_visible_catalog(selected_defs)
    raw_bytes = len(json_bytes([d.model_dump() for d in selected_defs]))
    trace = ToolAssemblyTrace(
        authorized_count=len(authorized),
        exposed_count=len(selected_defs),
        deferred_count=len(deferred_candidates),
        excluded_count=len(excluded),
        permission_filtered_count=permission_filtered,
        declaration_bytes=raw_bytes,
        declaration_tokens=max(1, raw_bytes // 4),
        exposed=tuple(exposed_trace),
        deferred=tuple(deferred_candidates.keys()),
        excluded=tuple(excluded),
    )
    return ToolAssemblyResult(selected_defs, authorized, deferred_candidates, trace)
```

---

## 3. 前端「工具与扩展」手动选择器契约与组件骨架

- **目标文件**：
  - `apps/agent/src/compass_agent/schemas.py`（新增 `ToolSelection`，统一挂载于 `RunConstraints.tool_selection`，自然复用既有 `SessionSubmit.constraints` 并随长任务 `GoalController.process()` 自动跨轮继承）
  - `apps/web/src/components/analysis-composer.tsx`（新增 `ToolSelectionBar` 与已选徽标栏）
- **变更语义**：
  1. `ToolSelection` 仅承载枚举化的能力包名（`packs`）及用户本人的 `workflowIds` / `mcpServerIds`，避免前端直接传递原始工具名字符串列表。
  2. 前端选择器依据 Web 内部鉴权透传的 `semantic_maintainer` 能力对非维护者隐藏 `semantic_govern` 勾选项，并在本地已选扩展工具数达到 `12` 个上限时置灰其余未勾选扩展项（展示 `COPY-06`）。

```python
# 设计示意：apps/agent/src/compass_agent/schemas.py（挂载于既有 RunConstraints）
from typing import Literal
from uuid import UUID
from pydantic import Field

SelectablePack = Literal["long_task", "workflow_pack", "semantic_govern", "python_sandbox"]


class ToolSelection(APIModel):
    packs: list[SelectablePack] = Field(default_factory=list, max_length=4)
    workflow_ids: list[UUID] = Field(default_factory=list, alias="workflowIds", max_length=12)
    mcp_server_ids: list[UUID] = Field(default_factory=list, alias="mcpServerIds", max_length=8)

    def is_empty(self) -> bool:
        return not (self.packs or self.workflow_ids or self.mcp_server_ids)


# 在既有 RunConstraints 中增量挂载，SessionSubmit / AnalysisRunInput / Goal 续跑天然共用：
# class RunConstraints(RunContractModel):
#     plan: Literal["required"] | None = None
#     max_steps: int | None = ...
#     timeout_seconds: int | None = ...
#     tool_selection: ToolSelection | None = Field(default=None, alias="toolSelection")
```

```tsx
// 设计示意：apps/web/src/components/analysis-composer.tsx（「工具与扩展」手动选择浮层骨架）
import type { SelectablePack, ToolSelection } from "@/generated/agent-types";

const MAX_MANUAL_EXTENSIONS = 12;

interface ExtensionOption {
  id: string;
  name: string;
  toolCount: number;
}

export function ToolSelectionBar({
  value,
  canGovernSemantics,
  workflows,
  mcpServers,
  onChange,
}: {
  value: ToolSelection;
  canGovernSemantics: boolean;
  workflows: ExtensionOption[];
  mcpServers: ExtensionOption[];
  onChange: (next: ToolSelection) => void;
}) {
  const selectedExtTools =
    value.workflowIds.length +
    mcpServers
      .filter((s) => value.mcpServerIds.includes(s.id))
      .reduce((sum, s) => sum + s.toolCount, 0);
  const totalSelectedBadges =
    value.packs.length + value.workflowIds.length + value.mcpServerIds.length;

  const togglePack = (pack: SelectablePack) => {
    const exists = value.packs.includes(pack);
    onChange({
      ...value,
      packs: exists ? value.packs.filter((p) => p !== pack) : [...value.packs, pack],
    });
  };

  const toggleWorkflow = (id: string) => {
    const exists = value.workflowIds.includes(id);
    if (!exists && selectedExtTools + 1 > MAX_MANUAL_EXTENSIONS) return;
    onChange({
      ...value,
      workflowIds: exists
        ? value.workflowIds.filter((item) => item !== id)
        : [...value.workflowIds, id],
    });
  };

  return (
    <div className="composer-tool-picker">
      <button type="button" className="composer-pill" aria-label="工具与扩展选择">
        {totalSelectedBadges > 0 ? `工具与扩展 (${totalSelectedBadges})` : "工具与扩展"}
      </button>
      <div className="composer-popover" role="group" aria-label="手动装载工具组与扩展">
        <div className="picker-section">
          <strong>能力增强组</strong>
          <label>
            <input
              type="checkbox"
              checked={value.packs.includes("long_task")}
              onChange={() => togglePack("long_task")}
            />
            长任务目标 (goal)
          </label>
          <label>
            <input
              type="checkbox"
              checked={value.packs.includes("workflow_pack")}
              onChange={() => togglePack("workflow_pack")}
            />
            工作流编排
          </label>
          <label>
            <input
              type="checkbox"
              checked={value.packs.includes("python_sandbox")}
              onChange={() => togglePack("python_sandbox")}
            />
            Python 计算绘图
          </label>
          {canGovernSemantics && (
            <label>
              <input
                type="checkbox"
                checked={value.packs.includes("semantic_govern")}
                onChange={() => togglePack("semantic_govern")}
              />
              指标口径治理
            </label>
          )}
        </div>
        <div className="picker-section">
          <strong>个人工作流 ({selectedExtTools}/{MAX_MANUAL_EXTENSIONS})</strong>
          {workflows.map((wf) => {
            const checked = value.workflowIds.includes(wf.id);
            const disabled = !checked && selectedExtTools + 1 > MAX_MANUAL_EXTENSIONS;
            return (
              <label key={wf.id}>
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={disabled}
                  onChange={() => toggleWorkflow(wf.id)}
                />
                {wf.name}
              </label>
            );
          })}
          {selectedExtTools >= MAX_MANUAL_EXTENSIONS && (
            <p className="muted">单次最多手动装载 12 个扩展工具，请取消部分已选项后再选。</p>
          )}
        </div>
      </div>
    </div>
  );
}
```

---

## 4. 极简输入适配、流式循证收口与结构化可自愈错误

- **目标文件**：
  - `apps/agent/src/compass_agent/tools/schema_compact.py`（新增：扁平 Schema 净化、多连接 SQL 路由、抢跑 `final_answer` 延期回执门禁、流式循证 `publish_citations` 精确锚点构建、外简内全状态机适配与周期转换）
  - `apps/agent/src/compass_agent/tools/errors.py`（新增）
  - `apps/agent/src/compass_agent/tools/builtin/database_query/__init__.py`（修改：含 `SAVEPOINT` 防 `25P02`、元数据 `kind="metadata"` 与 `resultId`/`sourceQueryId` 双写）
  - `apps/agent/src/compass_agent/workflows/engine.py`（修改：双向兼容 `{tasks}` 与 `{prompt}`）
- **变更语义**：
  1. `compact_json_schema()` 自动剥离 Pydantic 生成的 `"title"`、`anyOf: [T, {"type": "null"}]` 与机械校验装饰；`route_connection_for_sql()` 识别纯 `information_schema` 元数据查询并在多连接工作区触发并行聚合（打标 `kind="metadata"` 防误判 `transient` 阻塞附件保存），多连接同名 schema 冲突时返回结构化引导。
  2. `partition_terminal_final_answer()` 对同批抢跑的 `final_answer` 合成标准 `role="tool"` 延期回执（严禁静默删除 `tool_call` 导致上游 API 400）；`assemble_ordered_evidences()` 与 `build_stream_citations()` 支撑 `answerProtocol == 3` 自由流式循证输出，并生成完全兼容既有 `publish_citations` 与 `<CitedAnswer>` 弹窗的精确 `{anchors, details}`。
  3. 外简内全适配函数（`adapt_slim_todos`、`adapt_slim_goal`、`adapt_child_report`、`resolve_workflow_subagent_call`）保持 `ExecutionPlan`、`GoalController`/`gate.py`、`ChildReturn`/`Ledger` 与 `workflows/engine.py` 无损兼容。
  4. `ActionableToolError`（`<= 4 KiB`、最多 `8` 项候选）与 `authorized_columns_for_relations` 在 `ROLLBACK TO SAVEPOINT dq_exec` 恢复事务态后安全投影当前账号具备 `SELECT` 权限的可见列候选。

```python
# 设计示意：apps/agent/src/compass_agent/tools/schema_compact.py
from __future__ import annotations

import re
from uuid import UUID, uuid5
from compass_agent.runtime.private_state import digest
from compass_agent.tools.registry import parse_arguments

_WEEKDAY_CRON = {"MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6, "SUN": 0}
_DAILY_RE = re.compile(r"^daily\s+([01]?\d|2[0-3]):([0-5]\d)$", re.IGNORECASE)
_WEEKLY_RE = re.compile(
    r"^weekly\s+(MON|TUE|WED|THU|FRI|SAT|SUN)\s+([01]?\d|2[0-3]):([0-5]\d)$",
    re.IGNORECASE,
)
_CITE_BADGE_RE = re.compile(r"(?:\^\[(\d+)\]\^|\[(\d+)\])")
_VALID_ARTIFACTS = frozenset({"markdown", "csv", "dashboard"})


def compact_json_schema(schema: object) -> object:
    """Strip Pydantic title/default-null/nullable-anyOf bloat for clean model exposure."""
    if isinstance(schema, list):
        return [compact_json_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    if "anyOf" in schema and isinstance(schema["anyOf"], list):
        non_null = [
            b for b in schema["anyOf"] if not (isinstance(b, dict) and b.get("type") == "null")
        ]
        if len(non_null) == 1 and isinstance(non_null[0], dict):
            merged = {**non_null[0]}
            if "description" in schema:
                merged["description"] = schema["description"]
            return compact_json_schema(merged)
    dropped = {"title", "discriminator", "minLength", "maxLength", "format", "pattern"}
    out: dict[str, object] = {}
    for k, v in schema.items():
        if k in dropped or (k == "default" and v is None):
            continue
        out[k] = compact_json_schema(v) if k != "properties" else {
            pk: compact_json_schema(pv) for pk, pv in v.items()  # type: ignore[union-attr]
        }
    return out


def is_metadata_only_sql(relations: list[str]) -> bool:
    """True when SQL reads only information_schema metadata (tagged kind='metadata', not transient)."""
    return bool(relations) and all(
        rel.strip('"').startswith("information_schema.") for rel in relations
    )


def route_connection_for_sql(
    relations: list[str],
    bindings: tuple,
    schema_owners_map: dict[str, list[object]],
) -> tuple[str, object]:
    """Deterministically route single-parameter database_query(sql); supports multi-connection metadata."""
    if not bindings:
        return "none", None
    if is_metadata_only_sql(relations):
        return ("single", bindings[0]) if len(bindings) == 1 else ("aggregate_metadata", bindings)
    if len(bindings) == 1:
        return "single", bindings[0]
    schemas = {
        rel.split(".", 1)[0].strip('"')
        for rel in relations
        if "." in rel and not rel.startswith("information_schema.")
    }
    candidate_sets = [set(schema_owners_map.get(s, ())) for s in schemas if s in schema_owners_map]
    if candidate_sets:
        common = set.intersection(*candidate_sets)
        if len(common) == 1:
            return "single", next(iter(common))
        if len(common) > 1:
            return "ambiguous", sorted(getattr(b, "name", "") for b in common)
    return "single", bindings[0]


def partition_terminal_final_answer(
    tool_calls: list,
) -> tuple[list, list[tuple[str, dict]], str | None, str | None]:
    """Never silently drop a premature final_answer: return a compliant role='tool' deferred receipt."""
    work_calls = [c for c in tool_calls if c.function.name != "final_answer"]
    final_calls = [c for c in tool_calls if c.function.name == "final_answer"]
    if work_calls:
        deferred_receipts = [
            (
                c.id,
                {
                    "status": "deferred",
                    "code": "final_answer_must_be_terminal",
                    "message": "本批其他工作工具正在执行，请待数据返回并确认充分后再单独调用 final_answer。",
                },
            )
            for c in final_calls
        ]
        return work_calls, deferred_receipts, None, None
    if not final_calls:
        return [], [], None, None
    args = parse_arguments(final_calls[0].function.arguments)
    mode = args.get("mode") if isinstance(args, dict) else None
    artifact = args.get("artifactFormat") if isinstance(args, dict) else None
    valid_artifact = artifact if artifact in _VALID_ARTIFACTS else None
    return [], [], ("evidence" if mode == "evidence" else "plain"), valid_artifact


def assemble_ordered_evidences(raw_evidences: dict[str, dict]) -> tuple[str, dict[int, dict]]:
    """Filter, rank, and number verified evidences [1]..[N] for evidence Answer Prompt injection."""
    priority = {"query": 0, "definition": 1, "document": 2, "artifact": 3, "user_input": 4}
    valid = [
        item for item in raw_evidences.values()
        if item.get("record") is not None and item.get("title")
    ]
    valid.sort(key=lambda x: (priority.get(x.get("kind", ""), 9), x.get("id", "")))
    blocks: list[str] = []
    index_map: dict[int, dict] = {}
    for idx, item in enumerate(valid[:24], start=1):
        index_map[idx] = item
        record = item["record"]
        excerpt = record.get("text") or record.get("summary") or str(record)[:600]
        blocks.append(f"[{idx}] 来源：{item['title']}（{item.get('locator', '')}）\n{excerpt}")
    return "\n\n".join(blocks), index_map


def build_stream_citations(
    run_id: UUID,
    markdown_text: str,
    index_map: dict[int, dict],
) -> dict[str, object]:
    """Parse ^[1]^ (or [1]) markers from streamed Markdown into publish_citations() {anchors, details}."""
    anchors: list[dict] = []
    details: dict[str, dict] = {}
    for match in _CITE_BADGE_RE.finditer(markdown_text):
        ordinal = int(match.group(1) or match.group(2))
        item = index_map.get(ordinal)
        if item is None:
            continue
        cid = str(uuid5(run_id, f"stream-citation:{ordinal}:{item['id']}"))
        anchors.append(
            {
                "citationId": cid,
                "ordinal": ordinal,
                "start": match.start(),
                "end": match.end(),
                "marker": match.group(0),
            }
        )
        if cid not in details:
            record = item["record"]
            details[cid] = {
                "citationId": cid,
                "claimId": str(uuid5(run_id, f"stream-claim:{ordinal}")),
                "revision": 1,
                "status": "supported",
                "statement": item["title"],
                "calculation": None,
                "sources": [
                    {
                        "kind": "detail" if item["kind"] in {"query", "artifact"} else item["kind"],
                        "title": item["title"],
                        "version": item.get("version", ""),
                        "locator": item.get("locator", ""),
                        "scope": "",
                        "excerpt": str(record.get("text") or record.get("summary") or record)[:800],
                    }
                ],
            }
        if len(anchors) >= 96:
            break
    return {"anchors": anchors, "details": details}


def adapt_slim_todos(plan, raw_args: dict, step_index: int) -> dict:
    """Outer-slim, inner-full lossless adapter: map {todos:[{step,status}]} -> ExecutionPlan snapshot."""
    if "op" in raw_args:  # Backward-compatible replay of legacy op-based checkpoints
        return raw_args
    todos = raw_args.get("todos") or []
    status_map = {"done": "verified", "skipped": "skipped", "pending": "pending", "in_progress": "pending"}
    selected_id = None
    items = {}
    statuses = {}
    for idx, row in enumerate(todos[:6], start=1):
        tid = f"t{idx}"
        items[tid] = {"id": tid, "goal": row["step"][:200], "dependsOn": [], "optional": False}
        statuses[tid] = status_map.get(row["status"], "pending")
        if row["status"] == "in_progress" and selected_id is None:
            selected_id = tid
    plan.version += 1
    plan.first_step = plan.first_step or step_index
    plan.selected = plan.active = selected_id
    return {"version": plan.version, "selectedTodoId": selected_id, "items": items, "statuses": statuses}


def adapt_slim_goal(raw_args: dict, open_goal, step_index: int, call_seq: int) -> dict:
    """Outer-slim, inner-full adapter preserving GoalController / Ledger / gate.py acceptance checks."""
    if "op" in raw_args:  # Backward-compatible replay of legacy goal payloads
        return raw_args
    action = raw_args["action"]
    if action == "propose":
        acceptance = [
            {"id": f"a{i}", "text": text}
            for i, text in enumerate(raw_args.get("acceptance") or [], start=1)
        ]
        return {
            "op": "propose",
            "contract": {"objective": raw_args["objective"], "acceptance": acceptance},
            "goalId": str(open_goal.id) if open_goal else None,
            "expectedRevision": open_goal.revision if open_goal else None,
        }
    if action == "record":
        ev_ids = list(raw_args.get("evidenceIds") or [])
        acc_ids = [f"a{i}" for i in (raw_args.get("acceptanceIndexes") or []) if isinstance(i, int) and i >= 1]
        return {
            "op": "record",
            "items": [
                {
                    "type": "conclusion" if ev_ids else "failure",
                    "key": f"finding.{step_index}.{call_seq}",
                    "title": (raw_args.get("finding") or "阶段记录")[:200],
                    "text": raw_args.get("finding"),
                    "evidenceIds": ev_ids,
                    "acceptanceIds": acc_ids,
                }
            ],
        }
    if action == "complete":
        return {"op": "complete", "expectedRevision": open_goal.revision if open_goal else 1}
    return {"op": "block", "reason": (raw_args.get("objective") or "等待外部决策")[:60]}


def compile_schedule_expr(expr: str) -> str:
    """Accept 'daily 09:00', 'weekly MON 09:00', or standard 5-field Cron."""
    text = expr.strip()
    if m := _DAILY_RE.match(text):
        return f"{int(m.group(2))} {int(m.group(1))} * * *"
    if m := _WEEKLY_RE.match(text):
        dow = _WEEKDAY_CRON[m.group(1).upper()]
        return f"{int(m.group(3))} {int(m.group(2))} * * {dow}"
    return text
```

```python
# 设计示意：apps/agent/src/compass_agent/tools/errors.py
from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches
from typing import Literal

from jsonschema import Draft202012Validator, ValidationError as JsonSchemaError

from compass_agent.tools.contracts import json_bytes

TOOL_ERROR_MAX_BYTES = 4096
MAX_ERROR_CANDIDATES = 8

ErrorCategory = Literal[
    "fix_arguments",
    "switch_tool",
    "clarify_with_user",
    "retry_transient",
    "permission_denied",
]


@dataclass(frozen=True, slots=True)
class ActionableToolError:
    code: str
    category: ErrorCategory
    message: str
    retryable: bool = False
    field: str | None = None
    candidates: tuple[str, ...] = ()
    next_action: str | None = None
    suggested_tool: str | None = None

    def to_payload(self, *, status: str = "error") -> dict[str, object]:
        capped = tuple(c[:80] for c in self.candidates[:MAX_ERROR_CANDIDATES])
        payload: dict[str, object] = {
            "status": status,
            "code": self.code,
            "category": self.category,
            "retryable": self.retryable,
            "message": self.message[:800],
        }
        if self.field:
            payload["field"] = self.field[:120]
        if capped:
            payload["candidates"] = list(capped)
        if self.next_action:
            payload["nextAction"] = self.next_action[:400]
        if self.suggested_tool:
            payload["suggestedTool"] = self.suggested_tool
        while len(json_bytes(payload)) > TOOL_ERROR_MAX_BYTES and payload.get("candidates"):
            payload["candidates"] = list(payload["candidates"])[:-1]  # type: ignore[index]
            if not payload["candidates"]:
                payload.pop("candidates", None)
        return payload


def schema_validation_error(schema: dict, args: object) -> ActionableToolError | None:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(args), key=lambda err: (len(err.path), str(err.path)))
    if not errors:
        return None
    first: JsonSchemaError = errors[0]
    field_path = ".".join(str(p) for p in first.path) or None
    if first.validator == "required":
        return ActionableToolError(
            code="invalid_parameters",
            category="fix_arguments",
            retryable=True,
            field=field_path,
            message=f"缺少必填参数：{first.message}",
            next_action="补齐缺失的必填字段后重新调用。",
        )
    if first.validator == "enum" and isinstance(first.validator_value, list):
        return ActionableToolError(
            code="invalid_parameters",
            category="fix_arguments",
            retryable=True,
            field=field_path,
            message=f"参数枚举值不受支持：{first.message}",
            candidates=tuple(str(v) for v in first.validator_value),
            next_action="从 candidates 合法枚举值中选择一项重新提交。",
        )
    return ActionableToolError(
        code="invalid_parameters",
        category="fix_arguments",
        retryable=True,
        field=field_path,
        message=f"参数格式不符合工具契约：{first.message[:240]}",
        next_action="核对字段类型约束后重试。",
    )


def closest_authorized_columns(missing_hint: str | None, authorized_columns: list[str]) -> tuple[str, ...]:
    if not authorized_columns:
        return ()
    if missing_hint:
        matched = get_close_matches(missing_hint, authorized_columns, n=MAX_ERROR_CANDIDATES, cutoff=0.4)
        if matched:
            return tuple(matched)
    return tuple(authorized_columns[:MAX_ERROR_CANDIDATES])
```

```python
# 设计示意：database_query 安全分级错误投影
import re
import psycopg
from compass_agent.tools.errors import ActionableToolError, closest_authorized_columns

_MISSING_COL_RE = re.compile(r'column "?([a-zA-Z0-9_]+)"? does not exist', re.IGNORECASE)


async def authorized_columns_for_relations(cursor, relations: list[str]) -> list[str]:
    """Rollback to SAVEPOINT first to escape PostgreSQL 25P02 aborted-tx state, then query SELECT-able columns."""
    if not relations:
        return []
    await cursor.execute("ROLLBACK TO SAVEPOINT dq_exec")
    await cursor.execute(
        """
        SELECT DISTINCT c.table_schema || '.' || c.table_name || '.' || c.column_name
        FROM information_schema.columns c
        WHERE pg_catalog.quote_ident(c.table_schema) || '.' || pg_catalog.quote_ident(c.table_name) = ANY(%s)
          AND pg_catalog.has_column_privilege(
              pg_catalog.format('%%I.%%I', c.table_schema, c.table_name), c.column_name, 'SELECT'
          )
        ORDER BY 1
        LIMIT 40
        """,
        (relations,),
    )
    return [row[0] for row in await cursor.fetchall()]


def classify_postgres_error(error: psycopg.Error, visible_cols: list[str]) -> ActionableToolError:
    sqlstate = getattr(error, "sqlstate", None)
    diag_msg = getattr(getattr(error, "diag", None), "message_primary", "") or ""
    if sqlstate == "42703":
        match = _MISSING_COL_RE.search(diag_msg)
        missing = match.group(1) if match else None
        short_cols = [col.rsplit(".", 1)[-1] for col in visible_cols]
        return ActionableToolError(
            code="sql_undefined_column",
            category="fix_arguments",
            retryable=True,
            field="sql",
            message=f"SQL 引用的字段不存在或当前账号不可见{f'：{missing}' if missing else ''}。",
            candidates=closest_authorized_columns(missing, short_cols),
            next_action="使用 candidates 中的可见列名修正 SQL，或查询 information_schema.columns 核对表结构。",
        )
    if sqlstate == "42601":
        return ActionableToolError(
            code="sql_syntax_error",
            category="fix_arguments",
            retryable=True,
            field="sql",
            message="PostgreSQL 语法解析失败，请检查括号、引号、GROUP BY 列及 schema.table 限定名。",
            next_action="修正单条只读 SELECT 语句语法后重试。",
        )
    if sqlstate in {"42P01", "42501", "3F000"}:
        return ActionableToolError(
            code="sql_relation_unavailable",
            category="fix_arguments",
            retryable=True,
            field="sql",
            message="SQL 引用的 schema.table 不存在或不在当前工作区授权范围内。",
            next_action="先查询 information_schema.tables 获取当前账号已授权的表名清单。",
        )
    return ActionableToolError(
        code="database_query_failed",
        category="fix_arguments",
        retryable=False,
        field="sql",
        message="数据库执行被拒绝（常见原因：除零、类型转换失败或超出表达式限制）。",
        next_action="检查类型转换、NULLIF 除零保护或改用已发布的 query_metric 指标查询。",
    )
```

---

## 5. 运行工具绑定与跨进程断点恢复衔接

- **目标文件**：
  - `apps/agent/src/compass_agent/runtime/tool_bindings.py`（修改）
  - `apps/agent/src/compass_agent/runtime/journal.py`（修改：跨版本存量快照恢复兼容）
- **变更语义**：
  1. `RunToolBindings.with_workflows()` 将个人工作流注册容量从旧的 `TOOL_CATALOG_LIMIT (32)` 放宽至 `TOOL_REGISTRY_POOL_LIMIT (128)`；外部 MCP 仅在用户显式勾选 `mcpServerIds` 或恢复既有绑定时拉取探测，避免普通查数被故障 MCP 拖慢 TTFT。
  2. 从 `AgentToolBinding.binding["connection_id"]` 还原 `mcp_server_id`、从 `workflow_<hex>` 还原 `workflow_id`，使跨进程恢复后通道三精确匹配。
  3. 恢复升级前冻结的存量持久 Run 时，若检测到历史 `AgentToolBinding` 快照与当前扁平 Schema 属于不同代次，直接按冻结 `declaration` 回退装载（跳过 `tool_catalog_changed` 强比对），工具执行由外简内全适配层兼容解析旧参数。

```python
# 设计示意：apps/agent/src/compass_agent/runtime/tool_bindings.py（按需绑定、资产 ID 还原与跨版本恢复）
from dataclasses import replace
from uuid import UUID
from compass_agent.schemas import ToolDefinition, ToolSelection
from compass_agent.tools.catalog_contracts import DEFAULT_EXTENSION_GOVERNANCE
from compass_agent.tools.contracts import McpBinding
from compass_agent.tools.registry import TOOL_REGISTRY_POOL_LIMIT


def bind_extension_entries(
    static_entries: list,
    workflow_entries: list,
    mcp_entries: list,
    *,
    frozen_snapshot: dict[str, tuple[dict, dict]] | None = None,
) -> list:
    """Register authorized static, workflow, and on-demand MCP tools up to pool limit 128."""
    resolved_static = []
    for entry in static_entries:
        # Cross-version resume: honor frozen historical declaration instead of failing tool_catalog_changed
        if frozen_snapshot and entry.name in frozen_snapshot:
            stored_decl, _ = frozen_snapshot[entry.name]
            if stored_decl:
                entry = replace(entry, definition=ToolDefinition.model_validate(stored_decl))
        resolved_static.append(entry)

    budget = max(0, TOOL_REGISTRY_POOL_LIMIT - len(resolved_static))
    combined_ext = []
    for entry in [*workflow_entries, *mcp_entries][:budget]:
        wf_id = (
            UUID(hex=entry.name.removeprefix("workflow_"))
            if entry.name.startswith("workflow_")
            else getattr(entry, "workflow_id", None)
        )
        mcp_id = (
            entry.binding.connection_id
            if isinstance(entry.binding, McpBinding)
            else getattr(entry, "mcp_server_id", None)
        )
        combined_ext.append(
            replace(
                entry,
                governance=DEFAULT_EXTENSION_GOVERNANCE,
                workflow_id=wf_id,
                mcp_server_id=mcp_id,
            )
        )
    return [*resolved_static, *combined_ext]


def restore_tool_selection(raw_constraints: dict | None) -> tuple[frozenset[str], frozenset[UUID], frozenset[UUID]]:
    """Safely restore RunConstraints.toolSelection across durable resume and Goal multi-rounds."""
    if not isinstance(raw_constraints, dict) or not raw_constraints.get("toolSelection"):
        return frozenset(), frozenset(), frozenset()
    parsed = ToolSelection.model_validate(raw_constraints["toolSelection"])
    return (
        frozenset(parsed.packs),
        frozenset(parsed.workflow_ids),
        frozenset(parsed.mcp_server_ids),
    )
```

---

## 6. Tool Eval 六维指标评估器与 Web 组装轨迹组件

- **目标文件**：
  - `apps/agent/tests/tool_eval/metrics.py`（新增）
  - `apps/web/src/features/contextops/panels.tsx`（增量新增 `ToolAssemblySection`）
- **变更语义**：
  1. `metrics.py` 对 A/B/C 三组运行轨迹统一计算六项核心指标，对分母为 `0` 的子集返回 `None`，拒绝伪造比率。
  2. 前端 `ToolAssemblySection` 对齐 PRD `UI-01` 与 `COPY-01`～`COPY-04`，清晰展示 `[常驻]`、`[场景]`、`[手动]` 三类通道徽标，并对权限裁剪项仅显示脱敏汇总数。

```python
# 设计示意：apps/agent/tests/tool_eval/metrics.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CaseEvalRecord:
    case_id: str
    completed: bool
    expected_primary_tools: frozenset[str]
    forbidden_tools: frozenset[str]
    called_tools: tuple[str, ...]
    valid_argument_calls: int
    total_calls: int
    first_turn_succeeded: bool
    redundant_calls: int
    recoverable_errors: int
    recovered_errors: int
    declaration_tokens_sum: int
    prompt_tokens_sum: int
    clarifications: int
    elapsed_ms: int
    cost_microusd: int


@dataclass(frozen=True, slots=True)
class GroupEvalSummary:
    group: str
    case_count: int
    tool_selection_accuracy: float
    argument_validity_rate: float | None
    first_turn_success_rate: float
    redundant_call_rate: float | None
    error_recovery_rate: float | None
    tool_declaration_token_share: float | None
    task_completion_rate: float
    avg_clarifications: float
    avg_elapsed_ms: float
    avg_cost_microusd: float


def summarize_group(group: str, records: list[CaseEvalRecord]) -> GroupEvalSummary:
    if not records:
        raise ValueError("evaluation records must not be empty")
    n = len(records)
    selection_hits = sum(
        1
        for r in records
        if r.expected_primary_tools.issubset(r.called_tools)
        and not r.forbidden_tools.intersection(r.called_tools)
    )
    total_calls = sum(r.total_calls for r in records)
    valid_calls = sum(r.valid_argument_calls for r in records)
    redundant_calls = sum(r.redundant_calls for r in records)
    recoverable = sum(r.recoverable_errors for r in records)
    recovered = sum(r.recovered_errors for r in records)
    decl_tokens = sum(r.declaration_tokens_sum for r in records)
    prompt_tokens = sum(r.prompt_tokens_sum for r in records)

    return GroupEvalSummary(
        group=group,
        case_count=n,
        tool_selection_accuracy=selection_hits / n,
        argument_validity_rate=(valid_calls / total_calls) if total_calls else None,
        first_turn_success_rate=sum(1 for r in records if r.first_turn_succeeded) / n,
        redundant_call_rate=(redundant_calls / total_calls) if total_calls else None,
        error_recovery_rate=(recovered / recoverable) if recoverable else None,
        tool_declaration_token_share=(decl_tokens / prompt_tokens) if prompt_tokens else None,
        task_completion_rate=sum(1 for r in records if r.completed) / n,
        avg_clarifications=sum(r.clarifications for r in records) / n,
        avg_elapsed_ms=sum(r.elapsed_ms for r in records) / n,
        avg_cost_microusd=sum(r.cost_microusd for r in records) / n,
    )
```

```tsx
// 设计示意：apps/web/src/features/contextops/panels.tsx 三通道工具组装折叠区片段
import { Disclosure } from "@/components/ui/disclosure";
import { num, phaseShort, type FlowNode } from "./client";

const channelLabel: Record<string, string> = {
  always_on: "常驻",
  scene_routed: "场景",
  user_selected: "手动",
};

const reasonLabel: Record<string, string> = {
  scene: "场景分流未命中",
  stage: "阶段收敛",
  role: "运行身份限制",
  budget: "目录容量预算",
};

export function ToolAssemblySection({ node }: { node: FlowNode }) {
  const assembly = node.toolAssembly;
  if (!assembly) {
    return (
      <p className="muted">历史运行未记录三通道组装明细，仅展示实际暴露工具列表。</p>
    );
  }
  const title =
    `工具组装决策 · 授权池 ${assembly.authorizedCount} → 本步装载 ${assembly.exposedCount}` +
    ` · 未挂载池 ${assembly.deferredCount} · 规则裁剪 ${assembly.excludedCount}` +
    ` · ${num(assembly.declarationTokens, 0)} tok`;

  return (
    <Disclosure title={title}>
      {assembly.exposedCount === 0 ? (
        <p className="muted">
          本阶段不暴露工具（阶段策略：{phaseShort[node.phase] ?? node.phase}）
        </p>
      ) : (
        <div className="co-tool-assembly">
          <div>
            <strong>本步装载（{assembly.exposedCount}）</strong>
            <div className="co-chip-list">
              {assembly.exposed.map((item) => (
                <span key={item.name} className="co-badge" data-tone="info">
                  [{channelLabel[item.channel] ?? item.channel}] {item.name}
                </span>
              ))}
            </div>
          </div>
          {assembly.deferred.length > 0 && (
            <div>
              <strong>未挂载获准池（{assembly.deferredCount}）</strong>
              <p className="muted">{assembly.deferred.join(", ")}</p>
            </div>
          )}
          {(assembly.excluded.length > 0 || assembly.permissionFilteredCount > 0) && (
            <div>
              <strong>裁剪与隔离说明</strong>
              <ul>
                {assembly.permissionFilteredCount > 0 && (
                  <li>
                    权限与连接过滤：{assembly.permissionFilteredCount} 项（不展示未授权能力名称）
                  </li>
                )}
                {assembly.excluded.map((item) => (
                  <li key={`${item.name}-${item.reason}`}>
                    <code>{item.name}</code>：{reasonLabel[item.reason] ?? item.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Disclosure>
  );
}
```
