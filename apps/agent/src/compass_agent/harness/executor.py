"""Business-independent model loop and model execution boundary."""

import inspect
from collections.abc import Awaitable, Callable
from typing import Literal

from compass_agent.harness.autonomy import AutonomyBudget, AutonomyStopped
from compass_agent.harness.context import validate_messages
from compass_agent.harness.contracts import (
    AgentState,
    ModelRoute,
    RepeatStep,
    RunFailure,
    Step,
)
from compass_agent.llm.contracts import AgentCallResponse, RuntimeDelta
from compass_agent.llm.policy import LogicalPolicy
from compass_agent.models.service import ModelInvocationCanceled, ModelService
from compass_agent.schemas import (
    RUN_STREAM_LIMITS,
    CallResponse,
    ChatMessage,
    GenerateInput,
    RunError,
    RunExtensionPayload,
    RunProgressEvent,
    RunResult,
    RunToolPreparation,
    StepFinished,
    StepFinishedPayload,
    StepStarted,
    StepStartedPayload,
    StreamExtension,
    TextDeltaEvent,
    ToolDeltaEvent,
    TrustedContext,
)
from compass_agent.tools.contracts import (
    PhaseCanceled,
    ResourceUnconfirmed,
    ToolControl,
    json_bytes,
)

EmitProgress = Callable[[RunProgressEvent], None]
IncompleteHandler = Callable[[Step, CallResponse], Awaitable[RunResult | RepeatStep | None]]


class AgentLoop:
    """The shared model/tool loop and its injected inference port."""

    async def execute[T](
        self,
        state: AgentState,
        route: ModelRoute | None,
        context: TrustedContext | None,
        control: ToolControl,
        emit: EmitProgress,
        prepare: Callable[[], tuple[GenerateInput, T] | Awaitable[tuple[GenerateInput, T]]],
        handle: Callable[
            [Step, CallResponse, tuple[GenerateInput, T]], Awaitable[RunResult | None]
        ],
        *,
        budget: AutonomyBudget | None = None,
        publish_text: bool | Callable[[], bool] = True,
        model_timeout: float | None = 25,
        handle_incomplete: IncompleteHandler | None = None,
        public_response: Callable[[CallResponse], bool] | None = None,
    ) -> RunResult:
        for index in range(1, RUN_STREAM_LIMITS["steps"] + 1):
            control.require_running()
            if budget:
                budget.require_step()
            step = state.start_step(index)
            if budget:
                budget.generation = step.model_call_id
            emit(
                StepStarted(
                    type="step.started",
                    payload=StepStartedPayload(
                        stepId=str(step.id),
                        stepIndex=step.index,
                        modelCallId=str(step.model_call_id),
                    ),
                )
            )
            prepared = prepare()
            if inspect.isawaitable(prepared):
                prepared = await prepared
            request = prepared[0]
            result = await control.perform(
                lambda step=step, request=request: self._invoke_model(
                    step,
                    request,
                    route,
                    context,
                    control,
                    budget,
                    emit,
                    publish_text=publish_text() if callable(publish_text) else publish_text,
                ),
                timeout_s=model_timeout if budget is not None else None,
            )
            if step.prepared_request is not None:
                prepared = (step.prepared_request, prepared[1])
                state.messages = list(step.prepared_request.messages)
            if budget:
                if result.error_code == "resource_cleanup_failed":
                    raise ResourceUnconfirmed("model cleanup unconfirmed")
                budget.require_continue()
                if result.error_code == "model_timeout":
                    raise AutonomyStopped("execution_timeout")
            if handle_incomplete and (
                result.status == "incomplete"
                or (result.status == "failed" and result.error_code == "empty_output")
            ):
                recovery = await handle_incomplete(step, result)
                if recovery is not None:
                    self._finish_step(step, "failed", emit)
                    if isinstance(recovery, RepeatStep):
                        continue
                    control.require_running()
                    return recovery
            self._require_complete(result)
            if state.trace and (public_response is None or public_response(result)):
                state.trace.model_content(str(step.id), result)
            candidate = await handle(step, result, prepared)
            self._finish_step(step, "completed", emit)
            if candidate is not None:
                control.require_running()
                return candidate
        raise RunFailure("step_limit")

    async def execute_tools[T](
        self,
        state: AgentState,
        route: ModelRoute | None,
        context: TrustedContext | None,
        control: ToolControl,
        emit: EmitProgress,
        prepare: Callable[[], tuple[GenerateInput, T] | Awaitable[tuple[GenerateInput, T]]],
        accept: Callable[
            [Step, CallResponse, tuple[GenerateInput, T]],
            Awaitable[RunResult | RepeatStep | None],
        ],
        dispatch: Callable[[Step, GenerateInput], Awaitable[RunResult | None]],
        *,
        budget: AutonomyBudget | None = None,
        publish_text: bool | Callable[[], bool] = True,
        model_timeout: float | None = 25,
        handle_incomplete: IncompleteHandler | None = None,
        public_response: Callable[[CallResponse], bool] | None = None,
    ) -> RunResult:
        """Advance any configured tool catalog using the same model loop.

        accept validates task constraints and completes plain replies; dispatch owns
        tool policy, execution and observation recording. Neither port is task-specific.
        """

        async def handle(step, result, prepared):
            candidate = await accept(step, result, prepared)
            if isinstance(candidate, RepeatStep):
                return None
            if candidate is not None:
                return candidate
            calls = tuple(result.tool_calls)
            if not calls or len(calls) > RUN_STREAM_LIMITS["toolsPerStep"]:
                raise RunFailure("model_incomplete")
            if not result.continuation_token:
                raise RunFailure("model_incomplete")
            step.tool_calls = calls
            state.messages.append(
                ChatMessage(
                    role="assistant",
                    content=result.text,
                    tool_calls=list(calls),
                )
            )
            state.continuation_token = result.continuation_token
            return await dispatch(step, prepared[0])

        return await self.execute(
            state,
            route,
            context,
            control,
            emit,
            prepare,
            handle,
            budget=budget,
            publish_text=publish_text,
            model_timeout=model_timeout,
            handle_incomplete=handle_incomplete,
            public_response=public_response,
        )

    async def _invoke_model(
        self, step, request, route, context, control, budget, emit=None, *, publish_text=True
    ):
        raise NotImplementedError

    @classmethod
    def _require_complete(cls, result) -> None:
        if result.error_code == "resource_cleanup_failed":
            raise ResourceUnconfirmed("model cleanup unconfirmed")
        if result.status != "completed":
            if result.error_code in {
                "input_token_limit_exceeded",
                "output_token_limit_exceeded",
                "invalid_model_input",
            }:
                raise RunFailure(result.error_code)
            if result.status == "incomplete":
                raise RunFailure("model_incomplete", reason=result.error_code)
            raise RunFailure(cls._model_error(result.error_code or ""), reason=result.error_code)
        expected = "tool_calls" if result.tool_calls else "stop"
        if result.finish_reason != expected:
            raise RunFailure("model_incomplete")

    @staticmethod
    def _final_answer(value: str) -> str:
        answer = value.strip()
        if not answer or len(json_bytes(answer)) > RUN_STREAM_LIMITS["answerBytes"]:
            raise RunFailure("model_incomplete")
        return answer

    @staticmethod
    def _model_error(code: str) -> RunError:
        if code == "model_rate_limited":
            return "model_rate_limited"
        if code == "runtime_overloaded":
            return "runtime_overloaded"
        if code in {
            "model_unavailable",
            "route_binding_missing",
            "chat_model_not_supported",
            "capability_not_supported",
        }:
            return "model_unavailable"
        return "model_failed"

    @staticmethod
    def _finish_step(
        step: Step,
        status: Literal["completed", "failed", "canceled"],
        emit: EmitProgress,
    ) -> None:
        if step.status != "running":
            return
        step.status = status
        emit(
            StepFinished(
                type="step.finished",
                payload=StepFinishedPayload(stepId=str(step.id), status=status),
            )
        )

    def _close_open_step(
        self,
        state: AgentState | None,
        status: Literal["failed", "canceled"],
        emit: EmitProgress,
    ) -> None:
        if state is not None and state.steps:
            self._finish_step(state.steps[-1], status, emit)


class AgentExecutor(AgentLoop):
    def __init__(self, models: ModelService):
        self.models = models

    async def _invoke_model(
        self, step, request, route, context, control, budget, emit=None, *, publish_text=True
    ):
        from compass_agent.execution_context import current_execution, recorded

        journal = current_execution.get()
        if (
            journal
            and getattr(journal, "context_binding", None)
            and getattr(route, "deliverable", None)
            in ("business_qa", "data_analysis", "dashboard", "business_report")
        ):
            from compass_agent.context_runtime.model_step import call_model

            return await call_model(
                journal, self, step, request, route, context, control, budget, emit
            )

        from compass_agent.context_runtime.run_facts import current_facts

        if facts := current_facts():
            messages = facts.messages(request.messages)
            validate_messages(messages)
            request = request.model_copy(update={"messages": messages})
        return await recorded(
            "model",
            f"model:{step.id}",
            {
                "request": request.model_dump(
                    mode="json",
                    exclude={"continuation_token"}
                    | {
                        field
                        for field in ("temperature", "top_p")
                        if getattr(request, field) is None
                    },
                ),
                "provider": str(route.provider_id),
                "binding": route.binding_version,
            },
            lambda: self._invoke_model_live(
                step, request, route, context, control, budget, emit, publish_text=publish_text
            ),
            AgentCallResponse,
            budget=budget,
        )

    async def _invoke_model_live(
        self,
        step: Step,
        request: GenerateInput,
        route: ModelRoute | None,
        context: TrustedContext | None,
        control: ToolControl,
        budget: AutonomyBudget | None,
        emit: EmitProgress | None = None,
        *,
        publish_text: bool | Callable[[], bool] = True,
    ):
        preparing: set[int] = set()

        def publish(index: int, status: Literal["preparing", "prepared"]) -> None:
            if emit is None:
                return
            payload = RunToolPreparation(stepId=str(step.id), toolIndex=index, status=status)
            emit(
                StreamExtension(
                    type="stream.extension",
                    payload=RunExtensionPayload(
                        name="compass.tool.preparation",
                        version=1,
                        required=False,
                        data=payload.model_dump(mode="json", by_alias=True),
                    ),
                )
            )

        async def project_delta(event: RuntimeDelta) -> None:
            from compass_agent.execution_context import (
                current_execution,
                execution_path,
            )

            journal = current_execution.get()
            if (
                publish_text
                and journal
                and isinstance(event, TextDeltaEvent)
                and execution_path.get().startswith("root/model:")
            ):
                await journal.text_delta(event.text)
            if emit is None or not isinstance(event, ToolDeltaEvent):
                return
            index = event.index + 1
            if 1 <= index <= RUN_STREAM_LIMITS["toolsPerStep"] and index not in preparing:
                preparing.add(index)
                publish(index, "preparing")

        assert route is not None and context is not None
        try:
            # Routing leaves the request-scoped ModelService on its classifier stage.
            # Reset it before prepare() creates the logical generation observation.
            self.models.stage = "inference"
            prepared = await self.models.prepare(
                step.model_call_id,
                route.provider_id,
                request,
                context,
                deadline=control.phase_deadline or control.deadline_at,
                expected_binding_version=route.binding_version,
            )
            result = await self.models.run(
                prepared,
                request,
                emit=project_delta if request.stream else None,
                logical_policy=LogicalPolicy(max_attempts=2),
                tool_output_validation="agent_dispatch",
                **({"attempt_budget": budget} if budget is not None else {}),
            )
            for index in sorted(preparing):
                publish(index, "prepared")
            return result
        except ModelInvocationCanceled as error:
            raise PhaseCanceled(cleanup_confirmed=error.cleanup_confirmed) from None
