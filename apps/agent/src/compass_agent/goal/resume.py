"""Reconcile a changed goal revision against live connection and source access."""

from sqlalchemy import select

from compass_agent.context_runtime.source_access import allows
from compass_agent.data_connections.entities import DataConnection
from compass_agent.goal.ledger import Ledger, unique_refs


def resume_check(store, tx, goal, trusted, epoch):
    if goal.runtime_state.get("checkedRevision") == goal.revision:
        return
    ledger = Ledger(store)
    versions = {
        str(key): revision
        for key, revision in tx.execute(
            select(DataConnection.id, DataConnection.revision).where(
                DataConnection.tenant_id == trusted.scope.tenant_id,
                DataConnection.workspace_id == trusted.scope.workspace_id,
            )
        )
    }
    dropped, stale = set(), set()
    rows = ledger.rows(tx, goal)

    def current_refs(refs):
        return [
            {**ref, "revision": versions[ref["connection"]]}
            if ref.get("connection") in versions and "revision" in ref
            else ref
            for ref in refs
        ]

    shared_refs = current_refs(unique_refs([body for row, body in rows if row.status != "dropped"]))
    all_accessible = allows(store, trusted, shared_refs, epoch=epoch, raise_unavailable=True)
    for row, body in rows:
        if row.status == "dropped":
            dropped.add(row.entry_key)
            continue
        refs = body.get("sourceRefs", [])
        # Validate the same relations/row witnesses using the current connection revision.
        current = current_refs(refs)
        if not all_accessible and not allows(
            store, trusted, current, epoch=epoch, raise_unavailable=True
        ):
            dropped.add(row.entry_key)
            continue
        changed = current != refs
        if changed:
            status = "done" if row.kind == "effect" else "stale"
            if row.kind == "effect":
                # Preserve the original write and data version. Its retained evidence
                # uses the same live authorization witnesses as the outer receipt.
                body = {
                    **body,
                    "evidence": [
                        {**record, "sourceRefs": current_refs(record.get("sourceRefs", []))}
                        for record in body.get("evidence", [])
                    ],
                }
            ledger.upsert(
                tx, goal, row.entry_key, row.kind, status, {**body, "sourceRefs": current}
            )
            if status == "stale":
                stale.add(row.entry_key)
                ledger.upsert(
                    tx,
                    goal,
                    f"refresh:{row.entry_key}",
                    "todo",
                    "open",
                    {
                        "title": "数据版本已变化，请补查：" + body["title"],
                        "required": True,
                        "sourceRefs": current,
                        "dependsOn": [],
                    },
                )
    while True:
        derived = {
            row.entry_key for row, body in rows if dropped.intersection(body.get("dependsOn", []))
        } - dropped
        if not derived:
            break
        dropped |= derived
    for row, body in ledger.rows(tx, goal):
        if row.entry_key in dropped:
            ledger.upsert(tx, goal, row.entry_key, row.kind, "dropped", body)
        elif stale.intersection(body.get("dependsOn", [])) and row.kind != "effect":
            ledger.upsert(tx, goal, row.entry_key, row.kind, "stale", body)
    goal.runtime_state = {**goal.runtime_state, "checkedRevision": goal.revision}
