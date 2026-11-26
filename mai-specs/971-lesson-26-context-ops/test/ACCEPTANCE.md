# 第 26 课 ContextOps 验收记录

日期：2026-11-25。需求依据为 [PRD](../spec/PRD.md)，用例定义见 [测试计划](TEST_PLAN.md)。本记录对应当前工作区实现。真实业务链路完整套件 11 项通过（52.3 秒）；后续统计、分页与 UI 改动按影响面复验通过，另新增 1 项 UI 分页场景通过。最后安全补丁已重启 Agent，并通过真实来源安全场景复验（14.2 秒）。用户追加 Docker 实测后，已执行实际商业模型、容器普通 Chat 和原版 Langfuse 联调，结果见下文。

## 环境与执行边界

- Node 24.12.0、pnpm 11.3.0、uv 0.13.0；PostgreSQL 17.11（55440）、Redis 8.10.2（56381，AOF `appendfsync=always`）。
- Web 8110 → Agent 8112 → 独立 Web/Agent/零售 PG 数据库；外部模型与 OTLP Protobuf 接收端位于 8114。原有服务、用户根 `.env`、旧库与数据卷均保留。
- 第一阶段外部模型由确定性 HTTP 夹具替代；Web 登录与 BFF、内部授权、Runtime、数据库工具、零售 RLS、投影、加密快照、审计和回放均调用实际实现。UI 错误恢复单独使用浏览器网络故障注入。Docker 追加阶段使用实际 DeepSeek Flash 和原版 Langfuse。
- 平台管理员、普通分析成员及另一租户账号均使用教学身份。内部 ContextOps 用途通过当前 Web 权限复核。敏感输入全部为合成哨兵。
- 执行入口：[浏览器套件](../../../apps/web/tests/e2e/contextops.spec.ts)、[隔离数据夹具](../../../apps/agent/tests/contextops_acceptance_fixture.py)、[模型与 OTLP 接收端](../../../apps/agent/tests/contextops_provider.py)。私有环境、令牌、数据库备份和浏览器 trace 保存在 `work/971-contextops/`，凭据文件权限为 0600。

## 业务验收

| 用例 / 需求             | 本次实际覆盖                                                                                                                                                                                                                                  | 结果                                                      |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| TC-971-01/02，AC-971-02 | 浏览器提交普通 Chat，真实授权查询两区域金额合计 200；回答卡进入 ContextOps；投影与快照实际存在；双轨迹、回答阶段、工具名称、刷新恢复                                                                                                          | 通过                                                      |
| TC-971-03，AC-971-03    | 与 PG 独立计数核对 Run/Session；同 Session 真实第二轮完成，最大轮数至少 2、平均搜索轮数大于 0；Flash 意图来源；空 Policy 条件为 0 条记录、未知指标为 null；真实空结果/工具错误及下钻；BFF limit=1 两页游标 Run 无重复，actor/session 筛选保持 | 修复后通过                                                |
| TC-971-04，BR-07        | 普通成员仅见本人；篡改 actor、他人 Run/快照和跨租户均受限；普通成员直接创建 Badcase 返回 403，页面隐藏管理入口                                                                                                                                | 通过                                                      |
| TC-971-05，AC-971-04    | 实际零售工具返回邮箱、手机号、API Key、Bearer 哨兵；模型接收与快照敏感原值为零；高危注入写审计且未到模型；正常“忽略之前口径重算”完成                                                                                                          | Review 来源绕过修复后通过                                 |
| NFR-01，在线 PEP 性能   | 实施流程以 50 条候选（2 条可信、48 条真实 columns/rows 结构）执行 200 次基准                                                                                                                                                                  | 主机 P95 5.415 ms；最终容器 P95 13.414 ms，均满足 ≤ 15 ms |
| TC-971-06，AC-971-05    | 快照真实读取审计 +1；PG 触发器拒绝审计写入时 HTTP 503 且无载荷；恢复后可读取                                                                                                                                                                  | 通过                                                      |
| TC-971-07，AC-971-05/07 | 当前来源的 region 2 权限真实撤销后快照/回放 403；源角色暂不可连接时快照 503、Badcase 为 authorization_unavailable；元数据 flow 仍可查，恢复后 200                                                                                             | 通过                                                      |
| TC-971-07，TTL          | 用 Scheduler 实际清理入口与 Scheduler 角色删除过期未引用快照；保留过期已被 Badcase 引用及未过期快照；过期读取 409                                                                                                                             | 通过                                                      |
| TC-971-08，AC-971-06    | 实际 MCP 初始化、四个只读工具、调用审计精确 +4、未知写方法/工具拒绝、跨作用域及当前权限撤销拒绝                                                                                                                                               | 通过                                                      |
| TC-971-09，AC-971-07    | API 与页面创建脱敏 Badcase，取消/重开；两次默认回放响应一致、基线匹配快照；调整预算和保护项；模型调用数不增加；直接回放实测 20.91 ms                                                                                                          | 通过                                                      |
| TC-971-10，AC-971-01    | 实际 OTLP Protobuf 导出 retriever/guardrail/chain 三种 Context Span；logical/request 同 trace；正文哨兵为零；收集端返回 503 时新 Run 完成                                                                                                     | 通过                                                      |
| TC-971-10，BR-05        | PG 拒绝 context_decision_summary INSERT：新 Run 完成、无该 Run 投影、错误计数增加、安全注入仍被拦截；结构化安全降级日志无正文哨兵                                                                                                             | 通过                                                      |
| TC-971-11，UI-01        | 1440 px 与 390 px 实页；无页面横向溢出；键盘 Enter/Esc、焦点恢复；快照查看、Badcase 取消/保存、Replay 编辑/执行；UI 503 重试恢复                                                                                                              | 通过                                                      |

UI 分页专项使用真实首屏响应，并仅替换后续游标与页内容，保留 BFF 会话版本头。验证运行分页继续传递时间窗、成员、场景和 Policy，审计分页保留成员与时间窗；该项单独计为 UI 测试。

多源 AccessRef 并集与分域摘要、跨租户候选拒绝、校验位身份证/订单号边界、密钥 JSON 保形、候选策略确实改变装配等细粒度条件由本次 Agent 开发测试覆盖。真实权限撤销验收使用一个受限连接的两区域授权；两个独立连接共同派生的快照未另建浏览器场景。

## PostgreSQL 与工程验证

| 验证                                                            | 本次结果                                                                                                                                                                                     |
| --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 根入口 `pnpm db:provision -F agent`、`pnpm db:migrate -F agent` | 在新专属 smoke 库空库安装至 0020，通过                                                                                                                                                       |
| 根入口 `pnpm db:seed -F agent` 连续两次                         | 显式关闭隔离环境不使用的默认零售连接配置后，两次通过；三天固定数据幂等                                                                                                                       |
| 通用 PostgreSQL 集成测试                                        | 35 项实际执行通过；首轮 5 项因本验收端点 allowlist 与既有用例默认端口不同，按这些用例默认配置重跑通过                                                                                        |
| 最终 0020 迁移源与 SQL 交付                                     | `pnpm db:sql:check -F agent` 通过；最终源指纹下重新建立专用验证库                                                                                                                            |
| 原生/增量 SQL `0019 → 0020`                                     | 两路升级、重复执行、旧业务行保留均通过                                                                                                                                                       |
| 角色权限                                                        | 四张 ContextOps 表运行角色可 SELECT/INSERT、不可 UPDATE/DELETE；Scheduler 仅快照表获 TTL DELETE 权限                                                                                         |
| 0020 备份恢复                                                   | 恢复至新独立 owner 库；旧行、快照摘要和解密结果保持；迁移重复执行为 already_applied                                                                                                          |
| 升级前恢复                                                      | 0019 备份恢复至新库，验证版本及旧行，再升级至 0020，通过                                                                                                                                     |
| 根入口 `pnpm smoke`                                             | 专用 Web 8115 → Agent 8116 → 新专属 PG 库通过；固定合成数据为 3 天、397 单、金额 4000000；两项临时服务已停止                                                                                 |
| 契约、`pnpm check`、`pnpm build`                                | 第一阶段综合检查 16 项任务通过，Agent 607 passed / 8 skipped；全工作区构建 6 项任务通过；契约及 SQL 无漂移。Docker 追加修复后的验证按应用范围分别执行，详见 [实施记录](../IMPLEMENTATION.md) |

Docker 追加阶段的根 `pnpm check` 在 tooling 完成后主动停止，避免 Home 测试和根构建读取其他 Feature。本轮整体 `pnpm check` 未计为通过。最终分项结果如下：

- Web 181 tests passed；Agent 618 passed / 8 skipped；Memory 1 passed；data-seed 34 passed。以上应用 lint/typecheck 均通过；Home lint/typecheck 两项通过。
- tooling 74 passed / 13 skipped；format、tooling lint、contracts、workspace 与 diff 检查通过。
- 四应用最终 Docker 镜像构建成功；Home 复用已有 public 资源。Agent/Memory/data-seed 原生包构建三项通过。
- 并发 Docker 构建期间，PEP 性能用例实测 78.349 ms，超出 15 ms 门限；构建结束后使用相同源码和原门限复跑，完整 Agent 套件通过。性能测试保留两次结果。
- 最终容器内同样执行 50 条候选、200 次采样：P50 6.259 ms、P95 13.414 ms、最大值 89.954 ms，P95 门限通过。

早期验收库在开发过程中写入的 0020 源指纹保留。最终迁移验证另建新库，未修改迁移账本或绕过历史校验。旧库、备份与恢复库保留在私有测试实例。

## Docker、实际模型与原版 Langfuse

Docker 恢复后保留既有服务、库与卷，新增独立项目 `compass-971-contextops`。四应用镜像完成构建，Web/Home/Agent Gateway/Memory 使用 18100–18103，专属 PG 为 55442、Redis 为 16379；Agent 有两个消费副本，Scheduler 独立部署。原版 Langfuse 4.75.0 的六项服务健康，浏览器入口为 8104。真实模型配置从原应用库只读取得，在测试库中使用测试密钥重新加密；原 Provider 保持不变。

| 实际链路                                                      | 本次结果                                                                                                                                                                |
| ------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 主机 Web → 生产 Agent → DeepSeek Flash → 授权 PG              | Run `39891cee-d183-4417-80a3-776d2df67f6e` 完成，14 次模型调用均 completed；真实查询两区域销售额并回答合计 200，回答卡进入 ContextOps                                   |
| 容器 Web 18100 → Agent Gateway 18102 → 消费副本 → 实际模型/PG | Run `eb22a55d-44e5-49aa-a12e-0405452a970d` 于 06:39:27–06:40:00 UTC 完成；7 次模型调用均 completed，错误数 0；实际查询合计 200，ContextOps 展示关联流程                 |
| 容器领取与持久队列                                            | Run 的 `owner_boot_id=ad510f62-4f78-410c-9768-b42b23b68e51`；job 为 done，claim_count=1，终态租约字段已清空                                                             |
| 原版 Langfuse API：主机链路                                   | Trace `302be7c0499d3d5c986e872c6c02eac8` 共 442 个 observation；retrieve/guardrail/compile 各 14 个，类型分别为 RETRIEVER/GUARDRAIL/CHAIN，均关联 logical → request     |
| 原版 Langfuse API：容器链路                                   | Trace `5f5a01019ed207e65f4914d704777a38` 共 147 个 observation；三类 Context observation 各 7 个，祖先链与同 Trace 关系通过；7 个 GENERATION 记录实际用量               |
| 原版 Langfuse 零正文                                          | 三条 Trace 所有 observation 的 input/output 为空；问题、SQL、PII、密钥和注入正文哨兵命中 0；Agent 配置 `capture_content=false`                                          |
| 最终镜像：真实模型与接管 Run                                  | Run `baac4fbc-ab4c-400a-8858-058646ce5147` 于 06:55:37–06:56:38 UTC 完成；9 次实际模型调用均 completed，错误数 0；实际回答合计 200，浏览器进入 ContextOps               |
| 最终镜像：原版 Langfuse API                                   | Trace `65f3bdfba41a2e80a3d32d9b8a08fe96` 共 195 个 observation；三类 Context 各 9 个，类型和祖先链通过；accepted/running/paused/completed 的 request level 均为 DEFAULT |
| 最终镜像：原版 Langfuse 界面                                  | 从上述 Run 的 ContextOps 实际点击 CTA，直接进入公开项目的准确 Trace 页面；无 404、无内部容器地址，compile 与 guardrail 可见，正常生命周期不再显示 Error                 |
| 最终镜像：快照 → Badcase → 默认局部回放                       | 同一 Run 的快照读取已审计；页面创建 Badcase 并默认回放，baseline_matches_snapshot=true；UI 往返 330 ms；PG 模型调用总数 18→18、该 Run 9→9；390 px 无全页横向溢出        |
| 最终四应用 smoke                                              | Web 认证、Web → BFF → Agent → seeded PG、Memory、Home 页面与资产全部通过，日志 `docker-final-smoke.log`                                                                 |

接管验证仅操作上述独立项目的精确容器 ID，并校验 Compose project/service 标签。先停止空闲副本 2，使新 Run 的首次领取明确归属副本 1；恢复副本 2 健康后，对仍持租执行的副本 1 发送 SIGTERM，保留 45 秒正常退出窗口。Run 自动经历 `process_interrupted → reclaiming → queued → running → completed`，沿同一 Run ID 从 checkpoint 8 接续，最终 checkpoint 19、claim_count=2。

| 领取 | 容器                           | worker_id / boot_id                                                               | execution_epoch                        |
| ---- | ------------------------------ | --------------------------------------------------------------------------------- | -------------------------------------- |
| 首次 | compass-971-contextops-agent-1 | `6f366b46-2688-455f-bcce-c08faedfb3fc:0` / `6f366b46-2688-455f-bcce-c08faedfb3fc` | `5eb43db9-65a0-4f5c-ae70-8d23e6123563` |
| 接管 | compass-971-contextops-agent-2 | `1ca000fc-5ba8-4689-a64f-88b3d13bd163:0` / `1ca000fc-5ba8-4689-a64f-88b3d13bd163` | `05126afb-8bd4-430f-a81f-66df28489243` |

两个 Agent 均已恢复健康。随后独立重启 Scheduler，holder 从 `c3f8de29-833c-4053-95ce-62827aabe741` 变为 `b39b234c-e284-4498-894d-34c76f322e34`，generation 从 2 增至 3，并实测租约继续续期；重启期间 Web/Home/Agent/Memory HTTP 均为 200。此项覆盖正常退出边界接管；未执行 SIGKILL 后外部调用状态不确定的故障场景。

主机切换真实 Provider 时，原测试库存在旧 Runtime 策略指纹。确认无活动 Run、旧消费进程已退出后，使用事务锁及已观察指纹的精确 CAS 切换，仅作用于专属测试库；原值保存在私有记录。迁移账本保持不变。容器使用新建数据库，首次启动前配置真实 Provider。

实际模型验收完成后，主机与容器中的两个测试 Provider 已停用、隐藏，并通过版本校验将其当前凭据替换为无效合成占位值。原应用 Provider 保持不变，Run/ModelCall、快照和 Badcase 历史保留。

## 本次发现与修复复验

1. Review 将已有来源内容作为 actor 再送模型，导致已脱敏或拦截的原文重新进入请求。实现将复核材料归入 observation/review，并对结构化叶值处理后统一隔离；真实工具返回攻击复验通过。
2. 回答阶段与数据库工具名称映射缺失。实现显式标识 reviewing/generate 和数据库执行键；新 Run 展示 answer 阶段和 database_query。
3. 空查询结果与失败工具统计读取了脱敏后的公共 timeline 或 journal 提交状态，漏计业务结果。实现改用公开 execution.node 的 empty/failed 状态；真实空查询与错误字段查询复验通过，空结果及失败 Run 均可下钻。
4. 原版 Langfuse 4.75.0 使用公开项目 `/project/<id>/traces/<trace_id>` 路由；原 CTA 的 `/trace/<id>` 返回 404，容器同时暴露内部主机地址。BFF 改用公开项目地址后，新镜像实际点击准确进入 Trace，复验通过。
5. 正常 accepted/running 阶段的 request observation 被映射为 ERROR。修复后用上述接管 Run 复验，accepted/running/paused/completed 均为 DEFAULT，原版 UI 不再误标正常生命周期。

新增分页夹具首次遗漏 BFF 会话版本响应头，触发 DocumentSession 的整页刷新；保留真实响应头后，UI 分页验收通过。

首次执行还遇到测试权限夹具缺少 module.plugins、端点 allowlist 与旧测试要求冲突、开发进程热切换及 React 严格模式重复请求等环境/脚本问题，均在隔离测试配置或用例定位后修正，业务预期保持。

## 未执行边界与记录位置

实际 DeepSeek Flash 已完成普通数据库问题的生成与工具调用。大规模生成质量评测、其他商业模型和 JEV 上游质量不在本次验收范围。确定性安全攻击、撤权与依赖故障继续使用第一阶段的隔离夹具。

本次私有结果包括 `playwright-final-results.json`、`playwright-final.log`、`playwright-statistics-final-results.json`、`playwright-pagination-final-results.json`、`playwright-ui-pagination-final-results.json`、`playwright-security-table-final-results.json`、`api-acceptance-results.json`、`ttl-results.json`、`migration-971-results.json`、`migration-final.log`、`sql-final-check.log`、根迁移/seed/smoke 日志，以及桌面和 390 px 截图。失败 trace 可能包含本地教学凭据，保持私有。

Docker 追加阶段记录包括 `real-host-run.json`、`real-docker-run.json`、`real-docker-recovery-run.json`、`langfuse-real-results.json`、`docker/langfuse-results.json`、`docker/langfuse-recovery-results.json`、`real-docker-recovery-langfuse-ui-results.json`、`docker/real-docker-recovery-run-recovery.json`、`docker/real-recovery-run-metadata.json`、`docker/real-replay-results.json`、两份 `real-provider-retired.json`、实际 Chat/ContextOps/Langfuse 截图，以及 `docker/` 下部署和 smoke 日志。执行脚本 `docker-recovery.py`、`langfuse-trace-ui.cjs`、`real-docker-replay.cjs` 保存在同一私有目录；凭据与浏览器登录状态保持私有。
