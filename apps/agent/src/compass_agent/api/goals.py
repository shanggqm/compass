"""Owner-scoped goal controls and authorized child execution snapshots."""

import asyncio
from uuid import UUID

from fastapi import APIRouter, Response

from compass_agent.api.runs import Executor, RunContext
from compass_agent.api.sessions import Runtime
from compass_agent.context_runtime.source_access import project_sources
from compass_agent.goal.controller import GoalController
from compass_agent.goal.handoff import read_child_answer
from compass_agent.models.errors import ModelError
from compass_agent.runtime.public_visibility import visible_response
from compass_agent.schemas import ChildTraceSnapshot, GoalAction, SessionGoal

router = APIRouter(prefix="/v1/analysis", tags=["goals"])


@router.get("/sessions/{session_id}/goal", response_model=SessionGoal)
async def read_goal(session_id: UUID, context: RunContext, service: Runtime, response: Response):
    await service.access.check(context)
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(GoalController(service.store).read, session_id, context)


@router.post("/sessions/{session_id}/goal/actions", response_model=SessionGoal)
async def goal_action(
    session_id: UUID,
    command: GoalAction,
    context: RunContext,
    service: Runtime,
    executor: Executor,
    response: Response,
):
    await service.access.check(context)
    if command.action == "resume":
        previous = await asyncio.to_thread(GoalController(service.store).read, session_id, context)
        if previous["runId"]:
            from compass_agent.runtime.journal import ExecutionJournal

            run_id = UUID(previous["runId"])
            run = await asyncio.to_thread(service.store.read_run, run_id, context)
            if run.status == "paused":
                journal = ExecutionJournal(
                    service.store, service.publisher, run_id, run.execution_epoch
                )
                await asyncio.to_thread(journal.validate_checkpoint)
                await service.streams.validate(run)
    receipt = await asyncio.to_thread(
        GoalController(service.store).act, session_id, command, context
    )
    if receipt["runId"]:
        run_id = UUID(receipt["runId"])
        if command.action in {"cancel", "interrupt"} and run_id in service.controls:
            service.controls[run_id].request_stop("user")
        elif command.action == "resume":
            service.schedule(run_id, executor, context)
    response.headers["Cache-Control"] = "no-store"
    return receipt


@router.get("/runs/{run_id}/children/{child_id}", response_model=ChildTraceSnapshot)
async def child_trace(
    run_id: UUID, child_id: UUID, context: RunContext, service: Runtime, response: Response
):
    await service.access.check(context)
    visibility = await service.access.visibility(context)
    run = await asyncio.to_thread(service.store.read_run, run_id, context)
    snapshot = await asyncio.to_thread(
        service.reader.messages, run.session_id, context, run_id=run_id
    )
    projected = await asyncio.to_thread(project_sources, service.store, snapshot, context)
    projected = visible_response(projected, details=visibility["details"])
    nodes = [
        packet.event.payload.value
        for packet in projected.packets
        if packet.event.type == "view.upsert" and packet.event.payload.kind == "execution.node"
    ]
    # Use the exact same projection as history and live events before selecting the subtree.
    values = [node.model_dump(mode="json", by_alias=True) for node in nodes]
    root = next(
        (
            node
            for node in values
            if node["kind"] == "subagent"
            and (node.get("subagent") or {}).get("childRunId") == str(child_id)
        ),
        None,
    )
    if root is None or not visibility["details"]:
        raise ModelError("not_found", 404)
    selected = {root["id"]}
    while True:
        children = {node["id"] for node in values if node.get("parentId") in selected} - selected
        if not children:
            break
        selected |= children
    response.headers["Cache-Control"] = "no-store"
    return {
        "answer": await asyncio.to_thread(
            read_child_answer, service.store, run_id, child_id, context
        ),
        "childRunId": str(child_id),
        "seq": projected.cursors[0].seq,
        "nodes": [node for node in values if node["id"] in selected],
    }
