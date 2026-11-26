"""Loopback model and real OTLP HTTP receiver for isolated ContextOps acceptance."""

import gzip
import json
from time import time
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from . import context_runtime_provider as baseline
from .goal_runtime_provider import final, text, tool

app = FastAPI()
requests: list[dict[str, Any]] = []
traces: list[dict[str, Any]] = []
export_available = True
SENTINELS = (
    "sk-contextops-acceptance-secret-0123456789",
    "Bearer contextops-acceptance-token-0123456789",
    "contextops-private@example.test",
    "13800138000",
    "INJECTION-CONTEXTOPS-971",
)


def observation(value):
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.startswith("<untrusted_context"):
            candidate = candidate.partition(">")[2].rsplit("</untrusted_context>", 1)[0].strip()
        try:
            return observation(json.loads(candidate))
        except ValueError:
            return None
    if isinstance(value, list):
        return next((found for item in reversed(value) if (found := observation(item))), None)
    if isinstance(value, dict):
        if value.get("resultId") and "rows" in value:
            return value
        return observation(list(value.values()))
    return None


baseline.observation = observation


def walk(value):
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.startswith("<untrusted_context"):
            candidate = candidate.partition(">")[2].rsplit("</untrusted_context>", 1)[0].strip()
        try:
            yield from walk(json.loads(candidate))
        except (ValueError, RecursionError):
            return
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


@app.get("/inspection")
def inspection():
    encoded = json.dumps(requests, ensure_ascii=False)
    return {
        "model_calls": len(requests),
        "model_sentinel_matches": {s: encoded.count(s) for s in SENTINELS},
        "model_sentinel_locations": [
            {
                "call": index,
                "role": message["role"],
                "prefix": str(message.get("content") or "")[:120],
                "sentinels": [s for s in SENTINELS if s in json.dumps(message, ensure_ascii=False)],
            }
            for index, body in enumerate(requests)
            for message in body["messages"]
            if any(s in json.dumps(message, ensure_ascii=False) for s in SENTINELS)
        ],
        "spans": traces,
        "export_available": export_available,
    }


@app.post("/control/export")
async def control_export(request: Request):
    global export_available
    export_available = (await request.json())["available"] is True
    return {"available": export_available}


@app.post("/api/public/otel/v1/traces")
async def receive_traces(request: Request):
    if not export_available:
        return Response(status_code=503)
    body = await request.body()
    if request.headers.get("content-encoding") == "gzip":
        body = gzip.decompress(body)
    payload = ExportTraceServiceRequest()
    payload.ParseFromString(body)
    for resource in payload.resource_spans:
        for scope in resource.scope_spans:
            for span in scope.spans:
                traces.append(MessageToDict(span, preserving_proto_field_name=True))
    del traces[:-4096]
    return Response(content=b"", media_type="application/x-protobuf")


def message_for(body) -> dict[str, Any]:
    user = "\n".join(str(m.get("content") or "") for m in body["messages"] if m["role"] == "user")
    tools = {t["function"]["name"] for t in body.get("tools", [])}
    has_result = any(m["role"] == "tool" for m in body["messages"])
    objects = list(walk(body["messages"]))
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
    if "final_answer" in tools:
        source_safety = "验证来源文本安全" in user
        receipts = [o for o in objects if "evidenceId" in o and o.get("kind") == "query"]
        if has_result and (source_safety or "验证空结果" in user or "验证工具错误" in user):
            return final("已检查当前授权来源，查询状态已记录。")
        if receipts:
            return final("销售金额已核对。", receipts[-1]["evidenceId"])
        if has_result:
            return final("已核对当前查询结果，未形成可用数据结论。")
        query = (
            "SELECT note FROM core.channel_product_daily ORDER BY region_id"
            if source_safety
            else "SELECT region_id, amount FROM core.channel_product_daily WHERE amount < 0"
            if "验证空结果" in user
            else "SELECT unavailable_column FROM core.channel_product_daily"
            if "验证工具错误" in user
            else "SELECT sum(amount)::int AS amount FROM core.channel_product_daily"
        )
        return tool("database_query", {"op": "query", "sql": query}, "read_contextops")
    query = None
    if "验证空结果" in user:
        query = "SELECT region_id, amount FROM core.channel_product_daily WHERE amount < 0"
    elif "验证工具错误" in user:
        query = "SELECT unavailable_column FROM core.channel_product_daily"
    if query and "database_query" in tools and not body.get("response_format"):
        if has_result:
            return {"role": "assistant", "content": "已核对当前查询结果，未形成可用数据结论。"}
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_contextops_query",
                    "type": "function",
                    "function": {
                        "name": "database_query",
                        "arguments": json.dumps({"op": "query", "sql": query}),
                    },
                }
            ],
        }
    return baseline.fixture_message(body)


@app.post("/{path:path}")
async def completion(request: Request):
    body = await request.json()
    requests.append(body)
    del requests[:-1024]
    message = message_for(body)
    reason = "tool_calls" if message.get("tool_calls") else "stop"
    common = {"id": "contextops-acceptance", "model": body["model"], "created": int(time())}
    usage = {"prompt_tokens": 31, "completion_tokens": 13, "total_tokens": 44}
    if body.get("thinking", {}).get("type") == "enabled":
        message["reasoning_content"] = "核对当前授权范围的可见查询结果。"
    if not body.get("stream"):
        return {
            **common,
            "object": "chat.completion",
            "usage": usage,
            "choices": [{"index": 0, "message": message, "finish_reason": reason}],
        }
    if reason == "tool_calls":
        for index, tool in enumerate(message["tool_calls"]):
            tool["index"] = index

    async def chunks():
        for delta, finish in ((message, None), ({}, reason)):
            chunk: dict[str, Any] = {
                **common,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if finish:
                chunk["usage"] = usage
            yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(chunks(), media_type="text/event-stream")
