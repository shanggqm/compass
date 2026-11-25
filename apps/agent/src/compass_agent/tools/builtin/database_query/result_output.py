"""Stream a single authorized SELECT into an original and a model observation."""

import json
from datetime import UTC, datetime
from uuid import uuid4

from compass_agent.tools.result_store import INLINE_TOKENS


async def full_result(cursor, query, binding, control, journal, projection, call_id):
    from compass_agent.data_connections.retail_access_refs import MAX_KEYS, reference
    from compass_agent.tools.builtin.database_query import DatabaseResult, JsonPairs, _json_value

    results = journal.context_results
    result_id = str(uuid4())
    inline_rows, columns = [], ()
    inline = True
    with results.files.writer(
        results.session_id, journal.run_id, journal.parent.get() or journal.run_id, call_id
    ) as writer:
        # Named cursors fetch bounded chunks in the same repeatable-read snapshot.
        async with cursor.connection.cursor(name="result_" + uuid4().hex) as stream:
            await stream.execute(
                f"SELECT pg_catalog.row_to_json(result)::text FROM ({query}) AS result"
            )
            while rows := await stream.fetchmany(256):
                control.require_running()
                for (raw,) in rows:
                    decoded = json.loads(raw, object_pairs_hook=JsonPairs, parse_float=str)
                    if not columns:
                        columns = tuple(name for name, _ in decoded)
                        writer.columns = list(columns)
                    row = tuple(_json_value(value) for _, value in decoded)
                    writer.row(row)
                    if inline:
                        candidate = DatabaseResult(result_id, columns, (*inline_rows, row), False)
                        inline = (
                            results.files.count_tokens(candidate.observation()) <= INLINE_TOKENS
                        )
                        if inline:
                            inline_rows.append(row)
        ref = {"kind": "transient"}
        if projection:
            await cursor.execute(
                f"SELECT row_to_json(keys) FROM ({projection[0]} LIMIT {MAX_KEYS + 1}) keys"
            )
            keys = [row[0] for row in await cursor.fetchall()]
            ref = reference(binding, projection, keys)
        if inline:
            return DatabaseResult(result_id, columns, tuple(inline_rows), False, source_ref=ref)
        source = ref
        if source["kind"] == "transient":
            source = {
                **source,
                "epoch": str(journal.epoch),
                "capturedAt": datetime.now(UTC).isoformat(),
            }
        manifest = writer.finish(complete=True, source_deps=[source])
        return DatabaseResult(
            result_id,
            columns,
            tuple(tuple(r) for r in writer.samples),
            True,
            source_ref=ref,
            context_result=manifest,
        )


async def result_fingerprint(cursor, query, control):
    """Recheck every row using the original JSONL encoding, without creating an artifact."""
    import hashlib

    from compass_agent.tools.builtin.database_query import DatabaseResult, JsonPairs, _json_value
    from compass_agent.tools.contracts import json_bytes

    checksum = hashlib.sha256()
    columns = ()
    count = 0
    async with cursor.connection.cursor(name="verify_" + uuid4().hex) as stream:
        await stream.execute(
            f"SELECT pg_catalog.row_to_json(result)::text FROM ({query}) AS result"
        )
        while rows := await stream.fetchmany(256):
            control.require_running()
            for (raw,) in rows:
                decoded = json.loads(raw, object_pairs_hook=JsonPairs, parse_float=str)
                if not count:
                    columns = tuple(name for name, _ in decoded)
                    checksum.update(json_bytes({"columns": list(columns)}) + b"\n")
                row = tuple(_json_value(value) for _, value in decoded)
                checksum.update(json_bytes(row) + b"\n")
                count += 1
    if not count:
        checksum.update(json_bytes({"columns": []}) + b"\n")
    return DatabaseResult(
        str(uuid4()),
        columns,
        (),
        False,
        context_result={"sha256": checksum.hexdigest(), "returnedCount": count, "complete": True},
    )
