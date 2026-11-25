"""Accepted execution results and explicit recovery boundaries, independent of HTTP."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from hashlib import sha256
from time import monotonic
from uuid import UUID, uuid4, uuid5

from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from compass_agent.execution_context import execution_scope
from compass_agent.models.errors import ModelError
from compass_agent.runtime.entities import (
    AgentAction,
    AgentArtifact,
    AgentAttempt,
    AgentCheckpoint,
    AgentMessage,
    AgentRun,
    AgentStep,
    AgentToolBinding,
)
from compass_agent.runtime.private_state import digest
from compass_agent.runtime.store import now
from compass_agent.tools.contracts import PhaseCanceled

logger = logging.getLogger(__name__)


class BoundaryYield(BaseException):
    """A root call and its worker receipt committed; return at this recovery boundary."""


class RecoveryStop(BaseException):
    """Unwind resource-owning orchestration without converting a paused Run into failure."""

    def __init__(self, reason):
        self.reason = reason


def checkpoint_digest(value):
    return digest(
        {
            name: getattr(value, name)
            for name in (
                "schema_version",
                "position",
                "completed_refs",
                "message_refs",
                "tool_binding_refs",
                "artifact_refs",
                "plan_state",
                "budget_state",
                "pending_calls",
            )
        }
        | {
            "verifier": sha256(value.verifier_state).hexdigest(),
            "continuation": sha256(value.continuation_state or b"").hexdigest(),
            "boundary": str(value.boundary_outbox_id),
        }
    )


class ExecutionJournal:
    def __init__(self, store, publisher, run_id, epoch, *, access=None):
        self.store, self.publisher, self.run_id, self.epoch = store, publisher, run_id, epoch
        self.access = access
        self.counters: dict[str, int] = {}
        self.parent: ContextVar[UUID | None] = ContextVar("journal_parent", default=None)
        self.active_attempt: ContextVar[UUID | None] = ContextVar("journal_attempt", default=None)
        self._last_attempt: ContextVar[UUID | None] = ContextVar(
            "journal_last_attempt", default=None
        )
        from compass_agent.models.secrets import SecretStore

        self.model_secrets: SecretStore | None = None
        self.flush_progress: Callable[[], Awaitable[None]] | None = None
        self.pending_proposal = None
        self.approved_proposal: dict | None = None
        self.approved_proposals: list[dict] = []
        self.completed_deliveries: dict[str, int] = {}
        self.fault: Callable[[str, UUID], Awaitable[None]] | None = None
        self.text_message = None
        self.text_lock = asyncio.Lock()
        self.pending_drafts = []
        self.active_started = monotonic()
        run = store.read_run(run_id)
        self.goal_id = (run.config_snapshot.get("goal") or {}).get("goalId")
        self.accepted_at = run.created_at
        self.user_question = run.config_snapshot.get("command", {}).get("question", "")
        self.context_binding = run.config_snapshot.get("contextRuntime")
        self.search_snapshot = run.config_snapshot.get("searchSnapshot")
        self.rebuilding_search = bool(self.search_snapshot and run.checkpoint_no)
        self.rebuilt_source_digest: str | None = None
        self.context_phase = "identify"
        self.context_receipts = {}
        self.context_questions = {}
        self.prompt_cache = {}
        self.capabilities: Callable[[], Awaitable[dict]] | None = None
        from compass_agent.context_runtime.run_facts import facts_for
        from compass_agent.schemas import TrustedContext

        self.prompt_facts = None
        self.context_date = run.created_at.date()
        if (environment := run.config_snapshot.get("promptEnvironment")) is not None:
            self.prompt_facts = facts_for(
                TrustedContext.model_validate(run.config_snapshot["context"]),
                run.created_at,
                environment,
                self.search_snapshot,
            )
            self.context_date = self.prompt_facts.local_date
        if self.search_snapshot:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            self.context_date = (
                datetime.fromisoformat(self.search_snapshot["knownAt"])
                .astimezone(ZoneInfo(self.search_snapshot["timezone"]))
                .date()
            )
        self.scheduled = bool(run.config_snapshot.get("schedule"))
        self.schedule_tools = store.distributed and "budget" in run.config_snapshot
        self.active_base = run.active_ms
        # Preserve the original model request fingerprint when an older Run resumes.
        self.deepseek_thinking = run.config_snapshot.get("deepseekThinking", False)
        self.state_providers = {}
        self.context_results = None
        self.context_optimizer = None
        self.context_accounted_attempts = set()
        if (self.context_binding or {}).get("optimizationVersion") == 1:
            from compass_agent.runtime.context_results import ContextResults

            self.context_results = ContextResults(self)
            from compass_agent.context_runtime.optimizer import ContextOptimizer

            self.context_optimizer = ContextOptimizer(self)

    def business_state(self):
        return {path: provider() for path, provider in self.state_providers.items()}

    def prefetch_prompts(self, paths):
        from compass_agent.context_runtime.pipeline import exact

        missing = [path for path in paths if path not in self.prompt_cache]
        if missing:
            with Session(self.store.engine) as tx:
                self.prompt_cache.update(
                    (file.path, file) for file in exact(tx, self.context_binding, missing)
                )

    def prompt(self, path: str) -> str:
        self.prefetch_prompts((path,))
        return self.prompt_cache[path].body.rstrip("\n")

    @property
    def last_attempt(self):
        return self._last_attempt.get()

    @last_attempt.setter
    def last_attempt(self, value):
        self._last_attempt.set(value)

    def identifier(self, path):
        number = self.counters.get(path, 0) + 1
        self.counters[path] = number
        return uuid5(self.run_id, f"id:{path}:{number}")

    @staticmethod
    def binding_snapshot(tool):
        from dataclasses import asdict

        from compass_agent.tools.contracts import InProcessBinding

        binding = tool.binding
        value = (
            {"environment": "in_process", "orchestrates": binding.orchestrates}
            if isinstance(binding, InProcessBinding)
            else asdict(binding)
            if binding
            else {}
        )
        return TypeAdapter(dict).dump_python(value, mode="json")

    def catalog_declarations(self):
        with Session(self.store.engine) as tx:
            rows = tx.scalars(
                select(AgentToolBinding)
                .where(AgentToolBinding.run_id == self.run_id)
                .order_by(AgentToolBinding.created_at, AgentToolBinding.id)
            ).all()
            if not rows:
                return None
            # Preserve model-visible ordering via the original catalog traversal order.
            rows = sorted(rows, key=lambda row: int(row.binding.get("catalogOrdinal", 0)))
            return {row.alias: (row.declaration, row.binding) for row in rows}

    def freeze_catalog(self, registry, *, asset_names=None):
        from dataclasses import asdict

        asset_names = asset_names or {}
        with self.store.transaction() as tx:
            _, run = self.store.lock(tx, self.run_id)
            self.store.require_epoch(run, self.epoch)
            existing = {
                r.alias: r
                for r in tx.scalars(
                    select(AgentToolBinding).where(AgentToolBinding.run_id == run.id)
                )
            }
            if existing and set(existing) != set(registry.entries):
                raise RecoveryStop("tool_catalog_changed")
            for ordinal, (name, tool) in enumerate(registry.entries.items()):
                declaration = tool.definition.model_dump(mode="json") if tool.definition else {}
                binding, policy = self.binding_snapshot(tool), asdict(tool.policy)
                if name in asset_names:
                    binding["workflowName"] = asset_names[name]
                if name in existing:
                    row = existing[name]
                    previous = {
                        key: value for key, value in row.binding.items() if key != "catalogOrdinal"
                    }
                    if (row.declaration, previous, row.policy) != (declaration, binding, policy):
                        raise RecoveryStop("tool_catalog_changed")
                    continue
                tx.add(
                    AgentToolBinding(
                        id=uuid5(run.id, "binding:" + name),
                        run_id=run.id,
                        catalog_id=registry.catalog_id,
                        alias=name,
                        original_name=binding.get("remote_name", name),
                        source=binding.get("environment", "in_process"),
                        config_ref=binding.get("connection_id"),
                        config_revision=binding.get("revision"),
                        declaration=declaration,
                        schema_sha256=digest(declaration),
                        binding={**binding, "catalogOrdinal": ordinal},
                        policy=policy,
                        created_at=now(),
                    )
                )

    def approves(self, prepared, step_id):
        return self.approved_action(prepared, step_id) is not None

    def approved_action(self, prepared, step_id):
        proposals = self.approved_proposals + (
            [self.approved_proposal] if self.approved_proposal else []
        )
        return next(
            (
                p
                for p in proposals
                if p.get("toolName") == prepared.tool.name
                and digest(p.get("args")) == digest(prepared.args)
                and p.get("callId") == prepared.call_id
                and p.get("stepId") == str(step_id)
            ),
            None,
        )

    def artifact(self, ref, content):
        from compass_agent.runtime.resources import RuntimeResources

        RuntimeResources(self.store).artifact(self, ref, content)

    def draft(self, detail, *, step_id=None):
        from compass_agent.runtime.resources import RuntimeResources

        return RuntimeResources(self.store).offer(self, detail, step_id=step_id)

    async def text_delta(self, value):
        if self.store.scheduler or not value:
            return
        async with self.text_lock:
            if self.text_message is None:
                with Session(self.store.engine) as tx:
                    message = tx.scalars(
                        select(AgentMessage).where(
                            AgentMessage.run_id == self.run_id,
                            AgentMessage.channel == "conversation",
                            AgentMessage.role == "assistant",
                        )
                    ).one()
                    self.text_message = [
                        str(message.id),
                        message.revision,
                        len((message.public_content or {}).get("text", "").encode()),
                    ]
            run = await asyncio.to_thread(self.store.read_run, self.run_id)
            self.store.require_epoch(run, self.epoch)
            from compass_agent.schemas import RUN_STREAM_LIMITS

            size = self.text_message[2] + len(value.encode())
            if size > RUN_STREAM_LIMITS["answerBytes"]:
                raise ModelError("run_content_limit", 409)
            self.text_message[2] = size
            for start in range(0, len(value), 8192):
                self.text_message[1] += 1
                await self.publisher.delta(
                    self.run_id,
                    {
                        "packetId": str(uuid4()),
                        "runId": str(self.run_id),
                        "seq": None,
                        "requestId": str(run.request_id),
                        "occurredAt": now().isoformat(),
                        "event": {
                            "type": "message.delta",
                            "payload": {
                                "messageId": self.text_message[0],
                                "revision": self.text_message[1],
                                "text": value[start : start + 8192],
                            },
                        },
                    },
                )

    def workflow(self, source, args, *, parent_model_step=None):
        from compass_agent.execution_context import execution_path

        key = str(self.identifier(execution_path.get() + "/workflow"))
        with self.store.transaction() as tx:
            _, run = self.store.lock(tx, self.run_id)
            self.store.require_epoch(run, self.epoch)
            parent = self.parent.get() or (
                uuid5(run.id, "step:root/model:" + str(parent_model_step))
                if parent_model_step is not None
                else None
            )
            snapshot = {
                "source": source,
                "args": args,
                "runnerVersion": 1,
                "clock": {
                    "timeMs": int(run.created_at.timestamp() * 1000),
                    "seed": int(key.replace("-", "")[:8], 16),
                },
            }
            identifier = uuid5(run.id, "workflow:" + key)
            step = tx.get(AgentStep, identifier)
            saved = None
            if step:
                if step.definition_snapshot != snapshot:
                    raise RecoveryStop("workflow_replay_mismatch")
                if step.accepted_attempt_id:
                    attempt = tx.get(AgentAttempt, step.accepted_attempt_id)
                    if attempt is None or attempt.output_content is None:
                        raise RecoveryStop("checkpoint_corrupt")
                    saved = self.store.private.open(
                        run.id, "attempt-output", attempt.output_content
                    )
                    if digest(saved) != attempt.output_sha256:
                        raise RecoveryStop("checkpoint_corrupt")
            else:
                ordinal = (
                    tx.scalar(select(func.max(AgentStep.ordinal)).where(AgentStep.run_id == run.id))
                    or 0
                )
                tx.add(
                    AgentStep(
                        id=identifier,
                        run_id=run.id,
                        parent_step_id=parent,
                        node_key="workflow:" + key,
                        kind="workflow",
                        ordinal=ordinal + 1,
                        status="running",
                        revision=1,
                        definition_snapshot=snapshot,
                        input_sha256=digest(args),
                        created_at=now(),
                    )
                )
            calls = tx.execute(
                select(AgentStep.node_key, AgentAttempt.delivery_no)
                .join(AgentAttempt, AgentStep.accepted_attempt_id == AgentAttempt.id)
                .where(AgentStep.run_id == run.id, AgentStep.node_key.like(f"host:{key}:%"))
                .order_by(AgentAttempt.delivery_no)
            ).all()
            return {
                "key": key,
                "stepId": identifier,
                "snapshot": snapshot,
                "clock": snapshot["clock"],
                "deliveryOrder": [int(row[0].rsplit(":", 1)[1]) for row in calls],
                "saved": saved,
            }

    async def workflow_result(self, workflow, args, result, *, rejection=None):
        prepared = await asyncio.to_thread(
            self.prepare,
            "workflow",
            "workflow:" + workflow["key"],
            args,
            workflow["snapshot"],
        )
        await self.host_result(prepared, result, notify=False, rejection=rejection)

    async def subagent_begin(self, key, prompt, model_step):
        token = None
        if self.parent.get() is None:
            token = self.parent.set(uuid5(self.run_id, "step:root/model:" + str(model_step)))
        try:
            return await asyncio.to_thread(self.prepare, "subagent", key, {"prompt": prompt}, None)
        finally:
            if token is not None:
                self.parent.reset(token)

    async def host_begin(self, workflow, number, inputs):
        return await asyncio.to_thread(
            self.prepare, "workflow_host", f"host:{workflow}:{number}", inputs, None
        )

    async def external_event(self, key, event_type, correlation_key, payload_schema):
        """A trusted workflow boundary resumes with the accepted event payload."""
        from compass_agent.runtime.hitl import HumanDecisions

        inputs = {
            "eventType": event_type,
            "correlationKey": correlation_key,
            "schema": payload_schema,
        }
        step_id, attempt_id, saved = await asyncio.to_thread(
            self.prepare, "external_event", "external:" + key, inputs, inputs
        )
        if saved is not None:
            return saved["result"]
        decisions = HumanDecisions(self.store)
        accepted = await asyncio.to_thread(
            decisions.external_result, self.run_id, self.epoch, step_id
        )
        if accepted is None:
            await self.gate()
            await asyncio.to_thread(
                decisions.await_external,
                self.run_id,
                self.epoch,
                step_id,
                event_type,
                correlation_key,
                payload_schema,
            )
            await self.publisher.flush(self.run_id)
            raise RecoveryStop("waiting_external_event")
        await self.gate()
        await asyncio.to_thread(self.dispatched, attempt_id)
        await asyncio.to_thread(
            self.accept,
            step_id,
            attempt_id,
            {"result": accepted["payload"], "budget": {}, "continuation": None},
        )
        await self.publisher.flush(self.run_id)
        return accepted["payload"]

    async def host_result(self, prepared, value, *, notify=True, rejection=None):
        step_id, attempt_id, saved = prepared
        wrapped = {"result": value, "budget": {}, "continuation": None}
        if rejection is not None:
            wrapped["rejection"] = rejection
        if saved is not None:
            if digest(saved) != digest(wrapped):
                raise RecoveryStop("workflow_replay_mismatch")
            return
        await asyncio.to_thread(self.dispatched, attempt_id)
        await asyncio.to_thread(self.accept, step_id, attempt_id, wrapped, self.business_state())
        if self.fault and notify:
            await self.fault("host_result_committed", self.run_id)

    async def gate(self):
        if self.flush_progress:
            await self.flush_progress()
        await self.publisher.flush(self.run_id)
        run = await asyncio.to_thread(self.store.read_run, self.run_id)
        if run.execution_epoch != self.epoch:
            raise RecoveryStop("execution_fenced")
        if run.status == "cancelling":
            from compass_agent.tools.contracts import StopRequested

            raise StopRequested()
        if run.control_intent == "pause":
            raise RecoveryStop("user_paused")
        if self.access:
            try:
                await self.access()
            except ModelError as error:
                # An admission failure precedes physical dispatch. Carry it through tool
                # and model adapters without manufacturing an unknown external effect.
                raise RecoveryStop(error.code) from None
        self.publisher.require_owner()

    def has_step(self, key):
        with Session(self.store.engine) as tx:
            return (
                tx.scalar(
                    select(AgentStep.id).where(
                        AgentStep.run_id == self.run_id, AgentStep.node_key == key
                    )
                )
                is not None
            )

    def prepare(self, kind, key, inputs, definition, *, frame_factory=None, receive_frame=None):
        with self.store.transaction() as tx:
            session, run = self.store.lock(tx, self.run_id)
            self.store.require_epoch(run, self.epoch)
            step = tx.scalar(
                select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.node_key == key)
            )
            frame = None
            if frame_factory is not None:
                assert receive_frame is not None
                if step:
                    if not step.prepared_context:
                        raise ModelError("checkpoint_corrupt", 409)
                    frame = self.store.private.open(run.id, "model-frame", step.prepared_context)
                else:
                    frame = frame_factory(tx)
                inputs = frame["input"]
                receive_frame(frame, step is not None)
            request_digest = digest(inputs)
            if step:
                if step.input_sha256 != request_digest or step.definition_snapshot != (
                    definition or {}
                ):
                    changes = []
                    if kind == "model" and step.accepted_attempt_id:
                        attempt = tx.get(AgentAttempt, step.accepted_attempt_id)
                        if attempt:
                            refs = attempt.receipt.get("inputMessageRefs", [])
                            messages = inputs.get("request", {}).get("messages", [])
                            changes = [
                                str(index + 1)
                                for index, (ref, message) in enumerate(
                                    zip(refs, messages, strict=False)
                                )
                                if ref != str(uuid5(run.id, "context:" + digest(message)))
                            ]
                            if len(refs) != len(messages):
                                changes.append("count")
                    logger.warning(
                        "analysis.checkpoint.mismatch run_id=%s step_id=%s kind=%s "
                        "input_changed=%s definition_changed=%s message_positions=%s",
                        run.id,
                        step.id,
                        kind,
                        step.input_sha256 != request_digest,
                        step.definition_snapshot != (definition or {}),
                        changes,
                    )
                    raise ModelError("workflow_replay_mismatch", 409)
                if step.accepted_attempt_id:
                    attempt = tx.get(AgentAttempt, step.accepted_attempt_id)
                    value = self.store.private.open(
                        run.id, "attempt-output", attempt.output_content
                    )
                    if digest(value) != attempt.output_sha256:
                        raise ModelError("checkpoint_corrupt", 409)
                    if kind == "model" and run.checkpoint_no:
                        checkpoint = tx.get(AgentCheckpoint, (run.id, run.checkpoint_no))
                        if checkpoint and checkpoint.continuation_state:
                            continuations = self.store.private.open(
                                run.id, "continuations", checkpoint.continuation_state
                            )
                            value = {**value, "continuation": continuations.get(str(attempt.id))}
                    return step.id, attempt.id, value
                self.rebuilding_search = False
                attempts = tx.scalars(
                    select(AgentAttempt)
                    .where(AgentAttempt.step_id == step.id)
                    .order_by(AgentAttempt.number)
                ).all()
                if attempts and attempts[-1].status in {"dispatched", "unknown", "incomplete"}:
                    raise RecoveryStop("execution_unknown")
                if attempts and attempts[-1].status == "prepared":
                    attempts[-1].execution_epoch = self.epoch
                    return step.id, attempts[-1].id, None
                number = len(attempts) + 1
            else:
                self.rebuilding_search = False
                step = AgentStep(
                    id=uuid5(run.id, "step:" + key),
                    run_id=run.id,
                    parent_step_id=self.parent.get(),
                    node_key=key,
                    kind=kind,
                    ordinal=(
                        tx.scalar(
                            select(func.max(AgentStep.ordinal)).where(AgentStep.run_id == run.id)
                        )
                        or 0
                    )
                    + 1,
                    status="running",
                    revision=1,
                    definition_snapshot=definition or {},
                    prepared_context=self.store.private.seal(run.id, "model-frame", frame)
                    if frame is not None
                    else None,
                    input_sha256=request_digest,
                    created_at=now(),
                )
                tx.add(step)
                tx.flush()
                number = 1
            attempt = AgentAttempt(
                id=uuid4(),
                run_id=run.id,
                step_id=step.id,
                number=number,
                execution_epoch=self.epoch,
                status="prepared",
                input_content=self.store.private.seal(run.id, "attempt-input", inputs),
                receipt={"actionId": inputs["actionId"]}
                if kind == "tool" and inputs.get("actionId")
                else {},
                budget_receipt={},
                created_at=now(),
            )
            tx.add(attempt)
            tx.flush()
            if kind == "model":
                messages = inputs.get("request", {}).get("messages", [])
                refs = []
                ordinal = (
                    tx.scalar(
                        select(func.max(AgentMessage.ordinal)).where(AgentMessage.run_id == run.id)
                    )
                    or 0
                )
                for message in messages:
                    identifier = uuid5(run.id, "context:" + digest(message))
                    if tx.get(AgentMessage, identifier) is None:
                        ordinal += 1
                        tx.add(
                            AgentMessage(
                                id=identifier,
                                run_id=run.id,
                                step_id=step.id,
                                attempt_id=attempt.id,
                                ordinal=ordinal,
                                channel="context",
                                role=message["role"],
                                status="completed",
                                revision=1,
                                private_content=self.store.private.seal(
                                    run.id, "context-message", message
                                ),
                                created_at=now(),
                                updated_at=now(),
                            )
                        )
                    refs.append(str(identifier))
                attempt.receipt = {"inputMessageRefs": refs}
                attempt.input_content = self.store.private.seal(
                    run.id,
                    "attempt-input",
                    {
                        "requestDigest": request_digest,
                        "inputMessageRefs": refs,
                        "definition": definition or {},
                    },
                )
            return step.id, attempt.id, None

    def dispatched(self, attempt_id):
        self.publisher.require_owner()
        with self.store.transaction() as tx:
            _, run = self.store.lock(tx, self.run_id)
            self.store.require_epoch(run, self.epoch)
            if run.status != "running":
                raise ModelError("run_stopped", 409)
            attempt = tx.get(AgentAttempt, attempt_id)
            attempt.status, attempt.dispatched_at = "dispatched", now()
            if attempt.receipt.get("actionId"):
                action = tx.get(AgentAction, UUID(attempt.receipt["actionId"]))
                if action is None or action.run_id != run.id or action.state != "pending":
                    raise RecoveryStop("action_conflict")
                action.state, action.updated_at = "unknown", now()

    def accept(
        self,
        step_id,
        attempt_id,
        value,
        business_state=None,
        *,
        boundary=False,
        expected_attempt_status="dispatched",
    ):
        self.publisher.require_owner()
        with self.store.transaction() as tx:
            session, run = self.store.lock(tx, self.run_id)
            return self.accept_in_transaction(
                tx,
                session,
                run,
                step_id,
                attempt_id,
                value,
                business_state=business_state,
                boundary=boundary,
                expected_attempt_status=expected_attempt_status,
            )

    def accept_in_transaction(
        self,
        tx,
        session,
        run,
        step_id,
        attempt_id,
        value,
        *,
        business_state=None,
        boundary=False,
        expected_attempt_status="dispatched",
    ):
        states = business_state or {}
        self.store.require_epoch(run, self.epoch)
        step, attempt = tx.get(AgentStep, step_id), tx.get(AgentAttempt, attempt_id)
        if self.store.distributed and self.store.scheduler:
            active_ms = self.store.scheduler.active_at_boundary(
                tx, run, boundary and step.parent_step_id is None
            )
        else:
            run.active_ms = max(
                run.active_ms, self.active_base + int((monotonic() - self.active_started) * 1000)
            )
            active_ms = run.active_ms
        if attempt.status != expected_attempt_status:
            raise ModelError("attempt_conflict", 409)
        continuations = {}
        if run.checkpoint_no:
            previous = tx.get(AgentCheckpoint, (run.id, run.checkpoint_no))
            if previous and previous.continuation_state:
                continuations = self.store.private.open(
                    run.id, "continuations", previous.continuation_state
                )
        if value.get("continuation") is not None:
            continuations[str(attempt.id)] = value["continuation"]
            value = {**value, "continuation": None}
        attempt.output_content = self.store.private.seal(run.id, "attempt-output", value)
        attempt.output_sha256 = digest(value)
        attempt.status, attempt.finished_at = "completed", now()
        if attempt.receipt.get("actionId"):
            action = tx.get(AgentAction, UUID(attempt.receipt["actionId"]))
            if action is None or action.run_id != run.id:
                raise RecoveryStop("action_conflict")
            result = value["result"]
            action.state = (
                "unknown"
                if result.get("cleanup") == "unconfirmed"
                else "applied"
                if result.get("status") in {"ok", "empty"}
                else "denied"
            )
            action.receipt = {
                "attemptId": str(attempt.id),
                "status": result.get("status"),
                "cleanup": result.get("cleanup"),
                "outputDigest": digest(result),
            }
            action.updated_at = now()
        attempt.budget_receipt = value.get("budget", {})
        attempt.delivery_no = (
            tx.scalar(
                select(func.max(AgentAttempt.delivery_no)).where(AgentAttempt.run_id == run.id)
            )
            or 0
        ) + 1
        step.accepted_attempt_id = attempt.id
        step.status = "failed" if value.get("rejection") else "completed"
        step.finished_at = now()
        step.revision += 1
        box = self.store.outbox(tx, session, run, [])
        tx.flush()
        accepted = tx.scalars(
            select(AgentAttempt).where(
                AgentAttempt.run_id == run.id, AgentAttempt.status == "completed"
            )
        ).all()
        refs = [
            {"stepId": str(a.step_id), "attemptId": str(a.id), "outputDigest": a.output_sha256}
            for a in accepted
        ]
        position = {
            "schemaVersion": 1,
            "nextNode": "journal-replay",
            "lastStepId": str(step.id),
        }
        number = (run.checkpoint_no or 0) + 1
        private = {
            "schemaVersion": 1,
            "accepted": refs,
            "states": {path: state["verifier"] for path, state in states.items()},
        }
        checkpoint = AgentCheckpoint(
            run_id=run.id,
            number=number,
            schema_version=1,
            boundary_outbox_id=box.id,
            position=position,
            completed_refs=refs,
            message_refs=[
                str(v)
                for v in tx.scalars(
                    select(AgentMessage.id).where(
                        AgentMessage.run_id == run.id,
                        (AgentMessage.channel == "conversation")
                        | AgentMessage.attempt_id.is_not(None),
                    )
                )
            ],
            tool_binding_refs=[
                str(v)
                for v in tx.scalars(
                    select(AgentToolBinding.id).where(AgentToolBinding.run_id == run.id)
                )
            ],
            artifact_refs=[
                {"id": str(v.id), "sha256": v.sha256}
                for v in tx.scalars(select(AgentArtifact).where(AgentArtifact.run_id == run.id))
            ],
            plan_state={
                "schemaVersion": 1,
                "states": {path: state["plan"] for path, state in states.items()},
            },
            verifier_state=self.store.private.seal(run.id, "verifier", private),
            continuation_state=self.store.private.seal(run.id, "continuations", continuations),
            budget_state={
                "schemaVersion": 1,
                "activeMs": active_ms,
                "receipts": [{"attemptId": str(v.id), **v.budget_receipt} for v in accepted],
                "states": {path: state["budget"] for path, state in states.items()},
            },
            pending_calls=[
                str(v)
                for v in tx.scalars(
                    select(AgentAttempt.id).where(
                        AgentAttempt.run_id == run.id,
                        AgentAttempt.status.in_(["prepared", "dispatched", "unknown"]),
                    )
                )
            ],
            state_sha256="",
            created_at=now(),
        )
        checkpoint.state_sha256 = checkpoint_digest(checkpoint)
        tx.add(checkpoint)
        run.checkpoint_no = number
        self.last_attempt = attempt.id
        if boundary and self.store.scheduler:
            return self.store.scheduler.accepted_boundary(tx, run, step)
        return False

    def unknown(self, attempt_id, *, sandbox_execution_id=None):
        with self.store.transaction() as tx:
            _, run = self.store.lock(tx, self.run_id)
            attempt = tx.get(AgentAttempt, attempt_id)
            if attempt is None or attempt.run_id != run.id or attempt.execution_epoch != self.epoch:
                return
            if run.status == "cancelling" and sandbox_execution_id:
                attempt.receipt = {**attempt.receipt, "sandboxExecutionId": sandbox_execution_id}
            if run.execution_epoch == self.epoch and attempt.status == "dispatched":
                attempt.status, attempt.error_code = "unknown", "execution_unknown"

    def confirm_canceled_attempt(self, attempt_id, *, before_dispatch=False):
        # A fenced callback may confirm cleanup, but cannot commit output or a checkpoint.
        with self.store.transaction() as tx:
            _, run = self.store.lock(tx, self.run_id)
            if run.status != "cancelling":
                return
            attempt = tx.get(AgentAttempt, attempt_id)
            if (
                attempt is None
                or attempt.run_id != run.id
                or attempt.execution_epoch != self.epoch
                or attempt.status not in {"prepared", "dispatched", "unknown", "incomplete"}
            ):
                return
            action_id = attempt.receipt.get("actionId")
            if action_id:
                if not before_dispatch:
                    return
                action = tx.get(AgentAction, UUID(action_id))
                if action and action.run_id == run.id and action.state in {"pending", "unknown"}:
                    action.state, action.updated_at = "denied", now()
            attempt.status, attempt.finished_at = "canceled", now()
            attempt.error_code = "user_canceled"

    async def local_read(self, key, inputs, operation, *, kind="tool", yield_boundary=True):
        step, attempt, saved = await asyncio.to_thread(
            self.prepare, kind, key, inputs, {"environment": "in_process"}
        )
        if saved is not None:
            return saved["result"]
        await self.gate()
        result = await operation()
        state = self.business_state()

        def accept():
            with self.store.transaction() as tx:
                session, run = self.store.lock(tx, self.run_id)
                return self.accept_in_transaction(
                    tx,
                    session,
                    run,
                    step,
                    attempt,
                    {"result": result, "continuation": None, "budget": {}},
                    business_state=state,
                    boundary=yield_boundary,
                    expected_attempt_status="prepared",
                )

        if await asyncio.to_thread(accept):
            raise BoundaryYield()
        return result

    async def call(
        self,
        kind,
        key,
        inputs,
        operation,
        result_type,
        *,
        definition=None,
        budget=None,
        frame_factory=None,
        receive_frame=None,
        revalidate=None,
        yield_boundary=True,
        settled_on_return=False,
    ):
        adapter = TypeAdapter(result_type)
        # Read accepted results before pause checks: pure reconstruction consumes no new resources.
        try:
            options = (
                {"frame_factory": frame_factory, "receive_frame": receive_frame}
                if frame_factory is not None
                else {}
            )
            step_id, attempt_id, saved = await asyncio.to_thread(
                self.prepare, kind, key, inputs, definition, **options
            )
            budget_fields = ("steps", "tokens", "cost_microusd", "attempts", "unknown_attempts")
            if saved is not None:
                if revalidate:
                    await revalidate()
                self.last_attempt = attempt_id
                already_counted = (
                    kind in {"ctx_select", "ctx_compact"}
                    and attempt_id in self.context_accounted_attempts
                )
                if budget is not None and not already_counted:
                    for name, value in saved.get("budget", {}).items():
                        setattr(budget, name, getattr(budget, name) + value)
                    # Admission can fail without spending anything. Its stop reason
                    # still determines the child delivery and must survive replay.
                    reason = saved.get("budgetStopReason")
                    if (
                        "budgetStopReason" not in saved
                        and isinstance(saved["result"], dict)
                        and saved["result"].get("error_code") == "autonomy_admission_denied"
                    ):
                        # Receipts predating stop-reason persistence retain the
                        # admission failure, but not its token/cost classification.
                        reason = "model_budget_unavailable"
                    if reason is not None:
                        budget.reason = reason
                    if kind in {"ctx_select", "ctx_compact"}:
                        self.context_accounted_attempts.add(attempt_id)
                saved = self.restore_continuation(saved["result"], saved.get("continuation"))
                return adapter.validate_json(__import__("json").dumps(saved))
            await self.gate()
            if revalidate:
                await revalidate()
            before = {name: getattr(budget, name) for name in budget_fields} if budget else {}
        except asyncio.CancelledError:
            # Only local admission and replay ran; the physical dispatch intent has
            # not been committed and operation() has never been invoked.
            raise PhaseCanceled(cleanup_confirmed=True) from None
        # The routing host only combines journaled classifiers and local metadata.
        # Its prepared attempt can be reconstructed until the final route is accepted.
        if kind != "routing_host":
            dispatch = asyncio.create_task(asyncio.to_thread(self.dispatched, attempt_id))
            try:
                await asyncio.shield(dispatch)
            except asyncio.CancelledError:
                try:
                    await dispatch
                except ModelError as error:
                    if error.code not in {"execution_fenced", "lease_lost", "run_stopped"}:
                        raise
                await asyncio.to_thread(
                    self.confirm_canceled_attempt, attempt_id, before_dispatch=True
                )
                raise PhaseCanceled(cleanup_confirmed=True) from None
        parent_token, attempt_token = self.parent.set(step_id), self.active_attempt.set(attempt_id)
        tool_finished = False
        model_finished = False
        sandbox_execution_id = None
        try:
            with execution_scope(key, absolute=True):
                result = await operation()
            encoded = adapter.dump_python(result, mode="json")
            model_finished = (
                kind == "model"
                and isinstance(encoded, dict)
                and encoded.get("status") == "completed"
                and not encoded.get("error_code")
            )
            if kind == "tool":
                receipt = encoded.get("receipt", encoded) if isinstance(encoded, dict) else encoded
                receipts = receipt if isinstance(receipt, list) else [receipt]
                tool_finished = settled_on_return or all(
                    isinstance(receipt, dict)
                    and receipt.get("cleanup") in {"confirmed", "not_started", "remote_managed"}
                    for receipt in receipts
                )
                if (
                    isinstance(encoded, dict)
                    and encoded.get("reason_code") == "sandbox_cleanup_unconfirmed"
                ):
                    sandbox_execution_id = encoded.get("execution_id")
            continuation = None
            if (
                isinstance(encoded, dict)
                and encoded.get("continuation_token")
                and self.model_secrets
            ):
                continuation = self.model_secrets.open(
                    encoded["continuation_token"], "tool_continuation", ttl=900
                )
                encoded["continuation_token"] = None
            encoded = {
                "result": encoded,
                "continuation": continuation,
                "budget": {name: getattr(budget, name) - old for name, old in before.items()},
                **({"budgetStopReason": budget.reason} if budget is not None else {}),
            }
            yielded = await asyncio.to_thread(
                self.accept,
                step_id,
                attempt_id,
                encoded,
                self.business_state(),
                boundary=yield_boundary,
                **({"expected_attempt_status": "prepared"} if kind == "routing_host" else {}),
            )
            self.last_attempt = attempt_id
            if kind in {"ctx_select", "ctx_compact"} and budget is not None:
                self.context_accounted_attempts.add(attempt_id)
            await self.publisher.flush(self.run_id)
            if yielded:
                if self.flush_progress:
                    await self.flush_progress()
                raise BoundaryYield()
            return result
        except BaseException as error:
            if tool_finished or (
                kind == "tool" and isinstance(error, PhaseCanceled) and error.cleanup_confirmed
            ):
                await asyncio.to_thread(self.confirm_canceled_attempt, attempt_id)
            await asyncio.shield(
                asyncio.to_thread(
                    self.unknown, attempt_id, sandbox_execution_id=sandbox_execution_id
                )
            )
            if type(error) is asyncio.CancelledError and (model_finished or tool_finished):
                raise PhaseCanceled(cleanup_confirmed=True) from None
            raise
        finally:
            self.parent.reset(parent_token)
            self.active_attempt.reset(attempt_token)

    def validate_checkpoint(self):
        with Session(self.store.engine) as tx:
            run = tx.get(AgentRun, self.run_id)
            assert run is not None
            unknown = tx.scalar(
                select(AgentAttempt.id)
                .join(AgentStep, AgentStep.id == AgentAttempt.step_id)
                .where(
                    AgentAttempt.run_id == run.id,
                    AgentAttempt.status.in_(["dispatched", "unknown", "incomplete"]),
                    AgentStep.kind != "workflow_host",
                )
                .limit(1)
            )
            if unknown:
                raise ModelError("execution_unknown", 409)
            if run.checkpoint_no is None:
                return
            checkpoint = tx.get(AgentCheckpoint, (run.id, run.checkpoint_no))
            if (
                checkpoint is None
                or checkpoint.schema_version != 1
                or checkpoint.state_sha256 != checkpoint_digest(checkpoint)
            ):
                raise ModelError("checkpoint_invalid", 409)
            self.store.private.open(run.id, "verifier", checkpoint.verifier_state)
            if checkpoint.continuation_state:
                self.store.private.open(run.id, "continuations", checkpoint.continuation_state)
            for identifier in checkpoint.message_refs:
                message = tx.get(AgentMessage, UUID(identifier))
                if message is None or message.run_id != run.id:
                    raise ModelError("checkpoint_invalid", 409)
                if message.private_content and message.attempt_id is not None:
                    self.store.private.open(run.id, "context-message", message.private_content)
            for identifier in checkpoint.tool_binding_refs:
                binding = tx.get(AgentToolBinding, UUID(identifier))
                if (
                    binding is None
                    or binding.run_id != run.id
                    or digest(binding.declaration) != binding.schema_sha256
                ):
                    raise ModelError("checkpoint_invalid", 409)
            for ref in checkpoint.artifact_refs:
                artifact = tx.get(AgentArtifact, UUID(ref["id"]))
                if (
                    artifact is None
                    or artifact.run_id != run.id
                    or sha256(artifact.content).hexdigest() != ref["sha256"]
                ):
                    raise ModelError("checkpoint_invalid", 409)
            for ref in checkpoint.completed_refs:
                attempt = tx.get(AgentAttempt, UUID(ref["attemptId"]))
                if (
                    attempt is None
                    or attempt.run_id != run.id
                    or attempt.output_sha256 != ref["outputDigest"]
                ):
                    raise ModelError("checkpoint_invalid", 409)
                value = self.store.private.open(run.id, "attempt-output", attempt.output_content)
                if digest(value) != ref["outputDigest"]:
                    raise ModelError("checkpoint_corrupt", 409)

    def restore_continuation(self, value, continuation):
        if continuation is not None:
            if self.model_secrets is None:
                raise ModelError("runtime_state_invalid", 409)
            value = dict(value)
            value["continuation_token"] = self.model_secrets.seal("tool_continuation", continuation)
        return value
