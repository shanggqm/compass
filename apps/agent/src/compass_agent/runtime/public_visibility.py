"""Deterministic tool summaries and current-view filtering shared by snapshot and SSE."""

from copy import deepcopy

from compass_agent.schemas import AgentResponse

SUMMARY = {"policy": "summary", "inputPreview": None, "outputPreview": None, "language": "text"}


def managed_events(store, tx, run, events):
    """Only publish progress metadata. Authorized reads hydrate the final private answer."""
    output = []
    for event in events:
        from compass_agent.runtime.answer_publication import marker_data

        if marker_data(event):
            # The chunk is already encrypted; this ordered reference carries no answer text.
            output.append(event)
            continue
        if event["type"] == "message.delta":
            continue
        if event["type"] == "view.upsert":
            kind = event["payload"]["kind"]
            if kind == "artifact":
                output.append(event)
                continue
            from compass_agent.context_runtime.source_access import save_view

            original = (
                search_event(event)
                if run.config_snapshot.get("searchSnapshot")
                else public_event(event)
            )
            save_view(store, tx, run, original)
            if kind == "execution.node":
                event = public_event(event, details=False)
                node = event["payload"]["value"]
                node["title"] = {"model": "生成", "tool": "执行工具"}.get(node["kind"], "执行任务")
                node["summary"] = None
            else:
                meta = original["payload"]
                event = {
                    "type": "view.upsert",
                    "payload": {
                        "objectId": meta["objectId"],
                        "revision": meta["revision"],
                        "kind": "timeline",
                        "value": {
                            "ordinal": meta["value"].get("ordinal", 1),
                            "event": {
                                "type": "stream.extension",
                                "payload": {
                                    "name": "compass.runtime.protected_view",
                                    "version": 1,
                                    "required": False,
                                    "data": {},
                                },
                            },
                        },
                    },
                }
        output.append(event)
    return output


def public_event(event, *, details=True):
    value = deepcopy(event)
    if value["type"] != "view.upsert":
        return value
    obj = value["payload"]
    if obj["kind"] == "execution.node":
        node = obj["value"]
        # Legacy preview strings predate the authorized execution detail projection.
        node["display"] = dict(SUMMARY)
        if not details:
            node.pop("detail", None)
            if (node.get("origin") or {}).get("kind") == "mcp":
                node["origin"] = None
        if node.get("executor"):
            node["executor"]["environmentId"] = None
        if node["kind"] == "tool" and not details:
            node["title"] = {
                "retail_sales": "比较渠道实付金额",
                "database_query": "查询经营数据",
                "python_analysis": "生成分析图表",
            }.get((node.get("origin") or {}).get("toolName", ""), "工具调用")
            node["summary"] = {
                "completed": "工具已完成。",
                "empty": "未查询到结果。",
                "failed": "工具未完成。",
                "waiting": "等待执行确认。",
                "canceled": "调用已终止。",
                "incomplete": "结果待确认。",
            }.get(node["status"], "正在执行工具。")
    elif obj["kind"] == "timeline":
        progress = obj["value"]["event"]
        payload = progress["payload"]
        if progress["type"] == "tool.started":
            payload["inputPreview"] = None
            if not details:
                payload["params"] = None
        elif progress["type"] == "tool.finished":
            payload["resultPreview"] = None
            if not details:
                obj["value"]["event"] = {
                    "type": "stream.extension",
                    "payload": {
                        "name": "compass.runtime.tool_summary",
                        "version": 1,
                        "required": False,
                        "data": {"status": payload["status"], "summary": "工具调用状态已更新。"},
                    },
                }
    return value


def search_event(event):
    """Expose execution metadata even to an authorized viewer; drafts stay private."""
    value = public_event(event, details=False)
    if value["type"] != "view.upsert":
        return value
    if value["payload"]["kind"] == "execution.node":
        node = value["payload"]["value"]
        if node["kind"] == "model":
            node.update(title="分析请求", summary=None)
        elif node["kind"] == "tool":
            node["title"] = {
                "search_semantics": "查找指标口径",
                "get_metric_definition": "读取指标定义",
                "resolve_semantics": "确认适用口径",
                "query_metric": "查询指标",
                "database_query": "查询经营数据",
                "search_documents": "查找资料",
                "read_document": "读取原文",
                "todo_write": "更新任务计划",
            }.get((node.get("origin") or {}).get("toolName") or "", "调用工具")
    elif value["payload"]["kind"] == "timeline":
        progress = value["payload"]["value"]["event"]
        payload = progress["payload"]
        if progress["type"] == "stream.extension" and payload["name"] == "compass.autonomy.action":
            # Task goals are model-authored and can contain an unverified answer.
            payload["data"] = {
                **payload["data"],
                "items": [],
                "todoId": None,
                "selectedTodoId": None,
            }
    return value


def visible_response(response, *, details):
    value = response.model_dump(mode="json", by_alias=True)
    for packet in value["packets"]:
        packet["event"] = public_event(packet["event"], details=details)
    return AgentResponse.model_validate(value)
