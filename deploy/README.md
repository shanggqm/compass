# 本地部署与应用镜像

第 14 课的独立 `observability` profile 在同一 Compose 网络部署本机 Langfuse，入口为 `pnpm observability`，UI 使用 loopback 8104。主 Agent / Web 可选加载忽略 Git 的观测配置，停止观测栈保留卷且不停止业务应用。镜像、初始化、数据格式、多实例接入与恢复见 [Langfuse 运行说明](observability/README.md)。

`compose/compose.yaml` 提供带 pgvector 的 PostgreSQL 17 与持久 Redis，镜像以 digest 固定。Web 数据库的供应步骤安装 vector 扩展，0009 迁移创建知识库片段向量表。`app` profile 加入 Agent 迁移与 seed、Web 独立数据库供应/迁移、FastAPI 与 Next.js standalone。`web` profile 只启动 PostgreSQL、Web 迁移和 Web，无 Python 服务依赖。所有宿主机端口绑定 127.0.0.1；演示账号和合成数据仅供本地。

根目录使用以下统一入口，镜像构建上下文始终为 Compass 根目录：

```bash
pnpm run deploy          # 全部 home / web / agent / memory
pnpm run deploy web      # 仅 Web；所需数据库与迁移自动执行
pnpm run deploy home     # 仅官网，无 Python / 数据库依赖
pnpm run deploy agent    # 仅 Agent，包含所属迁移和三天 seed
pnpm run deploy memory
pnpm deploy:status       # 可追加 app 名称
pnpm deploy:logs web     # 最近 100 行日志，不持续占用终端
pnpm deploy:down         # 整个项目停止，保留数据卷和镜像
```

`pnpm deploy` 是 pnpm 的包分发命令，项目部署必须使用 `pnpm run deploy`。单应用部署只停止并重建指定应用，其余应用保持现状；数据库按需启动。只需数据库时仍使用 `pnpm db:up`。

## 部署顺序与失败处理

1. 检查参数和本机 Docker socket，将后续 Docker 调用固定到该地址；固定 Compose 文件和 `compass` 项目名。忽略 `COMPOSE_FILE`、`COMPOSE_PROFILES`、`COMPOSE_PROJECT_NAME` 等环境覆盖，禁止 TCP/SSH 远程端点。
2. 获取 `work/local-deploy.lock`，检查所需端口、已有数据库健康；不结束开发进程。运行中的同项目同服务端口允许复用。
3. Web 执行 `pnpm contracts:check`；有漂移时先运行 `pnpm contracts`。home 从规范源准备素材。构建所选应用及迁移镜像，复用 Docker 构建缓存；构建失败时旧应用继续运行。
4. 等待 PostgreSQL 健康（仅 Web / Agent 需要），停止所选应用。每次显式运行所属迁移容器：Agent 执行 Alembic 与幂等 seed；Web 执行已有供应和迁移，不自动写教学账号。
5. 重建所选应用并等待健康检查（最多 120 秒），执行固定本机地址的 smoke；随后启动 Scheduler 并检查角色租约。部署核对 Agent 副本数与各容器健康状态，端到端验收覆盖实际领取和触发。

任一步失败即退出非零，不启动后续阶段，不自动清卷或回滚数据库；迁移失败后所选应用保持停止，修复原因后重跑。跨应用迁移不具备原子性，恢复遵循各所有者数据库规范。部署脚本只创建部署锁等临时文件，不记录凭据；异常终止遗留锁时先核对 `owner.json` 的 PID，再删除过期锁。

不主动读取根 `.env`，smoke 使用 `--local` 固定 127.0.0.1 地址；Compose 使用已登记的本地凭据。`COMPASS_NPM_REGISTRY` 可显式传入。Node / pnpm 与 Docker 必需；Web 的契约检查还需要 uv 和已安装 Python workspace。端口分别为 home 8101、Web 8100、Agent 8102、Memory 8103、PostgreSQL 55432。

CI 复用同一 `pnpm run deploy` 入口，之前执行 `pnpm check` 和 `pnpm build`。本机部署默认复用已完成的开发检查，部署仍强制执行契约检查、素材准备、容器健康检查与 smoke；完整发布前可手动先执行项目检查。

## 镜像与 Compose

- `containers/home.Dockerfile`：消费部署入口生成的 public 素材，使用 standalone 构建；复制 static/public，非 root 运行。直接调用 Compose 构建 home 前，先执行 `node apps/home/scripts/prepare-assets.mjs`。
- `containers/agent.Dockerfile`：uv 冻结安装当前 API 所需生产依赖；独立非 root 用户。
- `containers/web.Dockerfile`：BuildKit 缓存依赖下载，pnpm 冻结安装，构建 Next.js standalone，运行时只复制追踪产物与 static，非 root 用户。
- `containers/memory.Dockerfile` 安装 Memory 的 Python 生产依赖并提供健康检查。
- Python 迁移文件随 API 镜像交付；迁移任务先成功，API 再启动。生产部署需分离数据库迁移凭据。
- Web 镜像消费已生成且通过 CI 校验的契约；本地修改 DTO 后先运行 `pnpm contracts`。

停止所有服务：`pnpm deploy:down`。该入口保留数据卷，不接受 `--volumes`；数据重置需另行明确处理。

Agent 采用同一镜像运行 HTTP 副本和独立 Scheduler。Compose 的 `agent-gateway` 在 loopback 8102 均衡 HTTP 和 SSE，Scheduler 无公开端口。使用 `COMPASS_AGENT_REPLICAS` 调整 Agent 副本数。可通过 `pnpm deploy:status agent` 和 `pnpm deploy:logs agent` 查看两个角色。Memory 可用独立 profile 启动，宿主机端口为 8103。

网络受限时，可在 Web 镜像构建时指定可访问的 npm 镜像，锁文件 integrity 校验仍然生效，默认使用官方 registry：

```bash
COMPASS_NPM_REGISTRY=https://registry.npmmirror.com/ docker compose -f deploy/compose/compose.yaml --profile app up --build -d --wait
```

## 独立零售业务库

Feature 998 的 [离线工具](../tools/data-seed/README.md) 复用同一 PostgreSQL 实例，通过 `data:seed` 创建带 release 摘要的独立候选库及专用装载/只读账号。配置模板见 [retail-local.example.json](retail-local.example.json)，操作与受限连接边界见 [业务数据说明](../docs/data-seeding.md)。模板不含凭据，`state_directory` 以 Compass 根目录为基准；工作目录与生成数据不提交 Git。应用库和原三天 seed 不参与零售版本切换。

同库保留旧数据的对照模式使用 [retail-schema.example.json](retail-schema.example.json) 与 `publish-schema`；新 schema 使用独立装载角色和状态目录，详见业务数据说明的“语义版本与旧数据对照”。

## Web 独立部署

```bash
pnpm run deploy web
docker compose -f deploy/compose/compose.yaml --profile web run --rm web-migrate node apps/web/scripts/db-manage.mjs create-user --login-name local.admin --display-name 本地管理员 --platform-admin true
```

`web-migrate` 使用 Web Dockerfile 的 `database-tools` 阶段，拥有独立供应/迁移凭据；运行容器仅得到 Web runtime 连接。迁移成功后 `/readyz` 才就绪；不会自动创建账号或写教学 fixture。账号命令交互输入隐藏密码；打开 <http://127.0.0.1:8100/login>。已有库保留，供应拒绝未知角色/数据库。跨服务旧演示只在 `app` profile 下可用，失败不影响 Web 管理。

完整 SQL 与备份恢复见 [Web 数据库说明](../apps/web/db/main/README.md)。首次发布顺序为供应角色/库、独立迁移、受管建账号、启动 Web；恢复时切换到已验证且已撤销旧会话的新库。此 Compose 为 loopback 教学环境；共享环境自行供应私有数据库凭据、HTTPS 来源和备份/审计保留策略。

## 模型 Runtime 配置

Agent 迁移容器先供应独立 `compass_agent` 库，再迁移至 `0020` 并写入幂等工程样本；运行容器仅持有 `compass_agent_runtime` 凭据。既有旧版 `compass` 演示库保留，接管步骤见 [agent/main](../apps/agent/db/main/README.md)。自定义已存在角色时可通过 `COMPASS_AGENT_RUNTIME_URL` / `COMPASS_AGENT_MIGRATOR_URL` 提供原凭据，供应不会覆盖密码。

`pnpm run deploy` 不读取根 `.env`。启用模型能力时从受控进程环境传入 `COMPASS_AGENT_SERVICE_TOKEN`、`COMPASS_MODEL_ENCRYPTION_KEY`、`COMPASS_MODEL_ALLOWED_ORIGINS`；前两项默认空，模型入口会明确不可用。模型 Key 经平台页面录入；数据库备份与加密密钥分别保存。完整接入见 [Agent 说明](../apps/agent/README.md#模型接入)。

## Agent Run 的 Retail 绑定

第 15 课的固定 Retail 工具连接独立的已发布零售库。要导入默认连接，向部署进程成对提供私有的 `COMPASS_RETAIL_RUN_SCOPE` 与 `COMPASS_RETAIL_RUN_READONLY_DSN`，并提供 `COMPASS_MODEL_ENCRYPTION_KEY`。Compose 将导入参数交给一次性迁移与 seed 服务。DSN 使用 Agent 容器能访问的地址，同一 Compose PostgreSQL 使用 `postgres:5432`。默认 Retail 的允许目标由 `COMPASS_RETAIL_ALLOWED_ENDPOINTS` 指定。通用 PostgreSQL/MySQL 连接可通过 `COMPASS_DATA_ALLOWED_ENDPOINTS` 放行额外的主机和端口。

导入完成后，在对应工作区的「数据连接」页测试和编辑。平台管理员保存的修改用于后续 Run，进行中的 Run 使用已读取的配置。取消意图写入主库，由当前持租 Agent 副本处理。配置与验证步骤见 [课堂复现](../docs/lesson-15-agent-run.md)。

第 18 课独立计算与 NAS 使用 `deploy/compose/sandbox.yaml`，入口 `pnpm sandbox up`，配置和执行边界见 [工具执行说明](../docs/lesson-18-tool-runtime.md)。

## 持久 Runtime 配置

Agent 部署需显式提供独立 Fernet `COMPASS_RUNTIME_STATE_KEY`，同时保留 Web/Agent 服务令牌、模型密钥。缺失 Runtime 密钥时 readyz 返回 503。部署入口继续只消费进程环境；可用 `node --env-file=.env scripts/deploy.mjs up` 加载私有根环境。Redis 配置见 `redis/redis.conf`，固定 AOF always/noeviction，卷 `redis-data` 在停止后保留，默认 loopback 56379。服务令牌用于当前权限复查，独立 `COMPASS_RUNTIME_EVENT_TOKEN` 用于可信工作流事件。Agent 容器为 SSE 设 5 秒 HTTP 关闭宽限，随后 lifespan 收口任务，超时的外部执行保持待核对状态。恢复与迁移顺序见 [第 19 课](../docs/lesson-19-durable-runtime.md)。

第 20 课多副本队列、Scheduler、0015 迁移与恢复入口见 [运行说明](../docs/lesson-20-production-runtime.md)。

### 第 21 课的首次发布与升级

所有 Agent 副本使用相同的镜像、`COMPASS_CONTEXT_SCOPE_PROFILES` 和指标词典。首次上线时运行 0016 迁移，用平台管理员在 Web「平台管理 → PromptOps」导入随包文件并整体发布。首次生效前，管理入口可用，受管模式下的新分析任务会提示配置不可用。可用单文件发布验证新 Run 使用新正文并从管理历史核对回执。

从 0015 滚动切换时先暂停新受理并退出旧消费者，备份 Agent 数据库和密钥，执行 0016 迁移。过渡窗口可设置 `COMPASS_CONTEXT_RUNTIME_ENABLED=false`。全体接入端与消费者使用兼容版本且 S001 可读后，统一启用该开关并恢复流量。若已在过渡模式运行，切换开关、范围映射或指标词典前，先停止 Agent 和 Scheduler，由迁移所有者清空执行策略指纹再统一重启，步骤见[集群策略变更](../docs/lesson-20-production-runtime.md)。恢复整库快照会生成新快照，之前受理的 Run 仍按原代次读取。数据库恢复步骤见 [agent/main](../apps/agent/db/main/README.md#0016-提示词发布与运行上下文)。

`COMPASS_CONTEXT_SCOPE_PROFILES` 接受 JSON 数组，元素包含 `tenantId`、可选 `workspaceId` 和 `profile`（`finance` 或 `operations`）。ID 使用实际租户和工作空间 UUID。本地开发通过 `COMPASS_METRIC_DICTIONARY_PATH` 指向经审核的词典文件或含 `current.json` 和保留版本的目录。使用 Compose 时可在部署覆盖文件中为 Agent 增加相同的只读挂载，并用 `COMPASS_CONTAINER_METRIC_DICTIONARY_PATH` 指向容器内路径。

## 工作区经营语义 Registry

备份两库与 Agent 私有状态密钥，暂停受理，排空使用旧策略的在途和等待任务，停止旧 Agent 与 Scheduler。停用旧应用后迁移 Web 至 `0008_semantic_asset_grants`（其中 `0007_semantic_cleanup` 完成目录收缩），再迁移 Agent 至 `0018`。Web 先检查旧发布是否仍限制访问，Agent 核对 Run 已固定的发布清单。升级成功后启动新版 Web、Agent 和 Scheduler。在 Web 的 PromptOps 导入随包变更并整体发布。复用任意原发布时仍需保留对应的提示词代次与代码策略。

在「数据与语义 → 指标口径」登记来源与维护范围，由两名独立责任人依次审核定义和工作区绑定。先发布平台定义，再按工作区发布本地执行绑定。把获准工作区 UUID 用逗号分隔写入 `COMPASS_SEMANTIC_WORKSPACES`，所有 Agent 副本采用同一配置，按集群策略变更流程重启。默认空值使用已配置的文件字典。切换后用一个已发布指标核对实际来源、单位和期间，再恢复新流量。

回切定义可在发布历史选择旧版本并填写理由。需要停用 Registry 时，先排空 Registry Run，再调整工作区配置。保留历史发布、Run、PromptOps 和来源凭据供重新鉴权。数据库恢复在新库进行，步骤见 [Web 恢复说明](../apps/web/db/main/README.md) 和 [Agent 恢复说明](../apps/agent/db/main/README.md)。

## 工作区资料检索

在 Web 部署宿主设置 `COMPASS_DOCUMENTS_ROOT` 为已发布包目录的绝对路径，默认目录为仓库下的 `work/document-releases`。Compose 将它只读挂载到 Web 容器的 `/srv/compass/documents`。发布、撤销某份文档或改进资料后，以原子替换方式更新 `bindings/<tenantId>/<workspaceId>.json`，保留各代 `collections/<sha256>.json`。新运行采用通用搜索与交付流程；已有绑定中的场景字段仅供旧协议运行恢复。

Web 容器以 `node` 用户将 Run 固定记录写入 `/var/lib/compass/document-pins`。Compose 默认使用 `documents-pins` 持久卷。已有共享文件系统可设置宿主 `COMPASS_DOCUMENTS_PIN_DIR` 为可写的绝对路径，确保 UID 1000 有目录权限并支持同目录硬链接；所有 Web 实例使用同一套发布目录与固定记录。备份时一并保存两者和 Agent 数据库。回看旧 Run 时由 Web 读取其固定记录，并重新核对当前授予的权限。

部署包含答案协议 v2 的 Agent 后发布对应 PromptOps 版本。新 Run 固定通用答案协议，首次文档检索固定资料版本；来源权限和连接可用性在各工具中检查。旧 Run 沿其已保存协议恢复。课堂专用时点副本和验收入口位于[第 23 课案例目录](../examples/showcases/lesson-23-agentic-search/)。

Web 数据库迁移到 0010_knowledge_library 后可从管理页导入文件。先部署含 knowledge-embeddings 接口的 Agent，再启动新版 Web。复用已安装的 pgvector 扩展与 Agent 的 OpenAI Provider。管理页修改后的文件分段及向量在 Web 数据库备份内；由新 Run 固定的内容快照保存在现有 documents-pins 卷。

## 上下文原件共享卷

Agent 的 `context-results` 私有卷挂载到 `/var/lib/compass/tool-results`，所有 Worker 与同镜像 Scheduler 使用一致配置。复制 Agent 实例时共享该目录，保证已保存 ref 可在接管实例读取；禁止挂载到 Web 静态目录。保留结果卷与 Agent Journal 一起恢复，使用 `pnpm deploy:down` 停服务时保留卷。

新 Run 默认启用 `COMPASS_CONTEXT_OPTIMIZATION_ENABLED`。JEV 使用已配置的 decision Provider 及 `COMPASS_AUTONOMY_MODEL_PROFILES` 预算档案；未配置时保留历史并按容量进入摘要。发布前通过 PromptOps 发布新增选择、摘要及回读工具资产。该功能新增 0 张表，修改 0 张表。

### 长任务目标升级

第 25 课使用 Agent 0019 与现有 Web schema。先备份 Agent 与私有状态密钥、停止旧 Agent / Scheduler，再执行 Agent 迁移；发布 Goal 和委派提示词后启动新版消费者。旧进程须全部退出后按运行说明更新共享策略指纹。Goal 循环独立于运行 Worker 的健康检查，验收需观察目标实际提交下一轮、消费者领取、子任务交付和预算结算。[数据库顺序与恢复](../apps/agent/db/main/README.md#0019-目标与进度账本)
