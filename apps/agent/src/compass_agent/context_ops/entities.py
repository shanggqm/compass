"""Agent/main read models. Source text belongs exclusively in encrypted snapshots."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, LargeBinary, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from compass_agent.database import Base

DOCUMENT = JSON().with_variant(JSONB, "postgresql")


class ContextDecisionSummary(Base):
    __tablename__ = "context_decision_summary"
    __table_args__ = (
        Index("idx_ctx_summary_tenant_created", "tenant_id", "workspace_id", "created_at"),
        Index("idx_ctx_summary_run_step", "tenant_id", "run_id", "created_at"),
        Index(
            "idx_ctx_summary_policy_scene", "tenant_id", "scene_id", "policy_digest", "created_at"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    run_id: Mapped[UUID] = mapped_column(Uuid)
    step_id: Mapped[UUID] = mapped_column(Uuid)
    call_id: Mapped[UUID | None] = mapped_column(Uuid)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    snapshot_ref: Mapped[str | None] = mapped_column(String(64))
    scene_id: Mapped[str] = mapped_column(String(64))
    task: Mapped[str] = mapped_column(String(32))
    phase: Mapped[str] = mapped_column(String(32))
    policy_digest: Mapped[str] = mapped_column(String(64))
    prompt_generation: Mapped[int] = mapped_column(Integer)
    semantic_version: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int] = mapped_column(Integer)
    tokens_candidate: Mapped[int] = mapped_column(Integer)
    tokens_selected: Mapped[int] = mapped_column(Integer)
    tokens_after_compaction: Mapped[int] = mapped_column(Integer)
    token_limit: Mapped[int] = mapped_column(Integer)
    cache_hit_tokens: Mapped[int | None] = mapped_column(Integer)
    candidate_count: Mapped[int] = mapped_column(Integer)
    selected_count: Mapped[int] = mapped_column(Integer)
    omitted_count: Mapped[int] = mapped_column(Integer)
    security_blocked_count: Mapped[int] = mapped_column(Integer)
    redacted_count: Mapped[int] = mapped_column(Integer)
    compaction_applied: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    source_breakdown_json: Mapped[list] = mapped_column(DOCUMENT)
    funnel_json: Mapped[list] = mapped_column(DOCUMENT)
    decisions_json: Mapped[list] = mapped_column(DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ContextSnapshot(Base):
    __tablename__ = "context_snapshot"
    __table_args__ = (
        Index("idx_ctx_snapshot_tenant_run", "tenant_id", "workspace_id", "run_id"),
        Index("idx_ctx_snapshot_expires", "expires_at"),
    )
    snapshot_ref: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    run_id: Mapped[UUID] = mapped_column(Uuid)
    step_id: Mapped[UUID] = mapped_column(Uuid)
    content_sha256: Mapped[str] = mapped_column(String(64))
    min_trust_level: Mapped[str] = mapped_column(String(16))
    access_scope_json: Mapped[dict] = mapped_column(DOCUMENT)
    data_classification: Mapped[str] = mapped_column(String(32))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ContextSecurityEvent(Base):
    __tablename__ = "context_security_event"
    __table_args__ = (
        Index("idx_ctx_sec_tenant_time", "tenant_id", "workspace_id", "created_at"),
        Index("idx_ctx_sec_event_type", "tenant_id", "event_type", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid)
    run_id: Mapped[UUID | None] = mapped_column(Uuid)
    step_id: Mapped[UUID | None] = mapped_column(Uuid)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(16))
    rule_code: Mapped[str] = mapped_column(String(64))
    hit_count: Mapped[int] = mapped_column(Integer)
    source_kind: Mapped[str | None] = mapped_column(String(32))
    trust_level: Mapped[str | None] = mapped_column(String(16))
    target_ref: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict] = mapped_column(DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ContextBadcase(Base):
    __tablename__ = "context_badcase"
    __table_args__ = (
        Index("idx_ctx_badcase_tenant_created", "tenant_id", "workspace_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    workspace_id: Mapped[UUID] = mapped_column(Uuid)
    created_by: Mapped[UUID] = mapped_column(Uuid)
    run_id: Mapped[UUID] = mapped_column(Uuid)
    step_id: Mapped[UUID] = mapped_column(Uuid)
    snapshot_ref: Mapped[str] = mapped_column(String(64))
    scene_id: Mapped[str] = mapped_column(String(64))
    root_cause_category: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(160))
    note: Mapped[str | None] = mapped_column(Text)
    policy_digest: Mapped[str] = mapped_column(String(64))
    prompt_generation: Mapped[int] = mapped_column(Integer)
    semantic_version: Mapped[str | None] = mapped_column(String(64))
    baseline_metrics_json: Mapped[dict] = mapped_column(DOCUMENT)
    status: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


CONTEXTOPS_TABLES = {
    ContextDecisionSummary.__tablename__,
    ContextSnapshot.__tablename__,
    ContextSecurityEvent.__tablename__,
    ContextBadcase.__tablename__,
}
