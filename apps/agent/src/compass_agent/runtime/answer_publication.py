"""Private answer chunks carried by the existing ordered Outbox and authorized views."""

from copy import deepcopy
from uuid import uuid5

from sqlalchemy import select, text

from compass_agent.models.errors import ModelError
from compass_agent.runtime.entities import AgentAttempt, AgentMessage, AgentOutbox
from compass_agent.schemas import RUN_STREAM_LIMITS

EXTENSION = "compass.runtime.answer_delta"


def chunk_key(message_id, revision):
    return f"answer-stream/{message_id}/{revision}"


def marker_data(event):
    if event["type"] != "view.upsert" or event["payload"]["kind"] != "timeline":
        return None
    inner = event["payload"]["value"]["event"]
    if inner["type"] == "stream.extension" and inner["payload"]["name"] == EXTENSION:
        return inner["payload"]["data"]
    return None


def apply_marker(messages, data):
    message = messages.get(data["messageId"])
    if message is None:
        raise ModelError("projection_message_missing", 409)
    if message["status"] != "streaming" or message["revision"] + 1 != data["revision"]:
        raise ModelError("projection_revision_gap", 409)
    message = deepcopy(message)
    message["revision"] = data["revision"]
    messages[data["messageId"]] = message


def committed_message(tx, run, assistant):
    """Only fold unprojected events for this message, including close/cancel events."""
    from compass_agent.runtime.store import message_snapshot

    value = message_snapshot(assistant)
    marker = None
    for obj in run.public_view.get("objects", {}).values():
        data = marker_data({"type": "view.upsert", "payload": obj})
        if data and data["messageId"] == str(assistant.id):
            marker = data
    boxes = tx.scalars(
        select(AgentOutbox)
        .where(AgentOutbox.run_id == run.id, AgentOutbox.delivered_at.is_(None))
        .order_by(AgentOutbox.run_version)
    )
    for box in boxes:
        for packet in box.payload["packets"]:
            event = packet["event"]
            data = marker_data(event)
            if data and data["messageId"] == str(assistant.id):
                if data["revision"] > value["revision"]:
                    messages = {str(assistant.id): value}
                    apply_marker(messages, data)
                    value = messages[str(assistant.id)]
                    marker = data
            elif event["type"] == "message.upsert":
                incoming = event["payload"]
                if (
                    incoming["messageId"] == str(assistant.id)
                    and incoming["revision"] > value["revision"]
                ):
                    value = deepcopy(incoming)
    return value, marker


def append_chunk(store, run_id, epoch, attempt_id, operation_id, content):
    from compass_agent.context_runtime.source_access import read_private, save_private

    with store.transaction() as tx:
        if store.distributed:
            # Bound lock contention so a text writer cannot hold shutdown or cancellation open.
            tx.execute(text("SET LOCAL lock_timeout = '2s'"))
            tx.execute(text("SET LOCAL statement_timeout = '3s'"))
        session, run = store.lock(tx, run_id)
        existing = tx.scalar(
            select(AgentOutbox).where(
                AgentOutbox.run_id == run_id, AgentOutbox.operation_id == operation_id
            )
        )
        if existing:
            data = marker_data(existing.payload["packets"][0]["event"])
            if data is None:
                raise ModelError("packet_conflict", 409)
            saved = read_private(store, tx, run_id, chunk_key(data["messageId"], data["revision"]))
            if saved is None or saved["text"] != content or saved["attemptId"] != str(attempt_id):
                raise ModelError("packet_conflict", 409)
            return existing.id
        store.require_epoch(run, epoch)
        attempt = tx.get(AgentAttempt, attempt_id)
        if attempt is None or attempt.run_id != run_id or attempt.status != "dispatched":
            raise ModelError("execution_fenced", 409)
        assistant = tx.scalars(
            select(AgentMessage).where(
                AgentMessage.run_id == run_id,
                AgentMessage.channel == "conversation",
                AgentMessage.role == "assistant",
            )
        ).one()
        value, previous = committed_message(tx, run, assistant)
        if value["status"] != "streaming" or run.status != "running":
            raise ModelError("execution_fenced", 409)
        size = previous["answerBytes"] if previous else len(value["content"]["text"].encode())
        size += len(content.encode())
        if size > RUN_STREAM_LIMITS["answerBytes"]:
            raise ModelError("run_content_limit", 409)
        revision = value["revision"] + 1
        base = previous["baseRevision"] if previous else value["revision"]
        data = {
            "messageId": str(assistant.id),
            "revision": revision,
            "baseRevision": base,
            "answerBytes": size,
        }
        save_private(
            store,
            tx,
            run,
            chunk_key(assistant.id, revision),
            {
                "text": content,
                "attemptId": str(attempt_id),
                "prefix": value["content"]["text"] if previous is None else "",
            },
            assistant.source_refs,
        )
        event = {
            "type": "view.upsert",
            "payload": {
                "kind": "timeline",
                "objectId": f"answer-stream:{assistant.id}",
                "revision": revision,
                "value": {
                    "ordinal": assistant.ordinal,
                    "event": {
                        "type": "stream.extension",
                        "payload": {
                            "name": EXTENSION,
                            "version": 1,
                            "required": False,
                            "data": data,
                        },
                    },
                },
            },
        }
        return store.outbox(tx, session, run, [event], operation_id=operation_id).id


def hydrate_chunk(store, tx, run_id, data):
    from compass_agent.context_runtime.source_access import read_private

    saved = read_private(store, tx, run_id, chunk_key(data["messageId"], data["revision"]))
    if saved is None:
        raise ModelError("stream_data_lost", 410)
    return {
        "type": "message.delta",
        "payload": {
            "messageId": data["messageId"],
            "revision": data["revision"],
            "text": saved["text"],
        },
    }


def draft_text(store, tx, run, payload):
    """Rehydrate only the projected prefix, in one query, on snapshot/incomplete reads."""
    obj = run.public_view.get("objects", {}).get(f"answer-stream:{payload['messageId']}")
    if obj is None:
        return None
    data = marker_data({"type": "view.upsert", "payload": obj})
    if data is None:
        raise ModelError("stream_data_invalid", 503)
    through = min(data["revision"], payload["revision"])
    if through <= data["baseRevision"]:
        return None
    purposes = [
        chunk_key(payload["messageId"], n) for n in range(data["baseRevision"] + 1, through + 1)
    ]
    keys = {uuid5(run.id, "context:" + purpose): purpose for purpose in purposes}
    records = {
        item.id: item for item in tx.scalars(select(AgentMessage).where(AgentMessage.id.in_(keys)))
    }
    if len(records) != len(keys):
        raise ModelError("stream_data_lost", 410)
    chunks = [
        store.private.open(run.id, purpose, records[key].private_content)
        for key, purpose in keys.items()
    ]
    return chunks[0]["prefix"] + "".join(item["text"] for item in chunks)
