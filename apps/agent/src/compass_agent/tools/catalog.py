"""Shipped generic tools; per-Run providers supply authorized bindings."""

from compass_agent.tools.builtin.create_workflow import SPEC as CREATE_WORKFLOW
from compass_agent.tools.builtin.database_query import SPEC as DATABASE_QUERY
from compass_agent.tools.builtin.final_answer import SPEC as FINAL_ANSWER
from compass_agent.tools.builtin.goal import SPEC as GOAL
from compass_agent.tools.builtin.python_analysis import SPEC as PYTHON_ANALYSIS
from compass_agent.tools.builtin.read_tool_result import SPEC as READ_TOOL_RESULT
from compass_agent.tools.builtin.schedule_read import SPEC as SCHEDULE_READ
from compass_agent.tools.builtin.schedule_write import SPEC as SCHEDULE_WRITE
from compass_agent.tools.builtin.sub_agent import SPEC as SUB_AGENT
from compass_agent.tools.builtin.todo_write import SPEC as TODO_WRITE
from compass_agent.tools.business.discover_semantic_candidates import (
    SPEC as DISCOVER_SEMANTIC_CANDIDATES,
)
from compass_agent.tools.business.get_metric_definition import SPEC as GET_METRIC_DEFINITION
from compass_agent.tools.business.query_metric import SPEC as QUERY_METRIC
from compass_agent.tools.business.read_document import SPEC as READ_DOCUMENT
from compass_agent.tools.business.resolve_semantics import SPEC as RESOLVE_SEMANTICS
from compass_agent.tools.business.review_semantic_change import SPEC as REVIEW_SEMANTIC_CHANGE
from compass_agent.tools.business.search_documents import SPEC as SEARCH_DOCUMENTS
from compass_agent.tools.business.search_semantics import SPEC as SEARCH_SEMANTICS
from compass_agent.tools.business.submit_semantic_change import SPEC as SUBMIT_SEMANTIC_CHANGE
from compass_agent.tools.catalog_contracts import CatalogContext, ToolSpec

TOOLS: tuple[ToolSpec, ...] = (
    FINAL_ANSWER,
    GOAL,
    TODO_WRITE,
    DATABASE_QUERY,
    READ_TOOL_RESULT,
    PYTHON_ANALYSIS,
    SUB_AGENT,
    CREATE_WORKFLOW,
    SCHEDULE_READ,
    SCHEDULE_WRITE,
    SEARCH_SEMANTICS,
    SEARCH_DOCUMENTS,
    READ_DOCUMENT,
    GET_METRIC_DEFINITION,
    RESOLVE_SEMANTICS,
    QUERY_METRIC,
    DISCOVER_SEMANTIC_CANDIDATES,
    REVIEW_SEMANTIC_CHANGE,
    SUBMIT_SEMANTIC_CHANGE,
)


def selected(context: CatalogContext) -> tuple[ToolSpec, ...]:
    return tuple(
        spec for spec in TOOLS if spec.capability in context.enabled and spec.available(context)
    )


def definitions(context: CatalogContext):
    from compass_agent.promptops.structured import enrich_definition

    return [
        enrich_definition(value)
        for spec in selected(context)
        if (value := spec.definition(context)) is not None
    ]


def registrations(context: CatalogContext, handler):
    return [spec.register(context, handler) for spec in selected(context)]
