"""Owner-scoped reusable sandbox and independently recoverable command leases."""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE sandbox_allocation ADD COLUMN shared BOOLEAN NOT NULL DEFAULT false;
        ALTER TABLE sandbox_allocation ADD COLUMN idle_until TIMESTAMPTZ;
        ALTER TABLE sandbox_allocation ADD COLUMN absent_since TIMESTAMPTZ;
        CREATE UNIQUE INDEX uq_sandbox_shared_owner
            ON sandbox_allocation (tenant_id,workspace_id,actor_user_id)
            WHERE shared AND state <> 'released';
        CREATE TABLE sandbox_execution (
            id UUID PRIMARY KEY,
            allocation_id UUID NOT NULL REFERENCES sandbox_allocation(id),
            run_id UUID NOT NULL,
            step_id UUID NOT NULL,
            call_id VARCHAR(128) NOT NULL,
            request_digest VARCHAR(64) NOT NULL,
            command_id VARCHAR(128),
            state VARCHAR(16) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            deadline TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_sandbox_execution_call UNIQUE(run_id,step_id,call_id),
            CONSTRAINT ck_sandbox_execution_state
                CHECK(state IN ('preparing','running','unknown','finished'))
        );
        CREATE INDEX ix_sandbox_execution_active ON sandbox_execution(allocation_id,state,deadline);
    """)


def downgrade() -> None:
    raise RuntimeError("Restore the pre-upgrade backup into a new database; retain sandbox history")
