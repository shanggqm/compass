"""Pure budget/selection compiler used by live assembly and frozen-candidate replay."""

from dataclasses import asdict

from compass_agent.context_runtime.contracts import Compaction, ContextItem, Selection
from compass_agent.harness.context import ContextLimit
from compass_agent.schemas import RUN_CONTEXT_MESSAGES


def compile_candidates(
    items,
    limit,
    *,
    overhead=0,
    continuation=False,
    protect_dictionary=False,
    compaction_threshold=1.0,
    max_observation_items=None,
):
    selected = list(items)
    transforms = []
    protected = {
        i.item_id for i in items if i.required or (protect_dictionary and i.trust == "dictionary")
    }
    target = max(1, int(limit * compaction_threshold))

    def size():
        return overhead + sum(i.byte_bound for i in selected)

    before = size()
    for group in dict.fromkeys(i.group for i in selected):
        observations = sum(i.trust == "observation" for i in selected)
        if (
            size() <= target
            and len(selected) <= RUN_CONTEXT_MESSAGES
            and (max_observation_items is None or observations <= max_observation_items)
        ):
            break
        if continuation:
            break
        members = [i for i in selected if i.group == group]
        if any(i.item_id in protected for i in members):
            continue
        old = size()
        selected = [i for i in selected if i.group != group]
        transforms.append(Compaction(group, old, size()))
    if size() > limit or len(selected) > RUN_CONTEXT_MESSAGES:
        raise ContextLimit()
    kept = {i.item_id for i in selected}
    decisions = tuple(
        Selection(
            i.item_id,
            "selected" if i.item_id in kept else "omitted",
            "required"
            if i.item_id in protected
            else "in_budget"
            if i.item_id in kept
            else "budget",
            i.kind,
            i.trust,
            i.source,
            i.group,
            i.message.role,
            i.byte_bound,
            i.access,
        )
        for i in items
    )
    return selected, before, size(), decisions, tuple(transforms)


def freeze(items, selected, *, overhead, limit, continuation=False) -> dict:
    from compass_agent.context_security.redactor import storage_value

    return storage_value(
        {
            "version": 1,
            "candidates": [
                {**asdict(i), "message": i.message.model_dump(mode="json", exclude_none=True)}
                for i in items
            ],
            "assembled": [i.message.model_dump(mode="json", exclude_none=True) for i in selected],
            "selected_ids": [i.item_id for i in selected],
            "overhead": overhead,
            "limit": limit,
            "continuation": continuation,
        }
    )


def restore(payload):
    from compass_agent.context_runtime.contracts import AccessRef, SourceRef
    from compass_agent.schemas import ChatMessage

    return [
        ContextItem(
            **{
                **item,
                "message": ChatMessage.model_validate(item["message"]),
                "source": SourceRef(**item["source"]),
                "access": tuple(AccessRef(**ref) for ref in item["access"]),
            },
        )
        for item in payload["candidates"]
    ]


def secure_items(items, trusted, *, mode="enforce"):
    from compass_agent.context_security.guard import ContextSecurityGuard

    clean, events, blocked = ContextSecurityGuard(mode=mode).enforce(items, trusted)
    # Original rejected data is represented by a constant protocol placeholder.
    return clean, events, blocked
