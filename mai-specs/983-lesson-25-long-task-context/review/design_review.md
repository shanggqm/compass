# 设计评审｜983 第 25 课长任务上下文治理

> 评审对象：[TECH_DESIGN.md](../design/TECH_DESIGN.md)、[CODE_DESIGN.md](../design/CODE_DESIGN.md)｜有效需求：[PRD](../spec/PRD.md)，AC-01～AC-10｜依据：项目 AGENTS.md、docs/database-migrations.md、apps/agent 当前工作区代码（行号对应 2026-11-24 快照）｜评审人：架构评审 Agent｜日期：2026-11-24
>
> 评审子任务环境只读，本文件由主 Agent 按其回传正文原样落盘。

## 1. 总体结论：需要修改

方向成立。在已有持久 Run 外层加 Goal 控制器、跨 Run 账本、父子交接契约，并复用公平队列、审批等待和 subagent step，分层清楚。迁移登记基本符合规范。

阻断有两处。

- 用户消息与自动续轮的交互模型和现有 Session 单活动 Run 约束冲突，AC-07/FR-06 的主路径无法实现。
- 续轮扫描挂进 `maintain` 后，单个目标的异常就能让整个副本失去就绪和 owner 健康状态，锁顺序还会和 `finish` 互相死锁。

另有 10 项 P1，涉及预算口径、子任务预算语义、账本来源格式、权限过滤、工具目录稳定性和主流游标。MVP 方面，建议删除 D9（主流折叠）和 D10（隐藏 todo_write，改投影 plan 视图）。删除后风险和工作量都会下降，且不影响 AC。

## 2. P0（阻断）

### P0-1 用户消息、修改目标和续轮之间没有可行的交互路径

- 证据
  - `store.submit_in_transaction` 在上一 Run 非终态或 outbox 未投递时直接抛 `session_busy` 409（runtime/store.py:413-422）。
  - `uq_agent_run_active` 限定每会话只有一个活动 Run，`paused` 也算活动（entities.py:170-179）。
  - 方案中 settle 设置 `next_round_due=now()`，maintain 每 0.5 s 扫描一次（durable_service.py:416）。用户能插话的窗口只有一个扫描周期加 outbox 投递时间；续轮 Run 进入 created/running 之后，用户提交只会得到 409。
  - 目标面板只有暂停、恢复、取消（TECH §2.1.3），修改目标只能通过模型调用 `goal_propose`，而这需要用户先发消息。AC-07 写的“第 2 轮执行中分析员修改约束并确认”在本方案下没有入口。
  - 方案只给 `goal_submit` 的 Run 写 `goal_id`（CODE §1 末段）。用户 Run 不绑定目标时，settle 会直接返回，`last_settled_run_id` 不更新。`_admit_one` 的条件 `latest.id != goal.last_settled_run_id` 因此永远成立，目标在用户发过一条消息后就卡死。CODE §1 的 Review 说明默认用户 Run 会被 settle，和拟议改动自相矛盾。
- 影响：FR-06“新消息优先、原续轮作废”、AC-07 修改与插话场景、NFR-02 都无法验收；目标还可能静默停摆。
- 建议（选一种并写入方案，再补对应 AC-07 测试）
  1. 会话存在 open goal 时，所有新 Run（用户 Run 和续轮 Run）都在提交事务内写 `goal_id`。
  2. 定义“用户提交抢占”：最新 Run 是 created 状态的续轮 Run 时，在同一事务中取消它、回退 `rounds_started`，再受理用户 Run；续轮 Run 已 running 时，用户提交设置 `control_intent`，在下一个 gate 边界结束本轮（计入已用量，不计无进展），随后受理用户 Run。也可以给出“排队一条用户消息”的替代方案。
  3. 目标面板增加「修改」入口，复用 2 的抢占路径，提交一条修改请求，由模型发起 `goal_propose`。
  4. Web 输入框在续轮运行期间保持可用，并按 2 的语义提示用户。

### P0-2 续轮扫描的故障隔离与锁顺序

- 证据
  - `maintain` 的 try 块里任何异常都会把 `store.ready=False`、`owner_healthy=False`（durable_service.py:409-415）。`journal.gate()` 会调用 `publisher.require_owner()`（journal.py:496），所以 owner 不健康时，本副本上所有运行中的 Run 都会在下一个边界中断。
  - `_admit_one` 只捕获 `GoalError.capacity`，而 `require_capacity` 抛的是 `ModelError(429)`（run_scheduler.py:112-125）。`submit_in_transaction` 还可能抛 `session_busy`、`not_found`（会话已软删除，owner_filter 在 store.py:139 排除 deleted）、`runtime_unavailable`。这些都没被捕获，16 个目标在同一事务里，会一起回滚。被清掉的 `next_round_due` 也随之回滚，于是每 0.5 s 重复失败一次，副本持续不可用。
  - 锁顺序相反：`finish` 先锁 session，再锁 run，再锁 goal（store.py:788，加上方案的 settle）；`admit_due` 先拿全局准入锁 984001（run_scheduler.py:80-82），再锁 goal（SKIP LOCKED），最后在 submit 里锁 session（store.py:382-386）。两者交叉时 PostgreSQL 会判定死锁并终止一方。`finish` 被终止的话，Run 会进入 `pause_interrupted` 并要求恢复（durable_service.py:769-791）。`act()` 同样是先锁 goal 再锁 run。
- 影响：一个已删除会话上的 open goal，或一次容量背压，就能让整个副本停摆、在途 Run 被中断；死锁还会把正常结束的 Run 变成待恢复。
- 建议
  1. `admit_due` 放在独立任务或独立 try 中执行，失败只记日志和指标，不影响 ready/owner_healthy。
  2. 每个目标单独一个短事务。统一锁顺序：先无锁选出候选 goal id，再按“准入锁 → session FOR UPDATE → goal FOR UPDATE NOWAIT/SKIP”复查。settle、act、goal_propose 的写入以及账本写入都遵守 session → run → goal。
  3. 统一捕获 `ModelError`，按错误码分别处理：顺延（429）、清除标记（session_busy、not_found），终态错误把目标转为 blocked 并带原因码。
  4. 会话软删除时，同一事务把 open goal 置为 canceled。
  5. 补充并发集成测试：finish 与 admit 交错、已删除会话、容量背压。

## 3. P1（重要，实施前修正）

### P1-1 只有 `store.finish` 结算，其他终态路径会让目标卡住

- 证据：暂停时取消 created 续轮 Run 的路径（TECH §2.1.3，CODE §1 `_pause_active_run`）不会经过 `finish`，因为 `finish` 要求 epoch（store.py:789、641-647）。现有取消、`canceled_execution` 等路径也不保证调用 `finish`。不经结算时 `last_settled_run_id` 停在旧 Run，P0-1 中的判断永远拒绝准入。
- 建议：把 settle 收敛成“任何 Run 进入 TERMINAL 时调用”的单一钩子，列出全部终态写入点并逐一接入。`_admit_one` 不再依赖 `last_settled_run_id == latest.id`，改用“最新 Run 已终态且已结算”判断（例如给 run 加 `goal_settled` 标记，或检查 last_settled_run_id ≥ 最新 Run 序号）。

### P1-2 预算用量来源不存在，轮内约束和收尾也没有落地

- 证据
  - `AgentRun` 没有 `budget_state`（entities.py:181-204），它只存在于 Checkpoint（entities.py:435），而且可能落后于最后一个模型步骤。
  - `finish` 不接收用量参数（store.py:772-786）。
  - 子任务的预算只在 `bindings.controlled` 时存在（subagent.py:239）；非受控 Run 没有 AutonomyBudget，目标用量会一直是 0。
  - 锚点里的剩余百分比只在 settle 时更新，本轮自己的消耗不计入，所以把预算用尽的那一轮看不到“收尾指令”。
  - 写入 `config_snapshot.goal.limits` 只是记录。真正约束 Run 的是 `config["budget"]=scheduler.budget`（store.py:433-434），而它属于策略指纹的校验对象（durable_service.py:488，`budget_policy_mismatch`）。
- 影响：NFR-03“含子任务、不重复”和 AC-07“当前轮输出已完成项与剩余项、超额如实记录”都无法验收。
- 建议
  1. settle 的用量改从 Run 内已接受的模型 Attempt 汇总（`agent_attempt.budget_receipt`，或经 `model_call` 关联的 `model_attempt.usage/actual_cost`）。子任务模型步骤在 subagent step 下，自然包含在内。
  2. 轮内收尾由锚点计算“目标剩余 − 本 Run 实时用量”，低于保留额度时注入收尾指令。到达硬上限时，走与 `partial_delivery` 类似的部分交付，避免以 failed 结束。
  3. 说明是否需要按目标剩余额度收紧每个 Run 的上限，以及如何与 budget 策略指纹兼容。不需要的话删除 `limits`。

### P1-3 `child_budget` 的实际语义和方案不一致

- 证据（harness/autonomy.py）
  - `child_budget` 已经是 contextmanager，finally 中已把 steps/tools/tokens/cost/attempts 并回父预算（158-208）。`children` 是 set（61）。CODE §4 中新增的 `absorb()`、`children.append`、`AutonomyBudget(available, controlled=True)` 都和现有签名不符。按附录实现会重复记账，直接违反 NFR-03。
  - 子预算上限是 `steps=min(3, …)`、`tools=min(4, …)`（189-190），而方案的委派契约 `maxSteps` 默认 8、上限 16（TECH §2.2，CODE §2）。子进程 `MAX_STEPS=8`（subagent_process.py:21）也和它不一致。
  - 第 3 个委派抛的 `AutonomyStopped` 继承 `ToolBusinessStopped`（autonomy.py:15），派发器对它是 re-raise（dispatcher.py:286-287、314-318），会直接停止父 Run。方案写的“父模型可等待后再委派”不成立。
- 建议：直接使用 `with parent.child_budget() as budget:`，删除 `absorb`。在 `admit_delegation` 中先做额度判断，超额或并发满时返回 `ToolRejected("delegation_budget_exceeded")`。统一步数口径：要么把 `child_budget` 改成按契约切片，要么把契约的上限改成 3，并同步 MAX_STEPS。

### P1-4 去掉 `model_lock` 后，子任务回传状态被并发子任务共享

- 证据：`SubAgent` 一个 Run 只有一个实例，`model_lock` 是实例属性，正是为了串行化多个子任务（subagent.py:60-63、139）。CODE §5 把暂存结果放在 `self.returned`，两个并发子任务会互相覆盖或误判 `already_returned`。另外，子进程报 `error`（含 step_limit）时，`_process` 会直接抛 `ToolRejected`（subagent.py:170-171），到不了 `_settle`，于是已经 `return_result` 的结果也会丢失。
- 建议：每次 `run()` 用局部状态对象保存 returned、progress 等。重组 `_process`，让 done、error、预算停止都汇入 `_settle`。`return_result` 被接受后让子进程结束，避免再多跑一步，也避免在 step_limit 处报错。

### P1-5 账本来源格式、跨 Run 复用和引用链没有闭合

- 证据
  - `source_access.allows` 只认 document/search_source/retail/metadata/metric_dictionary/semantic\*/transient 这些类型，其他类型一律返回 False（source_access.py:170-189）。CODE §3 写入的 `{"evidence": e}` 会让每条结论都被拒绝（`evidence_required`），复用判定也永远不会命中。
  - `transient` 来源只在同一 epoch 有效（183-186）。`read_tool_result` 跨 Run 读取时，遇到 transient 来源会直接拒绝（context_results.py:161-162）。
  - 只有超过 INLINE_TOKENS 的结果才会落盘生成 ref（context_results.py:54-55），小结果没有 `resultRef`，`body["resultRef"]` 无法取值。
  - 在 answerProtocol 2 下，复用返回的 payload 能否生成本 Run 可引用的证据，方案没有说明。如果不能，模型为了引用只能重查，AC-04 的“复用”和 AC-08 的证据门禁会互相冲突。
- 建议：账本 `source_refs` 保存解析后的来源（证据对应的 sourceDeps 或 message.source_refs 格式）。复用条目在私有正文中保存有界的结果副本，或强制保存成 context_result。明确复用命中后如何登记为本 Run 证据（例如复用 `importEvidenceId` 机制并调用 `record_sources`）。transient 来源的条目不参与复用，恢复时按 BR-09 剔除。补一条集成测试：上一轮查询 → 本轮复用 → 最终回答引用成功。

### P1-6 锚点只在 revision 变化时做权限过滤，不满足 BR-09

- 证据：TECH §2.3.6 只在“revision 变化后的首轮”执行 `drop_unauthorized`。续轮之间可能相隔数小时，期间撤权后，锚点仍会把失权来源派生的结论和数值注入模型，最终回答也可能带出这些内容。对照现有做法，`history_command` 每个 Run 都用 `allows` 过滤继承的历史（store.py:573-575）。
- 建议：每个 Run 首次装配锚点时，对 done 条目和结论按当前权限过滤，结果在 Run 内缓存（`allows` 会建立 psycopg 连接，不宜每步都做）。被过滤的条目在锚点和 GoalView 计数中都不出现。持久化的 `dropped` 级联继续只在恢复核对时执行。

### P1-7 `data_version` 取连接 `config_revision`，无法表达 AC-06 的“表数据版本变化”

- 证据：`AgentToolBinding.config_revision` 记录的是连接配置的版本（entities.py:274、journal.py:239）。更新一张销售表的数据不会改变它。AC-06 要求“更新一张销售表的数据版本后，版本变化的销售查询被重新执行”。
- 建议：交 PRD 负责人确认“数据版本”的口径。可选：(a) 用零售活动版本清单的版本加连接 revision；(b) 接受“连接或数据集版本”口径，并修改 AC-06 的 Given。方案中写清来源与比较规则。

### P1-8 工具目录随目标状态变化，审批恢复时会触发 `tool_catalog_changed`

- 证据：`freeze_catalog` 在恢复时要求工具集合与首次冻结完全一致，否则抛 `RecoveryStop("tool_catalog_changed")`（journal.py:215-228）。方案让 `catalog.py` 在目标活跃时隐藏 `todo_write`（D10），goal_progress/goal_update 也按目标状态出现。`goal_propose` 获批后会在同一个 Run 中恢复执行，此时目标已活跃，注册表就变了。这正是目标确认的主路径。
- 建议：goal\_\* 工具和 todo_write 在注册表中始终存在，用 `authorize` 或 visible_names 按目标状态控制可调用性。更简单的做法是删除 D10：保留 todo_write，账本待办只在目标面板显示（FR-08 只要求面板可见）。

### P1-9 主流折叠（D9）会卡住 SSE 游标；建议删除 D9

- 证据
  - events 流只在 `visible` 非空时推进 `cursor`（api/sessions.py:263-294）。如果一批 16 行全是被折叠的子任务节点，过滤后为空，游标不动，就会反复读同一批数据；终止条件 `cursor == run.projected_seq` 也永远达不到（298-303）。`project_sources` 就是为了这个问题，用状态包代替被隐藏的事件（source_access.py:449-454）。下钻流中被过滤的比例更高，问题更严重。
  - CODE §7 假设包结构是 `event.view.type/node`，实际结构是 `view.upsert` 的 `payload.kind/value`。
  - 兼容性表也承认，新 Agent 配旧 Web 时会丢失子任务过程。
- 影响：NFR-01 的续接和去重失效，可能出现无限轮询。
- 建议（MVP）：删除 D9，主流保持不变，Web 按 `parentId` 把后代节点归到子任务卡片里（FR-04 的折叠本来就是 UI 行为）。下钻接口改成 JSON 快照 `GET /runs/{run_id}/children/{child_id}`，从 `read_run` 的 public_view 过滤子树，走同一套 `project_sources` 与 `visible_response`，满足 AC-02 的错配 404 和越权校验；实时更新复用主流。如果坚持保留独立的过滤流，过滤后必须用状态包推进游标，并补“整批被过滤”的测试。

### P1-10 续轮 Run 的执行入口和上下文继承没有定义

- 证据
  - 非调度模式下，Run 只能由 API 请求调用 `service.schedule(run_id, executor, …)` 启动（durable_service.py:509-512；sessions.py:122-123），而 executor 依赖 request 构建（sessions.py:447）。maintain 里没有 executor，续轮 Run 会一直停在 created。
  - 用户提交时需要从 Web 获取 `run_environment`（sessions.py:111），缺失时 `promptEnvironment={}`，`facts_for` 的行为未经验证（journal.py:109-116）。
  - `owner_context(session)` 的来源没有写明。
- 建议：写明续轮只在调度或分布式模式下可用，非调度模式下拒绝启用目标（`goal_propose` 返回可判定原因）。`TrustedContext`、`promptEnvironment`、`constraints` 从最近结算的 Run 的 `config_snapshot` 继承；执行开始时现有的 `access.check` 会复核当前权限（durable_service.py:559）。

## 4. P2（建议）

1. 账本写入需要 epoch 防护：goal_progress、自动入账和交付合入都在 `store.lock` 加 `require_epoch` 的事务中执行，锁顺序 session → run → goal，防止接管后旧执行者继续写账本。
2. 续轮消息的通道：`submit_in_transaction` 固定写 conversation 的 user 消息（store.py:486-503）。改用 context 通道会违反 `ck_agent_message_channel`（entities.py:216-221），也会让 `history_command` 中 `len(messages)==2` 的判断失效（store.py:563-567）。建议保留 conversation 的 user 消息，公开文本使用 C-09 标记，完整指令放进锚点。
3. 兼容 977：新的 `evidenceIds` 只来自 findings，`answer` 被截到 1000 字，和现在返回全部子任务证据、完整答案的行为不同（subagent.py:163-169）。建议 `evidenceIds` 继续包含子任务路径下的全部证据，findings 只做逐条剔除。在途的 0018 Run 重放时，`host_result` 摘要会对不上（journal.py:462-470），发布清单应要求“排空含 subagent step 的活动 Run”，不能写“允许接管”。
4. 委派快照只在首次 `subagent_begin` 时计算并持久化，重放直接读取已存快照，避免引用状态变化引发 `workflow_replay_mismatch`（journal.py:528-557）。
5. goal\_\* 工具明确排除出 `child_tools`：标记 local_effect 或显式排除，满足 BR-08，防止子任务直接写账本。
6. `_settle` 读取 `answer_evidence` 时必须处于子任务的 `execution_scope` 内。
7. settle 产生的状态迁移，以及续轮受理后 rounds 的变化，要把 `goal` 视图写进同一事务的 outbox，保证面板实时更新。
8. 前置条件：目标能力依赖 contextRuntime 且 answerProtocol 为 2，否则没有证据，结论写不进去。未满足时不暴露 `goal_propose`。
9. 恢复目标时，已暂停的旧 revision 续轮 Run 是继续执行（锚点会取到新版本）还是作废后回退轮数，需要写明，并与 BR-08、AC-07 的措辞对齐。
10. 附录准确性：`GoalError.capacity` 改为按 `ModelError` 错误码处理；`subtree_filter` 的包结构；`AutonomyBudget` 构造参数。
11. AC-05 对比实验：三种策略只能映射到现有通用配置（如第 24 课的 optimization 开关和 goal 开关），生产 Runtime 不新增策略分支（临时演示规范）。
12. `uq_agent_run_goal_round`、`request_id` 幂等和目标行锁三重防护，成本低，可以保留。

## 5. 数据库交付核对（agent/main 0018 → 0019）

- 符合：owner、真源路径、`down_revision`、派生 SQL（`pnpm db:sql -F agent` 已存在，见 apps/agent/db/main/README.md:17）、发布顺序、备份恢复、PostgreSQL 验收矩阵都已登记，新增内容只有新表和可空列。当前基线为 0018（README.md:3、27）。
- 需补：
  1. 在 apps/agent/db/main/README.md 新增 0019 章节，登记 owner、from/to、源、SQL 与恢复，并更新 bootstrap 和 manifest 的版本描述。
  2. `require_database_revision`（readyz）的目标版本同步改为 0019。
  3. 运行账号授权写明表和列级 UPDATE 的范围；runtime 不授予 DELETE。
  4. 处理会话软删除与 open goal 的关系（见 P0-2）。
  5. docs/database-migrations.md 中“版本 0017”的修正保留在 T1。

## 6. AC 覆盖与 MVP 减法

| AC    | 结论                                                                                                                                  |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------- |
| AC-01 | 依赖 P1-3、P1-4；并发路径（root 批次是否 parallel，还是只走 workflow）需写明，`ToolBatchPolicy` 默认 serial（tools/contracts.py:110） |
| AC-02 | 依赖 P1-9 的方案                                                                                                                      |
| AC-03 | 覆盖                                                                                                                                  |
| AC-04 | 依赖 P1-5                                                                                                                             |
| AC-05 | 覆盖，注意 P2-11                                                                                                                      |
| AC-06 | 依赖 P1-5、P1-6、P1-7                                                                                                                 |
| AC-07 | 依赖 P0-1、P0-2、P1-1、P1-2、P1-10                                                                                                    |
| AC-08 | 覆盖，依赖 P1-5 的证据可访问性                                                                                                        |
| AC-09 | 依赖 P1-3 的拒绝语义                                                                                                                  |
| AC-10 | 依赖 P1-4                                                                                                                             |

建议删除或延后：D9 主流折叠（改为 Web 分组加 JSON 子树快照）、D10 隐藏 todo_write 和 plan 投影。保持现状：程序化完成核验、冲突检测、report_progress、账本条目上限、子任务作为父 Run 内的 step 子树（D1）。

## 7. 待决问题

1. 用户插话和修改目标的抢占语义（P0-1）由 andy 选择方案。
2. “数据版本”口径（P1-7），需要 PRD 负责人确认。
3. 非调度模式是否支持目标续轮（P1-10）。
4. 子任务步数口径：3 步还是按契约 8～16 步（P1-3）。

## 8. 下一步

由方案作者按 P0-1、P0-2 及 P1-1～P1-10 修订 TECH_DESIGN 和 CODE_DESIGN 后复评。测试缺口：AC-07 的抢占与并发、AC-06 的跨轮权限，建议编排者安排 my-create-test-plan。
