"""Finite metadata construction and dependency-log redaction."""

import json
import logging
import re
from math import isfinite

FIELDS = frozenset(
    [
        "call_id",
        "parent_call_id",
        "attempt_id",
        "tenant_id",
        "workspace_id",
        "actor_id",
        "purpose",
        "stage",
        "provider_id",
        "model",
        "adapter",
        "config_version",
        "binding_version",
        "dictionary_version",
        "routing_version",
        "category_id",
        "scene_id",
        "difficulty",
        "mode",
        "reason",
        "status",
        "error_code",
        "sent",
        "outcome_unknown",
        "cleanup_confirmed",
        "first_visible_ms",
        "queue_wait_ms",
        "usage_complete",
        "unknown_usage_attempts",
        "cost_state",
        "cost_source",
        "reasoning_tokens",
        "instance_id",
        "simulation",
        "content_input_truncated",
        "content_output_truncated",
        "content_output_partial",
        "generation",
        "policy_version",
        "task",
        "tokens_before",
        "tokens_after",
        "token_limit",
        "prompt_path",
        "prompt_version",
        "tool",
        "skill",
    ]
)
FIELDS = FIELDS | {
    "snapshot_no",
    "changed_count",
    "budget_method",
    "restore_mode",
    "run_id",
    "step_id",
    "item_position",
    "selection",
    "scope_profile",
    "metric_count",
    "omitted_count",
    "classifier",
    "request_digest",
    "tool_status",
    "tool_operation",
    "tool_evidence",
    "snapshot_ref",
    "policy_digest",
    "trust_level",
    "guardrail_action",
    "guardrail_rule",
    "candidate_count",
    "selected_count",
    "security_blocked_count",
    "redacted_count",
    "compaction_ratio",
    "funnel_stage",
}
# Only the Context Runtime emits structured span events. Give each event its own
# schema so SDK exception messages and arbitrary payloads never reach OTLP.
EVENT_FIELDS = {
    "prompt.used": {"prompt_path", "prompt_version"},
    "skill.selected": {"skill"},
    "tool.selected": {"tool"},
    "context.material": {"item_position", "selection", "reason"},
    "capability.excluded": {"reason"},
    "context.compacted": {"reason", "tokens_before", "tokens_after"},
    "context.rejected": {"reason"},
    "context.guardrail": {"guardrail_action", "guardrail_rule", "trust_level"},
    "context.funnel": {"funnel_stage", "candidate_count"},
}

META = "langfuse.observation.metadata."
TOOL_LABELS = {
    "todo_write",
    "database_query",
    "read_document",
    "search_documents",
    "python_analysis",
    "schedule_read",
    "schedule_write",
    "sub_agent",
    "create_workflow",
    "external",
}
DATABASE_OPERATIONS = {"list_connections", "list_tables", "describe_table", "query"}
SKILL_LABELS = {"metric-check", "report-outline", "agentic-search"}
EXCLUSION_REASONS = {"permission", "stage", "scope", "dependency"}
CONTEXT_LISTS = {"prompts_used", "skills_selected", "tools_selected", "capabilities_excluded"}


def tool_label(name):
    return name if name in TOOL_LABELS else "external"


def tool_operation(name, args):
    if name == "database_query" and isinstance(args, dict):
        op = args.get("op")
        if isinstance(op, str) and op in DATABASE_OPERATIONS:
            return op
    return None


def context_list_valid(key, values):
    """Only public, finite identifiers may be exported, even through the native SDK API."""
    if not isinstance(values, (tuple, list)) or any(type(v) is not str for v in values):
        return False
    if key == "prompts_used":
        from compass_agent.promptops.catalog import bundled
        from compass_agent.promptops.layout import MEMBERS

        catalog = set(bundled()) | set(MEMBERS)
        if len(values) > len(catalog):
            return False
        for value in values:
            path, _, version = value.rpartition("@v")
            if path not in catalog or not version.isascii() or not version.isdecimal():
                return False
            if len(version) > 16 or not 0 < int(version) <= 2**53 - 1:
                return False
        return True
    if key == "capabilities_excluded":
        labels = {
            f"{name}: {reason}"
            for name in TOOL_LABELS | SKILL_LABELS
            for reason in EXCLUSION_REASONS
        }
    else:
        labels = SKILL_LABELS if key == "skills_selected" else TOOL_LABELS
    return len(values) <= len(labels) and all(v in labels for v in values)


def safe_attribute(span_name, key, value):
    name = key.removeprefix(META)
    if name == "snapshot_ref":
        return isinstance(value, str) and bool(re.fullmatch(r"csnp_[0-9a-f]{32}", value))
    if name == "policy_digest":
        return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))
    if name == "guardrail_rule":
        from compass_agent.context_security.policy import RULES

        return value in RULES
    if name == "trust_level":
        return value in {"managed", "dictionary", "actor", "observation"}
    if name == "guardrail_action":
        return value in {"block", "strip", "redact", "isolate", "deny", "allow_audit"}
    if name == "funnel_stage":
        return value in {"candidate", "filtered", "ranked", "compacted", "window"}
    if name in CONTEXT_LISTS:
        return span_name == "context.assemble" and context_list_valid(name, value)
    if name == "tool":
        return type(value) is str and value in TOOL_LABELS
    if name == "tool_operation":
        return type(value) is str and value in DATABASE_OPERATIONS
    if name == "tool_evidence":
        return value == "execution_receipt"
    if name == "tool_status":
        from typing import get_args

        from compass_agent.tools.contracts import ToolStatus

        return value in get_args(ToolStatus)
    return scalar(value) is not None


def safe_context_event(event):
    if event.name not in EVENT_FIELDS:
        return False
    prefix = META
    attrs = event.attributes or {}
    expected = {prefix + key for key in EVENT_FIELDS[event.name]}
    if set(attrs) != expected or any(scalar(v) is None for v in attrs.values()):
        return False
    if not all(safe_attribute("compass.context", key, value) for key, value in attrs.items()):
        return False
    reason = attrs.get(prefix + "reason")
    if reason is not None and reason not in {
        "budget",
        "required",
        "in_budget",
        "dependency",
        "permission",
        "stage",
        "scope",
        "context_limit",
        "forbidden",
        "context_access_changed",
        "source_access_denied",
        "source_authorization_unavailable",
        "model_budget_unavailable",
        "context_policy_unavailable",
        "context_dictionary_unavailable",
        "model_binding_changed",
        "invalid_context",
        "missing_prompt",
        "corrupt_prompt",
        "incomplete_generation",
        "generation_unavailable",
    }:
        return False
    if event.name == "prompt.used":
        from compass_agent.promptops.catalog import bundled
        from compass_agent.promptops.layout import MEMBERS

        return (
            attrs[prefix + "prompt_path"] in (set(bundled()) | set(MEMBERS))
            and type(attrs[prefix + "prompt_version"]) is int
        )
    if event.name == "context.material":
        return str(attrs[prefix + "item_position"]).isdigit() and attrs[prefix + "selection"] in {
            "selected",
            "omitted",
        }
    if event.name == "skill.selected":
        return attrs[prefix + "skill"] in SKILL_LABELS
    if event.name == "tool.selected":
        return attrs[prefix + "tool"] in TOOL_LABELS
    return True


DEPENDENCY_PREFIXES = ("litellm", "LiteLLM", "openai", "opentelemetry", "httpx", "httpcore")


def scalar(value):
    if type(value) is str:
        return value if len(value) <= 256 else None
    if type(value) is bool or type(value) is int and 0 <= value <= 2**53 - 1:
        return value
    if type(value) is float and isfinite(value) and value >= 0:
        return value
    return None


def fields(values):
    return {
        META + key: clean
        for key, value in values.items()
        if key in FIELDS and (clean := scalar(value)) is not None
    }


class DependencyFilter(logging.Filter):
    def filter(self, record):
        if not record.name.startswith(DEPENDENCY_PREFIXES):
            return True
        record.msg = "telemetry_dependency_event"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def install_dependency_filters():
    prefixes = DEPENDENCY_PREFIXES
    names = set(logging.Logger.manager.loggerDict) | set(prefixes)
    for name in names:
        if name.startswith(prefixes):
            logger = logging.getLogger(name)
            if not any(isinstance(f, DependencyFilter) for f in logger.filters):
                logger.addFilter(DependencyFilter())
    # Handler filters also cover child loggers created after lazy SDK imports.
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, DependencyFilter) for f in handler.filters):
            handler.addFilter(DependencyFilter())


class DiagnosticFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "event": "observability",
                "level": record.levelname,
                "instance_id": getattr(record, "instance_id", ""),
                "reason": getattr(record, "safe_reason", "dependency_failure"),
                **getattr(record, "safe_counts", {}),
            },
            separators=(",", ":"),
        )


def diagnostic(instance_id, reason, counts=None):
    logger = logging.getLogger("compass.observability")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(DiagnosticFormatter())
        logger.addHandler(handler)
        logger.propagate = False
        logger.setLevel(logging.INFO)
    safe_counts = {
        key: value
        for key, value in (counts or {}).items()
        if key in {"queue_size", "dropped", "export_failures", "instrumentation_errors"}
        and type(value) is int
        and value >= 0
    }
    logger.log(
        logging.INFO if reason == "shutdown" else logging.WARNING,
        "observability",
        extra={"instance_id": instance_id, "safe_reason": reason, "safe_counts": safe_counts},
    )
