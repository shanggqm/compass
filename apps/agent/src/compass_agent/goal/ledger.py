"""Encrypted progress with source authorization and idempotent evidence import."""

from decimal import Decimal
from uuid import UUID, uuid5

from sqlalchemy import func, select

from compass_agent.context_runtime.source_access import (
    allows,
    read_private,
    record_sources_locked,
    save_private,
)
from compass_agent.execution_context import execution_path
from compass_agent.goal.contracts import LEDGER_LIMIT
from compass_agent.models.errors import ModelError
from compass_agent.runtime.entities import AgentGoal, AgentGoalEntry
from compass_agent.runtime.private_state import digest
from compass_agent.runtime.store import now
from compass_agent.tools.contracts import json_bytes


def bound_goal(tx, run, *, locked=True):
    identity = (run.config_snapshot.get("goal") or {}).get("goalId")
    return tx.get(AgentGoal, UUID(identity), with_for_update=locked) if identity else None


LEDGER_SOURCE_LIMIT = 256
LEDGER_SOURCE_BYTES = 512 * 1024


def sources_within_limit(refs):
    # Evidence conservatively retains prior authorized inputs, including default terms.
    return len(refs) <= LEDGER_SOURCE_LIMIT and len(json_bytes(refs)) <= LEDGER_SOURCE_BYTES


def unique_refs(records):
    refs = []
    for record in records:
        for ref in record.get("sourceRefs", []):
            if ref not in refs:
                refs.append(ref)
    return refs


def evidence_closure(records, available):
    retained = {record["id"]: record for record in records}
    pending = list(records)
    while pending:
        for identifier in pending.pop().get("parentEvidenceIds", []):
            if identifier in retained:
                continue
            parent = available.get(identifier)
            if parent is None:
                raise ModelError("evidence_required", 422)
            retained[identifier] = parent
            pending.append(parent)
    return list(retained.values())


class Ledger:
    def __init__(self, store):
        self.store = store

    def rows(self, tx, goal):
        return [
            (row, self.store.private.open(goal.id, "ledger", row.private_body))
            for row in tx.scalars(
                select(AgentGoalEntry)
                .where(AgentGoalEntry.goal_id == goal.id)
                .order_by(AgentGoalEntry.updated_at, AgentGoalEntry.entry_key)
            )
        ]

    def visible(self, tx, goal, context, *, epoch=None):
        rows = self.rows(tx, goal)
        # One live authorization pass for overlapping inputs; denied sets retain
        # per-entry filtering so an unrelated inaccessible source hides no extra work.
        shared_refs = unique_refs([body for row, body in rows if row.status != "dropped"])
        all_visible = allows(self.store, context, shared_refs, epoch=epoch)
        hidden = {
            row.entry_key
            for row, body in rows
            if row.status == "dropped"
            or (
                not all_visible
                and not allows(self.store, context, body.get("sourceRefs"), epoch=epoch)
            )
        }
        while True:
            derived = {
                row.entry_key
                for row, body in rows
                if hidden.intersection(body.get("dependsOn", []))
            } - hidden
            if not derived:
                break
            hidden |= derived
        return [(row, body) for row, body in rows if row.entry_key not in hidden]

    def upsert(self, tx, goal, key, kind, status, body):
        if not sources_within_limit(body.get("sourceRefs", [])):
            raise ModelError("ledger_source_limit", 422)
        entry = tx.get(AgentGoalEntry, (goal.id, key))
        if entry and (
            entry.kind,
            entry.status,
            self.store.private.open(goal.id, "ledger", entry.private_body),
        ) == (kind, status, body):
            return entry
        if entry is None:
            count = tx.scalar(
                select(func.count())
                .select_from(AgentGoalEntry)
                .where(AgentGoalEntry.goal_id == goal.id)
            )
            if count >= LEDGER_LIMIT:
                raise ModelError("ledger_full", 409)
            entry = AgentGoalEntry(goal_id=goal.id, entry_key=key)
            tx.add(entry)
        entry.kind, entry.status = kind, status
        entry.private_body = self.store.private.seal(goal.id, "ledger", body)
        entry.updated_at = goal.updated_at = now()
        goal.ledger_revision += 1
        tx.flush()
        return entry

    def record(self, tx, goal, items, journal, trusted):
        evidence = getattr(journal, "answer_evidence", {}).get(execution_path.get(), {})
        acceptance = {item["id"] for item in goal.contract["acceptance"]}
        for item in items:
            previous = tx.get(AgentGoalEntry, (goal.id, item.key))
            if item.type == "todo_done":
                if previous is None or previous.kind != "todo":
                    raise ModelError("goal_todo_not_found", 422)
                body = self.store.private.open(goal.id, "ledger", previous.private_body)
                self.upsert(tx, goal, item.key, "todo", "done", body)
                continue
            if previous and previous.kind != item.type:
                raise ModelError("goal_entry_kind_conflict", 409)
            if not set(item.acceptance_ids) <= acceptance:
                raise ModelError("goal_acceptance_unknown", 422)
            records = [evidence.get(key) for key in item.evidence_ids]
            if (item.type == "conclusion" and not records) or any(r is None for r in records):
                raise ModelError("evidence_required", 422)
            records = evidence_closure(records, evidence)
            refs = unique_refs(records)
            if not allows(self.store, trusted, refs, epoch=journal.epoch):
                raise ModelError("evidence_required", 422)
            body = {
                **item.model_dump(mode="json", by_alias=True),
                "sourceRefs": refs,
                "evidence": records,
                **({"runId": str(journal.run_id)} if records else {}),
            }
            # References to existing ledger entries must remain within this goal.
            for key in item.depends_on:
                if tx.get(AgentGoalEntry, (goal.id, key)) is None:
                    raise ModelError("goal_dependency_unknown", 422)
            status = "open" if item.type == "todo" else "done"
            self.upsert(tx, goal, item.key, item.type, status, body)
            if item.type == "conclusion":
                for key in item.resolves:
                    prior = tx.get(AgentGoalEntry, (goal.id, key))
                    if (
                        prior is None
                        or prior.status not in {"conflict", "stale"}
                        or key == item.key
                    ):
                        raise ModelError("goal_resolution_invalid", 422)
                    old = self.store.private.open(goal.id, "ledger", prior.private_body)
                    self.upsert(
                        tx, goal, key, prior.kind, "dropped", {**old, "resolvedBy": item.key}
                    )
                self.mark_conflicts(tx, goal, item.key, body)

    def mark_conflicts(self, tx, goal, key, body):
        if not body.get("factKey") or body.get("value") is None:
            return
        for entry, prior in self.rows(tx, goal):
            if (
                entry.entry_key == key
                or entry.kind != "conclusion"
                or entry.status not in {"done", "conflict"}
            ):
                continue
            if prior.get("factKey") != body["factKey"] or prior.get("value") is None:
                continue
            if (Decimal(prior["value"]), prior.get("unit")) == (
                Decimal(body["value"]),
                body.get("unit"),
            ):
                continue
            for name, record, other in (
                (entry.entry_key, prior, key),
                (key, body, entry.entry_key),
            ):
                updated = {
                    **record,
                    "conflictsWith": sorted(set(record.get("conflictsWith", [])) | {other}),
                }
                self.upsert(tx, goal, name, "conclusion", "conflict", updated)

    def remember(self, tx, run, journal, call, result, entry, records, context=None):
        if not entry or not entry.fingerprint or result.status not in {"ok", "empty"}:
            return
        goal = bound_goal(tx, run)
        if goal is None or goal.status != "active":
            return
        import json

        args = json.loads(call.function.arguments)
        identity = entry.fingerprint(args, context)
        if identity is None or not isinstance(result.payload, dict) or result.payload.get("reused"):
            return
        fingerprint, kind = identity
        if len(json_bytes(result.payload)) > 16 * 1024:
            return
        manifest = result.context_result
        if not records and not manifest:
            return
        records = evidence_closure(
            records, getattr(journal, "answer_evidence", {}).get(execution_path.get(), {})
        )
        refs = unique_refs(records) or (manifest or {}).get("sourceDeps", [])
        if not refs and manifest:
            return
        if not sources_within_limit(refs):
            return
        body = {
            "title": entry.name,
            "result": result.payload,
            "contextResult": manifest,
            "artifacts": [
                {
                    "artifactId": ref.id,
                    "runId": str(ref.run_id),
                    "name": ref.name,
                    "sha256": ref.sha256,
                    "mediaType": ref.media_type,
                }
                for ref in result.artifacts
            ],
            "evidence": records,
            "sourceRefs": refs,
            "runId": str(run.id),
            "dependsOn": [],
            "dataVersion": {
                r["connection"]: r["revision"]
                for r in refs
                if r.get("connection") and "revision" in r
            },
        }
        self.upsert(tx, goal, f"{kind}:{fingerprint}", kind, "done", body)

    def reuse(self, journal, prepared, context):
        if prepared.tool.fingerprint is None:
            return None
        identity = prepared.tool.fingerprint(prepared.args, context)
        if identity is None:
            return None
        fingerprint, kind = identity
        with self.store.transaction() as tx:
            _, run = self.store.lock(tx, journal.run_id, context.trusted)
            self.store.require_epoch(run, journal.epoch)
            goal = bound_goal(tx, run)
            if goal is None or goal.status != "active":
                return None
            entry = tx.get(AgentGoalEntry, (goal.id, f"{kind}:{fingerprint}"))
            if entry is None or entry.status != "done":
                return None
            body = self.store.private.open(goal.id, "ledger", entry.private_body)
            refs = body.get("sourceRefs", [])
            if any(ref.get("kind") == "transient" for ref in refs):
                return None
            if not allows(self.store, context.trusted, refs, raise_unavailable=True):
                return None
            records = body.get("evidence", [])
            if not records and not body.get("contextResult"):
                return None
            identifiers = []
            record_sources_locked(self.store, tx, run, refs, journal.epoch)
            replacements = {
                record["id"]: str(
                    uuid5(run.id, f"goal-evidence:{entry.entry_key}:{digest(record)}:{index}")
                )
                for index, record in enumerate(records)
            }
            for record in records:
                identifier = replacements[record["id"]]
                imported = {**record, "id": identifier, "originRunId": body["runId"]}
                if record.get("parentEvidenceIds"):
                    imported["parentEvidenceIds"] = [
                        replacements[key] for key in record["parentEvidenceIds"]
                    ]
                save_private(
                    self.store, tx, run, f"answer/v2/evidence/{identifier}", imported, refs
                )
                identifiers.append(identifier)
            key = f"goal/reuse/{execution_path.get()}/{context.step_id}/{prepared.call_id}"
            if read_private(self.store, tx, run.id, key) is None:
                save_private(self.store, tx, run, key, {"entry": entry.entry_key}, refs)
                goal.runtime_state = {
                    **goal.runtime_state,
                    "reusedCalls": goal.runtime_state.get("reusedCalls", 0) + 1,
                }
            payload = {**body["result"], "reused": True, "evidenceIds": identifiers}
            if body.get("artifacts"):
                payload["artifacts"] = body["artifacts"]
            if (
                prepared.tool.name == "database_query"
                and "rows" in payload
                and payload.get("resultId")
            ):
                from compass_agent.tools.contracts import AuthorizedInput

                async def revalidate():
                    import asyncio

                    return await asyncio.to_thread(
                        allows, self.store, context.trusted, refs, raise_unavailable=True
                    )

                context.sources[payload["resultId"]] = AuthorizedInput(
                    run.id, context.owner, payload, revalidate
                )
            from compass_agent.tools.contracts import ToolExecutionResult

            return ToolExecutionResult(
                prepared.call_id, "ok", payload=payload, context_result=body.get("contextResult")
            )
