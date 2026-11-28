"""Bounded, non-blocking projection. Snapshot reads use a separate synchronous audit."""

import json
import logging
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from queue import Empty, Full, Queue
from threading import Event, Thread
from uuid import uuid4, uuid5

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from compass_agent.context_ops.compiler import restore
from compass_agent.context_ops.crypto import SnapshotCipher
from compass_agent.context_ops.entities import (
    ContextDecisionSummary,
    ContextSecurityEvent,
    ContextSnapshot,
)
from compass_agent.context_security.policy import derive_lineage
from compass_agent.context_security.redactor import storage_value
from compass_agent.runtime.entities import AgentMessage

active_capture: ContextVar[tuple | None] = ContextVar("contextops_capture", default=None)


@contextmanager
def capture(journal, trusted, receipt):
    token = active_capture.set((journal, trusted, receipt))
    try:
        yield
    finally:
        active_capture.reset(token)


def security_fallback(events, *, reason):
    logger = logging.getLogger("compass.context_security")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
        logger.propagate = False
    for event in events:
        logger.warning(
            json.dumps(
                {"event": "context_security", "reason": reason, **event}, separators=(",", ":")
            )
        )


def audit(
    tx,
    context,
    event_type,
    action,
    rule,
    *,
    run_id=None,
    step_id=None,
    target_ref=None,
    hit_count=1,
    source_kind=None,
    trust_level=None,
    trace_id=None,
    identifier=None,
):
    row = ContextSecurityEvent(
        id=identifier or uuid4(),
        tenant_id=context.scope.tenant_id,
        workspace_id=context.scope.workspace_id,
        actor_id=context.actor_user_id,
        run_id=run_id,
        step_id=step_id,
        trace_id=trace_id,
        event_type=event_type,
        action=action,
        rule_code=rule,
        hit_count=hit_count,
        source_kind=source_kind,
        trust_level=trust_level,
        target_ref=target_ref,
        metadata_json={},
        created_at=datetime.now(UTC),
    )
    tx.add(row)
    return row


def projection(payload):
    frozen = payload["frozen"]
    kept = set(frozen["selected_ids"])
    blocked = payload.get("blocked", {})
    by_source = {}
    decisions = []
    for item in frozen["candidates"]:
        kind = item["kind"]
        value = by_source.setdefault(
            kind, {"source": kind, "candidate_tokens": 0, "selected_tokens": 0}
        )
        value["candidate_tokens"] += item["byte_bound"]
        selected = item["item_id"] in kept and item["item_id"] not in blocked
        if selected:
            value["selected_tokens"] += item["byte_bound"]
        decisions.append(
            {
                "item_id": item["item_id"],
                "kind": kind,
                "source": item["source"],
                "trust": item["trust"],
                "decision": "selected" if selected else "omitted",
                "reason": blocked.get(
                    item["item_id"],
                    "required" if item["required"] else "in_budget" if selected else "budget",
                ),
                "byte_bound": item["byte_bound"],
                "access": item["access"],
            }
        )
    n, count = len(decisions), sum(d["decision"] == "selected" for d in decisions)
    safe = n - len(blocked)
    funnel = [
        {"stage": stage, "count": amount}
        for stage, amount in (
            ("candidate", n),
            ("filtered", safe),
            ("ranked", safe),
            ("compacted", count),
            ("window", count),
        )
    ]
    return decisions, list(by_source.values()), funnel


class ContextOpsRecorder:
    def __init__(self, engine, settings, *, capacity=32):
        self.engine, self.settings = engine, settings
        self.queue = Queue(maxsize=capacity)
        self.closed = Event()
        self.errors = self.dropped = 0
        self.worker = Thread(target=self._consume, name="contextops-projection", daemon=True)
        self.worker.start()

    def submit(self, context, run_id, step_id, receipt, *, call_id=None, trace_id=None):
        payload = receipt.get("contextOps")
        if not payload:
            return
        item = (context, run_id, step_id, deepcopy(receipt), call_id, trace_id)
        try:
            if self.closed.is_set():
                raise Full
            self.queue.put_nowait(item)
        except Full:
            self.dropped += 1
            security_fallback(payload.get("security_events", []), reason="queue_full")

    def _consume(self):
        while not self.closed.is_set() or not self.queue.empty():
            try:
                args = self.queue.get(timeout=0.05)
            except Empty:
                continue
            try:
                self.record(*args)
            except Exception:
                self.errors += 1
                security_fallback(
                    args[3]["contextOps"].get("security_events", []),
                    reason="projection_unavailable",
                )
            finally:
                self.queue.task_done()

    def flush(self):
        self.queue.join()

    def close(self):
        self.closed.set()
        self.worker.join(timeout=5)

    def record(self, context, run_id, step_id, receipt, call_id=None, trace_id=None):
        payload = storage_value(receipt["contextOps"])
        identifier = uuid5(run_id, "contextops:" + str(step_id))
        ref = "csnp_" + identifier.hex
        frozen = payload["frozen"]
        lineage = derive_lineage(restore(frozen))
        decisions, sources, funnel = projection(payload)
        now = datetime.now(UTC)
        with Session(self.engine) as tx, tx.begin():
            if self.engine.dialect.name == "postgresql":
                tx.execute(text("SET LOCAL statement_timeout = '1500ms'"))
                tx.execute(text("SET LOCAL lock_timeout = '500ms'"))
            if tx.get(ContextDecisionSummary, identifier):
                return
            message = tx.scalar(
                select(AgentMessage).where(
                    AgentMessage.run_id == run_id,
                    AgentMessage.channel == "conversation",
                    AgentMessage.role == "assistant",
                )
            )
            refs = list(message.source_refs or []) if message else []
            for source in receipt.get("sourceDeps", []):
                if source not in refs:
                    refs.append(source)
            scope = {"access_refs": [asdict(r) for r in lineage.access_scope], "source_refs": refs}
            snapshot = {
                **frozen,
                "toolAssembly": receipt.get("toolAssembly"),
                "policy_digest": receipt.get("policyDigest", ""),
                "prompt_generation": receipt.get("generation", 0),
                "semantic_version": receipt.get("dictionaryDigest"),
                "security_events": payload.get("security_events", []),
                "blocked": payload.get("blocked", {}),
            }
            ciphertext, digest = SnapshotCipher(
                self.settings.compass_model_encryption_key
            ).encrypt_json(snapshot)
            tx.add(
                ContextSnapshot(
                    snapshot_ref=ref,
                    tenant_id=context.scope.tenant_id,
                    workspace_id=context.scope.workspace_id,
                    run_id=run_id,
                    step_id=step_id,
                    content_sha256=digest,
                    min_trust_level=lineage.min_trust_level,
                    access_scope_json=scope,
                    data_classification="restricted"
                    if refs or lineage.access_scope
                    else lineage.data_classification,
                    ciphertext=ciphertext,
                    created_at=now,
                    expires_at=now
                    + timedelta(days=self.settings.compass_context_snapshot_ttl_days),
                )
            )
            events = payload.get("security_events", [])
            actions = Counter()
            for index, event in enumerate(events):
                actions[event["action"]] += event["hit_count"]
                audit(
                    tx,
                    context,
                    event["event_type"],
                    event["action"],
                    event["rule_code"],
                    run_id=run_id,
                    step_id=step_id,
                    trace_id=trace_id,
                    hit_count=event["hit_count"],
                    source_kind=event.get("source_kind"),
                    trust_level=event.get("trust_level"),
                    identifier=uuid5(identifier, str(index)),
                )
            count = sum(d["decision"] == "selected" for d in decisions)
            budget = receipt["budget"]
            tx.add(
                ContextDecisionSummary(
                    id=identifier,
                    tenant_id=context.scope.tenant_id,
                    workspace_id=context.scope.workspace_id,
                    run_id=run_id,
                    step_id=step_id,
                    call_id=call_id,
                    trace_id=trace_id,
                    snapshot_ref=ref,
                    scene_id=receipt.get("scene") or "unclassified",
                    task=receipt.get("task") or "identify",
                    phase="generate" if payload.get("reviewing") else receipt["phase"],
                    policy_digest=receipt.get("policyDigest", ""),
                    prompt_generation=receipt["generation"],
                    semantic_version=receipt.get("dictionaryDigest"),
                    duration_ms=payload.get("duration_ms", 0),
                    tokens_candidate=budget["before"],
                    tokens_selected=sum(s["selected_tokens"] for s in sources),
                    tokens_after_compaction=budget["after"],
                    token_limit=budget["limit"],
                    cache_hit_tokens=None,
                    candidate_count=len(decisions),
                    selected_count=count,
                    omitted_count=len(decisions) - count,
                    security_blocked_count=actions["block"] + actions["deny"],
                    redacted_count=actions["redact"],
                    compaction_applied=bool(
                        receipt.get("compactions")
                        or receipt.get("contextOptimization", {}).get("compacted")
                    ),
                    status="blocked" if actions["block"] + actions["deny"] else "compiled",
                    error_code=None,
                    source_breakdown_json=sources,
                    funnel_json=funnel,
                    decisions_json={"items": decisions, "toolAssembly": receipt["toolAssembly"]}
                    if receipt.get("toolAssembly")
                    else decisions,
                    created_at=now,
                )
            )


def record_logical(observation, call_id):
    current = active_capture.get()
    if current is None:
        return
    journal, trusted, receipt = current
    from compass_agent.observability.context_spans import emit_context_spans

    step_id = uuid5(journal.run_id, "step:" + receipt["stepKey"])
    ref = "csnp_" + uuid5(journal.run_id, "contextops:" + str(step_id)).hex
    emit_context_spans(observation, receipt, ref)
    recorder = getattr(journal.store, "contextops", None)
    if recorder:
        trace_id = observation.headers().get("X-Compass-Trace-Id") if observation else None
        recorder.submit(
            trusted, journal.run_id, step_id, receipt, call_id=call_id, trace_id=trace_id
        )
