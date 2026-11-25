"""Optimistic, idempotent Run controls and Session context boundaries."""

from sqlalchemy import select

from compass_agent.models.errors import ModelError
from compass_agent.runtime.entities import (
    AgentAction,
    AgentAttempt,
    AgentRun,
    AgentSession,
    AgentStep,
    AgentWait,
)
from compass_agent.runtime.hitl import approval_event
from compass_agent.runtime.run_scheduler import ACTIVE_JOBS
from compass_agent.runtime.store import TERMINAL, canceled_execution, now, owner_filter


def control(store, run_id, kind, command, context):
    payload = {"kind": kind, "runId": str(run_id), **command.model_dump(mode="json", by_alias=True)}
    with store.transaction(admission=kind == "resume") as tx:
        old = store.command(tx, context, command.request_id, payload)
        session, run = store.lock(tx, run_id, context)
        if old:
            return old.response
        if run.version != command.expected_version:
            raise ModelError("version_conflict", 409, run.version)
        if run.status in TERMINAL:
            raise ModelError("run_terminal", 409)
        events = control_run(store, tx, session, run, kind)
        store.outbox(tx, session, run, events)
        result = {
            "sessionId": str(session.id),
            "runId": str(run.id),
            "version": run.version,
            "status": run.status,
            "code": None,
        }
        store.record_command(
            tx, context, command.request_id, kind, payload, session.id, run.id, result
        )
        return result


def control_run(store, tx, session, run, kind):
    events = []
    if kind == "pause":
        if run.status != "running":
            raise ModelError("state_conflict", 409)
        run.control_intent = "pause"
    elif kind == "resume":
        if run.status == "waiting":
            raise ModelError("decision_required", 409)
        legacy_created = (
            store.scheduler is not None
            and run.status == "created"
            and store.scheduler.job(tx, run.id) is None
        )
        if (
            (run.status != "paused" and not legacy_created)
            or run.recovery_status == "blocked"
            or run.cleanup_status == "unconfirmed"
        ):
            raise ModelError("resume_blocked", 409)
        if not store.ready:
            raise ModelError("runtime_unavailable", 503)
        run.control_intent, run.reason_code = None, None
        if store.scheduler:
            events.append(store.scheduler.enqueue(tx, session, run))
    elif kind == "cancel":
        run.status, run.control_intent = "cancelling", None
        job = store.scheduler.job(tx, run.id) if store.scheduler else None
        reclaiming = job is not None and job.status in ACTIVE_JOBS
        unknown = tx.scalar(
            select(AgentAttempt.id)
            .join(AgentStep, AgentStep.id == AgentAttempt.step_id)
            .where(
                AgentAttempt.run_id == run.id,
                AgentAttempt.status.in_(["dispatched", "unknown", "incomplete"]),
                AgentStep.kind != "workflow_host",
            )
            .limit(1)
        )
        if unknown and run.execution_epoch is None and not reclaiming:
            if job is not None and job.status == "parked":
                if run.recovery_status != "blocked":
                    run.recovery_status, run.reason_code = "blocked", "execution_unknown"
            else:
                run.cleanup_status, run.reason_code = "unconfirmed", "cleanup_unconfirmed"
        pending = tx.scalars(
            select(AgentWait).where(
                AgentWait.run_id == run.id,
                AgentWait.status == "pending",
                AgentWait.kind != "reconciliation",
            )
        ).all()
        for wait in pending:
            wait.status, wait.decided_at = "canceled", now()
            events.append(approval_event(wait, reason="user_canceled"))
        # Dispatch moves an approved action to unknown under the same Run lock.
        for action in tx.scalars(
            select(AgentAction).where(AgentAction.run_id == run.id, AgentAction.state == "pending")
        ):
            action.state, action.updated_at = "denied", now()
        if (
            run.execution_epoch is None
            and run.cleanup_status != "unconfirmed"
            and not reclaiming
            and not unknown
        ):
            run.status, run.finished_at, run.cleanup_status = "canceled", now(), "confirmed"
            run.reason_code, run.recovery_status = "user_canceled", "ready"
            events.extend(canceled_execution(store, tx, run))
        if store.scheduler and not store.distributed and job is not None and job.status == "leased":
            job.status, run.execution_epoch = "reclaiming", None
            store.scheduler.acknowledge(tx, run, "interrupted", "user_canceled")
    else:
        raise ModelError("validation_failed", 422)
    return events


def context_reset(store, session_id, command, context):
    payload = {
        "kind": "reset",
        "sessionId": str(session_id),
        **command.model_dump(mode="json", by_alias=True),
    }
    with store.transaction() as tx:
        old = store.command(tx, context, command.request_id, payload)
        session = tx.scalar(
            select(AgentSession)
            .where(AgentSession.id == session_id, *owner_filter(context))
            .with_for_update()
        )
        if session is None:
            raise ModelError("not_found", 404)
        if old:
            return old.response
        if session.version != command.expected_version:
            raise ModelError("version_conflict", 409)
        last = tx.scalar(
            select(AgentRun)
            .where(AgentRun.session_id == session.id)
            .order_by(AgentRun.ordinal.desc())
            .limit(1)
        )
        if last and last.status not in TERMINAL:
            raise ModelError("session_busy", 409)
        session.context_start_ordinal = last.ordinal + 1 if last else 1
        session.version += 1
        session.updated_at = now()
        result = {
            "sessionId": str(session.id),
            "runId": None,
            "version": session.version,
            "status": "reset",
            "code": None,
        }
        store.record_command(
            tx, context, command.request_id, "reset", payload, session.id, None, result
        )
        return result
