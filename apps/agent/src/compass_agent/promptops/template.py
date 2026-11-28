"""Single-pass, literal substitution of declared variables in managed Markdown."""

import re

from compass_agent.promptops.catalog import (
    EXAMPLE_VALUES,
    MAX_BYTES,
    member_usages,
    validate_body,
)
from compass_agent.promptops.errors import PromptError

_TOKEN = re.compile(r"\{\{|\}\}")
_NAME = re.compile(r"[a-zA-Z_][a-zA-Z_0-9]{0,63}")


def parts(body):
    cursor = 0
    while match := _TOKEN.search(body, cursor):
        pos = match.start()
        yield body[cursor:pos], None
        end = body.find("}}", pos + 2)
        name = body[pos + 2 : end].strip()
        if match[0] != "{{" or end < 0 or not _NAME.fullmatch(name):
            line = body.count("\n", 0, pos) + 1
            col = pos - body.rfind("\n", 0, pos)
            raise PromptError("invalid_template", f"第 {line} 行第 {col} 列的模板变量格式无效。")
        yield "", name
        cursor = end + 2
    yield body[cursor:], None


def validate_template(path, body, allowed=None):
    validate_body(body)
    if path.endswith(".json"):
        from compass_agent.promptops.structured import validate_members

        validate_members(path, body, member_usages()[1])
        return []
    names = list(dict.fromkeys(name for _, name in parts(body) if name))
    if allowed is None:
        allowed = member_usages()[1]
    permitted = allowed.get(path, EXAMPLE_VALUES)
    for name in names:
        if name not in permitted:
            raise PromptError("unknown_variable", f"{path} 的调用方未声明变量 {name}。")
    return names


def render(path, body, variables) -> str:
    names = validate_template(path, body)
    for key in names:
        value = variables.get(key)
        evidence = path == "answer/evidence.md" and key == "ordered_evidences"
        maximum = 196608 if evidence else 256
        if (
            not isinstance(value, str)
            or not value.strip()
            or (len(value.encode("utf-8")) if evidence else len(value)) > maximum
            or any(ord(c) < 32 and not (evidence and c in "\n\t\r") for c in value)
        ):
            raise PromptError(
                "invalid_variable", f"变量 {key} 需要 1 至 {maximum} 字节的有效文本。"
            )
    chunks = []
    byte_count = 0
    for text, name in parts(body):
        replacement = variables[name] if name else text
        byte_count += len(replacement.encode("utf-8"))
        if byte_count > MAX_BYTES:
            raise PromptError("body_too_large", "模板渲染结果超过 256 KiB。", 413)
        chunks.append(replacement)
    return "".join(chunks)
