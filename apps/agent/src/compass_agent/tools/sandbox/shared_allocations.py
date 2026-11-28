"""Short PostgreSQL transactions coordinate reusable environments across Agent replicas."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from compass_agent.tools.contracts import ToolRejected
from compass_agent.tools.sandbox.allocations import ACTIVE, Allocation, Allocations
from compass_agent.tools.sandbox.entities import SandboxAllocation, SandboxExecution

COMMAND_SECONDS = 20
RESOURCE_SECONDS = 900
CREATE_GRACE_SECONDS = 65


@dataclass(frozen=True)
class Command:
    id: UUID
    allocation_id: UUID
    command_id: str | None
    state: str
    deadline: datetime


def command_value(row):
    return Command(row.id, row.allocation_id, row.command_id, row.state, row.deadline)


class SharedAllocations:
    def __init__(self, allocations: Allocations, *, idle_s=600, parallelism=2):
        if not 1 <= idle_s < RESOURCE_SECONDS or not 1 <= parallelism <= 8:
            raise ValueError("invalid shared sandbox limits")
        self.allocations = allocations
        self.engine = allocations.engine
        self.idle_s, self.parallelism = idle_s, parallelism

    def acquire(self, owner, run_id, step_id, call_id, digest):
        """Return (environment, command, creator), or None while a bounded lease is busy."""
        now = datetime.now(UTC)
        with Session(self.engine) as tx, tx.begin():
            # Owner first, then host; no network calls inside either lock.
            tx.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "sandbox-owner:" + ":".join(map(str, owner))},
            )
            existing = tx.scalar(
                select(SandboxExecution).where(
                    SandboxExecution.run_id == run_id,
                    SandboxExecution.step_id == step_id,
                    SandboxExecution.call_id == call_id,
                )
            )
            if existing:
                raise ToolRejected("sandbox_attempt_exists")
            row = tx.scalar(
                select(SandboxAllocation)
                .where(
                    SandboxAllocation.tenant_id == owner[0],
                    SandboxAllocation.workspace_id == owner[1],
                    SandboxAllocation.actor_user_id == owner[2],
                    SandboxAllocation.shared.is_(True),
                    SandboxAllocation.state != "released",
                )
                .with_for_update()
            )
            creator = row is None
            if row is None:
                self.allocations._quota(tx, owner[0], validate=False)
                count = tx.scalar(
                    select(func.count())
                    .select_from(SandboxAllocation)
                    .where(
                        SandboxAllocation.host == self.allocations.host,
                        SandboxAllocation.state.in_(ACTIVE),
                    )
                )
                if count is None or count >= 4:
                    raise ToolRejected("sandbox_capacity")
                used = set(
                    tx.scalars(
                        select(SandboxAllocation.slot).where(
                            SandboxAllocation.host == self.allocations.host,
                            SandboxAllocation.state != "released",
                        )
                    )
                )
                slot = next((i for i in range(self.allocations.slots) if i not in used), None)
                if slot is None:
                    raise ToolRejected("sandbox_storage_capacity")
                row = SandboxAllocation(
                    id=uuid4(),
                    tenant_id=owner[0],
                    workspace_id=owner[1],
                    actor_user_id=owner[2],
                    run_id=run_id,
                    step_id=step_id,
                    tool_call_id=call_id,
                    attempt_id=uuid4(),
                    host=self.allocations.host,
                    slot=slot,
                    request_digest=digest,
                    sandbox_id=None,
                    state="creating",
                    resource_phase="compute",
                    resource_seq=1,
                    created_at=now,
                    resource_deadline=now
                    + timedelta(seconds=RESOURCE_SECONDS + CREATE_GRACE_SECONDS),
                    shared=True,
                    idle_until=now + timedelta(seconds=self.idle_s),
                )
                tx.add(row)
                tx.flush()
            elif row.host != self.allocations.host:
                raise ToolRejected("sandbox_host_mismatch")
            elif row.state != "running" or row.resource_phase != "compute":
                return None
            calls = list(
                tx.scalars(
                    select(SandboxExecution).where(
                        SandboxExecution.allocation_id == row.id,
                        SandboxExecution.state != "finished",
                    )
                )
            )
            if len(calls) >= self.parallelism or any(c.state == "unknown" for c in calls):
                return None
            # Reserve the maximum possible renewed lifetime BEFORE the remote renewal.
            row.resource_deadline = now + timedelta(seconds=RESOURCE_SECONDS + CREATE_GRACE_SECONDS)
            row.idle_until = max(row.idle_until or now, now + timedelta(seconds=self.idle_s))
            call = SandboxExecution(
                id=uuid4(),
                allocation_id=row.id,
                run_id=run_id,
                step_id=step_id,
                call_id=call_id,
                request_digest=digest,
                command_id=None,
                state="preparing",
                created_at=now,
                deadline=now + timedelta(seconds=CREATE_GRACE_SECONDS + COMMAND_SECONDS),
            )
            tx.add(call)
            tx.flush()
            return self.allocations.value(row), command_value(call), creator

    def launched(self, command: Command, remote_id: str):
        with Session(self.engine) as tx, tx.begin():
            row = tx.get(SandboxExecution, command.id, with_for_update=True)
            if row is None or row.state != "preparing":
                raise ToolRejected("sandbox_execution_stale")
            row.command_id, row.state = remote_id, "running"
            row.deadline = datetime.now(UTC) + timedelta(seconds=COMMAND_SECONDS + 10)
            return command_value(row)

    def finish(self, command: Command, *, confirmed: bool):
        with Session(self.engine) as tx, tx.begin():
            allocation = tx.get(SandboxAllocation, command.allocation_id, with_for_update=True)
            row = tx.get(SandboxExecution, command.id, with_for_update=True)
            if (
                row is not None
                and row.state != "finished"
                and (confirmed or row.state != "unknown")
            ):
                row.state = "finished" if confirmed else "unknown"
                if allocation:
                    now = datetime.now(UTC)
                    allocation.idle_until = (
                        max(allocation.idle_until or now, now + timedelta(seconds=self.idle_s))
                        if confirmed
                        else now
                    )

    def source_runs(self, allocation: Allocation):
        with Session(self.engine) as tx:
            return list(
                tx.scalars(
                    select(SandboxExecution.run_id)
                    .where(SandboxExecution.allocation_id == allocation.id)
                    .distinct()
                )
            )

    def due_commands(self):
        with Session(self.engine) as tx:
            return [
                (self.allocations.value(a), command_value(c))
                for a, c in tx.execute(
                    select(SandboxAllocation, SandboxExecution)
                    .join(SandboxExecution, SandboxExecution.allocation_id == SandboxAllocation.id)
                    .where(
                        SandboxAllocation.host == self.allocations.host,
                        SandboxAllocation.state == "running",
                        SandboxExecution.state != "finished",
                        SandboxExecution.deadline < datetime.now(UTC),
                    )
                ).all()
            ]

    def claim_due(self):
        now = datetime.now(UTC)
        with Session(self.engine) as tx, tx.begin():
            rows = list(
                tx.scalars(
                    select(SandboxAllocation)
                    .where(
                        SandboxAllocation.host == self.allocations.host,
                        SandboxAllocation.shared.is_(True),
                        SandboxAllocation.state != "released",
                        or_(
                            SandboxAllocation.reconcile_until.is_(None),
                            SandboxAllocation.reconcile_until < now,
                        ),
                        or_(
                            SandboxAllocation.state.in_(("unknown", "stopping")),
                            SandboxAllocation.resource_deadline < now,
                            (SandboxAllocation.state == "running")
                            & (SandboxAllocation.resource_phase == "compute")
                            & (SandboxAllocation.idle_until < now),
                            (SandboxAllocation.state == "retained")
                            & (SandboxAllocation.retain_until < now),
                        ),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(4)
                )
            )
            claimed = []
            for row in rows:
                calls = list(
                    tx.scalars(
                        select(SandboxExecution).where(
                            SandboxExecution.allocation_id == row.id,
                            SandboxExecution.state != "finished",
                        )
                    )
                )
                # An unconfirmed command blocks new admissions but lets healthy companions finish.
                if any(c.state != "unknown" and c.deadline > now for c in calls):
                    continue
                row.reconcile_token = uuid4()
                row.reconcile_until = now + timedelta(seconds=60)
                if row.state == "running":
                    row.state = "stopping"
                claimed.append(self.allocations.value(row))
            return claimed

    def disposed(self, allocation):
        with Session(self.engine) as tx, tx.begin():
            row = self.allocations._locked(tx, allocation)
            if row.state not in ACTIVE:
                raise ToolRejected("sandbox_phase_stale")
            row.state, row.sandbox_id = "retained", None
            row.retain_until = datetime.now(UTC)
            row.reconcile_until = row.reconcile_token = None
            for call in tx.scalars(
                select(SandboxExecution).where(
                    SandboxExecution.allocation_id == row.id, SandboxExecution.state != "finished"
                )
            ):
                call.state = "finished"
            return self.allocations.value(row)

    def confirmed_executions(self, identifiers):
        keys = []
        for identifier in identifiers:
            try:
                keys.append(UUID(identifier))
            except ValueError:
                pass
        with Session(self.engine) as tx:
            return [
                str(i)
                for i in tx.scalars(
                    select(SandboxExecution.id).where(
                        SandboxExecution.id.in_(keys), SandboxExecution.state == "finished"
                    )
                )
            ]

    def inspection(self, identifier, owner):
        with Session(self.engine) as tx, tx.begin():
            command = tx.get(SandboxExecution, identifier)
            if command is None:
                return None
            row = tx.get(SandboxAllocation, command.allocation_id, with_for_update=True)
            if (
                row is None
                or (row.tenant_id, row.workspace_id, row.actor_user_id) != owner
                or row.state != "running"
                or row.resource_phase != "compute"
                or command.state != "finished"
                or row.resource_deadline < datetime.now(UTC) + timedelta(seconds=30)
            ):
                raise ToolRejected("sandbox_not_found")
            # Bound the read below five seconds; retain the environment during that read.
            row.idle_until = max(
                row.idle_until or datetime.now(UTC), datetime.now(UTC) + timedelta(seconds=30)
            )
            return (
                self.allocations.value(row),
                command_value(command),
                command.run_id,
                command.step_id,
                command.call_id,
            )

    def retire(self, allocation):
        with Session(self.engine) as tx, tx.begin():
            row = self.allocations._locked(tx, allocation)
            if row.state == "running":
                row.state = "stopping"
                row.idle_until = datetime.now(UTC)
