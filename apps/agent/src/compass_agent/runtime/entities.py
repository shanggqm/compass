"""Agent-owned durable execution records. Public projections and private state stay separate."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from compass_agent.database import Base

DOCUMENT = JSON().with_variant(JSONB(), "postgresql")
ACTIVE_RUN = "status IN ('created','running','waiting','paused','cancelling')"
TERMINAL_RUN = "status IN ('completed','failed','canceled')"


class AgentTenantSchedule(Base):
    __tablename__ = "agent_tenant_schedule"
    tenant_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    last_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRunJob(Base):
    __tablename__ = "agent_run_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ready','leased','reclaiming','parked','done')", name="ck_agent_job_status"
        ),
        CheckConstraint("claim_count >= 0", name="ck_agent_job_claim_count"),
        CheckConstraint("status != 'ready' OR ready_at IS NOT NULL", name="ck_agent_job_ready"),
        CheckConstraint(
            "(status IN ('leased','reclaiming') AND worker_id IS NOT NULL "
            "AND worker_boot_id IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status NOT IN ('leased','reclaiming') AND worker_id IS NULL "
            "AND worker_boot_id IS NULL AND lease_expires_at IS NULL)",
            name="ck_agent_job_lease",
        ),
        Index(
            "ix_agent_job_ready",
            "tenant_id",
            "ready_at",
            "run_id",
            postgresql_where=text("status = 'ready'"),
        ),
        Index(
            "ix_agent_job_expiry", "lease_expires_at", postgresql_where=text("status = 'leased'")
        ),
        Index(
            "uq_agent_job_worker",
            "worker_id",
            unique=True,
            postgresql_where=text("status::text = ANY (ARRAY['leased','reclaiming']::text[])"),
            sqlite_where=text("status IN ('leased','reclaiming')"),
        ),
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_run.id"), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("agent_tenant_schedule.tenant_id"))
    status: Mapped[str] = mapped_column(String(16), default="ready")
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(64))
    worker_boot_id: Mapped[UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovery_token: Mapped[UUID | None] = mapped_column(Uuid)
    recovery_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_count: Mapped[int] = mapped_column(Integer, default=0)


class AgentSession(Base):
    __tablename__ = "agent_session"
    __table_args__ = (
        CheckConstraint(
            "version > 0 AND context_start_ordinal > 0", name="ck_agent_session_version"
        ),
        CheckConstraint("session_type IN ('chat','workflow')", name="ck_agent_session_type"),
        CheckConstraint(
            "title_status IN ('fallback','pending','generated','manual')",
            name="ck_agent_session_title_status",
        ),
        CheckConstraint(
            "title_attempts BETWEEN 0 AND 2"
            " AND ((title_status = 'pending') = (title_next_at IS NOT NULL))"
            " AND (deleted_at IS NULL OR title_status != 'pending')",
            name="ck_agent_session_title_job",
        ),
        Index(
            "ix_agent_session_title_ready",
            "title_next_at",
            "id",
            postgresql_where=text("title_status = 'pending'"),
        ),
        Index(
            "ix_agent_session_owner_activity",
            "tenant_id",
            "workspace_id",
            "owner_user_id",
            "updated_at",
            "id",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    owner_user_id: Mapped[UUID] = mapped_column(Uuid)
    title: Mapped[str] = mapped_column(Text)
    title_status: Mapped[str] = mapped_column(String(12), server_default="fallback")
    title_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    title_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    session_type: Mapped[str] = mapped_column(String(16), default="chat")
    workflow_key: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(BigInteger, default=1)
    context_start_ordinal: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentRun(Base):
    __tablename__ = "agent_run"
    __table_args__ = (
        UniqueConstraint("session_id", "id", name="uq_agent_run_session_id"),
        UniqueConstraint("session_id", "ordinal", name="uq_agent_run_ordinal"),
        UniqueConstraint("session_id", "request_id", name="uq_agent_run_request"),
        CheckConstraint(
            "status IN "
            "('created','running','waiting','paused','cancelling','completed','failed','canceled')",
            name="ck_agent_run_status",
        ),
        CheckConstraint(
            f"({TERMINAL_RUN}) = (finished_at IS NOT NULL)", name="ck_agent_run_finished"
        ),
        CheckConstraint(
            "version > 0 AND ordinal > 0 AND active_ms >= 0", name="ck_agent_run_counts"
        ),
        CheckConstraint(
            "cleanup_status IN ('none','pending','confirmed','unconfirmed')",
            name="ck_agent_run_cleanup",
        ),
        CheckConstraint(
            "recovery_status IN ('ready','required','blocked')", name="ck_agent_run_recovery"
        ),
        CheckConstraint(
            "control_intent IS NULL OR control_intent = 'pause'", name="ck_agent_run_intent"
        ),
        ForeignKeyConstraint(
            ["id", "checkpoint_no"],
            ["agent_checkpoint.run_id", "agent_checkpoint.number"],
            name="fk_agent_run_checkpoint",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(
            "uq_agent_run_active",
            "session_id",
            unique=True,
            postgresql_where=text(
                "status::text = ANY (ARRAY['created','running','waiting',"
                "'paused','cancelling']::text[])"
            ),
            sqlite_where=text(ACTIVE_RUN),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("agent_session.id"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    ordinal: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="created")
    version: Mapped[int] = mapped_column(BigInteger, default=1)
    projected_seq: Mapped[str] = mapped_column(String(48), default="0-0")
    checkpoint_no: Mapped[int | None] = mapped_column(BigInteger)
    execution_epoch: Mapped[UUID | None] = mapped_column(Uuid)
    owner_boot_id: Mapped[UUID | None] = mapped_column(Uuid)
    control_intent: Mapped[str | None] = mapped_column(String(16))
    recovery_status: Mapped[str] = mapped_column(String(16), default="ready")
    cleanup_status: Mapped[str] = mapped_column(String(16), default="none")
    reason_code: Mapped[str | None] = mapped_column(String(80))
    config_snapshot: Mapped[dict] = mapped_column(DOCUMENT)
    public_view: Mapped[dict] = mapped_column(DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_token: Mapped[UUID | None] = mapped_column(Uuid)
    publish_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_initialized: Mapped[bool] = mapped_column(Boolean, server_default="false")
    publish_generation: Mapped[int] = mapped_column(BigInteger, server_default="0")
    active_ms: Mapped[int] = mapped_column(BigInteger, default=0)


class AgentMessage(Base):
    __tablename__ = "agent_message"
    __table_args__ = (
        UniqueConstraint("run_id", "id", name="uq_agent_message_run_id"),
        UniqueConstraint("run_id", "ordinal", name="uq_agent_message_ordinal"),
        CheckConstraint("revision > 0 AND ordinal > 0", name="ck_agent_message_counts"),
        CheckConstraint(
            "status IN ('streaming','completed','incomplete')", name="ck_agent_message_status"
        ),
        CheckConstraint(
            "(channel = 'conversation' AND role IN ('user','assistant') AND public_content IS "
            "NOT NULL AND private_content IS NULL) OR (channel = 'context' AND role IN "
            "('system','user','assistant','tool') AND public_content IS NULL AND "
            "private_content IS NOT NULL)",
            name="ck_agent_message_channel",
        ),
        ForeignKeyConstraint(
            ["run_id", "step_id"],
            ["agent_step.run_id", "agent_step.id"],
            name="fk_agent_message_step",
        ),
        ForeignKeyConstraint(
            ["run_id", "attempt_id"],
            ["agent_attempt.run_id", "agent_attempt.id"],
            name="fk_agent_message_attempt",
        ),
        Index(
            "uq_agent_message_conversation",
            "run_id",
            "role",
            unique=True,
            postgresql_where=text("channel = 'conversation'"),
            sqlite_where=text("channel = 'conversation'"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_run.id"))
    step_id: Mapped[UUID | None] = mapped_column(Uuid)
    attempt_id: Mapped[UUID | None] = mapped_column(Uuid)
    ordinal: Mapped[int] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String(16))
    role: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    source_refs: Mapped[list | None] = mapped_column(DOCUMENT)
    public_content: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
    )
    private_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    tool_call_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentToolBinding(Base):
    __tablename__ = "agent_tool_binding"
    __table_args__ = (
        UniqueConstraint("run_id", "id", name="uq_agent_tool_binding_run_id"),
        UniqueConstraint("run_id", "alias", name="uq_agent_tool_binding_alias"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_run.id"))
    catalog_id: Mapped[str] = mapped_column(Text)
    alias: Mapped[str] = mapped_column(Text)
    original_name: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16))
    config_ref: Mapped[str | None] = mapped_column(Text)
    config_revision: Mapped[int | None] = mapped_column(Integer)
    declaration: Mapped[dict] = mapped_column(DOCUMENT)
    schema_sha256: Mapped[str] = mapped_column(String(64))
    binding: Mapped[dict] = mapped_column(DOCUMENT)
    policy: Mapped[dict] = mapped_column(DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentStep(Base):
    __tablename__ = "agent_step"
    __table_args__ = (
        UniqueConstraint("run_id", "id", name="uq_agent_step_run_id"),
        UniqueConstraint("run_id", "node_key", name="uq_agent_step_key"),
        UniqueConstraint("run_id", "ordinal", name="uq_agent_step_ordinal"),
        UniqueConstraint("parent_step_id", "call_ordinal", name="uq_agent_step_host_call"),
        ForeignKeyConstraint(
            ["run_id", "parent_step_id"],
            ["agent_step.run_id", "agent_step.id"],
            name="fk_agent_step_parent",
        ),
        ForeignKeyConstraint(
            ["run_id", "tool_binding_id"],
            ["agent_tool_binding.run_id", "agent_tool_binding.id"],
            name="fk_agent_step_tool",
        ),
        ForeignKeyConstraint(
            ["id", "accepted_attempt_id"],
            ["agent_attempt.step_id", "agent_attempt.id"],
            name="fk_agent_step_accepted",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "ordinal > 0 AND revision > 0 AND (call_ordinal IS NULL OR call_ordinal > 0)",
            name="ck_agent_step_counts",
        ),
        CheckConstraint(
            "status IN ('pending','running','waiting','completed','failed','canceled')",
            name="ck_agent_step_status",
        ),
        CheckConstraint(
            "status <> 'completed' OR kind IN ('group','control') OR accepted_attempt_id IS NOT"
            " NULL",
            name="ck_agent_step_result",
        ),
        CheckConstraint(
            "parent_step_id IS NULL OR parent_step_id <> id", name="ck_agent_step_parent"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_run.id"))
    parent_step_id: Mapped[UUID | None] = mapped_column(Uuid)
    node_key: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16))
    ordinal: Mapped[int] = mapped_column(Integer)
    call_ordinal: Mapped[int | None] = mapped_column(Integer)
    tool_binding_id: Mapped[UUID | None] = mapped_column(Uuid)
    group_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    prepared_context: Mapped[bytes | None] = mapped_column(LargeBinary)
    definition_snapshot: Mapped[dict] = mapped_column(DOCUMENT)
    input_sha256: Mapped[str] = mapped_column(String(64))
    accepted_attempt_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentAttempt(Base):
    __tablename__ = "agent_attempt"
    __table_args__ = (
        UniqueConstraint("run_id", "id", name="uq_agent_attempt_run_id"),
        UniqueConstraint("step_id", "id", name="uq_agent_attempt_step_id"),
        UniqueConstraint("step_id", "number", name="uq_agent_attempt_number"),
        ForeignKeyConstraint(
            ["run_id", "step_id"],
            ["agent_step.run_id", "agent_step.id"],
            name="fk_agent_attempt_step",
        ),
        CheckConstraint("number > 0", name="ck_agent_attempt_number"),
        CheckConstraint(
            "status IN "
            "('prepared','dispatched','completed','failed','incomplete','canceled','unknown')",
            name="ck_agent_attempt_status",
        ),
        Index(
            "uq_agent_attempt_active",
            "step_id",
            unique=True,
            postgresql_where=text("status::text = ANY (ARRAY['prepared','dispatched']::text[])"),
            sqlite_where=text("status IN ('prepared','dispatched')"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_run.id"))
    step_id: Mapped[UUID] = mapped_column(Uuid)
    number: Mapped[int] = mapped_column(Integer)
    execution_epoch: Mapped[UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(16))
    input_content: Mapped[bytes] = mapped_column(LargeBinary)
    output_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    receipt: Mapped[dict] = mapped_column(DOCUMENT)
    budget_receipt: Mapped[dict] = mapped_column(DOCUMENT)
    delivery_no: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))


class AgentOutbox(Base):
    __tablename__ = "agent_outbox"
    __table_args__ = (
        UniqueConstraint("run_id", "id", name="uq_agent_outbox_run_id"),
        UniqueConstraint("run_id", "operation_id", name="uq_agent_outbox_operation"),
        UniqueConstraint("run_id", "run_version", name="uq_agent_outbox_version"),
        CheckConstraint(
            "(last_stream_id IS NULL) = (delivered_at IS NULL)", name="ck_agent_outbox_delivered"
        ),
        Index(
            "ix_agent_outbox_pending",
            "run_id",
            "run_version",
            postgresql_where=text("delivered_at IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_run.id"))
    run_version: Mapped[int] = mapped_column(BigInteger)
    request_id: Mapped[UUID] = mapped_column(Uuid)
    operation_id: Mapped[UUID] = mapped_column(Uuid)
    operation_sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(DOCUMENT)
    last_stream_id: Mapped[str | None] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentCheckpoint(Base):
    __tablename__ = "agent_checkpoint"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "boundary_outbox_id"],
            ["agent_outbox.run_id", "agent_outbox.id"],
            name="fk_agent_checkpoint_boundary",
        ),
        CheckConstraint("number > 0 AND schema_version > 0", name="ck_agent_checkpoint_version"),
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_run.id"), primary_key=True)
    number: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    boundary_outbox_id: Mapped[UUID] = mapped_column(Uuid)
    position: Mapped[dict] = mapped_column(DOCUMENT)
    completed_refs: Mapped[list] = mapped_column(DOCUMENT)
    message_refs: Mapped[list] = mapped_column(DOCUMENT)
    tool_binding_refs: Mapped[list] = mapped_column(DOCUMENT)
    artifact_refs: Mapped[list] = mapped_column(DOCUMENT)
    plan_state: Mapped[dict] = mapped_column(DOCUMENT)
    verifier_state: Mapped[bytes] = mapped_column(LargeBinary)
    budget_state: Mapped[dict] = mapped_column(DOCUMENT)
    pending_calls: Mapped[list] = mapped_column(DOCUMENT)
    continuation_state: Mapped[bytes | None] = mapped_column(LargeBinary)
    state_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentCommand(Base):
    __tablename__ = "agent_command"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'schedule' AND session_id IS NULL AND run_id IS NULL)"
            " OR (kind <> 'schedule' AND session_id IS NOT NULL)",
            name="ck_agent_command_schedule_scope",
        ),
        UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "actor_user_id",
            "request_id",
            name="uq_agent_command_request",
        ),
        ForeignKeyConstraint(
            ["session_id", "run_id"],
            ["agent_run.session_id", "agent_run.id"],
            name="fk_agent_command_run",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    actor_user_id: Mapped[UUID] = mapped_column(Uuid)
    request_id: Mapped[UUID] = mapped_column(Uuid)
    session_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_session.id"))
    run_id: Mapped[UUID | None] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(String(16))
    request_sha256: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentArtifact(Base):
    __tablename__ = "agent_artifact"
    __table_args__ = (
        UniqueConstraint("run_id", "id", name="uq_agent_artifact_run_id"),
        ForeignKeyConstraint(
            ["run_id", "attempt_id"],
            ["agent_attempt.run_id", "agent_attempt.id"],
            name="fk_agent_artifact_attempt",
        ),
        CheckConstraint(
            "size_bytes = octet_length(content)", name="ck_agent_artifact_bytes"
        ).ddl_if(dialect="postgresql"),
        CheckConstraint("size_bytes > 0 AND size_bytes <= 2097152", name="ck_agent_artifact_size"),
        CheckConstraint(
            "media_type IN ('image/png','text/csv','application/json','text/markdown')",
            name="ck_agent_artifact_type",
        ),
        Index("ix_agent_artifact_run", "run_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_run.id"))
    attempt_id: Mapped[UUID] = mapped_column(Uuid)
    tool_call_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(48))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    scope: Mapped[dict] = mapped_column(DOCUMENT)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentWorkflowDraft(Base):
    __tablename__ = "agent_workflow_draft"
    __table_args__ = (
        UniqueConstraint("run_id", "step_id", name="uq_agent_workflow_draft_step"),
        ForeignKeyConstraint(
            ["run_id", "step_id"],
            ["agent_step.run_id", "agent_step.id"],
            name="fk_agent_workflow_draft_step",
        ),
        CheckConstraint(
            "status IN ('offered','saved','expired')", name="ck_agent_workflow_draft_status"
        ),
        CheckConstraint(
            "(status = 'saved') = (asset_id IS NOT NULL AND saved_at IS NOT NULL)",
            name="ck_agent_workflow_draft_saved",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_run.id"))
    step_id: Mapped[UUID] = mapped_column(Uuid)
    suggested_name: Mapped[str] = mapped_column(Text)
    script_source: Mapped[str] = mapped_column(Text)
    script_sha256: Mapped[str] = mapped_column(String(64))
    definition_version: Mapped[int] = mapped_column(Integer)
    runner_api_version: Mapped[int] = mapped_column(Integer)
    validation_summary: Mapped[dict] = mapped_column(DOCUMENT)
    status: Mapped[str] = mapped_column(String(16))
    asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("workflow_asset.asset_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentWait(Base):
    __tablename__ = "agent_wait"
    __table_args__ = (
        UniqueConstraint("run_id", "id", name="uq_agent_wait_run_id"),
        ForeignKeyConstraint(
            ["run_id", "step_id"], ["agent_step.run_id", "agent_step.id"], name="fk_agent_wait_step"
        ),
        ForeignKeyConstraint(
            ["run_id", "checkpoint_no"],
            ["agent_checkpoint.run_id", "agent_checkpoint.number"],
            name="fk_agent_wait_checkpoint",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "kind IN ('approval','external_event','reconciliation')", name="ck_agent_wait_kind"
        ),
        CheckConstraint(
            "status IN ('pending','approved','rejected','expired','canceled')",
            name="ck_agent_wait_status",
        ),
        CheckConstraint("proposal_revision > 0", name="ck_agent_wait_revision"),
        CheckConstraint(
            "kind = 'reconciliation' OR expires_at > created_at", name="ck_agent_wait_expiry"
        ),
        CheckConstraint(
            "status NOT IN ('approved','rejected') OR (decision_id IS NOT NULL AND "
            "decider_user_id IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_agent_wait_decision",
        ),
        Index(
            "uq_agent_wait_pending",
            "run_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        Index("ix_agent_wait_expiry", "status", "expires_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_run.id"))
    step_id: Mapped[UUID] = mapped_column(Uuid)
    checkpoint_no: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(24))
    proposal_id: Mapped[UUID] = mapped_column(Uuid)
    proposal_revision: Mapped[int] = mapped_column(Integer)
    proposal_sha256: Mapped[str] = mapped_column(String(64))
    public_proposal: Mapped[dict] = mapped_column(DOCUMENT)
    private_proposal: Mapped[bytes] = mapped_column(LargeBinary)
    expectation: Mapped[dict] = mapped_column(DOCUMENT)
    status: Mapped[str] = mapped_column(String(16))
    decision_id: Mapped[UUID | None] = mapped_column(Uuid, unique=True)
    decider_user_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentAction(Base):
    __tablename__ = "agent_action"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "wait_id"],
            ["agent_wait.run_id", "agent_wait.id"],
            name="fk_agent_action_wait",
        ),
        ForeignKeyConstraint(
            ["run_id", "step_id"],
            ["agent_step.run_id", "agent_step.id"],
            name="fk_agent_action_step",
        ),
        CheckConstraint(
            "state IN ('pending','applied','unknown','denied')", name="ck_agent_action_state"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_run.id"))
    wait_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    step_id: Mapped[UUID] = mapped_column(Uuid)
    proposal_sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    receipt: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentDemoEffect(Base):
    __tablename__ = "agent_demo_effect"
    action_id: Mapped[UUID] = mapped_column(ForeignKey("agent_action.id"), primary_key=True)
    campaign_key: Mapped[str] = mapped_column(Text)
    delta_cent: Mapped[int] = mapped_column(BigInteger)
    proposal_sha256: Mapped[str] = mapped_column(String(64))
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModelAttempt(Base):
    __tablename__ = "model_attempt"
    __table_args__ = (
        UniqueConstraint("call_id", "number", name="uq_model_attempt_number"),
        CheckConstraint(
            "number > 0 AND reserved_tokens >= 0 AND reserved_cost_microusd >= 0",
            name="ck_model_attempt_counts",
        ),
        CheckConstraint(
            "status IN "
            "('prepared','dispatched','completed','failed','canceled','incomplete','unknown')",
            name="ck_model_attempt_status",
        ),
        Index("ix_model_attempt_call", "call_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    call_id: Mapped[UUID] = mapped_column(ForeignKey("model_call.call_id"))
    number: Mapped[int] = mapped_column(Integer)
    provider_id: Mapped[UUID] = mapped_column(ForeignKey("model_provider.id"))
    config_version: Mapped[int] = mapped_column(Integer)
    binding_version: Mapped[int] = mapped_column(Integer)
    requested_model: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    origin: Mapped[str] = mapped_column(String(16), default="live")
    request_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    output_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome_unknown: Mapped[bool] = mapped_column(Boolean, default=False)
    cleanup_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    usage: Mapped[dict] = mapped_column(DOCUMENT)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger)
    reserved_cost_microusd: Mapped[int] = mapped_column(BigInteger)
    actual_cost_microusd: Mapped[int | None] = mapped_column(BigInteger)
    upstream_request_id: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)


SCHEDULER_TABLES = frozenset({"agent_run_job", "agent_tenant_schedule"})

RUNTIME_TABLES = frozenset(
    table.name
    for table in Base.metadata.tables.values()
    if (table.name.startswith("agent_") or table.name == "model_attempt")
    and table.name not in SCHEDULER_TABLES
)

from compass_agent.workflows.assets import WorkflowAsset  # noqa: E402,F401

OPEN_GOAL = "status IN ('active','paused','blocked','budget_limited')"


class AgentGoal(Base):
    __tablename__ = "agent_goal"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','paused','blocked','budget_limited','completed','canceled')",
            name="ck_agent_goal_status",
        ),
        CheckConstraint(
            "revision > 0 AND rounds_started >= 0 AND tokens_used >= 0 "
            "AND cost_used_microusd >= 0 AND ledger_revision >= 0 AND last_settled_ordinal >= 0",
            name="ck_agent_goal_counts",
        ),
        Index(
            "uq_agent_goal_open",
            "session_id",
            unique=True,
            postgresql_where=text(
                "status::text = ANY (ARRAY['active','paused','blocked','budget_limited']::text[])"
            ),
            sqlite_where=text(OPEN_GOAL),
        ),
        Index(
            "ix_agent_goal_open_status",
            "status",
            postgresql_where=text(
                "status::text = ANY (ARRAY['active','paused','blocked','budget_limited']::text[])"
            ),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("agent_session.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="active")
    reason_code: Mapped[str | None] = mapped_column(String(80))
    contract: Mapped[dict] = mapped_column(DOCUMENT)
    budget: Mapped[dict] = mapped_column(DOCUMENT)
    runtime_state: Mapped[dict] = mapped_column(DOCUMENT, default=dict)
    rounds_started: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_used_microusd: Mapped[int] = mapped_column(BigInteger, default=0)
    ledger_revision: Mapped[int] = mapped_column(Integer, default=0)
    last_settled_ordinal: Mapped[int] = mapped_column(Integer, default=0)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentGoalEntry(Base):
    __tablename__ = "agent_goal_entry"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('query','effect','conclusion','todo','failure','delivery')",
            name="ck_agent_goal_entry_kind",
        ),
        CheckConstraint(
            "status IN ('done','open','stale','conflict','dropped')",
            name="ck_agent_goal_entry_status",
        ),
    )
    goal_id: Mapped[UUID] = mapped_column(ForeignKey("agent_goal.id"), primary_key=True)
    entry_key: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    private_body: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
