"""Protected current goal state, outside selectable conversational history."""

import json

from compass_agent.goal.ledger import Ledger, bound_goal
from compass_agent.goal.resume import resume_check
from compass_agent.harness.autonomy import AutonomyStopped


def anchor(journal, trusted, budget):
    if not getattr(journal, "goal_id", None):
        return None
    with journal.store.transaction() as tx:
        _, run = journal.store.lock(tx, journal.run_id, trusted)
        journal.store.require_epoch(run, journal.epoch)
        goal = bound_goal(tx, run)
        if goal is None:
            return None
        is_round = (run.config_snapshot.get("goal") or {}).get("round") is not None
        if goal.status not in {"active", "completed"}:
            if is_round:
                raise AutonomyStopped("goal_paused" if goal.status == "paused" else "goal_inactive")
        resume_check(journal.store, tx, goal, trusted, journal.epoch)
        rows = Ledger(journal.store).visible(tx, goal, trusted, epoch=journal.epoch)
        tokens = goal.tokens_used + (budget.tokens if budget else 0)
        cost = goal.cost_used_microusd + (budget.cost_microusd if budget else 0)
        if goal.status == "active" and (
            tokens >= goal.budget["tokens"] or cost >= goal.budget["costMicrousd"]
        ):
            raise AutonomyStopped(
                "token_budget" if tokens >= goal.budget["tokens"] else "cost_budget"
            )
        remaining = min(1 - tokens / goal.budget["tokens"], 1 - cost / goal.budget["costMicrousd"])
        value = {
            "goalId": str(goal.id),
            "revision": goal.revision,
            "status": goal.status,
            "contract": goal.contract,
            "budget": goal.budget,
            "round": (run.config_snapshot.get("goal") or {}).get("round"),
            "usage": {
                "tokens": tokens,
                "costMicrousd": cost,
                "remainingPct": round(remaining * 100, 1),
            },
            "todos": [],
            "failures": [],
            "review": [],
            "completed": [],
            "conclusions": [],
        }
        for row, body in rows:
            safe = {
                k: body[k]
                for k in (
                    "title",
                    "text",
                    "factKey",
                    "value",
                    "unit",
                    "evidenceIds",
                    "acceptanceIds",
                    "required",
                )
                if k in body
            }
            item = {"key": row.entry_key, **safe}
            if row.status in {"stale", "conflict"}:
                value["review"].append(
                    {
                        "key": row.entry_key,
                        "status": row.status,
                        "title": body["title"],
                        "conflictsWith": body.get("conflictsWith", []),
                    }
                )
            elif row.kind == "todo" and row.status == "open":
                value["todos"].append(item)
            elif row.kind == "failure":
                value["failures"].append(item)
            elif row.kind in {"query", "effect"} and row.status == "done":
                value["completed"].append({"key": row.entry_key, "title": body["title"]})
            elif row.kind == "conclusion" and row.status == "done":
                value["conclusions"].append(item)
        # Open work and failure records remain complete; completed material is bounded.
        value["completed"] = value["completed"][-30:]
        value["conclusions"] = value["conclusions"][-20:]
        instruction = (
            "从未完成待办继续。已完成查询与写入可直接复用。跨轮待办以账本为准。"
            if goal.status == "active"
            else "按当前状态回应用户。修改目标使用 propose，并带上 goalId 和 expectedRevision。"
        )
        if remaining < 0.1:
            instruction += "预算接近用尽：本轮收尾，交付已完成项、剩余项和下一步。"
        return (
            "目标锚点（当前已确认契约与运行记录）\n"
            + json.dumps(value, ensure_ascii=False, sort_keys=True)
            + "\n"
            + instruction
        )
