"""Explicit offline or real-provider A/B/C diagnostic entry point.

Run from Compass root: uv run --locked --package compass-agent python -m
apps.agent.tests.tool_eval.runner --mode offline --out work/tool-eval/offline.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from compass_agent.schemas import (
    ChatMessage,
    GenerateInput,
    PlatformScope,
    ToolDefinition,
    TrustedContext,
)

from .harness import DIRECTORY, ModelTurn, compact, load_dataset, offline_case, run_case
from .metrics import EvalGroup, summarize_group


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--mode", choices=("offline", "model"), default="offline")
    result.add_argument("--group", nargs="+", choices=("A", "B", "C"), default=["A", "B", "C"])
    result.add_argument("--case", action="append", dest="case_ids")
    result.add_argument("--out", type=Path)
    result.add_argument("--repetitions", type=int, default=1)
    result.add_argument("--concurrency", type=int, default=1)
    result.add_argument("--max-steps", type=int, default=6)
    result.add_argument("--max-tokens", type=int, default=4096)
    result.add_argument("--temperature", type=float, default=0)
    provider = result.add_mutually_exclusive_group()
    provider.add_argument("--provider-id", type=UUID)
    provider.add_argument("--provider-alias")
    result.add_argument("--input-usd-per-million", type=float)
    result.add_argument("--output-usd-per-million", type=float)
    return result


def _write(path: Path, report: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _model_port(args):
    # No env file is loaded implicitly. ModelService owns provider secret resolution.
    from compass_agent.api.models import create_model_service
    from compass_agent.llm.admission import ExecutionGate
    from compass_agent.models.entities import Provider
    from compass_agent.settings import get_settings
    from sqlalchemy import select
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session

    if os.environ.get("COMPASS_TOOL_EVAL") != "true":
        raise ValueError(
            "Model mode requires COMPASS_TOOL_EVAL=true in an isolated test environment."
        )
    database = make_url(get_settings().database_url)
    if database.get_backend_name() != "postgresql" or not (database.database or "").startswith(
        "compass_agent_test_"
    ):
        raise ValueError(
            "DATABASE_URL must point to an isolated compass_agent_test_* PostgreSQL database."
        )
    if not args.provider_id and not args.provider_alias:
        raise ValueError("Model mode requires --provider-id or --provider-alias.")
    service = create_model_service(ExecutionGate())
    with Session(service.repository.engine) as session:
        statement = select(Provider).where(Provider.enabled, Provider.kind == "llm")
        statement = (
            statement.where(Provider.id == args.provider_id)
            if args.provider_id
            else statement.where(Provider.alias == args.provider_alias)
        )
        provider = session.scalar(statement)
        if provider is None:
            raise ValueError("No enabled LLM provider matches the supplied selector.")
        provider_id, binding_version = provider.id, provider.binding_version
        model_config = {
            "provider_id": str(provider.id),
            "api_model": provider.api_model,
            "adapter": provider.adapter,
            "config_version": provider.version,
            "binding_version": binding_version,
        }
    context = TrustedContext(
        actor_user_id=uuid4(), scope=PlatformScope(kind="platform"), purpose="test"
    )

    async def invoke(messages: list[ChatMessage], definitions: list[ToolDefinition]) -> ModelTurn:
        request = GenerateInput(
            messages=messages,
            tools=definitions,
            stream=False,
            thinking=False,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
        )
        try:
            prepared = await service.prepare(
                uuid4(), provider_id, request, context, expected_binding_version=binding_version
            )
            result = await service.run(prepared, request, tool_output_validation="agent_dispatch")
        except Exception as error:
            # Exception strings may contain endpoints. Only bounded codes/types are exported.
            raw_code = str(getattr(error, "code", "model_call_failed"))
            code = raw_code if re.fullmatch(r"[a-z0-9_]{1,80}", raw_code) else "model_call_failed"
            return ModelTurn(
                "failed", "", [], metadata={"error_code": code, "error_type": type(error).__name__}
            )
        return ModelTurn(
            status=result.status,
            text=result.text or "",
            tool_calls=result.tool_calls,
            input_tokens=result.usage.input_tokens if result.usage else None,
            output_tokens=result.usage.output_tokens if result.usage else None,
            metadata={
                "call_id": str(result.call_id),
                "error_code": result.error_code,
                "requested_model": result.requested_model,
                "response_model": result.response_model,
                "config_version": result.config_version,
                "duration_ms": result.duration_ms,
                "usage": result.usage.model_dump() if result.usage else None,
            },
        )

    return invoke, model_config


async def execute(args) -> tuple[dict, Path]:
    if not 1 <= args.concurrency <= 3:
        raise ValueError("concurrency must be 1..3")
    if not 1 <= args.repetitions <= 20 or not 1 <= args.max_steps <= 12:
        raise ValueError("repetitions must be 1..20 and max-steps must be 1..12")
    if not 0 <= args.temperature <= 2 or not 1 <= args.max_tokens <= 32768:
        raise ValueError("temperature/max-tokens outside GenerateInput limits")
    prices = (args.input_usd_per_million, args.output_usd_per_million)
    if (prices[0] is None) != (prices[1] is None) or any(
        v is not None and (v < 0 or not v < float("inf")) for v in prices
    ):
        raise ValueError("supply both finite nonnegative token prices, or neither")
    if len(set(args.group)) != len(args.group):
        raise ValueError("groups must be unique")
    data = load_dataset()
    cases = [case for case in data["cases"] if not args.case_ids or case["id"] in args.case_ids]
    if not cases or args.case_ids and set(args.case_ids) - {c["id"] for c in cases}:
        raise ValueError("unknown or empty case selection")
    path = args.out or Path("work/tool-eval") / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json"
    )
    if path.exists():
        raise ValueError("output exists; choose a new report path")
    report = {
        "report_version": 1,
        "mode": args.mode,
        "started_at": datetime.now(UTC).isoformat(),
        "dataset_version": data["dataset_version"],
        "dataset_sha256": hashlib.sha256((DIRECTORY / "dataset.json").read_bytes()).hexdigest(),
        "baseline_sha256": hashlib.sha256(
            (DIRECTORY / "baseline_tools.json").read_bytes()
        ).hexdigest(),
        "sample_count_per_group": len(cases),
        "repetitions": args.repetitions,
        "configuration": {
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
            "max_steps": args.max_steps,
            "thinking": False,
            "concurrency": args.concurrency,
        },
        "cost_basis": {
            "input_usd_per_million": prices[0],
            "output_usd_per_million": prices[1],
            "kind": "usage times supplied price; not billed cost",
        },
        "limitations": [
            "合成工具回执；真实 SQL、Sandbox、审批、跨进程恢复与浏览器另作集成验收。",
            "自动结果依据公开确定性 rubric；最终答案质量需人工复核原始轨迹。",
            "声明占比按同一 UTF-8 bytes/4 估算；provider usage 独立保存。",
            "49 工具容量题中 A/B 平铺超出模型 32 工具上限时记录失败，不截断工具。",
        ],
        "runs": [],
        "summaries": [],
        "status": "running",
    }
    invoke = None
    if args.mode == "model":
        invoke, report["model"] = _model_port(args)
    _write(path, report)
    for repeat in range(args.repetitions):
        records = {group: [] for group in args.group}
        slots = asyncio.Semaphore(args.concurrency)
        for index, case in enumerate(cases):
            # Rotate group order to reduce systematic ordering effects, without changing cases.
            offset = (index + repeat) % len(args.group)
            order = args.group[offset:] + args.group[:offset]

            async def run_group(
                raw_group,
                current_case=case,
                current_repeat=repeat,
                gate=slots,
                group_records=records,
            ):
                async with gate:
                    group = cast(EvalGroup, raw_group)
                    if invoke is None:
                        trace = offline_case(group, current_case)
                        row = {
                            "group": group,
                            "repeat": current_repeat + 1,
                            "dimension": current_case["dimension"],
                            **trace,
                        }
                    else:
                        record, trace = await run_case(
                            group,
                            current_case,
                            invoke,
                            max_steps=args.max_steps,
                            input_usd_per_million=prices[0],
                            output_usd_per_million=prices[1],
                        )
                        group_records[group].append(record)
                        row = {
                            "group": group,
                            "repeat": current_repeat + 1,
                            "dimension": current_case["dimension"],
                            "record": record.to_dict(),
                            "trace": trace,
                        }
                    report["runs"].append(row)
                    _write(path, report)
                    print(
                        compact(
                            {
                                "group": group,
                                "repeat": current_repeat + 1,
                                "case": current_case["id"],
                                "status": trace["status"],
                            }
                        ),
                        flush=True,
                    )

            await asyncio.gather(*(run_group(group) for group in order))
        if invoke is not None:
            report["summaries"].extend(
                {"repeat": repeat + 1, **summarize_group(cast(EvalGroup, g), rows)}
                for g, rows in records.items()
            )
    report["status"] = "completed"
    report["finished_at"] = datetime.now(UTC).isoformat()
    _write(path, report)
    return report, path


def main():
    try:
        args = parser().parse_args()
        report, path = asyncio.run(execute(args))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except Exception as error:
        raise SystemExit(
            f"Evaluation stopped ({type(error).__name__}); inspect the partial report."
        ) from None
    print(
        compact(
            {"report": str(path.resolve()), "mode": report["mode"], "runs": len(report["runs"])}
        )
    )


if __name__ == "__main__":
    main()
