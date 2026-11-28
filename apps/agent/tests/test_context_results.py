import json
from uuid import uuid4

import pytest
from compass_agent.tools.contracts import ToolRejected, json_bytes
from compass_agent.tools.result_store import INLINE_TOKENS, ToolResultStore


def save(store, value, **overrides):
    return store.save(
        value,
        **{
            "session_id": uuid4(),
            "run_id": uuid4(),
            "step_id": uuid4(),
            "call_id": "call-1",
            **overrides,
        },
    )


def test_large_query_read_does_not_repeat_query_or_lose_rows(tmp_path):
    store = ToolResultStore(tmp_path)
    manifest = save(
        store, {"columns": ["id", "channel"], "rows": [[i, "渠道甲"] for i in range(10000)]}
    )
    receipt = store.receipt(manifest)
    assert receipt["returnedRows"] == 10000
    assert len(json_bytes(receipt)) <= 512
    assert len(receipt["preview"]) <= 5
    assert receipt["originalComplete"] and not receipt["truncated"]
    assert receipt["previewTruncated"]
    from compass_agent.semantic.evidence import result

    evidence = list(result({}, receipt, []))
    assert evidence[0]["kind"] == "query"
    assert evidence[0]["record"]["returnedRows"] == 10000
    assert "rows" not in evidence[0]["record"]
    page = store.read(manifest, offset=8000, limit=1)
    assert page["data"] == [[8000, "渠道甲"]]
    assert page["range"] == {"start": 8000, "end": 8001}
    assert page["nextCursor"]
    assert page["sourceComplete"]
    assert not page["complete"]
    assert store.read(manifest, offset=9999, limit=1)["complete"]
    empty = store.read(manifest, offset=10000, limit=1)
    assert empty["data"] == [] and empty["returnedRows"] == 10000
    from compass_agent.tools.builtin.read_tool_result import evidence as page_evidence

    record = list(page_evidence({}, empty, []))[0]
    assert record["record"]["range"]["start"] == 10000
    assert record["record"]["sourceComplete"]


def test_oversized_record_pages_advance_without_losing_characters(tmp_path):
    store = ToolResultStore(tmp_path)
    row = ["首尾🙂" * 4000]
    manifest = save(store, {"columns": ["text"], "rows": [row]})
    page = store.read(manifest, offset=0)
    assert len(json_bytes(page)) > 2048
    parts = []
    while True:
        assert len(json_bytes(page)) <= INLINE_TOKENS
        assert page["data"]["characterEnd"] > page["data"]["characterStart"]
        parts.append(page["data"]["fragment"])
        if page["complete"]:
            break
        page = store.read(manifest, cursor=page["nextCursor"])
    assert json.loads("".join(parts)) == row


def test_text_page_preserves_unicode_and_original_completeness(tmp_path):
    store = ToolResultStore(tmp_path)
    text = "退款🙂\n" * 1400
    manifest = save(store, text)
    collected = []
    page = store.read(manifest, limit=50)
    while True:
        collected.append(page["data"])
        assert len(json_bytes(page)) <= INLINE_TOKENS
        if page["complete"]:
            break
        page = store.read(manifest, cursor=page["nextCursor"])
    assert "".join(collected) == text
    partial = save(store, {"columns": ["id"], "rows": [[1]], "truncated": True})
    assert not store.receipt(partial)["complete"]
    assert store.receipt(partial)["truncated"]
    assert not store.read(partial)["sourceComplete"]
    assert store.read(partial)["complete"]


def test_result_cursor_cannot_be_reused_for_another_file(tmp_path):
    store = ToolResultStore(tmp_path)
    first = save(store, "x" * (INLINE_TOKENS * 2))
    second = save(store, "y" * (INLINE_TOKENS * 2))
    cursor = store.read(first)["nextCursor"]
    with pytest.raises(ToolRejected, match="result_cursor_invalid"):
        store.read(second, cursor=cursor)
    with pytest.raises(ToolRejected, match="result_reference_invalid"):
        store.path(uuid4(), "../../private")


def test_file_corruption_and_missing_original_are_explicit(tmp_path):
    store = ToolResultStore(tmp_path)
    manifest = save(store, {"result": "ok"})
    from uuid import UUID

    path = store.path(UUID(manifest["sessionId"]), manifest["ref"])
    path.write_text("changed")
    with pytest.raises(ToolRejected, match="result_file_changed"):
        store.read(manifest)
    path.unlink()
    with pytest.raises(ToolRejected, match="result_file_unavailable"):
        store.read(manifest)


def test_interrupted_writer_does_not_publish_partial_file(tmp_path):
    store = ToolResultStore(tmp_path)
    session = uuid4()
    with pytest.raises(RuntimeError), store.writer(session, uuid4(), uuid4(), "call") as writer:
        writer.row(["first row"])
        raise RuntimeError("interrupted")
    assert list(store.directory(session).iterdir()) == []


def test_wide_columns_and_metadata_are_readable_in_bounded_fragments(tmp_path):
    store = ToolResultStore(tmp_path)
    columns = ["原始列名🙂" * 100, "金额"]
    source = {"columns": columns, "rows": [["数据", "314.80"]], "description": "原始说明" * 200}
    manifest = save(store, source)
    assert len(json_bytes(store.receipt(manifest, metadata={"code": "x" * 300}))) <= 512
    page = store.read(manifest)
    metadata = []
    while isinstance(page["data"], dict) and "metadataFragment" in page["data"]:
        assert len(json_bytes(page)) <= INLINE_TOKENS
        metadata.append(page["data"]["metadataFragment"])
        page = store.read(manifest, cursor=page["nextCursor"])
    assert json.loads("".join(metadata)) == {k: v for k, v in source.items() if k != "rows"}
    assert page["data"] == source["rows"] and page["complete"]
