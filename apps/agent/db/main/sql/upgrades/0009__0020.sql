-- Generated from Agent Alembic migrations; do not edit.
-- owner=agent/main; feature=971; PostgreSQL 17; from=0009; to=0020
-- Apply through db:apply-sql after provision and ownership/version/checksum checks.
-- Single transaction; additive schema; restore verified backup to a new owned database.
BEGIN;
-- Running upgrade 0009 -> 0010

CREATE TABLE agent_tenant_schedule (
    tenant_id UUID PRIMARY KEY,
    last_claimed_at TIMESTAMPTZ
);
CREATE TABLE agent_run_job (
    run_id UUID PRIMARY KEY REFERENCES agent_run(id),
    tenant_id UUID NOT NULL REFERENCES agent_tenant_schedule(tenant_id),
    status VARCHAR(16) NOT NULL,
    ready_at TIMESTAMPTZ,
    first_claimed_at TIMESTAMPTZ,
    worker_id VARCHAR(64),
    worker_boot_id UUID,
    lease_expires_at TIMESTAMPTZ,
    claim_count INTEGER NOT NULL,
    CONSTRAINT ck_agent_job_status
      CHECK (status IN ('ready','leased','reclaiming','parked','done')),
    CONSTRAINT ck_agent_job_claim_count CHECK (claim_count >= 0),
    CONSTRAINT ck_agent_job_ready CHECK (status != 'ready' OR ready_at IS NOT NULL),
    CONSTRAINT ck_agent_job_lease CHECK (
        (status IN ('leased','reclaiming') AND worker_id IS NOT NULL
         AND worker_boot_id IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status NOT IN ('leased','reclaiming') AND worker_id IS NULL
            AND worker_boot_id IS NULL AND lease_expires_at IS NULL)
    )
);
CREATE INDEX ix_agent_job_ready ON agent_run_job(tenant_id, ready_at, run_id)
    WHERE status = 'ready';
CREATE INDEX ix_agent_job_expiry ON agent_run_job(lease_expires_at) WHERE status = 'leased';
CREATE UNIQUE INDEX uq_agent_job_worker ON agent_run_job(worker_id)
    WHERE status::text = ANY (ARRAY['leased','reclaiming']::text[]);;

UPDATE alembic_version SET version_num='0010' WHERE alembic_version.version_num = '0009';

-- Running upgrade 0010 -> 0011

ALTER TABLE agent_session
    ADD COLUMN title_status VARCHAR(12) NOT NULL DEFAULT 'fallback',
    ADD COLUMN title_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN title_next_at TIMESTAMPTZ,
    ADD COLUMN deleted_at TIMESTAMPTZ,
    ADD CONSTRAINT ck_agent_session_title_status
        CHECK (title_status IN ('fallback','pending','generated','manual')),
    ADD CONSTRAINT ck_agent_session_title_job
        CHECK (title_attempts BETWEEN 0 AND 2
            AND ((title_status = 'pending') = (title_next_at IS NOT NULL))
            AND (deleted_at IS NULL OR title_status != 'pending'));
CREATE INDEX ix_agent_session_title_ready ON agent_session(title_next_at, id)
    WHERE title_status = 'pending';;

UPDATE alembic_version SET version_num='0011' WHERE alembic_version.version_num = '0010';

-- Running upgrade 0011 -> 0012

ALTER TABLE workflow_asset
    ADD COLUMN description VARCHAR(500),
    ADD COLUMN parameters JSONB,
    ADD CONSTRAINT ck_workflow_asset_tool_definition CHECK (
        (entry_contract = 'agent_workflow@1'
            AND description IS NOT NULL AND length(btrim(description)) > 0
            AND parameters IS NOT NULL AND jsonb_typeof(parameters) = 'object')
        OR (entry_contract <> 'agent_workflow@1' AND description IS NULL AND parameters IS NULL)
    );;

UPDATE alembic_version SET version_num='0012' WHERE alembic_version.version_num = '0011';

-- Running upgrade 0012 -> 0013

ALTER TABLE model_provider ADD COLUMN generation_profile JSONB;

UPDATE alembic_version SET version_num='0013' WHERE alembic_version.version_num = '0012';

-- Running upgrade 0013 -> 0014

ALTER TABLE workflow_asset ADD COLUMN deleted_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE workflow_asset DROP CONSTRAINT uq_workflow_asset_scope_script;

CREATE UNIQUE INDEX uq_workflow_asset_scope_script ON workflow_asset
        (tenant_id, workspace_id, owner_user_id, runner_api_version, entry_contract, script_sha256)
        WHERE deleted_at IS NULL;

UPDATE alembic_version SET version_num='0014' WHERE alembic_version.version_num = '0013';

-- Running upgrade 0014 -> 0015

DO $switch$
        BEGIN
            IF NOT pg_try_advisory_xact_lock(985001) THEN
                RAISE EXCEPTION 'Stop the Agent execution owners before migrating to 0015';
            END IF;
        END;
        $switch$;

CREATE TABLE agent_runtime_control (
            key VARCHAR(24) PRIMARY KEY,
            mode VARCHAR(16),
            policy_sha256 VARCHAR(64),
            holder_id UUID,
            generation BIGINT DEFAULT 0 NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE
        );

ALTER TABLE agent_runtime_control ENABLE ROW LEVEL SECURITY;

CREATE POLICY runtime_control_read ON agent_runtime_control FOR SELECT USING (true);

CREATE POLICY runtime_control_write ON agent_runtime_control FOR UPDATE
        USING (key = 'scheduler' OR current_user <> 'compass_agent_scheduler')
        WITH CHECK (key = 'scheduler' OR current_user <> 'compass_agent_scheduler');

INSERT INTO agent_runtime_control (key, mode)
        VALUES ('execution', 'internal'), ('scheduler', NULL);

ALTER TABLE agent_command ALTER COLUMN session_id DROP NOT NULL,
        ADD CONSTRAINT ck_agent_command_schedule_scope CHECK (
            (kind = 'schedule' AND session_id IS NULL AND run_id IS NULL)
            OR (kind <> 'schedule' AND session_id IS NOT NULL)
        );

ALTER TABLE agent_run_job
        ADD COLUMN recovery_token UUID,
        ADD COLUMN recovery_expires_at TIMESTAMP WITH TIME ZONE,
        ADD COLUMN active_since TIMESTAMP WITH TIME ZONE;

ALTER TABLE agent_run
        ADD COLUMN publish_token UUID,
        ADD COLUMN publish_expires_at TIMESTAMP WITH TIME ZONE,
        ADD COLUMN publish_initialized BOOLEAN DEFAULT false NOT NULL,
        ADD COLUMN publish_generation BIGINT DEFAULT 0 NOT NULL;

CREATE TABLE agent_schedule (
            id UUID PRIMARY KEY,
            tenant_id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            owner_user_id UUID NOT NULL,
            name VARCHAR(120) NOT NULL,
            cron VARCHAR(120) NOT NULL,
            timezone VARCHAR(128) NOT NULL,
            input_ciphertext BYTEA NOT NULL,
            status VARCHAR(12) NOT NULL,
            revision BIGINT NOT NULL,
            next_fire_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
            CONSTRAINT ck_agent_schedule_state CHECK (
                status IN ('enabled','disabled','deleted') AND revision > 0
                AND ((status = 'enabled') = (next_fire_at IS NOT NULL))
            )
        );

CREATE INDEX ix_agent_schedule_owner ON agent_schedule (tenant_id, workspace_id, owner_user_id, id);

CREATE INDEX ix_agent_schedule_due ON agent_schedule (next_fire_at, id) WHERE status = 'enabled';

CREATE TABLE agent_schedule_trigger (
            id UUID PRIMARY KEY,
            schedule_id UUID NOT NULL REFERENCES agent_schedule(id),
            schedule_revision BIGINT NOT NULL,
            scheduled_at TIMESTAMP WITH TIME ZONE NOT NULL,
            detected_at TIMESTAMP WITH TIME ZONE NOT NULL,
            coalesced_from_at TIMESTAMP WITH TIME ZONE,
            input_ciphertext BYTEA,
            delivery_status VARCHAR(20) NOT NULL,
            reason_code VARCHAR(80),
            delivery_attempts INTEGER DEFAULT 0 NOT NULL,
            delivery_deadline_at TIMESTAMP WITH TIME ZONE,
            next_attempt_at TIMESTAMP WITH TIME ZONE,
            delivery_token UUID,
            delivery_generation BIGINT,
            delivery_expires_at TIMESTAMP WITH TIME ZONE,
            run_id UUID REFERENCES agent_run(id),
            accepted_at TIMESTAMP WITH TIME ZONE,
            observed_run_status VARCHAR(16),
            observed_run_version BIGINT,
            observed_reason_code VARCHAR(80),
            observed_at TIMESTAMP WITH TIME ZONE,
            finished_at TIMESTAMP WITH TIME ZONE,
            CONSTRAINT uq_agent_trigger_slot UNIQUE (schedule_id, scheduled_at),
            CONSTRAINT uq_agent_trigger_run UNIQUE (run_id),
            CONSTRAINT ck_agent_trigger_state CHECK (
                delivery_status IN ('pending','dispatching','retry_wait','reconcile_pending',
                                    'submitted','delivery_failed','skipped')
                AND delivery_attempts BETWEEN 0 AND 8
                AND ((delivery_status = 'submitted') = (run_id IS NOT NULL))
                AND ((run_id IS NOT NULL) = (accepted_at IS NOT NULL))
            )
        );

CREATE INDEX ix_agent_trigger_history ON agent_schedule_trigger
        (schedule_id, scheduled_at, id);

CREATE INDEX ix_agent_trigger_delivery ON agent_schedule_trigger
        (next_attempt_at, scheduled_at, id)
        WHERE delivery_status::text = ANY (ARRAY['pending','retry_wait','dispatching']::text[]);

CREATE INDEX ix_agent_trigger_reconcile ON agent_schedule_trigger
        (next_attempt_at, scheduled_at, id) WHERE delivery_status = 'reconcile_pending';

CREATE INDEX ix_agent_trigger_sync ON agent_schedule_trigger
        (observed_at, id) WHERE delivery_status = 'submitted' AND finished_at IS NULL;

CREATE VIEW agent_scheduled_run_summary WITH (security_barrier = true) AS
        SELECT run.id, run.version, run.status, run.reason_code, run.finished_at
        FROM agent_run AS run
        JOIN agent_schedule_trigger AS trigger ON trigger.run_id = run.id;

UPDATE alembic_version SET version_num='0015' WHERE alembic_version.version_num = '0014';

-- Running upgrade 0015 -> 0016

CREATE TABLE prompt_file (
        path TEXT NOT NULL,
        portable_key TEXT NOT NULL,
        origin TEXT NOT NULL,
        edit_rev BIGINT NOT NULL,
        max_version BIGINT NOT NULL,
        active_epoch BIGINT NOT NULL,
        draft_body TEXT,
        draft_base_version BIGINT,
        draft_base_epoch BIGINT,
        last_import_body TEXT,
        last_import_hash CHAR(64),
        updated_by UUID NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        PRIMARY KEY (path),
        CONSTRAINT uq_prompt_file_portable UNIQUE (portable_key),
        CONSTRAINT ck_prompt_origin CHECK (origin IN ('bundled','online')),
        CONSTRAINT ck_prompt_counters CHECK
        (edit_rev >= 1 AND max_version >= 0 AND active_epoch >= 0),
        CONSTRAINT ck_prompt_draft CHECK
        ((draft_body IS NULL AND draft_base_version IS NULL AND draft_base_epoch IS NULL)
         OR (draft_body IS NOT NULL AND draft_base_epoch IS NOT NULL)),
        CONSTRAINT ck_prompt_import CHECK ((last_import_body IS NULL) = (last_import_hash IS NULL))
        );

CREATE TABLE prompt_version (
        path TEXT NOT NULL,
        version BIGINT NOT NULL,
        body TEXT NOT NULL,
        byte_count INTEGER NOT NULL,
        sha256 CHAR(64) NOT NULL,
        created_by UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        note TEXT NOT NULL,
        PRIMARY KEY (path, version),
        CONSTRAINT ck_prompt_version CHECK (version > 0 AND byte_count > 0),
        FOREIGN KEY(path) REFERENCES prompt_file (path)
        );

CREATE TABLE prompt_generation (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY,
        parent_id BIGINT,
        manifest JSONB NOT NULL,
        manifest_digest CHAR(64) NOT NULL,
        policy_version TEXT NOT NULL,
        policy_digest CHAR(64) NOT NULL,
        action TEXT NOT NULL,
        snapshot_no BIGINT,
        restored_from_id BIGINT,
        created_by UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        note TEXT NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_prompt_snapshot UNIQUE (snapshot_no),
        CONSTRAINT ck_prompt_snapshot CHECK
        ((action = 'file' AND snapshot_no IS NULL)
         OR (action IN ('all','restore') AND snapshot_no > 0 AND snapshot_no IS NOT NULL)),
        CONSTRAINT ck_prompt_restore CHECK ((action = 'restore') = (restored_from_id IS NOT NULL)),
        FOREIGN KEY(parent_id) REFERENCES prompt_generation (id),
        FOREIGN KEY(restored_from_id) REFERENCES prompt_generation (id)
        );

CREATE TABLE prompt_head (
        id SMALLINT NOT NULL,
        generation BIGINT,
        last_snapshot_no BIGINT NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT ck_prompt_singleton CHECK (id = 1),
        FOREIGN KEY(generation) REFERENCES prompt_generation (id)
        );

CREATE TABLE prompt_command (
        actor_id UUID NOT NULL,
        request_id UUID NOT NULL,
        action TEXT NOT NULL,
        input_hash CHAR(64) NOT NULL,
        receipt JSONB NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        PRIMARY KEY (actor_id, request_id)
        );

INSERT INTO prompt_head (id,generation,last_snapshot_no) VALUES (1,NULL,0);

UPDATE agent_runtime_control SET policy_sha256 = NULL WHERE key = 'execution';

ALTER TABLE agent_step ADD COLUMN prepared_context BYTEA;

ALTER TABLE agent_message ADD COLUMN source_refs JSONB;

ALTER TABLE agent_artifact DROP CONSTRAINT ck_agent_artifact_type;

ALTER TABLE agent_artifact ADD CONSTRAINT ck_agent_artifact_type CHECK
        (media_type IN ('image/png','text/csv','application/json','text/markdown'));

UPDATE alembic_version SET version_num='0016' WHERE alembic_version.version_num = '0015';

-- Running upgrade 0016 -> 0017

CREATE TABLE semantic_run_binding (
          run_id uuid PRIMARY KEY REFERENCES agent_run(id), tenant_id uuid NOT NULL,
          workspace_id uuid NOT NULL, catalog_kind varchar(20) NOT NULL,
          manifest_hash varchar(64) NOT NULL, partition_releases jsonb NOT NULL,
          bound_at timestamptz NOT NULL);

CREATE TABLE semantic_metric_offer (
          offer_id uuid PRIMARY KEY, session_id uuid NOT NULL REFERENCES agent_session(id),
          run_id uuid NOT NULL REFERENCES agent_run(id), owner_id uuid NOT NULL,
          kind varchar(24) NOT NULL, status varchar(24) NOT NULL, private_payload bytea NOT NULL,
          expires_at timestamptz NOT NULL);

CREATE INDEX ix_semantic_metric_offer_session_id ON semantic_metric_offer(session_id);

UPDATE agent_runtime_control SET policy_sha256 = NULL WHERE key = 'execution';

UPDATE alembic_version SET version_num='0017' WHERE alembic_version.version_num = '0016';

-- Running upgrade 0017 -> 0018

LOCK TABLE semantic_run_binding IN SHARE MODE;
        DO $guard$
        BEGIN
            IF EXISTS (
                SELECT FROM semantic_run_binding b JOIN agent_run r ON r.id=b.run_id
                WHERE (r.config_snapshot #> '{contextRuntime,registry,manifest}')
                    IS DISTINCT FROM to_jsonb(b.manifest_hash)
                   OR (r.config_snapshot #> '{contextRuntime,registry,pins}')
                    IS DISTINCT FROM b.partition_releases
            ) THEN
                RAISE EXCEPTION 'Registry binding differs from Run snapshot';
            END IF;
        END
        $guard$;

DROP TABLE semantic_run_binding;

UPDATE alembic_version SET version_num='0018' WHERE alembic_version.version_num = '0017';

-- Running upgrade 0018 -> 0019

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
        );;

UPDATE alembic_version SET version_num='0019' WHERE alembic_version.version_num = '0018';

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
COMMENT ON TABLE alembic_version IS 'compass:agent/main;sources={"0001": "9513f1fcaa4a4f65d43e3942745c4e794dab30b10425d31be26c427dc379d1ac", "0002": "98c56506d80e00c70a9ff542ea252d0e51fbf6aae310e61b9e21ae0dd2b72202", "0003": "fca5d24e5ec8a8d83e3e2195a42c0842e2768111b46e3046897e9e9bde199c08", "0004": "0de305dc970161bc065373d077f441277e2d2094e21e819e3f76692a74e646cb", "0005": "468a7d8025c6f10ba13c8000ced21061df5c7801f42a1d56a839471efb335f1b", "0006": "766151447cb24d33194e792156d8844eb6a20916201547089fe19c6f14f10fad", "0007": "630ab76dbf04b8b55d21b35c7befea8d4a5b7b1d191aaa721851804d356364cd", "0008": "05accae62e4747b875a9ac58757c74d068b6440ec3235c0b56059bbadf8d8b9a", "0009": "8d13979b8334ec228e9866444ef5be407f918e639b449e05765b1e1824a73528", "0010": "1ff54134ba93bdcdd35ef723c11a4bb788dc8938f1a7820681217597bf757e76", "0011": "86d1068cfaa7d78c7fde00b91f9d1308e904591b464f335507af8bfc1b5e7950", "0012": "5260c371a96a73006a9ef3cce698b7912aa9bf0f753e67d87298511ee1b8c302", "0013": "fb388e9deb8f6f9311596742b7d0b45e838fb3642f1433429b9aeffd4dceb7ac", "0014": "26a9d717852a4670de56ec5f8fb87a894b0952831f7b5dc5cfaa8d37046e4059", "0015": "74ebf595a7953b7cb74b6d10017e0cbeea7b757761f79d73fdf1952e76fe0d0b", "0016": "86df24acad54e6d13460aff8bcf98cb62be4b63d3e88e36051e385851378d63b", "0017": "104e4ef6551e47f67eb1f87bd7fd8aa5fab5b9c2efa5a248360b550912083812", "0018": "b816e6380cc49497c28bb5aff9ff07e7bf0fcbbf30fe8df81c50178d3f7c8193", "0019": "b86bda24c01f2bd01009d15217f8254c0d9191acfbbda5d6fb792df2250e9f6f", "0020": "d85a29bb7ce0869f55ed36d338a2442460aaa3606dd06f15473c3e9f608eef43"}';
COMMIT;
