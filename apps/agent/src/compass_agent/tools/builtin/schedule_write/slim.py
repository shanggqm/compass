"""Compile human schedule shorthand and freeze the owner's revision before approval."""

import re
from uuid import UUID

from compass_agent.tools.contracts import ToolRejected
from compass_agent.tools.input_adapter import frozen_input


def compile_schedule_expr(value):
    daily = re.fullmatch(r"daily\s+([01]?\d|2[0-3]):([0-5]\d)", value.strip(), re.I)
    if daily:
        return f"{int(daily[2])} {int(daily[1])} * * *"
    weekly = re.fullmatch(
        r"weekly\s+(MON|TUE|WED|THU|FRI|SAT|SUN)\s+([01]?\d|2[0-3]):([0-5]\d)", value.strip(), re.I
    )
    if weekly:
        day = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}[
            weekly[1].upper()
        ]
        return f"{int(weekly[3])} {int(weekly[2])} * * {day}"
    return value.strip()


def normalizer(service):
    def normalize(raw, context):
        if "operation" in raw:
            return raw

        def build(tx, session, run):
            patch = {k: raw[k] for k in ("name", "question") if k in raw}
            if "schedule" in raw:
                patch["cron"] = compile_schedule_expr(raw["schedule"])
            if "status" in raw:
                patch["enabled"] = raw["status"] == "enabled"
            if raw.get("scheduleId"):
                plan = service.owned(tx, context.trusted, UUID(raw["scheduleId"]))
                args = {
                    "operation": "delete" if raw.get("status") == "deleted" else "update",
                    "scheduleId": str(plan.id),
                    "expectedRevision": plan.revision,
                }
                if args["operation"] == "update":
                    args["patch"] = patch
                return args
            if raw.get("status") == "deleted":
                raise ToolRejected("invalid_parameters")
            command = run.config_snapshot.get("command", {})
            # User-specific timezone is carried by the existing source snapshot when present.
            binding = run.config_snapshot.get("contextRuntime") or {}
            timezone = run.config_snapshot.get("promptEnvironment", {}).get("timezone") or (
                binding.get("search") or {}
            ).get("timezone", "UTC")
            return {
                "operation": "create",
                "selection": command.get("selection", {"mode": "auto"}),
                "timezone": timezone,
                **patch,
            }

        return frozen_input("schedule_write", raw, context, build)

    return normalize
