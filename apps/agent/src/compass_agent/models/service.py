"""Application-owned admission, configuration, task ownership and durable call metadata."""

import asyncio
import logging
from datetime import UTC, datetime
from math import nextafter
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool

from compass_agent.llm.admission import ExecutionGate
from compass_agent.llm.contracts import (
    AgentCallResponse,
    AttemptBudget,
    ExecutionRequest,
    GenerationOutput,
    ProviderAccess,
    RuntimeCancelled,
    ToolOutputValidation,
)
from compass_agent.llm.executor import ReliableExecutor
from compass_agent.llm.litellm_chat import LiteLLMChatAdapter
from compass_agent.llm.policy import CallPolicy, LogicalPolicy
from compass_agent.llm.runtime import Runtime
from compass_agent.llm.systemone_decision import SystemOneDecisionAdapter
from compass_agent.models.attempts import AttemptRecorder
from compass_agent.models.errors import ModelError
from compass_agent.models.secrets import ModelContinuationCodec
from compass_agent.schemas import CallResponse, GenerateInput

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from compass_agent.observability.observer import Observation


class ModelInvocationCanceled(asyncio.CancelledError):
    """Cancellation visible to existing callers plus a trusted cleanup receipt for Runs."""

    def __init__(self, *, cleanup_confirmed: bool):
        self.cleanup_confirmed = cleanup_confirmed
        super().__init__("model invocation canceled")


def create_runtime(secrets, transport_factory=None):
    return Runtime(
        {
            "litellm_chat": LiteLLMChatAdapter(ModelContinuationCodec(secrets), transport_factory),
            "systemone_decision": SystemOneDecisionAdapter(transport_factory),
        }
    )


def to_response(result, *, agent_call=False):
    response = CallResponse(
        execution=result.execution,
        call_id=result.call_id,
        provider_id=result.provider_id,
        config_version=result.config_version,
        requested_model=result.requested_model,
        status=result.status,
        duration_ms=result.duration_ms,
        kind=result.output.kind,
        usage=result.usage,
        validation=result.validation,
        response_model=result.response_model,
        upstream_request_id=result.upstream_request_id,
        finish_reason=result.finish_reason,
        error_code=result.error.code if result.error else None,
        upstream_outcome_unknown=result.upstream_outcome_unknown,
    )
    if isinstance(result.output, GenerationOutput):
        response.text = result.output.text
        response.tool_calls = list(result.output.tool_calls)
        response.continuation_token = result.output.continuation_token
        if agent_call:
            return AgentCallResponse(
                **response.model_dump(), analysis_thinking=result.output.analysis_thinking
            )
    else:
        response.answers = result.output.answers
        response.raw_response = result.output.raw_response
    return response


async def settle(task, seconds):
    """Bounded cancellation shielding for one already-owned task, never a retry."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    canceled = False
    owner = asyncio.current_task()
    cancellations = owner.cancelling() if owner else 0
    while not task.done() and loop.time() < deadline:
        try:
            await asyncio.wait_for(asyncio.shield(task), max(0, deadline - loop.time()))
        except asyncio.CancelledError:
            # The owned task may itself be canceled (e.g. the disconnect watcher).
            # Only cancellation directed at this caller changes its outcome.
            current = owner.cancelling() if owner else 0
            canceled = canceled or current > cancellations
            cancellations = current
        except Exception:
            break
    return canceled


def consume(task):
    if not task.cancelled():
        task.exception()


class ModelService:
    def __init__(self, repository, runtime=None, *, gate: ExecutionGate):
        self.repository = repository
        self.runtime = runtime or create_runtime(repository.secrets)
        self.gate = gate
        self.root_observation: Observation | None = None
        self.observations: dict[UUID, Observation] = {}
        self.stage = "test"

    async def prepare(
        self,
        call_id,
        provider_id,
        request,
        context,
        *,
        deadline=None,
        grant=None,
        expected_binding_version=None,
    ):
        observation = (
            self.root_observation.child(
                "compass.logical",
                call_id=str(call_id),
                provider_id=str(provider_id),
                stage=self.stage,
                purpose=context.purpose,
            )
            if self.root_observation
            else None
        )
        if observation:
            self.observations[call_id] = observation
        from compass_agent.context_ops.recorder import active_capture, record_logical

        if active_capture.get() is not None:
            if observation:
                observation.content_enabled = False
            try:
                record_logical(observation, call_id)
            except Exception:
                # Observability must never become a prerequisite for provider execution.
                if observation:
                    observation.owner.instrumentation_errors += 1
        try:
            prepared = await self._prepare(
                call_id,
                provider_id,
                request,
                context,
                deadline=deadline,
                grant=grant,
                expected_binding_version=expected_binding_version,
            )
            if observation:
                observation.set(
                    model=prepared.spec.api_model,
                    config_version=prepared.spec.config_version,
                    binding_version=prepared.spec.binding_version,
                )
            return prepared
        except BaseException as exc:
            if observation:
                observation.complete(
                    "canceled" if isinstance(exc, asyncio.CancelledError) else "failed",
                    getattr(exc, "code", "internal_error"),
                )
                observation.end()
                self.observations.pop(call_id, None)
                if self.root_observation:
                    self.root_observation.complete(
                        "canceled" if isinstance(exc, asyncio.CancelledError) else "failed",
                        getattr(exc, "code", "internal_error"),
                    )
            raise

    async def _prepare(
        self,
        call_id,
        provider_id,
        request,
        context,
        *,
        deadline=None,
        grant=None,
        expected_binding_version=None,
    ):
        started = asyncio.get_running_loop().time()
        if deadline is not None and deadline <= started:
            raise ModelError("model_timeout", 503)
        # Round toward the start so floating point addition cannot exceed the 180s cap.
        maximum = nextafter(started + 180, started)
        policy = CallPolicy(started, min(deadline, maximum) if deadline else maximum)
        task = asyncio.create_task(
            run_in_threadpool(
                self.repository.begin,
                call_id,
                provider_id,
                request.model_copy(deep=True),
                context.model_copy(deep=True),
                policy,
                datetime.now(UTC),
                grant,
                expected_binding_version,
            )
        )
        try:
            async with asyncio.timeout_at(policy.deadline_monotonic):
                return await asyncio.shield(task)
        except (asyncio.CancelledError, TimeoutError) as exc:
            canceled = isinstance(exc, asyncio.CancelledError)
            canceled = await settle(task, 2) or canceled
            cleanup_confirmed = task.done() and not task.cancelled()
            if task.done() and not task.cancelled() and task.exception() is None:
                prepared = task.result()
                result = self._initial(prepared, request)
                result.status = "canceled" if canceled else "failed"
                result.error_code = None if result.status == "canceled" else "model_timeout"
                result.duration_ms = max(
                    0, int((asyncio.get_running_loop().time() - started) * 1000)
                )
                canceled = await self._finish(result, False) or canceled
                cleanup_confirmed = result.error_code != "record_write_failed"
            else:
                task.add_done_callback(consume)
            if canceled:
                raise ModelInvocationCanceled(cleanup_confirmed=cleanup_confirmed) from None
            if isinstance(exc, TimeoutError):
                raise ModelError("model_timeout", 503) from None
            raise

    @staticmethod
    def _initial(prepared, request):
        return CallResponse(
            call_id=prepared.context.call_id,
            provider_id=prepared.spec.provider_id,
            config_version=prepared.spec.config_version,
            status="failed",
            duration_ms=0,
            requested_model=prepared.spec.api_model,
            kind=request.kind,
        )

    async def run(
        self,
        prepared,
        request,
        emit=None,
        disconnect=None,
        *,
        logical_policy: LogicalPolicy | None = None,
        tool_output_validation: ToolOutputValidation = "strict",
        attempt_budget: AttemptBudget | None = None,
    ):
        result = self._initial(prepared, request)
        task = watcher = None
        canceled = False
        sent = False
        cleanup_confirmed = True
        try:
            key, params = self.repository.secrets.open_connection(
                prepared.spec.provider_id, prepared.ciphertext
            )
            effective_request = request.model_copy(deep=True)
            if isinstance(effective_request, GenerateInput):
                profile = prepared.spec.generation_profile
                if profile is not None:
                    defaults = profile.defaults.model_dump(exclude_none=True)
                    for name, value in defaults.items():
                        if name not in request.model_fields_set or getattr(request, name) is None:
                            setattr(effective_request, name, value)
            invocation = ExecutionRequest(
                prepared.context,
                ProviderAccess(prepared.spec, SecretStr(key), params),
                effective_request,
                prepared.policy,
                tool_output_validation,
            )
            task = asyncio.create_task(
                ReliableExecutor(self.runtime, self.gate).execute(
                    invocation,
                    logical_policy or LogicalPolicy(),
                    emit,
                    observation=self.observations.get(prepared.context.call_id),
                    recorder=AttemptRecorder(self.repository, attempt_budget),
                    **({"attempt_budget": attempt_budget} if attempt_budget is not None else {}),
                )
            )
            if disconnect:

                async def watch():
                    while not await disconnect():
                        await asyncio.sleep(0.05)

                watcher = asyncio.create_task(watch())
                done, _ = await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    # A failed disconnect monitor must also terminate its owned invocation.
                    watcher.result()
                    raise asyncio.CancelledError()
            outcome = await asyncio.shield(task)
            sent = outcome.upstream_sent
            result = to_response(outcome, agent_call=tool_output_validation == "agent_dispatch")
            if len(result.model_dump_json().encode()) > 2 * 1048576:
                result.status = "failed"
                result.error_code = "response_too_large"
                result.text = ""
                result.tool_calls = []
                result.answers = {}
                result.raw_response = None
                result.continuation_token = None
                if isinstance(result, AgentCallResponse):
                    result.analysis_thinking = ""
        except asyncio.CancelledError:
            canceled = True
            result.status = "canceled"
            if task is not None:
                if not task.done():
                    task.cancel()
                await settle(task, prepared.policy.cleanup_timeout_s + 0.1)
                if task.done():
                    try:
                        outcome = task.result()
                        result = to_response(outcome)
                        sent = outcome.upstream_sent
                        cleanup_confirmed = outcome.cleanup_confirmed
                    except RuntimeCancelled as exc:
                        result = to_response(exc.result)
                        sent = exc.result.upstream_sent
                        cleanup_confirmed = exc.result.cleanup_confirmed
                    except (Exception, asyncio.CancelledError):
                        sent = True
                        cleanup_confirmed = False
                else:
                    sent = True
                    cleanup_confirmed = False
                    task.cancel()
                    task.add_done_callback(consume)
                result.status = "canceled"
                result.error_code = None
                result.upstream_outcome_unknown = sent
        except Exception as exc:
            result.error_code = exc.code if isinstance(exc, ModelError) else "internal_error"
            if task is not None:
                sent = True
                task.cancel()
                await settle(task, prepared.policy.cleanup_timeout_s + 0.1)
                task.add_done_callback(consume)
                result.upstream_outcome_unknown = True
        finally:
            if watcher is not None:
                watcher.cancel()
                canceled = await settle(watcher, 0.1) or canceled
                watcher.add_done_callback(consume)
            if not result.duration_ms:
                result.duration_ms = max(
                    0,
                    int(
                        (asyncio.get_running_loop().time() - prepared.policy.started_monotonic)
                        * 1000
                    ),
                )
            if result.status != "completed":
                result.continuation_token = None
            canceled = await self._finish(result, sent) or canceled
            cleanup_confirmed &= result.error_code != "record_write_failed"
        if canceled:
            raise ModelInvocationCanceled(cleanup_confirmed=cleanup_confirmed)
        return result

    async def _finish(self, result, sent):
        observation = self.observations.pop(result.call_id, None)
        finalize = observation.child("compass.finalize") if observation else None
        try:
            return await self._finish_record(result, sent)
        finally:
            if finalize:
                finalize.complete(result.status, result.error_code)
                finalize.end()
            if observation:
                observation.complete(result.status, result.error_code, authoritative=True)
                observation.end()

    async def _finish_record(self, result, sent):
        task = asyncio.create_task(
            run_in_threadpool(self.repository.finish, result.model_copy(deep=True))
        )
        canceled = await settle(task, 2)
        write_ok = False
        failure_type = "incomplete_transaction"
        if task.done() and not task.cancelled():
            try:
                write_ok = task.result() is True
            except Exception as exc:
                failure_type = type(exc).__name__
        if not write_ok:
            task.add_done_callback(consume)
            result.status = "failed"
            result.error_code = "record_write_failed"
            result.upstream_outcome_unknown = sent
            logger.error(
                "Model metadata write failed; call_id=%s; failure_type=%s",
                result.call_id,
                failure_type,
            )
        if result.status != "completed":
            result.continuation_token = None
        return canceled
