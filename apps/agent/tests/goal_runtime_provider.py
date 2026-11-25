"""Deterministic model upstream for the Goal browser contract tests, never production."""

import asyncio
import json
from time import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()
calls: list[dict] = []
holds: dict[str, asyncio.Event] = {}
pending_auto = 0


def walk(value):
    if isinstance(value, str):
        try:
            yield from walk(json.loads(value))
        except (ValueError, RecursionError):
            return
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def tool(name, args, key) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": key,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        ],
    }


def text(value) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
    }


def anchor(system):
    marker = "目标锚点（当前已确认契约与运行记录）\n"
    if marker not in system:
        return None
    return json.JSONDecoder().raw_decode(system.split(marker)[-1])[0]


def final(body, evidence=None):
    common = {"completion": "complete", "coveredGoalIds": ["root"], "unresolved": []}
    if evidence:
        value = {
            **common,
            "mode": "evidence",
            "claims": [
                {
                    "id": "c1",
                    "kind": "fact",
                    "text": "销售核对结果为 200 元。",
                    "refs": [{"evidenceId": evidence, "row": 0, "column": "amount"}],
                }
            ],
            "blocks": [{"type": "paragraph", "claimIds": ["c1"]}],
        }
    else:
        value = {**common, "mode": "plain", "body": body}
    return tool("final_answer", value, "finish")


def respond(body):
    messages = body["messages"]
    system = "\n".join(m.get("content") or "" for m in messages if m["role"] == "system")
    users = [m.get("content") or "" for m in messages if m["role"] == "user"]
    current = users[-1] if users else ""
    names = {t["function"]["name"] for t in body.get("tools", [])}
    objects = list(walk(messages))
    review = next(
        (
            o
            for o in reversed(objects)
            if "candidateDigest" in o and "evidenceRevision" in o and "candidate" in o
        ),
        None,
    )
    if review:
        return text(
            {
                "candidateDigest": review["candidateDigest"],
                "evidenceRevision": review["evidenceRevision"],
                "decision": "accept",
                "modeValid": True,
                "goals": [{"goalId": "root", "status": "covered", "reason": "已覆盖本轮请求"}],
                "claims": [
                    {"claimId": c["id"], "status": "supported", "reason": "与查询回执一致"}
                    for c in review["candidate"].get("claims", [])
                ],
                "feedback": "",
            }
        )
    if "classify" in system.lower() or "scene_id" in system and "deliverable" in system:
        return text({"scene_id": "SC-01", "deliverable": "business_qa"})
    if not names:
        return text("月度经营复盘")
    receipts = [o for o in objects if "evidenceId" in o and o.get("kind") == "query"]
    if "report_to_parent" in names:
        task = next(o for o in objects if "objective" in o and "name" in o and "maxSteps" in o)
        inventory = task["name"] == "库存核对"
        if not receipts:
            return tool(
                "database_query",
                {
                    "op": "query",
                    "sql": "SELECT sum(amount)::int AS amount FROM core."
                    + ("sku" if inventory else "channel_product_daily"),
                },
                "read",
            )
        progress = any(o.get("kind") == "progress" for o in objects)
        if not progress:
            return tool(
                "report_to_parent",
                {"kind": "progress", "summary": task["name"] + "已取得授权查询结果"},
                "progress",
            )
        return tool(
            "report_to_parent",
            {
                "kind": "final",
                "result": {
                    "status": "completed",
                    "summary": task["name"] + "已完成",
                    "findings": [
                        {
                            "statement": "库存为 50 件" if inventory else "销售额为 200 元",
                            "factKey": "stock" if inventory else "sales",
                            "value": "50" if inventory else "200",
                            "unit": "件" if inventory else "元",
                            "evidenceIds": [receipts[-1]["evidenceId"]],
                        }
                    ],
                },
            },
            "returned",
        )
    goal = anchor(system)
    if (
        not goal
        or "修改目标：" in current
        and not any(o.get("op") == "propose" for o in objects if o is not goal)
    ):
        proposal = {
            "op": "propose",
            "contract": {
                "objective": "完成月度销售与库存复盘",
                "acceptance": [
                    {"id": "a1", "text": "核对销售金额"},
                    {"id": "a2", "text": "核对库存数量"},
                ],
                "constraints": [
                    "仅使用获准数据",
                    *(["只看华东"] if "修改目标：" in current else []),
                ],
            },
            "budget": {"maxRounds": 3, "tokens": 2000000, "costMicrousd": 20000000},
        }
        if goal:
            proposal.update(goalId=goal["goalId"], expectedRevision=goal["revision"])
        return tool("goal", proposal, "propose")
    if goal["status"] == "completed":
        return final("复盘已完成。", receipts[0]["evidenceId"] if receipts else None)
    if goal["status"] == "paused" and goal["round"] is None:
        return final("已收到插话，将按当前目标继续核对。")
    if goal["round"] is None:
        if not goal["todos"] and not goal["conclusions"]:
            return tool(
                "goal",
                {
                    "op": "record",
                    "items": [
                        {"type": "todo", "key": k, "title": title}
                        for k, title in [("sales", "核对销售金额"), ("stock", "核对库存数量")]
                    ],
                },
                "plan",
            )
        return final("目标已确认，接下来核对销售与库存。")
    deliveries = next((o["deliveries"] for o in reversed(objects) if "deliveries" in o), None)
    if deliveries is None:
        return tool(
            "sub_agent",
            {
                "tasks": [
                    {
                        "name": name,
                        "objective": objective,
                        "expectations": ["返回带查询引用的数值"],
                        "tools": ["database_query"],
                        "maxSteps": 3,
                    }
                    for name, objective in [
                        ("销售核对", "汇总销售金额"),
                        ("库存核对", "汇总库存数量"),
                    ]
                ]
            },
            "branches",
        )
    if any(item["status"] != "completed" for item in deliveries):
        return tool("goal", {"op": "block", "reason": "子任务未完成，请检查查询与交付"}, "block")
    if goal["todos"]:
        items = []
        for index, d in enumerate(deliveries):
            f = d["result"]["findings"][0]
            key = "sales" if index == 0 else "stock"
            items += [
                {
                    "type": "conclusion",
                    "key": "accepted:" + key,
                    "title": f["statement"],
                    "text": f["statement"],
                    "factKey": f["factKey"],
                    "value": f["value"],
                    "unit": f["unit"],
                    "evidenceIds": f["evidenceIds"],
                    "acceptanceIds": ["a1" if index == 0 else "a2"],
                },
                {"type": "todo_done", "key": key, "title": "已完成"},
            ]
        return tool("goal", {"op": "record", "items": items}, "record")
    return tool("goal", {"op": "complete", "expectedRevision": goal["revision"]}, "complete")


@app.get("/inspection")
def inspection():
    return {"calls": calls, "holds": list(holds), "pendingAuto": pending_auto}


@app.post("/control/auto")
async def control_auto(request: Request):
    if (await request.json()).get("hold"):
        holds.setdefault("auto", asyncio.Event())
    elif event := holds.pop("auto", None):
        event.set()
    return {"held": "auto" in holds}


@app.post("/{path:path}")
async def completion(request: Request):
    global pending_auto
    body = await request.json()
    system = "\n".join(m.get("content") or "" for m in body["messages"] if m["role"] == "system")
    goal = anchor(system)
    if body.get("tools") and goal and goal["round"] is not None and (event := holds.get("auto")):
        pending_auto += 1
        try:
            await event.wait()
        finally:
            pending_auto -= 1
    message = respond(body)
    calls.append(
        {
            "at": time(),
            "model": body["model"],
            "tools": [t["function"]["name"] for t in body.get("tools", [])],
            "reply": message,
        }
    )
    # Let the browser observe both independent children in flight.
    if any(t["function"]["name"] == "report_to_parent" for t in body.get("tools", [])):
        await asyncio.sleep(0.35)
    reason = "tool_calls" if message.get("tool_calls") else "stop"
    common = {"id": "goal-e2e", "model": body["model"], "created": int(time())}
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    if body.get("thinking", {}).get("type") == "enabled":
        message["reasoning_content"] = "核对当前目标与授权回执。"
    if not body.get("stream"):
        return {
            **common,
            "object": "chat.completion",
            "usage": usage,
            "choices": [{"index": 0, "message": message, "finish_reason": reason}],
        }
    for index, call in enumerate(message.get("tool_calls", [])):
        call["index"] = index

    async def chunks():
        for delta, finish in ((message, None), ({}, reason)):
            value: dict[str, Any] = {
                **common,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if finish:
                value["usage"] = usage
            yield "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(chunks(), media_type="text/event-stream")
