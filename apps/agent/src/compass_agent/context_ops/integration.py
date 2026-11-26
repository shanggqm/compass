"""Protect newly restored history and summary output at the final model boundary."""

from dataclasses import asdict, replace
from hashlib import sha256

from compass_agent.context_ops.compiler import freeze, secure_items
from compass_agent.context_runtime.pipeline import FRAME_BYTE_LIMIT, canonical, collect, fit
from compass_agent.context_security.policy import SecurityEventRecord, aggregate_events
from compass_agent.schemas import ChatMessage, GenerateInput


def secure_optimized_frame(frame, trusted, journal):
    request = frame["input"]["request"]
    messages = [ChatMessage.model_validate(m) for m in request["messages"]]
    prior = frame["receipt"]["contextOps"]
    # Fixed instruction/metric roles retain their original provenance. Every summary
    # remains observation even though its transport role is user.
    known = {canonical(i["message"]): i for i in prior["frozen"]["candidates"]}
    items = collect(messages)
    for index, item in enumerate(items):
        source = known.get(canonical(item.message.model_dump(mode="json", exclude_none=True)))
        if source:
            from compass_agent.context_runtime.contracts import AccessRef

            items[index] = replace(
                item,
                kind=source["kind"],
                trust=source["trust"],
                access=tuple(AccessRef(**a) for a in source["access"]),
            )
        elif (item.message.content or "").startswith("以下是已执行历史的交接摘要"):
            items[index] = replace(item, trust="observation", kind="summary")
    clean, events, blocked = secure_items(
        items, trusted, mode=journal.store.source_settings.compass_context_security_mode
    )
    value = GenerateInput.model_validate({**request, "messages": [i.message for i in clean]})
    limit = min(FRAME_BYTE_LIMIT, frame["receipt"]["budget"]["limit"])
    value, before, after, selections, transforms = fit(
        value,
        limit,
        candidates=clean,
        continuation_extra=frame["receipt"]["budget"].get("protocolExtra", 0),
    )
    kept = {s.item for s in selections if s.decision == "selected"}
    frozen = freeze(
        clean,
        [i for i in clean if i.item_id in kept],
        overhead=max(0, before - sum(i.byte_bound for i in clean)),
        limit=limit,
    )
    # Model-assisted selection/summary outputs are frozen as input to deterministic replay.
    frozen["upstream_optimization"] = frame["receipt"].get("contextOptimization", {})
    frame["receipt"]["contextOps"] = {
        "frozen": frozen,
        "reviewing": prior.get("reviewing", False),
        "blocked": {**prior.get("blocked", {}), **blocked},
        "duration_ms": prior.get("duration_ms", 0),
        "security_events": [
            asdict(e)
            for e in aggregate_events(
                [
                    *(SecurityEventRecord(**e) for e in prior.get("security_events", [])),
                    *events,
                ]
            )
        ],
    }
    if value.messages != messages:
        frame["continuation"] = None
        frame["input"]["continuationDigest"] = None
        value = value.model_copy(
            update={"continuation_token": None, "thinking": False, "reasoning_effort": None}
        )
    frame["input"]["request"] = value.model_dump(mode="json", exclude={"continuation_token"})
    frame["receipt"]["requestDigest"] = sha256(canonical(frame["input"]["request"])).hexdigest()
    frame["receipt"]["selections"] = [asdict(s) for s in selections]
    frame["receipt"]["compactions"].extend(asdict(t) for t in transforms)
    frame["receipt"]["budget"].update(before=before, after=after)
    frame["receipt"]["sourceDeps"] = frame.get("contextOptimization", {}).get("sourceDeps", [])
    return frame
