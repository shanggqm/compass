# 983 业务测试计划

[返回需求简报](../BRIEF.md)

> Profile：standard。模板：[项目业务测试模板](../../_templates/TEST_PLAN.tmpl.md)。有效需求来源：[PRD](../spec/PRD.md) 2026-11-24 修订版 AC-01 至 AC-10；技术实施范围：[TECH_DESIGN](../design/TECH_DESIGN.md)、[PROMPT_DESIGN](../design/PROMPT_DESIGN.md)。本计划在开发开始时建立，本次执行结果见 [验收记录](TEST_REPORT.md)。

## 范围与依据

本次验证目标确认与自动推进、跨 Run 账本与恢复、父子双向交接、并发子任务和 Chat 授权下钻，包含 `agent/main` 从 `0018` 到 `0019` 的迁移。数据库验证按[项目规范](../../../docs/database-migrations.md)执行。跨会话 Memory、独立模型 Verifier、子任务派生孙任务和长于父 Run 存活不在当前 PRD 范围内。

开发检查负责类型、契约、组件、迁移和确定性并发断言；业务验收负责从用户入口操作并核对最终状态、展示和真实服务链路。外部模型使用受控响应的用例会明确标注；真实模型另做提示词行为评测。

| PRD AC | 业务用例            | 主要验证方式                        |
| ------ | ------------------- | ----------------------------------- |
| AC-01  | TC-01、TC-09        | Agent 并发集成 + 浏览器             |
| AC-02  | TC-02、TC-03        | Agent API / BFF + 浏览器            |
| AC-03  | TC-01、TC-03        | Web 组件 + 浏览器                   |
| AC-04  | TC-06               | 真实 PostgreSQL、受控模型、查询计数 |
| AC-05  | TC-11               | 离线三策略实验 + 每步上下文断言     |
| AC-06  | TC-07               | 真实 PostgreSQL、权限及连接版本变更 |
| AC-07  | TC-04、TC-05、TC-09 | 真实 PostgreSQL、受控模型 + 浏览器  |
| AC-08  | TC-08               | Agent 集成 + 目标面板               |
| AC-09  | TC-10               | 子进程输入捕获、授权与预算集成      |
| AC-10  | TC-01、TC-10        | 持久重放、交付与证据归属集成        |

## 已有测试覆盖

以下为可复用入口。当前执行结果统一记录在 [TEST_REPORT](TEST_REPORT.md)，新增 Goal、委派与三策略实验入口见文末。

| 需求 / 业务行为                 | 已有测试或入口                                                                                                                                                                                         | 当前覆盖范围                                                                       | 仍需补验的部分                                        |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------- | ----------------------------------------------------- |
| PostgreSQL 迁移、角色、备份恢复 | [test_database_tools_integration.py](../../../apps/agent/tests/test_database_tools_integration.py)                                                                                                     | 原生 / SQL、空库、旧数据、元数据一致性、角色隔离、恢复；`targets` 为每例创建隔离库 | `0018 → 0019` 的旧 Run 保留、新表约束、无 DELETE 权限 |
| 持久运行、审批、暂停与重放      | [test_durable_runtime_integration.py](../../../apps/agent/tests/test_durable_runtime_integration.py)、[test_run_scheduler_integration.py](../../../apps/agent/tests/test_run_scheduler_integration.py) | 真实 PostgreSQL 与 Redis；受控执行器                                               | Goal 结算、自动续轮、插话和双副本竞争                 |
| 当前权限与事件续接              | [test_runtime_visibility_integration.py](../../../apps/agent/tests/test_runtime_visibility_integration.py)                                                                                             | 活动 SSE 重查权限、权限收紧与授权失败                                              | 子树快照、错配父子 ID、前端已加载详情清除             |
| 子 Agent 与旧工作流             | [test_dynamic_workflow.py](../../../apps/agent/tests/test_dynamic_workflow.py)、[test_workflow_runner.py](../../../apps/agent/tests/test_workflow_runner.py)                                           | 旧 prompt 委派、保存工作流、取消                                                   | tasks 并发、结构化 final、单次交付、预算切片          |
| 上下文与计费恢复                | [test_context_optimization_integration.py](../../../apps/agent/tests/test_context_optimization_integration.py)、[test_context_optimization.py](../../../apps/agent/tests/test_context_optimization.py) | 原件、压缩、来源权限、已接受调用用量恢复                                           | 目标锚点、跨 Run 账本与可引用的复用证据               |
| BFF 与 durable 客户端           | [analysis-run-bff.test.ts](../../../apps/web/tests/domain/analysis-run-bff.test.ts)、[durable-controls.test.ts](../../../apps/web/tests/durable-controls.test.ts)                                      | 既有网络契约与控制行为                                                             | 三个新增端点的 Schema、状态码、no-store 与错误恢复    |
| 真实 Chat 链路                  | [context-delivery.spec.ts](../../../apps/web/tests/e2e/context-delivery.spec.ts)、[production-runtime-live.spec.ts](../../../apps/web/tests/e2e/production-runtime-live.spec.ts)                       | Web 登录、真实 Agent 消费者、PG、Redis、模型受控上游、刷新历史                     | Goal 卡、目标面板、自动轮和子任务下钻                 |
| 过程布局                        | [execution-process.spec.ts](../../../apps/web/tests/e2e/execution-process.spec.ts)                                                                                                                     | 既有过程展示                                                                       | 子任务独立折叠、加载 / 失权 / 重试状态及 390 px 窄屏  |

## 执行环境与入口

- 从 Compass 根目录运行命令，保留当前用户未提交修改。执行报告记录提交 SHA、相关未提交差异、执行时间、环境和实际命令。
- Agent 集成测试使用 `AGENT_TEST_ADMIN_URL`、`AGENT_TEST_DATABASE_URL`、`AGENT_TEST_MIGRATION_DATABASE_URL`，库名必须为 `compass_agent_test_*`。沿用现有 runtime / migrator 独立角色；`targets` 只创建和清理自己的临时库。PostgreSQL 备份工具必须与 17 兼容，容器工具可用 `AGENT_TEST_PG_CONTAINER` 指定。
- Web 用 `WEB_TEST_ADMIN_URL`、`WEB_TEST_DATABASE_URL`、`WEB_TEST_MIGRATION_DATABASE_URL` 和 `WEB_TEST_PASSWORD` 创建隔离教学账号；库名为 `compass_web_test_*`。凭据通过私有运行环境提供，不写入本计划或报告正文。
- [Playwright 配置](../../../apps/web/playwright.config.ts)默认端口 8110，`WEB_TEST_PORT` 可选择空闲端口；自启测试 Web 使用隔离构建目录。已有隔离服务采用 `WEB_TEST_EXTERNAL_SERVER=true`。执行前与主 Agent 约定唯一端口、Redis、账号和服务所有者。
- 受控上游可复用 [context_e2e_app.py](../../../apps/agent/tests/context_e2e_app.py) 的模型传输替换，保留真实应用服务；上游端口由 `COMPASS_E2E_MODEL_PORT` 指定。现有 [context_runtime_provider.py](../../../apps/agent/tests/context_runtime_provider.py) 尚无 Goal / tasks / report_to_parent 场景，需实施流程补充独立测试响应。确定性业务阶段使用门闩控制顺序，避免靠固定 sleep 争抢插话窗口。
- Goal 场景开启受控自主、持久调度消费者和回答协议 2，登记实际 Provider 的模型预算档案，发布包含新工具与提示片段的 PromptOps 清单。每个用例保存独立 Session；用量比较包含主模型、辅助调用和两个子任务的已接受调用。
- 浏览器优先复用项目 Playwright 或宿主浏览器能力。桌面采用 1440 × 1000，窄屏采用 390 px；保存必要截图、请求状态和最终业务结果。只修改本次隔离夹具的权限、数据版本和网络状态，测试后恢复。

已确认存在的入口如下；新增 983 文件落地后补入具体筛选命令：

```bash
pnpm test:setup -F web -- --fixture teaching
pnpm contracts
pnpm contracts:check
pnpm db:sql:check -F agent
pnpm lint -F agent
pnpm typecheck -F agent
pnpm test -F agent
pnpm test:integration -F agent
pnpm lint -F web
pnpm typecheck -F web
pnpm test -F web
pnpm test:e2e -F web -- --list
pnpm check
pnpm build
```

上述命令中的集成 / E2E 入口必须先注入隔离配置。`pnpm test:integration -F agent` 包含已有 Sandbox 等集成范围，执行前核对相应依赖；未就绪时先运行本次相关测试文件并单列未覆盖范围。纯列举用例只用于确认入口。完整迁移、两次 seed 与 smoke 仍按 TC-12 实际执行。

## 待验业务用例

### TC-01：并发子任务各自推进，父回答后仍可回看

- 关联需求：AC-01、AC-03、AC-10。
- 前提与数据：月度经营分析的库存、转化分支有独立可读样本；受控上游分别输出可区分的进度。库存调用持有门闩，转化先完成一个步骤；另一个场景让转化失败。
- 操作步骤：
  1. 从 Chat 提交普通分析问题，模型在一次 `sub_agent.tasks` 中委派两个分支。
  2. 展开库存卡，观察转化继续推进，随后让两者交错完成。
  3. 在一个子任务完成、另一个失败时检查父过程；等父回答开始，再重新展开父过程和两张卡。
- 预期结果：两任务执行区间重叠，卡片及详情归属稳定；库存详情只含其获准子树。子任务结束更新自己的状态与摘要，父回答阶段触发一次自动折叠。重新展开可见完整授权轨迹、失败原因；任务名称、输入范围和预算可核对。
- 验证方式与证据：集成测试记录开始 / 结束区间、childRunId 和步骤归属；浏览器保留交错、父回答和回看三种状态。

### TC-02：下钻、断线与撤权保持当前授权范围

- 关联需求：AC-02、NFR-01。
- 前提与数据：同一父 Run 两个子任务；另外准备不同父 Run 和不同租户 / 工作区的任务，结果中包含可见摘要和受限字段。
- 操作步骤：
  1. 展开一张卡，记录基线，断开并恢复浏览器连接，再刷新会话。
  2. 用本用户请求错配父子 ID；用另一租户用户请求原 childRunId。
  3. 卡片展开时撤销详情访问权限，等待已有权限复查 / 重连，再读取快照；另测读取期间访问校验服务暂时不可用。
- 预期结果：合法续接按节点 ID 与 revision 去重；错误归属和越权请求无轨迹。权限收紧后已加载详情被清除并显示「此执行过程不可访问」，响应无受限字段；临时故障可恢复。主流游标继续前进，不因子树筛选停滞。
- 验证方式与证据：Agent API 和 BFF 用例；保存脱敏响应字段、游标、浏览器清除后的可见内容。
- 清理：恢复隔离账号权限与网络状态。

### TC-03：键盘与窄屏完成子任务查看和错误恢复

- 关联需求：AC-01 至 AC-03、UI-01、项目 UI 规范。
- 前提与数据：TC-01 会话；浏览器分别设为桌面和 390 px。
- 操作步骤：用键盘展开 / 收起父过程与子任务；分别观察加载、暂无轨迹、临时失败、重试成功、不可访问；父回答自动折叠时焦点在过程内，随后再次展开。
- 预期结果：控件可聚焦，展开状态和关联区域正确，焦点在折叠后保持可操作；四态文案符合 PRD。卡片位置稳定、列和控件对齐，无页面横向溢出，代码长行局部滚动。
- 验证方式与证据：组件回归和真实页面操作；保存两种宽度及错误恢复截图。

### TC-04：目标草案只有本人确认后生效

- 关联需求：AC-07、FR-05、BR-08。
- 前提与数据：无未结束目标的新会话，模型提出 3 轮预算、两条验收项和范围约束。
- 操作步骤：
  1. 查看目标卡中的目标、验收项、约束、Token / 费用 / 轮数；先拒绝，另一次修改后确认。
  2. 确认后观察目标面板与首次自动续轮；在两浏览器标签中读取同一目标版本，先后发起互相竞争的操作。
  3. 取消目标时先用 Esc 退出，再打开并确认取消；刷新会话查看历史。
- 预期结果：未确认时没有有效目标或自动轮。确认后存在唯一未结束目标，自动轮标明「继续推进目标 · 第 N 轮」。旧版本操作显示冲突并可刷新重试。取消保留结论和产物，停止后续准入；取消弹层及按钮支持键盘。
- 验证方式与证据：浏览器从目标卡到面板的操作；API / DB 核对唯一目标、版本和自动 Run。

### TC-05：执行中插话、排队插话与主动暂停

- 关联需求：AC-07、NFR-02、FR-06。
- 前提与数据：Goal 自动推进；用确定性门闩分别控制「已排队未领取」「自动第 2 轮执行中」「终态已保存但 outbox 未投递」「准入事务之前」。
- 操作步骤：
  1. 第 2 轮执行时发送「修改目标：只看华东」，确认新目标卡，观察插话 Run 及后续自动轮。
  2. 在排队窗口发消息，随后让消费者继续，观察被取消轮次之后的首次自动 Run。
  3. 在准入前主动暂停，等待扫描，再手动恢复；运行中暂停后恢复原 Run。
  4. 双副本同时扫描，交错执行 finish、动作请求、Goal 扫描；同一 requestId 重发。
  5. 节点 revision 1 已发布时写入 revision 2 并暂缓投递，再取消 Run；分别验证 revision 2 仍运行与已完成两种状态。
- 预期结果：发送入口可用并说明会停下当前轮；插话先执行，结束后自动继续，后续上下文使用新契约。未执行即取消的自动轮不计轮数，后续仍能生成可执行 Run。主动暂停保持暂停直至手动恢复；恢复无重复执行。双副本无重复准入、无死锁，重复请求幂等，未投递终态不准入下一轮。取消按最新已落库节点版本生成终态，保留已完成节点及私有详情，投递不发生版本冲突。
- 验证方式与证据：真实 PostgreSQL 竞争测试、受控模型调用记录及浏览器插话路径；核对 Run 身份、轮次、目标 revision、终态与 outbox。

### TC-06：跨轮恢复从待办继续并引用已复用查询

- 关联需求：AC-04、BR-07。
- 前提与数据：销售阶段已完成，账本记录查询、带来源结论和营销待办；连接版本未变且权限有效。分别覆盖小结果和需要原件分页的大结果。
- 操作步骤：暂停并恢复；新一轮从营销待办开始，对销售发出相同查询；最终回答引用销售与营销来源。再次刷新、回看来源。
- 预期结果：阶段结论能回到原始结果；销售查询标记复用且底层执行次数不增加；复制到本 Run 的证据可通过最终回答门禁。不同参数、连接、版本或 transient 来源不误复用。复用计数与实际命中一致。
- 验证方式与证据：真实 PostgreSQL、受控模型集成；保留工具实际执行次数、锚点、已接受证据及最终引用结果。

### TC-07：恢复核对失权、数据版本、写入与冲突

- 关联需求：AC-06、BR-09。
- 前提与数据：目标已经生成报告产物；库存明细与派生结论有依赖关系；销售查询使用独立连接；保留同一 factKey 的两个可核对数值来源。
- 操作步骤：
  1. 暂停目标，撤销库存来源权限并递增销售连接修订号，连续两次恢复。
  2. 查看新模型上下文和目标面板，读取原报告产物，核对实际生成次数与摘要。
  3. 子任务交回与已有值冲突的结论；查看待核状态和两方引用，随后补充明确裁决结论。
- 预期结果：两次恢复均不重做已完成写入；失权明细及派生内容从上下文和可见内容中移除。销售转待补查并重新执行；冲突保留两方来源，未经裁决不完成目标，明确裁决后可解除待核。
- 验证方式与证据：真实 PostgreSQL；核对来源权限、连接 revision、账本状态、产物 hash 和工具执行次数。
- 清理：恢复本例权限与连接配置；保留隔离运行记录。

### TC-08：完成申请打回、补救和无进展停止

- 关联需求：AC-08、FR-08。
- 前提与数据：两条验收项只有一条有有效证据，另有必需待办；另准备独立无进展目标。
- 操作步骤：申请完成，查看缺口与补救待办；补证据并完成待办后再申请。分别尝试旧 revision、外来证据、失权证据和未处理冲突；让独立目标连续 3 轮无实质账本变化。
- 预期结果：第一次申请返回明确缺口并保持进行中，补救项可见；条件满足后完成且停止续轮。旧版本和不可用证据被拒绝。反复写相同内容不刷新实质进度；第 3 个无进展轮结束后受阻，插话中断轮不计无进展。
- 验证方式与证据：Agent 集成和目标面板状态；记录完成申请、补救项与轮次状态。

### TC-09：目标预算包含并发子任务，预算用尽可交付已有结果

- 关联需求：AC-07、NFR-03、AC-01。
- 前提与数据：已批准 3 轮目标；两子任务有可预测 Token / 费用回执；用独立场景分别触发轮数、Token、费用上限与连续 3 轮运行失败。
- 操作步骤：并发执行两个子任务并完成结算，模拟一次重放；让预算接近保留额度后继续到硬上限；再尝试扫描续轮。失败场景中让单个目标遇到容量背压或会话删除，同时执行无关目标。
- 预期结果：用量为所有已接受调用之和，每次只计一次；显示超额实际值。收尾包含已完成项、剩余项和下一步，用尽后不再准入。连续失败受阻；容量背压顺延、删除会话取消目标，副本就绪和无关任务保持正常。
- 验证方式与证据：真实 PostgreSQL 和受控模型；逐次回执求和、目标视图、模型收尾内容、就绪状态与无关任务结果。

### TC-10：委派最小上下文与单次结构化交付

- 关联需求：AC-09、AC-10、FR-09、FR-10。
- 前提与数据：父任务可读库存与销售；库存任务只引用库存待办与一份销售结论；并发任务使用不同 summary、factKey 和证据。
- 操作步骤：
  1. 检查子进程实际模型请求，分别提交允许、工具越权、连接越权、输入越权、预算超额的 tasks。
  2. 让一个子任务 progress 后 final，另一个未调用 final 就自然结束；单独模拟 step_limit、工具失败、取消和外来 evidenceId。
  3. 重放已持久化交付；再执行保存工作流中的旧 prompt 调用。
- 预期结果：子任务只获得任务包和获准工具，不含父历史；整批校验失败时未启动任何子任务并向父任务返回可判定原因。progress 只更新摘要；final 结束子任务并只合入一份交付，两个任务的返回状态互不覆盖。未结构化自然结束为部分完成且保留 answer；外来引用剔除并标注；重放无第二份交付。旧 prompt 的 answer 和 evidenceIds 仍可被工作流使用。
- 验证方式与证据：输入快照、模型请求、持久 Attempt / delivery、父观察与旧工作流回归。

### TC-11：三种上下文保留策略对比

- 关联需求：AC-05。
- 前提与数据：同一月度任务、固定数据版本、相同验收项与模型设置；降低窗口预算以实际触发压缩。实验资产遵循 [Showcase 规范](../../../examples/showcases/README.md)。
- 操作步骤：分别运行关闭优化的滑动窗口、开启优化的阶段摘要、优化加 Goal 账本三种配置；捕获每次模型调用上下文与查询记录；生成离线报告。
- 预期结果：报告有完成率、关键事实保留、重复查询次数、Token 和实际触发情况。Goal 策略的每次调用均保留目标、全部约束、未完成待办、失败及引用；摘要未回写目标或账本。所有结果按实际记录报告，不预设某策略必须领先。
- 验证方式与证据：实施提供的独立实验命令与 `work/` 报告；单元测试检查不可压缩锚点及账本内容。生产 Runtime 不引入案例分支，运行隔离测试核对默认清单 / 导入 / wheel。

### TC-12：数据库升级、权限与恢复

- 关联需求：技术方案 §4、项目数据库验证要求。
- 前提与数据：隔离 PostgreSQL 17；0018 原生库中有会话、已结束 Run、产物和加密内容，保留迁移前备份与对应私有状态密钥。
- 操作步骤：空库分别执行原生迁移和 bootstrap SQL；带数据旧库分别执行原生和 `0018__0019.sql` 升级；检查结构及旧数据，重跑受管入口并尝试错误起点；以 runtime 角色读写新表和尝试 DELETE；恢复升级前备份到另一独立 0018 库。运行两次 seed 和应用 smoke。
- 预期结果：两种交付结构一致，模型与迁移一致；旧记录保留，错误起点拒绝，重跑无额外变更；runtime 可 SELECT / INSERT / UPDATE 且 DELETE 被拒绝。备份恢复后旧数据与私有内容可读；迁移和启动责任保持分离。
- 验证方式与证据：复用数据库集成测试并补 0018→0019 用例；`pnpm db:sql:check -F agent`、`pnpm db:migrate -F agent`、两次 `pnpm db:seed -F agent`、`pnpm smoke`。命令显式连接隔离服务，保留实际版本、schema 比较、权限拒绝与恢复结果。

### TC-13：真实模型理解目标与委派规则

- 关联需求：技术方案 T5、T6，提示词设计；补充 AC-01、AC-07、AC-09 的真实模型行为。
- 前提与数据：隔离测试账号、已发布新 PromptOps 清单、可用模型配置与授权数据；用量受本次目标卡限制。
- 操作步骤：依次提交一次查询即可回答的问题、多阶段持续任务、两个独立分支任务、后一个分支依赖前一个结果的任务；在卡片确认后观察模型如何记录、委派及处理 partial。
- 预期结果：简单问题直接处理；多阶段任务先提出可审阅目标卡；独立分支可在同一次 tasks 并发，依赖分支先获取前置结果再委派。回传 findings 有合法来源，partial 的缺口得到处理或明确记录。
- 验证方式与证据：记录实际 Provider 配置版本、PromptOps 清单、输入、工具轨迹及最终结果；按行为逐项判定。若模型调用失败，保留失败状态和原因，安排修复后复跑。

## 待确认与补齐事项

1. 实施流程补齐 Goal / 账本 / Handoff 的开发测试，以及真实链路可控上游和 983 浏览器场景；本计划不代写生产实现或常规单元测试。
2. 排队自动轮取消并回退计数后，必须验收下一轮仍可执行；显示轮次回退与准入幂等身份需一并验证。该项已交实施者核对。
3. 跨 Run 查询复用必须走到回答引用成功，只有缓存命中回执仍保留待验。
4. 执行前由主 Agent 登记隔离环境的端口、账号、凭据引用与服务所有者，再调度本计划；当前尚未启动或修改服务，也未声称浏览器连通。
5. PRD 中默认预算、连接修订号的数据版本口径及插话行为按当前技术方案验收；本计划保持 PRD 已列的范围。实质业务选择若发生调整，只复核受影响用例。

## 当前执行入口

本次已建立 PostgreSQL 17.11 隔离实例，监听 `127.0.0.1:55439`，数据目录 `work/983-pg`。测试配置为私有文件 `work/983-postgres-test.env`；该文件仅保存本次实例的 `AGENT_TEST_*` 连接，权限 0600。数据库套件使用 `targets` 创建并清理自己的临时库，实例保留供主线程继续验收。未改动日常 PostgreSQL 或 Docker 容器。

### 本次新增验收入口

- [目标 Runtime 集成](../../../apps/agent/tests/test_goal_runtime_integration.py)：TC-05 至 TC-09；真实 PostgreSQL、生产调度器/账本/回答门禁；包含大结果、UNION 授权、权限撤回和跨轮引用。
- [双向交接集成](../../../apps/agent/tests/test_goal_handoff_integration.py)：TC-09、TC-10；真实子进程并发与重放、最小输入归属、固定工具及搜索范围、实际副作用文件计数、长回执和运行故障隔离。
- [三策略离线入口](../../../examples/showcases/lesson25_context_strategies/README.md)：TC-11；相同样本及输入上限，复用生产上下文优化器、摘要器与 Goal/Ledger/anchor，模型适配为确定性 fixture。

```bash
uv run --env-file work/983-postgres-test.env --locked --package compass-agent pytest apps/agent/tests/test_goal_runtime_integration.py apps/agent/tests/test_goal_handoff_integration.py -q --tb=short
uv run --env-file work/983-postgres-test.env --locked --package compass-agent python examples/showcases/lesson25_context_strategies/runner.py --output work/983-context-strategies.json
```

新增 [test_goal_database.py](test_goal_database.py) 补充 TC-12 的 `0018 → 0019` 原生 / SQL、旧 Run 保留、Goal 约束、运行账号权限及独立 0018 备份恢复：

```bash
uv run --env-file work/983-postgres-test.env --locked --package compass-agent \
  python -m pytest mai-specs/983-lesson-25-long-task-context/test/test_goal_database.py -q --tb=short
uv run --env-file work/983-postgres-test.env --locked --package compass-agent \
  pytest apps/agent/tests/test_database_tools_integration.py -q --tb=short
```

实际结果与待复验项见 [TEST_REPORT.md](TEST_REPORT.md)。
