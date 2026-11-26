"""Content-free native Langfuse observations beneath the actual logical call."""


def emit_context_spans(parent, receipt, snapshot_ref):
    if parent is None or not receipt.get("contextOps"):
        return
    payload = receipt["contextOps"]
    events = payload.get("security_events", [])
    candidates = payload["frozen"]["candidates"]
    selected = payload["frozen"]["selected_ids"]
    blocked = sum(e["hit_count"] for e in events if e["action"] in {"block", "deny"})
    redacted = sum(e["hit_count"] for e in events if e["action"] == "redact")
    with parent.child(
        "compass.context.retrieve", candidate_count=len(candidates), snapshot_ref=snapshot_ref
    ) as span:
        span.native("type", "retriever")
    with parent.child(
        "compass.context.guardrail",
        security_blocked_count=blocked,
        redacted_count=redacted,
        snapshot_ref=snapshot_ref,
    ) as span:
        span.native("type", "guardrail")
        for event in events[:16]:
            span.event(
                "context.guardrail",
                guardrail_action=event["action"],
                guardrail_rule=event["rule_code"],
                trust_level=event.get("trust_level") or "observation",
            )
    budget = receipt["budget"]
    with parent.child(
        "compass.context.compile",
        snapshot_ref=snapshot_ref,
        policy_digest=receipt.get("policyDigest"),
        candidate_count=len(candidates),
        selected_count=len(selected),
        tokens_before=budget["before"],
        tokens_after=budget["after"],
        token_limit=budget["limit"],
        compaction_ratio=budget["after"] / max(1, budget["before"]),
    ) as span:
        span.native("type", "chain")
        for stage, count in (("candidate", len(candidates)), ("window", len(selected))):
            span.event("context.funnel", funnel_stage=stage, candidate_count=count)
