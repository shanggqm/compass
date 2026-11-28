"""Apply a full model checklist atomically through the existing plan transitions."""

from copy import deepcopy

from compass_agent.tools.builtin.todo_write.plan import TODO_ACTION, TodoRejected


def apply_slim(plan, args, index):
    rows = args["todos"]
    if not 2 <= len(rows) <= 6 or len({r["step"] for r in rows}) != len(rows):
        raise TodoRejected("invalid_plan")
    active = [r for r in rows if r["status"] == "in_progress"]
    if len(active) > 1 or any(r["status"] == "pending" for r in rows) and len(active) != 1:
        raise TodoRejected("invalid_plan")
    # Stable IDs follow the step text, including when unfinished steps are reordered.
    by_goal = {v.goal: k for k, v in plan.items.items()}
    used = set(plan.items) | plan.retired
    identifiers = []
    for row in rows:
        key = by_goal.get(row["step"])
        if key is None:
            key = next((f"t{i}" for i in range(1, 100) if f"t{i}" not in used), None)
            if key is None:
                raise TodoRejected("invalid_plan")
        used.add(key)
        identifiers.append(key)
    candidate = deepcopy(plan)
    items = []
    for row, key in zip(rows, identifiers, strict=True):
        old = plan.items.get(key)
        item = (
            old.model_dump(by_alias=True)
            if old
            else {
                "id": key,
                "goal": row["step"],
                "dependsOn": [],
                "optional": row["status"] == "skipped",
            }
        )
        if (
            candidate.status.get(key) in {"verified", "skipped"}
            and row["status"] != {"verified": "done", "skipped": "skipped"}[candidate.status[key]]
        ):
            raise TodoRejected("invalid_transition")
        items.append(item)
    candidate.apply(
        TODO_ACTION.validate_python(
            {
                "op": "replace",
                "baseVersion": candidate.version,
                "items": items,
            }
        ),
        index,
    )
    for row, key in zip(rows, identifiers, strict=True):
        status = row["status"]
        if status == "done" and candidate.status[key] != "verified":
            candidate.apply(
                TODO_ACTION.validate_python(
                    {
                        "op": "complete",
                        "baseVersion": candidate.version,
                        "todoId": key,
                    }
                ),
                index,
            )
        elif status == "skipped" and candidate.status[key] != "skipped":
            candidate.apply(
                TODO_ACTION.validate_python(
                    {
                        "op": "skip",
                        "baseVersion": candidate.version,
                        "todoId": key,
                        "reason": "duplicate_optional",
                    }
                ),
                index,
            )
        elif status == "in_progress":
            candidate.apply(
                TODO_ACTION.validate_python(
                    {
                        "op": "select",
                        "baseVersion": candidate.version,
                        "todoId": key,
                    }
                ),
                index,
            )
    plan.__dict__.update(candidate.__dict__)
