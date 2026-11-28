# 意图分类体系极简重构与全局动态装配 代码设计附录

> 主技术方案：[TECH_DESIGN.md](./TECH_DESIGN.md) | Feature：968（[返回 BRIEF](../BRIEF.md)） | 状态：设计示意（待评审，尚未写入生产源码或运行验证）

本附录按模块给出供架构与代码评审核对的核心类型契约、关键算法分支、工具治理矩阵与双库迁移 DDL。外围样板代码（如普通 Getter/Setter、既有事务包装、UI 样式细节）已省略，关键正确性分支、权限边界与不变量完整展开。

---

## 1. 正交意图契约与只读字典（`schemas.py` & `routing/dictionary.py`）

### 1.1 公开契约模型（拟修改 `apps/agent/src/compass_agent/schemas.py`）

> 评审关注点：确认 `Intent`、`RoutingFeatures` 与 `RunRoutedPayload` 彻底去除 `scene_id`、`category_id` 与 `deliverable` 双轨冗余，且所有字面量枚举唯一定义、无字符串魔法值。

```python
# 设计示意：apps/agent/src/compass_agent/schemas.py（替换原 Intent / RoutingFeatures / RunRoutedPayload）

TaskIntentId = Literal[
    "qa",
    "analysis",
    "diagnosis",
    "dashboard",
    "report",
    "automation",
    "clarify",
    "handoff",
    "restricted",
    "off_topic",
]

WorkIntentId = Literal["qa", "analysis", "diagnosis", "dashboard", "report", "automation"]
BoundaryIntentId = Literal["clarify", "handoff", "restricted", "off_topic"]

DomainId = Literal[
    "sales_channel",
    "marketing_member",
    "product_supply",
    "trade_fulfillment",
    "store_region",
    "finance_cost",
    "platform_data",
]

DomainSource = Literal["classifier", "workspace_prior", "none"]
DeliveryKind = Literal["qa", "analysis", "dashboard", "report", "none"]
DifficultyLevel = Literal["L1", "L2", "L3"]


class Intent(StrictModel):
    task_intent: TaskIntentId
    domains: list[DomainId] = Field(default_factory=list, max_length=2)
    domain_source: DomainSource = "none"
    is_compound: bool = False


class RoutingFeatures(StrictModel):
    group: Literal["work", "boundary"]
    base_difficulty: DifficultyLevel
    effective_difficulty: DifficultyLevel
    response_mode: Literal["answer", "clarify", "refuse", "handoff"]
    delivery_kind: DeliveryKind
    semantic_domains: list[str] = Field(default_factory=list)
    requires_plan: bool = False


class RunRoutedPayload(RunContractModel):
    provider_id: RunUUID = Field(alias="providerId")
    model_label: Annotated[str, Field(min_length=1, max_length=160)] = Field(alias="modelLabel")
    task_intent: TaskIntentId = Field(alias="taskIntent")
    domains: list[DomainId] = Field(default_factory=list, max_length=2)
    domain_source: DomainSource = Field(alias="domainSource")
    is_compound: bool = Field(alias="isCompound")
    tools_enabled: bool = Field(alias="toolsEnabled")
```

### 1.2 资产结构与动态摘要加载（拟修改 `apps/agent/src/compass_agent/routing/dictionary.py`）

> 评审关注点：固定路由属性始终只从随包文件读取（防止线上 PromptOps 误改破坏执行契约）；`description` / `examples` / `guidance` 从当前 Run 绑定的 PromptOps 版本读取；字典版本摘要自动计算，杜绝手工维护版本号漂移。

```python
# 设计示意：apps/agent/src/compass_agent/routing/dictionary.py

import hashlib
import json
from functools import lru_cache
from importlib.resources import files
from typing import get_args

from compass_agent.models.errors import ModelError
from compass_agent.prompt_assets import prompt_text
from compass_agent.schemas import DomainId, Intent, RoutingFeatures, TaskIntentId

TASK_IDS: frozenset[str] = frozenset(get_args(TaskIntentId))
DOMAIN_IDS: frozenset[str] = frozenset(get_args(DomainId))
_TASK_EDITABLE = frozenset({"description", "examples"})
_DOMAIN_EDITABLE = frozenset({"description", "examples", "guidance"})


@lru_cache
def catalogs() -> tuple[str, dict[str, dict], dict[str, dict]]:
    root = files("compass_agent").joinpath("prompts/support/routing")
    raw_tasks = json.loads(root.joinpath("tasks.json").read_text())["tasks"]
    raw_domains = json.loads(root.joinpath("domains.json").read_text())["domains"]

    tasks = {
        key: {k: v for k, v in row.items() if k not in _TASK_EDITABLE}
        for key, row in raw_tasks.items()
    }
    domains = {
        key: {k: v for k, v in row.items() if k not in _DOMAIN_EDITABLE}
        for key, row in raw_domains.items()
    }
    if set(tasks) != TASK_IDS or set(domains) != DOMAIN_IDS:
        raise ValueError("Routing task/domain catalog IDs diverged from schema literals")

    payload = json.dumps({"tasks": tasks, "domains": domains}, sort_keys=True, ensure_ascii=False)
    version = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return version, tasks, domains


def build_intent(
    task_intent: str,
    raw_domains: list[str] | tuple[str, ...],
    *,
    is_compound: bool = False,
    workspace_default_domains: tuple[str, ...] = (),
) -> Intent:
    _, tasks, domains = catalogs()
    task_row = tasks.get(task_intent)
    if task_row is None:
        raise ModelError("intent_invalid", 503)

    # 边界守卫类意图不挂载业务域，也不触发复合任务升档
    if task_row["group"] == "boundary":
        return Intent(task_intent=task_intent, domains=[], domain_source="none", is_compound=False)

    deduped: list[str] = []
    for item in raw_domains:
        if item in domains and item not in deduped:
            deduped.append(item)
        if len(deduped) == 2:
            break

    if deduped:
        return Intent(
            task_intent=task_intent,
            domains=deduped,  # type: ignore[arg-type]
            domain_source="classifier",
            is_compound=bool(is_compound),
        )

    prior = [d for d in workspace_default_domains if d in domains][:2]
    if prior:
        return Intent(
            task_intent=task_intent,
            domains=prior,  # type: ignore[arg-type]
            domain_source="workspace_prior",
            is_compound=bool(is_compound),
        )

    return Intent(
        task_intent=task_intent,
        domains=[],
        domain_source="none",
        is_compound=bool(is_compound),
    )


def features_for(intent: Intent, *, explicit_plan: bool = False) -> RoutingFeatures:
    _, tasks, domains = catalogs()
    row = tasks[intent.task_intent]
    base = row["default_difficulty"]
    effective = base
    if row["group"] == "work":
        if intent.is_compound or len(intent.domains) >= 2:
            effective = "L3"

    semantic_domains: list[str] = []
    for domain_id in intent.domains:
        for code in domains[domain_id]["semantic_domains"]:
            if code not in semantic_domains:
                semantic_domains.append(code)

    return RoutingFeatures(
        group=row["group"],
        base_difficulty=base,
        effective_difficulty=effective,
        response_mode=row["response_mode"],
        delivery_kind=row["delivery_kind"],
        semantic_domains=semantic_domains,
        requires_plan=explicit_plan or (row["group"] == "work" and intent.is_compound),
    )


def domain_guidances(domain_ids: list[str] | tuple[str, ...]) -> list[tuple[str, str, str]]:
    """返回 [(domain_id, domain_name, guidance_text)]，从本 Run 固定的 PromptOps 版本读取。"""
    if not domain_ids:
        return []
    document = json.loads(prompt_text("support/routing/domains.json"))["domains"]
    _, _, fixed = catalogs()
    result: list[tuple[str, str, str]] = []
    for domain_id in domain_ids:
        text = (document.get(domain_id) or {}).get("guidance", "").strip()
        if text:
            result.append((domain_id, fixed[domain_id]["name"], text))
    return result


def semantic_context_args(question: str, features: RoutingFeatures) -> dict:
    return {
        "question": question[:2400],
        "domains": list(features.semantic_domains),
    }
```

---

## 2. 两级正交分类器与选模逻辑（`routing/intent.py` & `routing/service.py`）

> 评审关注点：
> 1. JEV `secondary_domain` 与 `primary_domain` 置信度不足时安全退化（次域忽略、主域可回退工作区先验），仅 `task_intent` 不确定时才触发 Flash 兜底。
> 2. 彻底移除原 `service.py` 中 `deliverable` 与 `features.response_mode` 互相覆盖的补丁代码。

```python
# 设计示意：apps/agent/src/compass_agent/routing/intent.py

class IntentPrediction(StrictModel):
    task_intent: TaskIntentId
    domains: list[DomainId] = Field(default_factory=list, max_length=2)
    is_compound: bool = False


def decision_input(value, *, facts: RunFacts | None = None) -> DecisionInput:
    state: dict = {"messages": [m.model_dump() for m in value.messages]}
    if facts and facts.organization:
        state["runOrganization"] = json.loads(facts.organization)
    domain_criteria = {
        "none": "无明确单一业务域，或属于边界守卫/通用闲聊请求",
        **domain_descriptions(),
    }
    return DecisionInput(
        state=state,
        questions={
            "task_intent": DecisionQuestion(
                type="choice",
                instructions=prompt_text("shared/stages/identify.md")
                + "\n"
                + prompt_text("support/routing/classify-jev.md")
                + ("\n\n" + facts.instructions if facts else ""),
                criteria=task_descriptions(),
            ),
            "primary_domain": DecisionQuestion(
                type="choice",
                instructions="识别本轮问题最核心涉及的业务域；不涉及具体业务域或边界请求选 none。",
                criteria=domain_criteria,
            ),
            "secondary_domain": DecisionQuestion(
                type="choice",
                instructions="仅当问题明确跨越第二个业务域时选择对应域，否则选 none。",
                criteria=domain_criteria,
            ),
            "is_compound": DecisionQuestion(
                type="noul",
                instructions="本轮任务是否需要多阶段拆解、跨多源交叉归因或先深度分析再生成报告？",
            ),
        },
    )


def from_jev(response, *, workspace_default_domains: tuple[str, ...] = ()) -> Intent | None:
    answers = response.answers or {}
    if "task_intent" not in answers:
        return None
    # 仅 task_intent 置信度不足才触发 Flash 兜底
    task_intent = choice(response, "task_intent")
    if task_intent is None:
        return None

    picked_domains: list[str] = []
    primary = choice(response, "primary_domain")
    if primary and primary != "none":
        picked_domains.append(primary)
    secondary = choice(response, "secondary_domain")
    if secondary and secondary != "none" and secondary not in picked_domains:
        picked_domains.append(secondary)

    compound_answer = answers.get("is_compound")
    is_compound = bool(
        compound_answer
        and compound_answer.type == "noul"
        and (compound_answer.probability or 0.0) >= 0.6
    )
    try:
        return build_intent(
            task_intent,
            picked_domains,
            is_compound=is_compound,
            workspace_default_domains=workspace_default_domains,
        )
    except ModelError:
        return None


def from_flash(response, *, workspace_default_domains: tuple[str, ...] = ()) -> Intent:
    if response.status != "completed":
        raise ModelError("intent_unavailable", 503)
    try:
        parsed = IntentPrediction.model_validate_json(response.text)
    except ValueError as exc:
        raise ModelError("intent_invalid", 503) from exc
    return build_intent(
        parsed.task_intent,
        parsed.domains,
        is_compound=parsed.is_compound,
        workspace_default_domains=workspace_default_domains,
    )
```

```python
# 设计示意：apps/agent/src/compass_agent/routing/service.py（select_route 核心选模段）

features = features_for(
    intent,
    explicit_plan=bool(
        getattr(command, "constraints", None) and command.constraints.plan == "required"
    ),
)
off_topic = intent.task_intent == "off_topic"
if off_topic:
    provider_id, reason = (await self.provider("flash"))[0], "off_topic_flash"
elif selection.mode == "fixed":
    provider_id, reason = selection.provider_id, "user_fixed_main_model"
elif features.group == "boundary":
    provider_id, reason = (await self.provider("flash"))[0], "boundary_flash"
else:
    upgraded = features.effective_difficulty != features.base_difficulty
    role = "pro" if features.effective_difficulty in {"L2", "L3"} else "flash"
    provider_id = (await self.provider(role))[0]
    reason = "compound_upgrade" if upgraded else f"difficulty_{features.effective_difficulty}"
```

---

## 3. 动态工具四通道全局装配器（`tools/contracts.py` & `tools/assembler.py`）

> 评审关注点：
> 1. 硬权限过滤（`plugins_permitted`、`requires_semantic_maintainer`、`scope_ceiling`、`caller_role`）始终在 Step 1 执行，任何意图或业务域均不能绕过授权边界。
> 2. 彻底清除 5 组 `SC-*` 常量与 `default_scenes` / `default_tasks` 双字段；同时支持 `intent_routed` 与 `domain_routed` 通道，并记录完整 `ToolAssemblyTrace`。

```python
# 设计示意：apps/agent/src/compass_agent/tools/contracts.py

LoadingChannel = Literal["always_on", "intent_routed", "domain_routed", "user_selected"]


@dataclass(frozen=True, slots=True)
class ToolGovernance:
    """代码权威维护的工具装载元数据；用户选择或意图匹配绝不授予执行权限。"""

    channel: LoadingChannel = "user_selected"
    pack: CapabilityPack = "user_extension"
    default_intents: frozenset[TaskIntentId] = frozenset()
    domains: frozenset[DomainId] = frozenset()
    compound_enabled: bool = False
    allowed_phases: frozenset[str] = frozenset({"work"})
    requires_semantic_maintainer: bool = False
    subagent_allowed: bool = False
    scheduler_allowed: bool = True
    summary: str = ""
```

```python
# 设计示意：apps/agent/src/compass_agent/tools/assembler.py

DIRECT_STREAM_INTENTS: frozenset[TaskIntentId] = frozenset({"clarify", "restricted", "off_topic"})


@dataclass(frozen=True, slots=True)
class AssemblyInput:
    """在 identify -> work 入口冻结一次，确保同一 Run 工作阶段工具前缀 100% 恒定。"""

    scope_profile: str
    task_intent: TaskIntentId
    domains: frozenset[DomainId] = frozenset()
    is_compound: bool = False
    phase: str = "query"
    turn_purpose: str = "work"
    delivery_only: bool = False
    caller_role: str = "root"
    plugins_permitted: bool = True
    is_semantic_maintainer: bool = False
    requires_plan: bool = False
    has_active_plan: bool = False
    has_active_goal: bool = False
    has_semantic_draft: bool = False
    selected_packs: frozenset[str] = frozenset()
    selected_workflow_ids: frozenset[UUID] = frozenset()
    selected_mcp_server_ids: frozenset[UUID] = frozenset()
    scope_ceiling: frozenset[str] | None = None
    scope_plugin_prefixes: tuple[str, ...] = ()
    skill_recommended: frozenset[str] = frozenset()


def should_direct_stream(ctx: AssemblyInput) -> bool:
    """纯澄清 / 明显越权 / 闲聊默认旁路工作工具循环，直接流式应答。"""
    return (
        ctx.caller_role == "root"
        and ctx.task_intent in DIRECT_STREAM_INTENTS
        and not ctx.selected_packs
        and not ctx.selected_workflow_ids
        and not ctx.selected_mcp_server_ids
        and not ctx.has_active_goal
    )


def _matches_user_selected(entry: RegisteredTool, ctx: AssemblyInput) -> bool:
    gov = entry.governance
    if ctx.caller_role == "subagent":
        return False
    if entry.name == "python_analysis" and "python_sandbox" in ctx.selected_packs:
        return True
    if gov.channel != "user_selected":
        return False
    if gov.pack == "long_task":
        return "long_task" in ctx.selected_packs or ctx.has_active_goal or (
            gov.compound_enabled and ctx.is_compound
        )
    if gov.pack == "workflow_pack":
        return "workflow_pack" in ctx.selected_packs or ctx.task_intent in gov.default_intents
    if gov.pack == "semantic_govern":
        return (
            "semantic_govern" in ctx.selected_packs
            or ctx.task_intent in gov.default_intents
            or ctx.has_semantic_draft
        )
    if gov.pack == "user_extension":
        wf_id = getattr(entry, "workflow_id", None)
        mcp_id = getattr(entry, "mcp_server_id", None)
        return (wf_id is not None and wf_id in ctx.selected_workflow_ids) or (
            mcp_id is not None and mcp_id in ctx.selected_mcp_server_ids
        )
    return False


def _matches_intent_or_domain(entry: RegisteredTool, ctx: AssemblyInput) -> str | None:
    """返回命中的通道名 ('intent_routed' | 'domain_routed')，未命中返回 None。"""
    gov = entry.governance
    if entry.name in ctx.skill_recommended:
        return "intent_routed"
    if entry.name == "todo_write" and (ctx.requires_plan or ctx.has_active_plan):
        return "intent_routed"

    intent_hit = (ctx.task_intent in gov.default_intents) or (
        gov.compound_enabled and ctx.is_compound
    )
    domain_hit = bool(gov.domains and (gov.domains & ctx.domains))

    if gov.channel == "domain_routed":
        if not domain_hit:
            return None
        if gov.default_intents and not intent_hit:
            return None
        return "domain_routed"

    if gov.channel == "intent_routed":
        if gov.domains and not domain_hit:
            return None
        return "intent_routed" if intent_hit else None

    return None
```

### 19 个静态工具治理元数据变更汇总

| 工具 | 变更前 (`L27`) | 变更后 (`Feature 968`) |
| --- | --- | --- |
| `final_answer`, `read_tool_result`, `database_query`, `search_semantics`, `get_metric_definition`, `resolve_semantics`, `query_metric`, `search_documents`, `read_document`, `schedule_read`, `schedule_write` | `channel="always_on"` | 保持 `channel="always_on"`（11 个核心底座） |
| `todo_write` | `channel="scene_routed"` + `L1_LIGHT_SCENES` 反选 | `channel="intent_routed"`, `default_intents={"analysis", "diagnosis", "dashboard", "report", "automation"}`, `compound_enabled=True` |
| `python_analysis` | `channel="scene_routed"`, 25 个 `SC-*` | `channel="intent_routed"`, `default_intents={"analysis", "diagnosis", "dashboard", "report"}` |
| `sub_agent` | `channel="scene_routed"`, 12 个 `SC-*` | `channel="intent_routed"`, `default_intents={"diagnosis", "report"}`, `compound_enabled=True` |
| `goal` | `pack="long_task"` + 硬编码 `SC-40` | `channel="user_selected"`, `pack="long_task"`, `compound_enabled=True` |
| `create_workflow` | `pack="workflow_pack"` + `AUTOMATION_SCENES` | `channel="user_selected"`, `pack="workflow_pack"`, `default_intents={"automation"}` |
| `discover_semantic_candidates`, `review_semantic_change`, `submit_semantic_change` | `pack="semantic_govern"` + `ADMIN_CONFIG_SCENES` | `channel="user_selected"`, `pack="semantic_govern"`, `default_intents={"handoff"}`, `requires_semantic_maintainer=True` |

---

## 4. Context Runtime 三层装配与 PromptOps 校验

### 4.1 Skill 装配与三层提示词组合（`context_runtime/profiles.py` & `pipeline.py`）

> 评审关注点：确保全部 6 个作业意图均精确命中唯一任务模板，所有 7 个业务域均触发域级口径指引注入；支持未来新增域级 Skill 零改动接入。

```python
# 设计示意：apps/agent/src/compass_agent/context_runtime/profiles.py

WORK_INTENTS: tuple[WorkIntentId, ...] = (
    "qa",
    "analysis",
    "diagnosis",
    "dashboard",
    "report",
    "automation",
)
POLICY_VERSION = "context-v2"


@dataclass(frozen=True, slots=True)
class SkillSpec:
    key: str
    path: str
    tools: frozenset[str]
    phases: tuple[Phase, ...]
    intents: tuple[WorkIntentId, ...]
    domains: frozenset[DomainId] = frozenset()   # 空集=全域通用；非空=仅命中业务域时加载
    compound_also: bool = False                   # True 表示 is_compound 时无视 intents 限制


SKILLS: tuple[SkillSpec, ...] = (
    SkillSpec(
        "metric-check",
        "shared/skills/metric-check.md",
        frozenset({"database_query"}),
        ("plan", "query", "analyze"),
        ("qa", "analysis", "diagnosis", "dashboard", "report"),
    ),
    SkillSpec(
        "agentic-search",
        "shared/skills/agentic-search.md",
        frozenset(),
        ("plan", "query", "analyze"),
        ("diagnosis", "report"),
        compound_also=True,
    ),
    SkillSpec(
        "report-outline",
        "shared/skills/report-outline.md",
        frozenset(),
        ("plan", "generate"),
        ("report",),
    ),
)
```

```python
# 设计示意：apps/agent/src/compass_agent/context_runtime/pipeline.py（三层系统提示词拼装段）

sections = [render(f.path, f.body, variables) for f in files]
if ctx.phase != "identify" and ctx.domains:
    (domain_file,) = exact(tx, binding, ["support/routing/domains.json"])
    domain_doc = json.loads(domain_file.body)["domains"]
    _, _, fixed_domains = catalogs()
    blocks = []
    for domain_id in ctx.domains:
        guidance = (domain_doc.get(domain_id) or {}).get("guidance", "").strip()
        if guidance:
            blocks.append(f"**业务域口径指引：{fixed_domains[domain_id]['name']}**\n\n{guidance}")
    if blocks:
        sections.extend(blocks)
        receipts.append(domain_file.receipt())
prompt = "\n\n".join(sections)
```

### 4.2 PromptOps 结构化校验（`promptops/structured.py`）

```python
# 设计示意：apps/agent/src/compass_agent/promptops/structured.py（替换 scenes.json 分支）

elif path == "support/routing/tasks.json":
    from compass_agent.routing.dictionary import catalogs

    tasks = document.get("tasks")
    _, expected_tasks, _ = catalogs()
    if set(document) != {"tasks"} or not isinstance(tasks, dict) or tasks.keys() != expected_tasks.keys():
        raise PromptError("invalid_routing_asset", "须保留代码登记的全部 10 个任务意图 ID。")
    for key, row in tasks.items():
        if not isinstance(row, dict):
            raise PromptError("invalid_routing_asset", f"{key} 须为对象。")
        fixed = {k: v for k, v in row.items() if k not in {"description", "examples"}}
        examples = row.get("examples")
        if (
            fixed != expected_tasks[key]
            or not isinstance(row.get("description"), str)
            or not 0 < len(row["description"].strip()) <= 1000
            or not isinstance(examples, list)
            or len(examples) > 10
            or not all(isinstance(v, str) and 0 < len(v) <= 500 for v in examples)
        ):
            raise PromptError("invalid_routing_asset", f"{key} 仅允许编辑 description 与 examples。")

elif path == "support/routing/domains.json":
    from compass_agent.routing.dictionary import catalogs

    domains = document.get("domains")
    _, _, expected_domains = catalogs()
    if (
        set(document) != {"domains"}
        or not isinstance(domains, dict)
        or domains.keys() != expected_domains.keys()
    ):
        raise PromptError("invalid_routing_asset", "须保留代码登记的全部 7 个业务域 ID。")
    for key, row in domains.items():
        if not isinstance(row, dict):
            raise PromptError("invalid_routing_asset", f"{key} 须为对象。")
        fixed = {k: v for k, v in row.items() if k not in {"description", "examples", "guidance"}}
        examples = row.get("examples")
        guidance = row.get("guidance")
        if (
            fixed != expected_domains[key]
            or not isinstance(row.get("description"), str)
            or not 0 < len(row["description"].strip()) <= 1000
            or not isinstance(guidance, str)
            or not 0 < len(guidance.strip()) <= 1500
            or not isinstance(examples, list)
            or len(examples) > 10
            or not all(isinstance(v, str) and 0 < len(v) <= 500 for v in examples)
        ):
            raise PromptError(
                "invalid_routing_asset", f"{key} 仅允许编辑 description、examples 与 guidance。"
            )
```

---

## 5. Web 语义同域加权与工作区先验透传（`apps/web/`）

> 评审关注点：
> 1. 同域加权绝不屏蔽用户显式点名的跨业务域指标（`requested`、`intent.metric` 优先占位）。
> 2. 工作区 `defaultDomains` 仅随 `runEnvironment` 只读透传，不绕过任何鉴权检查。

```typescript
// 设计示意：apps/web/src/server/modules/semantic/runtime.ts（operation === "context"）

if (operation === "context") {
  const maxItems = 8;
  const { question, domains } = z
    .object({
      question: z.string().max(2400).optional(),
      domains: z.array(z.string().min(1).max(32)).max(6).default([]),
    })
    .parse(raw ?? {});

  const domainSet = new Set(domains);
  const domainBoost = (v: MetricView) => (domainSet.has(v.domain) ? 1 : 0);

  // 1. 精确点名与正式解析仍在全量授权目录内执行，确保跨业务域提问不被过滤
  const intent = question
    ? await resolveMetric(c, { mention: question, dimensions: [] }, all, manifest_hash)
    : undefined;
  const normalizedQuestion = normalize(question ?? "");
  const lexical =
    question && !intent?.mention ? relatedMetrics(all, question) : { suggestions: [] };

  // ... requested 显式多指标解析保持原有逻辑 ...

  // 2. 模糊与词法候选：匹配强度 > 同域加权 > 词法得分 > 稳定 ID
  const matched = question
    ? all
        .filter((v) => rank(v, question) >= 3)
        .sort(
          (a, b) =>
            strength(b) - strength(a) ||
            domainBoost(b) - domainBoost(a) ||
            rank(b, question) - rank(a, question) ||
            a.asset_id.localeCompare(b.asset_id),
        )
        .map((v) => v.asset_id)
    : [];

  // 3. 当识别出业务域且首屏名额未满时，按同域补齐授权已发布指标（替代无域盲填）
  const domainDefaults =
    domainSet.size > 0
      ? all
          .filter((v) => v.kind === "metric" && domainSet.has(v.domain))
          .sort((a, b) => a.code.localeCompare(b.code))
          .map((v) => v.asset_id)
      : [];

  const items = initialItems(
    all,
    [
      ...requested.keys(),
      ...(intent?.metric ? [intent.metric.asset_id] : []),
      ...(intent?.options ?? []).map((v) => v.asset_id),
      ...matched,
    ],
    [...lexical.suggestions.map((v) => v.asset_id), ...domainDefaults],
    question ?? "",
    maxItems,
  );
  // 返回结构保持不变
}
```

```typescript
// 设计示意：apps/web/src/server/composition.ts（runEnvironment 增加 workspaceDefaultDomains）

return {
  ...(tenant
    ? {
        runEnvironment: {
          tenantName: tenant.name,
          workspaceName: workspace.name,
          timezone: tenant.timezone,
          workspaceDefaultDomains: workspace.defaultDomains,
        },
      }
    : {}),
  // details / revision / capabilityGrants 保持不变
};
```

---

## 6. 双库迁移脚本设计（`agent/main 0021` & `web/main 0011`）

### 6.1 `agent/main` Alembic 迁移（拟新增 `apps/agent/migrations/versions/0021_intent_taxonomy_refactor.py`）

> 评审关注点：
> 1. 清除 `prompt_generation` 自引用外键后再删除，避免 PostgreSQL 外键约束报错。
> 2. 重置 `agent_runtime_control.policy_sha256` 使新版 `POLICY_DIGEST` 在首个新消费者启动时登记。
> 3. `context_decision_summary` 与 `context_badcase` 使用显式 `DEFAULT` 完成非空列添加再撤销默认值，保证空库与已有未上线开发库均可确定性升级。

```python
# 设计示意：apps/agent/migrations/versions/0021_intent_taxonomy_refactor.py

"""Refactor intent taxonomy to orthogonal task_intent + domains + is_compound (968)."""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DROP INDEX IF EXISTS idx_ctx_summary_policy_scene;

        ALTER TABLE context_decision_summary
            ADD COLUMN task_intent VARCHAR(32) NOT NULL DEFAULT 'unclassified',
            ADD COLUMN primary_domain VARCHAR(32),
            ADD COLUMN domains_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN domain_source VARCHAR(24) NOT NULL DEFAULT 'none',
            ADD COLUMN is_compound BOOLEAN NOT NULL DEFAULT FALSE;

        ALTER TABLE context_decision_summary
            ALTER COLUMN task_intent DROP DEFAULT,
            ALTER COLUMN domains_json DROP DEFAULT,
            ALTER COLUMN domain_source DROP DEFAULT,
            ALTER COLUMN is_compound DROP DEFAULT,
            DROP COLUMN scene_id,
            DROP COLUMN task,
            ADD CONSTRAINT ck_ctx_summary_domain_source
                CHECK (domain_source IN ('classifier', 'workspace_prior', 'none'));

        CREATE INDEX idx_ctx_summary_policy_intent
            ON context_decision_summary (tenant_id, task_intent, policy_digest, created_at);

        CREATE INDEX idx_ctx_summary_domain
            ON context_decision_summary (tenant_id, primary_domain, created_at);

        ALTER TABLE context_badcase
            ADD COLUMN task_intent VARCHAR(32) NOT NULL DEFAULT 'unclassified',
            ADD COLUMN primary_domain VARCHAR(32),
            ADD COLUMN domains_json JSONB NOT NULL DEFAULT '[]'::jsonb;

        ALTER TABLE context_badcase
            ALTER COLUMN task_intent DROP DEFAULT,
            ALTER COLUMN domains_json DROP DEFAULT,
            DROP COLUMN scene_id;

        -- 预上线零历史债：清理过时的 PromptOps 旧场景资产，升级后导入并发布首个新版代次
        UPDATE prompt_head SET generation = NULL, last_snapshot_no = 0 WHERE id = 1;
        UPDATE prompt_generation SET parent_id = NULL, restored_from_id = NULL;
        DELETE FROM prompt_generation;
        DELETE FROM prompt_version;
        DELETE FROM prompt_file;
        DELETE FROM prompt_command;

        -- 清空旧执行策略指纹，由新版消费者重新登记 context-v2 指纹
        UPDATE agent_runtime_control SET policy_sha256 = NULL WHERE key = 'execution';
    """)


def downgrade() -> None:
    raise RuntimeError("Restore the verified 0020 backup to a separate database.")
```

### 6.2 `web/main` 原生 SQL 迁移（拟新增 `apps/web/db/main/migrations/0011_workspace_default_domains.sql`）

```sql
-- 设计示意：apps/web/db/main/migrations/0011_workspace_default_domains.sql
-- Owner web/main. Add workspace-level default business domain prior (Feature 968).
-- Up Migration
ALTER TABLE web.workspace
  ADD COLUMN default_domains text[] NOT NULL DEFAULT '{}',
  ADD CONSTRAINT ck_workspace_default_domains CHECK (
    cardinality(default_domains) <= 2
    AND (
      cardinality(default_domains) = 0
      OR (
        array_ndims(default_domains) = 1
        AND array_position(default_domains, NULL) IS NULL
      )
    )
    AND default_domains <@ ARRAY[
      'sales_channel',
      'marketing_member',
      'product_supply',
      'trade_fulfillment',
      'store_region',
      'finance_cost',
      'platform_data'
    ]::text[]
  );

-- Down Migration
DO $$ BEGIN RAISE EXCEPTION 'Restore the verified web/main 0010 backup to recover the previous version'; END $$;
```
