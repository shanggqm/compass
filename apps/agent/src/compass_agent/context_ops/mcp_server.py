"""Stateless Streamable HTTP MCP with four read-only tools and scope-bound credentials."""

import asyncio
import json
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import Field, ValidationError
from sqlalchemy.orm import Session

from compass_agent import schemas as dto
from compass_agent.api.models import trusted_context
from compass_agent.api.sessions import Runtime
from compass_agent.context_ops.analytics import Analytics
from compass_agent.context_ops.recorder import audit
from compass_agent.context_ops.snapshots import Snapshots
from compass_agent.models.errors import ModelError
from compass_agent.models.secrets import SecretStore
from compass_agent.settings import get_settings

router = APIRouter(prefix="/v1/contextops", tags=["contextops-mcp"])
PROTOCOL = "2025-06-18"


class RunInput(dto.StrictModel):
    run_id: UUID


class StatsInput(dto.StrictModel):
    window: dto.ContextWindow = "24h"
    actor_id: UUID | None = None
    kind: Literal["overview", "intents", "anomalies"] = "overview"


class BadcasesInput(dto.StrictModel):
    root_cause_category: dto.ContextRootCause | None = None
    scene_id: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=50, ge=1, le=100)


TOOLS = {
    "contextops_list_runs": (dto.ContextOpsFilter, "List authorized runs and sanitized summaries"),
    "contextops_get_run_flow": (RunInput, "Read content-free run flow and decision metadata"),
    "contextops_get_stats": (StatsInput, "Read aggregate runtime statistics"),
    "contextops_list_badcases": (BadcasesInput, "List sanitized badcases without snapshot bodies"),
}


def issue_token(settings, context):
    if context.scope.kind != "tenant" or context.purpose not in {
        "contextops_read",
        "contextops_admin",
    }:
        raise ModelError("forbidden", 403)
    return "ctxmcp_" + SecretStore(settings.compass_model_encryption_key).seal(
        "contextops_read", context.model_dump(mode="json")
    )


async def identity(
    request: Request,
    runtime: Runtime,
    authorization: Annotated[str | None, Header()] = None,
    x_compass_context: Annotated[str | None, Header()] = None,
):
    settings = get_settings()
    if not settings.compass_contextops_mcp_enabled:
        raise ModelError("not_found", 404)
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != request.url.netloc:
            raise ModelError("forbidden", 403)
    version = request.headers.get("mcp-protocol-version")
    if version and version != PROTOCOL:
        raise ModelError("validation_failed", 400)
    if authorization and authorization.startswith("Bearer ctxmcp_"):
        try:
            context = dto.TrustedContext.model_validate(
                SecretStore(settings.compass_model_encryption_key).open(
                    authorization.removeprefix("Bearer ctxmcp_"), "contextops_read", ttl=3600
                )
            )
        except (ModelError, ValidationError):
            raise ModelError("unauthenticated", 401) from None
    else:
        context = trusted_context(authorization, x_compass_context)
    if context.scope.kind != "tenant" or context.purpose not in {
        "contextops_read",
        "contextops_admin",
    }:
        raise ModelError("forbidden", 403)
    await runtime.access.check(context)
    return context


Identity = Annotated[dto.TrustedContext, Depends(identity)]


def call_tool(store, context, name, arguments, observation=None):
    schema, _ = TOOLS[name]
    data = schema.model_validate(arguments)
    # A successful audit commit precedes the query result, including failed source checks.
    with Session(store.engine) as tx, tx.begin():
        audit(tx, context, "mcp_queried", "allow_audit", "mcp_read", target_ref=name)
    admin = context.purpose == "contextops_admin"
    analytics = Analytics(store, context, admin, observation)
    if name == "contextops_list_runs":
        result = analytics.list_runs(data)
    elif name == "contextops_get_run_flow":
        result = analytics.flow(data.run_id)
    elif name == "contextops_get_stats":
        filters = dto.ContextOpsFilter(window=data.window, actor_id=data.actor_id)
        result = getattr(analytics, data.kind)(filters)
    else:
        result = Snapshots(store, context, admin).list_badcases(**data.model_dump())
    value = result.model_dump(mode="json")
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "structuredContent": value,
        "isError": False,
    }


@router.get("/mcp", status_code=405)
async def no_stream(context: Identity):
    return Response(status_code=405, headers={"Allow": "POST", "Cache-Control": "no-store"})


@router.post("/mcp", response_model=dto.ContextMcpResponse, response_model_exclude_none=True)
async def mcp(body: dto.ContextMcpRequest, context: Identity, runtime: Runtime):
    def error(code, message):
        return dto.ContextMcpResponse(id=body.id, error={"code": code, "message": message})

    if body.id is None:
        if body.method == "notifications/initialized":
            return Response(status_code=202, headers={"Cache-Control": "no-store"})
        return error(-32600, "Invalid request")
    if body.method == "initialize":
        result = {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "compass-contextops", "version": "1.0.0"},
        }
    elif body.method == "ping":
        result = {}
    elif body.method == "tools/list":
        result = {
            "tools": [
                {
                    "name": name,
                    "description": description,
                    "inputSchema": schema.model_json_schema(),
                    "annotations": {
                        "readOnlyHint": True,
                        "destructiveHint": False,
                        "openWorldHint": False,
                    },
                }
                for name, (schema, description) in TOOLS.items()
            ]
        }
    elif body.method == "tools/call":
        name = body.params.get("name")
        if not isinstance(name, str) or name not in TOOLS:
            return error(-32602, "Unknown read-only tool")
        if set(body.params) - {"name", "arguments", "_meta"}:
            return error(-32602, "Invalid parameters")
        try:
            result = await asyncio.to_thread(
                call_tool, runtime.store, context, name, body.params.get("arguments", {})
            )
        except ValidationError:
            return error(-32602, "Invalid parameters")
        except ModelError as failure:
            result = {"content": [{"type": "text", "text": failure.code}], "isError": True}
    else:
        return error(-32601, "Method not found")
    return dto.ContextMcpResponse(id=body.id, result=result)
