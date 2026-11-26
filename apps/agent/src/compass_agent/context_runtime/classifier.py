"""Prepare the actual decision or generation request for the durable identify stage."""

import asyncio
from hashlib import sha256
from types import SimpleNamespace

from compass_agent.context_runtime.model_step import model_limits, observe, observe_failure
from compass_agent.context_runtime.pipeline import canonical, exact, input_budget
from compass_agent.context_runtime.source_access import allows, record_sources, require_run_sources
from compass_agent.execution_context import execution_path
from compass_agent.harness.context import ContextLimit
from compass_agent.llm.policy import OutputLimits
from compass_agent.models.errors import ModelError
from compass_agent.prompt_assets import use_prompts
from compass_agent.routing.intent import decision_input, flash_input
from compass_agent.schemas import CallResponse, GenerateInput


async def call_classifier(
    journal, service, command, context, role, provider_id, value, deadline, budget
):
    binding = journal.context_binding
    root = getattr(service.models, "root_observation", None)
    view = await asyncio.to_thread(service.models.repository.get, provider_id)
    route = SimpleNamespace(
        limits=view.generation_profile.effective_limits if view.generation_profile else None,
        vendor=view.vendor,
        model=view.api_model,
        provider_id=view.id,
        binding_version=view.binding_version,
    )
    frame = {}
    fresh = False

    def build(tx):
        generate = value.kind == "generate"
        paths = [
            "shared/fragments/base.md",
            "shared/stages/identify.md",
            "support/routing/deliverable.md",
            "support/routing/classify-flash-managed.md"
            if generate
            else "support/routing/classify-jev.md",
            "support/routing/scenes.json",
        ]
        if generate:
            paths.extend(("shared/fragments/output-json.md", "shared/fragments/output-schema.md"))
        files = exact(tx, binding, paths)
        bodies = {p.path: p.body.rstrip("\n") for p in files}
        limit = OutputLimits().visible_input_bytes
        method = "decision_wire_bytes"
        if generate:
            limits = model_limits(route, budget)
            limit = min(limit, input_budget(limits, value.max_tokens))
            method = "utf8_upper_bound"
        elif budget:
            limits = model_limits(route, budget)
            limit = min(limit, input_budget(limits, 0))
            method = "utf8_upper_bound"
        from dataclasses import asdict
        from time import perf_counter

        from compass_agent.context_ops.compiler import freeze, secure_items
        from compass_agent.context_runtime.pipeline import collect

        started = perf_counter()

        candidates, security_events, blocked = secure_items(
            collect(command.input.messages),
            context,
            mode=journal.store.source_settings.compass_context_security_mode,
        )
        original = [i.message for i in candidates]
        offset = 0
        transforms = []
        before = None
        facts = getattr(journal, "prompt_facts", None)
        with use_prompts(bodies):
            while True:
                material = command.input.model_copy(update={"messages": original[offset:]})
                request = (
                    flash_input(material, managed=True, facts=facts)
                    if generate
                    else decision_input(material, managed=True, facts=facts)
                )
                prefix = bodies["shared/fragments/base.md"] + "\n"
                wire_extra = 0
                if isinstance(request, GenerateInput):
                    from compass_agent.llm.litellm_chat import output_message

                    request.messages[0].content = prefix + (request.messages[0].content or "")
                    wire_extra = len(canonical(output_message(request))) + 8
                else:
                    question = request.questions["scene"]
                    question.instructions = prefix + str(question.instructions)
                data = request.model_dump(mode="json")
                size = len(canonical(data)) + wire_extra + 32
                if before is None:
                    before = size
                if transforms:
                    transforms[-1]["after"] = size
                if size <= limit:
                    break
                following = next(
                    (i for i in range(offset + 1, len(original)) if original[i].role == "user"),
                    None,
                )
                if following is None:
                    raise ContextLimit()
                transforms.append({"group": str(offset), "before": size})
                offset = following

        routing_groups: list = getattr(journal, "context_routing_groups", [])
        frozen = freeze(
            candidates,
            candidates[offset:],
            overhead=max(0, (before or 0) - sum(i.byte_bound for i in candidates)),
            limit=limit,
        )
        receipt = {
            "contextOps": {
                "frozen": frozen,
                "blocked": blocked,
                "security_events": [asdict(e) for e in security_events],
                "duration_ms": round((perf_counter() - started) * 1000),
            },
            "policyDigest": binding["policyDigest"],
            "generation": binding["generation"],
            "phase": "identify",
            "scene": None,
            "task": None,
            "policyVersion": binding["policyVersion"],
            "scopeProfile": binding["scopeProfile"],
            "classifier": role,
            "runId": str(journal.run_id),
            "stepKey": f"{execution_path.get()}/{role}",
            "tools": [],
            "skills": [],
            "excluded": [],
            "metrics": [],
            "files": [f.receipt() for f in files],
            "selections": [
                {
                    "item": str(i),
                    "decision": "omitted" if i < offset else "selected",
                    "reason": "budget"
                    if i < offset
                    else "required"
                    if i == len(original) - 1
                    else "in_budget",
                }
                for i in range(len(original))
            ],
            "compactions": transforms,
            "budget": {
                "before": before,
                "after": size,
                "limit": limit,
                "method": method,
            },
            "requestDigest": sha256(canonical(data)).hexdigest(),
            "sourceDeps": [
                ref
                for i, group in enumerate(routing_groups)
                if 2 * i + 1 >= offset
                for ref in group.source_deps
            ],
        }
        return {
            "input": {
                "provider": str(provider_id),
                "binding": view.binding_version,
                "request": data,
            },
            "receipt": receipt,
        }

    def receive(saved, existed):
        nonlocal fresh
        fresh = not existed
        frame.update(saved)
        if (
            frame["receipt"]["generation"] != binding["generation"]
            or frame["receipt"]["classifier"] != role
            or frame["input"]["provider"] != str(provider_id)
            or frame["input"]["binding"] != view.binding_version
        ):
            raise ModelError("model_binding_changed", 409)

    async def revalidate():
        grants = await journal.capabilities() if journal.capabilities else {}
        if not grants.get("analysis") or not grants.get("model"):
            raise ModelError("forbidden", 403)
        await asyncio.to_thread(
            require_run_sources,
            journal.store,
            journal.run_id,
            context,
            epoch=journal.epoch,
        )
        sources = frame["receipt"].get("sourceDeps", ())
        if not await asyncio.to_thread(
            allows, journal.store, context, sources, epoch=journal.epoch, raise_unavailable=True
        ):
            raise ModelError("context_access_changed", 403)
        if sources:
            await asyncio.to_thread(record_sources, journal, sources)
        observe(root, frame["receipt"], restored=not fresh)

    async def dispatch():
        prepared = type(value).model_validate(frame["input"]["request"])
        from compass_agent.context_ops.recorder import capture

        with capture(journal, context, frame["receipt"]):
            return await service._dispatch(
                command,
                context,
                role,
                provider_id,
                prepared,
                deadline,
                "classifier",
                view.binding_version,
                None,
                budget,
            )

    try:
        return await journal.call(
            "classifier",
            f"{execution_path.get()}/{role}",
            {},
            dispatch,
            CallResponse,
            frame_factory=build,
            receive_frame=receive,
            revalidate=revalidate,
            yield_boundary=False,
        )
    except (ContextLimit, ModelError) as error:
        observe_failure(root, binding, "identify", error)
        raise
