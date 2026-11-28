"""Deterministic, permission-first per-step tool supply."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from compass_agent.schemas import TOOL_CATALOG_LIMIT, ToolDefinition
from compass_agent.tools.contracts import RegisteredTool, json_bytes
from compass_agent.tools.registry import TOOL_DECLARATION_BYTES_LIMIT

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
    required_tools: frozenset[str] = frozenset()


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
    deferred_pool: Mapping[
        str, RegisteredTool
    ]  # Plug-in boundary reserved for Lesson 28 ToolSearch
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
        return "long_task" in ctx.selected_packs or ctx.has_active_goal or ctx.scene_id == "SC-40"
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
        return ctx.scene_id not in L1_LIGHT_SCENES or ctx.requires_plan or ctx.has_active_plan
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
        and not ctx.required_tools
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
        if ctx.caller_role == "subagent" or gov.channel == "always_on":
            always_on_list.append(entry)
        elif _matches_user_selected(entry, ctx):
            if gov.pack == "user_extension":
                user_ext_list.append(entry)
            else:
                user_static_list.append(entry)
        elif (name in ctx.required_tools and gov.pack != "user_extension") or (
            gov.channel == "scene_routed" and _matches_scene_routed(entry, ctx)
        ):
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

    user_ext_sorted = sorted(user_ext_list + auto_workflow_fallback, key=lambda e: e.name)
    for overflow_ext in user_ext_sorted[USER_SELECTED_EXTENSION_LIMIT:]:
        deferred_candidates[overflow_ext.name] = overflow_ext
        excluded.append((overflow_ext.name, "budget"))
    capped_ext = user_ext_sorted[:USER_SELECTED_EXTENSION_LIMIT]

    ordered_plan: list[tuple[RegisteredTool, str]] = [
        *((entry, "always_on") for entry in always_on_list),
        *((entry, "user_selected") for entry in user_static_list),
        *((entry, "user_selected") for entry in capped_ext),
        *((entry, "scene_routed") for entry in scene_routed_list),
    ]

    # Step 4: Enforce <= 32 slots and <= 64 KiB declaration budget
    selected_defs: list[ToolDefinition] = []
    exposed_trace: list[ExposedToolTrace] = []
    seen_names: set[str] = set()
    encoded_bytes = 2
    for entry, channel_source in ordered_plan:
        if entry.name in seen_names:
            continue
        seen_names.add(entry.name)
        assert entry.definition is not None
        candidate_def = entry.definition
        extra_bytes = len(json_bytes(candidate_def.model_dump())) + bool(selected_defs)
        if (
            len(selected_defs) < TOOL_CATALOG_LIMIT
            and encoded_bytes + extra_bytes <= TOOL_DECLARATION_BYTES_LIMIT
        ):
            encoded_bytes += extra_bytes
            selected_defs.append(candidate_def)
            exposed_trace.append(
                ExposedToolTrace(entry.name, channel_source, entry.governance.pack)
            )
        else:
            deferred_candidates[entry.name] = entry
            excluded.append((entry.name, "budget"))

    raw_bytes = encoded_bytes
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
