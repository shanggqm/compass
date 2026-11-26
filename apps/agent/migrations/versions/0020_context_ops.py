"""ContextOps projections, encrypted snapshots and security audit (971)."""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE context_decision_summary (
            id UUID NOT NULL,
            tenant_id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            run_id UUID NOT NULL,
            step_id UUID NOT NULL,
            call_id UUID,
            trace_id VARCHAR(64),
            snapshot_ref VARCHAR(64),
            scene_id VARCHAR(64) NOT NULL,
            task VARCHAR(32) NOT NULL,
            phase VARCHAR(32) NOT NULL,
            policy_digest VARCHAR(64) NOT NULL,
            prompt_generation INTEGER NOT NULL,
            semantic_version VARCHAR(64),
            duration_ms INTEGER NOT NULL,
            tokens_candidate INTEGER NOT NULL,
            tokens_selected INTEGER NOT NULL,
            tokens_after_compaction INTEGER NOT NULL,
            token_limit INTEGER NOT NULL,
            cache_hit_tokens INTEGER,
            candidate_count INTEGER NOT NULL,
            selected_count INTEGER NOT NULL,
            omitted_count INTEGER NOT NULL,
            security_blocked_count INTEGER NOT NULL,
            redacted_count INTEGER NOT NULL,
            compaction_applied BOOLEAN NOT NULL,
            status VARCHAR(32) NOT NULL,
            error_code VARCHAR(64),
            source_breakdown_json JSONB NOT NULL,
            funnel_json JSONB NOT NULL,
            decisions_json JSONB NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id)
        );

        CREATE INDEX idx_ctx_summary_policy_scene
            ON context_decision_summary (tenant_id, scene_id, policy_digest, created_at);

        CREATE INDEX idx_ctx_summary_run_step
            ON context_decision_summary (tenant_id, run_id, created_at);

        CREATE INDEX idx_ctx_summary_tenant_created
            ON context_decision_summary (tenant_id, workspace_id, created_at);

        CREATE TABLE context_snapshot (
            snapshot_ref VARCHAR(64) NOT NULL,
            tenant_id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            run_id UUID NOT NULL,
            step_id UUID NOT NULL,
            content_sha256 VARCHAR(64) NOT NULL,
            min_trust_level VARCHAR(16) NOT NULL,
            access_scope_json JSONB NOT NULL,
            data_classification VARCHAR(32) NOT NULL,
            ciphertext BYTEA NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (snapshot_ref)
        );

        CREATE INDEX idx_ctx_snapshot_expires ON context_snapshot (expires_at);

        CREATE INDEX idx_ctx_snapshot_tenant_run
            ON context_snapshot (tenant_id, workspace_id, run_id);

        CREATE TABLE context_security_event (
            id UUID NOT NULL,
            tenant_id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            actor_id UUID,
            run_id UUID,
            step_id UUID,
            trace_id VARCHAR(64),
            event_type VARCHAR(32) NOT NULL,
            action VARCHAR(16) NOT NULL,
            rule_code VARCHAR(64) NOT NULL,
            hit_count INTEGER NOT NULL,
            source_kind VARCHAR(32),
            trust_level VARCHAR(16),
            target_ref VARCHAR(128),
            metadata_json JSONB NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id)
        );

        CREATE INDEX idx_ctx_sec_event_type
            ON context_security_event (tenant_id, event_type, created_at);

        CREATE INDEX idx_ctx_sec_tenant_time
            ON context_security_event (tenant_id, workspace_id, created_at);

        CREATE TABLE context_badcase (
            id UUID NOT NULL,
            tenant_id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            created_by UUID NOT NULL,
            run_id UUID NOT NULL,
            step_id UUID NOT NULL,
            snapshot_ref VARCHAR(64) NOT NULL,
            scene_id VARCHAR(64) NOT NULL,
            root_cause_category VARCHAR(32) NOT NULL,
            title VARCHAR(160) NOT NULL,
            note TEXT,
            policy_digest VARCHAR(64) NOT NULL,
            prompt_generation INTEGER NOT NULL,
            semantic_version VARCHAR(64),
            baseline_metrics_json JSONB NOT NULL,
            status VARCHAR(24) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            PRIMARY KEY (id)
        );

        CREATE INDEX idx_ctx_badcase_tenant_created
            ON context_badcase (tenant_id, workspace_id, created_at);
    """)


def downgrade() -> None:
    op.drop_table("context_badcase")
    op.drop_table("context_security_event")
    op.drop_table("context_snapshot")
    op.drop_table("context_decision_summary")
