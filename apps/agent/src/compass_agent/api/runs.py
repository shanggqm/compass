"""Private Agent Run API with versioned SSE and a separate cancellation control plane."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from compass_agent.api.models import get_model_service, require, trusted_context
from compass_agent.data_connections.connections import DataConnections
from compass_agent.data_connections.tools import provider
from compass_agent.database import get_engine
from compass_agent.harness.autonomy import AutonomyLimits
from compass_agent.models.errors import ModelError
from compass_agent.models.service import ModelService
from compass_agent.routing.service import RoutingService
from compass_agent.runtime.context import compass_definition
from compass_agent.runtime.control import (
    OwnerKey,
    RunCapacityExceeded,
    RunControl,
    RunRegistry,
    RunRegistryClosed,
    TerminalRunSummary,
)
from compass_agent.runtime.execution_detail import legacy_node
from compass_agent.runtime.observation import (
    _log_cancel as _log_cancel,
)
from compass_agent.runtime.observation import (
    _log_progress as _log_progress,
)
from compass_agent.runtime.observation import (
    _log_terminal as _log_terminal,
)
from compass_agent.runtime.observation import (
    _run_log as _run_log,
)
from compass_agent.runtime.service import RunService
from compass_agent.runtime.stream import RunWriter
from compass_agent.schemas import (
    DEFAULT_RUN_TIMEOUT_SECONDS,
    RUN_STREAM_LIMITS,
    AnalysisCancelAck,
    AnalysisCancelAlreadyTerminal,
    AnalysisRunInput,
    ControlCommand,
    ControlReceipt,
    DashboardDocumentV1,
    ModelErrorResponse,
    RunEmpty,
    RunFailed,
    RunFinished,
    RunProgressEvent,
    RunStarted,
    SandboxInspection,
    TrustedContext,
)
from compass_agent.settings import get_settings
from compass_agent.tools.contracts import ToolRejected
from compass_agent.tools.mcp.repository import owner_key
from compass_agent.workflows.assets import WorkflowAssets

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/v1/analysis",
    tags=["analysis"],
    responses={
        401: {"model": ModelErrorResponse},
        403: {"model": ModelErrorResponse},
        404: {"model": ModelErrorResponse},
        422: {"model": ModelErrorResponse},
        429: {"model": ModelErrorResponse},
        503: {"model": ModelErrorResponse},
    },
)


def get_run_registry(request: Request) -> RunRegistry:
    return request.app.state.run_registry


Context = Annotated[TrustedContext, Depends(trusted_context)]
Registry = Annotated[RunRegistry, Depends(get_run_registry)]


class RunStreamResponse(StreamingResponse):
    """Always listen for disconnect independently of a potentially blocked send."""

    def __init__(
        self,
        *args,
        on_unstarted_close: Callable[[], Awaitable[None]] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.on_unstarted_close = on_unstarted_close

    async def __call__(self, scope, receive, send) -> None:
        stream = asyncio.create_task(self.stream_response(send))
        disconnect = asyncio.create_task(self.listen_for_disconnect(receive))
        try:
            done, _ = await asyncio.wait({stream, disconnect}, return_when=asyncio.FIRST_COMPLETED)
            if disconnect in done:
                stream.cancel()
                await asyncio.gather(stream, return_exceptions=True)
            else:
                disconnect.cancel()
                await asyncio.gather(disconnect, return_exceptions=True)
                try:
                    stream.result()
                except OSError as error:
                    raise ClientDisconnect() from error
        finally:
            for task in (stream, disconnect):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stream, disconnect, return_exceptions=True)
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                try:
                    await close()
                except (Exception, asyncio.CancelledError):
                    pass
            if self.on_unstarted_close is not None:
                await self.on_unstarted_close()
        if self.background is not None:
            await self.background()


def _owner(context: TrustedContext) -> OwnerKey:
    require(context, {"chat"}, "tenant")
    return OwnerKey.from_context(context)


def require_run_context(context: Context) -> TrustedContext:
    _owner(context)
    return context


RunContext = Annotated[TrustedContext, Depends(require_run_context)]


def get_agent_executor(
    request: Request,
    _context: RunContext,
    service: Annotated[ModelService, Depends(get_model_service)],
) -> RunService:
    return build_agent_executor(get_settings(), get_engine(), request.app.state, service)


def build_agent_executor(settings, engine, app_state, model_service, *, budget=None):
    from compass_agent.runtime.deployment import budget_snapshot
    from compass_agent.scheduler.service import ScheduleService
    from compass_agent.settings import AutonomyModelProfile

    config = budget if budget is not None else budget_snapshot(settings)
    connections = DataConnections(engine, settings)
    executor = RunService(
        model_service,
        RoutingService(model_service, settings),
        tools_provider=provider(connections),
        tool_loader=app_state.tool_loader,
        autonomy_profiles=tuple(AutonomyModelProfile.model_validate(p) for p in config["profiles"])
        if config["controlled"]
        else None,
        autonomy_limits=AutonomyLimits(
            tokens=config["tokens"],
            cost_microusd=config["costMicrousd"],
        ),
        definition=compass_definition(),
        workflow_assets=WorkflowAssets(engine),
        workflow_drafts=app_state.workflow_drafts,
    )
    runtime = getattr(app_state, "durable_runtime", None)
    if runtime and runtime.store.distributed:
        executor.schedules = ScheduleService(runtime.store, runtime.access, model_service, settings)
    return executor


Executor = Annotated[RunService, Depends(get_agent_executor)]


async def _observed_runner(
    request: Request,
    executor: RunService,
    command: AnalysisRunInput,
    context: TrustedContext,
    control: RunControl,
    emit,
):
    observation = getattr(request.state, "model_observation", None)
    try:
        result = await executor.run(command, context, control, emit)
        if observation:
            observation.complete(result.status, getattr(result, "error_code", None))
        _log_terminal(control, result)
        return result
    except BaseException as error:
        if control.terminal is None:
            if isinstance(error, asyncio.CancelledError):
                control.mark_cleanup_unconfirmed()
                control.request_stop("shutdown")
            result = control.commit(RunFailed(status="failed", errorCode="internal_error"))
        else:
            result = control.terminal
        if observation:
            observation.complete(
                "canceled" if isinstance(error, asyncio.CancelledError) else "failed",
                getattr(error, "code", "internal_error"),
            )
        _log_terminal(control, result)
        return result
    finally:
        if observation:
            observation.end()


@router.post(
    "/runs",
    responses={
        200: {
            "content": {
                "text/event-stream": {
                    "schema": {"type": "string", "description": "compass.run v1 SSE packets"}
                }
            }
        }
    },
)
async def start_run(
    body: AnalysisRunInput,
    request: Request,
    context: RunContext,
    registry: Registry,
    executor: Executor,
):
    if hasattr(request.app.state, "durable_runtime"):
        from compass_agent.runtime.legacy import start_legacy

        service = request.app.state.durable_runtime
        if service is None or not service.store.private.key:
            raise ModelError("runtime_unavailable", 503)
        return await start_legacy(service, body, context, executor, request.headers)
    owner = _owner(context)
    try:
        timeout = body.constraints.timeout_seconds if body.constraints else None
        control = registry.register(owner, lifetime_s=timeout or DEFAULT_RUN_TIMEOUT_SECONDS)
    except RunCapacityExceeded:
        raise ModelError("runtime_overloaded", 429) from None
    except RunRegistryClosed:
        raise ModelError("unavailable", 503) from None

    observer = getattr(request.app.state, "observation", None)
    if observer and observer.tracer:
        root = observer.root(control.id, context, request.headers, capture_content=False)
        request.state.model_observation = root
        executor.models.root_observation = root

    events_started = False

    def finish_without_runner() -> None:
        if control.terminal is not None:
            return
        ack = control.request_stop("disconnected")
        _log_cancel(control, ack, "disconnected")
        result = control.commit(RunFailed(status="failed", errorCode="internal_error"))
        observation = getattr(request.state, "model_observation", None)
        if observation:
            observation.complete(result.status, getattr(result, "error_code", None))
            observation.end()
        _log_terminal(control, result)

    async def finish_if_unstarted() -> None:
        if not events_started:
            finish_without_runner()

    async def events():
        nonlocal events_started
        events_started = True
        writer = RunWriter(body.request_id, control.id)
        progress: asyncio.Queue[RunProgressEvent] = asyncio.Queue(
            maxsize=RUN_STREAM_LIMITS["events"]
        )
        runner: asyncio.Task | None = None
        pending: asyncio.Task | None = None

        def emit(event: RunProgressEvent) -> None:
            _log_progress(control, event)
            if event.type == "stream.extension" and event.payload.name == "compass.execution.node":
                data = legacy_node(event.payload.data)
                event = event.model_copy(
                    update={"payload": event.payload.model_copy(update={"data": data})}
                )
            progress.put_nowait(event)

        try:
            # Iteration starts only after FastAPI has accepted the response; no routing,
            # model or SQL work exists before this first packet is yielded.
            _run_log("analysis.run.started", control, status="running")
            yield writer.write(RunStarted(type="run.started", payload=RunEmpty()))
            runner = request.app.state.model_tasks.create(
                _observed_runner(request, executor, body, context, control, emit)
            )
            assert runner is not None
            while True:
                if runner.done():
                    while not progress.empty():
                        yield writer.write(progress.get_nowait())
                    result = runner.result()
                    yield writer.write(
                        RunFinished(
                            type="run.finished", payload=result, durationMs=control.duration_ms
                        )
                    )
                    return
                pending = asyncio.create_task(progress.get())
                done, _ = await asyncio.wait(
                    {runner, pending},
                    timeout=RUN_STREAM_LIMITS["heartbeatMs"] / 1000,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if pending in done:
                    yield writer.write(pending.result())
                    pending = None
                    continue
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                pending = None
                if runner in done:
                    continue
                yield writer.heartbeat()
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            if control.terminal is None:
                if runner is None:
                    finish_without_runner()
                else:
                    ack = control.request_stop("disconnected")
                    _log_cancel(control, ack, "disconnected")

    return RunStreamResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"},
        on_unstarted_close=finish_if_unstarted,
    )


@router.post("/runs/{run_id}/cancel", response_model=AnalysisCancelAck | ControlReceipt)
async def cancel_run(
    run_id: UUID,
    _body: ControlCommand | RunEmpty,
    response: Response,
    context: RunContext,
    registry: Registry,
    request: Request,
):
    if isinstance(_body, ControlCommand):
        from compass_agent.api.sessions import apply_control, runtime

        response.headers["Cache-Control"] = "no-store"
        return await apply_control(run_id, "cancel", _body, context, runtime(request))
    owner = _owner(context)
    durable = getattr(getattr(request.scope.get("app"), "state", None), "durable_runtime", None)
    if durable is not None:
        from uuid import uuid4

        from compass_agent.api.sessions import apply_control
        from compass_agent.runtime.store import TERMINAL
        from compass_agent.schemas import AnalysisCancelAccepted

        run = await asyncio.to_thread(durable.store.read_run, run_id, context)
        response.headers["Cache-Control"] = "no-store"
        if run.status in TERMINAL:
            await durable.access.check(context)
            response.status_code = 200
            return AnalysisCancelAck(
                root=AnalysisCancelAlreadyTerminal(
                    runId=str(run_id), status="already_terminal", terminalStatus=run.status
                )
            )
        await apply_control(
            run_id,
            "cancel",
            ControlCommand(requestId=uuid4(), expectedVersion=run.version),
            context,
            durable,
        )
        response.status_code = 202
        return AnalysisCancelAck(root=AnalysisCancelAccepted(runId=str(run_id), status="accepted"))
    item = registry.lookup(run_id)
    if item is None or item.owner != owner:
        raise ModelError("not_found", 404)
    if isinstance(item, TerminalRunSummary):
        response.status_code = 200
        ack = AnalysisCancelAlreadyTerminal(
            runId=str(run_id),
            status="already_terminal",
            terminalStatus=item.status,
        )
        logger.info(
            "analysis.run.cancel",
            extra={
                "event_name": "analysis.run.cancel",
                "run_id": str(run_id),
                "status": ack.status,
                "terminal_status": item.status,
                "stop_reason": "user",
                "cleanup": ("unconfirmed" if item.status == "cleanup_unconfirmed" else "confirmed"),
            },
        )
        return AnalysisCancelAck(root=ack)
    response.status_code = 202
    ack = item.request_stop("user")
    _log_cancel(item, ack, "user")
    return AnalysisCancelAck(root=ack)


@router.get(
    "/runs/{run_id}/artifacts/{artifact_id}",
    response_class=Response,
    responses={
        200: {
            "content": {
                t: {} for t in ("image/png", "text/csv", "text/markdown", "application/json")
            }
        }
    },
)
async def read_artifact(run_id: UUID, artifact_id: UUID, request: Request, context: RunContext):
    service = request.app.state.durable_runtime
    if service is not None and service.store.private.key:
        await service.access.check(context)
        item = await asyncio.to_thread(service.store.artifact, run_id, artifact_id, context)
        return Response(
            item.content,
            media_type=item.media_type,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'attachment; filename="{item.name}"',
            },
        )
    try:
        item = request.app.state.tool_loader.artifacts.read(
            str(artifact_id), owner_key(context), run_id
        )
    except ToolRejected:
        raise ModelError("not_found", 404) from None
    return Response(
        item.content,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'inline; filename="chart.png"',
        },
    )


@router.get("/runs/{run_id}/artifacts/{artifact_id}/dashboard")
async def read_dashboard(
    run_id: UUID, artifact_id: UUID, request: Request, response: Response, context: RunContext
) -> DashboardDocumentV1:
    service = request.app.state.durable_runtime
    if service is None or not service.store.private.key:
        raise ModelError("not_found", 404)
    await service.access.check(context)
    item = await asyncio.to_thread(service.store.artifact, run_id, artifact_id, context)
    if item.media_type != "application/json" or len(item.content) > 512 * 1024:
        raise ModelError("not_found", 404)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return DashboardDocumentV1.model_validate_json(item.content)


@router.post("/runs/{run_id}/sandboxes/{allocation_id}/inspect")
async def inspect_sandbox(
    run_id: UUID, allocation_id: UUID, request: Request, context: RunContext
) -> SandboxInspection:
    backend = request.app.state.tool_loader.sandbox
    if backend is None:
        raise ModelError("not_found", 404)
    try:
        allocation = await asyncio.to_thread(
            backend.inspection_target, allocation_id, owner_key(context)
        )
        if allocation.run_id != run_id:
            raise ToolRejected("sandbox_not_found")
        service = request.app.state.durable_runtime
        if service is not None:
            from compass_agent.context_runtime.source_access import sandbox_sources

            await service.access.check(context)
            runs = await asyncio.to_thread(
                backend.inspection_runs, allocation_id, owner_key(context)
            )
            await asyncio.to_thread(sandbox_sources, service.store, context, runs)
        receipt = await backend.inspect(allocation_id, owner_key(context))
    except ToolRejected:
        raise ModelError("not_found", 404) from None
    chart_digest = None
    if receipt.file is not None:
        from compass_agent.tools.artifacts import validate_png

        try:
            validate_png(receipt.file)
            chart_digest = hashlib.sha256(receipt.file).hexdigest()
        except ToolRejected:
            pass
    return SandboxInspection.model_validate(
        {
            "allocationId": str(allocation.id),
            "runId": str(run_id),
            "stepId": str(allocation.step_id),
            "toolCallId": allocation.call_id,
            "status": receipt.status,
            "cleanup": receipt.cleanup,
            "result": receipt.payload,
            "chartSha256": chart_digest,
        }
    )
