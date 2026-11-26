"""Deterministic replay of frozen candidates. No model, tool or business-source execution."""

from hashlib import sha256

from compass_agent import schemas as dto
from compass_agent.context_ops.compiler import compile_candidates, restore
from compass_agent.context_runtime.pipeline import FRAME_BYTE_LIMIT, canonical
from compass_agent.harness.context import ContextLimit
from compass_agent.models.errors import ModelError


def compare(payload, override):
    items = restore(payload)
    blocked = payload.get("blocked", {})

    def run(limit, *, protect=False, threshold=1.0, observations=None):
        try:
            selected, _, tokens, _, _ = compile_candidates(
                items,
                min(limit, FRAME_BYTE_LIMIT),
                overhead=payload["overhead"],
                continuation=payload.get("continuation", False),
                protect_dictionary=protect,
                compaction_threshold=threshold,
                max_observation_items=observations,
            )
        except ContextLimit:
            raise ModelError("context_replay_budget_exceeded", 422) from None
        ids = [i.item_id for i in selected]
        dictionary = {i.item_id for i in items if i.trust == "dictionary"}
        count = sum(i.item_id not in blocked for i in selected)
        safe = len(items) - len(blocked)
        return dto.ContextReplayResult(
            selected_ids=ids,
            tokens=tokens,
            dictionary_preserved=dictionary.issubset(ids),
            assembly_sha256=sha256(
                canonical([i.message.model_dump(mode="json", exclude_none=True) for i in selected])
            ).hexdigest(),
            security_blocked_count=len(blocked),
            funnel=[
                dto.ContextFunnelStage.model_validate({"stage": s, "count": max(0, n)})
                for s, n in (
                    ("candidate", len(items)),
                    ("filtered", safe),
                    ("ranked", safe),
                    ("compacted", count),
                    ("window", count),
                )
            ],
        )

    baseline = run(payload["limit"])
    candidate = run(
        override.max_input_tokens or payload["limit"],
        protect=override.protect_dictionary,
        threshold=override.compaction_threshold or 1.0,
        observations=override.max_observation_items,
    )
    return dto.ContextReplayComparison(
        baseline=baseline,
        candidate=candidate,
        baseline_matches_snapshot=baseline.selected_ids == payload["selected_ids"]
        and baseline.assembly_sha256 == sha256(canonical(payload["assembled"])).hexdigest(),
        newly_included_ids=sorted(set(candidate.selected_ids) - set(baseline.selected_ids)),
        newly_omitted_ids=sorted(set(baseline.selected_ids) - set(candidate.selected_ids)),
        token_delta=candidate.tokens - baseline.tokens,
        frozen_model_outputs=bool(payload.get("upstream_optimization")),
    )
