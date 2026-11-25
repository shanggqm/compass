"""Agent-owned queue, short quota transactions and database-clock execution leases."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import Field
from sqlalchemy import and_, case, func, or_, select, text, tuple_
from sqlalchemy.orm import Session

from compass_agent.models.errors import ModelError
from compass_agent.runtime.deployment import budget_snapshot
from compass_agent.runtime.entities import (
    AgentCommand,
    AgentOutbox,
    AgentRun,
    AgentRunJob,
    AgentSession,
    AgentTenantSchedule,
)
from compass_agent.runtime.private_state import digest
from compass_agent.schemas import RunContractModel, SchedulingPublic

ACTIVE_JOBS = ("leased", "reclaiming")
TERMINAL = {"completed", "failed", "canceled"}
NON_RETRYABLE_RECOVERY = frozenset(
    {
        "workflow_replay_mismatch",
        "checkpoint_invalid",
        "checkpoint_corrupt",
        "runtime_state_invalid",
        "budget_policy_mismatch",
        "schedule_permission_revoked",
    }
)
ADVANCE_NAMESPACE = uuid5(NAMESPACE_URL, "compass.runtime.worker.advance.v1")


class ExecutionGrant(RunContractModel):
    run_id: Annotated[UUID, Field(strict=False)] = Field(alias="runId")
    worker_id: str = Field(alias="workerId", min_length=1, max_length=64)
    boot_id: Annotated[UUID, Field(strict=False)] = Field(alias="bootId")
    epoch: Annotated[UUID, Field(strict=False)]
    expires_at: Annotated[datetime, Field(strict=False)] = Field(alias="expiresAt")
    checkpoint_no: int = Field(alias="checkpointNo", ge=0)


class AdvanceCommand(RunContractModel):
    grant: ExecutionGrant
    expected_checkpoint: int = Field(alias="expectedCheckpoint", ge=0)


class AdvanceAck(RunContractModel):
    command_id: Annotated[UUID, Field(strict=False)] = Field(alias="commandId")
    status: Literal["running", "continue", "parked", "done", "interrupted"]
    checkpoint_no: int = Field(alias="checkpointNo", ge=0)
    reason_code: str | None = Field(alias="reasonCode", max_length=80)


@dataclass(frozen=True)
class ConsumerSlot:
    worker_id: str
    boot_id: UUID


class RunScheduler:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        raw = json.loads(settings.compass_runtime_tenant_overrides)
        if not isinstance(raw, dict):
            raise ValueError("Runtime tenant overrides must be an object")
        self.overrides = {}
        self.budget = budget_snapshot(settings)
        for tenant, limits in raw.items():
            tenant = str(UUID(tenant))
            if not isinstance(limits, dict) or set(limits) - {"active", "backlog"}:
                raise ValueError("Invalid runtime tenant limits")
            if any(
                type(v) is not int or not 1 <= v <= (20 if kind == "backlog" else 200)
                for kind, v in limits.items()
            ):
                raise ValueError("Invalid runtime tenant capacity")
            if limits.get("backlog", 0) > settings.compass_runtime_tenant_backlog:
                raise ValueError("Tenant backlog overrides may only lower the configured limit")
            self.overrides[tenant] = limits
        self.owner_boot_id: UUID | None = None

    @staticmethod
    def admission(tx):
        tx.execute(text("SELECT pg_advisory_xact_lock(984001)"))

    def limits(self, tenant_id):
        return {
            "active": self.settings.compass_runtime_tenant_active,
            "backlog": self.settings.compass_runtime_tenant_backlog,
            **self.overrides.get(str(tenant_id), {}),
        }

    @staticmethod
    def clock(tx):
        return tx.scalar(select(func.clock_timestamp()))

    @staticmethod
    def job(tx, run_id):
        return tx.scalar(select(AgentRunJob).where(AgentRunJob.run_id == run_id).with_for_update())

    @staticmethod
    def count(tx, statuses, tenant_id=None, *, exclude_blocked=False):
        query = (
            select(func.count()).select_from(AgentRunJob).where(AgentRunJob.status.in_(statuses))
        )
        if exclude_blocked:
            query = query.join(AgentRun, AgentRun.id == AgentRunJob.run_id).where(
                (AgentRunJob.status != "parked") | (AgentRun.recovery_status != "blocked")
            )
        if tenant_id is not None:
            query = query.where(AgentRunJob.tenant_id == tenant_id)
        return tx.scalar(query) or 0

    def require_capacity(self, tx, tenant_id):
        pending = ("ready", "leased", "reclaiming", "parked")
        if (
            self.count(tx, pending, exclude_blocked=True)
            >= self.settings.compass_runtime_max_backlog
        ):
            raise ModelError(
                "runtime_backpressure", 429, reason_code="runtime_cluster_backpressure"
            )
        if (
            self.count(tx, pending, tenant_id, exclude_blocked=True)
            >= self.limits(tenant_id)["backlog"]
        ):
            raise ModelError("runtime_backpressure", 429, reason_code="runtime_tenant_backpressure")

    def enqueue(self, tx, session, run, *, initial=False):
        job = self.job(tx, run.id)
        if job is None:
            self.require_capacity(tx, session.tenant_id)
            if tx.get(AgentTenantSchedule, session.tenant_id) is None:
                tx.add(AgentTenantSchedule(tenant_id=session.tenant_id))
                tx.flush()
            job = AgentRunJob(
                run_id=run.id,
                tenant_id=session.tenant_id,
                status="ready",
                ready_at=self.clock(tx),
                claim_count=0,
            )
            tx.add(job)
        elif job.status != "parked":
            raise ModelError("state_conflict", 409)
        else:
            if run.recovery_status == "blocked":
                self.require_capacity(tx, session.tenant_id)
            job.status, job.ready_at = "ready", self.clock(tx)
        run.execution_epoch, run.control_intent, run.reason_code = None, None, "queued"
        if not initial:
            run.status, run.recovery_status = "paused", "ready"
        return self.event(run, job, "queued")

    @staticmethod
    def event(run, job, reason):
        labels = {
            "queued": "任务已接收，等待执行。",
            "claimed": "任务开始执行。",
            "lease_expired": "执行连接中断，正在检查恢复位置。",
            "agent_restarted": "服务已恢复，正在检查执行记录。",
            "worker_released": "执行资源正在释放。",
            "recovered": "已恢复完成的步骤，等待继续执行。",
            "parked": "任务已保存，等待下一步操作。",
            "done": "本次执行已结束。",
            "cleanup_unconfirmed": "正在核对执行结果和资源状态。",
        }
        value = SchedulingPublic(
            status=job.status,
            reasonCode=reason,
            claimCount=job.claim_count,
            occurredAt=datetime.now(UTC),
            summary=labels.get(reason, "任务状态已更新。"),
        ).model_dump(mode="json", by_alias=True)
        ordinal = 1 + sum(
            v["kind"] == "timeline" for v in run.public_view.get("objects", {}).values()
        )
        return {
            "type": "view.upsert",
            "payload": {
                "objectId": "timeline:" + str(uuid4()),
                "revision": 1,
                "kind": "timeline",
                "value": {
                    "ordinal": ordinal,
                    "event": {
                        "type": "stream.extension",
                        "payload": {
                            "name": "compass.runtime.scheduling",
                            "version": 1,
                            "required": False,
                            "data": value,
                        },
                    },
                },
            },
        }

    @staticmethod
    def clear_lease(job):
        job.worker_id = job.worker_boot_id = job.lease_expires_at = None
        job.active_since = job.recovery_token = job.recovery_expires_at = None

    def grant(self, run, job):
        return ExecutionGrant(
            runId=run.id,
            workerId=job.worker_id,
            bootId=job.worker_boot_id,
            epoch=run.execution_epoch,
            expiresAt=job.lease_expires_at,
            checkpointNo=run.checkpoint_no or 0,
        )

    def require_execution(self, tx, run, epoch):
        job = self.job(tx, run.id)
        if (
            job is None
            or job.status != "leased"
            or run.execution_epoch != epoch
            or job.lease_expires_at <= self.clock(tx)
        ):
            raise ModelError("lease_lost", 409)
        return job

    def require_grant(self, tx, run, worker, grant):
        job = self.require_execution(tx, run, grant.epoch)
        if (
            run.id != grant.run_id
            or job.worker_id != worker.worker_id
            or job.worker_id != grant.worker_id
            or job.worker_boot_id != worker.boot_id
            or job.worker_boot_id != grant.boot_id
        ):
            raise ModelError("lease_lost", 409)
        return job

    def claim(self, worker):
        if not self.store.ready:
            raise ModelError("runtime_unavailable", 503)
        with self.store.transaction() as tx:
            self.admission(tx)
            previous = tx.scalar(
                select(AgentRunJob.run_id).where(
                    AgentRunJob.worker_id == worker.worker_id,
                    AgentRunJob.status.in_(ACTIVE_JOBS),
                )
            )
            if previous:
                _, run = self.store.lock(tx, previous)
                job = self.job(tx, run.id)
                if (
                    job.status == "leased"
                    and job.worker_boot_id == worker.boot_id
                    and job.lease_expires_at > self.clock(tx)
                    and run.execution_epoch
                ):
                    return self.grant(run, job)
                return None
            if self.count(tx, ACTIVE_JOBS) >= self.settings.compass_runtime_max_active:
                return None
            for run_id, session_id, tenant_id in self.ready_candidates(tx):
                session = tx.scalar(
                    select(AgentSession)
                    .where(AgentSession.id == session_id)
                    .with_for_update(skip_locked=True)
                )
                if session is None:
                    continue
                run = tx.scalar(
                    select(AgentRun).where(AgentRun.id == run_id).with_for_update(skip_locked=True)
                )
                if run is None:
                    continue
                job = self.job(tx, run_id)
                if (
                    job.status != "ready"
                    or run.status not in {"created", "paused"}
                    or run.recovery_status == "blocked"
                    or run.control_intent
                    or run.cleanup_status == "unconfirmed"
                    or tx.scalar(
                        select(AgentOutbox.id)
                        .where(AgentOutbox.run_id == run_id, AgentOutbox.delivered_at.is_(None))
                        .limit(1)
                    )
                ):
                    continue
                clock = self.clock(tx)
                run.execution_epoch = uuid4()
                run.owner_boot_id = worker.boot_id if self.store.distributed else self.owner_boot_id
                run.status, run.reason_code, run.recovery_status = "running", None, "ready"
                run.started_at = run.started_at or clock
                job.status, job.worker_id, job.worker_boot_id = (
                    "leased",
                    worker.worker_id,
                    worker.boot_id,
                )
                job.lease_expires_at = clock + timedelta(
                    seconds=self.settings.compass_runtime_lease_seconds
                )
                job.first_claimed_at = job.first_claimed_at or clock
                job.claim_count += 1
                tx.get(AgentTenantSchedule, tenant_id).last_claimed_at = clock
                self.store.outbox(tx, session, run, [self.event(run, job, "claimed")])
                return self.grant(run, job)
            return None

    def ready_candidates(self, tx):
        job = AgentRunJob
        occupied = (
            select(job.tenant_id, func.count().label("active"))
            .where(job.status.in_(ACTIVE_JOBS))
            .group_by(job.tenant_id)
            .subquery()
        )
        limits = {
            UUID(tenant): value["active"]
            for tenant, value in self.overrides.items()
            if "active" in value
        }
        capacity = (
            case(limits, value=job.tenant_id, else_=self.settings.compass_runtime_tenant_active)
            if limits
            else self.settings.compass_runtime_tenant_active
        )
        last = func.coalesce(AgentTenantSchedule.last_claimed_at, datetime(1970, 1, 1, tzinfo=UTC))
        order = (last, job.tenant_id, job.ready_at, job.run_id)
        query = (
            select(job.run_id, AgentRun.session_id, job.tenant_id, last, job.ready_at)
            .join(AgentRun, AgentRun.id == job.run_id)
            .join(AgentTenantSchedule, AgentTenantSchedule.tenant_id == job.tenant_id)
            .outerjoin(occupied, occupied.c.tenant_id == job.tenant_id)
            .where(
                job.status == "ready",
                job.ready_at <= func.statement_timestamp(),
                func.coalesce(occupied.c.active, 0) < capacity,
                AgentRun.status.in_(("created", "paused")),
                AgentRun.recovery_status != "blocked",
                AgentRun.control_intent.is_(None),
                AgentRun.cleanup_status != "unconfirmed",
                ~select(AgentOutbox.id)
                .where(AgentOutbox.run_id == job.run_id, AgentOutbox.delivered_at.is_(None))
                .exists(),
            )
            .order_by(*order)
            .limit(32)
        )
        page = query
        for _ in range(4):
            rows = tx.execute(page).all()
            for run_id, session_id, tenant_id, _, _ in rows:
                yield run_id, session_id, tenant_id
            if len(rows) < 32:
                return
            run_id, _, tenant_id, last_claim, ready_at = rows[-1]
            page = query.where(tuple_(*order) > tuple_(last_claim, tenant_id, ready_at, run_id))

    def settle_active(self, tx, run, job):
        if job.active_since is not None:
            until = min(self.clock(tx), job.lease_expires_at)
            run.active_ms += max(0, int((until - job.active_since).total_seconds() * 1000))
            job.active_since = None

    def active_at_boundary(self, tx, run, root_boundary):
        job = self.job(tx, run.id)
        if root_boundary:
            self.settle_active(tx, run, job)
        if job.active_since is None:
            return run.active_ms
        until = min(self.clock(tx), job.lease_expires_at)
        return run.active_ms + max(0, int((until - job.active_since).total_seconds() * 1000))

    def heartbeat(self, worker, grant):
        with self.store.transaction() as tx:
            _, run = self.store.lock(tx, grant.run_id)
            job = self.require_grant(tx, run, worker, grant)
            if run.status != "running" and not (
                self.store.distributed and run.status == "cancelling"
            ):
                raise ModelError("lease_lost", 409)
            job.lease_expires_at = self.clock(tx) + timedelta(
                seconds=self.settings.compass_runtime_lease_seconds
            )
            return self.grant(run, job)

    @staticmethod
    def command_identity(command):
        grant = command.grant
        key = uuid5(
            ADVANCE_NAMESPACE, f"{grant.run_id}:{grant.epoch}:{command.expected_checkpoint}"
        )
        payload = {
            "runId": str(grant.run_id),
            "workerId": grant.worker_id,
            "bootId": str(grant.boot_id),
            "epoch": str(grant.epoch),
            "expectedCheckpoint": command.expected_checkpoint,
        }
        return key, digest(payload)

    def prepare_advance(self, worker, command):
        with self.store.transaction() as tx:
            session, run = self.store.lock(tx, command.grant.run_id)
            key, sha = self.command_identity(command)
            previous = tx.scalar(
                select(AgentCommand).where(
                    AgentCommand.run_id == run.id, AgentCommand.request_id == key
                )
            )
            if previous:
                if (
                    previous.kind != "worker_advance"
                    or previous.request_sha256 != sha
                    or previous.response["grant"]["workerId"] != worker.worker_id
                    or previous.response["grant"]["bootId"] != str(worker.boot_id)
                ):
                    raise ModelError("request_conflict", 409)
                if previous.response["state"] == "committed":
                    return AdvanceAck.model_validate(previous.response["ack"]), False
            if not self.store.ready:
                raise ModelError("runtime_unavailable", 503)
            job = self.require_grant(tx, run, worker, command.grant)
            if previous:
                if previous.response["state"] != "running":
                    raise ModelError("command_interrupted", 409)
                return AdvanceAck.model_validate(previous.response["ack"]), False
            if run.status != "running" or run.control_intent:
                raise ModelError("run_not_runnable", 409)
            if (run.checkpoint_no or 0) != command.expected_checkpoint:
                raise ModelError("checkpoint_conflict", 409)
            if self.store.distributed:
                job.active_since = self.clock(tx)
            ack = AdvanceAck(
                commandId=key,
                status="running",
                checkpointNo=run.checkpoint_no or 0,
                reasonCode=None,
            )
            tx.add(
                AgentCommand(
                    id=uuid4(),
                    tenant_id=session.tenant_id,
                    workspace_id=session.workspace_id,
                    actor_user_id=session.owner_user_id,
                    request_id=key,
                    session_id=session.id,
                    run_id=run.id,
                    kind="worker_advance",
                    request_sha256=sha,
                    response={
                        "schemaVersion": 1,
                        "grant": command.grant.model_dump(mode="json", by_alias=True),
                        "expectedCheckpoint": command.expected_checkpoint,
                        "state": "running",
                        "ack": ack.model_dump(mode="json", by_alias=True),
                    },
                    created_at=self.clock(tx),
                )
            )
            return ack, True

    def acknowledge(self, tx, run, status, reason=None):
        rows = tx.scalars(
            select(AgentCommand).where(
                AgentCommand.run_id == run.id, AgentCommand.kind == "worker_advance"
            )
        ).all()
        for row in rows:
            if row.response["state"] != "running":
                continue
            response = dict(row.response)
            response["state"] = "interrupted" if status == "interrupted" else "committed"
            response["ack"] = AdvanceAck(
                commandId=row.request_id,
                status=status,
                checkpointNo=run.checkpoint_no or 0,
                reasonCode=reason,
            ).model_dump(mode="json", by_alias=True)
            row.response = response

    def accepted_boundary(self, tx, run, step):
        if step.parent_step_id is None:
            self.acknowledge(tx, run, "continue")
            return True
        return False

    def synchronize(self, tx, session, run, events):
        """Close scheduling and advance receipts in the same transaction as the Run boundary."""
        job = self.job(tx, run.id)
        if job is None:
            return events
        if job.status not in ACTIVE_JOBS:
            if run.status in TERMINAL and job.status != "done":
                job.status = "done"
                self.clear_lease(job)
                return [*events, self.event(run, job, "done")]
            return events
        if (
            run.status in TERMINAL
            or run.status in {"waiting", "paused"}
            or (self.store.distributed and run.cleanup_status == "unconfirmed")
        ):
            if self.store.distributed:
                self.settle_active(tx, run, job)
            if run.cleanup_status == "unconfirmed":
                job.status = "reclaiming"
                self.acknowledge(tx, run, "interrupted", "cleanup_unconfirmed")
                return [*events, self.event(run, job, "cleanup_unconfirmed")]
            # Keep the slot until the supervisor drains the old task.
            if job.status == "reclaiming":
                return events
            job.status = "done" if run.status in TERMINAL else "parked"
            self.clear_lease(job)
            run.execution_epoch = None
            self.acknowledge(tx, run, job.status)
            return [*events, self.event(run, job, job.status)]
        return events

    def revoke(self, run_id, reason, *, force=False):
        with self.store.transaction() as tx:
            session, run = self.store.lock(tx, run_id)
            job = self.job(tx, run_id)
            if job is None or job.status not in ACTIVE_JOBS:
                return False
            if job.status == "reclaiming":
                return True
            if not force and job.lease_expires_at > self.clock(tx):
                return False
            job.status = "reclaiming"
            run.execution_epoch, run.recovery_status, run.reason_code = None, "required", reason
            self.acknowledge(tx, run, "interrupted", reason)
            self.store.outbox(tx, session, run, [self.event(run, job, reason)])
            return True

    def release(self, worker, grant):
        with self.store.transaction() as tx:
            session, run = self.store.lock(tx, grant.run_id)
            job = self.require_grant(tx, run, worker, grant)
            reason = "user_paused" if run.control_intent else "worker_released"
            if self.store.distributed:
                self.settle_active(tx, run, job)
            job.status = "reclaiming"
            run.execution_epoch, run.reason_code = None, reason
            self.acknowledge(tx, run, "interrupted", reason)
            self.store.outbox(tx, session, run, [self.event(run, job, reason)])

    def reclaim_candidates(self, *, boot=False):
        with Session(self.store.engine) as tx:
            leased = AgentRunJob.status.in_(ACTIVE_JOBS)
            if not boot:
                leased = and_(
                    leased,
                    or_(
                        AgentRunJob.status == "reclaiming",
                        AgentRunJob.lease_expires_at <= func.clock_timestamp(),
                    ),
                )
            query = (
                select(AgentRunJob.run_id)
                .join(AgentRun, AgentRun.id == AgentRunJob.run_id)
                .where(
                    or_(
                        leased,
                        and_(
                            AgentRunJob.status == "parked",
                            AgentRun.status == "cancelling",
                            AgentRun.execution_epoch.is_(None),
                            AgentRun.cleanup_status != "unconfirmed",
                            AgentRun.reason_code.in_(("execution_unknown", "cleanup_unconfirmed")),
                        ),
                    )
                )
            )
            if self.store.distributed:
                query = (
                    query.where(
                        or_(
                            AgentRunJob.recovery_token.is_(None),
                            AgentRunJob.recovery_expires_at <= func.clock_timestamp(),
                        )
                    )
                    .order_by(AgentRunJob.lease_expires_at, AgentRunJob.run_id)
                    .limit(16)
                )
            return list(tx.scalars(query))

    def claim_recovery(self, run_id):
        with self.store.transaction() as tx:
            session, run = self.store.lock(tx, run_id)
            job = self.job(tx, run_id)
            now = self.clock(tx)
            if job is None or (job.recovery_expires_at and job.recovery_expires_at > now):
                return None
            if job.status == "leased":
                if job.lease_expires_at > now:
                    return None
                self.settle_active(tx, run, job)
                job.status = "reclaiming"
                run.execution_epoch = None
                run.recovery_status, run.reason_code = "required", "lease_expired"
                self.acknowledge(tx, run, "interrupted", "lease_expired")
                self.store.outbox(tx, session, run, [self.event(run, job, "lease_expired")])
            if job.status != "reclaiming" and not (
                job.status == "parked" and run.status == "cancelling"
            ):
                return None
            job.recovery_token = uuid4()
            job.recovery_expires_at = now + timedelta(
                seconds=max(5, self.settings.compass_runtime_lease_seconds)
            )
            return job.recovery_token

    def settle_canceled_model_attempts(self, tx, run_id, unresolved):
        from compass_agent.models.entities import ModelCall
        from compass_agent.runtime.entities import AgentStep, ModelAttempt

        # Model dispatch requires a physical receipt written under the live epoch.
        # Cleanup may arrive after fencing, when logical output can no longer commit.
        for attempt in tx.scalars(
            unresolved.where(
                AgentStep.kind.in_(("model", "routing", "classifier", "ctx_select", "ctx_compact"))
            )
        ):
            receipts = tx.scalars(
                select(ModelAttempt)
                .join(ModelCall, ModelCall.call_id == ModelAttempt.call_id)
                .where(
                    ModelCall.run_id == run_id,
                    ModelCall.agent_attempt_id == attempt.id,
                    ModelAttempt.origin == "live",
                )
            ).all()
            undispatched = [
                receipt
                for receipt in receipts
                if receipt.status == "prepared"
                and not receipt.sent
                and receipt.dispatched_at is None
            ]
            if any(
                not (
                    receipt.status in {"canceled", "completed", "failed", "incomplete"}
                    and receipt.finished_at is not None
                    and receipt.cleanup_confirmed
                )
                for receipt in receipts
                if receipt not in undispatched
            ):
                continue
            finished = self.clock(tx)
            for receipt in undispatched:
                receipt.status, receipt.finished_at = "canceled", finished
                receipt.cleanup_confirmed = True
                receipt.error_code = "user_canceled"
            attempt.status, attempt.finished_at = "canceled", finished
            attempt.error_code = "user_canceled"
        tx.flush()

    def settle_canceled_sandbox_attempts(self, tx, run_id, unresolved):
        from compass_agent.runtime.entities import AgentAttempt
        from compass_agent.tools.sandbox.entities import SandboxAllocation

        for attempt in tx.scalars(
            unresolved.where(AgentAttempt.receipt["sandboxExecutionId"].as_string().is_not(None))
        ):
            identifier = attempt.receipt["sandboxExecutionId"]
            try:
                allocation = tx.get(SandboxAllocation, UUID(identifier))
            except (ValueError, TypeError):
                continue
            if (
                allocation
                and allocation.run_id == run_id
                and allocation.state in {"retained", "released"}
                and not attempt.receipt.get("actionId")
            ):
                attempt.status, attempt.finished_at = "canceled", self.clock(tx)
                attempt.error_code = "user_canceled"
        tx.flush()

    def settle_reclaimed(self, run_id, *, recovery_error=None, recovery_token=None):
        from compass_agent.runtime.entities import (
            AgentAction,
            AgentAttempt,
            AgentStep,
            AgentWait,
        )
        from compass_agent.runtime.store import canceled_execution
        from compass_agent.tools.sandbox.entities import SandboxAllocation

        with self.store.transaction() as tx:
            session, run = self.store.lock(tx, run_id)
            job = self.job(tx, run_id)
            if job is None or run.execution_epoch is not None:
                return
            if self.store.distributed and (
                recovery_token is None
                or job.recovery_token != recovery_token
                or job.recovery_expires_at <= self.clock(tx)
            ):
                return
            parked_cancel = (
                job.status == "parked"
                and run.status == "cancelling"
                and run.reason_code in {"execution_unknown", "cleanup_unconfirmed"}
                and run.cleanup_status != "unconfirmed"
            )
            if job.status != "reclaiming" and not parked_cancel:
                return
            # Validation of stored bytes cannot repair a deterministic replay
            # mismatch. Preserve the execution failure and park it for intervention.
            if run.reason_code in NON_RETRYABLE_RECOVERY:
                recovery_error = recovery_error or run.reason_code
            allocation = tx.scalar(
                select(SandboxAllocation.id)
                .where(
                    SandboxAllocation.run_id == run_id,
                    SandboxAllocation.state.not_in(("released", "retained")),
                )
                .limit(1)
            )
            unresolved = (
                select(AgentAttempt)
                .join(AgentStep, AgentStep.id == AgentAttempt.step_id)
                .where(
                    AgentAttempt.run_id == run_id,
                    AgentAttempt.status.in_(("dispatched", "unknown", "incomplete")),
                    AgentStep.kind != "workflow_host",
                )
            )
            effect = tx.scalar(
                select(AgentAction.id)
                .where(AgentAction.run_id == run_id, AgentAction.state == "unknown")
                .limit(1)
            )
            if run.status == "cancelling":
                self.settle_canceled_model_attempts(tx, run_id, unresolved)
                if not allocation:
                    self.settle_canceled_sandbox_attempts(tx, run_id, unresolved)
            unknown_tool = tx.scalar(
                unresolved.where(AgentStep.kind.in_(("tool", "mcp", "sandbox"))).limit(1)
            )
            if allocation or effect or unknown_tool or run.cleanup_status == "unconfirmed":
                if run.recovery_status != "blocked" or run.reason_code != "cleanup_unconfirmed":
                    run.recovery_status, run.reason_code = "blocked", "cleanup_unconfirmed"
                    self.store.outbox(
                        tx, session, run, [self.event(run, job, "cleanup_unconfirmed")]
                    )
                return
            unknown = tx.scalar(unresolved.limit(1))
            if parked_cancel and unknown:
                return
            events = []
            wait = tx.scalar(
                select(AgentWait.id)
                .where(AgentWait.run_id == run_id, AgentWait.status == "pending")
                .limit(1)
            )
            if run.status == "cancelling" and not unknown:
                run.status, run.finished_at, run.cleanup_status = (
                    "canceled",
                    self.clock(tx),
                    "confirmed",
                )
                run.reason_code, run.recovery_status = "user_canceled", "ready"
                events.extend(canceled_execution(self.store, tx, run))
                job.status = "done"
            elif unknown or recovery_error or run.recovery_status == "blocked":
                run.status = "paused" if run.status != "cancelling" else "cancelling"
                run.recovery_status, run.reason_code = (
                    "blocked",
                    recovery_error or "execution_unknown",
                )
                job.status = "parked"
            elif run.control_intent == "pause" or wait or run.status in TERMINAL:
                job.status = "done" if run.status in TERMINAL else "parked"
                if run.control_intent == "pause":
                    run.status, run.reason_code = "paused", "user_paused"
                    run.cleanup_status, run.recovery_status = "confirmed", "ready"
                elif wait:
                    run.status, run.cleanup_status = "waiting", "confirmed"
            else:
                job.status, job.ready_at = "ready", self.clock(tx)
                run.status, run.reason_code, run.recovery_status = "paused", "queued", "ready"
                run.cleanup_status = "confirmed"
            self.clear_lease(job)
            self.acknowledge(tx, run, "interrupted", "lease_lost")
            reason = "recovered" if job.status == "ready" else job.status
            self.store.outbox(tx, session, run, [*events, self.event(run, job, reason)])
