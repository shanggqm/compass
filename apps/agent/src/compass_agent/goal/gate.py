"""Programmatic goal completion; gaps become required remedial work."""

from compass_agent.goal.controller import transition
from compass_agent.goal.ledger import Ledger
from compass_agent.models.errors import ModelError
from compass_agent.runtime.private_state import digest


def complete(store, tx, goal, revision, context, *, epoch=None):
    if revision != goal.revision:
        raise ModelError("goal_stale_revision", 409)
    ledger = Ledger(store)
    rows = ledger.visible(tx, goal, context, epoch=epoch)
    supported = {
        key
        for entry, body in rows
        if entry.kind == "conclusion" and entry.status == "done"
        for key in body.get("acceptanceIds", [])
    }
    gaps = {
        f"acceptance:{item['id']}": item["text"]
        for item in goal.contract["acceptance"]
        if item["id"] not in supported
    }
    for entry, body in rows:
        if entry.kind == "todo" and entry.status == "open" and body.get("required", True):
            gaps[f"todo:{entry.entry_key}"] = body["title"]
        elif entry.status in {"conflict", "stale"}:
            gaps[f"review:{entry.entry_key}"] = "核对待核或失效事实：" + body["title"]
    if not gaps:
        transition(goal, "completed")
    else:
        for key, title in gaps.items():
            # Existing required todos already describe their own remedy.
            if key.startswith("todo:"):
                continue
            ledger.upsert(
                tx,
                goal,
                f"remedy:{digest(key)}",
                "todo",
                "open",
                {"title": title, "required": True, "sourceRefs": [], "dependsOn": []},
            )
    return {"accepted": not gaps, "gaps": list(gaps.values())}
