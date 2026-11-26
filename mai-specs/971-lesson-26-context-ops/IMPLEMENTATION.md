# 第 26 课实施记录

有效需求为 [PRD](spec/PRD.md)，实施依据为 [技术方案](design/TECH_DESIGN.md)及[修订评审结论](review/design_review.md)。保留开始实施前已有的规格修改。当前记录仅维护实施与验证进度。

| 任务                                           | 对应验收          | 状态   |
| ---------------------------------------------- | ----------------- | ------ |
| T1：Agent 0020 迁移、实体及 SQL 交付           | AC-971-08         | 已完成 |
| T2：在线 PEP、精准脱敏与派生权限继承           | AC-971-04         | 已完成 |
| T3：Context / Guardrail OTel 零正文埋点        | AC-971-01         | 已完成 |
| T4：有界异步投影、加密快照、统计与局部回放     | AC-971-03、05、07 | 已完成 |
| T5：REST、只读 MCP 与生成契约                  | AC-971-06、08     | 已完成 |
| T6：ContextOps 控制台、BFF、导航和 Chat 入口   | AC-971-02、03     | 已完成 |
| T7：PostgreSQL、真实浏览器端到端及完整工程门禁 | 全部 AC           | 已完成 |

## 当前实现核对

- 源数据权限继续使用 `context_runtime/source_access.py` 实时复核；派生约束取全部受限源的并集（逻辑 AND）。
- 普通观测投影使用有界队列并允许丢弃，快照查看审计同步提交，失败拒绝返回正文。
- 每次模型调用前对最终装配结果再次执行 PEP，覆盖历史选择、分域摘要和 Review 的派生输入。高危内容使用固定隔离标记，工具协议配对和结构化 Review 字段继续有效；对原本无需脱敏的工具参数保留原字节。
- 模型入口保留用户主动提供的合法查询主键；落库使用结构化脱敏，手机号、邮箱、合法身份证和凭据不可逆替换，订单号等明确业务 ID 保留。重复编译的边界封装和脱敏保持幂等。
- 加密快照使用独立用途密钥派生，正文经压缩、加密并保存 SHA-256；每日 Scheduler 清理已过期且无 Badcase 引用的快照。被引用的过期快照保留记录，读取及回放返回明确失效状态。
- Run、会话、意图、工具结果和产物统计来自持久运行记录。工具空结果与失败使用公开执行节点的业务状态；搜索轮数按调用父节点聚合；安全拒答以模型明确的 `model_refused` 结果分类。空统计窗口保留未知值。
- 五个控制台标签共用顶部筛选与看板；管理员可查当前租户/工作区全部成员，普通成员只见本人会话。两个瀑布视角、三种尺度、阶段过滤、逐条决策、快照、Badcase 和回放均已接入实际接口。
- Context-only Replay 复用在线确定性装配函数。候选内容、已有模型摘要/选择结果和版本被冻结；参数可调整窗口预算、口径保护、截断阈值与 observation 上限，回放不重新请求模型或执行外部工具。

## 实现对齐记录

- 按用户确认，内部鉴权新增 `contextops_read` / `contextops_admin`，分别复用现有分析权限与平台管理员身份，并在 Agent 请求中再次经 Web 核验当前权限。这项实现落实 PRD 的本人读取/管理员作用域；既有 `runtime_usage`、`runtime_diagnostics` 的入口规则保持各自职责。
- BFF 复用项目已有的 `/api/web/v1/[...path]` 分发器，ContextOps 参数、CSRF、作用域和上游 JSON Schema 校验集中在独立 handler；无需新增同层级 catch-all 路由。
- 增量 SQL 沿用项目受管入口及 `db/main/sql/upgrades/0019__0020.sql` 命名。owner 为 `agent/main`，迁移 `0019 → 0020`；[迁移真源](../../apps/agent/migrations/versions/0020_context_ops.py)、[完整 SQL](../../apps/agent/db/main/sql/bootstrap.sql)、[增量 SQL](../../apps/agent/db/main/sql/upgrades/0019__0020.sql) 与 [manifest](../../apps/agent/db/main/sql/manifest.json) 同步交付。Web 无新增迁移。
- Runtime 对四张新表仅有 SELECT/INSERT；Scheduler 仅能读取快照引用并删除满足 TTL 的快照。发布与恢复顺序见 [数据库说明](../../apps/agent/db/main/README.md#0020-contextops)。旧版迁移及历史增量保持原字节。
- MCP 入口仅登记四个只读工具；使用专用一小时作用域凭据，每次调用实时核验 Web 权限并同步记录审计。控制台提供无秘密值的客户端配置模板，签发入口见 [Agent 运行说明](../../apps/agent/README.md)。
- `contracts/` 和 Web generated 文件全部由 Agent DTO/路由生成；应用 README、`.env.example`、`manifest.yml` 已同步。本次保持 BRIEF Phase 与 ROADMAP 原状态。

## 界面还原修订

- 控制台按 [静态原型](spec/contextops-prototype.html) 重建：页头状态胶囊与操作、单行筛选栏、4×2 KPI 卡（ECharts 趋势迷你图）、下划线子标签、三轨瀑布与业务阶段色带、按轮分隔的轨迹列表与吸顶详情、意图分布与 ECharts 分类耗时哑铃图、异常排行与记录表、安全校验与审计表、内联策略回放、快照弹层与 MCP 抽屉。
- Agent 概览、意图与异常响应只增字段：`trend`、压缩节省率、缓存命中率、异常 / 空结果计数、1–3 轮会话占比、安全模式、Langfuse 状态、策略版本；意图按场景的判决来源拆分与语义版本；异常运行原因与阶段。Web 为管理员页加载成员显示名，并以公开项目地址替换 Langfuse 入口。
- 原型中的演示视角切换由真实权限替代；「运行校验」改为「刷新状态」，按实时审计计数展示防护命中，不伪造校验结果。MCP 抽屉的试跑经 Web 调用同一只读查询生成。

## 验证

独立验收计划见 [TEST_PLAN.md](test/TEST_PLAN.md)，完整执行与环境边界见 [ACCEPTANCE.md](test/ACCEPTANCE.md)。先使用独立 PostgreSQL 17.11、Redis、Web、Agent 和受控模型/OTLP HTTP 上游完成全套业务验收；2026-11-25 恢复 Docker 后追加四应用容器、真实 DeepSeek 和原版 Langfuse 验收。日常数据库与隔离验收库分别保留。

| 检查                   | 本次结果                                                                                                                                                                           |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 完整 Playwright 端到端 | 11 项完整套件通过，52.3 秒；新增 UI 分页、真实同会话多轮和 BFF 分页、最终表格脱敏及受影响场景定向复验通过                                                                          |
| PostgreSQL 通用集成    | 35 项实际执行通过；依赖默认 allowlist 的 5 项在匹配环境下复跑通过                                                                                                                  |
| 0020 专属数据库验收    | 原生/SQL 空库与 0019 升级、幂等、历史行保留、Runtime/Scheduler 权限、TTL、0020 快照备份恢复解密、0019 备份恢复后再升级通过                                                         |
| 源撤权与审计故障       | 实际行权限撤销后快照/回放 403；授权源不可用 503；审计拒写 503 且无载荷，恢复后可用                                                                                                 |
| 观测故障隔离           | 实际 OTLP 503 与 PG 投影 INSERT 故障下 Run 均完成；在线安全拦截保持生效                                                                                                            |
| Replay                 | 两次默认回放结果一致且匹配冻结装配，实测 20.91 ms，外部模型调用数不增加                                                                                                            |
| 安全性能               | 50 条混合候选（含真实 columns/rows 结构）、200 次执行，PEP P95 5.415 ms；全量测试同时执行 50 条候选性能门限                                                                        |
| 契约 / 综合检查 / 构建 | 首轮完成 `pnpm contracts:check`、`pnpm check`（16 项任务成功，Agent 607 passed / 8 skipped）和 `pnpm build`（6 项任务成功）；SQL 一致性通过。Docker 补验修复后的最新分项检查见下文 |
| 根迁移与重复 seed      | 独立 smoke 库迁移、连续两次 seed 与 Web → BFF → Agent → PG 完整 smoke 全部通过                                                                                                     |

## Docker 与真实模型补验（2026-11-25）

Docker Desktop 已恢复。原有 PostgreSQL 和 Langfuse 的数据卷保留；隔离项目 `compass-971-contextops` 使用独立 PostgreSQL、Redis、Home、Web、Memory、两个 Agent 副本、Gateway 和 Scheduler。原版 Langfuse 六个服务健康，采集关闭正文。隔离应用库从空库完成迁移，真实查询使用专用只读零售连接。

实测修复了四项问题，并重新构建、替换 Web / Agent / Scheduler 镜像：

1. Compose 与 Agent 开发任务透传快照 TTL、安全模式、MCP 开关。
2. 可选路由 Provider ID 的空环境变量按未配置处理；合法 UUID 和非法非空输入保持原校验。
3. BFF 使用公开项目地址生成准确的 Langfuse Trace 链接；缺失或非法配置禁用跳转，导出失败状态保留。
4. Langfuse 正常生命周期 `accepted/running/paused/completed` 使用 DEFAULT；失败和不完整执行继续使用 ERROR，取消使用 WARNING。

| 检查               | 本次结果                                                                                                                                                                                                                        |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 真实商业模型       | 原生环境完成 14 次、容器首轮完成 7 次 DeepSeek Flash 调用；均通过授权数据库查询得到金额 200，并从 Chat 进入 ContextOps。补验使用隔离 Provider 副本                                                                              |
| 两副本接管         | 容器 Run `baac4fbc-ab4c-400a-8858-058646ce5147` 在工作实例正常退出后保留 checkpoint 8，由另一个 worker / epoch 继续，claim_count=2，最终 completed、金额 200；两个副本恢复健康                                                  |
| Scheduler 独立重启 | holder 变更，generation 2→3，租约持续续期；期间四应用 HTTP 均为 200                                                                                                                                                             |
| 原版 Langfuse      | 最终接管 Run 的 Trace 含 195 个 observation，retrieve / guardrail / compile 各 9 个，类型和父子关系正确；正常状态全部 DEFAULT，输入/输出与明文哨兵均为空；浏览器 CTA 直接打开准确 Trace                                         |
| 容器真实业务闭环   | 同一 Run 完成审计快照读取、Badcase 创建和默认 Replay；baseline_matches_snapshot=true，界面往返约 330 ms；全库 model_call 18→18、该 Run 9→9，390px 页面无横向溢出                                                                |
| 容器安全性能       | 50 条候选（2 条可信输入、48 条 columns/rows 结构化观测），200 次执行，PEP P50 6.259 ms、P95 13.414 ms，满足 15 ms 门限                                                                                                          |
| 最新代码检查       | Web 181 项、Agent 618 项、Memory 1 项、data-seed 34 项通过；Agent 8 项依赖独立凭据的测试跳过。四应用与数据工具 lint / typecheck 通过，工具入口 74 项通过 / 13 项跳过；格式、脚本 lint、契约漂移、workspace 边界与 diff 检查通过 |
| 最新构建           | 四应用 Docker 镜像构建并启动成功；Agent / Memory / data-seed 原生分发包构建 3 项通过                                                                                                                                            |

并发执行 Docker 镜像构建与全套应用检查时，PEP 性能用例出现一次 P95 78.349 ms 超限；构建结束后以原代码和原门限重跑 Agent 全套测试，618 项全部通过，再于实际容器完成上述 200 次测量。保留首次失败与复验日志。

本轮 `pnpm check` 在工具入口测试后停止，随后分项执行格式、契约、边界和应用检查。Home 测试及根构建会读取其他 Feature 的课程资料，依当前 Feature 访问范围约束，本轮复用已有 Home 分发资源构建容器并验收页面；未重跑该课程资料测试，也未将本轮根 `pnpm check` / `pnpm build` 记作通过。

最终镜像再执行四应用 smoke，Web 鉴权、Web → BFF → Agent → 已 seed PostgreSQL、Memory 就绪及 Home 页面/分发资源全部通过。验收结束后已停用并隐藏两个环境的真实 Provider 副本，以无效合成值替换本次复制的密钥；原 Provider、历史 Run 和数据卷保留。

测试日志、截图、执行脚本与私有环境保存在不提交的 `work/971-contextops/`。商业模型样本覆盖当前合成数据库问题；完整业务边界由受控上游端到端、PostgreSQL 故障注入和上述实际容器链路共同验证。
