"""Compile one bound, authorized logical model request and a private decision receipt."""

import hashlib
import json
from dataclasses import asdict
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import select

from compass_agent.context_runtime.contracts import (
    AccessRef,
    Compaction,
    ContextItem,
    ContextRequest,
    PreparedContext,
    Selection,
    SourceRef,
)
from compass_agent.context_runtime.profiles import (
    POLICY_DIGEST,
    POLICY_VERSION,
    resolve_profile,
    scope_profile,
)
from compass_agent.data_connections.entities import DataConnection
from compass_agent.harness.context import ContextLimit
from compass_agent.models.errors import ModelError
from compass_agent.schemas import RUN_CONTEXT_MESSAGES, ChatMessage

if TYPE_CHECKING:
    from compass_agent.schemas import ModelTokenLimits


# The file provider's selection rules and tool gates are preserved when registry is disabled.
FILE_POLICY_DIGEST = "afc5fd0aeb8ad8f2217e759ec216091243ace17d08c0506fef942095469b6699"


def compatible_policy(version, digest, *, registry=False):
    from compass_agent.promptops.layout import LEGACY_POLICY_DIGESTS

    return version == POLICY_VERSION and (
        digest == POLICY_DIGEST
        or digest in LEGACY_POLICY_DIGESTS
        or (not registry and digest == FILE_POLICY_DIGEST)
    )


def pin(tx, context, store):
    from compass_agent.promptops.reader import PromptReader

    generation = PromptReader.pin_current(tx)
    info = PromptReader.describe(tx, generation)
    from compass_agent.semantic.client import enabled

    if not compatible_policy(
        info.policy_version,
        info.policy_digest,
        registry=enabled(store.source_settings, context.scope.workspace_id)
        if hasattr(store, "source_settings")
        else False,
    ):
        raise ModelError("context_policy_unavailable", 503)
    return {
        "schemaVersion": 1,
        "generation": generation,
        "policyVersion": POLICY_VERSION,
        "policyDigest": info.policy_digest,
        "manifestDigest": info.manifest_digest,
        "dictionaryDigest": store.metric_catalog.active,
        "scopeProfile": scope_profile(context.scope, store.context_scopes),
        "scopeConfigDigest": hashlib.sha256(
            canonical(
                sorted(
                    (str(r.tenant_id), str(r.workspace_id or ""), r.profile)
                    for r in store.context_scopes
                )
            )
        ).hexdigest(),
    }


def exact(tx, binding, paths):
    from compass_agent.promptops.reader import PromptReader

    if not compatible_policy(
        binding["policyVersion"], binding["policyDigest"], registry=bool(binding.get("registry"))
    ):
        raise ModelError("context_policy_unavailable", 503)
    info = PromptReader.describe(tx, binding["generation"])
    if (info.policy_version, info.policy_digest) != (
        binding["policyVersion"],
        binding["policyDigest"],
    ) or (binding.get("manifestDigest") and info.manifest_digest != binding["manifestDigest"]):
        raise ModelError("context_policy_unavailable", 503)
    return PromptReader.load_exact(tx, binding["generation"], tuple(dict.fromkeys(paths)))


def sources(tx, scope):
    return {
        str(key): revision
        for key, revision in tx.execute(
            select(DataConnection.id, DataConnection.revision).where(
                DataConnection.tenant_id == scope.tenant_id,
                DataConnection.workspace_id == scope.workspace_id,
            )
        )
    }


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def input_budget(limits: "ModelTokenLimits", output: int):
    candidates = [
        limit
        for limit in (
            limits.max_input_tokens,
            limits.context_window_tokens - output if limits.context_window_tokens else None,
        )
        if limit is not None
    ]
    if not candidates:
        raise ModelError("model_budget_unavailable", 503)
    return min(candidates) - 32


def collect(messages, required_positions=(), metric_access=(), organization_position=None):
    """Keep complete conversational turns and assistant/tool result batches together."""
    items = []
    group = 0
    last_user = max((i for i, m in enumerate(messages) if m.role == "user"), default=0)
    for i, m in enumerate(messages):
        if m.role == "user":
            group += 1
        required = i >= last_user or m.role == "system" or i in required_positions
        kind = (
            "instruction"
            if m.role == "system"
            else "tool"
            if m.role == "tool" or m.tool_calls
            else "conversation"
        )
        trust = "managed" if m.role == "system" else "observation" if kind == "tool" else "actor"
        if i == organization_position:
            required, kind, trust = True, "organization", "observation"
        elif i in required_positions:
            kind, trust = "metric", "dictionary"
        payload = canonical(m.model_dump(mode="json", exclude_none=True))
        items.append(
            ContextItem(
                str(i),
                kind,
                m,
                SourceRef(kind, hashlib.sha256(payload).hexdigest(), "1"),
                str(group),
                required,
                access=metric_access if kind == "metric" else (),
                trust=trust,
                byte_bound=len(payload) + 8,
            )
        )
    return items


FRAME_BYTE_LIMIT = 192 * 1024


def fit(
    value,
    limit,
    *,
    continuation_extra=0,
    required_positions=(),
    metric_access=(),
    organization_position=None,
):
    items = collect(value.messages, required_positions, metric_access, organization_position)
    selected = list(items)
    decisions = []

    def frame():
        result = value.model_copy(update={"messages": [i.message for i in selected]})
        # UTF-8 byte bound includes tool arguments, schemas and per-message framing.
        payload = result.model_dump(mode="json", exclude_none=True, exclude={"continuation_token"})
        size = len(canonical(payload)) + len(result.messages) * 8 + continuation_extra
        return result, size

    result, size = frame()
    before = size
    for group in dict.fromkeys(i.group for i in selected if not i.required):
        if size <= limit and len(selected) <= RUN_CONTEXT_MESSAGES:
            break
        if value.continuation_token:
            break
        group_items = [i for i in selected if i.group == group]
        if any(i.required for i in group_items):
            continue
        old = size
        selected = [i for i in selected if i.group != group]
        result, size = frame()
        decisions.append(Compaction(group, old, size))
    if size > limit or len(selected) > RUN_CONTEXT_MESSAGES:
        raise ContextLimit()
    kept = {i.item_id for i in selected}
    selections = tuple(
        Selection(
            i.item_id,
            "selected" if i.item_id in kept else "omitted",
            "required" if i.required else "in_budget" if i.item_id in kept else "budget",
            kind=i.kind,
            trust=i.trust,
            source=i.source,
            group=i.group,
            role=i.message.role,
            byte_bound=i.byte_bound,
            access=i.access,
        )
        for i in items
    )
    return result, before, size, selections, tuple(decisions)


def prepare_input(
    tx,
    ctx: ContextRequest,
    binding,
    draft,
    limits,
    trusted,
    grants,
    catalog,
    *,
    variables,
    as_of: date,
    tools=None,
    continuation_extra=0,
    previous=None,
    output_schema=None,
    support_paths=(),
    question=None,
    registry_context=None,
    run_facts=None,
    fit_budget=True,
    goal_anchor=None,
):
    from compass_agent.promptops.template import render

    native_continuation_extra = continuation_extra
    current = sources(tx, trusted.scope)
    search = binding.get("search")
    if search:
        current = {
            k: v
            for k, v in current.items()
            if k == search.get("connectionId") and v == search.get("connectionRevision")
        }
    available = tuple(t.function.name for t in draft.tools)
    if not current and binding.get("answerProtocol") != 2:
        available = tuple(t for t in available if t != "database_query")
    allow = grants.get("analysis") is True and grants.get("model") is True
    if not allow:
        raise ModelError("forbidden", 403)
    profile = resolve_profile(
        ctx.scope_profile,
        ctx.task,
        ctx.scene,
        ctx.phase,
        available=available,
        plugins=grants.get("plugins") is True,
        enabled=allow,
        registry=bool(binding.get("registry")),
        search=search,
        answer_protocol=binding.get("answerProtocol", 1),
        intermediate=binding.get("answerIntermediate", False),
    )
    declared = [t for t in draft.tools if t.function.name in profile.tools]
    from compass_agent.tools.catalog import TOOLS

    names = {t.function.name for t in declared}
    usage = []
    if "database_query" in names:
        usage.append("shared/tools/database-query.md")
    if "schedule_read" in names:
        usage.append("shared/tools/schedule-guidance.md")
    if "create_workflow" in names:
        usage.append("shared/fragments/workflow.md")
    if "goal" in names:
        usage.append("shared/fragments/long-task.md")
    if "sub_agent" in names:
        usage.append("shared/fragments/delegation.md")
    paths = list(dict.fromkeys([*profile.paths, *support_paths, *usage]))
    if binding.get("optimizationVersion") == 1:
        paths.append("shared/fragments/context-results.md")
    files = exact(tx, binding, paths)
    receipts = [f.receipt() for f in files]
    definitions = [f"tools/{tool.name}.json" for tool in TOOLS if tool.name in names]
    receipts.extend(f.receipt() for f in exact(tx, binding, definitions))
    selected_metrics = []
    semantic = {"status": "unconfigured", "candidates": [], "excluded": []}
    required_positions = ()
    organization_position = None
    if draft.continuation_token:
        if not previous or not set(previous.get("tools", ())).issubset(available):
            raise ModelError("context_access_changed", 403)
        if any(current.get(k) != rev for k, rev in previous.get("sources", {}).items()):
            raise ModelError("context_access_changed", 403)
        # The provider validates its entire visible prefix, including the previous system message.
        receipts = previous["files"]
        declared = [t for t in draft.tools if t.function.name in previous["tools"]]
        if not grants.get("plugins") and (
            previous["skills"]
            or any(t.startswith(("mcp_", "workflow_")) for t in previous["tools"])
        ):
            raise ModelError("context_access_changed", 403)
        semantic = previous.get("semantic", semantic)
        metric_ids = previous["metrics"]
        metric_sources = previous.get("metricSources", {})
        profile_skills = previous["skills"]
        messages = list(draft.messages)
        used_sources = previous.get("sources", {})
        if "database_query" in [t.function.name for t in declared]:
            used_sources = {**used_sources, **current}
    else:
        sections = [render(f.path, f.body, variables) for f in files]
        if ctx.phase != "identify":
            (scene_catalog,) = exact(tx, binding, ["support/routing/scenes.json"])
            scene = json.loads(scene_catalog.body)["scenes"].get(ctx.scene, {})
            if sop := scene.get("sop"):
                sections.append(sop)
                receipts.append(scene_catalog.receipt())
        prompt = "\n\n".join(sections)
        messages = [
            ChatMessage(role="system", content=prompt),
            *(m for m in draft.messages if m.role != "system"),
        ]
        dictionary = (
            None
            if search or binding.get("registry") or binding.get("registryIssue")
            else catalog.resolve(binding.get("dictionaryDigest"))
        )
        if registry_context and ctx.phase != "identify":
            semantic = {
                "status": (registry_context.get("resolution") or {}).get("status", "no_match"),
                "candidates": [],
                "excluded": [],
            }
            content = {
                "source": "publishedSemanticRegistry",
                "items": registry_context["items"],
                "previous_results": registry_context.get("results", []),
                "authorized_resolution": registry_context.get("resolution"),
                "requested_metrics": registry_context.get("requested_metrics", []),
            }
            if search:
                content.pop("previous_results")
            messages.insert(1, ChatMessage(role="user", content=canonical(content).decode()))
            required_positions = (1,)
            if decisions := registry_context.get("owner_clarifications"):
                owner_turn = max(i for i, message in enumerate(messages) if message.role == "user")
                names = {
                    item.get("asset_id"): item.get("name") for item in registry_context["items"]
                }
                confirmations = []
                for choice in decisions:
                    selected = (
                        choice.get("definition")
                        or names.get(choice.get("metric_id"))
                        or choice["metric_id"]
                    )
                    confirmations.append(f"「{choice['mention']}」使用：{selected}")
                latest = messages[owner_turn]
                messages[owner_turn] = latest.model_copy(
                    update={
                        "content": (latest.content or "")
                        + "\n\n我刚在本轮选择卡确认了以下口径，请按此完成原问题：\n"
                        + "\n".join(confirmations)
                    }
                )
        if dictionary and messages:
            question = question or next(
                (m.content or "" for m in reversed(messages) if m.role == "user"), ""
            )
            prior = [m.content or "" for m in draft.messages if m.role == "user"]
            if len(prior) > 1 and any(
                term in question for term in ("该指标", "这个指标", "同一口径", "上述指标")
            ):
                question = prior[-2][-2048:] + "\n" + question
            match = dictionary.match(question, trusted.scope, as_of, allowed_sources=set(current))
            selected_metrics = match.records
            semantic = {
                "status": match.status,
                "candidates": list(match.candidates),
                "excluded": list(match.excluded),
            }
            metric_data = {
                "source": "approvedMetricDictionary",
                "status": match.status,
                "candidates": list(match.candidates),
                "definitions": [
                    m.model_dump(mode="json", exclude={"tenant_id", "workspace_id"})
                    for m in selected_metrics
                ],
            }
            messages.insert(1, ChatMessage(role="user", content=canonical(metric_data).decode()))
            required_positions = (1,)
        if run_facts:
            messages = run_facts.messages(messages)
            if run_facts.organization:
                organization_position = 1
                required_positions = tuple(i + 1 for i in required_positions)
        used_sources = {m.source: current[m.source] for m in selected_metrics}
        metric_ids = [m.metric_id for m in selected_metrics]
        metric_sources = used_sources
        profile_skills = list(profile.skills)
    if goal_anchor:
        messages[0] = messages[0].model_copy(
            update={"content": (messages[0].content or "") + "\n\n" + goal_anchor}
        )
    update: dict = {"messages": messages, "tools": declared}
    if output_schema:
        # The root delivery step formats accepted observations. Reserve its output
        # budget for the structured answer and artifact.
        update.update(
            output_format="schema",
            output_schema=output_schema,
            thinking=False,
            reasoning_effort=None,
        )
    if limits.max_output_tokens:
        update["max_tokens"] = min(draft.max_tokens, limits.max_output_tokens)
    value = draft.model_copy(update=update)
    if value.output_format != "text":
        from compass_agent.llm.litellm_chat import output_message

        protocol = ["shared/fragments/output-json.md"]
        if value.output_schema:
            protocol.append("shared/fragments/output-schema.md")
        protocol_files = exact(tx, binding, protocol)
        by_path = {f.path: f.body.rstrip("\n") for f in protocol_files}
        receipts.extend(f.receipt() for f in protocol_files)
        continuation_extra += len(canonical(output_message(value, by_path.__getitem__))) + 8
    limit = min(FRAME_BYTE_LIMIT, input_budget(limits, value.max_tokens))
    value, before, after, selections, transforms = (
        fit(
            value,
            limit,
            continuation_extra=continuation_extra,
            required_positions=required_positions,
            organization_position=organization_position,
            metric_access=tuple(
                AccessRef(ctx.tenant_id, ctx.workspace_id, key, str(rev))
                for key, rev in used_sources.items()
            ),
        )
        if fit_budget
        else (
            value,
            len(canonical(value.model_dump(mode="json", exclude_none=True)))
            + len(value.messages) * 8
            + continuation_extra,
            len(canonical(value.model_dump(mode="json", exclude_none=True)))
            + len(value.messages) * 8
            + continuation_extra,
            (),
            (),
        )
    )
    receipt = {
        "schemaVersion": 1,
        "generation": ctx.generation,
        "stepKey": ctx.step_key,
        "runId": ctx.run_id,
        "bindingVersion": ctx.binding_version,
        "dictionaryDigest": binding.get("dictionaryDigest"),
        "policyDigest": binding["policyDigest"],
        "principal": str(trusted.actor_user_id),
        "tenant": ctx.tenant_id,
        "workspace": ctx.workspace_id,
        "dataAsOf": as_of.isoformat(),
        "policyVersion": binding["policyVersion"],
        "task": ctx.task,
        "phase": ctx.phase,
        "scene": ctx.scene,
        "files": receipts,
        "skills": profile_skills,
        "tools": [t.function.name for t in declared],
        "sources": used_sources,
        "scopeProfile": ctx.scope_profile,
        "excluded": [list(p) for p in profile.excluded],
        "metrics": metric_ids,
        "semantic": semantic,
        "metricSources": metric_sources,
        "budget": {
            "method": "utf8_upper_bound",
            "before": before,
            "after": after,
            "limit": limit,
            "continuationExtra": native_continuation_extra,
            "protocolExtra": continuation_extra - native_continuation_extra,
        },
        "selections": [asdict(s) for s in selections],
        "compactions": [asdict(c) for c in transforms],
        "requestDigest": hashlib.sha256(
            canonical(value.model_dump(mode="json", exclude={"continuation_token"}))
        ).hexdigest(),
    }
    if search:
        receipt["searchSnapshot"] = {
            k: search.get(k)
            for k in (
                "collectionDigest",
                "dataRelease",
                "knownAt",
                "timezone",
                "skillEnabled",
                "kinds",
            )
        }
        receipt["scenePack"] = {
            "digest": hashlib.sha256(canonical([f.receipt() for f in files])).hexdigest(),
            "bytes": sum(len(f.body.encode()) for f in files),
            "paths": [f.path for f in files],
        }
        for selection in receipt["selections"]:
            item = selection["item"]
            selection["layer"] = (
                "L0-L2"
                if selection["role"] == "system"
                else (
                    "L4"
                    if selection["role"] == "tool"
                    else "L1"
                    if str(item) in {str(p) for p in required_positions}
                    else "L3-L5"
                )
            )
    return PreparedContext(value, receipt, selections, transforms)


def require_tool_bindings(tx, run_id, trusted, tool_names):
    """Recheck frozen user asset identities before exposing or replaying their declarations."""
    from uuid import UUID

    from compass_agent.runtime.entities import AgentToolBinding
    from compass_agent.tools.mcp.entities import McpConnection
    from compass_agent.tools.mcp.service import tool_name
    from compass_agent.workflows.assets import WorkflowAsset
    from compass_agent.workflows.catalog import PREFIX

    for name in tool_names:
        if name.startswith("mcp_"):
            frozen = tx.scalar(
                select(AgentToolBinding).where(
                    AgentToolBinding.run_id == run_id, AgentToolBinding.alias == name
                )
            )
            conn = (
                tx.get(McpConnection, UUID(frozen.config_ref))
                if frozen and frozen.config_ref
                else None
            )
            if (
                not frozen
                or not conn
                or not conn.enabled
                or conn.revision != frozen.config_revision
                or (conn.tenant_id, conn.workspace_id, conn.actor_user_id)
                != (trusted.scope.tenant_id, trusted.scope.workspace_id, trusted.actor_user_id)
                or name != tool_name(conn.id, frozen.original_name)
                or conn.tools["selected_tools"] != "all"
                and frozen.original_name not in conn.tools["selected_tools"]
            ):
                raise ModelError("context_access_changed", 403)
        elif name.startswith(PREFIX):
            try:
                identifier = UUID(hex=name.removeprefix(PREFIX))
            except ValueError:
                raise ModelError("context_access_changed", 403) from None
            asset = tx.get(WorkflowAsset, identifier)
            if (
                not asset
                or asset.status != "active"
                or (asset.tenant_id, asset.workspace_id, asset.owner_user_id)
                != (trusted.scope.tenant_id, trusted.scope.workspace_id, trusted.actor_user_id)
            ):
                raise ModelError("context_access_changed", 403)
