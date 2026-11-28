"""Thin model-routing flow; no Harness, task dispatch, retries or recovery engine."""

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any
from uuid import uuid5

from starlette.concurrency import run_in_threadpool

from compass_agent.context_runtime.run_facts import current_facts, prompt_environment
from compass_agent.llm.policy import LogicalPolicy
from compass_agent.models import RoutingGrant
from compass_agent.models.errors import ModelError
from compass_agent.models.service import ModelInvocationCanceled
from compass_agent.routing.config import POLICY
from compass_agent.routing.dictionary import catalog, features_for
from compass_agent.routing.intent import (
    Delivery,
    DeliveryPrediction,
    choice,
    decision_input,
    flash_input,
    from_flash,
    from_jev,
    tool_requirements,
)
from compass_agent.routing.prompts import route_prompt
from compass_agent.schemas import (
    CallOutcome,
    ChatMessage,
    ClassificationEvent,
    Intent,
    ModelRoute,
    ProviderView,
    RoutedCallResponse,
    RouteSelectedEvent,
    RoutingFeatures,
    RoutingMetrics,
)
from compass_agent.settings import get_settings


@dataclass(frozen=True, slots=True)
class SelectedRoute:
    intent: Intent
    features: RoutingFeatures
    routing: ModelRoute
    provider: ProviderView
    prompt: str
    off_topic: bool
    metrics: RoutingMetrics
    deliverable: str | None = None
    required_tools: tuple[str, ...] = ()


class RoutingService:
    def __init__(self, models, settings=None):
        self.models = models
        self.settings = settings or get_settings()

    async def provider(self, role):
        configured = getattr(self.settings, f"compass_routing_{role}_provider_id")
        return await run_in_threadpool(self.models.repository.routing_provider, role, configured)

    async def _stage(
        self,
        command,
        context,
        role,
        provider_id,
        value,
        stage_deadline,
        kind,
        metrics,
        *,
        emit=None,
        check=None,
        expected_binding_version=None,
        attempt_budget=None,
    ):
        if check:
            check()
        from compass_agent.execution_context import current_execution

        journal = current_execution.get()
        if kind == "classifier" and getattr(journal, "context_binding", None):
            from compass_agent.context_runtime.classifier import call_classifier

            result = await call_classifier(
                journal,
                self,
                command,
                context,
                role,
                provider_id,
                value,
                stage_deadline,
                attempt_budget,
            )
        else:
            result = await self._dispatch(
                command,
                context,
                role,
                provider_id,
                value,
                stage_deadline,
                kind,
                expected_binding_version,
                emit,
                attempt_budget,
            )
        if attempt_budget is not None:
            if result.error_code == "resource_cleanup_failed":
                raise ModelInvocationCanceled(cleanup_confirmed=False)
            if result.error_code == "model_timeout":
                attempt_budget.reason = "execution_timeout"
        if kind == "classifier":
            metrics.classification_calls.append(
                CallOutcome.model_validate(result.model_dump(include=set(CallOutcome.model_fields)))
            )
        if result.error_code == "record_write_failed":
            raise ModelError("record_write_failed", 503)
        return result

    async def _dispatch(
        self,
        command,
        context,
        role,
        provider_id,
        value,
        stage_deadline,
        kind,
        binding,
        emit,
        attempt_budget,
    ):
        physical_id = command.call_id if role == "inference" else uuid5(command.call_id, role)
        grant = (
            RoutingGrant(context.actor_user_id, context.scope, physical_id, provider_id, value.kind)
            if kind == "classifier"
            else None
        )
        self.models.stage = role
        prepared = await self.models.prepare(
            physical_id,
            provider_id,
            value,
            context,
            deadline=stage_deadline,
            grant=grant,
            expected_binding_version=binding,
        )
        return await self.models.run(
            prepared,
            value,
            emit=emit if role == "inference" else None,
            **({"attempt_budget": attempt_budget} if attempt_budget is not None else {}),
            logical_policy=LogicalPolicy(
                max_attempts=2 if role == "inference" and value.kind == "generate" else 1
            ),
        )

    async def select_route(
        self,
        command,
        context,
        *,
        deadline=None,
        emit=None,
        check=None,
        metrics=None,
        attempt_budget=None,
        intent_hint: Intent | None = None,
    ) -> SelectedRoute:
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = deadline or started + POLICY.parent_seconds
        metrics = metrics or RoutingMetrics()
        classification_deadline = min(deadline - 1, started + POLICY.classification_seconds)
        intent = None
        from compass_agent.execution_context import current_execution

        binding = getattr(current_execution.get(), "context_binding", None) or {}
        managed = bool(binding)
        if managed and intent_hint is None:
            paths = ["support/routing/scenes.json"]
            await asyncio.to_thread(current_execution.get().prefetch_prompts, paths)
        deliverable = None
        required_tools = ()
        # M2 uses the Flash stage with a declared token and cost bound.
        routing_mode = (
            "flash_only" if attempt_budget is not None else self.settings.compass_routing_mode
        )
        if intent_hint is not None:
            # Deterministic task routing provides an already validated intent.
            features_for(intent_hint)
            intent = intent_hint
        elif routing_mode == "jev_with_flash":
            metrics.fallback_reason = "jev_unavailable"
            try:
                if check:
                    check()
                jev_id, _ = await self.provider("jev")
                if emit:
                    await emit(ClassificationEvent(call_id=command.call_id, stage="jev"))
                decision = await self._stage(
                    command,
                    context,
                    "intent_jev",
                    jev_id,
                    decision_input(command.input, managed=managed, facts=current_facts()),
                    min(classification_deadline, loop.time() + POLICY.jev_seconds),
                    "classifier",
                    metrics,
                    check=check,
                )
                intent = from_jev(decision, managed=True) if managed else from_jev(decision)
                if managed:
                    deliverable = choice(decision, "deliverable")
                    if deliverable not in Delivery.__args__:
                        intent = None
                    needs = {name: choice(decision, f"tool:{name}") for name in tool_requirements()}
                    if any(value is None for value in needs.values()):
                        intent = None
                    required_tools = tuple(
                        name for name, value in needs.items() if value == "required"
                    )
                if intent is None:
                    metrics.fallback_reason = "jev_unavailable_or_uncertain"
            except ModelError as exc:
                if exc.code not in {
                    "route_binding_missing",
                    "capability_not_supported",
                    "response_too_large",
                    "model_unavailable",
                    "model_budget_unavailable",
                }:
                    raise
        elif routing_mode != "flash_only":
            raise ModelError("route_binding_missing", 503)
        if intent is None:
            if check:
                check()
            flash_id, _ = await self.provider("flash")
            if emit:
                await emit(ClassificationEvent(call_id=command.call_id, stage="flash"))
            prediction = await self._stage(
                command,
                context,
                "intent_flash",
                flash_id,
                flash_input(command.input, managed=managed, facts=current_facts()),
                min(classification_deadline, loop.time() + POLICY.flash_seconds),
                "classifier",
                metrics,
                check=check,
                attempt_budget=attempt_budget,
            )
            if managed and prediction.status == "completed":
                try:
                    parsed = DeliveryPrediction.model_validate_json(prediction.text)
                except ValueError:
                    intent = from_flash(prediction)
                    deliverable = "clarify"
                else:
                    from compass_agent.routing.dictionary import intent_for_scene

                    intent = intent_for_scene(parsed.scene_id)
                    deliverable = parsed.deliverable
                    required_tools = tuple(
                        name for name in tool_requirements() if name in parsed.required_tools
                    )
            else:
                intent = from_flash(prediction)
            metrics.classifier_source = "flash"
        elif intent_hint is None:
            metrics.classifier_source = "jev"
            metrics.fallback_reason = None
        features = features_for(intent)
        compound_search = bool(binding.get("search")) and features.task_type == "复合任务"
        if (
            binding.get("search")
            and features.response_mode == "answer"
            and deliverable == "clarify"
        ):
            # A reference to an earlier meeting or result can be resolved through authorized
            # discovery. Missing information is reported after that read is attempted.
            deliverable = "business_qa"
        elif compound_search and deliverable in {"existing", "clarify", None}:
            deliverable = "business_qa"
        elif (
            managed
            and not compound_search
            and (
                features.response_mode in {"clarify", "handoff", "refuse"}
                or intent.scene_id == "SC-39"
            )
        ):
            deliverable = "existing"
        route_observation = (
            self.models.root_observation.child("compass.route")
            if getattr(self.models, "root_observation", None)
            else None
        )
        with route_observation if route_observation else nullcontext():
            selection = command.selection()
            off_topic = intent.scene_id == "SC-39"
            if check:
                check()
            if off_topic:
                provider_id, _ = await self.provider("flash")
                reason = "off_topic_flash"
            elif selection.mode == "fixed":
                provider_id, reason = selection.provider_id, "user_fixed_main_model"
            else:
                role = "pro" if features.difficulty in {"L2", "L3"} else "flash"
                provider_id, _ = await self.provider(role)
                reason = (
                    "task_route" if intent_hint is not None else "difficulty_" + features.difficulty
                )
            if check:
                check()
            view = await run_in_threadpool(self.models.repository.get, provider_id)
            routing = ModelRoute(
                mode=selection.mode,
                provider_id=provider_id,
                model=view.api_model,
                reason=reason,
                dictionary_version=catalog()[0],
                routing_version=POLICY.version,
            )
            if route_observation:
                route_observation.set(
                    category_id=intent.category_id,
                    scene_id=intent.scene_id,
                    difficulty=features.difficulty,
                    mode=selection.mode,
                    reason=reason,
                    provider_id=str(provider_id),
                    model=view.api_model,
                    dictionary_version=catalog()[0],
                    routing_version=POLICY.version,
                )
        if emit:
            await emit(
                RouteSelectedEvent(
                    call_id=command.call_id,
                    intent=intent,
                    features=features,
                    routing=routing,
                )
            )
        return SelectedRoute(
            intent=intent,
            features=features,
            routing=routing,
            provider=view,
            prompt=route_prompt(
                "clarify" if deliverable == "clarify" else features.response_mode,
                off_topic=off_topic,
            ),
            off_topic=off_topic,
            metrics=metrics,
            deliverable=deliverable,
            required_tools=required_tools,
        )

    def _pin_direct_prompts(self):
        from compass_agent.promptops.catalog import bundled
        from compass_agent.promptops.layout import MEMBERS
        from compass_agent.promptops.reader import current_bodies

        paths = [
            path
            for path in sorted(set(bundled()) | set(MEMBERS))
            if path.startswith(("support/routing/", "shared/fragments/output-"))
        ]
        paths.append("shared/stages/identify.md")
        return current_bodies(self.models.repository.engine, paths)

    async def run(self, command, context, emit=None) -> Any:
        with prompt_environment(context):
            return await self._run_with_prompts(command, context, emit)

    async def _run_with_prompts(self, command, context, emit):
        from compass_agent.prompt_assets import use_prompts

        if self.settings.compass_context_runtime_enabled:
            try:
                prompts = await asyncio.to_thread(self._pin_direct_prompts)
            except ModelError as error:
                return RoutedCallResponse(
                    call_id=command.call_id, status="failed", error_code=error.code
                )
            with use_prompts(prompts):
                return await self._run(command, context, emit)
        return await self._run(command, context, emit)

    async def _run(self, command, context, emit=None) -> Any:
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + POLICY.parent_seconds
        response = RoutedCallResponse(call_id=command.call_id, status="failed")
        metrics = response.metrics
        try:
            async with asyncio.timeout_at(deadline):
                selected = await self.select_route(
                    command,
                    context,
                    deadline=deadline,
                    emit=emit,
                    metrics=metrics,
                )
                response.intent = selected.intent
                response.features = selected.features
                response.routing = selected.routing
                updates: dict = {
                    "messages": [
                        ChatMessage(role="system", content=selected.prompt),
                        *[m for m in command.input.messages if m.role != "system"],
                    ]
                }
                if selected.off_topic:
                    updates.update(
                        thinking=False,
                        reasoning_effort=None,
                        tools=[],
                        output_format="text",
                        output_schema=None,
                        max_tokens=512,
                        continuation_token=None,
                    )
                if facts := current_facts():
                    updates["messages"] = facts.messages(updates["messages"])
                generated = command.input.model_copy(update=updates)
                result = await self._stage(
                    command,
                    context,
                    "inference",
                    selected.provider.id,
                    generated,
                    deadline,
                    "inference",
                    metrics,
                    emit=emit,
                    expected_binding_version=selected.provider.binding_version,
                )
                response.result = result
                response.status = result.status
                response.error_code = result.error_code
        except ModelError as exc:
            if exc.code == "call_already_exists":
                raise
            response.error_code = exc.code
        except TimeoutError:
            response.error_code = "model_timeout"
        finally:
            metrics.duration_ms = max(0, int((loop.time() - started) * 1000))
            usages = [x.usage for x in metrics.classification_calls]
            if response.result:
                usages.append(response.result.usage)
            metrics.usage_complete = bool(usages) and all(
                x.input_tokens is not None and x.output_tokens is not None for x in usages
            )
        return response
