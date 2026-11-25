"""Session goals and encrypted cross-round progress (983)."""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agent_goal (
            id UUID PRIMARY KEY,
            session_id UUID NOT NULL REFERENCES agent_session(id),
            revision INTEGER NOT NULL,
            status VARCHAR(20) NOT NULL,
            reason_code VARCHAR(80),
            contract JSONB NOT NULL,
            budget JSONB NOT NULL,
            runtime_state JSONB NOT NULL,
            rounds_started INTEGER NOT NULL,
            tokens_used BIGINT NOT NULL,
            cost_used_microusd BIGINT NOT NULL,
            ledger_revision INTEGER NOT NULL,
            last_settled_ordinal INTEGER NOT NULL,
            retry_after TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            ended_at TIMESTAMPTZ,
            CONSTRAINT ck_agent_goal_status CHECK
                (status IN ('active','paused','blocked','budget_limited','completed','canceled')),
            CONSTRAINT ck_agent_goal_counts CHECK
                (revision > 0 AND rounds_started >= 0 AND tokens_used >= 0
                 AND cost_used_microusd >= 0 AND ledger_revision >= 0
                 AND last_settled_ordinal >= 0)
        );
        CREATE UNIQUE INDEX uq_agent_goal_open ON agent_goal(session_id)
            WHERE status::text = ANY (ARRAY['active','paused','blocked','budget_limited']::text[]);
        CREATE INDEX ix_agent_goal_open_status ON agent_goal(status)
            WHERE status::text = ANY (ARRAY['active','paused','blocked','budget_limited']::text[]);
        CREATE TABLE agent_goal_entry (
            goal_id UUID NOT NULL REFERENCES agent_goal(id),
            entry_key TEXT NOT NULL,
            kind VARCHAR(16) NOT NULL,
            status VARCHAR(16) NOT NULL,
            private_body BYTEA NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (goal_id, entry_key),
            CONSTRAINT ck_agent_goal_entry_kind CHECK
                (kind IN ('query','effect','conclusion','todo','failure','delivery')),
            CONSTRAINT ck_agent_goal_entry_status CHECK
                (status IN ('done','open','stale','conflict','dropped'))
        );
    """)


def downgrade() -> None:
    raise RuntimeError("Restore the verified 0018 backup to a separate database.")
