## 设计目标

定义第 25 课新增与修改的全部模型可见文本：三层装配关系、主/子 Agent 工具清单、`goal` / `sub_agent` / `report_to_parent` 工具描述、系统提示片段、子 Agent 提示、目标锚点模板，以及发布前的真实模型评测用例。

所有文本均为拟议稿，落地时写入 `apps/agent/src/compass_agent/prompts/` 并经 PromptOps 登记发布；工具参数 Schema 以代码中的 Pydantic 模型为准，由 `refresh_tool_schema` 自动刷新到工具 JSON。

## 装配位置与分层原则

{{code:assembly_diagram}}

三层文本各司其职，规则只写一处：

| 层           | 承载内容                                 | 变化频率          |
| ------------ | ---------------------------------------- | ----------------- |
| 系统提示片段 | 何时建目标、何时委派、何时并行等决策规则 | 随 PromptOps 版本 |
| 工具描述     | 单个工具的操作枚举、参数含义、调用时机   | 随 PromptOps 版本 |
| 目标锚点     | 本目标的契约、预算、待办等运行状态       | 每次模型调用      |

决策规则放在系统提示片段，工具描述只写本工具的用法并引用片段中的规则名，目标锚点只放状态不放规则。

片段按工具声明动态加入，沿用 `context_runtime/pipeline.py` 中 `database-query.md`、`workflow.md` 的现有做法：

{{code:pipeline_usage}}

PromptOps 登记：`promptops/catalog.py` 的 `families` 中登记 `("shared/fragments", "长任务", ("long-task", "delegation"))` 与 `("support/subagent", "子任务", ("analysis", "report-to-parent"))`；`delegation.md` 使用 `{{max_parallel}}`、`{{max_tasks}}` 两个变量，在 `allowed` 中声明。

## 工具清单

本课只给主 Agent 新增 1 个工具（`goal`），给子 Agent 新增 1 个宿主拦截工具（`report_to_parent`），避免工具数量膨胀。

### 主 Agent

| 工具         | 本课变化 | 出现条件                                                               | 一句话用途                               |
| ------------ | -------- | ---------------------------------------------------------------------- | ---------------------------------------- |
| `goal`       | 新增     | 满足目标能力前置条件（调度模式、受控自主、回答协议 2），与目标状态无关 | 提出目标、记录进度、申请完成、报告受阻   |
| `sub_agent`  | 修改     | 沿用现有条件（`context.workflow`）                                     | 委派一个或多个独立只读调查分支           |
| `todo_write` | 不变     | 沿用                                                                   | 本轮内的步骤计划；跨轮待办以 `goal` 为准 |
| 其余已有工具 | 不变     | 沿用                                                                   | 查询、检索、分析、定时等                 |

`goal` 在注册表中常驻，不随目标状态增删，避免审批恢复时触发 `tool_catalog_changed`；不适用的操作由 `authorize` 拒绝。

### 子 Agent

| 工具               | 本课变化 | 来源                                                                                          |
| ------------------ | -------- | --------------------------------------------------------------------------------------------- |
| 契约内只读工具     | 修改     | `child_tools()` ∩ `task.tools`；显式排除 `goal`、`sub_agent`、`todo_write`、`create_workflow` |
| `report_to_parent` | 新增     | 宿主拦截，不进入 Dispatcher；合并“汇报进度”和“交回结果”两种操作                               |

## `goal` 工具

一个工具，通过 `op` 判别字段区分四种操作，写法沿用 `todo_write` 的 `op` 结构。

### 工具定义（`prompts/tools/goal.json`）

{{code:tool_goal}}

### 参数说明

| 字段                               | 适用 op                   | 说明                                                                    |
| ---------------------------------- | ------------------------- | ----------------------------------------------------------------------- |
| `op`                               | 全部                      | `propose` / `record` / `complete` / `block`                             |
| `contract.objective`               | propose                   | 一句话目标，含时间范围与业务范围                                        |
| `contract.acceptance[]`            | propose                   | 1–10 条可核对的验收项，`id` 形如 `a1`                                   |
| `contract.constraints[]`           | propose                   | 口径、数据范围、禁止事项                                                |
| `budget`                           | propose                   | `maxRounds`、`tokens`、`costMicrousd`；省略则用默认值                   |
| `goalId`、`expectedRevision`       | propose（修改）、complete | 取自目标锚点；版本过期时返回 `goal_stale_revision`                      |
| `items[]`                          | record                    | 1–10 条，每条 `type` 为 `conclusion` / `todo` / `todo_done` / `failure` |
| `items[].key`                      | record                    | 条目稳定键；同键再次写入即更新                                          |
| `items[].evidenceIds`              | record（conclusion）      | 本轮证据 id，至少 1 个                                                  |
| `items[].factKey`、`value`、`unit` | record（conclusion）      | 可比较事实的键与数值                                                    |
| `items[].acceptanceIds`            | record（conclusion）      | 该结论支撑的验收项                                                      |
| `items[].required`                 | record（todo）            | 是否为完成目标所必需，默认 true                                         |
| `reason`                           | block                     | 需要用户做什么，60 字以内                                               |

### 运行时审批与拒绝码

| op       | 审批                                      | 目标状态要求                        | 拒绝码                                                |
| -------- | ----------------------------------------- | ----------------------------------- | ----------------------------------------------------- |
| propose  | `prepare_approval` 返回目标卡，需用户确认 | 无目标，或修改进行中/预算用尽的目标 | `goal_exists`、`goal_stale_revision`                  |
| record   | 无                                        | `active`                            | `goal_not_active`、`evidence_required`、`ledger_full` |
| complete | 无                                        | `active`                            | `goal_not_active`、`goal_stale_revision`              |
| block    | 无                                        | `active`                            | `goal_not_active`                                     |

`propose` 需要审批，按现有 `dispatch_batch` 规则必须单独成批；其余操作可与查询工具同批。

## `sub_agent` 与 `report_to_parent` 工具

### 主 Agent 委派工具（`prompts/tools/sub_agent.json`）

通过 `tasks` 列表显式表达单分支或并行多分支（最多 `{{max_tasks}}` 个，同时运行 `{{max_parallel}}` 个），同时兼容旧 `prompt` 写法。

{{code:tool_sub_agent}}

### 子 Agent 汇报工具（`prompts/support/subagent/report-to-parent.json`）

子进程内的宿主拦截工具，用 `kind` 区分中间进展（`progress`）和最终交付（`final`）。

{{code:tool_report_to_parent}}

## 系统提示片段

### 长任务规则（`shared/fragments/long-task.md`）

声明 `goal` 工具时自动加入系统提示：

{{code:frag_long_task}}

### 委派规则（`shared/fragments/delegation.md`）

声明 `sub_agent` 工具时自动加入系统提示。变量 `max_tasks = 4`、`max_parallel = 2`，与 `SubAgentInput.tasks` 上限和 `child_budget` 并发上限一致：

{{code:frag_delegation}}

### 子 Agent 系统提示（`support/subagent/analysis.md`）

子进程初始化时注入，约束子 Agent 仅依据任务包完成目标并以 `report_to_parent` 交付：

{{code:subagent_system}}

## 目标锚点模板

由 `goal/anchor.py` 在每次模型调用前重新生成，作为 `context` 通道消息插入系统提示之后。锚点只包含运行状态，不含决策规则；固定区块顺序，空区块省略，便于提示缓存。

{{code:goal_anchor}}

## 提示词评测

模拟模型无法检验决策质量，PromptOps 发布前需使用真实模型运行以下用例并核对工具调用序列：

| 用例                                                       | 期望行为                                    | 关联       |
| ---------------------------------------------------------- | ------------------------------------------- | ---------- |
| “10 月华东 GMV 是多少”                                     | 不调用 goal；不委派                         | 长任务规则 |
| “把 10 月经营分析做完，四个阶段，每个结论要有来源”         | 先 `goal.propose`，单独成批                 | AC-07      |
| 目标进行中，锚点有“库存缺货排查”“渠道转化排查”两条独立待办 | 一次 `sub_agent`，`tasks` 含 2 个任务       | AC-01      |
| 待办“对比库存与转化结论”依赖前两项                         | 不委派；前两项结果 record 后由主 Agent 完成 | 委派规则   |
| 子任务返回 partial 且 gaps 非空                            | 补查或改写后委派一次；不原样重试            | AC-10      |
| 锚点出现收尾提示                                           | 不再发起新查询，输出已完成项与剩余项        | AC-07      |
| `goal.complete` 被打回                                     | 按 gaps 补齐后再申请                        | AC-08      |

用例与第 24 课的真实模型验收用例放在同一入口，缺少模型凭据时跳过，跳过不计为通过。用例只用于评估提示词，生产 Runtime 不按用例内容设置执行分支。
