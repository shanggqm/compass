"""Bind the pure assembler to frozen Run state and current authorization."""

from dataclasses import asdict
from uuid import UUID

from compass_agent.context_runtime.source_access import read_private, save_private
from compass_agent.execution_context import execution_path
from compass_agent.tools.assembler import AssemblyInput, assemble_step
from compass_agent.tools.contracts import RegisteredTool, ToolGovernance, ToolPolicy
from compass_agent.tools.registry import bounded_payload, parse_arguments


def freeze_work_entry(
    journal, *, requires_plan=False, is_semantic_maintainer=False, required_tools=()
):
    path = execution_path.get()
    key = f"tools/v3/work-entry/{path}"
    with journal.store.transaction() as tx:
        _, run = journal.store.lock(tx, journal.run_id)
        journal.store.require_epoch(run, journal.epoch)
        if read_private(journal.store, tx, journal.run_id, key) is None:
            from compass_agent.goal.controller import open_goal
            from compass_agent.schemas import TrustedContext
            from compass_agent.semantic.service import has_session_draft

            active_goal = open_goal(tx, run.session_id)
            frozen = {
                "has_active_plan": bool(journal.entry_plan_active),
                "has_active_goal": bool(active_goal and active_goal.status in {"active", "paused"}),
                "has_semantic_draft": is_semantic_maintainer
                and has_session_draft(
                    journal, tx, run, TrustedContext.model_validate(run.config_snapshot["context"])
                ),
                "requires_plan": requires_plan,
                "required_tools": list(required_tools),
            }
            save_private(journal.store, tx, run, key, frozen, [])


def assemble(journal, tx, ctx, draft, grants, *, requires_plan=False):
    path = execution_path.get()
    frozen = read_private(journal.store, tx, journal.run_id, f"tools/v3/work-entry/{path}") or {}
    selection = journal.tool_selection
    turn = getattr(journal, "answer_turn", {}).get(path, {})
    inputs = AssemblyInput(
        scope_profile=ctx.scope_profile,
        task=ctx.task,
        scene_id=ctx.scene,
        phase="answer_stream" if turn.get("purpose") == "answer_stream" else ctx.phase,
        turn_purpose=turn.get("purpose", "work"),
        delivery_only=turn.get("delivery_only", False),
        caller_role="subagent" if path != "root" else "scheduler" if journal.scheduled else "root",
        plugins_permitted=grants.get("plugins") is True,
        is_semantic_maintainer=grants.get("semantic_maintainer") is True,
        selected_packs=frozenset(selection.get("packs", [])),
        selected_workflow_ids=frozenset(UUID(v) for v in selection.get("workflowIds", [])),
        selected_mcp_server_ids=frozenset(UUID(v) for v in selection.get("mcpServerIds", [])),
        has_active_plan=bool(frozen.get("has_active_plan")),
        has_active_goal=bool(frozen.get("has_active_goal")),
        has_semantic_draft=bool(frozen.get("has_semantic_draft")),
        requires_plan=bool(frozen.get("requires_plan", requires_plan)),
        required_tools=frozenset(frozen.get("required_tools", ())),
    )
    available = {t.function.name: t for t in draft.tools}
    from dataclasses import replace

    entries = {
        name: replace(e, definition=available[name])
        for name, e in journal.tool_entries.items()
        if name in available
    }
    if path != "root" and "report_to_parent" in available:
        # The supervisor executes this primitive. Reserve its declaration inside
        # the same slot/byte budget as the child's inherited read-only tools.
        report = RegisteredTool(
            "report_to_parent",
            available["report_to_parent"],
            ToolPolicy(),
            None,
            parse_arguments,
            lambda *_: None,
            bounded_payload,
            governance=ToolGovernance(channel="always_on", pack="subagent_base"),
        )
        entries = {"report_to_parent": report, **entries}
    result = assemble_step(entries, inputs)
    definitions = result.definitions
    trace = asdict(result.trace)
    trace["exposed"] = [{**item, "label": item["name"]} for item in trace["exposed"]]
    trace["deferred"] = list(trace["deferred"][:20])
    trace["excluded"] = [{"name": name, "reason": reason} for name, reason in trace["excluded"]]
    return draft.model_copy(update={"tools": definitions}), trace
