"""Packaged defaults and usage contracts shared by the editor and runtime."""

import hashlib
import json
import re
import unicodedata
from functools import lru_cache
from importlib.resources import files

from compass_agent.promptops.errors import PromptError
from compass_agent.promptops.layout import MEMBERS, asset_path

MAX_BYTES = 256 * 1024
EXAMPLE_VALUES = {"period": "本期", "unit": "元", "audience": "经营团队"}


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_hash(value) -> str:
    return digest_text(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def normalize_path(value: str) -> str:
    path = unicodedata.normalize("NFC", value)
    if (
        not path
        or len(path) > 512
        or not path.endswith((".md", ".json"))
        or "\\" in path
        or re.match(r"^[A-Za-z]:", path)
        or any(p in ("", ".", "..") for p in path.split("/"))
        or any(unicodedata.category(c).startswith("C") for c in path)
    ):
        raise PromptError("invalid_path", "请填写规范的相对 Markdown 或 JSON 文件路径。")
    return path


def validate_body(body: str, *, draft=False):
    if any(ord(c) < 32 and c not in "\n\r\t" for c in body):
        raise PromptError("invalid_body", "正文中包含无效字符。")
    if len(body.encode("utf-8")) > MAX_BYTES:
        raise PromptError("body_too_large", "正文超过 256 KiB。", 413)
    if not draft and not body.strip():
        raise PromptError("empty_body", "发布正文不能为空。")


@lru_cache(maxsize=1)
def bundled() -> dict[str, str]:
    result = {}
    portable = set()

    def visit(node, prefix):
        for entry in sorted(node.iterdir(), key=lambda x: x.name):
            path = prefix + entry.name
            if getattr(entry, "is_symlink", lambda: False)():
                raise PromptError("invalid_package", "提示词包中含有符号链接。")
            if entry.is_dir():
                visit(entry, path + "/")
            elif entry.name.endswith((".md", ".json")) and entry.is_file():
                path = normalize_path(path)
                if path.casefold() in portable:
                    raise PromptError("path_collision", "提示词包中的文件路径冲突。")
                portable.add(path.casefold())
                body = entry.read_bytes().decode("utf-8")
                if path.startswith("tools/"):
                    from compass_agent.promptops.structured import refresh_tool_schema

                    body = refresh_tool_schema(path, body)
                validate_body(body)
                result[path] = body

    visit(files("compass_agent").joinpath("prompts"), "")
    if not result:
        raise PromptError("empty_package", "当前安装包没有提示词文件。", 503)
    return result


def current_policy():
    from compass_agent.context_runtime.profiles import POLICY_DIGEST, POLICY_VERSION

    return POLICY_VERSION, POLICY_DIGEST


def member_usages(version=None):
    from compass_agent.context_runtime.profiles import enumerate_usages

    version = version or current_policy()[0]
    refs: dict[str, set[str]] = {}
    allowed: dict[str, set[str]] = {}
    for use in enumerate_usages(version):
        label = " / ".join(str(s) for s in (use.scope, use.task, use.phase) if s)
        refs.setdefault(use.path, set()).add(label or "Context Runtime")
        available = set(use.variables)
        allowed.setdefault(use.path, available.copy()).intersection_update(available)
    from compass_agent.promptops.starter_catalog import PREFIX, SAVED_WORKFLOW, starter_usages
    from compass_agent.tools.catalog import TOOLS

    refs.setdefault("support/routing/scenes.json", set()).add("业务场景分类")
    allowed["support/routing/scenes.json"] = set()
    for spec in TOOLS:
        refs.setdefault(f"tools/{spec.name}.json", set()).add("工具声明")
        allowed[f"tools/{spec.name}.json"] = set()

    families = (
        (
            "support/routing",
            "意图识别与路由",
            (
                "business",
                "clarify",
                "classify-flash",
                "classify-flash-managed",
                "classify-jev",
                "deliverable",
                "handoff",
                "off-topic",
                "restricted",
            ),
        ),
        (
            "support/routing/scenes",
            "业务场景分类",
            (p.rsplit("/", 1)[-1][:-3] for p in MEMBERS if p.startswith("support/routing/scenes/")),
        ),
        (
            "shared/tools/definitions",
            "工具声明",
            (
                p.rsplit("/", 1)[-1][:-3]
                for p in MEMBERS
                if p.startswith("shared/tools/definitions/")
            ),
        ),
        (
            "shared/tools",
            "受管工具说明",
            ("database-query", "schedule-guidance", "schedule-unavailable", "workflow-label"),
        ),
        (
            "shared/fragments",
            "Agent 装配",
            (
                "base",
                "retail",
                "output-json",
                "output-schema",
                "plan-required",
                "tool-limit",
                "workflow",
            ),
        ),
        ("shared/fragments", "长任务", ("long-task", "delegation")),
        ("support/context", "上下文优化", ("selection", "compaction")),
        ("shared/fragments", "上下文优化", ("context-results",)),
        ("support/diagnostics", "运行诊断", ("reliability-request",)),
        ("support/delivery", "最终交付格式修正", ("repair",)),
        ("answer", "交付完成度复核", ("review",)),
        ("support/session", "会话标题", ("title",)),
        ("support/subagent", "子任务", ("analysis",)),
    )
    for directory, label, names in families:
        for name in names:
            path = f"{directory}/{name}.md"
            refs.setdefault(path, set()).add(label)
            allowed.setdefault(path, set())
    for path, label in starter_usages().items():
        refs.setdefault(path, set()).add(label)
        allowed.setdefault(path, set())
    for path in ("shared/fragments/tool-limit.md", "shared/fragments/workflow.md"):
        allowed[path] = {"tool_limit"}
    refs["support/subagent/report-to-parent.json"] = {"子任务汇报"}
    allowed["support/subagent/report-to-parent.json"] = set()
    allowed["shared/fragments/delegation.md"] = {"max_tasks", "max_parallel"}
    allowed[PREFIX + SAVED_WORKFLOW] = {"workflow_name"}
    return refs, allowed


def usages(version=None):
    refs, allowed = member_usages(version)
    grouped: dict[str, set[str]] = {}
    variables: dict[str, set[str]] = {}
    for path, labels in refs.items():
        target = asset_path(path)
        grouped.setdefault(target, set()).update(labels)
        variables.setdefault(target, set()).update(allowed.get(path, ()))
    return grouped, variables


def retired(path):
    return path in MEMBERS
