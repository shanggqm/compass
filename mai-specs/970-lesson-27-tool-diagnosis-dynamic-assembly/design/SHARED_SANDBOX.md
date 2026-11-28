# 共享 Sandbox 实施补充

需求来源：2026-11-27 用户明确授权，同一用户、租户和工作区复用一个 Sandbox，支持并行与共享文件，兼容分布式，修复历史回收，并使用 GPT 6.1-sol 实际验收。本补充扩展原方案的无 DDL 范围；原工具组装范围保持。

## 契约与最小实现

- 复用 `sandbox_allocation` 作为远端环境/独占卷租约。增加 `shared`、`idle_until`、`absent_since`；owner 三元组的部分唯一索引保证至多一个未释放共享环境。
- 新增 `sandbox_execution`，保存每次调用、远端 command ID、执行期限及确认状态。环境与命令有不同生命周期，无法用一条环境状态表示多个并行调用。
- `SharedAllocations.acquire` 仅在短事务中锁 owner、分配卷、登记命令；远端创建、续租和 Python 不持数据库锁。创建中的同 owner 等待，其他 owner 独立推进。并发上限默认 2，满额在本次剩余期限内等待；每 host 维持 4 个计算环境上限，清理独立于计算配额。
- `SandboxBackend.execute` 复用 OpenSandbox connect/renew/background command。Python 的 `data`、`result` 及请求字段保持；当前目录和 `chart_path`/`output_dir` 属于本次调用，`shared_dir=/workspace/shared` 属于同 owner。共享同名文件由任务显式协调，系统不改写代码或拦截文件 API。新调用图表使用相对 `chart.png` 或 `chart_path`；历史产物和旧 collector 兼容保留。
- 每次命令最长 20 秒。取消按 command ID 终止并确认；失联调用进入 unknown，停止新入场，允许已运行同伴完成，再回收环境。远端环境最大续租 900 秒，数据库提前记录含创建宽限的期限。
- 空闲 600 秒且无活动调用后删除容器、确认远端消失、清空卷、释放槽位。未释放环境持续复用共享文件；Sandbox 文件为有期限工作上下文，交付产物沿原 ArtifactStore 持久保存。
- 每次复用在执行前、交付前复核所有输入 Run 的来源权限，并把来源集合带入当前 Run 的交付复核；撤权结果不交付，失效环境待活动同伴结束后排空，以便后续新工作重建；身份来自 trusted context，无法通过模型参数指定别人的环境。
- collector 使用镜像内固定代码、隔离 Python 启动、目录 FD/NOFOLLOW、普通文件/大小检查。并行任务拥有独立目录；相同用户共享环境是明确的文件信任边界。
- 历史清理解除计算配额依赖。未知创建无 ID 的资源在最大创建/存活期限之后，以两次间隔至少 60 秒的成功空列表确认收敛；服务不可达仍保留租约。清理状态变更受 phase/sequence/reconcile token 校验。

## 数据库交付

owner：`agent/main`；起点 `0020`，终点 `0021`。迁移真源：`apps/agent/migrations/versions/0021_shared_sandbox.py`；实体：`tools/sandbox/entities.py`。运行角色新增表仅 SELECT/INSERT/UPDATE；不赋予 DDL 或清历史记录权限。

通过 `pnpm db:sql -F agent` 生成 `apps/agent/db/main/sql/bootstrap.sql`、所有受支持起点到 0021 的增量与 manifest；旧增量 SQL 保持不变。发布顺序：确认活动 Run 排空、备份现有库和资源清单、构建 Sandbox 镜像、迁移 Agent、发布代码及 Prompt、双 worker 启动、观察历史资源自动回收、真实验收。恢复使用备份恢复到新数据库并切回旧版本，保留原库及未清理工作卷；不执行破坏性 downgrade。

## 验收条件

1. 两个 Run/不同 Agent worker 同 owner 获得同一远端 ID，文件可连续读写。
2. 两次 Python 实际重叠运行，分别交付正确结果和各自图表。
3. 单次取消不终止同伴；断联/工作进程退出不重复执行同一调用。
4. tenant/workspace/user 任一变化隔离；来源撤权阻止旧文件继续传播。
5. 活动命令保护、空闲释放、未知创建恢复、清卷后重分配；记录现有三条历史资源回收结果。
6. 空库安装、0020 升级、生成一致性、运行角色权限、备份恢复与真实 PostgreSQL/OpenSandbox 测试。
7. 全部真实模型调用统一 GPT 6.1-sol，业务案例沿通用工具路径完成，无案例专属执行分支。
