"""Shared transport and human confirmation used by the seven registered tools."""

import asyncio
from uuid import uuid5

import psycopg
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from compass_agent.models.errors import ModelError
from compass_agent.prompt_assets import tool_text
from compass_agent.runtime.private_state import digest
from compass_agent.schemas import ToolApprovalProposal, ToolDefinition, ToolFunction
from compass_agent.semantic.client import SourceUnavailable
from compass_agent.semantic.draft_fields import authored_preview
from compass_agent.semantic.evidence import result as evidence_result
from compass_agent.tools.catalog_contracts import ToolSpec
from compass_agent.tools.contracts import (
    ExecutionReceipt,
    InProcessBinding,
    PreparedApproval,
    RegisteredTool,
    ToolPolicy,
    ToolRejected,
)
from compass_agent.tools.registry import bounded_payload, parse_arguments


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Parameters(Arguments):
    start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    as_of: str | None = Field(default=None, min_length=16, max_length=40)
    group_by: str | None = Field(default=None, max_length=120)
    filters: dict[str, str | int | bool] = Field(default_factory=dict, max_length=32)


def tool(name, op, capability, args_type):
    def definition(_context):
        return ToolDefinition(
            function=ToolFunction(
                name=name,
                description=tool_text(name),
                parameters=args_type.model_json_schema(),
            )
        )

    def register(context):
        service = context.semantics
        assert service is not None

        def authorize(args, execution):
            execution.require_running()
            if execution.caller != "model":
                raise ToolRejected("semantic_model_required")

        def arguments(raw):
            try:
                return args_type.model_validate(raw).model_dump(mode="json", exclude_none=True)
            except ValidationError:
                raise ToolRejected("invalid_parameters") from None

        async def execute(raw, execution):
            try:
                result = await service.run(op, arguments(raw), execution)
                if result.get("status") == "unavailable_binding":
                    from compass_agent.tools.errors import ActionableToolError

                    result = {
                        **ActionableToolError(
                            "semantic_binding_unavailable",
                            "switch_tool",
                            "定义可用，尚无已发布执行绑定。",
                            suggested_tool="database_query",
                            next_action=(
                                "按用户确认的定义，使用当前获准的只读明细完成即时计算并说明计算口径；"
                                "保留权限检查，缺少字段或权限时说明缺口。"
                            ),
                        ).payload(),
                        **result,
                    }
                if op == "query" and result.get("resultId"):
                    from compass_agent.context_runtime.source_access import require_run_sources
                    from compass_agent.tools.contracts import AuthorizedInput

                    async def revalidate_source():
                        await asyncio.to_thread(
                            require_run_sources,
                            service.store,
                            service.journal.run_id,
                            execution.trusted,
                            epoch=service.journal.epoch,
                        )
                        return True

                    result = {**result, "sourceQueryId": result["resultId"]}
                    execution.sources[result["resultId"]] = AuthorizedInput(
                        execution.run_id, execution.owner, result, revalidate_source
                    )
                return ExecutionReceipt(execution.call_id, "ok", "confirmed", result)
            except SourceUnavailable as exc:
                if not getattr(getattr(service, "journal", None), "search_snapshot", None):
                    raise ToolRejected(exc.code) from None
                code = "rate_limited" if exc.status == 429 else "semantic_service_unavailable"
                payload = {"status": "unavailable", "retryable": True, "code": code}
                if exc.retry_after is not None:
                    payload["retryAfterSeconds"] = exc.retry_after
                return ExecutionReceipt(execution.call_id, "error", "confirmed", payload, code)
            except ModelError as exc:
                raise ToolRejected(exc.code) from None
            except (psycopg.Error, KeyError, ValueError):
                raise ToolRejected("semantic_source_unavailable") from None

        async def prepare(raw, execution):
            try:
                return await proposal_for(raw, execution)
            except ModelError as exc:
                raise ToolRejected(exc.code) from None

        async def proposal_for(raw, execution):
            args = arguments(raw)
            options = None
            fields = None
            revision = None
            if op == "resolve":
                probe = {
                    k: v for k, v in args.items() if k not in {"parameters", "historical_as_of"}
                }
                result = await service.api(op, probe)
                if result["status"] != "needs_clarification":
                    return None
                saved = await asyncio.to_thread(service.choice, args["mention"])
                if saved and saved.get("customDefinition"):
                    return None
                if saved and any(
                    (v["asset_id"], v["version_id"]) == (saved["asset_id"], saved["version_id"])
                    for v in result["options"]
                ):
                    return None
                from compass_agent.semantic.clarification import options as choice_options

                options = choice_options(result["options"])
                kind = "semantic_choice"
                summary = "请选择本次查询使用的指标口径。"
            elif op == "discover" and args["trigger"] == "suggest":
                if args.get("source_refs") or args.get("revise_metric"):
                    raise ToolRejected("invalid_parameters")
                return None
            elif op == "discover" and args.get("revise_metric"):
                if (
                    not args.get("fields")
                    or set(raw["fields"]) != set(args["fields"])
                    or set(args) - {"revise_metric", "fields", "trigger"}
                    or args["trigger"] != "requested"
                ):
                    raise ToolRejected("invalid_parameters")
                view = await service.api(
                    "revision-lookup",
                    {"metric": args["revise_metric"], "changes": args["fields"]},
                )
                if view.get("status"):
                    return None
                if not view["changes"]:
                    raise ToolRejected("semantic_definition_unchanged")
                revision = {
                    "asset_id": view["asset_id"],
                    "expected_digest": view["expected_digest"],
                    "changes": {c["field"]: c["after"] for c in view["changes"]},
                }
                kind = "semantic_revision"
                summary = "请核对原指标与拟修改的字段，确认后将修订保存到同一指标。"
            elif op == "discover" and (ref := await service.requested_result(args)):
                ref, source = await asyncio.to_thread(service.open_result, ref)
                args["result_ref"] = ref
                from compass_agent.context_runtime.source_access import allows

                if not await asyncio.to_thread(
                    allows,
                    service.store,
                    service.trusted,
                    source["refs"],
                    epoch=service.journal.epoch,
                ):
                    raise ToolRejected("semantic_reference_expired")
                from compass_agent.semantic.service import session_results

                available = await asyncio.to_thread(
                    session_results, service.journal, service.trusted
                )
                selected = next(
                    (r for r in available if r["result_ref"] == args["result_ref"]), None
                )
                if not selected:
                    raise ToolRejected("semantic_invitation_unavailable")
                chosen = await asyncio.to_thread(service.chosen_result)
                if len(available) > 1 and chosen != args["result_ref"]:
                    kind = "semantic_result_choice"
                    options = [
                        {
                            "id": r["result_ref"],
                            "versionId": r["result_ref"],
                            "name": r["title"],
                            "description": r["created_at"],
                            "unit": "",
                            "version": 1,
                        }
                        for r in sorted(available, key=lambda r: r["result_ref"] != ref)
                    ]
                    summary = "请选择要保存的临时查询结果。"
                else:
                    fields = authored_preview(source, args.get("fields"), ref)
                    if not fields:
                        return None
                    kind = "semantic_draft"
                    summary = "请核对来自本次临时查询的定义，确认后保存为指标草稿。"
            elif op == "submit":
                view = await service.api("validate", {"version_id": args["version_id"]})
                if view["digest"] != args["expected_digest"] or any(
                    f["severity"] == "error" for f in view["findings"]
                ):
                    raise ToolRejected("semantic_validation_failed")
                kind = "semantic_submit"
                summary = "请确认将当前指标版本送交业务专家评审。"
            else:
                return None
            proposal = ToolApprovalProposal(
                proposalId=str(
                    uuid5(execution.run_id, f"{name}:{execution.step_id}:{execution.call_id}")
                ),
                toolName=name,
                toolCallId=execution.call_id,
                argumentsDigest=digest(args),
                summary=summary,
            )
            return PreparedApproval(
                proposal,
                {
                    "kind": kind,
                    **({"revision": revision} if revision else {}),
                    "options": options,
                    "fields": fields,
                    **({"result_ref": args["result_ref"]} if args.get("result_ref") else {}),
                },
            )

        return RegisteredTool(
            name,
            definition(context),
            ToolPolicy(),
            InProcessBinding(execute),
            parse_arguments,
            authorize,
            bounded_payload,
            prepare_approval=prepare if op in {"resolve", "submit", "discover"} else None,
            local_effect=op in {"submit", "discover"},
            parallel_safe=op in {"definition", "resolve", "query", "validate"},
        )

    return ToolSpec(
        name,
        "business",
        "semantic_" + capability,
        definition,
        lambda ctx: ctx.semantics is not None,
        context_factory=register,
        evidence_adapter=evidence_result,
    )
