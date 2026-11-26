"""Protect externally derived fields while keeping structured verification protocols intact."""

from compass_agent.context_security.detector import RuleDetector
from compass_agent.context_security.policy import SecurityEventRecord
from compass_agent.context_security.redactor import redact_structure


def protect(value, *, mode="enforce"):
    value, hits = redact_structure(value)
    value, events = _inspect(value, mode=mode)
    events.extend(
        SecurityEventRecord("pii_redacted", "redact", rule, "review", "observation", count)
        for rule, count in hits.items()
    )
    return value, events


def _inspect(value, *, mode):
    from compass_agent.context_security.guard import ISOLATED

    events = []
    if isinstance(value, dict):
        result = {}
        for name, item in value.items():
            safe, found = _inspect(item, mode=mode)
            result[name] = safe
            events.extend(found)
        return result, events
    if isinstance(value, list):
        result = []
        for item in value:
            safe, found = _inspect(item, mode=mode)
            result.append(safe)
            events.extend(found)
        return result, events
    if not isinstance(value, str):
        return value, events
    hit = RuleDetector().detect(value, "observation")
    if hit:
        action = (
            "allow_audit"
            if mode == "audit_only"
            else "block"
            if hit.severity == "high"
            else "strip"
        )
        events.append(
            SecurityEventRecord(
                "injection_blocked" if hit.severity == "high" else "injection_stripped",
                action,
                hit.rule_code,
                "review",
                "observation",
            )
        )
        if action == "block":
            value = ISOLATED
        elif action == "strip":
            value = hit.stripped_text or ISOLATED
    return value, events
