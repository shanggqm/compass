"""Generic after-tool-batch port; interpretation belongs to registered adapters."""

import json
from uuid import uuid5

from compass_agent.context_runtime.source_access import (
    assistant_message,
    read_private,
    save_private,
)
from compass_agent.execution_context import execution_path
from compass_agent.models.errors import ModelError
from compass_agent.runtime.private_state import digest


def model_observation(payload, observed):
    """Keep the receipt once; reference identical evidence records with JSON pointers."""
    locations = {}

    def visit(value, pointer):
        if isinstance(value, dict):
            locations.setdefault(digest(value), pointer)
            for key, child in value.items():
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                visit(child, f"{pointer}/{escaped}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{pointer}/{index}")

    visit(payload, "#/result")
    summaries = []
    for summary in observed["evidence"]:
        pointer = locations.get(digest(summary["record"])) if "record" in summary else None
        summaries.append(
            {
                **{key: value for key, value in summary.items() if key != "record"},
                "recordRef": pointer,
            }
            if pointer
            else summary
        )
    return {"result": payload, "verification": {**observed, "evidence": summaries}}


def user_input(journal, question):
    path = execution_path.get()
    identifier = str(uuid5(journal.run_id, f"answer-input:{path}"))
    item = {
        "id": identifier,
        "kind": "user_input",
        "title": "用户提供",
        "version": digest(question),
        "locator": "user-input",
        "sourceRefs": [],
        "record": {"sectionId": "user-input", "text": question},
    }
    with journal.store.transaction() as tx:
        _, run = journal.store.lock(tx, journal.run_id)
        journal.store.require_epoch(run, journal.epoch)
        save_private(journal.store, tx, run, f"answer/v2/evidence/{identifier}", item, [])
    ledger = getattr(journal, "answer_evidence", {})
    ledger.setdefault(path, {})[identifier] = item
    journal.answer_evidence = ledger
    return identifier


def after_tool_batch(journal, step, results, registry, trusted):
    observations = []
    with journal.store.transaction() as tx:
        _, run = journal.store.lock(tx, journal.run_id, trusted)
        journal.store.require_epoch(run, journal.epoch)
        refs = list(assistant_message(tx, run.id).source_refs or [])
        ledger = getattr(journal, "answer_evidence", {})
        evidence = ledger.setdefault(execution_path.get(), {})
        batch_key = f"answer/v2/batch/{execution_path.get()}/{step.id}"
        previous = read_private(journal.store, tx, run.id, batch_key)
        if previous:
            for observed in previous["observations"]:
                for summary in observed["evidence"]:
                    identifier = summary["evidenceId"]
                    item = read_private(
                        journal.store, tx, run.id, f"answer/v2/evidence/{identifier}"
                    )
                    if item is None:
                        raise ModelError("checkpoint_corrupt", 409)
                    evidence[identifier] = item
            journal.answer_evidence = ledger
            return previous["observations"]
        for call, result in zip(step.tool_calls, results, strict=True):
            entry = registry.entries.get(call.function.name)
            observed = {
                "callId": call.id,
                "tool": call.function.name,
                "status": result.status,
                "code": result.reason_code,
                "evidence": [],
            }
            if entry and entry.evidence_adapter and result.status in {"ok", "empty"}:
                try:
                    items = (
                        [
                            {"importEvidenceId": identifier}
                            for identifier in result.payload["evidenceIds"]
                        ]
                        if isinstance(result.payload, dict) and result.payload.get("reused")
                        else list(
                            entry.evidence_adapter(
                                json.loads(call.function.arguments), result.payload, refs
                            )
                        )
                    )
                except (ValueError, KeyError, TypeError):
                    items = []
                    observed["feedback"] = "结果可继续使用；引用定位暂不可用，可重读来源。"
                for index, item in enumerate(items):
                    imported = item.get("importEvidenceId")
                    if imported:
                        item = read_private(
                            journal.store, tx, run.id, f"answer/v2/evidence/{imported}"
                        )
                        if item is None or not all(ref in refs for ref in item["sourceRefs"]):
                            observed["feedback"] = "引用来源当前不可导入，请重新读取所需来源。"
                            continue
                    identifier = imported or str(
                        uuid5(
                            run.id,
                            f"answer-evidence:{execution_path.get()}:{step.id}:{call.id}:{index}",
                        )
                    )
                    if not imported:
                        parents = [
                            key
                            for key, previous in evidence.items()
                            if previous["record"].get("resultId") in item.get("parentResultIds", [])
                        ]
                        if item.get("parentResultIds") and len(parents) < len(
                            set(item["parentResultIds"])
                        ):
                            observed["feedback"] = (
                                "派生结果缺少已登记的输入来源，请先查询并保留输入回执。"
                            )
                            continue
                        if parents:
                            item = {**item, "parentEvidenceIds": parents}
                        item = {
                            **item,
                            "id": identifier,
                            "callId": call.id,
                            "tool": call.function.name,
                        }
                        save_private(
                            journal.store,
                            tx,
                            run,
                            f"answer/v2/evidence/{identifier}",
                            item,
                            item["sourceRefs"],
                        )
                    evidence[identifier] = item
                    observed["evidence"].append(
                        {
                            "evidenceId": identifier,
                            "kind": item["kind"],
                            "title": item["title"],
                            "locator": item["locator"],
                            "record": item["record"],
                        }
                    )
            from compass_agent.goal.ledger import Ledger

            context = getattr(journal, "goal_tool_contexts", {}).get((str(step.id), call.id))
            Ledger(journal.store).remember(
                tx,
                run,
                journal,
                call,
                result,
                entry,
                [evidence[v["evidenceId"]] for v in observed["evidence"]],
                context,
            )
            observations.append(observed)
        journal.answer_evidence = ledger
        save_private(
            journal.store,
            tx,
            run,
            batch_key,
            {"observations": observations, "revision": digest(evidence)},
            refs,
        )
    return observations
