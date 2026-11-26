"""Enforce before selection; rejected tool results preserve protocol with a safe marker."""

import html
import json
import re
from dataclasses import replace

from compass_agent.context_runtime.contracts import ContextItem
from compass_agent.context_security.detector import InjectionDetector, RuleDetector
from compass_agent.context_security.policy import SecurityEventRecord, aggregate_events
from compass_agent.context_security.redactor import redact, redact_content, redact_structure

ISOLATED = "[该条上下文已由安全策略隔离，内容不可用于回答或执行。]"


def wrap_untrusted(text: str, source: str) -> str:
    opening = (
        f'<untrusted_context source="{html.escape(source, quote=True)}" trust="observation">\n'
    )
    # Recompilation of a sealed prefix must keep identical bytes.
    if text.startswith(opening) and text.endswith("\n</untrusted_context>"):
        text = text[len(opening) : -len("\n</untrusted_context>")]
    escaped = re.sub(r"<\s*/?\s*untrusted_context\b", "&lt;/untrusted_context", text, flags=re.I)
    return opening + escaped + "\n</untrusted_context>"


class ContextSecurityGuard:
    def __init__(self, detector: InjectionDetector | None = None, *, mode="enforce"):
        self.detector = detector or RuleDetector()
        self.mode = mode

    def enforce(self, items: list[ContextItem], context):
        scope = context.scope
        accepted, events, blocked = [], [], {}
        for item in items:
            action = None
            rule = None
            text = item.message.content or ""
            if item.kind in {"review", "summary"} and item.trust == "observation":
                events.append(
                    SecurityEventRecord(
                        "trust_downgraded", "isolate", "source_lineage", item.kind, item.trust
                    )
                )
            if item.kind == "review":
                from compass_agent.context_security.structured import protect

                try:
                    material = json.loads(text)
                except (ValueError, TypeError):
                    material = None
                if isinstance(material, dict) and material.get("purpose") == "verify":
                    for field in ("candidate", "evidence"):
                        material[field], found = protect(material.get(field), mode=self.mode)
                        events.extend(found)
                    text = json.dumps(material, ensure_ascii=False, separators=(",", ":"))
            if any(
                ref.tenant_id != str(getattr(scope, "tenant_id", ""))
                or ref.workspace_id != str(getattr(scope, "workspace_id", ""))
                for ref in item.access
            ):
                action, rule = "deny", "tenant_workspace_mismatch"
                events.append(
                    SecurityEventRecord(
                        "tenant_mismatch_rejected",
                        action,
                        rule,
                        item.kind,
                        item.trust,
                    )
                )
            else:
                hit = self.detector.detect(text, item.trust)
                if hit:
                    action = (
                        ("block" if hit.severity == "high" else "strip")
                        if self.mode == "enforce"
                        else "allow_audit"
                    )
                    rule = hit.rule_code
                    events.append(
                        SecurityEventRecord(
                            "injection_blocked" if hit.severity == "high" else "injection_stripped",
                            action,
                            rule,
                            item.kind,
                            item.trust,
                        )
                    )
                    if action == "strip":
                        text = hit.stripped_text or ISOLATED
            if action in {"deny", "block"}:
                blocked[item.item_id] = "security_" + str(rule)
                # Keep the tool call/result pairing and latest owner turn structurally valid.
                # Only this constant marker survives; the rejected bytes are never compiled.
                text = ISOLATED
            text, hits = redact_content(text, pii=item.trust == "observation")
            data = item.message.model_dump(mode="json", exclude_none=True)
            data["content"] = (
                wrap_untrusted(text, item.kind) if item.trust == "observation" else text
            )
            if data.get("tool_calls"):
                for call in data["tool_calls"]:
                    arguments = call["function"]["arguments"]
                    try:
                        structured = json.loads(arguments)
                    except (ValueError, TypeError):
                        clean, found = redact(arguments, pii=False)
                    else:
                        structured, found = redact_structure(structured, pii=False)
                        clean = (
                            json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
                            if found
                            else arguments
                        )
                    hits.update(found)
                    call["function"]["arguments"] = clean
            for name in ("reasoning_content",):
                if data.get(name):
                    data[name], found = redact(data[name], pii=item.trust == "observation")
                    hits.update(found)
            events.extend(
                SecurityEventRecord(
                    "pii_redacted",
                    "redact",
                    rule,
                    item.kind,
                    item.trust,
                    count,
                )
                for rule, count in hits.items()
            )
            message = item.message.model_validate(data)
            accepted.append(
                replace(
                    item,
                    message=message,
                    byte_bound=len(
                        json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
                    )
                    + 8,
                )
            )
        return accepted, aggregate_events(events), blocked
