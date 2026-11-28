"""One frozen MCP catalog per Run; remote annotations never grant local execution rights."""

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid5

from jsonschema import Draft202012Validator
from mcp.types import CallToolResult, TextContent, Tool

from compass_agent.schemas import ToolApprovalProposal, ToolDefinition, ToolFunction
from compass_agent.tools.contracts import (
    ExecutionBinding,
    ExecutionReceipt,
    McpBinding,
    RegisteredTool,
    ToolExecutionContext,
    ToolPolicy,
    ToolRejected,
    json_bytes,
)
from compass_agent.tools.mcp.client import EndpointPolicy, McpSession
from compass_agent.tools.mcp.repository import McpConfigStore, Owner
from compass_agent.tools.registry import (
    ToolRegistry,
    bounded_payload,
    parse_arguments,
    validate_catalog,
    validate_schema,
)


def tool_name(connection_id, name):
    return f"mcp_{connection_id.hex}_{hashlib.sha256(name.encode()).hexdigest()[:24]}"


def schema_hash(tool: Tool):
    return hashlib.sha256(
        json_bytes({"input": tool.input_schema, "output": tool.output_schema})
    ).hexdigest()


@dataclass(frozen=True)
class DiscoverySummary:
    loaded: int
    skipped: int


class McpRun:
    def __init__(
        self,
        repository: McpConfigStore,
        policy: EndpointPolicy,
        owner: Owner,
        *,
        defer_catalog_limit: bool = False,
        selected_connection_ids: frozenset | None = None,
    ):
        self.defer_catalog_limit = defer_catalog_limit
        self.selected_connection_ids = selected_connection_ids
        self.repository, self.policy, self.owner = repository, policy, owner
        self.sessions: dict = {}
        self.entries: tuple[RegisteredTool, ...] = ()
        self.summary = DiscoverySummary(0, 0)
        self.required_tools: frozenset[str] = frozenset()

    async def open(self):
        configs = await asyncio.to_thread(self.repository.load, self.owner)
        if self.selected_connection_ids is not None:
            configs = [c for c in configs if c.id in self.selected_connection_ids]
        if len(configs) > (8 if self.selected_connection_ids is not None else 4):
            raise ToolRejected("mcp_connection_limit")
        slots = asyncio.Semaphore(2)
        self.sessions = {c.id: McpSession(c, self.policy) for c in configs}
        entries = []
        skipped = 0

        async def load(session):
            try:
                self.policy.validate(session.config.endpoint)
                await session.start(slots)
                by_name = {t.name: t for t in session.tools}
                selected = session.config.selected_tools
                names = list(by_name) if selected == "all" else list(selected)
                if any(name not in by_name for name in names):
                    raise ToolRejected("mcp_selection_stale")
                policies = {
                    p.name: ToolPolicy(p.effect, p.approval) for p in session.config.tool_policies
                }
                return [
                    self._registration(
                        session,
                        by_name[name],
                        policies.get(name, ToolPolicy("unknown", "required")),
                    )
                    for name in names
                ]
            except Exception:
                await session.close()
                if session.config.required:
                    raise ToolRejected("mcp_required_unavailable") from None
                return None

        jobs = {asyncio.create_task(load(s)): s for s in self.sessions.values()}
        try:
            done, pending = await asyncio.wait(jobs, timeout=10) if jobs else (set(), set())
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if any(jobs[task].config.required for task in pending):
                raise ToolRejected("mcp_required_unavailable")
            values = [task.result() if task in done else None for task in jobs]
            for value in values:
                if value is None:
                    skipped += 1
                else:
                    entries.extend(value)
            for session in self.sessions.values():
                if session.client is not None and not await asyncio.to_thread(
                    self.repository.current, session.config
                ):
                    raise ToolRejected("tool_config_changed")
            if self.defer_catalog_limit:
                if len({entry.name for entry in entries}) != len(entries):
                    raise ToolRejected("tool_name_conflict")
            else:
                ToolRegistry(entries)
                validate_catalog([e.definition for e in entries if e.definition])
            self.entries = tuple(entries)
            self.required_tools = frozenset(
                entry.name
                for entry in self.entries
                if isinstance(entry.binding, McpBinding)
                and self.sessions[entry.binding.connection_id].config.required
            )
            self.summary = DiscoverySummary(len(entries), skipped)
            return self
        except BaseException:
            for task in jobs:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await self.close()
            raise

    def _registration(self, session, remote, policy):
        validate_schema(remote.input_schema)
        if remote.output_schema is not None:
            validate_schema(remote.output_schema)
        config = session.config
        name = tool_name(config.id, remote.name)
        digest = schema_hash(remote)
        binding = McpBinding(config.id, config.revision, remote.name, digest)

        def authorize(args, context):
            if context.owner != config.owner:
                raise ToolRejected("mcp_scope_denied")
            if session.changed or session.client is None:
                raise ToolRejected("mcp_catalog_stale")

        async def revalidate(context):
            authorize(None, context)
            if not await asyncio.to_thread(self.repository.current, config):
                raise ToolRejected("mcp_configuration_stale")

        def proposal(args, context):
            return ToolApprovalProposal(
                proposalId=str(uuid5(context.run_id, f"{context.step_id}:{context.call_id}")),
                toolName=name,
                toolCallId=context.call_id,
                argumentsDigest=hashlib.sha256(json_bytes(args)).hexdigest(),
                summary=f"审批后调用 MCP 工具：{config.name} / {remote.name}",
                connectionId=str(config.id),
                connectionRevision=config.revision,
                schemaHash=digest,
            )

        description = (
            (f"{config.name} / {remote.name}: " + (remote.description or ""))
            .encode()[:2000]
            .decode(errors="ignore")
        )
        from compass_agent.context_runtime.receipt_evidence import receipt
        from compass_agent.tools.contracts import ToolGovernance

        return RegisteredTool(
            name,
            ToolDefinition(
                type="function",
                function=ToolFunction(
                    name=name, description=description, parameters=remote.input_schema
                ),
            ),
            policy,
            binding,
            parse_arguments,
            authorize,
            bounded_payload,
            proposal,
            parallel_safe=True,
            mcp_server_id=config.id,
            governance=ToolGovernance(
                subagent_allowed=policy.effect == "read_only" and policy.approval == "none"
            ),
            revalidate=revalidate,
            evidence_adapter=receipt,
        )

    async def execute(
        self, binding: ExecutionBinding, args: object, context: ToolExecutionContext
    ) -> ExecutionReceipt:
        if not isinstance(binding, McpBinding) or not isinstance(args, dict):
            raise ToolRejected("invalid_tool_binding")
        session = self.sessions.get(binding.connection_id)
        if session is None or context.owner != self.owner:
            raise ToolRejected("mcp_scope_denied")
        if not await asyncio.to_thread(self.repository.current, session.config):
            raise ToolRejected("mcp_configuration_stale")
        remote = next((t for t in session.tools if t.name == binding.remote_name), None)
        if (
            remote is None
            or schema_hash(remote) != binding.schema_hash
            or session.config.revision != binding.revision
        ):
            raise ToolRejected("mcp_catalog_stale")
        context.require_running()

        async def watch_revision():
            while True:
                await asyncio.sleep(0.25)
                if not await asyncio.to_thread(self.repository.current, session.config):
                    raise ToolRejected("tool_config_changed")

        invocation = asyncio.create_task(
            session.call(binding.remote_name, args, min(20, context.control.remaining))
        )
        watcher = asyncio.create_task(watch_revision())
        try:
            done, _ = await asyncio.wait({invocation, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)
                return ExecutionReceipt(
                    context.call_id, "unknown", "unconfirmed", reason_code="tool_config_changed"
                )
            result = invocation.result()
        except ToolRejected:
            raise
        except (TimeoutError, asyncio.CancelledError):
            # MCP cancellation leaves remote effects unconfirmed.
            return ExecutionReceipt(
                context.call_id, "unknown", "unconfirmed", reason_code="mcp_remote_unknown"
            )
        except Exception:
            return ExecutionReceipt(
                context.call_id, "unknown", "unconfirmed", reason_code="mcp_remote_unknown"
            )
        finally:
            for task in (invocation, watcher):
                if not task.done():
                    task.cancel()
            await asyncio.gather(invocation, watcher, return_exceptions=True)
        try:
            if session.changed or not await asyncio.to_thread(
                self.repository.current, session.config
            ):
                return ExecutionReceipt(
                    context.call_id, "rejected", "remote_managed", reason_code="tool_config_changed"
                )
            if not isinstance(result, CallToolResult):
                raise ToolRejected("mcp_result_unsupported")
            if any(not isinstance(c, TextContent) for c in result.content):
                raise ToolRejected("mcp_content_unsupported")
            if result.is_error:
                return ExecutionReceipt(
                    context.call_id, "error", "remote_managed", reason_code="mcp_tool_error"
                )
            payload = {
                "text": [c.text for c in result.content if isinstance(c, TextContent)],
                "structuredContent": result.structured_content,
            }
            from compass_agent.execution_context import current_execution

            if getattr(current_execution.get(), "context_results", None) is None:
                bounded_payload(payload)
            if remote.output_schema and not Draft202012Validator(remote.output_schema).is_valid(
                cast(Any, result.structured_content)
            ):
                raise ToolRejected("mcp_invalid_result")
            return ExecutionReceipt(
                context.call_id,
                "ok",
                "remote_managed",
                payload,
                environment_id=str(binding.connection_id),
            )
        except ToolRejected as error:
            return ExecutionReceipt(
                context.call_id, "error", "remote_managed", reason_code=error.code
            )

    async def close(self):
        await asyncio.gather(*(s.close() for s in self.sessions.values()), return_exceptions=True)
