"""Private Agent API. Only the BFF may supply a trusted, already-authorized context."""

import asyncio
import base64
import hmac
from contextlib import nullcontext
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from compass_agent.database import get_engine
from compass_agent.documents.embeddings import EmbeddingUnavailable, embed
from compass_agent.llm.catalog import catalog_page
from compass_agent.llm.policy import LogicalPolicy
from compass_agent.models.errors import ModelError, message
from compass_agent.models.lifecycle import root_task
from compass_agent.models.repository import ModelRepository
from compass_agent.models.secrets import SecretStore
from compass_agent.models.service import ModelService
from compass_agent.prompt_assets import use_prompts
from compass_agent.promptops.reader import current_bodies
from compass_agent.schemas import (
    AvailableModels,
    CallRecordView,
    CallResponse,
    CatalogPage,
    FinalEvent,
    KnowledgeEmbeddingBatch,
    KnowledgeEmbeddings,
    ModelChangePage,
    ModelComparison,
    ModelErrorDetail,
    ModelErrorResponse,
    ObservabilityStatus,
    ProviderCreate,
    ProviderPage,
    ProviderPatch,
    ProviderView,
    SavedProvider,
    ShowcaseCatalog,
    ShowcaseFinal,
    ShowcaseInput,
    ShowcaseReport,
    ShowcaseStart,
    StartEvent,
    TestCallInput,
    TrustedContext,
)
from compass_agent.settings import get_settings

router = APIRouter(
    prefix="/v1/models",
    tags=["models"],
    responses={
        422: {"model": ModelErrorResponse},
        401: {"model": ModelErrorResponse},
        403: {"model": ModelErrorResponse},
        404: {"model": ModelErrorResponse},
        409: {"model": ModelErrorResponse},
        503: {"model": ModelErrorResponse},
    },
)


def get_model_service(request: Request):
    return create_model_service(request.app.state.execution_gate)


def create_model_service(gate):
    settings = get_settings()
    return ModelService(
        ModelRepository(
            get_engine(),
            SecretStore(settings.compass_model_encryption_key),
            settings.compass_model_allowed_origins,
        ),
        gate=gate,
    )


def trusted_context(
    authorization: Annotated[str | None, Header()] = None,
    x_compass_context: Annotated[str | None, Header()] = None,
) -> TrustedContext:
    token = get_settings().compass_agent_service_token
    if not token:
        raise ModelError("unavailable", 503)
    if not authorization or not hmac.compare_digest(
        authorization.encode(), ("Bearer " + token).encode()
    ):
        raise ModelError("unauthenticated", 401)
    try:
        if not x_compass_context or len(x_compass_context) > 4096:
            raise ValueError()
        decoded = base64.urlsafe_b64decode(x_compass_context + "=" * (-len(x_compass_context) % 4))
        return TrustedContext.model_validate_json(decoded)
    except (ValueError, ValidationError) as exc:
        raise ModelError("forbidden", 403) from exc


Context = Annotated[TrustedContext, Depends(trusted_context)]
Service = Annotated[ModelService, Depends(get_model_service)]


def require(context, purpose, scope):
    if context.purpose not in purpose or context.scope.kind != scope:
        raise ModelError("forbidden", 403)


def page_size(value):
    if value not in {20, 50, 100}:
        raise ModelError("validation_failed")
    return value


@router.get("/catalog", response_model=CatalogPage)
def catalog(
    context: Context,
    q: str = Query(default="", max_length=100),
    vendor: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size_: int = Query(default=20, alias="page_size"),
):
    require(context, {"manage"}, "platform")
    return catalog_page(q, vendor, page, page_size(page_size_))


@router.get("/providers", response_model=ProviderPage)
def providers(
    context: Context,
    service: Service,
    q: str = Query(default="", max_length=100),
    kind: Literal["llm", "decision"] | None = None,
    page: int = Query(default=1, ge=1),
    page_size_: int = Query(default=20, alias="page_size"),
):
    require(context, {"manage"}, "platform")
    return service.repository.list(q, kind, page, page_size(page_size_))


@router.post("/providers", response_model=SavedProvider, status_code=201)
def create_provider(body: ProviderCreate, context: Context, service: Service):
    require(context, {"manage"}, "platform")
    return service.repository.save(body, context)


@router.get("/providers/{provider_id}", response_model=ProviderView)
def provider(provider_id: UUID, context: Context, service: Service):
    require(context, {"manage"}, "platform")
    return service.repository.get(provider_id)


@router.patch("/providers/{provider_id}", response_model=SavedProvider)
def patch_provider(provider_id: UUID, body: ProviderPatch, context: Context, service: Service):
    require(context, {"manage"}, "platform")
    return service.repository.save(body, context, provider_id)


@router.get("/providers/{provider_id}/changes", response_model=ModelChangePage)
def changes(
    provider_id: UUID,
    context: Context,
    service: Service,
    page: int = Query(default=1, ge=1),
    page_size_: int = Query(default=20, alias="page_size"),
):
    require(context, {"manage"}, "platform")
    return service.repository.changes(provider_id, page, page_size(page_size_))


@router.get("/operations/{operation_id}", response_model=SavedProvider)
def operation(operation_id: UUID, context: Context, service: Service):
    require(context, {"manage"}, "platform")
    return service.repository.operation(operation_id)


@router.get("/available", response_model=AvailableModels)
def available(context: Context, service: Service, view: Literal["chat", "module"] = "chat"):
    require(context, {"chat" if view == "chat" else "module_read"}, "tenant")
    return service.repository.available()


def start_observation(request, service, context, call_id):
    observer = getattr(request.app.state, "observation", None)
    if observer and observer.tracer:
        service.root_observation = observer.root(call_id, context, request.headers)
        request.state.model_observation = service.root_observation


async def observed_run(request, coroutine):
    observation = getattr(request.state, "model_observation", None)
    try:
        result = await coroutine
        if observation:
            observation.complete(result.status, result.error_code)
        return result
    except BaseException as exc:
        if observation:
            observation.complete(
                "canceled" if isinstance(exc, asyncio.CancelledError) else "failed",
                getattr(exc, "code", "internal_error"),
            )
        raise
    finally:
        if observation:
            observation.end()


@router.get("/observability", response_model=ObservabilityStatus)
def observability(request: Request, context: Context):
    require(context, {"manage"}, "platform")
    return request.app.state.observation.status()


@router.get("/observability/summary", response_model=ModelComparison)
async def summary(
    context: Context,
    service: Service,
    provider_ids: str = Query(max_length=80),
    window: Literal["1h", "24h", "7d"] = "24h",
):
    require(context, {"manage"}, "platform")
    from compass_agent.observability.summary import model_comparison

    try:
        ids = [UUID(value) for value in provider_ids.split(",")]
        if len(ids) != 2 or len(set(ids)) != 2:
            raise ValueError()
    except ValueError:
        raise ModelError("validation_failed") from None
    models = await run_in_threadpool(lambda: [service.repository.get(value) for value in ids])
    return await model_comparison(get_settings(), models, window)


async def invoke(request, service, context, call_id, provider_id, value):
    paths = []
    if getattr(value, "output_format", "text") != "text":
        paths.append("shared/fragments/output-json.md")
        if value.output_schema is not None:
            paths.append("shared/fragments/output-schema.md")
    prompts = (
        await asyncio.to_thread(current_bodies, service.repository.engine, paths)
        if paths and get_settings().compass_context_runtime_enabled
        else None
    )

    async def run(*args, **kwargs):
        with use_prompts(prompts) if prompts is not None else nullcontext():
            return await service.run(*args, **kwargs)

    start_observation(request, service, context, call_id)
    spec = await service.prepare(call_id, provider_id, value, context)
    if not value.stream:
        return await root_task(
            request,
            observed_run(
                request,
                run(
                    spec,
                    value,
                    disconnect=request.is_disconnected,
                    logical_policy=LogicalPolicy(max_attempts=1),
                ),
            ),
        )

    async def events():
        queue = asyncio.Queue(maxsize=16)

        async def emit(event):
            await queue.put(event)

        runner = root_task(
            request,
            observed_run(
                request,
                run(spec, value, emit=emit, logical_policy=LogicalPolicy(max_attempts=1)),
            ),
        )
        pending = None
        try:
            start = StartEvent(
                call_id=call_id, provider_id=provider_id, config_version=spec.spec.config_version
            )
            yield encode_event(start)
            while True:
                pending = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({runner, pending}, return_when=asyncio.FIRST_COMPLETED)
                if pending in done:
                    yield encode_event(pending.result())
                    pending = None
                    continue
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                pending = None
                while not queue.empty():
                    yield encode_event(queue.get_nowait())
                result = runner.result()
                final = FinalEvent.model_validate(
                    result.model_dump(
                        exclude={"kind", "text", "tool_calls", "answers", "raw_response"}
                    )
                )
                yield encode_event(final)
                break
        finally:
            if pending:
                pending.cancel()
            if not runner.done():
                runner.cancel()
            try:
                await asyncio.shield(runner)
            except (Exception, asyncio.CancelledError):
                pass

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/reliability/scenarios", response_model=ShowcaseCatalog)
def reliability_scenarios(context: Context):
    require(context, {"manage"}, "platform")
    from compass_agent.reliability.service import CATALOG

    return CATALOG


@router.post("/reliability/runs", response_model=ShowcaseReport)
async def reliability_run(body: ShowcaseInput, request: Request, context: Context):
    require(context, {"manage"}, "platform")
    if request.app.state.showcase_running:
        raise ModelError("runtime_overloaded", 503)
    prompts = None
    if get_settings().compass_context_runtime_enabled:
        paths = ["support/diagnostics/reliability-request.md"]
        if body.scenario == "schema_invalid":
            paths.extend(("shared/fragments/output-json.md", "shared/fragments/output-schema.md"))
        prompts = await asyncio.to_thread(current_bodies, get_engine(), paths)

    async def events():
        # Acquire admission when iteration starts; check/increment contains no await.
        if request.app.state.showcase_running:
            raise ModelError("runtime_overloaded", 503)
        request.app.state.showcase_running = True
        queue = asyncio.Queue(maxsize=32)
        from compass_agent.reliability.service import run_showcase

        async def emit(event):
            await queue.put(event)

        with use_prompts(prompts) if prompts is not None else nullcontext():
            runner = root_task(request, run_showcase(body, context, emit))
        pending = None
        try:
            yield encode_event(ShowcaseStart(call_id=body.call_id))
            while True:
                pending = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({runner, pending}, return_when=asyncio.FIRST_COMPLETED)
                if pending in done:
                    yield encode_event(pending.result())
                    pending = None
                    continue
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                pending = None
                while not queue.empty():
                    yield encode_event(queue.get_nowait())
                yield encode_event(ShowcaseFinal(**runner.result().model_dump()))
                break
        finally:
            if pending:
                pending.cancel()
            runner.cancel()
            await asyncio.gather(runner, *([pending] if pending else []), return_exceptions=True)
            request.app.state.showcase_running = False

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"},
    )


def encode_event(event):
    encoded = event.model_dump_json()
    if len(encoded.encode()) > 2 * 1048576:
        raise ModelError("response_too_large", 503)
    return "event: " + event.type + "\ndata: " + encoded + "\n\n"


@router.post(
    "/providers/{provider_id}/tests",
    response_model=CallResponse,
    responses={
        200: {
            "content": {
                "text/event-stream": {
                    "schema": {"type": "string", "description": "SSE ModelEvent records"}
                }
            }
        }
    },
)
async def test_model(
    provider_id: UUID, body: TestCallInput, request: Request, context: Context, service: Service
):
    require(context, {"test"}, "platform")
    return await invoke(request, service, context, body.call_id, provider_id, body.input)


@router.get("/calls/{call_id}", response_model=CallRecordView)
def get_call(call_id: UUID, context: Context, service: Service):
    require(context, {"chat"}, "tenant")
    return service.repository.call(call_id, context)


@router.get("/test-calls/{call_id}", response_model=CallRecordView)
def get_test_call(call_id: UUID, context: Context, service: Service):
    require(context, {"test"}, "platform")
    return service.repository.call(call_id, context)


def error_response(error):
    return JSONResponse(
        status_code=error.status,
        content=ModelErrorResponse(
            error=ModelErrorDetail(
                code=error.code,
                message=getattr(error, "message", None) or message(error.reason_code or error.code),
                requestId=str(uuid4()),
                currentVersion=error.current_version,
                reasonCode=error.reason_code,
            )
        ).model_dump(exclude_none=True),
        headers={
            "Cache-Control": "private, no-store",
            **({"Retry-After": "1"} if error.status == 429 else {}),
        },
    )


class ModelBoundaryMiddleware:
    """Bound request bodies and sanitize failures on private Agent APIs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        private = scope.get("path", "").startswith(
            (
                "/v1/models",
                "/v1/promptops",
                "/v1/contextops",
                "/v1/prompt-starters",
                "/v1/analysis",
                "/v1/internal/analysis",
                "/v1/data/",
                "/v1/mcp/",
                "/internal/scheduler/",
            )
        )
        if scope["type"] != "http" or not private:
            return await self.app(scope, receive, send)
        promptops = scope["path"].startswith("/v1/promptops")
        limit = (
            2 * 1048576
            if promptops or (scope["method"] == "POST" and scope["path"].endswith("/tests"))
            else 65536
        )
        body = bytearray()
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body.extend(msg.get("body", b""))
            if len(body) > limit:
                error = ModelError("validation_failed", 413 if promptops else 422)
                return await error_response(error)(scope, receive, send)
            if not msg.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        async def no_store(message):
            if message["type"] == "http.response.start":
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"
                ]
                root = scope.get("state", {}).get("model_observation")
                if root:
                    headers += [(k.lower().encode(), v.encode()) for k, v in root.headers().items()]
                    if message.get("status", 200) >= 400:
                        if root.status is None:
                            root.complete("failed", "internal_error")
                        root.end()
                message["headers"] = [*headers, (b"cache-control", b"private, no-store")]
            await send(message)

        try:
            await self.app(scope, bounded_receive, no_store)
        except SQLAlchemyError:
            await error_response(ModelError("unavailable", 503))(scope, bounded_receive, no_store)


@router.post("/knowledge-embeddings", response_model=KnowledgeEmbeddings)
async def knowledge_embeddings(command: KnowledgeEmbeddingBatch, context: Context):
    if context.scope.kind == "platform":
        require(context, {"manage"}, "platform")
    else:
        require(context, {"knowledge_manage"}, "tenant")
    try:
        vectors = await embed(get_engine(), get_settings(), command.texts)
    except EmbeddingUnavailable as exc:
        raise ModelError("unavailable", 503) from exc
    return KnowledgeEmbeddings(vectors=vectors)
