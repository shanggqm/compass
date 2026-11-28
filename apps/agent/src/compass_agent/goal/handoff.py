"""Resolve only delegated inputs and validate child-owned final evidence."""

import json
from uuid import UUID, uuid5

from compass_agent.context_runtime.source_access import (
    allows,
    assistant_message,
    read_private,
    record_sources_locked,
    require_run_sources,
    save_private,
)
from compass_agent.data_connections.connections import DataConnections
from compass_agent.documents.client import restrict_connections, verify
from compass_agent.execution_context import current_execution, execution_path
from compass_agent.goal.contracts import ChildDelivery, ChildReturn, LedgerItem
from compass_agent.goal.ledger import Ledger, bound_goal
from compass_agent.models.errors import ModelError
from compass_agent.runtime.entities import AgentArtifact, AgentAttempt, AgentStep
from compass_agent.runtime.private_state import digest
from compass_agent.tools.contracts import ToolRejected


def admit_delegation(journal, tasks, context, names, budget):
    if journal is not None:
        catalog = journal.catalog_declarations()
        if catalog is not None:
            names = names.intersection(catalog)
    if any(task.tools and not set(task.tools) <= names for task in tasks):
        raise ToolRejected("delegation_out_of_scope")
    if budget and not budget.can_slice([t.max_steps for t in tasks[:2]]):
        raise ToolRejected("delegation_budget_exceeded")
    if journal is None:
        if any(task.inputs or task.connection_ids for task in tasks):
            raise ToolRejected("delegation_input_forbidden")
        return [task.model_dump(mode="json", by_alias=True) for task in tasks]
    snapshots = []
    for task in tasks:
        snapshots.append(resolve_task(journal, task, context))
    return snapshots


def resolve_task(journal, task, context):
    if task.connection_ids:
        if (
            context.connection_ids is not None
            and not set(task.connection_ids) <= context.connection_ids
        ):
            raise ToolRejected("delegation_out_of_scope")
        search = getattr(journal, "search_snapshot", None)
        if search and (
            "detail" not in search["kinds"]
            or not verify(
                journal.store.source_settings, context.trusted, journal.run_id, kinds=["detail"]
            )
        ):
            raise ToolRejected("delegation_out_of_scope")
        bindings = DataConnections(
            journal.store.engine, journal.store.source_settings
        ).bindings_for_run(context.trusted.scope)
        allowed = {
            str(b.connection_id or b.workspace_id) for b in restrict_connections(bindings, search)
        }
        if not set(task.connection_ids) <= allowed:
            raise ToolRejected("delegation_out_of_scope")
    with journal.store.transaction() as tx:
        _, run = journal.store.lock(tx, journal.run_id, context.trusted)
        journal.store.require_epoch(run, journal.epoch)
        base = f"goal/delegation/{execution_path.get()}/{context.step_id}/{context.call_id}"
        key = f"{base}/{task.name}"
        saved = read_private(journal.store, tx, run.id, key)
        request = task.model_dump(mode="json", by_alias=True)
        if saved:
            if saved["request"] != request:
                raise ToolRejected("workflow_replay_mismatch")
            # Current permissions still gate replay of an immutable delegation snapshot.
            if not allows(journal.store, context.trusted, saved["sourceRefs"], epoch=journal.epoch):
                raise ToolRejected("delegation_input_forbidden")
            record_sources_locked(journal.store, tx, run, saved["sourceRefs"], journal.epoch)
            return saved["task"]
        goal = bound_goal(tx, run)
        entries = (
            {
                e.entry_key: (e, b)
                for e, b in Ledger(journal.store).visible(
                    tx, goal, context.trusted, epoch=journal.epoch
                )
            }
            if goal
            else {}
        )
        parent_evidence = getattr(journal, "answer_evidence", {}).get(execution_path.get(), {})
        resolved, refs = [], []
        for ref in task.inputs:
            body = None
            if ref.kind == "ledger" and ref.id in entries:
                entry, value = entries[ref.id]
                body = (
                    value
                    if entry.status == "done" or entry.kind == "todo"
                    else {
                        "title": value["title"],
                        "status": "已失效，需补查",
                        "sourceRefs": value.get("sourceRefs", []),
                    }
                )
            elif ref.kind == "evidence":
                body = parent_evidence.get(ref.id)
            elif ref.kind == "artifact":
                try:
                    artifact = tx.get(AgentArtifact, UUID(ref.id))
                except ValueError:
                    artifact = None
                if artifact and artifact.run_id == run.id:
                    body = {
                        "artifactId": ref.id,
                        "name": artifact.name,
                        "mediaType": artifact.media_type,
                        "sha256": artifact.sha256,
                        "sourceRefs": list(assistant_message(tx, run.id).source_refs or []),
                    }
                    if artifact.media_type != "image/png":
                        body["preview"] = artifact.content[: 16 * 1024].decode(
                            "utf-8", errors="ignore"
                        )
                        body["truncated"] = artifact.size_bytes > 16 * 1024
            if body is None or not allows(
                journal.store, context.trusted, body.get("sourceRefs"), epoch=journal.epoch
            ):
                raise ToolRejected("delegation_input_forbidden")
            if context.connection_ids is not None and any(
                ref.get("connection") and ref["connection"] not in context.connection_ids
                for ref in body.get("sourceRefs", [])
            ):
                raise ToolRejected("delegation_input_forbidden")
            refs.extend(r for r in body.get("sourceRefs", []) if r not in refs)
            resolved.append({"kind": ref.kind, "id": ref.id, "content": body})
        snapshot = {**request, "inputs": resolved}
        if len(json.dumps(snapshot, ensure_ascii=False).encode()) > 64 * 1024:
            raise ToolRejected("delegation_input_limit")
        save_private(
            journal.store,
            tx,
            run,
            key,
            {"request": request, "task": snapshot, "sourceRefs": refs},
            refs,
        )
        record_sources_locked(journal.store, tx, run, refs, journal.epoch)
        return snapshot


def import_inputs(journal, snapshot: dict, child_id, trusted) -> dict:
    """Give authorized delegated evidence stable identities owned by this child."""
    records = []
    for item in snapshot.get("inputs", []):
        body = item["content"]
        if item["kind"] == "evidence":
            records.append(body)
        elif item["kind"] == "ledger":
            records.extend(body.get("evidence", []))
    if not records:
        return snapshot
    ledger = getattr(journal, "answer_evidence", {})
    evidence = ledger.setdefault(execution_path.get(), {})
    replacements = {
        record["id"]: str(uuid5(child_id, "input:" + digest(record))) for record in records
    }

    def remap(value, field=None):
        if isinstance(value, dict):
            return {
                key: item if key == "record" else remap(item, key) for key, item in value.items()
            }
        if isinstance(value, list):
            return [remap(item, field) for item in value]
        if isinstance(value, str) and field in {"id", "evidenceIds", "parentEvidenceIds"}:
            return replacements.get(value, value)
        return value

    with journal.store.transaction() as tx:
        _, run = journal.store.lock(tx, journal.run_id, trusted)
        journal.store.require_epoch(run, journal.epoch)
        for record in records:
            refs = record.get("sourceRefs", [])
            if not allows(journal.store, trusted, refs, epoch=journal.epoch):
                raise ToolRejected("delegation_input_forbidden")
            identifier = replacements[record["id"]]
            imported = remap(record)
            save_private(journal.store, tx, run, f"answer/v2/evidence/{identifier}", imported, refs)
            evidence[identifier] = imported
            record_sources_locked(journal.store, tx, run, refs, journal.epoch)
    journal.answer_evidence = ledger

    result = remap(snapshot)
    assert isinstance(result, dict)
    return result


def delivery(child_id, returned, answer, reason, evidence, artifact_ids=()):
    rejected = []
    if returned:
        findings = []
        for finding in returned.findings:
            bad = [key for key in finding.evidence_ids if key not in evidence]
            rejected.extend(bad)
            valid = [key for key in finding.evidence_ids if key in evidence]
            if valid:
                findings.append(finding.model_copy(update={"evidence_ids": valid}))
        gaps = list(returned.gaps)
        invalid_artifacts = set(returned.artifact_refs) - set(artifact_ids)
        if rejected or len(findings) != len(returned.findings) or invalid_artifacts:
            gaps = [*gaps[:9], "部分发现缺少本子任务证据，已移出待核。"]
        returned = returned.model_copy(
            update={
                "findings": findings,
                "artifact_refs": [ref for ref in returned.artifact_refs if ref in artifact_ids],
                "gaps": gaps,
                "status": "partial" if gaps and returned.status == "completed" else returned.status,
            }
        )
    elif reason == "natural_end":
        returned = ChildReturn(
            status="partial",
            summary=answer[:1000] or "子任务已结束",
            gaps=["子任务未按契约返回结构化结果"],
        )
    return ChildDelivery(
        deliveryId=str(uuid5(child_id, "delivery")),
        childRunId=str(child_id),
        reason=reason,
        status=returned.status if returned else "failed",
        result=returned,
        rejectedEvidenceIds=list(dict.fromkeys(rejected)),
        answer=answer or (returned.summary if returned else ""),
        evidenceIds=list(evidence),
    ).model_dump(mode="json", by_alias=True)


def merge_delivery(journal, result, trusted):
    with journal.store.transaction() as tx:
        _, run = journal.store.lock(tx, journal.run_id, trusted)
        journal.store.require_epoch(run, journal.epoch)
        goal = bound_goal(tx, run)
        if goal is None or goal.status != "active":
            return
        ledger = Ledger(journal.store)
        key = "delivery:" + result["childRunId"]
        if any(e.entry_key == key for e, _ in ledger.rows(tx, goal)):
            return
        evidence = getattr(journal, "answer_evidence", {}).get(execution_path.get(), {})
        refs = []
        for identifier in result["evidenceIds"]:
            refs.extend(r for r in evidence[identifier].get("sourceRefs", []) if r not in refs)
        ledger.upsert(
            tx,
            goal,
            key,
            "delivery",
            "done",
            {
                "title": result["answer"][:200] or "子任务交付",
                "delivery": result,
                "sourceRefs": refs,
                "dependsOn": [],
            },
        )
        for index, finding in enumerate((result.get("result") or {}).get("findings", [])):
            item = LedgerItem(
                type="conclusion",
                key=f"child:{result['childRunId']}:{index}",
                title=finding["statement"][:200],
                text=finding["statement"],
                evidenceIds=finding["evidenceIds"],
                factKey=finding.get("factKey"),
                value=finding.get("value"),
                unit=finding.get("unit"),
            )
            ledger.record(tx, goal, [item], journal, trusted)


def read_child_answer(store, run_id, child_id, context):
    """Read the full accepted handoff, rechecking current source access on every read."""
    require_run_sources(store, run_id, context)
    with store.transaction() as tx:
        step = tx.get(AgentStep, child_id)
        if not step or step.run_id != run_id or step.kind != "subagent":
            return None
        if not step.accepted_attempt_id:
            return None
        attempt = tx.get(AgentAttempt, step.accepted_attempt_id)
        if attempt is None or attempt.output_content is None:
            raise ModelError("service_unavailable", 503)
        value = store.private.open(run_id, "attempt-output", attempt.output_content)
        if digest(value) != attempt.output_sha256:
            raise ModelError("service_unavailable", 503)
        result = ChildDelivery.model_validate(value["result"])
        if result.child_run_id != str(child_id):
            raise ModelError("service_unavailable", 503)
        return result.answer or None


def normalize_delegation_input(raw, context):
    if "tasks" not in raw or not any("inputRefs" in t for t in raw["tasks"]):
        return raw
    from compass_agent.goal.ledger import Ledger, bound_goal
    from compass_agent.runtime.entities import AgentArtifact

    journal = current_execution.get()
    if journal is None:
        if any(t.get("inputRefs") for t in raw["tasks"]):
            raise ToolRejected("delegation_input_forbidden")
        return {"tasks": [{k: v for k, v in t.items() if k != "inputRefs"} for t in raw["tasks"]]}
    with journal.store.transaction() as tx:
        _, run = journal.store.lock(tx, journal.run_id, context.trusted)
        evidence = getattr(journal, "answer_evidence", {}).get(execution_path.get(), {})
        goal = bound_goal(tx, run)
        ledger = (
            {
                e.entry_key
                for e, _ in Ledger(journal.store).visible(
                    tx,
                    goal,
                    context.trusted,
                    epoch=journal.epoch,
                )
            }
            if goal
            else set()
        )
        tasks = []
        for task in raw["tasks"]:
            value = {k: v for k, v in task.items() if k != "inputRefs"}
            inputs = []
            for identifier in task.get("inputRefs", []):
                result_evidence = next(
                    (
                        key
                        for key, item in evidence.items()
                        if item["record"].get("resultId") == identifier
                    ),
                    None,
                )
                if identifier in evidence or result_evidence:
                    identifier = result_evidence or identifier
                    kind = "evidence"
                elif identifier in ledger:
                    kind = "ledger"
                else:
                    try:
                        artifact = tx.get(AgentArtifact, UUID(identifier))
                    except ValueError:
                        artifact = None
                    if artifact is None or artifact.run_id != run.id:
                        raise ToolRejected("delegation_input_forbidden")
                    kind = "artifact"
                inputs.append({"kind": kind, "id": identifier})
            value["inputs"] = inputs
            tasks.append(value)
        return {"tasks": tasks}
