"""Versioned human decisions and generic execution receipts."""

from datetime import timedelta
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from compass_agent.models.errors import ModelError
from compass_agent.runtime.entities import (
    AgentAction,
    AgentStep,
    AgentWait,
)
from compass_agent.runtime.private_state import digest
from compass_agent.runtime.store import now, settled_nodes
from compass_agent.schemas import PublicApproval, WaitView


def action_id(wait):
    if wait.kind == "reconciliation" and wait.expectation.get("actionId"):
        return UUID(wait.expectation["actionId"])
    return uuid5(wait.id, f"action:{wait.proposal_revision}")


def approval_event(wait, *, reason=None):
    value = PublicApproval(
        waitId=str(wait.id),
        proposalId=str(wait.proposal_id),
        proposalRevision=wait.proposal_revision,
        proposalDigest=wait.proposal_sha256,
        actionId=str(action_id(wait)),
        kind=wait.kind,
        status=wait.status,
        summary=wait.public_proposal["summary"],
        goal=wait.public_proposal.get("goal"),
        expiresAt=wait.expires_at.isoformat() if wait.expires_at else None,
        decisionId=str(wait.decision_id) if wait.decision_id else None,
        reasonCode=reason,
    )
    return {
        "type": "view.upsert",
        "payload": {
            "objectId": str(wait.id),
            "revision": 1 if wait.status == "pending" else 2,
            "kind": "approval",
            "value": value.model_dump(mode="json", by_alias=True),
        },
    }


class HumanDecisions:
    def __init__(self, store):
        self.store = store

    def wait(self, run_id, epoch, result, private_proposal=None):
        with self.store.transaction() as tx:
            session, run = self.store.lock(tx, run_id)
            clock = tx.scalar(select(func.clock_timestamp()))
            self.store.require_epoch(run, epoch)
            if run.status == "cancelling":
                raise ModelError("run_stopped", 409)
            proposal = result.proposal.model_dump(mode="json", by_alias=True)
            node_key = f"{result.node_key}:{result.proposal.proposal_id}"
            step = tx.scalar(
                select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.node_key == node_key)
            )
            if step is None:
                step = AgentStep(
                    id=uuid5(run.id, node_key),
                    run_id=run.id,
                    node_key=node_key,
                    kind="control",
                    ordinal=(
                        tx.scalar(
                            select(func.max(AgentStep.ordinal)).where(AgentStep.run_id == run.id)
                        )
                        or 0
                    )
                    + 1,
                    status="waiting",
                    revision=1,
                    definition_snapshot={"continuation": result.continuation},
                    input_sha256=digest(proposal),
                    created_at=now(),
                )
                tx.add(step)
                tx.flush()
            if run.checkpoint_no is None:
                raise ModelError("checkpoint_missing", 409)
            wait = AgentWait(
                id=UUID(result.wait_id),
                run_id=run.id,
                step_id=step.id,
                checkpoint_no=run.checkpoint_no,
                kind="approval",
                proposal_id=UUID(result.proposal.proposal_id),
                proposal_revision=1,
                proposal_sha256=digest(proposal),
                public_proposal=proposal,
                private_proposal=self.store.private.seal(
                    run.id,
                    "proposal",
                    private_proposal or proposal,
                ),
                expectation={
                    "continuation": result.continuation,
                    "owner": str(session.owner_user_id),
                },
                status="pending",
                created_at=clock,
                expires_at=clock + timedelta(minutes=15),
            )
            tx.add(wait)
            run.status, run.execution_epoch, run.cleanup_status = "waiting", None, "confirmed"
            self.store.outbox(tx, session, run, [approval_event(wait)])

    def owner_decision(self, wait):
        if wait.kind != "approval" or not wait.private_proposal:
            return None
        snapshot = self.store.private.open(wait.run_id, "proposal", wait.private_proposal)
        kind = snapshot.get("kind")
        return (
            kind
            if kind
            in {
                "schedule_owner",
                "semantic_choice",
                "semantic_result_choice",
                "semantic_draft",
                "semantic_revision",
                "semantic_submit",
            }
            else None
        )

    def is_schedule_approval(self, wait):
        return self.owner_decision(wait) == "schedule_owner"

    def assert_confirmation_scope(self, session, wait, context):
        kind = self.owner_decision(wait)
        if kind and (session.owner_user_id != context.actor_user_id or session.deleted_at):
            raise ModelError("not_found", 404)
        return kind

    def authorization_kind(self, run_id, wait_id, context):
        with self.store.transaction() as tx:
            session, run = self.store.lock(tx, run_id)
            if context.scope.kind != "tenant" or (session.tenant_id, session.workspace_id) != (
                context.scope.tenant_id,
                context.scope.workspace_id,
            ):
                raise ModelError("not_found", 404)
            wait = tx.get(AgentWait, wait_id)
            if wait is None or wait.run_id != run.id:
                raise ModelError("not_found", 404)
            return self.assert_confirmation_scope(session, wait, context)

    def get(self, run_id, wait_id, context, *, may_approve=False):
        with self.store.transaction() as tx:
            session, run = self.store.lock(tx, run_id, None if may_approve else context)
            if context.scope.kind != "tenant" or (session.tenant_id, session.workspace_id) != (
                context.scope.tenant_id,
                context.scope.workspace_id,
            ):
                raise ModelError("not_found", 404)
            wait = tx.get(AgentWait, wait_id)
            if wait is None or wait.run_id != run_id:
                raise ModelError("not_found", 404)
            self.assert_confirmation_scope(session, wait, context)
            actions = []
            if wait.status == "pending" and (
                wait.expires_at is None
                or wait.expires_at > tx.scalar(select(func.clock_timestamp()))
            ):
                if wait.kind == "approval":
                    if may_approve:
                        actions.append("approve")
                    if session.owner_user_id == context.actor_user_id:
                        actions.append("reject")
                elif (
                    wait.kind == "reconciliation" and session.owner_user_id == context.actor_user_id
                ):
                    actions.append("reconcile")
            extra = {}
            if wait.status == "pending" and self.owner_decision(wait):
                from compass_agent.semantic.decisions import card

                private = self.store.private.open(run.id, "proposal", wait.private_proposal)
                extra = card(self.store, run, context, private)
            return WaitView.model_validate(
                {
                    **approval_event(wait)["payload"]["value"],
                    "runVersion": run.version,
                    "allowedActions": actions,
                    **extra,
                }
            )

    def close_wait(self, tx, session, run, wait, status, reason):
        wait.status, wait.decided_at = status, now()
        step = tx.get(AgentStep, wait.step_id)
        step.status, step.finished_at = "canceled", now()
        run.status, run.finished_at, run.execution_epoch = "completed", now(), None
        run.reason_code, run.cleanup_status = reason, "confirmed"
        from compass_agent.runtime.entities import AgentMessage
        from compass_agent.runtime.store import message_snapshot

        assistant = tx.scalar(
            select(AgentMessage).where(
                AgentMessage.run_id == run.id,
                AgentMessage.channel == "conversation",
                AgentMessage.role == "assistant",
            )
        )
        message = message_snapshot(assistant)
        message["revision"] += 1
        message["status"] = "completed"
        message["content"] = {
            **message["content"],
            "text": wait.public_proposal["summary"]
            + ("\n\n已拒绝本次动作。" if status == "rejected" else "\n\n确认已过期，动作未执行。"),
        }
        self.store.outbox(
            tx,
            session,
            run,
            [
                approval_event(wait, reason=reason),
                {"type": "message.upsert", "payload": message},
                *settled_nodes(self.store, tx, run, "canceled"),
            ],
        )

    def decide(self, run_id, command, context, *, may_approve, boot_id=None):
        if command.decision == "approve" and not may_approve:
            raise ModelError("forbidden", 403)
        payload = {
            "kind": "decision",
            "runId": str(run_id),
            **command.model_dump(mode="json", by_alias=True),
        }
        with self.store.transaction(admission=True) as tx:
            old = self.store.command(tx, context, command.request_id, payload)
            session, run = self.store.lock(tx, run_id, None if may_approve else context)
            if context.scope.kind != "tenant" or (session.tenant_id, session.workspace_id) != (
                context.scope.tenant_id,
                context.scope.workspace_id,
            ):
                raise ModelError("not_found", 404)
            if old:
                return old.response
            wait = tx.scalar(
                select(AgentWait)
                .where(AgentWait.id == command.wait_id, AgentWait.run_id == run_id)
                .with_for_update()
            )
            if wait is None:
                raise ModelError("not_found", 404)
            decision_kind = self.assert_confirmation_scope(session, wait, context)
            if command.custom_definition is not None and decision_kind != "semantic_choice":
                raise ModelError("invalid_parameters", 422)
            if (wait.proposal_id, wait.proposal_revision, wait.proposal_sha256) != (
                command.proposal_id,
                command.proposal_revision,
                command.proposal_digest,
            ):
                raise ModelError("proposal_conflict", 409)
            if wait.decision_id == command.decision_id:
                if wait.decider_user_id != context.actor_user_id or wait.status != (
                    "approved" if command.decision == "approve" else "rejected"
                ):
                    raise ModelError("decision_conflict", 409)
                return wait.expectation.get("decisionReceipt") or {
                    "runId": str(run.id),
                    "sessionId": str(session.id),
                    "version": run.version,
                    "status": run.status,
                    "code": None,
                }
            if run.version != command.expected_version:
                raise ModelError("version_conflict", 409)
            if run.status != "waiting" or wait.status != "pending" or wait.kind != "approval":
                raise ModelError("decision_conflict", 409)
            clock = tx.scalar(select(func.clock_timestamp()))
            expired = clock >= wait.expires_at
            if expired:
                self.close_wait(tx, session, run, wait, "expired", "approval_expired")
            elif command.decision == "reject":
                if wait.private_proposal:
                    from compass_agent.semantic.results import dismiss_proposal

                    dismiss_proposal(
                        tx,
                        self.store,
                        run,
                        self.store.private.open(run.id, "proposal", wait.private_proposal),
                    )
                wait.decision_id, wait.decider_user_id = command.decision_id, context.actor_user_id
                self.close_wait(tx, session, run, wait, "rejected", "approval_rejected")
            else:
                selection = None
                if wait.private_proposal:
                    from compass_agent.semantic.decisions import apply

                    private = self.store.private.open(run.id, "proposal", wait.private_proposal)
                    if (
                        private.get("kind", "").startswith("semantic_")
                        or command.choice_id
                        or command.custom_definition
                        or command.draft_edits
                    ):
                        from compass_agent.semantic.clarification import retain_selection

                        apply(self.store, run, context, private, command)
                        selection = retain_selection(tx, self.store, session, run, private)
                        wait.private_proposal = self.store.private.seal(run.id, "proposal", private)
                wait.status, wait.decision_id, wait.decider_user_id, wait.decided_at = (
                    "approved",
                    command.decision_id,
                    context.actor_user_id,
                    now(),
                )
                gate = tx.get(AgentStep, wait.step_id)
                gate.status, gate.finished_at = "completed", now()
                gate.revision += 1
                tx.add(
                    AgentAction(
                        id=action_id(wait),
                        run_id=run.id,
                        wait_id=wait.id,
                        step_id=wait.step_id,
                        proposal_sha256=wait.proposal_sha256,
                        state="applied" if selection else "pending",
                        receipt=selection,
                        created_at=now(),
                        updated_at=now(),
                    )
                )
                run.status, run.execution_epoch, run.recovery_status = "running", uuid4(), "ready"
                run.owner_boot_id = boot_id or run.owner_boot_id
                run.reason_code = "approval_accepted"
                events = [approval_event(wait)]
                if self.store.scheduler:
                    events.append(self.store.scheduler.enqueue(tx, session, run))
                self.store.outbox(tx, session, run, events)
            receipt = {
                "runId": str(run.id),
                "sessionId": str(session.id),
                "version": run.version,
                "status": run.status,
                "code": "approval_expired" if expired else None,
            }
            wait.expectation = {**wait.expectation, "decisionReceipt": receipt}
            self.store.record_command(
                tx, context, command.request_id, "decision", payload, session.id, run.id, receipt
            )
            return receipt

    def expire(self):
        with Session(self.store.engine) as tx:
            ids = tx.scalars(
                select(AgentWait.id)
                .where(
                    AgentWait.status == "pending",
                    AgentWait.kind == "approval",
                    AgentWait.expires_at <= func.clock_timestamp(),
                )
                .limit(100)
            ).all()
        changed = []
        for identifier in ids:
            with self.store.transaction() as tx:
                wait = tx.get(AgentWait, identifier)
                session, run = self.store.lock(tx, wait.run_id)
                tx.refresh(wait)
                if (
                    run.status == "waiting"
                    and wait.status == "pending"
                    and wait.expires_at <= tx.scalar(select(func.clock_timestamp()))
                ):
                    self.close_wait(tx, session, run, wait, "expired", "approval_expired")
                    changed.append(run.id)
        return changed

    def approved(self, run_id):
        with Session(self.store.engine, expire_on_commit=False) as tx:
            action = tx.scalar(
                select(AgentAction)
                .where(AgentAction.run_id == run_id)
                .order_by(AgentAction.created_at.desc())
                .limit(1)
            )
            if action is None:
                return None
            wait = tx.get(AgentWait, action.wait_id)
            assert wait is not None
            return action, wait

    def approvals(self, run_id):
        with Session(self.store.engine, expire_on_commit=False) as tx:
            return tx.scalars(
                select(AgentWait)
                .where(
                    AgentWait.run_id == run_id,
                    AgentWait.kind == "approval",
                    AgentWait.status == "approved",
                )
                .order_by(AgentWait.created_at)
            ).all()

    def require_reconciliation(self, run_id):
        with self.store.transaction() as tx:
            session, run = self.store.lock(tx, run_id)
            action = tx.scalar(
                select(AgentAction)
                .where(AgentAction.run_id == run_id, AgentAction.state.in_(["pending", "unknown"]))
                .order_by(AgentAction.created_at.desc())
                .limit(1)
            )
            if action is None or run.checkpoint_no is None:
                return
            if tx.scalar(
                select(AgentWait.id).where(
                    AgentWait.run_id == run_id, AgentWait.status == "pending"
                )
            ):
                return
            original = tx.get(AgentWait, action.wait_id)
            assert original is not None
            action.state = "unknown"
            wait = AgentWait(
                id=uuid4(),
                run_id=run_id,
                step_id=action.step_id,
                checkpoint_no=run.checkpoint_no,
                kind="reconciliation",
                proposal_id=original.proposal_id,
                proposal_revision=original.proposal_revision,
                proposal_sha256=original.proposal_sha256,
                public_proposal={"summary": "上次执行结果待核对。"},
                private_proposal=original.private_proposal,
                expectation={"actionId": str(action.id), "continuation": "reconciliation"},
                status="pending",
                created_at=now(),
                expires_at=None,
            )
            tx.add(wait)
            if run.status != "cancelling":
                run.status = "waiting"
            run.recovery_status, run.cleanup_status, run.reason_code = (
                "required",
                "unconfirmed",
                "execution_unknown",
            )
            self.store.outbox(tx, session, run, [approval_event(wait)])

    def await_external(self, run_id, epoch, step_id, event_type, correlation_key, payload_schema):
        """Trusted workflow port: the expected event and schema are frozen by server code."""
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(payload_schema)
        with self.store.transaction() as tx:
            session, run = self.store.lock(tx, run_id)
            self.store.require_epoch(run, epoch)
            if run.checkpoint_no is None:
                raise ModelError("checkpoint_missing", 409)
            step = tx.get(AgentStep, step_id)
            if step is None or step.run_id != run_id:
                raise ModelError("not_found", 404)
            identifier = uuid4()
            expectation = {
                "eventType": event_type,
                "correlationKey": correlation_key,
                "schema": payload_schema,
                "continuation": "external_event",
            }
            wait = AgentWait(
                id=identifier,
                run_id=run_id,
                step_id=step_id,
                checkpoint_no=run.checkpoint_no,
                kind="external_event",
                proposal_id=identifier,
                proposal_revision=1,
                proposal_sha256=digest(expectation),
                public_proposal={"summary": "等待外部事件确认。"},
                private_proposal=self.store.private.seal(run_id, "proposal", expectation),
                expectation=expectation,
                status="pending",
                created_at=now(),
                expires_at=now() + timedelta(days=1),
            )
            tx.add(wait)
            step.status = "waiting"
            run.status, run.execution_epoch, run.cleanup_status = "waiting", None, "confirmed"
            self.store.outbox(tx, session, run, [approval_event(wait)])
            return wait.id

    def external_event(self, run_id, event, *, boot_id):
        from jsonschema import Draft202012Validator

        from compass_agent.schemas import TrustedContext

        payload = {
            "kind": "external_event",
            "runId": str(run_id),
            **event.model_dump(mode="json", by_alias=True),
        }
        with self.store.transaction(admission=True) as tx:
            session, run = self.store.lock(tx, run_id)
            context = TrustedContext.model_validate(run.config_snapshot["context"])
            previous = self.store.command(tx, context, event.event_id, payload)
            if previous:
                return previous.response
            wait = tx.get(AgentWait, event.wait_id)
            if wait is None or wait.run_id != run_id or wait.kind != "external_event":
                raise ModelError("not_found", 404)
            if wait.status != "pending" or run.status != "waiting":
                raise ModelError("event_conflict", 409)
            expected = wait.expectation
            if (event.event_type, event.correlation_key) != (
                expected["eventType"],
                expected["correlationKey"],
            ):
                raise ModelError("event_mismatch", 409)
            if not Draft202012Validator(expected["schema"]).is_valid(event.payload):
                raise ModelError("validation_failed", 422)
            wait.status, wait.decision_id = "approved", event.event_id
            wait.decider_user_id, wait.decided_at = session.owner_user_id, now()
            wait.private_proposal = self.store.private.seal(run_id, "proposal", {"event": payload})
            run.status, run.execution_epoch, run.owner_boot_id = "running", uuid4(), boot_id
            run.reason_code, run.recovery_status = "external_event_accepted", "ready"
            events = [approval_event(wait)]
            if self.store.scheduler:
                events.append(self.store.scheduler.enqueue(tx, session, run))
            self.store.outbox(tx, session, run, events)
            result = {
                "runId": str(run_id),
                "sessionId": str(session.id),
                "version": run.version,
                "status": run.status,
                "code": None,
            }
            self.store.record_command(
                tx, context, event.event_id, "external_event", payload, session.id, run_id, result
            )
            return result

    def external_result(self, run_id, epoch, step_id):
        with self.store.transaction() as tx:
            _, run = self.store.lock(tx, run_id)
            self.store.require_epoch(run, epoch)
            wait = tx.scalar(
                select(AgentWait).where(
                    AgentWait.run_id == run_id,
                    AgentWait.step_id == step_id,
                    AgentWait.kind == "external_event",
                    AgentWait.status == "approved",
                )
            )
            if wait is None:
                return None
            return self.store.private.open(run_id, "proposal", wait.private_proposal)["event"]

    def reconcile(self, run_id, command, context):
        payload = {
            "kind": "reconcile",
            "runId": str(run_id),
            **command.model_dump(mode="json", by_alias=True),
        }
        with self.store.transaction(admission=True) as tx:
            previous = self.store.command(tx, context, command.request_id, payload)
            session, run = self.store.lock(tx, run_id, context)
            if previous:
                return previous.response
            wait, action = (
                tx.get(AgentWait, command.wait_id),
                tx.get(AgentAction, command.action_id),
            )
            if (
                wait is None
                or action is None
                or wait.run_id != run_id
                or action.run_id != run_id
                or wait.proposal_sha256 != action.proposal_sha256
            ):
                raise ModelError("not_found", 404)
            if run.version != command.expected_version:
                raise ModelError("version_conflict", 409)
            if run.status not in {"waiting", "paused", "cancelling"}:
                raise ModelError("state_conflict", 409)
            code = "effect_unconfirmed"
            if action.receipt is not None and action.state == "applied":
                action.state, action.updated_at = "applied", now()
                if run.status != "cancelling":
                    run.status, run.reason_code, run.recovery_status = (
                        "paused",
                        "effect_confirmed",
                        "ready",
                    )
                code = "effect_confirmed"
                if wait.kind == "reconciliation" and wait.status == "pending":
                    wait.status, wait.decided_at = "canceled", now()
            events = []
            if self.store.scheduler and code == "effect_confirmed" and run.status != "cancelling":
                events.append(self.store.scheduler.enqueue(tx, session, run))
            self.store.outbox(tx, session, run, events)
            result = {
                "runId": str(run.id),
                "sessionId": str(session.id),
                "version": run.version,
                "status": run.status,
                "code": code,
            }
            self.store.record_command(
                tx, context, command.request_id, "reconcile", payload, session.id, run.id, result
            )
            return result
