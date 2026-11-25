import asyncio
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from compass_agent.api.goals import router as goal_router
from compass_agent.api.mcp import router as mcp_router
from compass_agent.api.models import ModelBoundaryMiddleware, create_model_service, error_response
from compass_agent.api.models import router as model_router
from compass_agent.api.prompt_starters import router as starter_router
from compass_agent.api.promptops import router as promptops_router
from compass_agent.api.retail import router as retail_connection_router
from compass_agent.api.runs import build_agent_executor
from compass_agent.api.runs import router as run_router
from compass_agent.api.runtime_diagnostics import router as runtime_diagnostics_router
from compass_agent.api.scheduler_triggers import router as scheduler_triggers_router
from compass_agent.api.schedules import router as schedules_router
from compass_agent.api.semantic import router as semantic_router
from compass_agent.api.sessions import internal_router
from compass_agent.api.sessions import router as session_router
from compass_agent.api.workflows import router as workflow_asset_router
from compass_agent.data_connections.entities import DataConnection
from compass_agent.database import Base, DemoSale, get_engine, get_session
from compass_agent.llm.admission import ExecutionGate
from compass_agent.models.entities import ModelCall, ModelChange, Provider
from compass_agent.models.errors import ModelError
from compass_agent.models.lifecycle import ExecutionTasks
from compass_agent.observability.observer import Observer
from compass_agent.runtime.control import RunRegistry
from compass_agent.runtime.deployment import REVISION
from compass_agent.runtime.durable_service import DurableRuntime
from compass_agent.runtime.session_management import process_titles
from compass_agent.schemas import DemoOverview, Health
from compass_agent.settings import get_settings
from compass_agent.tools.contracts import ToolBatchPolicy
from compass_agent.tools.loader import ToolLoader
from compass_agent.tools.mcp.client import EndpointPolicy
from compass_agent.tools.mcp.entities import McpConnection
from compass_agent.tools.mcp.repository import McpRepository
from compass_agent.tools.sandbox.allocations import Allocations
from compass_agent.tools.sandbox.client import SandboxBackend
from compass_agent.tools.sandbox.entities import SandboxAllocation
from compass_agent.workflows.assets import WorkflowAsset
from compass_agent.workflows.drafts import WorkflowDrafts


@asynccontextmanager
async def lifespan(app):
    settings = get_settings()
    app.state.execution_gate = ExecutionGate(
        settings.compass_model_max_concurrency, settings.compass_model_max_inflight
    )
    app.state.model_tasks = ExecutionTasks()
    app.state.run_registry = RunRegistry()
    app.state.workflow_drafts = WorkflowDrafts()
    app.state.durable_runtime = None
    try:
        app.state.durable_runtime = DurableRuntime(
            get_engine(), settings, app.state.run_registry, schedule_jobs=True, distributed=True
        )
        await app.state.durable_runtime.start()
        from compass_agent.runtime.resources import RuntimeResources

        app.state.workflow_drafts.durable = RuntimeResources(app.state.durable_runtime.store)
    except SQLAlchemyError:
        pass  # /healthz remains available while /readyz reports the dependency failure.
    app.state.observation = Observer(settings)
    if app.state.durable_runtime:
        app.state.durable_runtime.observer = app.state.observation
    app.state.showcase_running = False
    sandbox = (
        SandboxBackend(
            Allocations(
                get_engine(), settings.compass_sandbox_host_id, slots=settings.compass_sandbox_slots
            ),
            domain=settings.compass_sandbox_domain,
            api_key=settings.compass_sandbox_api_key.get_secret_value(),
            image=settings.compass_sandbox_image,
            volume_prefix=settings.compass_sandbox_volume_prefix,
        )
        if settings.compass_sandbox_enabled
        else None
    )
    app.state.tool_loader = ToolLoader(
        repository=lambda: McpRepository(get_engine(), settings.compass_model_encryption_key),
        endpoints=EndpointPolicy(
            settings.compass_mcp_allowed_origins, settings.compass_mcp_local_fixture_origins
        ),
        sandbox=sandbox,
        policy=ToolBatchPolicy(
            mode=settings.compass_tool_batch_mode, failure_mode=settings.compass_tool_failure_mode
        ),
    )
    closing = asyncio.Event()

    async def reconcile_sandboxes():
        while not closing.is_set():
            try:
                await app.state.tool_loader.reconcile()
            except Exception:
                # Durable leases remain occupied; transient service failures are retried next tick.
                import logging

                logging.getLogger(__name__).warning("sandbox.reconcile_unavailable")
            try:
                await asyncio.wait_for(closing.wait(), timeout=5)
            except TimeoutError:
                pass

    reconciler = asyncio.create_task(reconcile_sandboxes()) if sandbox else None
    runtime = app.state.durable_runtime
    if runtime and runtime.store.distributed:
        runtime.start_consumers(
            lambda budget: build_agent_executor(
                settings,
                get_engine(),
                app.state,
                create_model_service(app.state.execution_gate),
                budget=budget,
            )
        )
    title_worker = (
        asyncio.create_task(
            process_titles(
                runtime.store,
                runtime.access.check,
                lambda: create_model_service(app.state.execution_gate),
                lambda: runtime.store.ready and runtime.owner_healthy,
            )
        )
        if runtime
        else None
    )
    try:
        yield
    finally:
        if title_worker:
            title_worker.cancel()
            await asyncio.gather(title_worker, return_exceptions=True)
        if app.state.durable_runtime:
            await app.state.durable_runtime.close()
        await app.state.run_registry.close()
        closing.set()
        if reconciler:
            try:
                async with asyncio.timeout(10):
                    await asyncio.shield(reconciler)
            except TimeoutError:
                reconciler.cancel()
                await asyncio.gather(reconciler, return_exceptions=True)
        await app.state.model_tasks.close()
        await asyncio.to_thread(app.state.observation.shutdown)


app = FastAPI(
    lifespan=lifespan,
    title="Compass Agent",
    version="0.1.0",
    description="Commerce analysis scaffold",
)


@app.get("/healthz", response_model=Health, operation_id="health")
def health() -> Health:
    return Health()


@app.get("/readyz", response_model=Health, operation_id="readiness")
def readiness(request: Request) -> Health:
    try:
        with get_engine().connect() as connection:
            revisions = connection.scalars(text("SELECT version_num FROM alembic_version")).all()
            if revisions != [REVISION]:
                raise HTTPException(status_code=503, detail="Database or migration unavailable")
            # Resolve required columns without fetching configuration, credentials or call data.
            for model in (
                DemoSale,
                Provider,
                ModelChange,
                ModelCall,
                DataConnection,
                WorkflowAsset,
                McpConnection,
                SandboxAllocation,
            ):
                connection.execute(select(model).limit(0))
            for table in Base.metadata.tables.values():
                if table.name.startswith(("agent_", "prompt_")) or table.name == "model_attempt":
                    connection.execute(select(table).limit(0))
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Database or migration unavailable") from exc
    service = request.app.state.durable_runtime
    if service is None or not service.store.ready:
        raise HTTPException(status_code=503, detail="Runtime dependencies unavailable")
    return Health()


@app.get("/v1/demo/overview", response_model=DemoOverview, operation_id="demo_overview")
def demo_overview(
    response: Response, session: Annotated[Session, Depends(get_session)]
) -> DemoOverview:
    # Opt-in fixture endpoint; this never accepts a tenant or a user-supplied query.
    if not get_settings().compass_demo_enabled:
        raise HTTPException(status_code=404, detail="Demo disabled")
    response.headers["Cache-Control"] = "no-store"
    try:
        row = session.execute(
            select(
                func.min(DemoSale.day),
                func.max(DemoSale.day),
                func.sum(DemoSale.revenue_minor),
                func.sum(DemoSale.orders),
                func.count(),
            ).select_from(DemoSale)
        ).one()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Demo database unavailable") from exc
    start, end, revenue, orders, days = row
    if not days:
        raise HTTPException(status_code=503, detail="Run the demo seed first")
    return DemoOverview(
        start_date=start, end_date=end, revenue_minor=revenue, orders=orders, days=days
    )


# Private model endpoints are isolated from the opt-in synthetic demo API.

app.add_middleware(ModelBoundaryMiddleware)
app.include_router(model_router)
app.include_router(promptops_router)
app.include_router(starter_router)
app.include_router(mcp_router)
app.include_router(run_router)
app.include_router(session_router)
app.include_router(goal_router)

app.include_router(internal_router)
app.include_router(semantic_router)
app.include_router(schedules_router)
app.include_router(scheduler_triggers_router)
app.include_router(runtime_diagnostics_router)
app.include_router(retail_connection_router)


@app.exception_handler(ModelError)
async def model_error_handler(request, error):
    return error_response(error)


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request, error):
    if request.url.path.startswith(
        (
            "/v1/models",
            "/v1/promptops",
            "/v1/prompt-starters",
            "/v1/analysis",
            "/v1/data/",
            "/v1/mcp/",
            "/v1/internal/analysis/",
            "/internal/runtime/",
        )
    ):
        return error_response(ModelError("validation_failed"))
    from fastapi.exception_handlers import request_validation_exception_handler

    return await request_validation_exception_handler(request, error)


app.include_router(workflow_asset_router)
