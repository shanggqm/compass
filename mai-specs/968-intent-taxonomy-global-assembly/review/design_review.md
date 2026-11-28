# 968 意图分类体系极简重构与全局动态装配 — 技术方案与代码设计评审报告

> 评审对象：[TECH_DESIGN.md](../design/TECH_DESIGN.md) · [CODE_DESIGN.md](../design/CODE_DESIGN.md)（[返回 BRIEF](../BRIEF.md)）
> 评审日期：2026-11-27 | 代码核验基线：`main @ 5180841`（含工作区在研的 L27 工具动态组装变更）
> 评审依据：[BRIEF.md](../BRIEF.md)、[TECH_DESIGN.md §需求依据与验收](../design/TECH_DESIGN.md#需求依据与验收)、[AGENTS.md](../../../AGENTS.md)、[数据所有权与 SQL 迁移管理规范](../../../docs/database-migrations.md)

---

## 1. 总体结论

**评审结论：需要修改（存在 4 项 P0 阻断缺陷与 5 项 P1 重要问题，修订后再进入实施与复评）。**

### 核心判断依据

- **方向成立**：将 40 场景笛卡尔积与 `scene_id` / `deliverable` 双轨冲突收敛为正交的 `task_intent`（10 项）+ `domains`（7 项，与 `apps/web/src/features/semantic/directory.tsx:16-27` 既有 11 个语义域码实现无重叠全覆盖映射）+ `is_compound`，并用 Web `web.workspace.default_domains` 作为只读弱先验、语义 `/context` 采用同域加权且保留全量跨域检索、保持 `agent/main` 与 `web/main` 双库边界独立，整体架构简洁且直击当前真实痛点。
- **当前阻断实施的 4 个硬缺陷**：
  1. **边界守卫意图链路断裂（P0-1）**：4 个边界意图在 `harness/executor.py`、`runtime/stream_answer_turn.py`、`context_runtime/pipeline.py`、`context_runtime/profiles.py` 中存在四重断层，按当前方案落地会导致守卫提示词被 `answer/plain.md` 静默覆盖、边界意图缺 `tasks/*.md` 报 503，或绕过 `call_model()` 导致 ContextOps 中 40% 意图类目永久丢失为 `unclassified`。
  2. **删除 `support/routing/business.md` 会击穿现有强依赖调用链（P0-2）**：`model_step.py:266` 与 `routing/prompts.py:7`（由 `routing/service.py:349` 每次路由调用）均强依赖 `support/routing/business.md`，删键后全部 6 个业务作业意图会直接报 `503`。
  3. **代码设计存在确定性运行时崩溃（P0-3）**：`CODE_DESIGN.md:291` 读取 `DecisionAnswer.probability`，但 `schemas.py:1258-1265` 的实际字段为 `noul`；`CODE_DESIGN.md:246` 无条件读取 `shared/stages/identify.md`，会击穿 `routing/service.py:358-367` 的非受管固定列表。
  4. **工具通道与观测契约同步遗漏导致 500 崩溃（P0-4）**：`schemas.py:2298,2681,2687` 与前端 `promptops/editor.tsx:132`、`contextops/flow.tsx:684` 仍硬编码 `scene_routed` / `scene`，且 `0021` 迁移保留的旧观测行 `decisions_json.toolAssembly` 含 `scene_routed`、升级后回读将触发 Pydantic 500。

### 评审覆盖与未核验边界

- **本次重点核验**：Agent 路由与分类器（`routing/`）、Context Runtime 与工具动态装配（`context_runtime/`、`tools/`、`runtime/`、`harness/`）、PromptOps 资产与校验（`promptops/`）、ContextOps 存储/聚合/可观测（`context_ops/`、`observability/`）、公开契约（`schemas.py`）、Web 语义检索与工作区访问（`apps/web/src/server/`、`features/`）、双库迁移与 SQL 工具链（`migrations/`、`scripts/database.py`、`apps/web/scripts/db-common.mjs`）。
- **未核验项**：离线 HTML 阅读版（`TECH_DESIGN.html` / `CODE_DESIGN.html`）未做视觉核验；未在真实 PostgreSQL 17 或 JEV/Flash 上游模型执行运行期实测（待修改方案后于实施及测试阶段验证）。

---

## 2. 需求、设计与验证对照摘要

| AC 编号 | 对应方案章节 | 评审核验状态 | 主要缺口 |
| --- | --- | --- | --- |
| **AC-01** 正交分类体系 | §2.1.1、CODE §1 | 基本闭合（局部缺陷） | 10 意图 + 7 域映射与 Web 11 语义域已核实一致；`build_intent` 对先验域缺去重。 |
| **AC-02** 两级分类纯粹化 | §2.3.1、CODE §2 | **阻断（P0-3、P1-2）** | `DecisionAnswer.probability` 字段名错误；`flash_input` 缺失；受控模式 `flash_only` 强制分支未核对。 |
| **AC-03** 工作区先验与跨域自由 | §2.3.1、§2.3.5、CODE §5 | **重要缺陷（P1-1、P1-2）** | 把 `workspaceDefaultDomains` 传进分类器会污染 `domain_source`；双先验域误触发 L3/Pro 升档；缺 `RunFacts` 与 `durable_service.py` 校验设计。 |
| **AC-04** 全意图 100% 触发真实机制 | §2.1.1 A、§2.3.3–2.3.4 | **阻断（P0-1、P0-2）** | Direct Stream 覆写守卫提示词且缺 512 token 限制；`boundary` 无 `tasks/*.md`；删 `business.md` 击穿 `model_step.py`/`routing/prompts.py`；`handoff` 普通用户可能暴露 `schedule_write`。 |
| **AC-05** 业务域全局装配生效 | §2.3.3–2.3.5、CODE §3–5 | **重要缺陷（P1-3、P2-1）** | 19 个静态工具与 3 个 Skill 均无域专属项，AC-05 第 ③ 项 `domain_routed` 与域级 Skill 当前空转；语义词法候选未做同域加权。 |
| **AC-06** 复合复杂度闭环 | §2.3.1、§2.3.4、CODE §1/3 | **重要缺陷（P1-1）** | `features_for`、`requires_plan` 与 `assembler.py` 对 `>=2` 域的判断不一致，且未排除 `workspace_prior` 双域误升档。 |
| **AC-07** 动态工具装配去硬编码 | §2.3.4、CODE §3 | **阻断（P0-4、P1-5）** | `schemas.py` 与前端两处遗漏 `LoadingChannel` 枚举；`CODE_DESIGN.md §3` 缺 `assemble_step` 与 `automation` 4 个 `workflow_*` 兜底。 |
| **AC-08** PromptOps 资产与校验 | §2.3.2、CODE §4.2 | **阻断（P0-2、P1-5）** | 遗漏 `starter_catalog.json` 校验同步、`legacy_layout.json` / `catalog.py` 删键同步与 `examples` 空白校验。 |
| **AC-09** 契约、前端与 ContextOps | §2.1.3、§2.3.6 | **阻断（P0-1、P0-4、P1-5）** | 缺 ContextOps 聚合设计（`domain` 筛选与计数口径、SQLite 兼容）、缺 `unclassified` 类型边界、Web 工作区 API 路径写错。 |
| **AC-10** 可观测字段一致 | §2.3.6 | **重要缺陷（P0-1、P1-5）** | 边界意图若绕过 `call_model` 会丢失 `context.assemble` 观测；`observability/privacy.py` 的 `EXCLUSION_REASONS` 与事件白名单未同步说明。 |
| **AC-11** 双库迁移与合规 | §2.1.1 D、§4.2、CODE §6 | **阻断（P0-4、P1-4）** | `/readyz` 未核对 `prompt_head.generation IS NOT NULL`（与 R3 描述不符）；T5 遗漏 `apps/agent/scripts/database.py` 与 `apps/web/scripts/db-common.mjs`；`web.workspace` CHECK 缺元素去重。 |
| **AC-12** 端到端验证 | §5 | 待前置项修复后核对 | 需补齐 `delivery.py`、`search_verification.py`、`stream_answer_turn.py`、`executor.py` 等遗漏文件的测试与业务测试计划。 |

---

## 3. 关键发现（按优先级排序）

### P0（阻断）

#### [P0-1] 边界守卫意图（`clarify` / `restricted` / `off_topic` / `handoff`）在执行路由、提示词加载与 ContextOps 观测间存在四重断层

- **证据**：
  - 方案承诺：`TECH_DESIGN.md §2.1.1 A`（L131–L134）与 `§2.1.2`（L211–L214）声明 `clarify`/`restricted`/`off_topic` 走 Direct Stream 旁路（0 工具，加载 `routing/{clarify,restricted,off-topic}`，`off_topic` 上限 512 tokens），`handoff` 走治理/转交分支（加载 `routing/handoff`，维护者解锁 `semantic_govern`，普通用户 0 写工具）。
  - 真实代码：
    1. `apps/agent/src/compass_agent/harness/executor.py:278-288` 仅在 `route.deliverable in ("business_qa", "data_analysis", "dashboard", "business_report")` 时进入 `call_model()`；方案 `T4` 完全未列出 `harness/executor.py`。
    2. `apps/agent/src/compass_agent/context_runtime/classifier.py:130-131` 在调用分类模型前生成 `identify` 回执（`scene=None, task=None`），落库为 `unclassified`；`context_ops/analytics.py:248` 取最后一个非 `unclassified` 摘要行作为 Run 的意图归属。若边界意图不走 `call_model()`，这 4 个意图将永远不会写入带 `task_intent` 的摘要行，在 ContextOps 全部退化为 `unclassified`，且 `handoff` 维护者根本无法进入 `tool_assembly.py` 解锁 `semantic_govern`。
    3. 当前 `runtime/store.py:492` 默认启用 `answerProtocol = 3`；`runtime/stream_answer_turn.py:44-48, 78, 84, 106`（`T4` 遗漏此文件）在 Direct Stream 时设置 `turn["purpose"]="answer_stream"`、`mode="plain"`、`max_tokens=min(8192, ...)`；经 `model_step.py:367-369` 传入 `pipeline.py:279-280` 后，执行 `if answer_mode: paths = ["answer/plain.md"]`，**直接丢弃所有守卫提示词**（`routing/{clarify,restricted,off-topic}` 永远不被加载），且 `off_topic` 的 512 token 限制落空（原 512 限制仅在 `routing/service.py:418`，`RunService` 并不调用该方法）。
    4. `TECH_DESIGN.md §2.3.3`（L347, L354）让 `ContextRequest.task_intent` 接收全部 10 项并加载 `tasks/{task_intent}.md`，但 `prompts/tasks/` 仅建 6 份 `WorkIntentId` 模板；若 `handoff` 或用户手选工具包后的 `clarify` 进入 `call_model()`，`resolve_profile` 拼接 `tasks/handoff.md` 将触发 `PromptError("missing_prompt", 503)`。
    5. `tools/builtin/schedule_write/__init__.py:237` 保持 `channel="always_on"`（`CODE_DESIGN.md:477` 未改），其属于外部写能力；若普通用户命中 `handoff` 后进入工具主循环，拥有排期权限的普通用户仍会暴露 `schedule_write`，与“普通用户 0 写工具”矛盾。
- **触发条件**：任一命中 `clarify` / `restricted` / `off_topic` / `handoff` 的受管 Run（`answerProtocol == 3`）。
- **影响**：安全拒答/越权/闲聊守卫提示词失效、运行报 `503 missing_prompt`、ContextOps 丢失全部 4 类边界意图统计。
- **可执行动作**：
  1. 将 `harness/executor.py`、`runtime/stream_answer_turn.py`、`context_runtime/tool_assembly.py` 纳入 `T4` 并在 `CODE_DESIGN.md` 补齐核心分支。
  2. 明确 **10 个意图均通过受管 `call_model()` 执行**，统一完成 PEP 安全检查、预算裁剪、PromptOps 审计与 ContextOps `task_intent` 落库。
  3. 在 `resolve_profile()` 与 `pipeline.py` 中按 `group` 分流：`group == "work"` 加载 `tasks/{task_intent}.md`；`group == "boundary"` 不加载 `tasks/*.md`，改为加载 `support/routing/{clarify,handoff,restricted,off-topic}.md`；Direct Stream 边界意图走独立 `answer_mode`（或在 `answer_mode` 中保留对应守卫提示词，不退化为泛用 `answer/plain.md`）。
  4. `stream_answer_turn.py` 复用 `assembler.should_direct_stream()` 作为唯一判据，并为 `off_topic` 强制 `max_tokens = min(512, ...)`；对 `handoff` 且非语义维护者（且无手选工具包/活跃 Goal/语义草稿）直接纳入 Direct Stream（0 工具），天然满足“普通用户 0 写工具”。

---

#### [P0-2] 计划移除 `support/routing/business.md`，与 `model_step.py`、`routing/prompts.py` 强依赖冲突

- **证据**：
  - `TECH_DESIGN.md §2.3.2`（L329）要求从 `prompts/support/routing/instructions.json` 移除 `business` 键。
  - `apps/agent/src/compass_agent/context_runtime/model_step.py:266` 在所有非子代理受管模型步均将 `support/routing/business.md` 加入 `support_paths`，经 `exact(tx, binding, paths)` 校验，缺失即抛 `PromptError("missing_prompt", 503)`。
  - `apps/agent/src/compass_agent/routing/prompts.py:6-16` 映射 `_MODE_FILES["answer"] = "business"`，由 `routing/service.py:349-352` 在每次 `select_route()` 中无条件调用。
  - `apps/agent/tests/test_prompt_format.py:85,154` 亦引用该路径。
- **触发条件**：任意业务作业意图（`qa`/`analysis`/`diagnosis`/`dashboard`/`report`/`automation`，均满足 `response_mode == "answer"`）进入 `select_route()` 或 `call_model()`。
- **影响**：全部 6 类核心业务查询 100% 报 503 失败。
- **可执行动作**：在方案 `§2.3.1` / `§2.3.3` 与 `CODE_DESIGN.md` 中明确同步处理——`model_step.py:266` 移除 `support/routing/business.md`（由 `shared/fragments/base.md` + `tasks/{intent}.md` 接管），同时修改 `routing/prompts.py` 与 `routing/service.py` 使 `work` 意图在 `route_prompt()` 中不再读取 `support/routing/business.md`（例如仅边界意图调用 `route_prompt`，非受管 `RoutingService.run()` 使用 `shared/fragments/base.md`），并同步清理 `promptops/catalog.py:111` 与 `legacy_layout.json:151`。

---

#### [P0-3] `CODE_DESIGN.md §2` 存在两处确定性崩溃（`DecisionAnswer.probability` 不存在与非受管 Prompt 缺失）

- **证据**：
  1. `CODE_DESIGN.md:288-292` 在 `from_jev()` 中读取 `compound_answer.probability`：
     ```python
     compound_answer and compound_answer.type == "noul" and (compound_answer.probability or 0.0) >= 0.6
     ```
     核查 `apps/agent/src/compass_agent/schemas.py:1258-1265`：`DecisionAnswer` 定义为 `noul: float | None = Field(default=None, ge=0, le=1)`，**不存在 `probability` 字段**。
  2. `CODE_DESIGN.md:246` 在 `decision_input()` 中去掉原 `if managed else ""` 条件，无条件调用 `prompt_text("shared/stages/identify.md")`。核查 `apps/agent/src/compass_agent/routing/service.py:358-367`：`_pin_direct_prompts()` 仅固定 `support/routing/` 与 `shared/fragments/output-*`，未包含 `shared/stages/identify.md`。
- **触发条件**：
  1. 任意走 JEV 路径返回 `is_compound` 答案的分类请求。
  2. 任意通过 `RoutingService.run()`（非受管直连路由）发起的分类请求。
- **影响**：前者每次 JEV 调用必抛 `AttributeError` 导致分类崩溃；后者在受管 Prompt 开启时必抛 `503 prompt_version_unavailable`。
- **可执行动作**：
  1. 将 `CODE_DESIGN.md:291` 修正为 `compound_answer.noul`，并把阈值 `0.6` 收拢到 `routing/config.py` 的 `POLICY` 常量。
  2. 将 `shared/stages/identify.md` 加入 `routing/service.py:_pin_direct_prompts()` 的固定路径集合，或明确 `RoutingService.run()` 的提示词固定边界；同时在 `CODE_DESIGN.md §2` 补全 `flash_input()` 设计示意。

---

#### [P0-4] `LoadingChannel` / `ToolExcludedItemView` 公开契约遗漏同步，且 `0021` 迁移遗留不兼容的 `decisions_json` 与 `unclassified` 类型冲突

- **证据**：
  1. `apps/agent/src/compass_agent/schemas.py` 中仍有三处硬编码旧枚举（方案 `§2.1.3` 与 `T7` 均未提及）：
     - `ToolGovernanceView.channel`（L2298）：`Literal["always_on", "scene_routed", "user_selected"]`
     - `ToolAssemblyItemView.channel`（L2681）：`Literal["always_on", "scene_routed", "user_selected"]`
     - `ToolExcludedItemView.reason`（L2687）：`Literal["scene", "stage", "role", "budget"]`
  2. Web 前端两处硬编码映射同样被 `T7` 遗漏：`apps/web/src/features/promptops/editor.tsx:132` 与 `apps/web/src/features/contextops/flow.tsx:684`（均硬编码 `scene_routed: "按场景"`）。
  3. `CODE_DESIGN.md §6.1`（L740–L771）在 `0021` 迁移中保留了既有 `context_decision_summary` 和 `context_badcase` 行（并将 `task_intent` 填为 `'unclassified'`），但：① 未处理 `decisions_json->'toolAssembly'` 里已持久化的 `"channel": "scene_routed"` / `"reason": "scene"`；② `CODE_DESIGN.md §1.1` 的 `TaskIntentId` 不含 `'unclassified'`，而新建库在 `identify` 阶段（分类尚未完成时）也必须落盘 `task_intent`（列约束为 `NOT NULL`）。
- **触发条件**：
  - 打开 PromptOps 查看任意 `intent_routed` 工具详情；
  - 打开 ContextOps 查看任意新 Run 或 `0020 → 0021` 升级库中的既有 Run 轨迹；
  - 读取 `identify` 阶段失败的 Run 或基于 `identify` 步骤创建的 Badcase。
- **影响**：FastAPI 响应校验直接报 `500 ResponseValidationError`，PromptOps 编辑器和 ContextOps 轨迹页不可用。
- **可执行动作**：
  1. 同步更新 `schemas.py:2298,2681,2687`、`apps/web/src/features/promptops/editor.tsx:132` 与 `apps/web/src/features/contextops/flow.tsx:684`，并纳入 `T7` 文件清单。
  2. 明确 `ContextRunView` / `ContextBadcaseView` / `ContextIntentRecord` 的 `task_intent` 字段类型为 `TaskIntentId | Literal["unclassified"]`（或仅在 `identify` 摘要行用 `'unclassified'` 并在视图层显式处理，保持 `recorder.py` 与 `analytics.py` 语义闭环）。
  3. 鉴于本需求明确“预上线零历史债、不保留旧观测语义”，在 `0021` 迁移中二选一落地：① 直接清空预上线旧观测表（`context_badcase`、`context_decision_summary`、`context_snapshot`、`context_security_event`，与同步清空 `prompt_*` 一致，避免孤儿 `prompt_generation` 与旧 JSONB 残留）；② 若需保留旧观测行以配合通用升级测试，则在 `0021` 中将 `decisions_json` 内的 `toolAssembly` 键清理或规范化为新枚举值。

---

### P1（重要）

#### [P1-1] 复合任务升档规则在 AC 与代码间自相矛盾，且双默认先验域会误升档简单 `qa`

- **证据**：
  - `TECH_DESIGN.md AC-06`（L30）要求：`is_compound=true`（或命中 `>= 2` 个业务域）时，同时触发 ① L1 升档 Pro、② `requires_plan=true`、③ 解锁 `todo_write`、`sub_agent` 与 `goal`。
  - `CODE_DESIGN.md:175` 对 `effective_difficulty` 判断了 `intent.is_compound or len(intent.domains) >= 2`，但同函数 L191（`requires_plan`）和 `CODE_DESIGN.md:426,454`（`assembler.py` 解锁 `goal`/`todo_write`/`sub_agent`）**只判断了 `is_compound`，漏掉 `len(domains) >= 2`**。
  - 更隐蔽的缺陷：若工作区配置了 2 个 `default_domains`（AC-03 允许至多 2 个），用户问一句简单概念问答（如“什么是 GMV？”），分类器返回 `task_intent="qa", domains=[], is_compound=False`，经 `build_intent()` 回退先验后 `len(intent.domains) == 2`（`domain_source="workspace_prior"`），L175 会将其误升档为 **L3 / Pro**！
- **触发条件**：工作区配置 2 个默认业务域，且用户发起未显式指明业务域的 `qa` 问答；或用户发起跨 2 域但分类器未标 `is_compound=true` 的分析问题。
- **影响**：简单问答成本与延迟暴增（强走 Pro），且 AC-06 三项联动行为割裂。
- **可执行动作**：在 `RoutingFeatures` 中新增或收敛单一派生布尔规则：`effective_compound = (group == "work") and (intent.is_compound or (intent.domain_source == "classifier" and len(intent.domains) >= 2))`，由它统一驱动 `effective_difficulty`、`requires_plan` 与 `AssemblyInput.is_compound`（明确排除 `workspace_prior` 双域导致的误升档）。

---

#### [P1-2] 工作区先验注入分类器会污染 `domain_source` 归因，且先验透传链路缺少关键设计

- **证据**：
  - `TECH_DESIGN.md §2.3.1`（L304）将 `workspaceDefaultDomains` 放入 JEV/Flash 的 `state["runOrganization"]`；但 `§3 R2`（L452）又期望“先验仅在分类器返回 `domains == []` 时生效”。若分类器提前看到工作区默认域，面对泛化问题会直接输出该域并标为 `domain_source="classifier"`，同时放大跨业务域误导偏置，令 ContextOps 的“先验命中占比”失真。
  - `apps/agent/src/compass_agent/context_runtime/run_facts.py:15-18, 73-84` 当前无 `workspace_default_domains` 字段；`runtime/durable_service.py:90-96` 写死仅保留 `("tenantName", "workspaceName", "timezone")` 三个键。`CODE_DESIGN.md` 未覆盖这两处变更。
  - `context_runtime/classifier.py:37-45, 82-84, 94` 仍硬编码 `scenes.json`、`deliverable.md`、`classify-flash-managed.md`、`managed=True` 及 `request.questions["scene"]`，`routing/service.py:187` 仍预取 `scenes.json`，需在设计中显式同步。
  - `CODE_DESIGN.md:152` 对 `workspace_default_domains` 未去重；`CODE_DESIGN.md:279-286` 当 `primary_domain` 低于阈值（返回 `None`）但 `secondary_domain` 命中时，会把次域隐式提升为主域。
- **触发条件**：工作区配置 `default_domains` 后发起分析 Run，或进入 `call_classifier`。
- **影响**：先验字段在 Agent 入口被静默丢弃、`domain_source` 统计失真、`call_classifier` 报 `KeyError('scene')`。
- **可执行动作**：
  1. **禁止**将 `workspaceDefaultDomains` 塞入 `runOrganization` 传给 JEV/Flash，仅在 `RunFacts` 上保存独立字段 `workspace_default_domains: tuple[DomainId, ...] = ()`，由 `build_intent()` 在分类器返回 `domains == []` 时确定性兜底。
  2. 在 `CODE_DESIGN.md` 补齐 `durable_service.py` 校验、`run_facts.py` 解析、`classifier.py` 路径与 `"task_intent"` 键更新、`build_intent()` 对 `prior` 去重，以及 `from_jev()` 在 `primary` 未命中时跳过 `secondary` 的规则。

---

#### [P1-3] MVP 减法机会：`domain_routed` 工具通道与 `SkillSpec.domains` 当前零真实消费者，与“零空转”原则冲突

- **证据**：
  - 核查 `CODE_DESIGN.md:473-484`（全部 19 个静态工具）与 `CODE_DESIGN.md:518-541`（全部 3 个 Skill）：**没有任何一个工具声明 `channel="domain_routed"` 或非空 `domains`，也没有任何一个 Skill 声明非空 `domains`**。
  - `TECH_DESIGN.md §1.1 #2`（L49）强调“越少越好、先简单后复杂、有必要再新增、杜绝形同虚设的标签或字段”，而 `AC-05`（L29）第 ③ 项仍承诺“驱动 `tools/assembler.py` 的 `domain_routed` 通道与域级 Skill 匹配”，实为为未来扩展预埋的空通道（`§2.3.4` L390 亦直承“支持未来按域挂载的业务工具/插件零侵入接入”）。
- **触发条件**：本次全链路实施与验收。
- **影响**：为 0 个现有消费者增加了一整条 `LoadingChannel` 枚举、四通道分支、DTO 契约及 PromptOps/ContextOps UI 展示分支，且导致 AC-05 第 ③ 项无法通过真实生产工具验收（只能靠构造假测试工具验证）。
- **可执行动作**：
  - **推荐删减/延后**：本期 `LoadingChannel` 保持 3 通道（`always_on`、`intent_routed`、`user_selected`），延后 `domain_routed` 通道、`ToolGovernance.domains` 及 `SkillSpec.domains` 至首个真实域专属工具或插件（如 L28 插件市场）落地时再引入；将 `AC-05` 聚焦为当前 7 域真实生效的两项机制（① 域口径指引注入；② Web 语义 `/context` 同域加权排序与首屏补齐）加 ContextOps 域维度统计。
  - 注：该删减涉及调整已登记的 `AC-05` 范围，需由需求负责人（andy）确认取舍。

---

#### [P1-4] 双库迁移门禁与工具链注册存在事实偏差和文件遗漏

- **证据**：
  1. **R3 缓解措施与真实代码不符**：`TECH_DESIGN.md §3 R3`（L453）声称“`/readyz` 与 smoke 验收核对 `prompt_head.generation IS NOT NULL`”，但核查 `apps/agent/src/compass_agent/main.py:175-202`，`/readyz` 仅校验 `alembic_version == [REVISION]`、表结构 `SELECT ... LIMIT 0` 及 `service.store.ready`，**并未检查 `prompt_head.generation IS NOT NULL`**。按 `§4.2` Step 4“先启动新版服务、核对 `/readyz`、再同步提示词”的顺序执行，在同步完成前 `/readyz` 会返回 200 而受管请求全部报 `503 baseline_unavailable`。
  2. **T5 遗漏两个 SQL 生成与版本校验脚本**：
     - `apps/agent/scripts/database.py:67-69, 488-492, 518-534` 硬编码 `REVISIONS`、`TARGET = "0020"`、Feature 映射、SQL 头注释（L490 默认写死 `"additive schema"`，对 `0021` 破坏性删列与清表不准确）以及升级矩阵（`write_sql()` 会生成 `0001..0020 → 0021` 全区间增量，变更 `TARGET` 时须保留 `0020` 历史交付不丢失）。
     - `apps/web/scripts/db-common.mjs:16, 175` 硬编码迁移清单与版本检查至 `0010_knowledge_library`。
  3. **`0011_workspace_default_domains.sql` CHECK 未防重复元素**：`CODE_DESIGN.md:798-816` 允许 `ARRAY['sales_channel', 'sales_channel']` 通过数据库约束。
- **触发条件**：执行 `pnpm db:sql -F agent`、`pnpm db:sql -F web`、`pnpm db:sql:check`、或按 `§4.2` 执行上线发布。
- **影响**：SQL 校验脚本无法识别 `0021` / `0011`、发布窗口出现假健康真 503、数据库层可写入重复默认域。
- **可执行动作**：
  1. 在 `T5` 显式纳入 `apps/agent/scripts/database.py` 与 `apps/web/scripts/db-common.mjs`（并修正 `0021` 的 SQL 头变更描述）。
  2. 调整 `§4.2` 发布顺序为“停入口与备份 → 迁移 `web/main` → 迁移 `agent/main` → 启动前执行 `python -m compass_agent.promptops.sync --apply` 完成首代发布 → 启动新版服务并核验 `/readyz` 与 `smoke`”；若需要 `/readyz` 兜底，明确将其作为本次小改造补入 `main.py`。
  3. 在 `ck_workspace_default_domains` 增加 `AND (cardinality(default_domains) < 2 OR default_domains[1] <> default_domains[2])`。

---

#### [P1-5] 代码设计附录存在多处核心骨架缺失，且任务清单遗漏多个真实消费方文件

- **证据**：
  1. **ContextOps 聚合与筛选设计缺席**：`TECH_DESIGN.md §2.4`（L443）注明 `CODE_DESIGN.md §6` 包含“ContextOps 聚合”，实际 `CODE_DESIGN.md §6` 仅有迁移脚本。这导致三个影响实现的契约问题悬而未决：
     - `/v1/contextops/runs` 与 `/badcases` 的 `domain` 筛选是仅匹配 `primary_domain`（走新建 B-tree 索引），还是同时匹配次域 `domains_json`（需考虑 PostgreSQL JSONB 与 SQLite 单测方言差异）？
     - `domain_breakdown` 对包含 2 个域的 Run 计 1 次（主域）还是按每个域各计 1 次？
     - `context_ops/recorder.py` 如何从回执读取新字段并区分 `identify` 步与工作步？
  2. **`assemble_step()` 主循环与 `resolve_profile()` 逻辑缺席**：
     - `CODE_DESIGN.md §3` 未给出 `assemble_step()` 重构段，`automation` 自动加载至多 4 个 `workflow_*` 兜底（原 `assembler.py:241-247`）无对应代码设计；`_matches_intent_or_domain`（L448）中 `skill_recommended` 位于 `gov.channel` 判断之前，可能意外放行 `user_selected` 工具包。
     - `CODE_DESIGN.md §4.1` 未给出 `resolve_profile()` 变更段，而现网 `profiles.py:248-251` 对 `answer_protocol >= 2` 存在 `agentic-search` 短路逻辑（无条件挂入 `skills` 而不检查 `intents` / `compound_also` 且不加载对应 md 文件），不清理会使 `CODE_DESIGN.md:532` 的 `compound_also=True` 形同虚设。
  3. **任务表（`T1–T7`）遗漏的真实代码文件**：
     - `apps/agent/src/compass_agent/context_runtime/delivery.py:60,348,374,391,404`（硬编码 `business_report` / `data_analysis` / `dashboard`）；
     - `apps/agent/src/compass_agent/context_runtime/search_verification.py:442,607,723`（硬编码 `deliverable == "business_report"` 与 `search_pack(..., scene)`）；
     - `apps/agent/src/compass_agent/promptops/starter_catalog.json:118`（`validate_starter` 强校验 `intent-routing.json` 的选项键列表，单改 JSON 会导致 PromptOps 发布校验失败）；
     - `apps/web/src/server/modules/access/application/{ports.ts,service.ts}` 与 `apps/web/src/server/http/` 的工作区 DTO（且 `TECH_DESIGN.md §2.1.3 #5` 误将路由写作 `/api/v1/workspaces`，真实路由见 `handler.ts:278,345` 为 `/api/web/v1/tenants/:tenantId/workspaces`）。
- **触发条件**：研发按任务清单与代码附录逐项落地。
- **影响**：交付产物（`report.md` / `analysis.csv`）静默失效、PromptOps 无法发布、ContextOps 域筛选口径前后端不一致。
- **可执行动作**：补全 `CODE_DESIGN.md` 中 `assemble_step()`、`resolve_profile()`、`flash_input()` 与 `ContextOps`（`recorder.py` + `analytics.py` 筛选/统计口径）的核心代码示意，并将上述遗漏文件补入 `T1/T3/T4/T6/T7`，更正 Web 工作区接口路径。

---

### P2（建议）

#### [P2-1] Web 语义 `/context` 的词法候选（`lexical.suggestions`）补充同域加权

- **证据**：`CODE_DESIGN.md:656-689` 仅对 `matched`（`rank >= 3`）应用 `domainBoost`，未对 `lexical.suggestions`（`relatedMetrics`）做同域优先排序，且 `lexical.suggestions` 排在 `domainDefaults` 之前。
- **影响**：泛化问句（如“昨天库存整体怎么样”，`rank < 3`）可能先被异域词法候选占满首屏 8 项名额，削弱 AC-05 的业务域召回效果。
- **建议动作**：对 `lexical.suggestions` 按 `domainBoost(b) - domainBoost(a)` 做稳定重排（保持原相关性序作为次排序键），再与 `domainDefaults` 合并补齐。

#### [P2-2] 核对受控自主模式下 `routing/service.py:191-193` 强制走 `flash_only` 的预期行为

- **证据**：现网 `apps/agent/src/compass_agent/routing/service.py:191-193` 在 `attempt_budget is not None` 时强制 `routing_mode = "flash_only"`；而 `TECH_DESIGN.md §2.1.2` 与 `§3 R1` 假设标准 Run 默认先走 JEV 再降级 Flash。
- **影响**：若维持该逻辑，生产受控 Run 不会触发 JEV 路径，R1 的 JEV 延迟与 Fallback 率仅在非受控路径生效。
- **建议动作**：在方案 `§2.3.1` 明确本次是否保持受控 Run 走 `flash_only`（若是，注明 JEV 适用场景与验证入口；若放开 JEV，补齐 JEV 计入 `attempt_budget` 的边界验证）。

#### [P2-3] PromptOps 校验与结构化资产清理细节

- **证据**：`CODE_DESIGN.md:586,615` 校验 `examples` 用 `len(v)` 允许全空白字符串；`promptops/catalog.py:110-120` 与 `legacy_layout.json:151-186` 仍含拟删除的 `classify-flash-managed.md`、`deliverable.md`（及 `business.md`）。
- **建议动作**：`examples` 校验改为 `0 < len(v.strip()) <= 500`；明确列出 `legacy_layout.json` 保留的 `instructions.json`/`execution.json`/`policies.json`/`tool-guidance.json` 成员映射与须删除的旧键清单，避免 `validate_members` 在发布时抛 `KeyError`。

---

## 4. 范围取舍清单（补齐 / 删减 / 保持现状）

| 分类 | 具体事项 | 依据 |
| --- | --- | --- |
| **本次必须补齐** | 1. 统一 10 意图的受管执行、Direct Stream 守卫提示词加载、`off_topic` 512 token 限制与 `handoff` 零写工具边界（P0-1）。<br>2. 解除 `model_step.py` / `routing/prompts.py` 对 `support/routing/business.md` 的依赖或保留该键（P0-2）。<br>3. 修正 `DecisionAnswer.noul` 字段名与 `_pin_direct_prompts()`（P0-3）。<br>4. 同步 `schemas.py`、`promptops/editor.tsx`、`contextops/flow.tsx` 工具通道枚举，处理 `0021` 旧观测 JSONB 与 `unclassified` 类型（P0-4）。<br>5. 统一 `effective_compound`（排除 `workspace_prior` 双域误升档），禁止将 `workspaceDefaultDomains` 塞入分类器输入（P1-1, P1-2）。<br>6. 补齐 `database.py`、`db-common.mjs`、发布前执行 `promptops.sync`、以及 `delivery.py` / `search_verification.py` / `starter_catalog.json` 等遗漏文件与 ContextOps 聚合口径（P1-4, P1-5）。 | 消除运行时 503/500 崩溃、安全守卫失效、成本误升档与迁移门禁缺口。 |
| **建议删减或延后** | 1. **延后 `domain_routed` 工具通道、`ToolGovernance.domains` 与 `SkillSpec.domains`**（P1-3，需用户确认调整 AC-05 第 ③ 项）。<br>触发重新引入条件：首个真实绑定单一/少数业务域的静态工具或 L28 域级插件落地时。 | 当前 19 工具与 3 Skill 均无消费者，删减可避免跨前后端空转枚举与虚假扩展点。 |
| **保持现状** | 1. `llm/` 适配器与并发重试结算内核、`harness/` 通用循环框架、`context_ops/compiler.py`（PEP 脱敏/拦截）与 `replay.py`、`goal/` 长任务账本。<br>2. 既有 RBAC、受限只读数据库连接、语义发布范围与 Context PEP 权限体系（工作区先验与域标签绝不介入鉴权）。<br>3. 10 `task_intent` + 7 `domain_id`（覆盖 Web 11 语义域）的正交模型与双库独立所有权边界。 | 复用既有成熟防护，满足 MVP 边界与项目硬约束。 |

---

## 5. 待验证事项与业务测试缺口回传

### 待确认与待验证事项

1. **产品/架构取舍待决（负责人：andy）**：
   - 是否确认采纳 **P1-3** 删减建议：本期移除 `domain_routed` 通道与 `SkillSpec.domains`，收敛 AC-05 为“域口径指引注入 + Web 语义同域加权 + ContextOps 域维度分析”？
   - 是否确认 `0021` 迁移直接清空预上线旧观测表（`context_decision_summary` / `context_badcase` / `context_snapshot` / `context_security_event`），还是保留旧行并清洗 `decisions_json.toolAssembly`？
   - 受控自主模式（`attempt_budget is not None`）是否维持 `flash_only`（P2-2）？
2. **实现前/实现中待验证假设**：
   - `IntentPrediction.model_json_schema()` 生成的 `domains` 数组 Schema 在当前配置的 Flash 上游 Provider 上稳定通过严格 JSON Schema 输出校验。
   - PostgreSQL 17 实库验证 `0020 → 0021` 与 `0010_knowledge_library → 0011_workspace_default_domains` 的空库安装、旧库升级、错版本拒绝、受限运行角色权限、`pnpm db:sql:check` 一致性与备份恢复演练。

### 业务测试缺口回传（供编排者安排 `my-create-test-plan`）

- **缺口清单**：
  1. 既有 E2E 用例（如 `apps/web/tests/e2e/intent-routing.spec.ts` 中对 `SC-39` 与旧 ContextOps 面板的断言）随契约替换将全面失效。
  2. 新增关键业务路径缺少可验收场景：全部 10 个 `task_intent`（含 4 个边界意图 Direct Stream 守卫文案、`off_topic` 512 token 截断、`handoff` 维护者 vs 普通用户权限差异）、工作区 `default_domains` 配置（含 0/1/2 项、桌面/390px 窄屏与键盘操作）、双先验域不误升档 Pro、显式跨业务域提问不被先验覆盖且不丢失精确指标、复合任务升档与工具包解锁、ContextOps 双维意图/域筛选与轨迹展示。
- **建议动作**：在本次方案 P0/P1 修订并复评通过后，由编排者调用 `my-create-test-plan` 为 `968-intent-taxonomy-global-assembly` 建立完整业务验收测试计划，并重点覆盖上述回归与新增路径。
