"""Bridge one logical model call to the existing Journal's prepare, dispatch and replay."""

import asyncio
import hashlib

from sqlalchemy.orm import Session

from compass_agent.context_runtime.contracts import ContextRequest
from compass_agent.context_runtime.pipeline import (
    canonical,
    prepare_input,
    require_tool_bindings,
    sources,
)
from compass_agent.context_runtime.semantics import report_variables
from compass_agent.execution_context import execution_path
from compass_agent.llm.contracts import AgentCallResponse
from compass_agent.models.errors import ModelError
from compass_agent.observability.privacy import SKILL_LABELS, tool_label
from compass_agent.schemas import RUN_STREAM_LIMITS, GenerateInput, ModelTokenLimits
from compass_agent.tools.contracts import PhaseCanceled


def model_limits(route, budget):
    if route.limits and (route.limits.max_input_tokens or route.limits.context_window_tokens):
        return route.limits
    if budget:
        for p in budget.profiles:
            if p.provider_id == route.provider_id and p.binding_version == route.binding_version:
                return ModelTokenLimits(
                    max_input_tokens=p.input_tokens, max_output_tokens=p.output_tokens
                )
    from compass_agent.llm.catalog import catalog_token_limits

    available = catalog_token_limits(route.vendor, route.model)
    if available:
        return available.limits
    raise ModelError("model_budget_unavailable", 503)


def observe(root, receipt, *, restored=False):
    if not root:
        return
    with root.child(
        "context.assemble",
        generation=receipt["generation"],
        restore_mode="restored" if restored else "prepared",
        budget_method=receipt["budget"].get("method"),
        run_id=receipt.get("runId"),
        step_id=hashlib.sha256(receipt["stepKey"].encode()).hexdigest()
        if receipt.get("stepKey")
        else None,
        scope_profile=receipt.get("scopeProfile"),
        classifier=receipt.get("classifier"),
        request_digest=receipt.get("requestDigest"),
        metric_count=len(receipt.get("metrics", ())),
        omitted_count=sum(s["decision"] == "omitted" for s in receipt["selections"]),
        task=receipt["task"],
        stage=receipt["phase"],
        scene_id=receipt["scene"],
        policy_version=receipt["policyVersion"],
        tokens_before=receipt["budget"]["before"],
        tokens_after=receipt["budget"]["after"],
        token_limit=receipt["budget"]["limit"],
    ) as span:
        # Langfuse ingests observation attributes. Keep the complete assembly on
        # its own node, including decisions split across the 16-event OTLP batches.
        used = tuple(
            dict.fromkeys(
                (source.get("assetPath", source["path"]), source["version"])
                for file in receipt["files"]
                for source in file.get("sourceMembers", [file])
            )
        )
        span.native("metadata.prompts_used", tuple(f"{path}@v{version}" for path, version in used))
        span.native("metadata.skills_selected", tuple(dict.fromkeys(receipt["skills"])))
        span.native(
            "metadata.tools_selected", tuple(dict.fromkeys(tool_label(t) for t in receipt["tools"]))
        )
        span.native(
            "metadata.capabilities_excluded",
            tuple(
                dict.fromkeys(
                    f"{name if name in SKILL_LABELS else tool_label(name)}: {reason}"
                    for name, reason in receipt["excluded"]
                )
            ),
        )
        events = []

        def add(name, **fields):
            events.append((name, fields))

        for path, version in used:
            add("prompt.used", prompt_path=path, prompt_version=version)
        for skill in receipt["skills"]:
            add("skill.selected", skill=skill)
        for tool in receipt["tools"]:
            add("tool.selected", tool=tool_label(tool))
        for item in receipt["selections"]:
            add(
                "context.material",
                item_position=item["item"],
                selection=item["decision"],
                reason=item["reason"],
            )
        for _, reason in receipt["excluded"]:
            add("capability.excluded", reason=reason)
        for transform in receipt["compactions"]:
            add(
                "context.compacted",
                reason="budget",
                tokens_before=transform["before"],
                tokens_after=transform["after"],
            )

        # The existing observer retains sixteen events per span. Preserve the full
        # decision list on bounded children of this same assembly operation.
        for name, metadata in events[:16]:
            span.event(name, **metadata)
        for offset in range(16, len(events), 16):
            with span.child("context.decisions") as batch:
                for name, metadata in events[offset : offset + 16]:
                    batch.event(name, **metadata)


def observe_failure(root, binding, phase, error):
    if root:
        with root.child("context.assemble", generation=binding["generation"], stage=phase) as span:
            code = getattr(error, "code", "context_limit")
            span.event("context.rejected", reason=code)
            span.complete("failed", code)


async def finish_context_read(read):
    """Drain scoped metadata and source-recording work before reporting a clean cancellation."""
    task = asyncio.create_task(read)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # No provider or business tool has started. Wait for the local transaction/HTTP
        # reader to finish so a timed-out model phase cannot leave a detached operation.
        await asyncio.gather(task, return_exceptions=True)
        raise PhaseCanceled(cleanup_confirmed=True) from None


async def call_model(journal, executor, step, draft, route, trusted, control, budget, emit):
    from compass_agent.context_runtime.source_access import require_run_sources

    path = execution_path.get()
    node_key = f"{path}/model:{step.id}"
    binding = journal.context_binding
    child = "/subagent:" in path
    phase = "analyze" if child else journal.context_phase
    scope = trusted.scope
    ctx = ContextRequest(
        str(journal.run_id),
        node_key,
        str(scope.tenant_id),
        str(scope.workspace_id),
        binding["scopeProfile"],
        route.deliverable,
        route.scene_id,
        phase,
        binding["generation"],
        route.binding_version,
    )

    async def read_inputs():
        # Restoring an immutable frame uses its recorded inputs. Current permissions and
        # source access are still checked by revalidate() before using the saved result.
        if await asyncio.to_thread(journal.has_step, node_key):
            return None
        permitted = await journal.capabilities() if journal.capabilities else {}
        issue = binding.get("registryIssue") or getattr(journal, "registry_issue", None)
        registry_context = {"items": [], "resolution": issue} if issue else None
        if not issue and (
            (binding.get("registry") or binding.get("glossary"))
            and not draft.continuation_token
            and phase != "identify"
        ):
            from compass_agent.routing.dictionary import semantic_context_args
            from compass_agent.semantic.service import RegistryTools

            service = RegistryTools(journal, trusted)
            question = journal.context_questions.get(path, "")
            initial = getattr(journal, "registry_initial_context", None) if not child else None
            try:
                if binding.get("glossary"):
                    registry_context = await service.api("glossary")
                else:
                    registry_context = dict(
                        initial
                        or await service.api(
                            "context", semantic_context_args(question, route.scene_id)
                        )
                    )
            except ModelError as exc:
                return permitted, {
                    "items": [],
                    "resolution": {"status": "unavailable", "code": exc.code},
                }
            if not child:
                journal.registry_initial_context = registry_context
                registry_context["owner_clarifications"] = [
                    {
                        "mention": choice["args"]["mention"],
                        **(
                            {"definition": choice["customDefinition"]}
                            if choice.get("customDefinition")
                            else {"metric_id": choice["choiceId"]}
                        ),
                    }
                    for choice in getattr(journal, "approved_proposals", ())
                    if choice.get("kind") == "semantic_choice"
                    and (choice.get("choiceId") or choice.get("customDefinition"))
                ]

            from compass_agent.semantic.service import session_results

            if not binding.get("search"):
                registry_context["results"] = await asyncio.to_thread(
                    session_results, journal, trusted
                )
            seen = list(registry_context["items"])
            metric = (registry_context.get("resolution") or {}).get("metric")
            if metric and all(item["version_id"] != metric["version_id"] for item in seen):
                seen.append(metric)
            await asyncio.to_thread(service.record_page, seen)
        return permitted, registry_context

    from compass_agent.goal.anchor import anchor

    goal_anchor = (
        None
        if child
        else await finish_context_read(asyncio.to_thread(anchor, journal, trusted, budget))
    )
    if goal_anchor and draft.continuation_token:
        draft = draft.model_copy(
            update={"continuation_token": None, "thinking": False, "reasoning_effort": None}
        )
    inputs = await finish_context_read(read_inputs())
    previous = journal.context_receipts.get(path)
    frame = {}
    fresh = False
    optimizer = getattr(journal, "context_optimizer", None)
    if child or getattr(journal, "answer_turn", {}).get(path, {}).get("purpose") == "verify":
        optimizer = None

    def build(tx):
        if inputs is None:
            raise ModelError("checkpoint_corrupt", 409)
        permitted, registry_context = inputs
        support = (
            ["support/subagent/analysis.md", "shared/fragments/tool-limit.md"]
            if child
            else ["support/routing/business.md"]
        )
        if route.requires_plan and phase == "plan" and not binding.get("search"):
            support.append("shared/fragments/plan-required.md")
        continuation = None
        if draft.continuation_token:
            if not journal.model_secrets:
                raise ModelError("runtime_state_invalid", 409)
            continuation = journal.model_secrets.open(
                draft.continuation_token, "tool_continuation", ttl=900
            )
        budget_extra = len(canonical(continuation)) if continuation else 0
        schema = None
        answer_turn = getattr(journal, "answer_turn", {}).get(path, {})
        if binding.get("answerProtocol") == 2:
            schema = answer_turn.get("schema")
        elif phase == "generate" and not child and binding.get("search"):
            if binding["search"].get("reportFormat") == "comparison_brief":
                from compass_agent.context_runtime.comparison_brief import ComparisonBrief

                schema = ComparisonBrief.model_json_schema()
            else:
                from compass_agent.context_runtime.profiles import search_pack
                from compass_agent.context_runtime.search_verification import search_draft_schema

                progress = getattr(journal, "search_progress", None)
                schema = search_draft_schema(
                    getattr(progress, "previous_draft", None) is not None,
                    pack=search_pack(binding["search"], route.scene_id),
                )
        elif phase == "generate" and not child and route.deliverable != "business_qa":
            from compass_agent.context_runtime.delivery import output_contract

            schema = output_contract(route.deliverable)
        limits = model_limits(route, budget)
        model_draft = draft
        if (
            (binding.get("registry") or binding.get("search"))
            and phase in {"plan", "query", "analyze", "generate"}
            and (route.deliverable != "business_qa" or binding.get("search"))
        ):
            output_limit = limits.max_output_tokens or draft.max_tokens
            profiles = (
                [
                    p.output_tokens
                    for p in budget.profiles
                    if p.provider_id == route.provider_id
                    and p.binding_version == route.binding_version
                ]
                if budget
                else []
            )
            model_draft = draft.model_copy(
                update={"max_tokens": min(8192, output_limit, *profiles)}
            )
        # Root search proposals are published only after verification. Request one
        # JSON response so per-token SSE framing does not consume their wire budget.
        # Recording this choice in the frame also preserves earlier steps on replay.
        if binding.get("answerProtocol") == 2 or (path == "root" and binding.get("search")):
            model_draft = model_draft.model_copy(update={"stream": False})
            # Search opens a fresh phase with verified tool observations. Native
            # reasoning can resume only with the provider's original sealed prefix.
            if not draft.continuation_token and any(m.role == "tool" for m in draft.messages):
                model_draft = model_draft.model_copy(
                    update={"thinking": False, "reasoning_effort": None}
                )
        value = prepare_input(
            tx,
            ctx,
            {**binding, "answerIntermediate": True} if child else binding,
            model_draft,
            limits,
            trusted,
            permitted,
            journal.store.metric_catalog,
            registry_context=registry_context,
            run_facts=getattr(journal, "prompt_facts", None),
            variables={
                **report_variables(
                    journal.context_questions.get(path, ""),
                    journal.prompt_facts.local_date
                    if getattr(journal, "prompt_facts", None)
                    else journal.context_date,
                    binding["scopeProfile"],
                ),
                "tool_limit": str(RUN_STREAM_LIMITS["toolsPerStep"]),
                "max_tasks": "4",
                "max_parallel": "2",
            },
            as_of=journal.context_date,
            continuation_extra=budget_extra,
            previous=previous,
            output_schema=schema,
            question=journal.context_questions.get(path),
            support_paths=support,
            fit_budget=optimizer is None,
            goal_anchor=goal_anchor,
        )
        data = value.request.model_dump(mode="json", exclude={"continuation_token"})
        return {
            "input": {
                "provider": str(route.provider_id),
                "binding": route.binding_version,
                "request": data,
                "continuationDigest": hashlib.sha256(canonical(continuation)).hexdigest()
                if continuation
                else None,
            },
            "continuation": continuation,
            "receipt": value.receipt,
        }

    frame_factory = build
    if optimizer and inputs is not None:

        def compile_frame():
            with Session(journal.store.engine) as tx:
                return build(tx)

        compiled = await finish_context_read(asyncio.to_thread(compile_frame))
        compiled = await optimizer.optimize(
            compiled,
            executor.models,
            trusted,
            control,
            budget,
            route,
            model_limits(route, budget),
            draft,
        )

        def frozen_frame(tx):
            return compiled

        frame_factory = frozen_frame

    def receive(value, existed):
        nonlocal fresh
        fresh = not existed
        frame.update(value)
        request_input = value["input"]
        if (
            value["receipt"]["generation"] != binding["generation"]
            or value["receipt"]["task"] != ctx.task
            or value["receipt"]["phase"] != ctx.phase
            or request_input["provider"] != str(route.provider_id)
            or request_input["binding"] != route.binding_version
        ):
            raise ModelError("workflow_replay_mismatch", 409)
        step.prepared_request = GenerateInput.model_validate(
            journal.restore_continuation(request_input["request"], value["continuation"])
        )
        optimization = value.get("contextOptimization")
        if optimization:
            if hashlib.sha256(canonical(optimization)).hexdigest() != request_input.get(
                "contextOptimizationDigest"
            ):
                raise ModelError("checkpoint_corrupt", 409)
            if optimizer:
                optimizer.restore(optimization, step.prepared_request.messages)

    async def revalidate():
        now = await journal.capabilities() if journal.capabilities else {}
        receipt = frame["receipt"]
        if optimization := frame.get("contextOptimization"):
            from compass_agent.context_runtime.optimization_calls import restore_budget
            from compass_agent.context_runtime.source_access import allows

            await asyncio.to_thread(
                restore_budget,
                journal,
                budget,
                optimization.get("attemptRefs")
                or [
                    *optimization["selection"]["batchAttemptRefs"],
                    *(optimization.get("summary") or {}).get("attempt_refs", []),
                ],
            )

            if not await asyncio.to_thread(
                allows, journal.store, trusted, optimization["sourceDeps"], raise_unavailable=True
            ):
                raise ModelError("context_access_changed", 403)
        if (
            not now.get("analysis")
            or not now.get("model")
            or (
                (
                    receipt["skills"]
                    or any(t.startswith(("mcp_", "workflow_")) for t in receipt["tools"])
                )
                and not now.get("plugins")
            )
        ):
            raise ModelError("forbidden", 403)
        if binding.get("search"):
            from compass_agent.context_runtime.source_access import require_search_sources

            await asyncio.to_thread(require_search_sources, journal, trusted, epoch=journal.epoch)
        else:
            await asyncio.to_thread(
                require_run_sources,
                journal.store,
                journal.run_id,
                trusted,
                epoch=journal.epoch,
            )

        def recheck():
            with Session(journal.store.engine) as tx:
                require_tool_bindings(tx, journal.run_id, trusted, receipt["tools"])
                current = sources(tx, trusted.scope)
                if any(current.get(k) != v for k, v in receipt["sources"].items()):
                    raise ModelError("context_access_changed", 403)
                if (
                    "database_query" in receipt["tools"]
                    and not current
                    and binding.get("answerProtocol") != 2
                ):
                    raise ModelError("context_access_changed", 403)

        await asyncio.to_thread(recheck)
        if receipt.get("metricSources"):
            from compass_agent.context_runtime.source_access import record_source

            for connection, revision in receipt["metricSources"].items():
                await asyncio.to_thread(
                    record_source,
                    journal,
                    {
                        "kind": "metric_dictionary",
                        "tenant": str(trusted.scope.tenant_id),
                        "workspace": str(trusted.scope.workspace_id),
                        "connection": connection,
                        "revision": revision,
                    },
                )

        observe(getattr(executor.models, "root_observation", None), receipt, restored=not fresh)
        if info := receipt.get("contextOptimization"):
            trace = getattr(journal, "execution_trace", None)
            identifier = trace.steps.get(str(step.id)) if trace else None
            if trace and identifier and (info["dropped"] or info["compacted"]):
                parts = [f"历史保留 {info['selected']} 轮，移出 {info['dropped']} 轮"]
                if info["compacted"]:
                    parts.append(f"已生成交接摘要，回填最近 {info['tail']} 轮")
                trace.update(identifier, summary="；".join(parts))

    async def invoke():
        assert step.prepared_request is not None
        return await executor._invoke_model_live(
            step, step.prepared_request, route, trusted, control, budget, emit, publish_text=False
        )

    from compass_agent.harness.context import ContextLimit
    from compass_agent.promptops.errors import PromptError

    try:
        result = await journal.call(
            "model",
            node_key,
            {},
            invoke,
            AgentCallResponse,
            budget=budget,
            frame_factory=frame_factory,
            receive_frame=receive,
            revalidate=revalidate,
            # Review results remain durably checkpointed. Complete the delivery check
            # on this worker so the final reserve does not require a full journal replay.
            yield_boundary=not binding.get("search")
            and getattr(journal, "answer_turn", {}).get(path, {}).get("purpose") != "verify",
        )
    except (ContextLimit, ModelError, PromptError) as error:
        observe_failure(getattr(executor.models, "root_observation", None), binding, phase, error)
        raise
    receipt = frame["receipt"]
    journal.context_receipts[path] = receipt
    return result
