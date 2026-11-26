"""Source constraints accumulate with AND semantics when material is derived."""

from collections import Counter
from dataclasses import dataclass, replace
from typing import Literal

from compass_agent.context_runtime.contracts import AccessRef, ContextItem

type TrustLevel = Literal["managed", "dictionary", "actor", "observation"]
TRUST_RANK = {"observation": 0, "actor": 1, "dictionary": 2, "managed": 3}
RULES = frozenset(
    {
        "tenant_workspace_mismatch",
        "role_forgery",
        "system_prompt_extraction",
        "instruction_override",
        "suspicious_instruction",
        "secret",
        "phone",
        "email",
        "identity_number",
        "source_revoked",
        "snapshot_read",
        "mcp_read",
        "source_lineage",
    }
)


@dataclass(frozen=True, slots=True)
class SecurityEventRecord:
    event_type: str
    action: str
    rule_code: str
    source_kind: str | None = None
    trust_level: str | None = None
    hit_count: int = 1


@dataclass(frozen=True, slots=True)
class DerivedLineage:
    min_trust_level: TrustLevel
    access_scope: tuple[AccessRef, ...]
    data_classification: str


def derive_lineage(items: list[ContextItem] | tuple[ContextItem, ...]) -> DerivedLineage:
    trust: TrustLevel = min(items, key=lambda i: TRUST_RANK[i.trust]).trust if items else "managed"
    refs = tuple(
        sorted(
            {ref for item in items for ref in item.access},
            key=lambda r: (r.tenant_id, r.workspace_id, r.binding, r.revision),
        )
    )
    return DerivedLineage(trust, refs, "restricted" if refs else "internal")


def aggregate_events(events: list[SecurityEventRecord]) -> list[SecurityEventRecord]:
    counts: Counter = Counter()
    for event in events:
        counts[replace(event, hit_count=1)] += event.hit_count
    return [replace(event, hit_count=count) for event, count in counts.items()]
