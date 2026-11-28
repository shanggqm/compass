"""Controlled HTTP provider used only by tests and the browser test host."""

import asyncio
import json
from typing import Any

import httpx


def schema_value(schema):
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type", "object")
    if kind == "object":
        return {k: schema_value(v) for k, v in schema.get("properties", {}).items()}
    if kind == "array":
        return [schema_value(schema.get("items", {"type": "string"}))]
    if kind == "string":
        return "测试值"
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    return schema.get("minimum", 1)


class FixtureStream(httpx.AsyncByteStream):
    def __init__(self, chunks, delay: float = 0):
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


class ProviderFixture:
    def __init__(self):
        self.requests = []
        self.streams = []

    @staticmethod
    def routing_scene(text):
        # Controlled upstream answers for protocol/E2E tests, never production classification.
        if any(word in text for word in ["讲个笑话", "你好呀", "天气怎么样"]):
            return "SC-39"
        if "销售持续下降" in text:
            return "SC-03"
        return "SC-02"

    def transport(self):
        return httpx.MockTransport(self.handle)

    async def handle(self, request):
        self.requests.append(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/systemone"):
            answers = {}
            intent_scene = None
            if set(body["questions"]) == {"scene"}:
                context = json.dumps(body["state"], ensure_ascii=False)
                intent_scene = self.routing_scene(context)
            for key, question in body["questions"].items():
                kind = question["type"]
                criteria = question.get("criteria")
                if kind == "noul":
                    answers[key] = {"type": kind, "noul": 0.8}
                elif kind == "choice":
                    keys = list(criteria)
                    if intent_scene:
                        keys.remove(intent_scene)
                        keys.insert(0, intent_scene)
                    answers[key] = {
                        "type": kind,
                        "choice": keys[0],
                        "probabilities": {k: float(k == keys[0]) for k in keys},
                        "confidence": 1.0,
                    }
                else:
                    answers[key] = {
                        "type": kind,
                        "score": 0,
                        "probabilities": {str(i): float(i == 0) for i in range(len(criteria))},
                        "legend": {str(i): v for i, v in enumerate(criteria)},
                        "confidence": 1.0,
                    }
            return httpx.Response(
                200,
                json={
                    "answers": answers,
                    "model": body["model"],
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                },
            )
        messages = body["messages"]
        text = " ".join(str(m.get("content", "")) for m in messages)
        if "E2E_AUTH_FAIL" in text:
            return httpx.Response(401, json={"error": {"message": "private upstream body"}})
        if "E2E_RETRY_400" in text:
            return httpx.Response(400, json={"error": {"message": "unsupported messages"}})
        if "E2E_RETRY_429_ONCE" in text and "你是Compass意图分类器" not in text:
            matched = [r for r in self.requests if r.content == request.content]
            if len(matched) == 1:
                return httpx.Response(429, json={"error": {"message": "controlled transient"}})
        tools = []
        if body.get("tools") and not any(m["role"] == "tool" for m in messages):
            for index, definition in enumerate(body["tools"]):
                fn = definition["function"]
                tools.append(
                    {
                        "id": "fixture_tool_" + str(index + 1),
                        "type": "function",
                        "function": {
                            "name": fn["name"],
                            "arguments": json.dumps(schema_value(fn["parameters"])),
                        },
                    }
                )
        answer = (
            "工具结果已接收。"
            if any(m["role"] == "tool" for m in messages)
            else "这是模型测试回复。"
        )
        if body.get("response_format"):
            schema = None
            for message in messages:
                content = message.get("content") or ""
                marker = "Follow this JSON Schema: "
                if marker in content:
                    schema = json.loads(content.split(marker, 1)[1])
            answer = json.dumps(
                schema_value(schema) if schema else {"answer": "测试回复"}, ensure_ascii=False
            )
        if "你是Compass意图分类器" in text:
            scene = self.routing_scene(text)
            answer = json.dumps({"scene_id": scene})
        elif "用户本轮问题属于与业务数据分析无关" in text:
            # This controlled response exercises the plain reply/partial-stream path.
            tools = []
            answer = (
                "我主要帮助处理经营与业务数据分析。你有没有想分析的销售、库存或其他业务数据任务？"
            )
        finish = "tool_calls" if tools else "stop"
        message: dict[str, Any] = {"role": "assistant", "content": "" if tools else answer}
        if tools:
            message["tool_calls"] = tools
        if body.get("thinking", {}).get("type") == "enabled":
            message["reasoning_content"] = "private reasoning must never be rendered or persisted"
        common = {"id": "fixture-response", "model": body["model"], "created": 1}
        if not body.get("stream"):
            return httpx.Response(
                200,
                json={
                    **common,
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            )
        chunks = []

        def add(delta, reason=None):
            chunk = {
                **common,
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": reason,
                    }
                ],
            }
            chunks.append(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())

        if "reasoning_content" in message:
            add({"reasoning_content": message["reasoning_content"]})
        if tools:
            if len(tools) == 1:
                add({"tool_calls": [{"index": 0, **tools[0]}]})
            else:
                for index, tool in enumerate(tools):
                    name = tool["function"]["name"]
                    split = max(1, len(name) // 2)
                    add(
                        {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": tool["id"],
                                    "type": "function",
                                    "function": {"name": name[:split], "arguments": ""},
                                }
                            ]
                        }
                    )
                for index in reversed(range(len(tools))):
                    tool = tools[index]
                    name = tool["function"]["name"]
                    split = max(1, len(name) // 2)
                    add(
                        {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "function": {
                                        "name": name[split:],
                                        "arguments": tool["function"]["arguments"],
                                    },
                                }
                            ]
                        }
                    )
        else:
            for character in answer:
                add({"content": character})
        add({}, finish)
        if "E2E_TRUNCATED" not in text:
            chunks.append(b"data: [DONE]\n\n")
        stream = FixtureStream(chunks, 0.5 if "E2E_SLOW" in text else 0)
        self.streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
