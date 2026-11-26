"""Scoped, live-authorized ContextOps queries and mandatory audited snapshot access."""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from compass_agent import schemas as dto
from compass_agent.api.models import Context, require
from compass_agent.api.sessions import Runtime
from compass_agent.context_ops.analytics import Analytics
from compass_agent.context_ops.snapshots import Snapshots

router = APIRouter(
    prefix="/v1/contextops",
    tags=["contextops"],
    responses={s: {"model": dto.ModelErrorResponse} for s in (401, 403, 404, 409, 422, 503)},
)


async def authorize(context: Context, runtime: Runtime):
    require(context, {"contextops_read", "contextops_admin"}, "tenant")
    await runtime.access.check(context)
    administrator = context.purpose == "contextops_admin"
    return (
        Analytics(runtime.store, context, administrator, runtime.observer),
        Snapshots(runtime.store, context, administrator),
    )


Services = Annotated[tuple[Analytics, Snapshots], Depends(authorize)]
Filters = Annotated[dto.ContextOpsFilter, Query()]


@router.get("/overview", response_model=dto.ContextOverview)
async def overview(services: Services, filters: Filters):
    return await asyncio.to_thread(services[0].overview, filters)


@router.get("/intents", response_model=dto.ContextIntents)
async def intents(services: Services, filters: Filters):
    return await asyncio.to_thread(services[0].intents, filters)


@router.get("/anomalies", response_model=dto.ContextAnomalies)
async def anomalies(services: Services, filters: Filters):
    return await asyncio.to_thread(services[0].anomalies, filters)


@router.get("/runs", response_model=dto.ContextRuns)
async def runs(services: Services, filters: Filters):
    return await asyncio.to_thread(services[0].list_runs, filters)


@router.get("/runs/{run_id}/flow", response_model=dto.ContextRunFlow)
async def flow(run_id: UUID, services: Services):
    return await asyncio.to_thread(services[0].flow, run_id)


@router.get("/snapshots/{snapshot_ref}", response_model=dto.ContextSnapshotView)
async def snapshot(snapshot_ref: str, services: Services):
    return await asyncio.to_thread(services[1].read, snapshot_ref)


@router.get("/security/events", response_model=dto.ContextSecurityEvents)
async def events(
    services: Services,
    window: dto.ContextWindow = "24h",
    event_type: Annotated[str | None, Query(max_length=32)] = None,
    action: Annotated[str | None, Query(max_length=16)] = None,
    actor_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: UUID | None = None,
):
    filters = dto.ContextOpsFilter(window=window, actor_id=actor_id, limit=limit, cursor=cursor)
    return await asyncio.to_thread(services[0].security_events, filters, event_type, action)


@router.get("/badcases", response_model=dto.ContextBadcases)
async def badcases(
    services: Services,
    root_cause_category: dto.ContextRootCause | None = None,
    scene_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    return await asyncio.to_thread(
        services[1].list_badcases,
        root_cause_category=root_cause_category,
        scene_id=scene_id,
        limit=limit,
    )


@router.post("/badcases", response_model=dto.ContextBadcaseView, status_code=201)
async def create_badcase(data: dto.ContextBadcaseInput, services: Services):
    return await asyncio.to_thread(services[1].create_badcase, data)


@router.post("/badcases/{badcase_id}/replay", response_model=dto.ContextReplayComparison)
async def replay(badcase_id: UUID, data: dto.ContextReplayInput, services: Services):
    return await asyncio.to_thread(services[1].replay, badcase_id, data.override)
