"""Cross-replica quotas and fenced resource phases for pre-created private NAS volumes."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import case, func, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from compass_agent.tools.contracts import ToolRejected
from compass_agent.tools.sandbox.entities import SandboxAllocation

ACTIVE = ("creating", "running", "stopping", "unknown")


@dataclass(frozen=True)
class Allocation:
    id: UUID
    owner: tuple[UUID, UUID, UUID]
    run_id: UUID
    step_id: UUID
    call_id: str
    attempt_id: UUID
    host: str
    slot: int
    request_digest: str
    sandbox_id: str | None
    state: str
    phase: str
    seq: int
    deadline: datetime
    retain_until: datetime | None
    shared: bool = False
    reconcile_token: UUID | None = None
    absent_since: datetime | None = None

    @property
    def metadata(self):
        return {
            "compass.allocation": str(self.id),
            "compass.phase": self.phase,
            "compass.sequence": str(self.seq),
            "compass.host": self.host,
        }


class Allocations:
    def __init__(self, engine: Engine, host: str, *, slots: int = 16, retention_s: int = 86400):
        if not host or not 1 <= slots <= 256 or not 1 <= retention_s <= 86400:
            raise ValueError("invalid allocation configuration")
        self.engine, self.host, self.slots, self.retention_s = engine, host, slots, retention_s

    @staticmethod
    def value(row):
        return Allocation(
            row.id,
            (row.tenant_id, row.workspace_id, row.actor_user_id),
            row.run_id,
            row.step_id,
            row.tool_call_id,
            row.attempt_id,
            row.host,
            row.slot,
            row.request_digest,
            row.sandbox_id,
            row.state,
            row.resource_phase,
            row.resource_seq,
            row.resource_deadline,
            row.retain_until,
            row.shared,
            row.reconcile_token,
            row.absent_since,
        )

    def _quota(self, session, tenant, *, validate=True):
        # Identical lock ordering in reserve and every auxiliary phase prevents cross-host races.
        for key in (f"sandbox-tenant:{tenant}", f"sandbox-host:{self.host}"):
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
            )
        if not validate:
            return
        for field, value, limit in (
            (SandboxAllocation.host, self.host, 4),
            (SandboxAllocation.tenant_id, tenant, 2),
        ):
            count = session.scalar(
                select(func.count())
                .select_from(SandboxAllocation)
                .where(field == value, SandboxAllocation.state.in_(ACTIVE))
            )
            if count is None or count >= limit:
                raise ToolRejected("sandbox_capacity")

    def reserve(self, owner, run_id, step_id, call_id, attempt_id, digest) -> Allocation:
        now = datetime.now(UTC)
        with Session(self.engine) as session, session.begin():
            self._quota(session, owner[0], validate=False)
            previous = session.scalar(
                select(SandboxAllocation).where(
                    SandboxAllocation.tenant_id == owner[0],
                    SandboxAllocation.workspace_id == owner[1],
                    SandboxAllocation.actor_user_id == owner[2],
                    SandboxAllocation.attempt_id == attempt_id,
                )
            )
            if previous:
                # Repeated delivery must not create a second remote computation.
                raise ToolRejected("sandbox_attempt_exists")
            self._quota(session, owner[0])
            used = set(
                session.scalars(
                    select(SandboxAllocation.slot).where(
                        SandboxAllocation.host == self.host, SandboxAllocation.state != "released"
                    )
                )
            )
            slot = next((i for i in range(self.slots) if i not in used), None)
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
                attempt_id=attempt_id,
                host=self.host,
                slot=slot,
                request_digest=digest,
                sandbox_id=None,
                state="creating",
                resource_phase="compute",
                resource_seq=1,
                created_at=now,
                resource_deadline=now + timedelta(seconds=155),
                retain_until=None,
                reconcile_until=None,
                reconcile_token=None,
            )
            session.add(row)
            session.flush()
            return self.value(row)

    @staticmethod
    def _locked(session, allocation):
        row = session.get(SandboxAllocation, allocation.id, with_for_update=True)
        if (
            row is None
            or row.resource_seq != allocation.seq
            or row.resource_phase != allocation.phase
            or (
                allocation.reconcile_token is not None
                and row.reconcile_token != allocation.reconcile_token
            )
        ):
            raise ToolRejected("sandbox_phase_stale")
        return row

    def attach(self, allocation: Allocation, sandbox_id: str) -> Allocation:
        with Session(self.engine) as session, session.begin():
            row = self._locked(session, allocation)
            if row.state != "creating" or row.sandbox_id is not None:
                raise ToolRejected("sandbox_phase_stale")
            row.sandbox_id, row.state = sandbox_id, "running"
            return self.value(row)

    def unknown(self, allocation: Allocation) -> None:
        with Session(self.engine) as session, session.begin():
            row = self._locked(session, allocation)
            if row.state in ACTIVE:
                row.state = "unknown"

    def cancel_before_create(self, allocation: Allocation) -> None:
        with Session(self.engine) as session, session.begin():
            row = self._locked(session, allocation)
            if row.state != "creating" or row.sandbox_id is not None:
                raise ToolRejected("sandbox_phase_stale")
            # Fresh slots have no data. Collection is using a previously retained volume.
            row.state = "released" if allocation.phase == "compute" else "retained"

    def confirmed(self, allocation: Allocation, *, volume_clean: bool = False) -> Allocation:
        with Session(self.engine) as session, session.begin():
            row = self._locked(session, allocation)
            if row.state not in ACTIVE:
                raise ToolRejected("sandbox_phase_stale")
            if volume_clean and row.resource_phase != "clean":
                raise ToolRejected("sandbox_phase_stale")
            row.sandbox_id = None
            row.state = "released" if volume_clean else "retained"
            row.retain_until = row.retain_until or datetime.now(UTC) + timedelta(
                seconds=self.retention_s
            )
            row.reconcile_until = row.reconcile_token = None
            return self.value(row)

    def phase(self, allocation: Allocation, phase: str, owner=None) -> Allocation:
        if phase not in {"collect", "inspect", "clean"}:
            raise ToolRejected("sandbox_phase_stale")
        with Session(self.engine) as session, session.begin():
            self._quota(session, allocation.owner[0], validate=phase != "clean")
            row = self._locked(session, allocation)
            if row.state != "retained" or row.sandbox_id is not None:
                raise ToolRejected("sandbox_phase_stale")
            if phase == "inspect" and (
                owner != allocation.owner
                or row.retain_until is None
                or row.retain_until <= datetime.now(UTC)
            ):
                raise ToolRejected("sandbox_not_found")
            if phase == "clean" and (
                row.retain_until is None or row.retain_until > datetime.now(UTC)
            ):
                raise ToolRejected("sandbox_retention_active")
            row.resource_phase, row.resource_seq, row.state = (
                phase,
                row.resource_seq + 1,
                "creating",
            )
            row.resource_deadline = datetime.now(UTC) + timedelta(seconds=155)
            row.absent_since = None
            row.reconcile_token = row.reconcile_until = None
            return self.value(row)

    def get(self, identifier: UUID, owner) -> Allocation:
        with Session(self.engine) as session:
            row = session.get(SandboxAllocation, identifier)
            if (
                row is None
                or (row.tenant_id, row.workspace_id, row.actor_user_id) != owner
                or row.state == "released"
            ):
                raise ToolRejected("sandbox_not_found")
            return self.value(row)

    def claim_due(self, *, limit: int = 4) -> list[Allocation]:
        now = datetime.now(UTC)
        with Session(self.engine) as session, session.begin():
            rows = list(
                session.scalars(
                    select(SandboxAllocation)
                    .where(
                        SandboxAllocation.host == self.host,
                        SandboxAllocation.shared.is_(False),
                        SandboxAllocation.state != "released",
                        or_(
                            SandboxAllocation.reconcile_until.is_(None),
                            SandboxAllocation.reconcile_until < now,
                        ),
                        or_(
                            SandboxAllocation.state == "unknown",
                            SandboxAllocation.state.in_(ACTIVE)
                            & (SandboxAllocation.resource_deadline < now),
                            (SandboxAllocation.state == "retained")
                            & (SandboxAllocation.retain_until < now),
                        ),
                    )
                    .order_by(
                        case((SandboxAllocation.state.in_(ACTIVE), 0), else_=1),
                        SandboxAllocation.resource_deadline,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            )
            for row in rows:
                row.reconcile_token = uuid4()
                row.reconcile_until = now + timedelta(seconds=60)
            return [self.value(row) for row in rows]

    def confirmed_executions(self, identifiers: set[str]) -> list[str]:
        keys = []
        for identifier in identifiers:
            try:
                keys.append(UUID(identifier))
            except ValueError:
                continue
        if not keys:
            return []
        with Session(self.engine) as session:
            return [
                str(identifier)
                for identifier in session.scalars(
                    select(SandboxAllocation.id).where(
                        SandboxAllocation.id.in_(keys),
                        SandboxAllocation.host == self.host,
                        SandboxAllocation.state.in_(("retained", "released")),
                    )
                )
            ]

    def observe_absent(self, allocation: Allocation) -> bool:
        # A create may finish after its HTTP response is lost. The stored deadline
        # includes the remote maximum lifetime and server create timeout. Require
        # two successful remote empty listings, separated by a reconciliation lease.
        now = datetime.now(UTC)
        with Session(self.engine) as session, session.begin():
            row = self._locked(session, allocation)
            if row.resource_deadline >= now or row.state not in ACTIVE:
                return False
            if row.absent_since is None:
                row.absent_since = now
                return False
            return row.absent_since + timedelta(seconds=60) <= now

    def observe_resource(self, allocation: Allocation, sandbox_id: str) -> Allocation:
        with Session(self.engine) as session, session.begin():
            row = self._locked(session, allocation)
            if row.state not in ACTIVE:
                raise ToolRejected("sandbox_phase_stale")
            if row.sandbox_id not in (None, sandbox_id):
                raise ToolRejected("sandbox_phase_stale")
            row.sandbox_id = sandbox_id
            row.absent_since = None
            return self.value(row)
