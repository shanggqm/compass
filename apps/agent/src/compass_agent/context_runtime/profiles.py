"""Versioned selection rules for runtime assembly and PromptOps usage previews."""

import json
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

Task = Literal["business_qa", "data_analysis", "dashboard", "business_report"]
Phase = Literal["identify", "plan", "query", "analyze", "generate"]
TASKS: tuple[Task, ...] = ("business_qa", "data_analysis", "dashboard", "business_report")
PHASES: tuple[Phase, ...] = ("identify", "plan", "query", "analyze", "generate")
POLICY_VERSION = "context-v1"


def _policy_digest():
    root = files("compass_agent")
    digest = sha256()
    for path in (
        "context_runtime/profiles.py",
        "context_runtime/assembly.json",
        "promptops/catalog.py",
        "promptops/template.py",
        "promptops/layout.py",
        "promptops/structured.py",
        "promptops/legacy_layout.json",
        "promptops/starter_catalog.json",
        "routing/dictionary.py",
    ):
        data = root.joinpath(*path.split("/")).read_bytes()
        digest.update(path.encode() + b"\0" + sha256(data).digest())
    scene_document = json.loads(root.joinpath("prompts/support/routing/scenes.json").read_text())
    fixed = {
        key: {
            field: value
            for field, value in row.items()
            if field not in {"description", "examples", "sop"}
        }
        for key, row in scene_document["scenes"].items()
    }
    digest.update(json.dumps(fixed, sort_keys=True, ensure_ascii=False).encode())
    return digest.hexdigest()


POLICY_DIGEST = _policy_digest()
BASE = "shared/fragments/base.md"

ASSEMBLY = json.loads(files("compass_agent").joinpath("context_runtime/assembly.json").read_text())
SCENE_FILES = {
    (r["task"], r["scene"]): r["path"] for r in ASSEMBLY["sceneTemplates"] if "scope" not in r
}
SCOPED_SCENE_FILES = {
    (r["task"], r["scene"], r["scope"]): r["path"]
    for r in ASSEMBLY["sceneTemplates"]
    if "scope" in r
}


def search_pack(search, scene):
    pack = ASSEMBLY["searchPacks"].get((search or {}).get("scenePack"))
    return pack if pack and scene not in pack["excludedScenes"] else {}


def discover_plugins(scope: str, registry: bool) -> bool:
    ceiling = ASSEMBLY["scopeProfiles"][scope].get("toolCeiling")
    return ceiling is None or bool(registry and ceiling.get("registryPluginPrefixes"))


def numeric_intent(task_type: str, deliverable: str | None) -> bool:
    return task_type not in ASSEMBLY["definitionIntents"] and (
        task_type in ASSEMBLY["numericIntents"]
        or deliverable in {"business_report", "dashboard", "data_analysis"}
    )


class ScopeRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: UUID = Field(alias="tenantId")
    workspace_id: UUID | None = Field(None, alias="workspaceId")
    profile: str

    @field_validator("profile")
    @classmethod
    def registered_profile(cls, value):
        if value not in ASSEMBLY["scopeProfiles"]:
            raise ValueError("Unknown context scope profile")
        return value


def scope_profile(scope, rules: list[ScopeRule]) -> str:
    matches = [rule for rule in rules if rule.tenant_id == scope.tenant_id]
    by_scope = {r.workspace_id: r.profile for r in matches}
    if len(matches) != len(by_scope):
        raise ValueError("duplicate context scope rule")
    return by_scope.get(scope.workspace_id, by_scope.get(None, "platform"))


@dataclass(frozen=True, slots=True)
class PathUsage:
    path: str
    task: str | None = None
    phase: str | None = None
    scope: str = "platform"
    variables: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillSpec:
    key: str
    path: str
    tools: frozenset[str]
    phases: tuple[Phase, ...]
    tasks: tuple[Task, ...]


SKILLS = (
    SkillSpec(
        "agentic-search",
        "shared/skills/agentic-search.md",
        frozenset(),
        ("plan", "query", "analyze"),
        TASKS,
    ),
    SkillSpec(
        "metric-check",
        "shared/skills/metric-check.md",
        frozenset({"database_query"}),
        ("plan", "query", "analyze"),
        TASKS,
    ),
    SkillSpec(
        "report-outline",
        "shared/skills/report-outline.md",
        frozenset(),
        ("plan", "generate"),
        ("business_report",),
    ),
)


@dataclass(frozen=True, slots=True)
class Profile:
    paths: tuple[str, ...]
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]


def enumerate_usages(policy_version=POLICY_VERSION) -> tuple[PathUsage, ...]:
    if policy_version != POLICY_VERSION:
        raise ValueError("incompatible context policy")
    return (
        PathUsage(BASE),
        PathUsage("shared/fragments/answer-protocol.md"),
        PathUsage("shared/fragments/answer-work.md"),
        PathUsage("answer/plain.md"),
        PathUsage("answer/evidence.md", variables=("ordered_evidences",)),
        PathUsage("shared/fragments/search-protocol.md"),
        *(PathUsage(p["path"]) for p in ASSEMBLY["searchPacks"].values()),
        PathUsage("shared/fragments/semantics.md"),
        PathUsage("shared/fragments/semantic-governance.md"),
        *(PathUsage(f"tasks/{t}/system.md", t) for t in TASKS),
        *(PathUsage(f"shared/stages/{s}.md", phase=s) for s in PHASES),
        *(
            PathUsage(
                r["path"],
                r["task"],
                scope=r.get("scope", "platform"),
                variables=tuple(r.get("variables", ())),
            )
            for r in ASSEMBLY["sceneTemplates"]
        ),
        *(PathUsage(s.path, t, phase) for s in SKILLS for t in s.tasks for phase in s.phases),
    )


def resolve_profile(
    scope: str,
    task: Task,
    scene: str,
    phase: Phase,
    *,
    available: tuple[str, ...],
    plugins: bool,
    enabled: bool = True,
    registry: bool = False,
    search: dict | None = None,
    answer_protocol: int = 1,
    intermediate: bool = False,
) -> Profile:
    stage_tools = {
        "identify": frozenset(),
        "plan": frozenset({"todo_write"}),
        "query": None,
        "analyze": None,
        "generate": frozenset(),
    }[phase]
    if answer_protocol >= 2 and phase != "identify":
        stage_tools = None
    from compass_agent.tools.catalog import TOOLS

    ceiling = ASSEMBLY["scopeProfiles"][scope].get("toolCeiling")
    if answer_protocol >= 2:
        ceiling = None
    scope_tools = set(ceiling["base"]) if ceiling else set()
    plugin_prefixes = ()
    if ceiling and registry:
        scope_tools.update(t.name for t in TOOLS if t.capability in ceiling["registryCapabilities"])
        if plugins:
            plugin_prefixes = tuple(ceiling["registryPluginPrefixes"])
    if ceiling and search:
        scope_tools.update(ceiling["withSearch"])
    allowed, excluded = [], []
    for name in available:
        if search:
            from compass_agent.context_runtime.search_control import READ_TOOLS

            if name not in READ_TOOLS | {"todo_write"}:
                excluded.append((name, "scope"))
                continue
        if not enabled or (name.startswith("mcp_") and not plugins):
            excluded.append((name, "permission"))
        elif ceiling and name not in scope_tools and not name.startswith(plugin_prefixes):
            excluded.append((name, "scope"))
        elif stage_tools is not None and name not in stage_tools:
            excluded.append((name, "stage"))
        else:
            allowed.append(name)
    paths = [BASE]
    if search and phase != "identify":
        paths.append("shared/fragments/search-protocol.md")
        pack = search_pack(search, scene)
        if pack:
            paths.append(pack["path"])
    if registry and phase != "identify":
        paths.append("shared/fragments/semantics.md")
    if phase != "identify":
        paths.append(f"tasks/{task}/system.md")
        scene_path = SCOPED_SCENE_FILES.get((task, scene, scope), SCENE_FILES.get((task, scene)))
        if scene_path:
            paths.append(scene_path)
    paths.append(f"shared/stages/{phase}.md")
    skills = []
    for skill in SKILLS:
        if skill.key == "agentic-search" and answer_protocol >= 2:
            # The default protocol already contains the search method, without legacy gates.
            skills.append(skill.key)
            continue
        if skill.key == "agentic-search" and not (search and search.get("skillEnabled")):
            continue
        if phase not in skill.phases or task not in skill.tasks:
            continue
        if not plugins or not skill.tools.issubset(allowed):
            excluded.append((skill.key, "dependency" if plugins else "permission"))
            continue
        paths.append(skill.path)
        skills.append(skill.key)
    if answer_protocol >= 2 and phase != "identify" and not intermediate:
        paths.append(
            "shared/fragments/answer-work.md"
            if answer_protocol >= 3
            else "shared/fragments/answer-protocol.md"
        )
    return Profile(tuple(paths), tuple(allowed), tuple(skills), tuple(excluded))
