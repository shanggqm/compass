# 983 验收记录

[测试计划](TEST_PLAN.md) · [需求简报](../BRIEF.md)

> 日期：2026-11-24。代码基线：`588ee4f5db62f534e159c186caab10b496c183cc` 加当前 Feature 983 实现。本次 Goal、子任务、迁移、离线策略实验、完整浏览器验收、既有 Runtime 回归及所列工程检查均已通过。

## 环境与范围

本次使用 Homebrew PostgreSQL 17.11，隔离集群 `work/983-pg` 监听 `127.0.0.1:55439`。配置文件 `work/983-postgres-test.env` 权限为 0600；凭据仅保存于私有文件。`targets` 为各例创建 `compass_agent_test_*` 临时数据库，结束时清理。备份恢复使用本机 PostgreSQL 17 工具。

初始日常 PostgreSQL `127.0.0.1:55432` 拒绝连接，Docker Desktop 镜像读取出现 containerd blob I/O 错误；测试子任务使用上述独立实例，未操作日常容器、占用 8110–8139 或启动浏览器。主线程后续恢复日常 Docker 服务、重启 Agent workers，将 PromptOps 由 69 发布至 70（六项本需求资产），保留其余资产与数据卷；随后再次通过 `smoke --memory`。

Goal 集成使用生产 Controller、Scheduler、Journal、Ledger、Dispatcher、来源鉴权和回答门禁；本地 publisher 驱动真实持久 outbox 投影。子任务执行真实独立 Python 进程；模型回答与 BFF 授权传输采用明确受控适配。Python effect 回归采用受控执行适配，实际写入 PNG、登记产物并核对文件 hash。

## 已执行结果

| 验证范围                     | 入口                                                                                                                       | 本次结果                                                       | 日志 / 报告                                                                                                                                                   |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 数据库既有回归               | `pytest apps/agent/tests/test_database_tools_integration.py -q --tb=short`                                                 | 35 通过，42.14 s                                               | `work/983-database-tests.log`                                                                                                                                 |
| 0018→0019 增量及恢复         | `python -m pytest mai-specs/983-lesson-25-long-task-context/test/test_goal_database.py -q --tb=short`                      | 3 通过，5.23 s                                                 | `work/983-goal-database-tests.log`                                                                                                                            |
| Goal / Handoff 业务边界      | `pytest apps/agent/tests/test_goal_runtime_integration.py apps/agent/tests/test_goal_handoff_integration.py -q --tb=short` | 29 通过，20.27 s                                               | `work/983-goal-runtime-tests.log`                                                                                                                             |
| 既有 Runtime 完整回归        | durable / scheduler / visibility / limits / context 九个测试文件                                                           | 87 通过，139.25 s                                              | `work/983-runtime-regression-final.log`                                                                                                                       |
| 既有 Runtime 失败项复验      | 五个原测试文件中的 8 个失败用例（包含于上述 87 项）                                                                        | 8 通过，16.98 s                                                | `work/983-runtime-eight-recheck.log`                                                                                                                          |
| 三策略离线实验               | `python examples/showcases/lesson25_context_strategies/runner.py --output work/983-context-strategies.json`                | 1 通过，15.63 s                                                | `work/983-context-strategies.log`、`work/983-context-strategies.json`                                                                                         |
| Showcase 默认清单 / 导入隔离 | `pytest apps/agent/tests/test_showcase_isolation.py -q`                                                                    | 5 通过，2.55 s                                                 | `work/983-showcase-isolation.log`                                                                                                                             |
| 浏览器完整业务链             | `long-task-goal.spec.ts`，开启 `COMPASS_GOAL_REAL_E2E=true`                                                                | 5 通过，4.3 min                                                | `work/983-e2e-final.log`                                                                                                                                      |
| 真实模型首轮决策             | 普通问题、目标提议、独立 / 依赖任务、缺口、冲突、预算收尾                                                                  | 7 通过                                                         | `work/983-real-prompt-report.json`、`work/983-real-prompt.log`                                                                                                |
| 日常应用入口                 | Agent 迁移、两次 seed、`smoke --memory`                                                                                    | 全部通过                                                       | `work/983-local-migrate.log`、`work/983-local-seed-first.log`、`work/983-local-seed-second.log`、`work/983-local-smoke.log`、`work/983-local-smoke-final.log` |
| 新增及修订测试质量           | 定点 Ruff、Ruff format、Pyright；文档 Prettier 与 `git diff --check`                                                       | Ruff / format、文档格式 / 链接、diff 检查通过；Pyright 0 error | `work/983-goal-typecheck.log`                                                                                                                                 |

上述 Python 命令均从 Compass 根目录执行，前缀为 `uv run --locked --package compass-agent`；数据库与实验另加 `--env-file work/983-postgres-test.env`。数据库及实验命令完整形式见 [测试计划](TEST_PLAN.md) 和 [实验入口](../../../examples/showcases/lesson25_context_strategies/README.md)。

### Goal、恢复与预算

- 终态 outbox 未发布时不准入下一轮；双线程同时扫描只创建一个自动 Run。
- 排队轮被插话取消后归还轮次，用户消息结束后创建新的 Run / requestId；主动暂停保持 `user_paused`，旧 revision 操作被拒绝。
- 连续三个无进展轮与三个失败轮分别停止；Token、费用、轮数预算达到上限后无额外续轮，超额如实呈现。
- 父调用和并发子调用的已接受用量只结算一次；重放不追加费用；目标完成后的最终模型调用仍进入结算。
- 完成申请缺项时生成幂等补救待办；冲突保留双方记录，经显式裁决后可以完成。
- 32 个待办、12 个失败记录及契约约束保留在锚点中；接近预算提示收尾，硬上限停止。
- 真实租户 backlog 满额只延迟该租户目标，另一租户可续轮；实际删除 Session 取消其目标，其他目标继续运行。
- v2 运行在节点 revision 1 已投递、revision 2 已落库但尚未投递时取消：运行节点生成 canceled revision 3，已完成节点保留 completed revision 2。原 revision 2 私有视图、标题和详情保持，无 `delivery_conflict`。

### 查询、来源与副作用

- 小结果跨 Run 复用不增加业务 SQL 执行次数；复制的 evidence 可通过最终回答引用门禁，真实撤销 SELECT 后引用及后续输入校验拒绝。
- 1500 行单表结果和 3000 行 `UNION ALL` 结果均保留原件；后续 Run 命中相同 manifest，读取末页。撤销 UNION 中一张表的权限后读取被拒；含不可认证叶子的查询继续标记 transient。
- 连接 revision 变化将查询和派生结论转为待补查；真实撤权使相关结论和依赖项退出锚点与可见结果。连续两次 resume 保持幂等。
- Python effect 使用生产 fingerprint 与授权数据库输入；两轮间仅实际写入一次 PNG，原 hash 和产物身份保持。连接 revision 变化后 effect 保持完成，原 dataVersion 保留，内外来源权限版本同步；派生 evidence 的父引用在新 Run 中闭合并通过最终回答门禁。
- 合法长 Goal 契约经真实 GoalTools 返回并落入原件，sourceDeps 为空集合；后续 Run 可读取，不依赖旧执行 epoch。

### 委派与交付

- 工具越权、父连接范围扩大、固定清单之外工具、搜索范围之外连接及未知输入均被拒绝；批内失败发生于子任务启动之前。
- 委派只包含显式输入，已保存快照在重放时重新检查真实来源权限。
- 两个真实子进程通过门闩同时到达第一次模型调用；每个子任务形成独立持久 Step，分别执行一次 progress 与一次 final。final 后无额外模型调用。
- 子任务只使用显式交接的证据且无需新查询；各自取得稳定、不同的子任务证据标识。Journal 与协调器重建后模型物理调用数仍为 4，交付保持两份。
- 外来 evidence 被移除，缺口使交付转为部分完成；自然结束保留文本并说明结构化交付缺口。重复合入不增加账本 revision。
- 跨 Run 重复同一无证据 todo / failure 不产生新进展。

### 浏览器、真实模型与旧协议

- 最终整套 5 个浏览器场景同时通过：确认后自动续轮与双子任务、窄屏拒绝草案、修改 / 暂停错误恢复 / 键盘取消、自动执行中插话，以及真实模型的普通查询和目标核对。授权子树刷新、503 重试与 403 清空均在主链路中断言。
- 真实 Flash 模型普通 SQL 得出 200；长任务最终得到 200 和 50，命中两次账本复用并在刷新后完整回看。该真实模型场景在用户首个 Run 内完成 Goal，自动轮数为 0；自动续轮由受控浏览器场景覆盖，跨 Run 复用由专用 PostgreSQL 集成覆盖。末轮候选曾被格式门禁拒绝，模型随后纠正并通过同一交付验收。最终 Agent 日志无 supervisor exception。
- 桌面和窄屏检查记录为 `work/983-goal-desktop.png`、`work/983-goal-mobile.png`，真实目标最终页面为 `work/983-real-goal.png`。浏览器先前因并行 Web 构建出现的临时授权失败保留于早期日志；最终稳定环境整套结果为 5 通过。
- 8 项旧回归保留原业务断言。无上下文的历史执行器显式使用旧协议；分类器和定时接管夹具保留真实发布 Context pin 并声明持久化的 answerProtocol 1；v2 子任务使用 `report_to_parent`，从实际工具观察中的 evidenceId 提交最终交付，重放不增加模型调用。

## AC-05 三策略实验

同一月度复盘 fixture、同一 9000 输入上限、三个装配步骤。选择器保守保留候选；摘要器只提取其实际输入中的显式事实标记。物理 SQL 使用真实只读 PostgreSQL。以下完成率基于单个受控任务；Token 为生产窗口估算采用的 UTF-8 字节保守上界，真实模型计费值不适用。

| 策略       | 最终任务完成率 | 每步关键事实平均保留率 | 重复查询 | 主输入上界 | 辅助输入上界 |
| ---------- | -------------: | ---------------------: | -------: | ---------: | -----------: |
| 固定窗口   |             0% |                     0% |        3 |     18,939 |            0 |
| 阶段摘要   |           100% |                   100% |        0 |      1,656 |      227,701 |
| 目标与账本 |           100% |                   100% |        0 |      5,871 |      227,701 |

每一步实际触发截断或压缩，最终输入均在上限内；摘要覆盖本步全部原始片段。目标策略三个主输入均保留目标、约束、销售事实、待办、失败记录和来源标记。阶段摘要在此确定性 fixture 中也保留了全部事实；该实验用于验证装配机制和上下文开销，真实模型行为由 TC-13 单独验收。

## 本次发现及修复复验

| 问题                                | 最小定位                                               | 复验状态                                         |
| ----------------------------------- | ------------------------------------------------------ | ------------------------------------------------ |
| 新 report-to-parent JSON 未注册     | PromptOps structured 校验 `unregistered_asset`         | 35 项数据库回归通过                              |
| 0017 历史夹具访问新 Goal 表         | 旧库夹具使用当前提交路径                               | 历史升级 native / SQL 通过                       |
| Goal partial index 备份重建文本差异 | 原与恢复后的数组强制类型转换表示不同                   | 明确 text predicate 后备份恢复通过               |
| 子任务可扩大父连接范围              | `handoff.resolve_task` 未相交父范围                    | 连接、工具清单与搜索范围回归通过                 |
| 大查询行数进入 200 行内联约束       | `ToolSucceededPayload.rowCount` 对 manifest 总行数报错 | 1500 / 3000 行原件与跨轮读取通过                 |
| revision 变化只更新 effect 外层来源 | 导入证据仍为旧 revision，最终引用为空                  | 内层来源同步后两个 effect 用例通过               |
| 取消与未投递进度竞争                | `settled_nodes` 仅从已投影节点生成下一版本             | 合并已落库 outbox 后两个取消用例与完整浏览器通过 |

## 业务用例覆盖

| 用例         | 本次执行结论                                                                    | 补充范围                                    |
| ------------ | ------------------------------------------------------------------------------- | ------------------------------------------- |
| TC-01        | 子进程并发、独立 Step、单次交付、重放、浏览器卡片与子树通过                     | 模型决策采用受控输入；真实 Chat 另验        |
| TC-02、TC-03 | 来源撤销、刷新续接、503 重试、403 清空、桌面 / 窄屏 / 键盘通过                  | 浏览器最终整套 5 项                         |
| TC-04、TC-05 | 插话免轮次、暂停恢复、revision、双扫描、取消 outbox 竞争、浏览器修改 / 插话通过 | 真 PG 与浏览器分别控制竞争窗口              |
| TC-06、TC-07 | 小 / 大结果跨轮复用、引用、撤权、版本、effect、冲突、真实 Chat 刷新通过         | effect 执行适配受控，实际文件与 hash 可核对 |
| TC-08、TC-09 | 补救、停止、父子计费、容量及删除隔离通过                                        | 真实模型预算收尾首轮决策 probe 通过         |
| TC-10        | 最小输入、权限、progress/final、重建去重通过                                    | 原 8 个失败项复验与完整旧套 87 项均通过     |
| TC-11        | 生产模块三策略离线实验通过                                                      | 真实模型质量不在本离线实验范围              |
| TC-12        | 35 项既有数据库、3 项新增迁移、日常迁移 / 两次 seed / smoke 通过                | 使用独立 PG 与日常入口分别验证              |
| TC-13        | 7 项真实模型首轮决策与实际 Chat 通过                                            | 真实 Flash 上游、生产 SQL 与刷新回看        |

主线程已确认 Agent 普通测试 578 通过、8 跳过，Web 176 项通过，Agent / Memory / Web 构建、契约与 SQL 一致性检查通过。既有 Runtime PostgreSQL 完整回归 87 项通过，最终 Agent lint（389 个文件）、类型检查、实现文件 Prettier 与 `git diff --check` 通过；日志为 `work/983-agent-lint-final.log`、`work/983-agent-typecheck-final.log`。8 个跳过项保留原状态，不计作通过。
