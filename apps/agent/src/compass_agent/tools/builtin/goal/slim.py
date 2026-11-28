"""Translate the compact goal request to the existing approval and ledger contracts."""

from hashlib import sha256

from compass_agent.goal.controller import open_goal
from compass_agent.tools.contracts import ToolRejected
from compass_agent.tools.input_adapter import frozen_input


def normalize_input(raw, context):
    if "action" not in raw:
        return raw

    def build(tx, session, run):
        goal = open_goal(tx, session.id)
        action = raw["action"]
        if action == "propose":
            args = {
                "op": "propose",
                "contract": {
                    "objective": raw.get("objective", ""),
                    "acceptance": [
                        {"id": f"a{i}", "text": text}
                        for i, text in enumerate(raw.get("acceptance", []), 1)
                    ],
                },
            }
            if goal:
                args.update(goalId=str(goal.id), expectedRevision=goal.revision)
            return args
        if goal is None:
            raise ToolRejected("goal_not_active")
        if action == "complete":
            return {"op": "complete", "expectedRevision": goal.revision}
        if action == "block":
            return {"op": "block", "reason": raw.get("objective", "等待外部决策")[:60]}
        acceptance = goal.contract["acceptance"]
        indexes = raw.get("acceptanceIndexes", [])
        if any(type(i) is not int or not 1 <= i <= len(acceptance) for i in indexes):
            raise ToolRejected("invalid_parameters")
        identifiers = raw.get("evidenceIds", [])
        if indexes and not identifiers:
            raise ToolRejected("invalid_parameters")
        finding = raw.get("finding", "")
        key = sha256(f"{context.step_id}:{context.call_id}".encode()).hexdigest()[:24]
        return {
            "op": "record",
            "items": [
                {
                    "type": "conclusion" if identifiers else "failure",
                    "key": f"finding.{key}",
                    "title": finding[:200],
                    "text": finding,
                    "evidenceIds": identifiers,
                    "acceptanceIds": [acceptance[i - 1]["id"] for i in indexes],
                }
            ],
        }

    return frozen_input("goal", raw, context, build)
