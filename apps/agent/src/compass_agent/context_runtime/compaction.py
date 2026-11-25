"""Summarize closed ranges, then restore a contiguous recent suffix within budget."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass

from compass_agent.context_runtime.optimization_calls import auxiliary_call
from compass_agent.context_runtime.pipeline import FRAME_BYTE_LIMIT, canonical, input_budget
from compass_agent.models.errors import ModelError
from compass_agent.runtime.private_state import digest
from compass_agent.schemas import ChatMessage, GenerateInput


@dataclass(frozen=True)
class SummarySnapshot:
    text: str
    selected_ids: tuple[str, ...]
    covered_ranges: tuple[str, ...]
    query_hash: str
    prompt_version: str
    source_deps: tuple[dict, ...]
    attempt_refs: tuple[str, ...]
    pruned_bytes: int = 0

    def dump(self):
        from dataclasses import asdict

        return asdict(self)


def recent_turns(groups, selected_ids, budget):
    retained, used = [], 0
    for group in reversed(groups):
        if group.id not in selected_ids:
            break
        size = sum(
            len(canonical(m.model_dump(mode="json", exclude_none=True))) + 8 for m in group.messages
        )
        if used + size > budget:
            break
        retained.insert(0, group)
        used += size
    return retained


def summary_request(prompt, question, previous, material, output):
    target = min(768, max(1, output // 2))
    return GenerateInput(
        messages=[
            ChatMessage(
                role="system",
                content=prompt
                + f"\n本次交接正文以 {target} tokens 为目标，必须在 {output} tokens 内完整结束。"
                "优先写下一步必需的约定、已执行动作、关键结论和回读定位；"
                "大量明细保留回读位置，不逐项复制。合并上一份摘要后仍遵守同一长度目标。",
            ),
            ChatMessage(
                role="user",
                content=canonical(
                    {
                        "currentQuestion": question,
                        "previousSummary": previous,
                        "selectedHistory": material,
                        "outputTokenBudget": output,
                    }
                ).decode(),
            ),
        ],
        tools=[],
        stream=False,
        thinking=False,
        max_tokens=output,
    )


def request_size(request):
    return len(
        canonical(
            request.model_dump(mode="json", exclude_none=True, exclude={"continuation_token"})
        )
    ) + 8 * len(request.messages)


def fragments(group, recoverable=(), seen=None):
    seen = seen if seen is not None else set()
    for index, message in enumerate(group.messages):
        identifier = (
            group.ranges[index] + ":" + digest(message.model_dump(mode="json", exclude_none=True))
        )
        material = message.model_dump(mode="json", exclude_none=True)
        if message.role == "tool" and message.content:
            try:
                payload = json.loads(message.content)

                def prune(node):
                    if not isinstance(node, dict):
                        return
                    ref = node.get("ref")
                    if ref in recoverable and isinstance(node.get("preview"), list):
                        preview = node["preview"]
                        keep = [] if ref in seen else preview[:1]
                        if len(keep) < len(preview):
                            node["preview"] = keep
                            node["previewOmitted"] = len(preview) - len(keep)
                        seen.add(ref)
                    for child in node.values():
                        prune(child)

                prune(payload)
                content = canonical(payload).decode()
                if len(content.encode()) < len(message.content.encode()):
                    material = {**material, "content": content}
            except (ValueError, TypeError):
                pass
        yield {
            "groupId": group.id,
            "range": identifier,
            "message": material,
        }


def split_material(item: dict, fits: Callable[[list[dict]], bool]) -> list[dict]:
    """Only the summarizer receives text fragments; main tool exchanges stay atomic."""
    if fits([item]):
        return [item]
    text = canonical(item["message"]).decode() if "message" in item else item["fragment"]
    base = item.get("characters", [0])[0]
    role = item["message"]["role"] if "message" in item else item["role"]
    output, start = [], 0
    while start < len(text):
        low, high, best = start + 1, len(text), None
        while low <= high:
            end = (low + high) // 2
            part = {
                "groupId": item["groupId"],
                "range": item["range"],
                "characters": [base + start, base + end],
                "role": role,
                "fragment": text[start:end],
            }
            if fits([part]):
                best, low = part, end + 1
            else:
                high = end - 1
        if best is None:
            raise ModelError("context_summary_input_too_large", 422)
        output.append(best)
        start = best["characters"][1] - base
    return output


async def recoverable_refs(journal, trusted, groups):
    results = getattr(journal, "context_results", None)
    if results is None:
        return set()
    candidates = set()

    def visit(node):
        if not isinstance(node, dict):
            return
        ref = node.get("ref")
        if isinstance(ref, str) and ref.startswith("results/") and node.get("preview"):
            candidates.add(ref)
        for child in node.values():
            visit(child)

    for group in groups:
        for message in group.messages:
            if message.role == "tool":
                try:
                    visit(json.loads(message.content or ""))
                except (ValueError, TypeError):
                    continue
    from uuid import UUID

    from compass_agent.tools.contracts import ToolRejected

    accepted = set()
    for ref in candidates:
        try:
            manifest = await asyncio.to_thread(results.resolve, ref, trusted)
            if results.files.path(UUID(manifest["sessionId"]), ref).is_file():
                accepted.add(ref)
        except (ToolRejected, ModelError):
            continue
    return accepted


async def summarize(
    journal, models, trusted, control, budget, route, limits, groups, question, *, previous=None
):
    await asyncio.to_thread(journal.prefetch_prompts, ["support/context/compaction.md"])
    prompt = journal.prompt("support/context/compaction.md")
    version = digest(
        {
            "generation": journal.context_binding["generation"],
            "prompt": prompt,
            "provider": str(route.provider_id),
            "binding": route.binding_version,
        }
    )
    query_hash = digest(question)
    selected = tuple(g.id for g in groups)
    ranges = tuple(item["range"] for g in groups for item in fragments(g))
    profiles = (
        [
            p.output_tokens
            for p in budget.profiles
            if p.provider_id == route.provider_id and p.binding_version == route.binding_version
        ]
        if budget
        else []
    )
    output = min(
        2048, max(1, input_budget(limits, 2048) // 10), limits.max_output_tokens or 2048, *profiles
    )
    limit = min(FRAME_BYTE_LIMIT, input_budget(limits, output))
    reuse = (
        previous
        and previous.query_hash == query_hash
        and previous.prompt_version == version
        and set(previous.selected_ids).issubset(selected)
        and set(previous.covered_ranges).issubset(ranges)
    )
    covered = set(previous.covered_ranges) if reuse and previous else set()
    text = previous.text if reuse and previous else ""
    attempts = list(previous.attempt_refs) if reuse and previous else []
    originals = {item["range"]: item for group in groups for item in fragments(group)}
    recoverable = await recoverable_refs(journal, trusted, groups)
    seen = set()
    pending = [
        item
        for group in groups
        for item in fragments(group, recoverable, seen)
        if item["range"] not in covered
    ]
    pruned_bytes = sum(
        len(canonical(originals[item["range"]])) - len(canonical(item)) for item in pending
    )
    if not pending and reuse and previous:
        return previous
    # A model's large input window does not make a 2K-token handoff capable of
    # summarizing an entire long tool history in one response. Bound each merge
    # by its output budget too, preserving every original range across batches.
    batch_limit: int = min(
        limit,
        max(output * 12, request_size(summary_request(prompt, question, text, [], output)) + 1024),
    )
    default_batch_limit = batch_limit
    truncated_retries = 0

    def fits(items: list[dict]) -> bool:
        return request_size(summary_request(prompt, question, text, items, output)) <= batch_limit

    while pending:
        if not fits([]):
            raise ModelError("context_summary_input_too_large", 422)
        # The current handoff occupies input space too. Split against that exact request.
        first = pending.pop(0)
        parts = split_material(first, fits)
        material = [parts.pop(0)]
        pending = parts + pending
        while pending and fits([*material, pending[0]]):
            material.append(pending.pop(0))
        request = summary_request(prompt, question, text, material, output)
        if not fits(material):
            raise ModelError("context_summary_input_too_large", 422)
        fingerprint = digest([version, query_hash, selected, request.model_dump(mode="json")])
        key = f"root/context-compact:{fingerprint}"
        result = await auxiliary_call(
            journal,
            models,
            trusted,
            control,
            budget,
            kind="ctx_compact",
            key=key,
            provider_id=route.provider_id,
            binding_version=route.binding_version,
            request=request,
            source_refs=tuple(ref for group in groups for ref in group.source_deps),
        )
        if (
            result.status != "completed"
            or result.finish_reason == "length"
            or not result.text.strip()
        ):
            if result.finish_reason == "length" or result.error_code == "output_truncated":
                floor = request_size(summary_request(prompt, question, text, [], output)) + 512
                smaller: int = max(floor, batch_limit // 2)
                if truncated_retries < 2 and smaller < batch_limit:
                    # Never reuse a truncated summary or mark its ranges covered.
                    # Retry smaller original material through normal journal/budget
                    # accounting; a new request fingerprint gives it a distinct call.
                    pending = material + pending
                    batch_limit = smaller
                    truncated_retries += 1
                    continue
            raise ModelError(result.error_code or "context_summary_unavailable", 503)
        text = result.text
        # A smaller retry applies to that failed merge only. Carrying it forward
        # lets the rolling summary crowd out all subsequent original material.
        batch_limit = default_batch_limit
        truncated_retries = 0
        attempts.append(str(journal.last_attempt))
    refs = []
    for group in groups:
        for ref in group.source_deps:
            if ref not in refs:
                refs.append(ref)
    return SummarySnapshot(
        text, selected, ranges, query_hash, version, tuple(refs), tuple(attempts), pruned_bytes
    )
