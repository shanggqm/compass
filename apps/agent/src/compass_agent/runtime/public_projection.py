"""Pure v2 projection used for SQL snapshots and stream fault/replay checks."""

from copy import deepcopy

from compass_agent.models.errors import ModelError
from compass_agent.schemas import AgentPacket


def stream_position(value: str) -> tuple[int, int]:
    from re import fullmatch

    if not fullmatch(r"(0|[1-9][0-9]{0,19})-(0|[1-9][0-9]{0,19})", value):
        raise ModelError("cursor_invalid", 422)
    a, b = map(int, value.split("-"))
    if a > 2**64 - 1 or b > 2**64 - 1:
        raise ModelError("cursor_invalid", 422)
    return a, b


def upsert(values: dict, key: str, value: dict, revision: str = "revision"):
    previous = values.get(key)
    if previous is not None:
        if previous[revision] > value[revision]:
            return
        if previous[revision] == value[revision]:
            if previous != value:
                raise ModelError("projection_conflict", 409)
            return
    values[key] = value


def reduce_packet(view: dict, messages: dict, raw: dict):
    from compass_agent.runtime.answer_publication import apply_marker, marker_data

    packet = AgentPacket.model_validate(raw)
    event = packet.event.model_dump(mode="json", by_alias=True)
    kind, payload = event["type"], event["payload"]
    if data := marker_data(event):
        apply_marker(messages, data)
        upsert(view["objects"], payload["objectId"], payload)
    elif kind == "run.upsert":
        box = {"run": view["run"]} if view.get("run") else {}
        upsert(box, "run", payload, "version")
        view["run"] = box["run"]
    elif kind == "message.upsert":
        upsert(messages, payload["messageId"], payload)
    elif kind == "message.delta":
        message = messages.get(payload["messageId"])
        if message is None:
            raise ModelError("projection_message_missing", 409)
        if message["status"] != "streaming" or message["revision"] + 1 != payload["revision"]:
            raise ModelError("projection_revision_gap", 409)
        message = deepcopy(message)
        message["revision"] = payload["revision"]
        message["content"]["text"] += payload["text"]
        messages[payload["messageId"]] = message
    else:
        upsert(view["objects"], payload["objectId"], payload)
