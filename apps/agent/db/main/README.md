# Agent / main 数据库

Owner 为 `agent/main`，PostgreSQL 17。迁移唯一源为 [Alembic revisions](../../migrations/versions/)，受管工具为 [database.py](../../scripts/database.py)。当前版本 `0021`：保留 `0001` 的演示数据，0002 增加 `model_provider`、`model_change`、`model_call`，0003 将已有 `aliyun_decision` 绑定迁移为通用 `systemone_decision`，保持 ID、型号、凭据、配置版本和历史记录。0004 为调用表追加可空 `execution` JSON，记录至多两次尝试的安全摘要；旧行保持 null。0005 新增每个工作区一行的 `retail_connection`，配置密码加密，更新按 revision 比较。0006 将旧 Retail 记录复制为工作区默认连接，保留密码密文和 revision，再移除原表。新 `data_connection` 同时支持普通 PostgreSQL 与 MySQL。0007 追加 `workflow_asset`，保存工作区个人的 JS 源码、摘要与运行契约。0008 增加 MCP 配置和 Sandbox 租约。0009 增加持久运行实体及物理模型尝试，扩展调用表的加密输入/输出。0010 增加运行调度。`0011` 增加会话标题状态、任务领取信息与软删除时间。0012 给工作流资产增加工具说明和参数 JSON Schema。0013 为 Provider 增加可空的模型容量元数据和生成默认值。0014 新增工作流软删除。0015 新增计划、触发、角色控制及回收和发布租约。0016 保存 PromptOps 代次与运行上下文。Run 快照保存 Registry 发布引用；0018 核对清单与摘要并清理重复存储。Provider 凭据与 Runtime 内容使用不同用途的密文。

## 配置与执行

- `DATABASE_URL`：`compass_agent_runtime`，仅运行与 seed 使用。
- `AGENT_MIGRATION_DATABASE_URL`：`compass_agent_migrator`，通过 `SET ROLE compass_agent_owner` 迁移。
- `AGENT_PROVISION_DATABASE_URL`：本地供应管理员，只在 provision 使用。数据库限 `compass_agent` / `compass_agent_*`；既有 `compass` 演示库可显式接管。

从 Compass 根目录执行：

```bash
pnpm db:provision -F agent
pnpm db:migrate -F agent
pnpm db:seed -F agent
pnpm db:sql -F agent
pnpm db:sql:check -F agent
```

供应仅支持本机教学部署，拒绝未知角色与数据库对象；不会重设已有角色密码。新库 owner 为无登录角色，migrator 获得 owner 成员资格；runtime 无 DDL、删除审计或改写版本表权限。已有 `0001` 库须符合原表/约束，供应工具才会接管并登记迁移源摘要。接管前停止写入并备份；将两条 Agent URL 显式指向旧库，供应管理员指向同一实例。

原生迁移与 SQL 执行均核验数据库所有者、版本和迁移源 SHA-256，通过事务 advisory lock 串行化；失败回滚本次事务。版本摘要登记在 `alembic_version` 表注释，历史 revision 保持不可变。API 启动和契约生成不执行迁移。

## SQL 交付

[manifest.json](sql/manifest.json) 登记工具版本、源路径、SHA-256 和固定区间。清单 sources 相对本目录，files 相对 sql/。[bootstrap.sql](sql/bootstrap.sql) 从空库安装 0020，受管增量覆盖 0001～0020。已有历史增量保留原字节。使用受管入口安装：

```bash
pnpm db:apply-sql -F agent -- --from empty --to 0020
# 旧库按当前版本填写 --from，0016 升级示例使用 --from 0016 --to 0020。
```

先完成供应与备份，停止模型写入，迁移 Agent 到 0020。按部署需要使用运行账号执行 seed 导入默认 Retail 连接，再发布两端应用。准备服务令牌、加密密钥与端点允许列表。数据库各自独立事务；任一步失败保留实际版本并停止功能开放。

恢复方式为：停止模型写入，使用 PostgreSQL 17 `pg_dump` 保存原库，将备份恢复到新供应的 Agent 空库（`pg_restore --no-owner --role=compass_agent_owner`），配置原加密密钥，核验版本与受保护凭据可解密，再切换运行连接。保留旧库；本期不通过 downgrade 删除模型历史。生产备份还须采用部署方的受控存储与账号策略。

## 验证

`pnpm test:integration -F agent` 必须显式提供 `AGENT_TEST_DATABASE_URL`、`AGENT_TEST_MIGRATION_DATABASE_URL`、`AGENT_TEST_ADMIN_URL`，库名限定 `compass_agent_test_*`。运行与迁移使用上述独立角色；已有角色应使用其原凭据。测试创建并清理专用临时库，覆盖空库、旧库、SQL 一致性、权限、并发与恢复。`AGENT_TEST_PG_CONTAINER` 可指定同一实例的容器使用 PostgreSQL 17 备份工具，否则使用本机工具。不得对日常库运行测试。

## 0007 发布与应用回退

停止新 Run 和配置写入，等待在途请求结束，备份库及加密密钥。执行受管 0007 迁移，再发布 Agent/Web 与生成契约。`readyz` 核对 0007。运行角色可读取、插入资产，只更新名称、启停状态、元数据版本和更新时间。回退到要求 0006 的构建时，将已验证的升级前备份恢复到独立应用库，核对连接配置与密钥后切换，保留 0007 库以便恢复源码资产。

## 0008 工具执行配置与租约

Feature 986，owner `agent/main`，起点 0007、终点 0008。唯一迁移源是 `migrations/versions/0008_tool_runtime.py`。新增 `mcp_connection` 与 `sandbox_allocation`，个人配置按 tenant/workspace/actor 隔离，工作卷按 host/slot 建立存活唯一约束。运行角色对连接具有 CRUD，对分配记录只有 SELECT/INSERT/UPDATE，保留审计和核对记录。

先停止新 Run 和 MCP 写入、等待在途计算收口，备份数据库与加密密钥，再执行迁移并发布 Agent/Web 和生成契约。`readyz` 核对 0008。应用回退保留新表；切回要求 0007 的构建时，恢复升级前备份到独立应用库后切换连接，保留 0008 库与 NAS 租约供核对。严禁把未核实的未知租约或 NAS 目录直接释放给其他用户。

## 0009 持久运行与人工确认

Feature 985，owner `agent/main`，起点 0008、终点 0009。迁移真源为 `migrations/versions/0009_durable_runs.py`，派生 SQL 及摘要登记在本目录 manifest。新增会话、Run、消息、Step/Attempt、Checkpoint、Outbox、Command、ToolBinding、Artifact、WorkflowDraft、Wait、Action、DemoEffect 和 ModelAttempt；运行角色仅可 SELECT/INSERT/UPDATE。旧 model_call.execution 的尝试摘要标记 `legacy_summary`，缺失原始内容保持空值。

发布前停止新 Run，收口或记录在途状态，备份数据库、两类加密密钥与 Redis AOF，再执行受管迁移；启动配置正确的 Redis，发布 Agent 和 Web。readyz 全部通过后恢复入口。回退使用升级前备份恢复到独立库并切换连接，保留升级后的库、AOF 和 NAS，供原任务及效果核对。详细运行与恢复约束见 [第 19 课](../../../../docs/lesson-19-durable-runtime.md)。

## 0011 会话标题与管理

[Feature 981](../../../../mai-specs/981-session-management/design/TECH_DESIGN.md) 的迁移 owner 为 `agent/main`，从 0010 升至 0011。唯一迁移源为 [0011_session_management.py](../../migrations/versions/0011_session_management.py)，完整 SQL 及增量登记在本库 manifest。原有标题保留，新会话在首条消息事务中登记标题生成。运行角色沿用会话表 SELECT、INSERT、UPDATE 权限。

暂停新 Run 受理并备份后执行 0011 迁移，再发布 Agent 和 Web。恢复使用升级前验证过的备份和配套构建，在独立库核对后切换连接。

## 0012 个人工作流工具契约

迁移从 0011 升至 0012，唯一迁移源为 [0012_dynamic_workflow.py](../../migrations/versions/0012_dynamic_workflow.py)，派生增量见 [0011__0012.sql](sql/upgrades/0011__0012.sql)。已有资产保持原契约和源码。运行角色写入新定义时提交说明和参数 schema，更新权限继续限定在管理列。工作流子任务用现有 `agent_step` 父子关系及 Attempt 记录。

迁移前暂停受理，核对活跃运行并备份。先迁移 Agent 数据库，再部署 Agent 与 Web。恢复使用验证过的 0011 备份及对应构建，在独立库核对后切换。

## 0013 Provider 生成配置

从 0012 升至 0013，Owner 为 `agent/main`。唯一迁移源为 [0013_model_generation_profile.py](../../migrations/versions/0013_model_generation_profile.py)，派生增量见 [0012__0013.sql](sql/upgrades/0012__0013.sql)。原有 Provider 保留凭据、绑定版本和审计历史，配置列为 SQL NULL。运行角色沿用现有 Provider 读写权限。

发布前暂停受理新 Run 和配置写入，收口在途请求并备份数据库及加密密钥。执行受管迁移后部署 Agent 和 Web，检查 readyz 后开放入口。回退原构建时，将验证过的 0012 备份恢复至独立库，核对凭据及记录后切换，保留升级库供恢复新配置。

## 0014 工作流软删除

从 0013 升至 0014，Owner 为 `agent/main`。迁移真源见 [0014_workflow_asset_deletion.py](../../migrations/versions/0014_workflow_asset_deletion.py)，派生增量见 [0013__0014.sql](sql/upgrades/0013__0014.sql)。迁移保留现有工作流和历史引用，运行角色可写删除时间，脚本和参数保持不可修改。删除后，同脚本可作为新资产保存。恢复时使用升级前备份和独立的 0013 数据库。

## 0015 定时触发与多副本运行

`agent/main` 从 0014 升至 0015。迁移保留现有 Session、Run、job、Checkpoint、Outbox 和游标，并新增计划、触发、运行控制记录及按 Run 的回收和发布租约。[Alembic 迁移](../../migrations/versions/0015_scheduled_tasks.py) · [0014 到 0015 的 SQL](sql/upgrades/0014__0015.sql)

`pnpm db:provision -F agent` 支持 `COMPASS_SCHEDULER_DATABASE_URL`。它使用同库的 `compass_agent_scheduler` 登录，权限限于计划扫描、交付状态、调度租约及已关联 Run 的安全摘要视图。Agent 通过自己的运行账号原子提交 Run 和触发关联。迁移保留独立的 owner、migrator 和 runtime 权限。

停旧消费者与 Agent 后备份 PostgreSQL、Redis 和密钥，封存旧运行写凭据，再执行供应与迁移。先启动新 Agent 验证旧队列和游标，再启 Scheduler。回到仅支持 0014 的版本时，将升级前备份恢复到隔离数据库并采用对应 Redis 快照；封存 0015 数据并核对升级期间的受理和已提交效果。[发布与回退步骤](../../../../docs/lesson-20-production-runtime.md)

## 0016 提示词发布与运行上下文

Agent 独占提示词草稿、不可变正文版本、版本清单、快照、发布回执和活动指针。0015 升至 0016 时已有 Run 保留原配置。新模型步骤可保存密封上下文，消息可携带来源引用，受管产物支持看板 JSON 和 Markdown 报告。[迁移源](../../migrations/versions/0016_prompt_context.py) · [增量 SQL](sql/upgrades/0015__0016.sql) · [完整 SQL](sql/bootstrap.sql)

从 Compass 根执行 `pnpm db:sql:check -F agent` 核对派生 SQL，备份数据库与密钥，暂停受理并退出旧消费者，再用 `pnpm db:migrate -F agent` 升级。迁移清空旧执行策略指纹，首个新版消费者在锁内登记新指纹。运行账号只能插入版本、清单和回执，Scheduler 通过 Agent 受理。部署支持新契约的 Agent 和 Web 后，平台管理员在 PromptOps 导入随包文件并首次整体发布，再开启受管 Run。故障恢复先将备份还原到独立库，核对活动指针、旧任务固定的清单及密钥，再切换连接。

## 0018 经营语义与查询回执

Run 在 `agent_run.config_snapshot.contextRuntime.registry` 固定发布 ID 和摘要。会话私有回执继续保存在 `semantic_metric_offer`。从 0017 升级时核对每行既有清单与所属 Run 快照。出现差异会回滚，修复快照并复核后重试。升级前暂停旧 Agent 和 Scheduler，备份数据库与私有状态密钥。需要恢复时以原应用版本连接还原的独立库。[迁移源](../../migrations/versions/0018_semantic_cleanup.py) · [增量 SQL](sql/upgrades/0017__0018.sql)

## 0019 目标与进度账本

`agent/main` 独占 `agent_goal` 与 `agent_goal_entry`，目标属于 Session，来源内容使用 Runtime 私有状态密钥加密。每个会话最多一个未结束目标；账本由 `(goal_id, entry_key)` 唯一定位。运行角色仅获 SELECT / INSERT / UPDATE，保留历史目标与账本。

升级前暂停接收并排空旧策略 Run，停止 Agent / Scheduler，备份 Agent 数据库与私有状态密钥。执行 `pnpm db:migrate -F agent`，或 `pnpm db:apply-sql -F agent -- --from 0018 --to 0020`，再启动新版服务并发布本课提示词。恢复时停止新版，将备份还原到独立库并用旧应用版本连接；保留升级后的库供核对。迁移不自动降级删除已产生的目标数据。

[迁移源](../../migrations/versions/0019_goal_ledger.py) · [0018→0019 SQL](sql/upgrades/0018__0019.sql) · [完整 SQL](sql/bootstrap.sql)。原区间 SQL 保持不可变。空库、旧库、生成一致性、受限角色和独立库恢复由 `test_database_tools_integration.py` 与 Feature 983 的数据库验收覆盖。

## 0020 ContextOps

源为 [0020_context_ops.py](../../migrations/versions/0020_context_ops.py)，新增 `context_decision_summary`、`context_snapshot`、`context_security_event`、`context_badcase` 四表。增量交付 [0019 → 0020](sql/upgrades/0019__0020.sql)，完整交付 [bootstrap.sql](sql/bootstrap.sql)。`web/main` 无迁移。

发布顺序：备份 0019 数据库和当前密钥，停旧 Worker/Scheduler，使用迁移账号升级 0020 并核对 SQL 清单，再启动 Agent、Web、Scheduler。Runtime 对新表仅有 SELECT/INSERT；Scheduler 仅查询快照引用并删除到期未引用快照。审计表不授予 UPDATE/DELETE。

恢复使用备份到新数据库并切换连接，保留故障库和原密钥；回退旧应用需恢复其对应版本备份。原生及 SQL 升级保留旧表和旧行；受管历史增量保持原字节。迁移、权限和恢复验收见[第 26 课实施记录](../../../../mai-specs/971-lesson-26-context-ops/IMPLEMENTATION.md)。

## 0021 共享 Sandbox 与命令租约

owner `agent/main`，0020→0021。源为 [0021_shared_sandbox.py](../../migrations/versions/0021_shared_sandbox.py)，交付 [0020→0021 SQL](sql/upgrades/0020__0021.sql) 与 [完整 SQL](sql/bootstrap.sql)。`sandbox_allocation` 增加共享标志、空闲期限和远端空列表观察时间；`sandbox_execution` 独立记录各次命令。共享 owner 的存活唯一索引约束跨 worker 创建竞争，历史记录默认 `shared=false`，保持 ID、卷绑定和审计状态。

发布前排空 Run、备份旧库及私有状态密钥、构建新 Sandbox 镜像；升级数据库后启动新 Agent 并同步工具 Prompt。计算和清理沿现有 OpenSandbox/NFS 控制面执行，清卷确认后才释放槽位。运行角色对新表仅 SELECT/INSERT/UPDATE。恢复使用备份还原到新库并切回旧应用，保留升级后的库及工作卷；禁止用 downgrade 删除调用历史。
