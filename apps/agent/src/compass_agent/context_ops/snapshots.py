"""Current authorization and mandatory audit before releasing any decrypted snapshot."""

from datetime import UTC, datetime
from uuid import uuid4

from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from compass_agent import schemas as dto
from compass_agent.context_ops.analytics import aware, require_run, visible_runs
from compass_agent.context_ops.crypto import SnapshotCipher
from compass_agent.context_ops.entities import (
    ContextBadcase,
    ContextDecisionSummary,
    ContextSnapshot,
)
from compass_agent.context_ops.recorder import audit, security_fallback
from compass_agent.context_ops.replay import compare
from compass_agent.context_runtime.source_access import allows
from compass_agent.context_security.redactor import redact
from compass_agent.models.errors import ModelError


class Snapshots:
    def __init__(self, store, context, administrator=False):
        self.store, self.context, self.administrator = store, context, administrator

    def authorized(self, tx, ref):
        snapshot = tx.get(ContextSnapshot, ref)
        if (
            snapshot is None
            or self.context.scope.kind != "tenant"
            or (snapshot.tenant_id, snapshot.workspace_id)
            != (self.context.scope.tenant_id, self.context.scope.workspace_id)
        ):
            raise ModelError("forbidden", 403)
        require_run(tx, self.context, self.administrator, snapshot.run_id)
        if aware(snapshot.expires_at) <= datetime.now(UTC):
            raise ModelError("snapshot_expired_or_revoked", 409)
        scope = snapshot.access_scope_json
        refs = list(scope.get("source_refs", []))
        for access in scope.get("access_refs", []):
            if (access["tenant_id"], access["workspace_id"]) != (
                str(self.context.scope.tenant_id),
                str(self.context.scope.workspace_id),
            ):
                raise ModelError("forbidden", 403)
            try:
                revision = int(access["revision"])
            except (TypeError, ValueError):
                raise ModelError("snapshot_expired_or_revoked", 403) from None
            refs.append(
                {
                    "kind": "metric_dictionary",
                    "tenant": access["tenant_id"],
                    "workspace": access["workspace_id"],
                    "connection": access["binding"],
                    "revision": revision,
                }
            )
        if not allows(self.store, self.context, refs, raise_unavailable=True):
            raise ModelError("snapshot_expired_or_revoked", 403)
        return snapshot

    def denied(self, ref):
        # Only record identifiers owned by this scope; an opaque denial does not reveal existence.
        try:
            with Session(self.store.engine) as tx, tx.begin():
                audit(tx, self.context, "snapshot_denied", "deny", "source_revoked", target_ref=ref)
        except SQLAlchemyError:
            security_fallback(
                [
                    {
                        "event_type": "snapshot_denied",
                        "action": "deny",
                        "rule_code": "source_revoked",
                        "hit_count": 1,
                    }
                ],
                reason="audit_unavailable",
            )

    def read(self, ref):
        try:
            with Session(self.store.engine, expire_on_commit=False) as tx, tx.begin():
                snapshot = self.authorized(tx, ref)
                audit(
                    tx,
                    self.context,
                    "snapshot_viewed",
                    "allow_audit",
                    "snapshot_read",
                    run_id=snapshot.run_id,
                    step_id=snapshot.step_id,
                    target_ref=ref,
                )
                tx.flush()
                # Authentication plus digest verify the immutable payload. Transaction commit
                # must succeed before control reaches the return statement below.
                payload = SnapshotCipher(
                    self.store.source_settings.compass_model_encryption_key
                ).decrypt_json(snapshot.ciphertext, snapshot.content_sha256)
            return dto.ContextSnapshotView(
                snapshot_ref=ref,
                content_sha256=snapshot.content_sha256,
                min_trust_level=snapshot.min_trust_level,
                data_classification=snapshot.data_classification,
                expires_at=snapshot.expires_at,
                payload=payload,
            )
        except ModelError:
            self.denied(ref)
            raise
        except (SQLAlchemyError, ValueError, InvalidToken):
            raise ModelError("context_snapshot_unavailable", 503) from None

    def create_badcase(self, command):
        if not self.administrator:
            raise ModelError("forbidden", 403)
        with Session(self.store.engine, expire_on_commit=False) as tx, tx.begin():
            require_run(tx, self.context, self.administrator, command.run_id)
            summary = tx.scalar(
                select(ContextDecisionSummary).where(
                    ContextDecisionSummary.run_id == command.run_id,
                    ContextDecisionSummary.step_id == command.step_id,
                    ContextDecisionSummary.tenant_id == self.context.scope.tenant_id,
                    ContextDecisionSummary.workspace_id == self.context.scope.workspace_id,
                )
            )
            if summary is None or summary.snapshot_ref is None:
                raise ModelError("snapshot_expired_or_revoked", 409)
            self.authorized(tx, summary.snapshot_ref)
            row = ContextBadcase(
                id=uuid4(),
                tenant_id=self.context.scope.tenant_id,
                workspace_id=self.context.scope.workspace_id,
                created_by=self.context.actor_user_id,
                run_id=command.run_id,
                step_id=command.step_id,
                snapshot_ref=summary.snapshot_ref,
                scene_id=summary.scene_id,
                root_cause_category=command.root_cause_category,
                title=redact(command.title)[0],
                note=redact(command.note)[0] if command.note else None,
                policy_digest=summary.policy_digest,
                prompt_generation=summary.prompt_generation,
                semantic_version=summary.semantic_version,
                baseline_metrics_json={
                    "tokens": summary.tokens_after_compaction,
                    "funnel": summary.funnel_json,
                },
                status="open",
                created_at=datetime.now(UTC),
            )
            tx.add(row)
        return self.view(row)

    @staticmethod
    def view(row, status="open"):
        values = {key: getattr(row, key) for key in dto.ContextBadcaseView.model_fields}
        if status != "open":
            values.update(
                title="快照来源已失效"
                if status == "snapshot_expired_or_revoked"
                else "来源权限暂时无法核验",
                note=None,
            )
        return dto.ContextBadcaseView(**{**values, "status": status})

    def list_badcases(self, *, root_cause_category=None, scene_id=None, limit=50):
        with Session(self.store.engine) as tx:
            runs = visible_runs(self.context, self.administrator).with_only_columns(AgentRun.id)
            query = (
                select(ContextBadcase)
                .where(
                    ContextBadcase.tenant_id == self.context.scope.tenant_id,
                    ContextBadcase.workspace_id == self.context.scope.workspace_id,
                    ContextBadcase.run_id.in_(runs),
                )
                .order_by(ContextBadcase.created_at.desc())
            )
            if root_cause_category:
                query = query.where(ContextBadcase.root_cause_category == root_cause_category)
            if scene_id:
                query = query.where(ContextBadcase.scene_id == scene_id)
            items = []
            for row in tx.scalars(query.limit(limit)):
                status = "open"
                try:
                    self.authorized(tx, row.snapshot_ref)
                except ModelError as error:
                    status = (
                        "authorization_unavailable"
                        if error.status >= 500
                        else "snapshot_expired_or_revoked"
                    )
                items.append(self.view(row, status))
            return dto.ContextBadcases(items=items)

    def replay(self, identifier, override):
        if not self.administrator:
            raise ModelError("forbidden", 403)
        with Session(self.store.engine) as tx:
            row = tx.get(ContextBadcase, identifier)
            if row is None or (row.tenant_id, row.workspace_id) != (
                self.context.scope.tenant_id,
                self.context.scope.workspace_id,
            ):
                raise ModelError("forbidden", 403)
            require_run(tx, self.context, self.administrator, row.run_id)
            ref = row.snapshot_ref
        snapshot = self.read(ref)
        return compare(snapshot.payload, override)


from compass_agent.runtime.entities import AgentRun  # noqa: E402
