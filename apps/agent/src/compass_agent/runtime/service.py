"""Compose generic model routing, authorized tools and durable Run execution."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from traceback import extract_tb
from uuid import uuid5

from sqlalchemy.exc import DBAPIError

from compass_agent.harness.autonomy import (
    AutonomyBudget,
    AutonomyLimits,
    AutonomyStopped,
)
from compass_agent.harness.context import ContextLimit
from compass_agent.harness.contracts import RunFailure
from compass_agent.harness.verification import (
    RunVerifier,
    VerificationContext,
    VerificationRejected,
    verify_result,
)
from compass_agent.models.errors import ModelError
from compass_agent.models.service import ModelInvocationCanceled, ModelService
from compass_agent.prompt_assets import prompt_text
from compass_agent.routing.service import RoutingService, SelectedRoute
from compass_agent.runtime.context import AgentDefinition, RunRoute, initial_messages
from compass_agent.runtime.control import RunControl
from compass_agent.runtime.execution_trace import ExecutionTrace
from compass_agent.runtime.schedule_effect import execute_effect
from compass_agent.runtime.session import RuntimeSession
from compass_agent.runtime.task_state import TaskState
from compass_agent.runtime.tool_bindings import RunToolBindings
from compass_agent.runtime.turn import RuntimeTurn
from compass_agent.scheduler.service import ScheduleService
from compass_agent.schemas import (
    AnalysisRunInput,
    ChatCallInput,
    ChatMessage,
    GenerateInput,
    Intent,
    RunCompleted,
    RunError,
    RunExtensionPayload,
    RunFailed,
    RunProgressEvent,
    RunResult,
    RunRouted,
    RunRoutedPayload,
    RunWaiting,
    StreamExtension,
    TrustedContext,
)
from compass_agent.settings import AutonomyModelProfile
from compass_agent.tools.builtin.create_workflow import NAME as CREATE_WORKFLOW
from compass_agent.tools.catalog import definitions as catalog_definitions
from compass_agent.tools.catalog_contracts import CatalogContext
from compass_agent.tools.contracts import (
    PhaseCanceled,
    RegisteredTool,
    ResourceUnconfirmed,
    StopRequested,
    ToolControl,
    ToolRejected,
)
from compass_agent.tools.loader import ToolLoader
from compass_agent.tools.registry import has_catalog_room, validate_catalog

logger = logging.getLogger(__name__)


def _failure_diagnostic(emit, control, stage: str, error: Exception, reason: str) -> None:
    cause = error.orig if isinstance(error, DBAPIError) else error
    logger.error(
        "analysis.run.exception run_id=%s stage=%s exception=%s reason=%s frames=%s",
        control.id,
        stage,
        type(cause).__name__,
        reason,
        [(frame.filename, frame.lineno, frame.name) for frame in extract_tb(error.__traceback__)],
    )
    emit(
        StreamExtension(
            type="stream.extension",
            payload=RunExtensionPayload(
                name="compass.runtime.failure",
                version=1,
                required=False,
                data={"stage": stage, "exceptionType": type(cause).__name__, "reasonCode": reason},
            ),
        )
    )


EmitProgress = Callable[[RunProgressEvent], None]
ToolsProvider = Callable[
    [TrustedContext, ToolControl, EmitProgress], Awaitable[tuple[RegisteredTool, ...]]
]


def route_supports_tools(selected, journal):
    if (getattr(journal, "context_binding", None) or {}).get("answerProtocol") == 2:
        return selected.provider.capabilities.tools.state == "supported"
    if getattr(journal, "context_binding", None) and selected.deliverable in {
        "existing",
        "clarify",
    }:
        if selected.features.response_mode == "clarify":
            return False
    return selected.provider.capabilities.tools.state == "supported"


class RunService(RuntimeTurn):
    def __init__(
        self,
        models: ModelService,
        routing: RoutingService,
        *,
        definition: AgentDefinition | None = None,
        tool_loader: ToolLoader | None = None,
        tools_provider: ToolsProvider | None = None,
        autonomy_profiles: tuple[AutonomyModelProfile, ...] | None = None,
        autonomy_limits: AutonomyLimits | None = None,
        verifiers: tuple[RunVerifier, ...] = (),
        task_verifiers: Mapping[str, tuple[RunVerifier, ...]] | None = None,
        workflow_assets=None,
        workflow_drafts=None,
    ):
        super().__init__(models)
        self.tool_loader = tool_loader or ToolLoader()
        self.schedules: ScheduleService | None = None
        self.routing, self.definition = routing, definition or AgentDefinition()
        self.tools_provider = tools_provider
        self.workflow_assets, self.workflow_drafts = workflow_assets, workflow_drafts
        self.autonomy_profiles = autonomy_profiles
        self.autonomy_limits = autonomy_limits or AutonomyLimits()
        self.verifiers, self.task_verifiers = verifiers, dict(task_verifiers or {})

    async def run(
        self,
        command: AnalysisRunInput,
        context: TrustedContext,
        control: RunControl,
        emit: EmitProgress,
        *,
        verifiers: tuple[RunVerifier, ...] = (),
    ) -> RunResult:
        from compass_agent.context_runtime.run_facts import prompt_environment

        with prompt_environment(context):
            return await self._run(command, context, control, emit, verifiers=verifiers)

    async def _run(self, command, context, control, emit, *, verifiers):
        trace = ExecutionTrace(emit)
        emit = trace.emit
        state: TaskState | None = None
        discovered = None
        limits = self.autonomy_limits
        if command.constraints and command.constraints.max_steps is not None:
            limits = replace(limits, steps=min(limits.steps, command.constraints.max_steps))
        from compass_agent.execution_context import current_execution

        journal = current_execution.get()
        if journal:
            journal.execution_trace = trace
        search = (getattr(journal, "context_binding", None) or {}).get("search")
        if search and search.get("maxToolCalls") is not None:
            limits = replace(limits, tools=min(limits.tools, search["maxToolCalls"]))
        controlled = (
            RuntimeSession(self.autonomy_profiles, limits)
            if self.autonomy_profiles is not None
            else None
        )
        stop_reason: str | None = None
        candidate: RunResult = RunFailed(status="failed", errorCode="internal_error")
        stage = "tool_setup"
        try:
            if search and not self.autonomy_profiles:
                raise AutonomyStopped("model_budget_unavailable")
            # Retained user assets are readable, but retired execution contracts are unavailable.
            if command.workflow_asset_ref is not None:
                raise RunFailure("workflow_unavailable")
            discovered = await self._discover_tools(context, control, journal)
            provider = self.tools_provider
            provided = (
                await control.perform(lambda: provider(context, control, emit)) if provider else ()
            )
            entries = (*self.definition.tools, *provided)
            snapshot = (
                await control.perform(lambda: asyncio.to_thread(journal.catalog_declarations))
                if journal and self.workflow_assets is not None
                else None
            )
            catalog, declarations, workflow = await self.tool_catalog(
                context, journal, entries, discovered, snapshot
            )
            budget = controlled.budget if controlled else None
            stage = "routing"
            selected = await control.perform(
                lambda: self._select_route(
                    command,
                    context,
                    control,
                    budget,
                    command.question,
                    intent_hint=None,
                )
            )
            stage = "tool_setup"
            capabilities = selected.provider.capabilities
            supported = route_supports_tools(selected, journal)
            route = RunRoute(
                provider_id=selected.provider.id,
                binding_version=selected.provider.binding_version,
                model_label=selected.provider.alias,
                scene_id=selected.intent.scene_id,
                task_type=selected.features.task_type,
                schema_output=capabilities.schema_output.state == "supported",
                route_prompt=selected.prompt,
                stream=capabilities.stream.state == "supported",
                thinking=capabilities.thinking.state == "supported",
                deepseek=selected.provider.vendor == "deepseek"
                and (journal is None or getattr(journal, "deepseek_thinking", False)),
                tools_enabled=supported and bool(declarations),
                deliverable=getattr(selected, "deliverable", None),
                limits=selected.provider.generation_profile.effective_limits
                if getattr(selected.provider, "generation_profile", None)
                else None,
                requires_plan=bool(command.constraints and command.constraints.plan == "required")
                or (bool(search) and selected.features.task_type == "复合任务"),
                vendor=selected.provider.vendor,
                model=selected.provider.api_model,
            )
            state = TaskState(
                initial_messages(self.definition, command, route, workflow=workflow and supported)
            )
            if "schedule_read" in catalog.enabled:
                guidance = prompt_text(
                    "shared/tools/schedule-guidance.md"
                    if supported
                    else "shared/tools/schedule-unavailable.md"
                )
                first = state.messages[0]
                state.messages[0] = first.model_copy(
                    update={"content": (first.content or "") + "\n" + guidance}
                )
            state.trace = trace
            bindings = RunToolBindings(
                self,
                state,
                route,
                context,
                control,
                controlled,
                emit,
                self.tool_loader,
                discovered,
                supported,
                entries=entries,
                workflow=workflow,
                catalog=catalog,
            )
            state.tools = bindings
            if workflow and supported:
                await control.perform(lambda: bindings.with_workflows(declarations, snapshot))
            emit(
                RunRouted(
                    type="run.routed",
                    payload=RunRoutedPayload(
                        providerId=str(route.provider_id),
                        modelLabel=route.model_label,
                        sceneId=route.scene_id,
                        toolsEnabled=route.tools_enabled,
                    ),
                )
            )
            hooks = self.verifiers + self.task_verifiers.get(route.scene_id, ()) + verifiers
            stage = "execution"
            candidate = await self.execute_turn(
                state,
                route,
                context,
                control,
                controlled,
                emit,
                publish_text=not hooks,
                max_steps=command.constraints.max_steps if command.constraints else None,
            )
            if isinstance(candidate, RunCompleted):
                candidate = candidate.model_copy(update={"artifacts": list(state.tools.artifacts)})
            if hooks and isinstance(candidate, RunCompleted | RunWaiting):
                stage = "verification"
                proposed = candidate
                try:
                    await control.perform(
                        lambda: verify_result(
                            proposed,
                            VerificationContext(control.id, context, route.scene_id),
                            hooks,
                        )
                    )
                except VerificationRejected:
                    candidate = RunFailed(status="failed", errorCode="internal_error")
                    stop_reason = "verification_rejected"
        except StopRequested:
            self._close_open_step(state, "canceled", emit)
        except (AutonomyStopped, TimeoutError) as error:
            stop_reason = (
                error.reason if isinstance(error, AutonomyStopped) else "execution_timeout"
            )
            if stop_reason == "goal_paused":
                control.request_stop("user")
            self._close_open_step(
                state, "canceled" if stop_reason == "goal_paused" else "failed", emit
            )
            code: RunError = "internal_error"
            if stop_reason == "execution_timeout":
                code = "run_timeout"
            elif stop_reason == "max_steps":
                code = "step_limit"
            elif stop_reason in {"token_budget", "cost_budget", "max_tool_calls"}:
                code = "run_budget_exhausted"
            elif stop_reason == "model_budget_unavailable":
                code = "model_unavailable"
            candidate = RunFailed(status="failed", errorCode=code)
        except ToolRejected as error:
            self._close_open_step(state, "failed", emit)
            candidate = RunFailed(
                status="failed",
                errorCode=(
                    "tool_catalog_limit"
                    if error.code == "tool_catalog_limit"
                    else "tool_setup_failed"
                ),
            )
        except ContextLimit:
            self._close_open_step(state, "failed", emit)
            candidate = RunFailed(status="failed", errorCode="context_limit")
        except ResourceUnconfirmed:
            control.mark_cleanup_unconfirmed()
            self._close_open_step(state, "canceled", emit)
        except RunFailure as error:
            self._close_open_step(state, "failed", emit)
            _failure_diagnostic(emit, control, stage, error, error.reason)
            candidate = RunFailed(status="failed", errorCode=error.code)
        except ModelError as error:
            self._close_open_step(state, "failed", emit)
            if error.code in {"intent_invalid", "intent_unavailable"}:
                stop_reason = error.code
            _failure_diagnostic(
                emit,
                control,
                stage,
                error,
                (error.reason_code or error.code)
                if error.code == "source_access_denied"
                else error.code,
            )
            code = (
                "workflow_failed"
                if error.code == "workflow_replay_mismatch"
                else self._model_error(error.code)
            )
            candidate = RunFailed(status="failed", errorCode=code)
        except Exception as error:
            self._close_open_step(state, "failed", emit)
            sqlstate = (
                getattr(error.orig, "sqlstate", None) if isinstance(error, DBAPIError) else None
            )
            reason = (
                "database_schema_mismatch"
                if sqlstate in {"42703", "42P01"}
                else "unexpected_exception"
            )
            _failure_diagnostic(emit, control, stage, error, reason)
            candidate = RunFailed(status="failed", errorCode="internal_error")
        finally:
            if discovered is not None:
                await discovered.close()
        result = control.commit(candidate)
        trace.settle(result.status)
        if controlled:
            if search and state and state.tools and state.tools.search_dispatch:
                # A canceled physical request can outlive its logical receipt. Its
                # durable reservation still contributes to the terminal accounting.
                controlled.budget.tools = await asyncio.to_thread(
                    state.tools.search_dispatch.reserved_count
                )
            emit(controlled.result_event(result, stop_reason))
        return result

    async def _discover_tools(self, context, control, journal):
        binding = getattr(journal, "context_binding", None)
        if binding:
            grants = await journal.capabilities() if journal.capabilities else {}
            if not grants.get("analysis") or not grants.get("model"):
                raise ModelError("forbidden", 403)
            from compass_agent.context_runtime.profiles import discover_plugins

            if grants.get("plugins") is not True or (
                binding.get("answerProtocol") != 2
                and not discover_plugins(binding["scopeProfile"], bool(binding.get("registry")))
            ):
                return None
        options = (
            {"defer_catalog_limit": True}
            if self.schedules and journal and journal.schedule_tools
            else {}
        )
        return await control.perform(lambda: self.tool_loader.discover(context, control, **options))

    async def tool_catalog(self, context, journal, entries, discovered, snapshot):
        capabilities = self.definition.builtin_tools
        answer_protocol = (getattr(journal, "context_binding", None) or {}).get(
            "answerProtocol"
        ) == 2
        if answer_protocol:
            capabilities |= {"completion", "documents"}
        schedules = self.schedules if journal and journal.schedule_tools else None
        if schedules:
            capabilities |= await self.schedule_capabilities(context, journal, snapshot)
        semantic = None
        if journal and (getattr(journal, "context_binding", None) or {}).get("registry"):
            from compass_agent.semantic.service import RegistryTools

            semantic = RegistryTools(journal, context)
            try:
                grants = (await semantic.api("manifest"))["capabilities"]
            except ModelError as exc:
                journal.registry_issue = {"status": "unavailable", "code": exc.code}
                grants = {}
            capabilities = capabilities | {f"semantic_{k}" for k, v in grants.items() if v}
        documents = None
        if answer_protocol:
            from compass_agent.documents.service import DocumentTools

            documents = DocumentTools(journal, context)
        elif search := getattr(journal, "search_snapshot", None):
            if set(search["kinds"]) & {"knowledge", "history", "external"}:
                from compass_agent.documents.service import DocumentTools

                documents = DocumentTools(journal, context)
                capabilities = capabilities | {"documents"}
        result_reader = getattr(journal, "context_results", None)
        if result_reader is not None:
            capabilities = capabilities | {"context_results"}
        goals = None
        if (
            answer_protocol
            and self.autonomy_profiles
            and (journal.store.scheduler or journal.store.distributed)
        ):
            from compass_agent.goal.service import GoalTools

            goals = GoalTools(journal)
            capabilities = capabilities | {"goal"}
        catalog = CatalogContext(
            frozenset(capabilities),
            goals=goals,
            result_reader=result_reader.read if result_reader else None,
            documents=documents,
            semantics=semantic,
            sandbox=self.tool_loader.sandbox is not None,
            schedules=schedules,
            schedule_effect=execute_effect if schedules else None,
        )
        declarations = [entry.definition for entry in entries if entry.definition is not None]
        declarations.extend(catalog_definitions(catalog))
        if discovered:
            if schedules or answer_protocol:
                self.fit_mcp_tools(declarations, discovered, snapshot)
            else:
                declarations.extend(
                    entry.definition for entry in discovered.entries if entry.definition
                )
        workflow_definitions = catalog_definitions(
            CatalogContext(frozenset({"workflow"}), workflow=True)
        )
        workflow = (
            "workflow" in self.definition.builtin_tools
            and self.workflow_assets is not None
            and (
                CREATE_WORKFLOW in snapshot
                if snapshot is not None
                else has_catalog_room([*declarations, *workflow_definitions])
            )
        )
        if workflow:
            declarations.extend(workflow_definitions)
        validate_catalog(declarations)
        return catalog, declarations, workflow

    async def schedule_capabilities(self, context, journal, snapshot):
        if snapshot is not None:
            return set(snapshot).intersection({"schedule_read", "schedule_write"})
        assert self.schedules is not None
        try:
            await self.schedules.read_access(context)
        except ModelError as error:
            if error.status != 403:
                raise
            return set()
        return {"schedule_read"} if journal.scheduled else {"schedule_read", "schedule_write"}

    @staticmethod
    def fit_mcp_tools(declarations, discovered, snapshot):
        selected = []
        required = discovered.required_tools
        for entry in sorted(discovered.entries, key=lambda item: item.name not in required):
            declaration = entry.definition
            if snapshot is not None and entry.name not in snapshot:
                continue
            if declaration and not has_catalog_room([*declarations, declaration]):
                if entry.name in required:
                    raise ToolRejected("tool_catalog_limit")
                logger.info("analysis.tool_skipped tool=%s reason=catalog_limit", entry.name)
                continue
            selected.append(entry)
            if declaration:
                declarations.append(declaration)
        discovered.entries = tuple(selected)

    async def _select_route(self, command, context, control, budget, question, **options):
        from compass_agent.execution_context import current_execution, recorded

        journal = current_execution.get()
        if getattr(journal, "context_optimizer", None) and not journal.context_binding.get(
            "search"
        ):
            journal.context_routing_messages = await journal.context_optimizer.before_route(
                self.models, context, control, budget, question
            )
        if getattr(journal, "context_binding", None):
            from compass_agent.context_runtime.source_access import require_run_sources

            await asyncio.to_thread(
                require_run_sources, journal.store, journal.run_id, context, epoch=journal.epoch
            )
        return await recorded(
            "routing_host"
            if getattr(journal, "context_binding", None)
            and isinstance(self.routing, RoutingService)
            else "routing",
            "routing",
            {
                "question": question,
                "selection": command.selection.model_dump(mode="json"),
                **{
                    key: value.model_dump(mode="json") if isinstance(value, Intent) else value
                    for key, value in options.items()
                },
            },
            lambda: self._select_route_live(command, context, control, budget, question, **options),
            SelectedRoute,
            budget=budget,
        )

    async def _select_route_live(
        self,
        command: AnalysisRunInput,
        context: TrustedContext,
        control: RunControl,
        budget: AutonomyBudget | None,
        question: str,
        *,
        intent_hint: Intent | None,
    ) -> SelectedRoute:
        from compass_agent.execution_context import current_execution

        messages = []
        binding = getattr(current_execution.get(), "context_binding", None) or {}
        optimizer = getattr(current_execution.get(), "context_optimizer", None)
        if optimizer and not binding.get("search"):
            messages = getattr(current_execution.get(), "context_routing_messages", [])
        elif binding and not binding.get("search"):
            for pair in command.history:
                messages.extend(
                    (
                        ChatMessage(role="user", content=pair.question),
                        ChatMessage(role="assistant", content=pair.answer),
                    )
                )
        routing_command = ChatCallInput(
            call_id=uuid5(control.id, "routing"),
            main_model=command.selection,
            input=GenerateInput(messages=[ChatMessage(role="user", content=question)]).model_copy(
                update={"messages": [*messages, ChatMessage(role="user", content=question)]}
            ),
        )
        try:
            return await self.routing.select_route(
                routing_command,
                context,
                deadline=control.deadline_at,
                check=control.require_running,
                intent_hint=intent_hint,
                **({"attempt_budget": budget} if budget is not None else {}),
            )
        except ModelInvocationCanceled as error:
            raise PhaseCanceled(cleanup_confirmed=error.cleanup_confirmed) from None
