# Compass 工具定义（ACI）问题诊断报告：高失败率与高推理成本归因分析

> 代码基线：`compass@main (d0aa488)` | 关联需求：[spec/PRD.md](../spec/PRD.md) | 关联方案：[TECH_DESIGN.md](./TECH_DESIGN.md)

[返回需求简报](../BRIEF.md)

## 1. 核心诊断结论与量化总览

对 Compass 现有 **19 个静态注册工具**（[`prompts/tools/*.json`](../../../apps/agent/src/compass_agent/prompts/tools/)）及子 Agent 汇报工具 **`report_to_parent`**（[`report-to-parent.json`](../../../apps/agent/src/compass_agent/prompts/support/subagent/report-to-parent.json)）的源码与声明逐一拆解后，可以确认当前**调用失败率高**与**推理成本高**源自同一套底层反模式，并通过四级乘数效应相互放大：

$$\text{总工具成本} = \text{单步固定声明前缀} \times (\text{多跳复合拆步轮数} + \text{首轮踩雷率} \times \text{黑盒报错盲猜重试轮数})$$

### 1.1 源码实测体检指标（19 个静态工具 + `report_to_parent`）

| 诊断维度 | 源码实测数值 | 代码位置与直接后果 |
| --- | --- | --- |
| **静态工具声明总体积** | 格式化源码 `78,797 B`（`76.9 KiB`）<br>紧凑传输 JSON **`42,719 B`（约 `10,680 tok`/步）** | 子任务另含 `report_to_parent` `3,111 B`；Top 3 工具（`goal`、`final_answer`、`schedule_write`）占全目录 `37%` 体积 |
| **单轮暴露策略（`answer_protocol=2`）** | 除 `identify` 外**全量平铺暴露** | [`profiles.py`](../../../apps/agent/src/compass_agent/context_runtime/profiles.py#L198-L205) 将 `stage_tools` 与 `ceiling` 置为 `None`，简单查数也全程携带长任务与写治理工具 |
| **`$defs` 嵌套子模型数量** | 静态工具 **`43` 个**（含子任务共 `47` 个） | 大量使用 `discriminator + oneOf + $defs`，公共属性在多分支间重复展开 3–5 遍 |
| **Pydantic 导出噪声节点** | **`262` 处 `"title"`** + **`81` 处 `anyOf: [T, null]`** | 每个可选字段膨胀为 6 行联合结构，纯语法噪音消耗数千固定前缀 Token |
| **参数级 `description` 覆盖率** | `227` 个属性中仅 **`9` 个有说明（空白率 `96.0%`）** | **19 个工具中有 15 个参数说明数为 `0`**（含 38 属性的 `final_answer`、35 属性的 `schedule_write`、31 属性的 `goal`） |
| **复合多子命令工具占比** | `8` 个核心工具内置 `op / operation / mode / kind` | 用私有子命令状态机替代 SQL、Markdown、全量幂等数组等模型原生通用原语 |
| **参数与数据库报错可读性** | 统一折叠为裸错误码 `"invalid_parameters"` / `"database_query_failed"` | [`dispatcher.py`](../../../apps/agent/src/compass_agent/tools/dispatcher.py#L149-L158) 与 [`database_query`](../../../apps/agent/src/compass_agent/tools/builtin/database_query/__init__.py#L397-L404) 吞掉全部字段路径与列信息 |

---

## 2. 七大核心病灶深度剖析

### 病灶一：过度设计“复合工具（Mini-RPC 状态机）”，放弃模型原生擅长的通用原语

当前工具体系普遍采用“面向后端 CRUD / 状态机接口”的思路，在一个工具内塞入多个 `op / operation / mode / kind` 子命令。这既迫使 Schema 引入复杂的 `$defs` 分支与互斥校验，又把本可 1 步完成的任务拆成了 3–4 轮串行往返：

1. **`database_query`：人为封死 `information_schema`，再造出 4 合 1 子命令**
   - **代码根源**：[`database_query/__init__.py`](../../../apps/agent/src/compass_agent/tools/builtin/database_query/__init__.py#L60-L64) 在 `_CATALOG` 中硬编码了 `AND n.nspname <> 'information_schema'`，禁止标准 SQL 查询元数据视图；随后为了弥补表结构探索需求，额外包装出 `list_connections`、`list_tables`、`describe_table`、`query` 四个 `op` 分支与 12 个属性（`2,391 B`）。
   - **成本与失败代价**：PostgreSQL 的 `information_schema.tables` 与 `columns` 本身就支持按当前数据库会话角色权限自动过滤可见表列。拆成私有子命令后，模型核对两张表结构再执行聚合查询需要串行交互 **4 轮**（`list_tables` → `describe_table(A)` → `describe_table(B)` → `query`）。其实工具只需保留唯一参数 `sql`，在工具 `description` 中给出查表结构与查数据的 SQL 最佳实践即可一步到位。
2. **`todo_write`：把 3–6 行待办清单做成 4 子命令 + DAG 拓扑状态机**
   - **代码根源**：[`plan.py`](../../../apps/agent/src/compass_agent/tools/builtin/todo_write/plan.py#L24-L160) 为单轮内待办事项设计了 `replace`、`select`、`complete`、`skip` 四个动作，叠加 `baseVersion` 乐观锁、`selectTodoId`、`dependsOn` 拓扑环检测、已移除 ID 集合 `retired` 及 `SkipReason` 枚举，并在运行时抛出 `invalid_plan`、`invalid_transition`、`invalid_dependency`、`dependency_unmet` 等多种异常。
   - **成本与失败代价**：单轮计划只有寥寥几行，采用单参数全量幂等覆盖（`todos: [{step, status}]`）即可零状态错误表达，完全无需增量状态机与多子命令。
3. **`schedule_read` 与 `schedule_write`：列表人为删字段，逼模型跨子命令多跳读取**
   - **代码根源**：[`schedule_read/__init__.py`](../../../apps/agent/src/compass_agent/tools/builtin/schedule_read/__init__.py#L75-L78) 在处理 `operation="list"` 时专门执行了 `item.pop("question", None)`，隐去定时任务实际执行的分析问题；模型调完 `list` 后无法确认任务内容，被迫再调一轮 `operation="get"`。`schedule_write` 则在 `ScheduleCreateArgs` 与 `SchedulePatch` 之间把全部配置字段镜像重复定义两遍，属性数膨胀至 35 个。
4. **`goal`：在单一工具内嵌套两层复合状态机（31 个属性、`6,044 B`）**
   - **代码根源**：[`goal/contracts.py`](../../../apps/agent/src/compass_agent/goal/contracts.py#L87-L138) 第一层把需人工审批确认的 `propose`、高频静默写入的 `record`、触发审阅的 `complete` 与挂起等待的 `block` 合为一谈；第二层在 `op="record"` 的 `LedgerItem` 内部又混入 `conclusion`、`todo`、`todo_done`、`failure` 四种记录类型与 12 个字段，与 `todo_write` 职责重叠。
5. **`discover_semantic_candidates`：三种不同时机、不同审批语义的业务流混用 19 个平铺字段**
   - **代码根源**：[`semantic/tool.py`](../../../apps/agent/src/compass_agent/semantic/tool.py#L113-L184) 将“回答末尾顺带推荐新指标（无阻塞弹窗）”、“按用户要求保存临时查询为草稿（弹草稿确认卡）”、“维护者修订已发布指标（弹修订对比卡）”塞进同一个扁平输入类，只能在 Python 代码里堆砌 `if/elif` 拦截非法字段组合。
6. **`resolve_semantics` + `query_metric`：一面把单次查数拆成两跳，一面在 `query_metric` 内搞三合一**
   - 查询一个毫无歧义的正式指标（如“上周净销售额”）也必须先调 `resolve_semantics` 换取 `query_ref`，下一轮才能调 `query_metric`，固定多花一轮推理成本；同时 [`query_metric/__init__.py`](../../../apps/agent/src/compass_agent/tools/business/query_metric/__init__.py#L12-L20) 又将 `query`、`compare`、`drilldown` 三种模式及 `parameters`、`comparison`、`parent_group` 混在同一参数表内。
7. **`final_answer` 与 `report_to_parent`：全文塞进工具 JSON 阻塞流式输出 + 自造排版/计算/心跳 DSL**
   - [`answers.py`](../../../apps/agent/src/compass_agent/context_runtime/answers.py#L32-L85) 强迫模型把整篇回答、迷你排版树 `Block`（`paragraph` / `list` / `comparison_table`）和四则运算器 `Calculation` 全部写进工具入参 JSON 字符串，导致前端必须等整段 JSON 生成并校验结束后才能一次性拿到文本，首字延迟高且易触发截断；[`report-to-parent.json`](../../../apps/agent/src/compass_agent/prompts/support/subagent/report-to-parent.json) 在仅有 3–8 步预算的子 Agent 中混入 `kind="progress"`，诱导子任务消耗宝贵的模型推理步数去发送本可由 Runtime 事件确定性推送的进度文案。

---

### 病灶二：把确定性代码可决定的基础设施与运行态参数甩给大模型生成

只有真正需要模型做语义判断与内容生成的参数才应进入 Schema。当前多处工具把完全可由服务端确定性绑定或路由的底层参数交给了模型：

| 工具名 | 暴露给模型的确定性/机械字段 | 为什么完全应由代码确定性处理 |
| --- | --- | --- |
| `database_query` | `connectionId`（3 处重复）+ `list_connections` 子命令 | [`handler.py`](../../../apps/agent/src/compass_agent/tools/builtin/database_query/handler.py#L138-L144) 在 `connectionId` 为空时本就会自动选中工作区默认/唯一连接；即便存在多数据源，代码也可按 `schema.table` 归属关系确定性路由连接。描述写“先用 `list_connections` 发现连接”直接诱骗模型每题多跑一轮废调用。 |
| `sub_agent` | `DelegationTask.connectionIds`（UUID 数组）、`tools`（工具名数组） | 子 Agent 的可用数据源连接与只读工具白名单天然由父级授权上下文确定性继承，让模型手填 UUID 与工具名极易填错拒绝。 |
| `submit_semantic_change` | `request_key`（必填 `format: "uuid"`）、`binding_id` | 让概率生成模型凭空捏造符合十六进制格式的幂等 UUID 属于典型反模式；服务端用 `uuid5(run_id, f"{step_id}:{call_id}")` 即可 100% 确定性生成。 |
| `schedule_write` | `selection`（含 `provider_id` UUID）、`timezone`、`constraints`、`workflowAssetRef`、`expectedRevision` | 会话当前使用的模型路由、工作区默认时区、默认步数/超时约束及刚读取的计划版本号均已在运行上下文中，35 个属性里真正需要模型生成的仅任务名、问题与周期 3–4 项。 |
| `todo_write` / `goal` | `baseVersion`、`goalId`、`expectedRevision`、`costMicrousd` | 单步推进中的当前版本号与活动 `goalId` 在 Run 状态中唯一确定；微美元预算（如 `20000000`）单位反直觉，应由平台策略托管。 |
| `search_semantics` / `discover_semantic_candidates` | `scope_filter`、`owner_scope_id`、`usage_scope_id`、`timezone` | 组织与可见范围由 `trusted.scope` 硬权限决定，模型既不知道租户内部范围 UUID，也不应越俎代庖指定。 |
| `resolve_semantics` / `query_metric` | `Parameters.as_of` | `resolve_semantics` 描述已写明“服务端按 Run 的受理时刻填写 `as_of`”，却仍在两个工具的 `$defs.Parameters` 中暴露该字段。 |

---

### 病灶三：说明书职责错位——96% 的参数缺少 `description`，顶层 `description` 塞满内部细节与返回值解释

高质量工具定义应当遵守明确分工：**顶层 `function.description` 负责告诉模型“这个工具干什么、什么场景该调、什么场景禁调及最佳实践”；参数级 `description` 负责就近描述该参数的格式与取值约束**。现有定义恰好颠倒：

1. **参数级 `description` 几乎全空（`218 / 227` 为空）**：
   - 模型在生成具体字段（如 `SourceRef` 的 `row`/`column`/`paragraphId`、`LedgerItem` 的 12 个属性、`discover_semantic_candidates` 的 `fields`）时没有任何就近说明，只能去上千字的顶层描述里跨段寻找线索。
2. **顶层 `description` 大量混入“调用后返回值解释”**：
   - 例如 [`query_metric.json`](../../../apps/agent/src/compass_agent/prompts/tools/query_metric.json) 用近半篇幅解释返回结果中 `current_minus_comparison` 的正负号定义、单侧缺失分组按零参与差额的算法以及 `period_totals` 的含义。返回值语义完全可以在实际调用后的回执结构中自解释，塞进工具定义意味着哪怕整轮对话根本不调该工具，每一步也要为这段说明买单。
3. **顶层 `description` 大量混入“模型无法控制的后端安全/事务实现声明”**：
   - 如 *“资料权限和有效期在读取时复核”*（`read_document`）、*“系统另按资料封版时点过滤可见版本”*（`search_documents`）、*“查询按当前授权、超时和执行资源边界运行”*（`database_query`）、*“启停和删除不会取消已经受理的运行”*（`schedule_write`）、*“候选会经通用 Verify 再发布”*（`final_answer`）。这些后台机制无需模型操心，只会稀释有效指令密度。
4. **真正关键的“触发边界与防冗余规则”缺失，甚至内嵌整张 Markdown 表**：
   - `get_metric_definition` 与 `todo_write` 仅有一句功能简述，未说明“普通查数禁调 `get_metric_definition`”、“单步快查禁调 `todo_write`”；而 `goal` 与 `report_to_parent` 则把整张多列 Markdown 表格塞进了 JSON 字符串。

---

### 病灶四：后端 Pydantic DTO 原样直出，迷信重型 JSON Schema 语法而忽视“宽进严出”

所有工具直接调用 `Model.model_json_schema()` 导出模型契约，试图用最繁琐的 JSON Schema 语法锁死输入，带来了严重的 Token 膨胀与脆弱校验：

1. **`43` 个 `$defs`、`262` 个 `"title"` 与 `81` 个 `anyOf: [T, {"type": "null"}]`**：
   - JSON Schema 中属性只要不放进 `required` 天然就是可选参数；Pydantic 对每个 `str | None = None` 生成的 6 行 `anyOf` + `"default": null` + `"title"` 纯属序列化噪音。
2. **严苛机械正则误杀合法业务语义**：
   - `todo_write` 对步骤 ID 强卡 `"pattern": "^t[1-9][0-9]{0,2}$"`（传 `"1"` 或 `"step1"` 即报错）；`goal` 与 `report_to_parent` 对 `key` / `factKey` 强卡 `"pattern": "^[a-z0-9_.:-]{1,80}$"`（中文分析场景下模型写出中文键名即刻失败）。用简单 `string` 配合一句短参数说明，并在服务端做归一化，远比正则硬拦稳健且省 Token。
3. **内部补丁与废置定义直接泄漏到模型上下文**：
   - `search_semantics.cursor` 暴露出内部兼容补丁 `anyOf: [{format: "uuid"}, {const: 0}, {type: "null"}]`；`todo_write` 的 `$defs` 中残留了空定义 `"SkipReason": {}`。

---

### 病灶五：强迫模型“双份记账（Double-Bookkeeping）”，再用校验器惩罚不一致

多处契约要求模型在单次调用里把同一事实用两种格式重复输出，并在服务端编写 `@model_validator` 检查两边是否严丝合缝：

1. **`final_answer` 的三重冗余拼装**（[`answers.py`](../../../apps/agent/src/compass_agent/context_runtime/answers.py#L58-L85)）：
   - 普通单轮问答没有活动长任务，也必填 `coveredGoalIds: []` 与 `unresolved: []`；
   - `unresolved` 是否为空已经 100% 决定了回答是否完整，却强制模型再填一遍 `completion: "complete" | "partial"`，不一致就抛错；
   - `claims` 已经按顺序写出了每条结论文本，还强制模型再手写一遍 `blocks: [{type: "paragraph", claimIds: [...]}]`。
2. **`resolve_semantics` 与 `query_metric` 的重复维度/筛选字段**：
   - `resolve_semantics` 顶层有 `dimensions: list[str]`，嵌套 `parameters` 里又有 `group_by: str`；
   - `query_metric` 的 `query_ref` 已绑定解析参数，顶层却仍挂着完整 `parameters`，下钻时还要求把同一父级筛选在解析参数和 `parent_group` 里各写一遍。
3. **`sub_agent` 的 `InputRef` 类型标签冗余**：
   - 要求传 `{"kind": "ledger" | "artifact" | "evidence", "id": "..."}`，而系统存储完全能凭 ID 本身确定性识别来源类型。

---

### 病灶六：隐式跨字段耦合叠加“哑巴式”黑盒报错，彻底阻断单步自愈

当工具调用失败时，模型能否在 1 轮内修好参数，完全取决于错误回执的信息量。当前代码在校验与数据库执行两层都吞掉了全部定位线索：

1. **隐藏在 Python 层的互斥雷区**：
   - `read_tool_result`（`cursor` 与 `offset/limit` 互斥）、`search_documents`（`historical=true` 必传 `businessDate`）、`sub_agent`（`prompt` 与 `tasks` 二选一且任务名唯一）、`discover_semantic_candidates`（`trigger` 与 `revise_metric`/`source_refs` 互斥）在 Schema 层全部放行，进入 Python 函数才抛异常。
2. **`ToolDispatcher` 丢弃全部 Schema 错误路径**：
   - [`dispatcher.py`](../../../apps/agent/src/compass_agent/tools/dispatcher.py#L149-L158) 使用 `Draft202012Validator(...).is_valid(args)` 布尔判断，一旦返回 `False` 直接抛 `ToolRejected("invalid_parameters")`，不告诉模型究竟是缺了哪个必填项、哪个字段类型不对还是哪条正则没过。
3. **`database_query` 把 SQL 语法、函数白名单、权限拒绝与错列名全部折叠为两个裸词**：
   - [`database_query/__init__.py`](../../../apps/agent/src/compass_agent/tools/builtin/database_query/__init__.py#L136-L189) 中 `SqlRejected` 继承自 `ValueError`：只要模型写了不在 `_FUNCTIONS` 白名单内的常见 PostgreSQL 函数（如 `to_date`、`regexp_replace`）、漏写 `schema.table` 前缀或触发 AST 解析失败，在 [L397-L404](../../../apps/agent/src/compass_agent/tools/builtin/database_query/__init__.py#L397-L404) 全部被捕获为裸字符串 **`"invalid_parameters"`**！
   - 而一旦 SQL 进入 PostgreSQL 触发列名拼写错误（`UndefinedColumn 42703`）或类型转换错误，全部被捕获为裸字符串 **`"database_query_failed"`**，不返回报错列名，导致模型只能盲猜重试。

---

### 病灶七：全量平铺暴露、上下游数据句柄脱节与跨工具文档腐化

1. **全量平铺与容量死锁**：[`profiles.py`](../../../apps/agent/src/compass_agent/context_runtime/profiles.py#L198-L205) 在 `answer_protocol == 2` 下放开了阶段与范围限制，所有场景平铺暴露全部工具；同时 [`registry.py`](../../../apps/agent/src/compass_agent/tools/registry.py#L99-L127) 在初始化阶段对全量工具硬卡 `<= 32` 槽位，缺少按需发现机制。
2. **语义预注入未短路**：`model_step.py` 已向提示词预注入 `publishedSemanticRegistry`（含已解析的 `query_ref`），但 `search_semantics`、`get_metric_definition`、`resolve_semantics` 的描述均未声明“预注入已命中时直接跳过”，导致模型惯性串行连调 3–4 步。
3. **查询与 Python 沙箱衔接断层**：`database_query` 输出字段名为 `resultId`（[L213](../../../apps/agent/src/compass_agent/tools/builtin/database_query/__init__.py#L213)），而 `python_analysis` 入参名叫 `source_query_id`，且 `python_analysis` 未说明预置变量 `data` 的字典结构为 `{"columns": [...], "rows": [[...]]}` 及 `128 KiB` 输入上限，模型极易把 `data` 当作 `list[dict]` 报 `KeyError` 或整表送入沙箱超限。
4. **跨工具说明书腐化冲突**：[`create_workflow.json`](../../../apps/agent/src/compass_agent/prompts/tools/create_workflow.json) 至今仍在教模型写 `await agent("sub_agent", {prompt: "完整子任务"})` 并读取 `{answer}`，与已演进为 `tasks` 数组并返回 `{status, summary, findings, deliveries}` 的 `sub_agent` 现行契约直接冲突。

---

## 3. 全量工具逐项问题体检表（按紧凑声明体积降序）

| # | 工具名称 | 紧凑体积 | `$defs` | 属性数（含说明数） | 命中的核心病灶与具体问题摘要 |
| --- | --- | --- | --- | --- | --- |
| 1 | `goal` | `6,044 B` | 8 | `31 (0)` | **[病灶 1/2/3/4]** 双层复合工具（4 种 `op` × 4 种 `LedgerItem`）；暴露 `costMicrousd`、`goalId`、`expectedRevision`；`key`/`factKey` 正则易挂；描述内嵌整张表格；简单问答全程陪跑 |
| 2 | `final_answer` | `4,953 B` | 9 | `38 (0)` | **[病灶 1/4/5]** 自造 `Block` 排版树与 `Calculation` 计算器；强制双写 `completion` + `unresolved` + `coveredGoalIds`；7 字段可空 `SourceRef` 全部零参数说明 |
| 3 | `schedule_write` | `4,821 B` | 8 | `35 (0)` | **[病灶 1/2/4]** `Create` 与 `Patch` 全字段镜像重复；暴露 `MainModelSelection`（`provider_id` UUID）、`timezone`、`RunConstraints`、`WorkflowAssetRef`、`expectedRevision`；只读会话常驻暴露 |
| 4 | `discover_semantic_candidates` | `3,931 B` | 1 | `19 (4)` | **[病灶 1/2/6]** 推荐新指标、保存草稿、修订存量指标三件事混为一谈；暴露 `owner_scope_id`、`usage_scope_id`、`timezone`；Python 层隐藏多组参数互斥 |
| 5 | `sub_agent` | `3,291 B` | 2 | `12 (0)` | **[病灶 2/5/6]** 顶层混放遗留 `prompt` 与 `tasks` 且无 `required`；暴露 `connectionIds` UUID 数组与 `tools` 白名单；`InputRef` 强迫模型分类 `kind` |
| 6 | `report_to_parent` *(子任务)* | `3,111 B` | 4 | `16 (0)` | **[病灶 1/3/4]** 把消耗步数的 `progress` 心跳与终止交付 `final` 合在一起；`factKey` 强卡英文正则；顶层描述内嵌 Markdown 表格，16 个参数零说明 |
| 7 | `todo_write` | `2,837 B` | 6 | `19 (0)` | **[病灶 1/2/4]** 4 子命令 + DAG 拓扑依赖状态机；强制每步手算 `baseVersion`；`todoId` 强卡 `^t[1-9]...` 正则；残留空定义 `SkipReason: {}` |
| 8 | `query_metric` | `2,507 B` | 1 | `10 (2)` | **[病灶 1/3/5]** 混装 `query`/`compare`/`drilldown` 并挂着冗余 `parameters` 与 `as_of`；顶层描述大篇幅解释返回字段计算细节；未声明相对手写 SQL 的优先权 |
| 9 | `database_query` | `2,391 B` | 4 | `12 (0)` | **[病灶 1/2/6]** 封禁 `information_schema` 后包装 4 个 `op` 子命令；暴露 `connectionId` 并诱导先跑无意义的 `list_connections`；SQL 函数/权限/语法报错全吞成 `"invalid_parameters"`，缺列吞成 `"database_query_failed"` |
| 10 | `resolve_semantics` | `2,310 B` | 1 | `10 (1)` | **[病灶 2/5/7]** 顶层 `dimensions` 与嵌套 `group_by` 重复，英文说明 `historical_as_of` 与嵌套 `as_of` 重复；无歧义查数也被迫先走一跳拿 `query_ref`；未写预注入短路规则 |
| 11 | `schedule_read` | `1,779 B` | 3 | `9 (0)` | **[病灶 1/4]** 拆成 `list`/`get`/`history` 3 个 `$defs`；`list` 代码人为删掉 `question` 字段逼模型再调一次 `get` |
| 12 | `create_workflow` | `1,638 B` | 0 | `5 (2)` | **[病灶 6/7]** 描述腐化，仍在教模型使用旧版 `agent("sub_agent", {prompt})` 与 `{answer}`；Schema/语法/试跑报错统一折叠为 `invalid_workflow_script` |
| 13 | `read_tool_result` | `1,278 B` | 0 | `4 (0)` | **[病灶 3/4/6]** 描述重复解释返回状态字段；`cursor` 与 `offset/limit` 互斥藏在 Python 层，续页带残留 `offset` 直接报错 |
| 14 | `search_documents` | `1,219 B` | 0 | `5 (0)` | **[病灶 3/6]** 拆分 `historical` 布尔开关与 `businessDate` 制造跨字段条件依赖；`entity` 与 `query` 重叠；5 参数零说明 |
| 15 | `search_semantics` | `1,005 B` | 0 | `6 (0)` | **[病灶 2/4/7]** 暴露 `scope_filter` UUID；`cursor` 含 `{const: 0}` 内部补丁；`kinds` 无枚举约束；未声明预注入命中短路规则 |
| 16 | `submit_semantic_change` | `751 B` | 0 | `5 (0)` | **[病灶 2/4]** 强制模型手写随机幂等键 `request_key`（UUID 必填）并暴露 `binding_id`；普通查数会话全程暴露 |
| 17 | `read_document` | `589 B` | 0 | `2 (0)` | **[病灶 3/6]** 描述混入后端权限复核内幕；未知 `ref` 缺少引导先调 `search_documents` 的结构化提示 |
| 18 | `python_analysis` | `491 B` | 0 | `2 (0)` | **[病灶 7]** 未交代 `source_query_id` 与上游 `resultId` 的对应关系，未写明 `data={"columns":[...],"rows":[[...]]}` 结构与 `128 KiB` 输入上限 |
| 19 | `get_metric_definition` | `488 B` | 0 | `2 (0)` | **[病灶 2/3/7]** 暴露低频 `version_id`；未写清与 `resolve_semantics` 的分工，导致常规取数多走一跳定义查询 |
| 20 | `review_semantic_change` | `396 B` | 0 | `1 (0)` | **[病灶 3/7]** 未写清输出 `digest` 与 `submit_semantic_change.expected_digest` 的衔接；只读查数会话常驻陪跑 |

---

## 4. 工具定义重构的五条工程标尺（供后续方案对齐）

基于以上诊断，后续对 Compass 工具契约的改造应严格按以下五条精简准则逐项过筛：

1. **去复合化，回归通用原语与流式直出（Simple Single-Purpose Tools）**：能用标准原语表达的绝不拆分子命令状态机——`database_query` 收敛为单一 `sql` 参数（放开受控只读元数据查询，在 `description` 给 SQL 查表/查数最佳实践）；`todo_write` 收敛为单一幂等数组 `todos: [{step, status}]`；`schedule_read` 一次返回完整摘要避免 `list -> get` 二跳；`final_answer` 收敛为唯一 `mode`（`"plain" | "evidence"`）触发器，背后路由两套独立 Answer Prompt 实时流式推送前端（`evidence` 模式由服务端将搜索/查询结果排序过滤编号后作为模板变量嵌入，正文直接输出 `^[1]^` 角标），并通过工程门禁保证 `final_answer` 必定作为最后一个工具调用、流式输出完成后直接强制 `stop`。
2. **代码确定性优先，严禁向模型泄漏环境与状态字段（Deterministic Code over LLM Generation）**：凡是服务端已知或可推导的字段——数据库连接 `connectionId`、幂等键 `request_key`、乐观锁版本 `baseVersion` / `expectedRevision`、模型路由 `selection`、工作区 `timezone`、组织范围 `scope_filter` / `owner_scope_id`、受理时点 `as_of`、子任务只读连接与工具继承——全部从模型 Schema 中彻底剔除，由后端代码确定性完成。
3. **顶层 `description` 与参数 `description` 各司其职**：
   - **工具 `description`**：只写“干什么、何时用、何时禁调（含上下文短路规则）”与极简最佳实践，删除一切返回值算法解释与后端权限/事务内部声明；
   - **参数 `description`**：用几到十几个字的自然语言直接写明格式与取值约束，取代昂贵且易碎的 `$defs`、`if/then/not/oneOf` 与复杂正则表达式。
4. **模型输入契约与内部领域 DTO 解耦（宽进严出 + 零语法噪音）**：彻底剥离 Pydantic 导出的 `$defs`、`"title"` 与 `anyOf: [T, null]`，消除 `completion` + `unresolved`、`claims` + `blocks` 等双份记账要求，由轻量适配层转换为内部强类型对象。
5. **按场景动态暴露 + 可行动报错自愈（Dynamic Supply & Actionable Errors）**：只读分析默认隔离写操作、定时与语义治理工具，长尾能力按需发现；一旦参数校验或 SQL 执行出错，返回带具体问题字段、报错原因与授权可见列候选的精简反馈，确保模型 1 轮内修正自愈。
