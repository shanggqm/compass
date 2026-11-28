"""Atomic answer/citation publication and permission-aware readback for both protocols."""

from hashlib import sha256
from uuid import UUID

from compass_agent.context_runtime.source_access import (
    allows,
    assistant_message,
    final_text,
    read_private,
    save_private,
)
from compass_agent.models.errors import ModelError

# The persisted key remains stable for previously published answers.


def publish_citations(store, tx, run, assistant, body, revision, citations):
    anchors = citations["anchors"]
    if any(
        body[a["start"] : a["end"]] not in {f"[{a['ordinal']}]", f"^[{a['ordinal']}]^"}
        for a in anchors
    ):
        raise ModelError("delivery_conflict", 409)
    manifest = {
        "messageId": str(assistant.id),
        "revision": revision,
        "textDigest": sha256(body.encode()).hexdigest(),
        "anchors": anchors,
    }
    save_private(
        store,
        tx,
        run,
        f"search/answer/{revision}",
        {"manifest": manifest, "details": citations["details"]},
        assistant.source_refs,
    )


def read_citations(store, run_id: UUID, context, citation_id=None):
    store.read_run(run_id, context)
    with store.transaction() as tx:
        message = assistant_message(tx, run_id)
        if not message:
            raise ModelError("not_found", 404)
        saved = read_private(store, tx, run_id, f"search/answer/{message.revision}")
        if saved and not allows(store, context, message.source_refs, raise_unavailable=True):
            raise ModelError("not_found", 404)
        if saved:
            body = final_text(store, tx, run_id)
            if not body or sha256(body.encode()).hexdigest() != saved["manifest"]["textDigest"]:
                raise ModelError("not_found", 404)
            if citation_id is None:
                return saved["manifest"]
            if detail := saved["details"].get(str(citation_id)):
                return detail
        if citation_id is not None:
            raise ModelError("not_found", 404)
        return {
            "messageId": str(message.id),
            "revision": message.revision,
            "textDigest": sha256(b"").hexdigest(),
            "anchors": [],
        }
