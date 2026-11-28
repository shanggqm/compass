"""Static tool declarations shared by discovery and per-Run registration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from compass_agent.schemas import ToolDefinition
from compass_agent.tools.contracts import (
    InProcessBinding,
    RegisteredTool,
    ToolGovernance,
    ToolPolicy,
)
from compass_agent.tools.registry import bounded_payload, parse_arguments

if TYPE_CHECKING:
    from compass_agent.documents.service import DocumentTools
    from compass_agent.scheduler.service import ScheduleService
    from compass_agent.semantic.service import RegistryTools


@dataclass(frozen=True)
class CatalogContext:
    enabled: frozenset[str]
    database: bool = False
    sandbox: bool = False
    workflow: bool = False
    schedules: ScheduleService | None = None
    schedule_effect: Callable | None = None
    semantics: RegistryTools | None = None
    documents: DocumentTools | None = None
    result_reader: Callable | None = None
    goals: object | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    category: Literal["builtin", "business"]
    capability: str
    definition: Callable[[CatalogContext], ToolDefinition | None]
    available: Callable[[CatalogContext], bool]
    governance: ToolGovernance = field(default_factory=ToolGovernance)
    normalize_input: Callable | None = None
    legacy_definition: Callable | None = None
    parallel_safe: bool = False
    orchestrates: bool = False
    factory: Callable[[], RegisteredTool] | None = None
    context_factory: Callable[[CatalogContext], RegisteredTool] | None = None
    evidence_adapter: Callable | None = None
    fingerprint: Callable | None = None
    public_projection: Literal["normal", "status_only"] = "normal"

    def current_definition(self, context):
        from compass_agent.execution_context import current_execution

        journal = current_execution.get()
        if (
            journal is not None
            and (getattr(journal, "context_binding", None) or {}).get("answerProtocol", 1) < 3
            and self.legacy_definition
        ):
            return self.legacy_definition(context)
        return self.definition(context)

    def register(self, context: CatalogContext, handler: Callable) -> RegisteredTool:
        if self.context_factory is not None:
            entry = self.context_factory(context)
        elif self.factory is not None:
            entry = self.factory()
        else:
            entry = RegisteredTool(
                self.name,
                self.definition(context),
                ToolPolicy(),
                InProcessBinding(handler(self.name), orchestrates=self.orchestrates),
                parse_arguments,
                lambda args, execution: execution.require_running(),
                bounded_payload,
                parallel_safe=self.parallel_safe,
            )
        entry = replace(
            entry,
            definition=self.current_definition(context),
            legacy_definition=self.legacy_definition(context) if self.legacy_definition else None,
            governance=self.governance,
            normalize_input=self.normalize_input or entry.normalize_input,
        )
        if self.governance.subagent_allowed and (
            entry.policy.effect != "read_only"
            or entry.policy.approval == "required"
            or entry.local_effect
            or self.orchestrates
        ):
            raise ValueError(f"{self.name}: subagent tool must be read-only")
        if self.fingerprint is not None:
            entry = replace(entry, fingerprint=self.fingerprint)
        if self.evidence_adapter is not None:
            entry = replace(entry, evidence_adapter=self.evidence_adapter)
        if self.public_projection != "normal":
            entry = replace(entry, public_projection=self.public_projection)
        if entry.definition:
            from compass_agent.promptops.structured import enrich_definition

            entry = replace(entry, definition=enrich_definition(entry.definition))
        return entry
