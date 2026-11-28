"""Task-owned discovery and registry assembly; infrastructure capacity is process-owned."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from compass_agent.schemas import TrustedContext
from compass_agent.tools.artifacts import ArtifactStore
from compass_agent.tools.contracts import (
    AuthorizedInput,
    RegisteredTool,
    ToolBatchPolicy,
    ToolControl,
)
from compass_agent.tools.dispatcher import ExecutionSlots, ToolDispatcher
from compass_agent.tools.mcp.client import EndpointPolicy
from compass_agent.tools.mcp.repository import McpConfigStore, owner_key
from compass_agent.tools.mcp.service import McpRun
from compass_agent.tools.registry import ToolRegistry
from compass_agent.tools.sandbox.client import SandboxBackend


@dataclass
class RunTools:
    registry: ToolRegistry
    dispatcher: ToolDispatcher
    policy: ToolBatchPolicy
    sources: dict[str, AuthorizedInput] = field(default_factory=dict, repr=False)


class ToolLoader:
    def __init__(
        self,
        *,
        repository: McpConfigStore | Callable[[], McpConfigStore] | None = None,
        endpoints: EndpointPolicy | None = None,
        sandbox: SandboxBackend | None = None,
        slots: ExecutionSlots | None = None,
        artifacts: ArtifactStore | None = None,
        policy: ToolBatchPolicy | None = None,
    ):
        self.repository, self.endpoints, self.sandbox = repository, endpoints, sandbox
        self.slots = slots if slots is not None else ExecutionSlots()
        self.artifacts = artifacts if artifacts is not None else ArtifactStore()
        self.policy = policy or ToolBatchPolicy()

    async def discover(
        self,
        trusted: TrustedContext,
        control: ToolControl,
        *,
        defer_catalog_limit: bool = False,
        selected_connection_ids: frozenset | None = None,
    ) -> McpRun | None:
        control.require_running()
        if self.repository is None:
            return None
        assert self.endpoints is not None
        repository = self.repository() if callable(self.repository) else self.repository
        run = McpRun(
            repository,
            self.endpoints,
            owner_key(trusted),
            defer_catalog_limit=defer_catalog_limit,
            selected_connection_ids=selected_connection_ids,
        )
        try:
            async with asyncio.timeout(control.remaining):
                await run.open()
            control.require_running()
            return run
        except BaseException:
            await run.close()
            raise

    def assemble(self, builtins: Iterable[RegisteredTool], mcp: McpRun | None) -> RunTools:
        registry = ToolRegistry([*builtins, *(mcp.entries if mcp else ())])
        return RunTools(
            registry,
            ToolDispatcher(registry, self.slots, self.artifacts, sandbox=self.sandbox, mcp=mcp),
            self.policy,
        )

    async def reconcile(self):
        if self.sandbox:
            resolved = await self.sandbox.reconcile()
            pending = self.slots.pending("sandbox")
            if pending:
                # Another Agent replica may have confirmed cleanup in the shared lease table.
                resolved.extend(await self.sandbox.confirmed_executions(pending))
            for execution_id in set(resolved):
                await self.slots.confirm_cleanup(execution_id, environment="sandbox")
