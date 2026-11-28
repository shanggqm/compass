"""Durable allocation/volume leases; logical Run IDs deliberately have no foreign keys."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from compass_agent.database import Base


class SandboxAllocation(Base):
    __tablename__ = "sandbox_allocation"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "workspace_id", "actor_user_id", "attempt_id", name="uq_sandbox_attempt"
        ),
        Index(
            "uq_sandbox_live_slot",
            "host",
            "slot",
            unique=True,
            postgresql_where=text("state <> 'released'"),
        ),
        Index(
            "uq_sandbox_shared_owner",
            "tenant_id",
            "workspace_id",
            "actor_user_id",
            unique=True,
            postgresql_where=text("shared AND state <> 'released'"),
        ),
        Index("ix_sandbox_owner", "tenant_id", "workspace_id", "actor_user_id", "run_id"),
        Index("ix_sandbox_reconcile", "state", "resource_deadline"),
        CheckConstraint("slot >= 0 AND resource_seq > 0", name="ck_sandbox_slot_seq"),
        CheckConstraint(
            "state IN ('creating','running','stopping','retained','unknown','released')",
            name="ck_sandbox_state",
        ),
        CheckConstraint(
            "resource_phase IN ('compute','collect','inspect','clean')", name="ck_sandbox_phase"
        ),
        CheckConstraint("resource_deadline > created_at", name="ck_sandbox_deadline"),
        CheckConstraint(
            "retain_until IS NULL OR retain_until >= created_at", name="ck_sandbox_retention"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    actor_user_id: Mapped[UUID]
    run_id: Mapped[UUID]
    step_id: Mapped[UUID]
    tool_call_id: Mapped[str] = mapped_column(String(128))
    attempt_id: Mapped[UUID]
    host: Mapped[str] = mapped_column(String(128))
    slot: Mapped[int]
    request_digest: Mapped[str] = mapped_column(String(64))
    sandbox_id: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16))
    resource_phase: Mapped[str] = mapped_column(String(16))
    resource_seq: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resource_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconcile_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconcile_token: Mapped[UUID | None]

    shared: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    idle_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    absent_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SandboxExecution(Base):
    """A command lease is independent from the reusable container lease."""

    __tablename__ = "sandbox_execution"
    __table_args__ = (
        UniqueConstraint("run_id", "step_id", "call_id", name="uq_sandbox_execution_call"),
        Index("ix_sandbox_execution_active", "allocation_id", "state", "deadline"),
        CheckConstraint(
            "state IN ('preparing','running','unknown','finished')",
            name="ck_sandbox_execution_state",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    allocation_id: Mapped[UUID] = mapped_column(ForeignKey("sandbox_allocation.id"))
    run_id: Mapped[UUID]
    step_id: Mapped[UUID]
    call_id: Mapped[str] = mapped_column(String(128))
    request_digest: Mapped[str] = mapped_column(String(64))
    command_id: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
