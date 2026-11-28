-- Generated from Agent Alembic migrations; do not edit.
-- owner=agent/main; feature=970; PostgreSQL 17; from=0019; to=0021
-- Apply through db:apply-sql after provision and ownership/version/checksum checks.
-- Single transaction; additive schema; restore verified backup to a new owned database.
BEGIN;
-- Running upgrade 0019 -> 0020

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
            ON context_badcase (tenant_id, workspace_id, created_at);;

UPDATE alembic_version SET version_num='0020' WHERE alembic_version.version_num = '0019';

-- Running upgrade 0020 -> 0021

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
        CREATE INDEX ix_sandbox_execution_active ON sandbox_execution(allocation_id,state,deadline);;

UPDATE alembic_version SET version_num='0021' WHERE alembic_version.version_num = '0020';
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON demo_daily_sales TO compass_agent_runtime;
GRANT SELECT ON alembic_version TO compass_agent_runtime;
GRANT SELECT, INSERT, UPDATE ON model_provider, model_call TO compass_agent_runtime;
GRANT SELECT, INSERT ON model_change TO compass_agent_runtime;
GRANT SELECT, INSERT, UPDATE ON data_connection TO compass_agent_runtime;
GRANT SELECT, INSERT ON workflow_asset TO compass_agent_runtime;
GRANT UPDATE (name, status, metadata_revision, updated_at, deleted_at) ON workflow_asset TO compass_agent_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON mcp_connection TO compass_agent_runtime;
GRANT SELECT, INSERT, UPDATE ON sandbox_allocation TO compass_agent_runtime;
GRANT SELECT, INSERT, UPDATE ON sandbox_execution TO compass_agent_runtime;
GRANT SELECT, INSERT, UPDATE ON agent_action, agent_artifact, agent_attempt, agent_checkpoint, agent_command, agent_demo_effect, agent_message, agent_outbox, agent_run, agent_session, agent_step, agent_tool_binding, agent_wait, agent_workflow_draft, model_attempt TO compass_agent_runtime;
GRANT SELECT, INSERT, UPDATE ON agent_run_job, agent_tenant_schedule TO compass_agent_runtime;
GRANT SELECT, INSERT, UPDATE ON agent_schedule, agent_schedule_trigger TO compass_agent_runtime;
GRANT SELECT, UPDATE (policy_sha256, holder_id) ON agent_runtime_control TO compass_agent_runtime;
GRANT SELECT ON alembic_version, agent_runtime_control, agent_schedule,
    agent_schedule_trigger, agent_scheduled_run_summary TO compass_agent_scheduler;
GRANT UPDATE (holder_id, generation, expires_at) ON agent_runtime_control TO compass_agent_scheduler;
GRANT UPDATE (next_fire_at) ON agent_schedule TO compass_agent_scheduler;
GRANT INSERT (id, schedule_id, schedule_revision, scheduled_at, detected_at, coalesced_from_at,
    input_ciphertext, delivery_status, reason_code, delivery_deadline_at, next_attempt_at),
    UPDATE (delivery_status, reason_code, delivery_attempts, next_attempt_at, input_ciphertext,
    delivery_token, delivery_generation, delivery_expires_at, observed_run_status,
    observed_run_version, observed_reason_code, observed_at, finished_at)
    ON agent_schedule_trigger TO compass_agent_scheduler;
GRANT SELECT, INSERT ON prompt_file, prompt_version, prompt_generation, prompt_command TO compass_agent_runtime;
GRANT UPDATE (origin,edit_rev,max_version,active_epoch,draft_body,draft_base_version,
    draft_base_epoch,last_import_body,last_import_hash,updated_by,updated_at)
    ON prompt_file TO compass_agent_runtime;
GRANT SELECT, UPDATE (generation,last_snapshot_no) ON prompt_head TO compass_agent_runtime;
GRANT USAGE ON SEQUENCE prompt_generation_id_seq TO compass_agent_runtime;
GRANT SELECT, INSERT ON semantic_metric_offer TO compass_agent_runtime;
GRANT UPDATE(status,private_payload) ON semantic_metric_offer TO compass_agent_runtime;
GRANT SELECT, INSERT, UPDATE ON agent_goal, agent_goal_entry TO compass_agent_runtime;
GRANT SELECT, INSERT ON context_badcase, context_decision_summary, context_security_event, context_snapshot TO compass_agent_runtime;
GRANT SELECT ON context_snapshot, context_badcase TO compass_agent_scheduler;
GRANT DELETE ON context_snapshot TO compass_agent_scheduler;
COMMENT ON TABLE alembic_version IS 'compass:agent/main;sources={"0001": "9513f1fcaa4a4f65d43e3942745c4e794dab30b10425d31be26c427dc379d1ac", "0002": "98c56506d80e00c70a9ff542ea252d0e51fbf6aae310e61b9e21ae0dd2b72202", "0003": "fca5d24e5ec8a8d83e3e2195a42c0842e2768111b46e3046897e9e9bde199c08", "0004": "0de305dc970161bc065373d077f441277e2d2094e21e819e3f76692a74e646cb", "0005": "468a7d8025c6f10ba13c8000ced21061df5c7801f42a1d56a839471efb335f1b", "0006": "766151447cb24d33194e792156d8844eb6a20916201547089fe19c6f14f10fad", "0007": "630ab76dbf04b8b55d21b35c7befea8d4a5b7b1d191aaa721851804d356364cd", "0008": "05accae62e4747b875a9ac58757c74d068b6440ec3235c0b56059bbadf8d8b9a", "0009": "8d13979b8334ec228e9866444ef5be407f918e639b449e05765b1e1824a73528", "0010": "1ff54134ba93bdcdd35ef723c11a4bb788dc8938f1a7820681217597bf757e76", "0011": "86d1068cfaa7d78c7fde00b91f9d1308e904591b464f335507af8bfc1b5e7950", "0012": "5260c371a96a73006a9ef3cce698b7912aa9bf0f753e67d87298511ee1b8c302", "0013": "fb388e9deb8f6f9311596742b7d0b45e838fb3642f1433429b9aeffd4dceb7ac", "0014": "26a9d717852a4670de56ec5f8fb87a894b0952831f7b5dc5cfaa8d37046e4059", "0015": "74ebf595a7953b7cb74b6d10017e0cbeea7b757761f79d73fdf1952e76fe0d0b", "0016": "86df24acad54e6d13460aff8bcf98cb62be4b63d3e88e36051e385851378d63b", "0017": "104e4ef6551e47f67eb1f87bd7fd8aa5fab5b9c2efa5a248360b550912083812", "0018": "b816e6380cc49497c28bb5aff9ff07e7bf0fcbbf30fe8df81c50178d3f7c8193", "0019": "b86bda24c01f2bd01009d15217f8254c0d9191acfbbda5d6fb792df2250e9f6f", "0020": "d85a29bb7ce0869f55ed36d338a2442460aaa3606dd06f15473c3e9f608eef43", "0021": "cd350c7cd0514715720db5018c375ddb61fe981f748408c46bed36c6019e4172"}';
COMMIT;
