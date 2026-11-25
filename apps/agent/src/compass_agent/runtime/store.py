"""Short PostgreSQL transactions for Session, Run and critical publication boundaries."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, object_session

from compass_agent.models.errors import ModelError
from compass_agent.runtime.entities import (
    AgentArtifact,
    AgentAttempt,
    AgentCommand,
    AgentMessage,
    AgentOutbox,
    AgentRun,
    AgentSession,
    AgentStep,
)
from compass_agent.runtime.private_state import PrivateState, digest
from compass_agent.runtime.public_projection import reduce_packet, stream_position
from compass_agent.schemas import (
    DEFAULT_RUN_TIMEOUT_SECONDS,
    AgentPacket,
    AnalysisHistoryTurn,
    AnalysisRunInput,
    RunPublicSnapshot,
    SessionListItem,
    SessionSubmit,
    TrustedContext,
)

if TYPE_CHECKING:
    from compass_agent.runtime.run_scheduler import RunScheduler

TERMINAL = {"completed", "failed", "canceled"}


def now() -> datetime:
    return datetime.now(UTC)


def public_run(run: AgentRun) -> dict:
    return RunPublicSnapshot.model_validate(
        {
            "ordinal": run.ordinal,
            "version": run.version,
            "status": run.status,
            "reasonCode": run.reason_code,
            "recoveryStatus": run.recovery_status,
            "cleanupStatus": run.cleanup_status,
        }
    ).model_dump(mode="json", by_alias=True)


def session_item(session: AgentSession, run: AgentRun | None) -> dict:
    return SessionListItem.model_validate(
        {
            "sessionId": str(session.id),
            "title": session.title,
            "titleStatus": session.title_status,
            "sessionType": session.session_type,
            "iconKey": session.session_type,
            "version": session.version,
            "updatedAt": session.updated_at.astimezone(UTC).isoformat(),
            "latestRunId": str(run.id) if run else None,
            "executionStatus": run.public_view["run"]["status"] if run else None,
        }
    ).model_dump(mode="json", by_alias=True)


def message_snapshot(message: AgentMessage) -> dict:
    return {
        "messageId": str(message.id),
        "ordinal": message.ordinal,
        "role": message.role,
        "revision": message.revision,
        "status": message.status,
        "content": message.public_content,
    }


def canceled_execution(store, tx, run):
    for step in tx.scalars(
        select(AgentStep).where(
            AgentStep.run_id == run.id, AgentStep.status.in_(("pending", "waiting", "running"))
        )
    ):
        step.status, step.finished_at = "canceled", run.finished_at
        step.revision += 1
    for attempt in tx.scalars(
        select(AgentAttempt).where(AgentAttempt.run_id == run.id, AgentAttempt.status == "prepared")
    ):
        attempt.status, attempt.finished_at = "canceled", run.finished_at
        attempt.error_code = "user_canceled"
    message = tx.scalars(
        select(AgentMessage).where(
            AgentMessage.run_id == run.id,
            AgentMessage.role == "assistant",
            AgentMessage.channel == "conversation",
        )
    ).one()
    value = message_snapshot(message)
    value["revision"] += 1
    value["status"] = "incomplete"
    return [
        *settled_nodes(store, tx, run, "canceled"),
        {"type": "message.upsert", "payload": value},
    ]


def settled_nodes(store, tx, run, status):
    # Progress may be committed before Redis projection catches up. Settle from
    # the latest committed revision so a stop never overwrites an accepted view.
    objects = dict(run.public_view["objects"])
    for box in tx.scalars(
        select(AgentOutbox).where(AgentOutbox.run_id == run.id, AgentOutbox.delivered_at.is_(None))
    ):
        for packet in box.payload["packets"]:
            event = packet["event"]
            if event["type"] != "view.upsert" or event["payload"]["kind"] != "execution.node":
                continue
            item = event["payload"]
            previous = objects.get(item["objectId"])
            if previous is None or previous["revision"] < item["revision"]:
                objects[item["objectId"]] = item
    events = []
    for obj in objects.values():
        if obj["kind"] != "execution.node" or obj["value"]["status"] not in {
            "running",
            "preparing",
            "waiting",
        }:
            continue
        if run.config_snapshot.get("contextRuntime"):
            from compass_agent.context_runtime.source_access import read_private, view_key

            saved = read_private(store, tx, run.id, view_key(obj))
            if saved:
                obj = saved["payload"]
        value = deepcopy(obj)
        value["revision"] += 1
        value["value"].update(
            status=status,
            revision=value["revision"],
            endedAtMs=max(int(now().timestamp() * 1000), value["value"]["startedAtMs"]),
        )
        events.append({"type": "view.upsert", "payload": value})
    return events


def owner_filter(context: TrustedContext, *, include_deleted=False):
    if context.scope.kind != "tenant" or context.purpose != "chat":
        raise ModelError("forbidden", 403)
    return (
        AgentSession.tenant_id == context.scope.tenant_id,
        AgentSession.workspace_id == context.scope.workspace_id,
        AgentSession.owner_user_id == context.actor_user_id,
        *((AgentSession.deleted_at.is_(None),) if not include_deleted else ()),
    )


class RuntimeStore:
    def __init__(self, engine, key: str, *, settings=None):
        from compass_agent.context_runtime.semantics import MetricCatalog
        from compass_agent.settings import get_settings

        settings = settings or get_settings()
        self.source_settings = settings
        self.context_enabled = settings.compass_context_runtime_enabled
        self.context_scopes = settings.compass_context_scope_profiles
        self.metric_catalog = MetricCatalog(settings.compass_metric_dictionary_path)
        self.engine = engine
        self.private = PrivateState(key)
        self.ready = False
        self.distributed = False
        self.scheduler: RunScheduler | None = None

    @contextmanager
    def transaction(self, *, admission=False):
        with Session(self.engine, expire_on_commit=False) as tx, tx.begin():
            if self.distributed:
                from compass_agent.runtime.deployment import require_internal_mode

                require_internal_mode(tx)
            if admission and self.scheduler:
                self.scheduler.admission(tx)
            yield tx

    def lock(self, tx, run_id: UUID, context: TrustedContext | None = None):
        session_id = tx.scalar(select(AgentRun.session_id).where(AgentRun.id == run_id))
        query = select(AgentSession).where(AgentSession.id == session_id)
        if context is not None:
            query = query.where(*owner_filter(context))
        session = tx.scalar(query.with_for_update())
        if session is None:
            raise ModelError("not_found", 404)
        run = tx.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
        if run is None:
            raise ModelError("not_found", 404)
        return session, run

    def read_run(self, run_id: UUID, context: TrustedContext | None = None) -> AgentRun:
        with Session(self.engine, expire_on_commit=False) as tx:
            query = select(AgentRun).join(AgentSession).where(AgentRun.id == run_id)
            if context is not None:
                query = query.where(*owner_filter(context))
            run = tx.scalar(query)
            if run is None:
                raise ModelError("not_found", 404)
            return run

    def command(self, tx, context, request_id, payload):
        if context.scope.kind != "tenant":
            raise ModelError("forbidden", 403)
        identity = [
            str(context.scope.tenant_id),
            str(context.scope.workspace_id),
            str(context.actor_user_id),
            str(request_id),
        ]
        lock_id = int(digest(identity)[:16], 16) - 2**63
        tx.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})
        previous = tx.scalar(
            select(AgentCommand).where(
                AgentCommand.tenant_id == context.scope.tenant_id,
                AgentCommand.workspace_id == context.scope.workspace_id,
                AgentCommand.actor_user_id == context.actor_user_id,
                AgentCommand.request_id == request_id,
            )
        )
        if previous is not None and previous.request_sha256 != digest(payload):
            from compass_agent.runtime.history import same_request

            saved = tx.get(AgentRun, previous.run_id) if previous.kind == "submit" else None
            if saved is None or not same_request(saved.config_snapshot.get("command", {}), payload):
                raise ModelError("request_conflict", 409)
        return previous

    def accepted_retry(self, data: dict, context: TrustedContext):
        """Read a prior receipt; this boundary never creates or schedules a Run."""
        from compass_agent.runtime.history import retry_payload

        if context.scope.kind != "tenant":
            raise ModelError("forbidden", 403)
        try:
            request_id = UUID(data["requestId"])
        except (KeyError, TypeError, ValueError):
            raise ModelError("invalid_parameters", 422) from None
        with Session(self.engine) as tx:
            previous = tx.scalar(
                select(AgentCommand).where(
                    AgentCommand.tenant_id == context.scope.tenant_id,
                    AgentCommand.workspace_id == context.scope.workspace_id,
                    AgentCommand.actor_user_id == context.actor_user_id,
                    AgentCommand.request_id == request_id,
                    AgentCommand.kind == "submit",
                )
            )
            if previous is None:
                raise ModelError("invalid_parameters", 422)
            session = tx.scalar(
                select(AgentSession.id).where(
                    AgentSession.id == previous.session_id, *owner_filter(context)
                )
            )
            if session is None:
                raise ModelError("not_found", 404)
            run = tx.get(AgentRun, previous.run_id)
            if run is None:
                raise ModelError("not_found", 404)
            normalized = retry_payload(data, run.config_snapshot.get("command", {}))
            if digest(normalized) != previous.request_sha256:
                raise ModelError("request_conflict", 409)
            return previous.response

    @staticmethod
    def record_command(tx, context, request_id, kind, payload, session_id, run_id, response):
        tx.add(
            AgentCommand(
                id=uuid4(),
                tenant_id=context.scope.tenant_id,
                workspace_id=context.scope.workspace_id,
                actor_user_id=context.actor_user_id,
                request_id=request_id,
                session_id=session_id,
                run_id=run_id,
                kind=kind,
                request_sha256=digest(payload),
                response=response,
                created_at=now(),
            )
        )

    def outbox(self, tx, session, run, events, *, operation_id=None):
        from compass_agent.runtime.public_visibility import public_event

        if run.config_snapshot.get("contextRuntime"):
            from compass_agent.runtime.public_visibility import managed_events

            events = managed_events(self, tx, run, events)
        events = [public_event(event) for event in events]
        if self.scheduler:
            events = self.scheduler.synchronize(tx, session, run, events)
        run.version += 1
        session.version += 1
        session.updated_at = now()
        identifier = uuid4()
        timestamp = now().isoformat()
        packets = []
        for index, event in enumerate(
            [*events, {"type": "run.upsert", "payload": public_run(run)}]
        ):
            occurred_at = timestamp
            if event["type"] == "run.upsert" and run.finished_at is not None:
                occurred_at = run.finished_at.astimezone(UTC).isoformat()
            elif event["type"] == "message.upsert" and event["payload"]["role"] == "user":
                occurred_at = run.created_at.astimezone(UTC).isoformat()
            packet = {
                "packetId": str(uuid5(identifier, str(index))),
                "runId": str(run.id),
                "seq": None,
                "requestId": str(run.request_id),
                "occurredAt": occurred_at,
                "event": event,
            }
            AgentPacket.model_validate(packet)
            packets.append(packet)
        payload = {"protocolVersion": 2, "packets": packets}
        box = AgentOutbox(
            id=identifier,
            run_id=run.id,
            run_version=run.version,
            request_id=run.request_id,
            operation_id=operation_id or identifier,
            operation_sha256=digest(payload),
            payload=payload,
            created_at=now(),
        )
        tx.add(box)
        return box

    def submit(
        self,
        command: SessionSubmit,
        context: TrustedContext,
        imported_history=None,
        *,
        run_environment=None,
    ):
        if context.scope.kind != "tenant":
            raise ModelError("forbidden", 403)
        with self.transaction(admission=True) as tx:
            return self.submit_in_transaction(
                tx,
                command,
                context,
                imported_history=imported_history,
                run_environment=run_environment,
            )

    @staticmethod
    def submission_payload(command):
        payload = command.model_dump(mode="json", by_alias=True)
        if command.constraints is None:
            payload.pop("constraints", None)
        return payload

    def submit_in_transaction(
        self,
        tx,
        command,
        context,
        *,
        imported_history=None,
        schedule=None,
        run_environment=None,
        goal_round=None,
    ):
        payload = self.submission_payload(command)
        kind = (
            "schedule_submit"
            if schedule
            else ("goal_submit" if goal_round is not None else "submit")
        )
        old = self.command(tx, context, command.request_id, payload)
        if old:
            if old.kind != kind:
                raise ModelError("request_conflict", 409)
            if kind == "schedule_submit":
                return old.response
            session = tx.scalar(
                select(AgentSession).where(
                    AgentSession.id == old.session_id, *owner_filter(context)
                )
            )
            if session is None:
                raise ModelError("not_found", 404)
            if old.response.get("session"):
                latest = tx.scalar(
                    select(AgentRun)
                    .where(AgentRun.session_id == session.id)
                    .order_by(AgentRun.ordinal.desc())
                    .limit(1)
                )
                return {**old.response, "session": session_item(session, latest)}
            return old.response
        if not self.ready:
            raise ModelError("runtime_unavailable", 503)
        if self.scheduler:
            self.scheduler.require_capacity(tx, context.scope.tenant_id)
        session = None
        if command.session_id:
            session = tx.scalar(
                select(AgentSession)
                .where(AgentSession.id == command.session_id, *owner_filter(context))
                .with_for_update()
            )
            if session is None:
                raise ModelError("not_found", 404)
        new_session = session is None
        if session is None:
            session = AgentSession(
                id=uuid4(),
                tenant_id=context.scope.tenant_id,
                workspace_id=context.scope.workspace_id,
                owner_user_id=context.actor_user_id,
                title=" ".join(command.question.split())[:40],
                title_status="pending",
                title_next_at=now(),
                session_type="chat",
                version=1,
                context_start_ordinal=1,
                created_at=now(),
                updated_at=now(),
            )
            tx.add(session)
            tx.flush()
        last = tx.scalar(
            select(AgentRun)
            .where(AgentRun.session_id == session.id)
            .order_by(AgentRun.ordinal.desc())
            .limit(1)
        )
        if last and (
            last.status not in TERMINAL
            or last.public_view["run"]["status"] not in TERMINAL
            or tx.scalar(
                select(AgentOutbox.id)
                .where(AgentOutbox.run_id == last.id, AgentOutbox.delivered_at.is_(None))
                .limit(1)
            )
        ):
            raise ModelError("session_busy", 409)
        run_id = uuid4()
        config = {
            "command": payload,
            "context": context.model_dump(mode="json"),
            "promptEnvironment": run_environment or {},
            "schemaVersion": 1,
            "deepseekThinking": True,
            "timeoutSeconds": (command.constraints.timeout_seconds if command.constraints else None)
            or DEFAULT_RUN_TIMEOUT_SECONDS,
        }
        from compass_agent.goal.controller import open_goal

        goal = open_goal(tx, session.id)
        if goal_round is not None and (goal is None or goal.status != "active"):
            raise ModelError("goal_not_active", 409)
        if goal:
            config["goal"] = {
                "goalId": str(goal.id),
                "round": goal_round,
                "ledgerRevision": goal.ledger_revision,
            }
        if self.scheduler:
            config["budget"] = self.scheduler.budget
        if schedule:
            config["schedule"] = schedule
        if imported_history:
            config["importedHistory"] = imported_history
        if self.context_enabled:
            from compass_agent.context_runtime.pipeline import exact, pin

            config["contextRuntime"] = pin(tx, context, self)
            config["contextRuntime"]["answerProtocol"] = 2
            exact(
                tx,
                config["contextRuntime"],
                ["tools/final_answer.json", "shared/fragments/answer-protocol.md"],
            )
            if self.source_settings.compass_context_optimization_enabled:
                exact(
                    tx,
                    config["contextRuntime"],
                    [
                        "support/context/selection.md",
                        "support/context/compaction.md",
                        "shared/fragments/context-results.md",
                        "tools/read_tool_result.json",
                    ],
                )
                config["contextRuntime"]["optimizationVersion"] = 1
            from compass_agent.semantic.client import enabled, pin_manifest

            if enabled(self.source_settings, session.workspace_id):
                config["contextRuntime"].update(
                    pin_manifest(self.source_settings, context, run_id, "registry")
                )
        run = AgentRun(
            id=run_id,
            session_id=session.id,
            request_id=command.request_id,
            ordinal=1 if last is None else last.ordinal + 1,
            status="created",
            version=1,
            projected_seq="0-0",
            recovery_status="ready",
            cleanup_status="none",
            config_snapshot=config,
            public_view={},
            created_at=now(),
            active_ms=0,
        )
        run.public_view = {"schemaVersion": 1, "run": public_run(run), "objects": {}}
        tx.add(run)
        tx.flush()
        messages = []
        for ordinal, role, content, status in [
            (1, "user", command.question, "completed"),
            (2, "assistant", "", "streaming"),
        ]:
            message = AgentMessage(
                id=uuid5(run.id, role),
                run_id=run.id,
                ordinal=ordinal,
                channel="conversation",
                role=role,
                status=status,
                revision=1,
                public_content={"text": content, "artifactIds": []},
                created_at=now(),
                updated_at=now(),
            )
            tx.add(message)
            messages.append({"type": "message.upsert", "payload": message_snapshot(message)})
        if self.scheduler:
            messages.append(self.scheduler.enqueue(tx, session, run, initial=True))
        self.outbox(tx, session, run, messages, operation_id=command.request_id)
        response: dict[str, Any] = {
            "sessionId": str(session.id),
            "runId": str(run.id),
            "streamUrl": f"/v1/analysis/runs/{run.id}/events",
        }
        if new_session:
            response["session"] = session_item(session, run)
        self.record_command(
            tx, context, command.request_id, kind, payload, session.id, run.id, response
        )
        return response

    def history_command(self, run_id):
        from compass_agent.context_runtime.source_access import (
            allows,
            assistant_message,
            final_text,
        )

        with Session(self.engine) as tx, tx.begin():
            run = tx.get(AgentRun, run_id)
            assert run is not None
            session = tx.get(AgentSession, run.session_id)
            assert session is not None
            history = []
            managed = bool(run.config_snapshot.get("contextRuntime"))
            if run.config_snapshot.get("contextRuntime", {}).get("optimizationVersion") == 1:
                from compass_agent.runtime.history import decode_command

                data = dict(run.config_snapshot["command"])
                data.pop("sessionId", None)
                data = decode_command(data)
                data["history"] = []
                return AnalysisRunInput.model_validate(data)
            trusted = (
                TrustedContext.model_validate(run.config_snapshot["context"]) if managed else None
            )
            inherited = []
            for old in reversed(
                tx.scalars(
                    select(AgentRun)
                    .where(
                        AgentRun.session_id == session.id,
                        AgentRun.ordinal >= session.context_start_ordinal,
                        AgentRun.ordinal < run.ordinal,
                        AgentRun.status == "completed",
                    )
                    .order_by(AgentRun.ordinal.desc())
                    .limit(12)
                ).all()
            ):
                messages = tx.scalars(
                    select(AgentMessage)
                    .where(AgentMessage.run_id == old.id, AgentMessage.channel == "conversation")
                    .order_by(AgentMessage.ordinal)
                ).all()
                if (
                    len(messages) == 2
                    and messages[1].public_content
                    and messages[1].public_content["text"]
                ):
                    assert messages[0].public_content is not None
                    answer = messages[1].public_content["text"]
                    if managed:
                        if not old.config_snapshot.get("contextRuntime"):
                            continue
                        refs = messages[1].source_refs or []
                        if not allows(self, trusted, refs):
                            continue
                        answer = final_text(self, tx, old.id)
                        if not answer:
                            continue
                        inherited.extend(ref for ref in refs if ref not in inherited)
                    history.append(
                        AnalysisHistoryTurn(
                            question=messages[0].public_content["text"],
                            answer=answer,
                        )
                    )
            data = dict(run.config_snapshot["command"])
            if inherited:
                current = assistant_message(tx, run_id)
                if current:
                    old_refs = current.source_refs or []
                    current.source_refs = [
                        *old_refs,
                        *(ref for ref in inherited if ref not in old_refs),
                    ]
            data.pop("sessionId", None)
            from compass_agent.runtime.history import decode_command

            data = decode_command(data)
            while (
                history
                and len(
                    "\n".join(
                        [data["question"], *(v for h in history for v in (h.question, h.answer))]
                    ).encode()
                )
                > 32768
            ):
                history.pop(0)
            data["history"] = (
                history
                if managed or session.context_start_ordinal > 1
                else history or run.config_snapshot.get("importedHistory", [])
            )
            return AnalysisRunInput.model_validate(data)

    def pending(self, run_id=None, *, limit=None):
        with Session(self.engine, expire_on_commit=False) as tx:
            query = (
                select(AgentOutbox)
                .join(AgentRun, AgentRun.id == AgentOutbox.run_id)
                .where(
                    AgentOutbox.delivered_at.is_(None),
                    (AgentRun.recovery_status != "blocked")
                    | (
                        (AgentRun.status == "cancelling")
                        & AgentRun.reason_code.in_(("execution_unknown", "cleanup_unconfirmed"))
                    ),
                )
            )
            if run_id:
                query = query.where(AgentOutbox.run_id == run_id)
            ordered = query.order_by(AgentOutbox.run_id, AgentOutbox.run_version)
            return tx.scalars(ordered.limit(limit) if limit else ordered).all()

    def progress(self, run_id, epoch, events):
        with self.transaction() as tx:
            session, run = self.lock(tx, run_id)
            self.require_epoch(run, epoch)
            return self.outbox(tx, session, run, events).id

    def require_epoch(self, run, epoch):
        if (
            epoch is None
            or run.execution_epoch != epoch
            or run.status not in {"running", "cancelling"}
        ):
            raise ModelError("execution_fenced", 409)
        if self.scheduler:
            tx = object_session(run)
            if tx is None:
                with self.transaction() as tx:
                    _, current = self.lock(tx, run.id)
                    self.scheduler.require_execution(tx, current, epoch)
            else:
                self.scheduler.require_execution(tx, run, epoch)

    def claim(self, run_id, boot_id):
        if self.scheduler:
            raise ModelError("worker_required", 409)
        with self.transaction() as tx:
            session, run = self.lock(tx, run_id)
            if run.status not in {"created", "paused"} or run.recovery_status == "blocked":
                raise ModelError("resume_blocked", 409)
            if run.execution_epoch:
                raise ModelError("execution_owned", 409)
            run.execution_epoch = uuid4()
            run.owner_boot_id = boot_id
            run.status = "running"
            run.started_at = run.started_at or now()
            run.reason_code = None
            run.recovery_status = "ready"
            self.outbox(tx, session, run, [])
            return run.execution_epoch

    def pending_runs(self, limit=32):
        with Session(self.engine) as tx:
            outbox = AgentOutbox
            return tx.scalars(
                select(outbox.run_id)
                .join(AgentRun, AgentRun.id == outbox.run_id)
                .where(
                    outbox.delivered_at.is_(None),
                    AgentRun.recovery_status != "blocked",
                    (AgentRun.publish_token.is_(None))
                    | (AgentRun.publish_expires_at <= func.clock_timestamp()),
                )
                .group_by(outbox.run_id)
                .order_by(func.min(outbox.created_at), outbox.run_id)
                .limit(limit)
            ).all()

    def publication(self, run_id):
        with self.transaction() as tx:
            _, run = self.lock(tx, run_id)
            clock = tx.scalar(select(func.clock_timestamp()))
            assert isinstance(clock, datetime)
            if run.recovery_status == "blocked" or (
                run.publish_expires_at and run.publish_expires_at > clock
            ):
                return None
            run.publish_token = uuid4()
            run.publish_generation += 1
            run.publish_expires_at = clock + timedelta(seconds=10)
            return run

    def end_publication(self, lease):
        with self.transaction() as tx:
            _, run = self.lock(tx, lease.id)
            if run.publish_token == lease.publish_token:
                run.publish_token = run.publish_expires_at = None

    def project(self, run_id, entries, lease=None):
        if not entries and lease is None:
            return
        with self.transaction() as tx:
            _, run = self.lock(tx, run_id)
            if lease is not None:
                clock = tx.scalar(select(func.clock_timestamp()))
                assert isinstance(clock, datetime)
                if (
                    run.publish_token != lease.publish_token
                    or run.publish_generation != lease.publish_generation
                    or run.publish_expires_at is None
                    or run.publish_expires_at <= clock
                    or run.projected_seq != lease.projected_seq
                    or run.recovery_status == "blocked"
                ):
                    raise ModelError("publish_lease_lost", 409)
                run.publish_initialized = True
            entries = [
                (seq, row)
                for seq, row in entries
                if stream_position(seq) > stream_position(run.projected_seq)
            ]
            if not entries:
                return
            rows = tx.scalars(
                select(AgentMessage).where(
                    AgentMessage.run_id == run.id, AgentMessage.channel == "conversation"
                )
            ).all()
            messages = {str(m.id): message_snapshot(m) for m in rows}
            view = deepcopy(run.public_view)
            for seq, row in entries:
                packet = dict(row["packet"], seq=seq)
                reduce_packet(view, messages, packet)
            if (
                len(
                    __import__("json")
                    .dumps({"view": view, "messages": messages}, ensure_ascii=False)
                    .encode()
                )
                > 4 * 1024 * 1024
            ):
                raise ModelError("run_content_limit", 409)
            for message in rows:
                value = messages[str(message.id)]
                message.public_content, message.revision, message.status = (
                    value["content"],
                    value["revision"],
                    value["status"],
                )
                message.updated_at = now()
            run.public_view, run.projected_seq = view, entries[-1][0]
            for seq, row in entries:
                if row.get("outboxId") and row["batchIndex"] == row["batchSize"] - 1:
                    box = tx.get(AgentOutbox, UUID(row["outboxId"]))
                    if box is None or box.run_id != run_id:
                        raise ModelError("projection_outbox_missing", 409)
                    box.last_stream_id, box.delivered_at = seq, now()

    def finish(
        self,
        run_id,
        epoch,
        status,
        *,
        answer=None,
        reason=None,
        events=(),
        cleanup="confirmed",
        drafts=(),
        delivery=None,
        citations=None,
        answer_acceptance=None,
    ):
        with self.transaction() as tx:
            session, run = self.lock(tx, run_id)
            self.require_epoch(run, epoch)
            if (
                status == "completed"
                and run.config_snapshot.get("contextRuntime", {}).get("answerProtocol") == 2
                and answer_acceptance is None
            ):
                raise ModelError("delivery_conflict", 409)
            if answer_acceptance is not None:
                from compass_agent.context_runtime.source_access import read_private
                from compass_agent.runtime.private_state import digest

                accepted = read_private(
                    self, tx, run_id, f"answer/v2/accepted/{answer_acceptance['candidateId']}"
                )
                if (
                    accepted is None
                    or accepted != answer_acceptance
                    or accepted["executionPath"] != "root"
                    or accepted["textDigest"] != digest(answer)
                    or accepted["citationsDigest"] != digest(citations)
                ):
                    raise ModelError("delivery_conflict", 409)
                from hashlib import sha256

                artifact_digest = sha256(delivery.data).hexdigest() if delivery else None
                if accepted["artifactDigest"] != artifact_digest:
                    raise ModelError("delivery_conflict", 409)
                from compass_agent.context_runtime.source_access import require_run_sources

                require_run_sources(
                    self,
                    run_id,
                    TrustedContext.model_validate(run.config_snapshot["context"]),
                    epoch=epoch,
                )
            if run.status == "cancelling":
                status = "canceled" if cleanup == "confirmed" else "cancelling"
                if answer_acceptance is not None:
                    answer, citations, delivery, answer_acceptance = None, None, None, None
            run.status, run.reason_code, run.cleanup_status = status, reason, cleanup
            run.execution_epoch = None
            if status in TERMINAL:
                run.finished_at = now()
                events = [
                    *events,
                    *settled_nodes(
                        self,
                        tx,
                        run,
                        "completed"
                        if status == "completed"
                        else "canceled"
                        if status == "canceled"
                        else "failed",
                    ),
                ]
            if status == "completed" and drafts:
                from compass_agent.runtime.resources import draft_event

                for draft in drafts:
                    tx.add(draft)
                events = [*events, *(draft_event(draft) for draft in drafts)]
                session.session_type = "workflow"
                session.workflow_key = str(drafts[-1].id)
            assistant = tx.scalar(
                select(AgentMessage).where(
                    AgentMessage.run_id == run.id,
                    AgentMessage.role == "assistant",
                    AgentMessage.channel == "conversation",
                )
            )
            assert assistant is not None
            if status == "completed" and delivery is not None:
                from compass_agent.context_runtime.delivery import persist_delivery

                refs = assistant.source_refs or []
                if any(ref.get("kind") == "transient" for ref in refs):
                    if answer_acceptance is not None:
                        raise ModelError("delivery_conflict", 409)
                    answer = (
                        (answer or "") + "\n\n本次数据源仅支持即时读取，无法保存附件。"
                        "可使用支持历史读取的连接重新生成。"
                    )
                else:
                    events = [*events, persist_delivery(tx, session, run, delivery, refs)]
            value = message_snapshot(assistant)
            value["revision"] += 1
            value["status"] = "completed" if status == "completed" else "incomplete"
            if answer is not None:
                if run.config_snapshot.get("contextRuntime"):
                    from compass_agent.context_runtime.source_access import (
                        REFERENCE,
                        save_final_text,
                    )

                    if citations:
                        from compass_agent.context_runtime.citations import (
                            publish_citations,
                        )

                        publish_citations(
                            self, tx, run, assistant, answer, value["revision"], citations
                        )
                    save_final_text(self, tx, run, assistant, answer)
                    answer = REFERENCE
                value["content"] = {**value["content"], "text": answer}
                if answer_acceptance is not None:
                    value["content"].update(
                        answerMode=answer_acceptance["mode"],
                        completion=answer_acceptance["completion"],
                    )
            value["content"]["artifactIds"] = [
                str(v)
                for v in tx.scalars(select(AgentArtifact.id).where(AgentArtifact.run_id == run.id))
            ]
            self.outbox(tx, session, run, [*events, {"type": "message.upsert", "payload": value}])

    def record_active_time(self, run_id, epoch, duration_ms):
        with self.transaction() as tx:
            _, run = self.lock(tx, run_id)
            # The owner clears the epoch at a durable boundary before releasing its task.
            if run.execution_epoch in {None, epoch}:
                run.active_ms = max(run.active_ms, duration_ms)

    def recover_boot(self, boot_id):
        with Session(self.engine) as tx:
            ids = tx.scalars(
                select(AgentRun.id).where(AgentRun.status.in_(["running", "created", "cancelling"]))
            ).all()
        for identifier in ids:
            with self.transaction() as tx:
                session, run = self.lock(tx, identifier)
                if run.owner_boot_id == boot_id:
                    continue
                if run.recovery_status == "blocked":
                    continue
                if run.status != "cancelling":
                    run.status = "paused"
                run.execution_epoch = None
                run.recovery_status = "required"
                run.reason_code = "process_interrupted"
                self.outbox(tx, session, run, [])

    def blocked(self, run_id, reason):
        with self.transaction() as tx:
            session, run = self.lock(tx, run_id)
            if run.status not in TERMINAL and run.recovery_status != "blocked":
                run.recovery_status = "blocked"
                run.reason_code = reason
                if run.status == "running":
                    run.status = "paused"
                if not self.distributed:
                    run.execution_epoch = None
                # This recovery-only projection has no Redis successor. Message reload
                # exposes the blocked state at the last confirmed cursor.
                run.version += 1
                session.version += 1
                run.public_view = {**run.public_view, "run": public_run(run)}

    def artifact(self, run_id, artifact_id, context):
        run = self.read_run(run_id, context)
        if run.config_snapshot.get("contextRuntime"):
            from compass_agent.context_runtime.source_access import require_run_sources

            require_run_sources(self, run_id, context)
        elif self.context_enabled:
            raise ModelError("source_access_denied", 403)
        with Session(self.engine, expire_on_commit=False) as tx:
            item = tx.scalar(
                select(AgentArtifact).where(
                    AgentArtifact.run_id == run_id, AgentArtifact.id == artifact_id
                )
            )
            if item is None:
                raise ModelError("not_found", 404)
            if (
                __import__("hashlib").sha256(item.content).hexdigest() != item.sha256
                or len(item.content) != item.size_bytes
            ):
                raise ModelError("artifact_corrupt", 409)
            return item
