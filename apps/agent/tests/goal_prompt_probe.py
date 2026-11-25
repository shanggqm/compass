"""Explicit real-provider prompt acceptance. Run with an isolated Agent database.

uv run --env-file work/983-e2e.env python -m apps.agent.tests.goal_prompt_probe
Only model decisions are exercised here; runtime/SQL/browser cases run separately.
"""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from compass_agent.api.models import create_model_service
from compass_agent.llm.admission import ExecutionGate
from compass_agent.models.entities import Provider
from compass_agent.prompt_assets import prompt_text
from compass_agent.schemas import ChatMessage, GenerateInput, PlatformScope, TrustedContext
from compass_agent.tools.builtin.database_query import definition as database_definition
from compass_agent.tools.builtin.final_answer import definition as answer_definition
from compass_agent.tools.builtin.goal import definition as goal_definition
from compass_agent.tools.builtin.sub_agent import definition as child_definition
from sqlalchemy import select
from sqlalchemy.orm import Session

CASES = [
    ("simple", "10 月华东 GMV 是多少？", "", "simple"),
    (
        "propose",
        "把 10 月经营分析持续做完，按销售、库存、渠道、综合建议四个阶段，每个结论要有来源。",
        "",
        "propose",
    ),
    (
        "independent",
        "继续目标。库存缺货排查和渠道转化排查互不依赖，各需多次查询。",
        "未完成待办：库存缺货排查、渠道转化排查；均可独立调查，数据范围为 10 月华东。",
        "parallel",
    ),
    (
        "dependent",
        "继续目标，对比库存与转化结论并整理建议。",
        "库存、渠道两个分支已完成并 record。库存下降 20%，转化下降 10%，引用 e1/e2。"
        "只剩跨分支比较，需主任务判断。",
        "no_delegate",
    ),
    (
        "partial",
        "库存子任务部分完成，下一步怎么推进？",
        '最近交付 {"status":"partial","summary":"库存总量已查到",'
        '"gaps":["缺少华东区域维度"],"originalObjective":"分析库存变化"}。'
        "当前可查询门店区域表补齐。",
        "repair",
    ),
    (
        "wrap_up",
        "继续目标。",
        "预算接近用尽：本轮收尾，交付已完成项、剩余项和下一步。"
        "销售结论已完成，库存排查待办未完成。当前剩余预算 5%。",
        "wrap_up",
    ),
    (
        "gate_rejected",
        "继续目标。",
        '刚调用 goal.complete 返回 {"accepted":false,"gaps":["库存数量缺少证据"]}。'
        "补救待办：查询库存数量并写入带证据结论，再完成待办。",
        "repair",
    ),
]


def accepted(expected, calls):
    names = [c["name"] for c in calls]
    if expected == "simple":
        return "goal" not in names and "sub_agent" not in names
    if expected == "propose":
        return (
            len(calls) == 1
            and calls[0]["name"] == "goal"
            and calls[0]["args"].get("op") == "propose"
        )
    if expected == "parallel":
        return any(c["name"] == "sub_agent" and len(c["args"].get("tasks", [])) == 2 for c in calls)
    if expected == "no_delegate":
        return "sub_agent" not in names
    if expected == "wrap_up":
        return "database_query" not in names and "sub_agent" not in names
    return bool(calls) and not any(
        (c["name"] == "goal" and c["args"].get("op") == "complete")
        or (
            c["name"] == "sub_agent"
            and any(t["objective"] == "分析库存变化" for t in c["args"].get("tasks", []))
        )
        for c in calls
    )


async def main():
    if os.environ.get("COMPASS_GOAL_E2E") != "true":
        raise SystemExit("Use the isolated acceptance environment (COMPASS_GOAL_E2E=true).")
    service = create_model_service(ExecutionGate())
    with Session(service.repository.engine) as tx:
        provider = tx.scalar(
            select(Provider).where(
                Provider.enabled,
                Provider.alias
                == os.environ.get("GOAL_PROBE_MODEL", "983 真实模型 DeepSeek V4.1 Flash"),
            )
        )
        if provider is None:
            raise SystemExit("Configured real model unavailable.")
        provider_id, model = provider.id, provider.api_model
    tools = [
        goal_definition(None),
        child_definition(None),
        database_definition(),
        answer_definition(None),
    ]
    for tool in tools:
        tool.function.description = tool.function.description.replace("{{max_tasks}}", "4").replace(
            "{{max_parallel}}", "2"
        )
    system = "你是 Compass 电商经营分析助手。按当前阶段选择下一步工具行动。\n" + "\n".join(
        prompt_text(path)
        for path in (
            "shared/fragments/long-task.md",
            "shared/fragments/delegation.md",
            "shared/tools/database-query.md",
        )
    )
    system = system.replace("{{max_tasks}}", "4").replace("{{max_parallel}}", "2")
    context = TrustedContext(
        actor_user_id=uuid4(), scope=PlatformScope(kind="platform"), purpose="test"
    )
    results = []
    for name, question, state, expected in CASES:
        anchor = (
            (
                "\n目标锚点：目标为完成 10 月华东经营分析。状态 active，revision=1。"
                "约束：仅授权只读数据，结论附来源。" + state
            )
            if state
            else ""
        )
        request = GenerateInput(
            messages=[
                ChatMessage(role="system", content=system + anchor),
                ChatMessage(role="user", content=question),
            ],
            tools=tools,
            stream=False,
            thinking=False,
            max_tokens=4096,
        )
        prepared = await service.prepare(uuid4(), provider_id, request, context)
        result = await service.run(prepared, request)
        calls = [
            {"name": c.function.name, "args": json.loads(c.function.arguments)}
            for c in result.tool_calls
        ]
        passed = result.status == "completed" and accepted(expected, calls)
        row = {
            "case": name,
            "model": model,
            "status": result.status,
            "error": result.error_code,
            "passed": passed,
            "calls": calls,
            "text": result.text,
            "usage": result.usage.model_dump() if result.usage else None,
        }
        results.append(row)
        print(
            json.dumps(
                {
                    "case": name,
                    "passed": passed,
                    "status": result.status,
                    "tools": [c["name"] for c in calls],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        Path("work/983-real-prompt-report.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n"
        )
    raise SystemExit(0 if all(row["passed"] for row in results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
