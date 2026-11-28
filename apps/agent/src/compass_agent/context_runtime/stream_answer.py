"""Ordered source context and persisted anchors for answer protocol 3."""

import json
import re
from types import SimpleNamespace
from uuid import uuid5

from compass_agent.context_runtime.answer_artifacts import build_artifact

MAX_ANSWER_SOURCES = 128
MAX_EVIDENCE_PROMPT_BYTES = 180_000

MARKER = re.compile(r"\^\[(\d{1,3})\]\^|(?<![\w\[])\[(\d{1,3})\](?!\()")


def ordered_evidence(evidence):
    return [
        evidence[key]
        for key in sorted(evidence)
        if evidence[key].get("kind")
        in {"query", "document", "definition", "artifact", "user_input"}
    ][-MAX_ANSWER_SOURCES:]


def evidence_prompt(ordered):
    from compass_agent.context_security.guard import wrap_untrusted
    from compass_agent.context_security.structured import protect

    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    # Preserve ordinal identity and full records in storage; the prompt gets bounded previews.
    budget = min(24_000, MAX_EVIDENCE_PROMPT_BYTES // max(1, len(ordered)) - 1024)
    material = []
    for index, item in enumerate(ordered, 1):
        record = item["record"]
        encoded = encode(record).encode()
        if len(encoded) > budget:
            record = {
                "preview": encoded[: max(0, budget // 2)].decode("utf-8", errors="ignore"),
                "previewTruncated": True,
            }
        material.append(
            {
                "number": index,
                "title": item["title"][:80],
                "locator": item["locator"][:80],
                "record": record,
            }
        )
    material, _events = protect(material)
    return wrap_untrusted(encode(material), "answer_evidence")


def citations_for(run_id, body, ordered):
    anchors, details = [], {}
    for match in MARKER.finditer(body):
        ordinal = int(match[1] or match[2])
        if not 1 <= ordinal <= len(ordered):
            continue
        item = ordered[ordinal - 1]
        cid = str(uuid5(run_id, "answer-v3-citation:" + item["id"]))
        anchors.append(
            {"citationId": cid, "ordinal": ordinal, "start": match.start(), "end": match.end()}
        )
        record = item["record"]
        excerpt = (
            record.get("text")
            or record.get("summary")
            or json.dumps(record, ensure_ascii=False, sort_keys=True)
        )
        details[cid] = {
            "citationId": cid,
            "claimId": cid,
            "revision": 1,
            "status": "supported",
            "statement": item["title"],
            "calculation": None,
            "sources": [
                {
                    "kind": {
                        "query": "detail",
                        "artifact": "detail",
                        "user_input": "knowledge",
                        "document": record.get("kind", "knowledge"),
                    }.get(item["kind"], item["kind"]),
                    "title": item["title"],
                    "version": "" if item["kind"] == "user_input" else item["version"],
                    "locator": item["locator"],
                    "scope": json.dumps(
                        {
                            k: record[k]
                            for k in (
                                "scope",
                                "unit",
                                "time_range",
                                "data_scope",
                                "truncated",
                                "as_of",
                            )
                            if k in record
                        },
                        ensure_ascii=False,
                    ),
                    "excerpt": excerpt[:16000],
                }
            ],
        }
    if len(anchors) > 96:
        raise ValueError("引用数量超过 96")
    return {"anchors": anchors, "details": details}


def stream_artifact(format, evidence, body, node_key, as_of):
    if format is None:
        return None
    if any(
        ref.get("kind") == "transient" for item in evidence.values() for ref in item["sourceRefs"]
    ):
        raise ValueError("当前来源仅支持即时读取，无法保存附件。")
    identifiers = [key for key, item in evidence.items() if item["kind"] == "query"]
    spec = SimpleNamespace(
        format=format,
        title="分析结果",
        evidence_ids=identifiers[-24:] if format != "markdown" else [],
    )
    return build_artifact(SimpleNamespace(artifact=spec), evidence, body, node_key, as_of)
