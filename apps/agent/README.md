# Agent

从原 `apps/agent-api` 迁入，定位为 Agent Runtime 与推理相关业务应用。FastAPI + SQLAlchemy + psycopg + Alembic，Python 包名 `compass-agent`，导入入口 `compass_agent`。`package.json` 仅将 Python 命令暴露给 Turbo；Python 依赖以 `pyproject.toml` / 根 `uv.lock` 为准。

从工程根运行 `pnpm dev -F agent`，统一加载根 `.env`。本目录 `turbo.json` 为构建和静态检查声明 Python 工具链缓存指纹；开发、迁移及 seed 不缓存。

- `/healthz`：进程存活，无数据库依赖。
- `/readyz` 检查数据库版本 0019、运行表、模型与数据连接、工作流资产和提示词表。迁移、Runtime 密钥、Redis、运行协议或共享策略检查失败时返回 503。
- `/v1/demo/overview`：固定合成数据聚合；默认关闭，启用后仍只读。空数据或数据库故障返回 503。

Feature 993 增加进程内 LLM Runtime、平台模型 Provider 配置、能力测试和 Chat 调用。`models/` 拥有配置与元记录，`llm/` 统一参数、适配器、结果和错误；`api/models.py` 提供内部 HTTP 入口。账户、租户与权限由 Web / BFF 拥有，每次调用先完成短事务鉴权，再携服务令牌及可信上下文调用 Agent。Agent 校验上下文与 Provider 状态，不访问 Web 业务表。Feature 989 在此基础上增加进程内 Run / Step、顺序 ReAct、固定 Retail 查询、SSE 进度、CSV 与在途终止；第 16～18 课补充计划预算、Workflow、统一工具调度、MCP 与独立 Sandbox；第 19 课接入持久 Session/Run、Redis 投影与人工确认，见 [运行说明](../../docs/lesson-19-durable-runtime.md)。

数据库迁移唯一源为 `migrations/`，受管入口和 SQL 交付见 [agent/main](db/main/README.md)。首次运行 `pnpm db:provision -F agent`，再执行 `pnpm db:migrate -F agent`。供应、迁移与运行凭据分离；`pnpm db:seed -F agent` 使用运行账号幂等写入三天虚构数据。

单元测试使用内存 SQLite 验证聚合、seed 幂等、开关和错误行为；PostgreSQL 差异通过真实迁移、seed 和根 `pnpm smoke` 验收。迁移与运行初始化分开，模块导入和 OpenAPI 导出不会连接数据库。

## 模型接入

在根 `.env` 设置 `COMPASS_AGENT_SERVICE_TOKEN`（Web 与 Agent 一致）、`COMPASS_MODEL_ENCRYPTION_KEY`（Fernet 密钥）和 `COMPASS_MODEL_ALLOWED_ORIGINS`（逗号分隔的精确 HTTPS origin）。可分别用 `uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'` 与 `uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` 在本机生成前两项，妥善保存。密钥留空时模型入口返回不可用，既有工程演示保持独立。

模型凭据从平台管理页面录入，数据库只保存密文。加密密钥须与数据库备份独立保存；直接替换加密密钥会使旧配置无法解密。本期没有在线密钥轮换工具。

目录读取锁定版本 LiteLLM 的本地清单，并补充课程重点模型及 System One 决策预设。DeepSeek API 标识为 `deepseek-v4-pro`、`deepseek-flash`；别称可使用课程名称。阿里云决策服务配置工作区 Base URL `https://<工作区>.<地域>.maas.aliyuncs.com/compatible-mode/v1`；JEV 官方使用 `https://api.typesafe.ai/v1`。通用 `SystemOneDecisionAdapter` 追加 `/systemone`，两家均需 Bearer API Key，并将对应 origin 加入允许列表。目录含 `aliyun/decision-model-preview` 与 `typesafe/jev-latest` 预设，也可手工使用 `aliyun/<model>`、`typesafe/<model>`、`systemone/<model>`，API 模型名无白名单。支持 Choice/Noul/Score、结构化指令及判据、实际输入/输出用量；图片与 Box 实验能力暂未开放。升级前备份 Agent 库与加密密钥，运行 `pnpm db:migrate -F agent` 到 0003 后启动新版应用，旧阿里云配置自动保留。平台测试和 Chat 复用 LiteLLM 支持的生成模型协议，决策模型在管理端测试。各租户管理员须显式授予 `models.invoke`，旧权限模板不会自动获得该动作。

每个生成调用执行一次 LiteLLM SDK 推理调用，关闭重试与 fallback；认证及协议交互由 SDK 处理，超时上限 180 秒；取消与错误保留安全元记录，不保存对话正文。工具测试只接收手工填写的结果，Thinking 只显示开关与强度元信息。复杂路由、重试、Fallback、代理服务和 Agent Loop 未包含在本期。

V2 按 [Runtime 目录说明](src/compass_agent/llm/README.md)和[模型应用服务说明](src/compass_agent/models/README.md)划分生命周期：Runtime 管模型执行、绝对期限、取消和资源关闭；ModelService 管配置快照、凭据与调用记录；HTTP 层管 SSE 队列和事件编码。两类适配器实现同一类型化入口，Runtime 可通过 FakeAdapter 独立验证。数据库已升级至 0003，公共 HTTP DTO 与事件名保持兼容。

验证入口：`pnpm lint -F agent`、`pnpm typecheck -F agent`、`pnpm test -F agent`、`pnpm test:integration -F agent`。最后一项需要独立 PostgreSQL 配置，见数据库说明。`tests/e2e_app.py` 是显式测试宿主，以真实 API/数据库配合受控上游 HTTP 验证 Web 调用链；生产入口使用 `compass_agent.main:app`。真实供应商验收另外使用已授权账号与密钥，状态见 [993 实施记录](../../mai-specs/993-lesson-11-llm-runtime/IMPLEMENTATION.md)。

## 离线业务数据准备

完整零售业务数据由独立的 [tools/data-seed](../../tools/data-seed/README.md) 生成、校验和发布，使用 `pnpm data:seed`。工具及其零售迁移、测试已移出 Agent；Agent 包和运行镜像只携带自身代码。

`AGENT_API_URL` 为 Web 的内部 HTTP 接口地址配置，Compose 内服务地址为 `http://agent:8102`。统一 Dispatcher、个人 MCP 与本地 Sandbox 控制见下文第 18 课；Memory Tools 按后续切片接入。

本期顺序执行内核位于 `src/compass_agent/runtime/`；已交付的单次模型调用仍位于 `src/compass_agent/llm/runtime.py`。持久任务、状态、恢复与人工确认由 runtime 内部模块分工管理；模块职责和目录规则见 [LLM Runtime 说明](src/compass_agent/llm/README.md#目录演进)。

## 第 15 课 Agent Run

`POST /v1/analysis/runs` 返回 `compass.run` v1 SSE，首包在任何外部派发前提供 Run ID；`POST /v1/analysis/runs/{run_id}/cancel` 受理同一所有者的停止请求。执行内核位于 `src/compass_agent/runtime/`，最多 20 个模型 Step、每步 4 条顺序工具调用，Run 总期限 180 秒。原模型管理、诊断和单次 Chat API 保持原契约。

默认工具为 `todo_write`、授权连接可用时的 `database_query` 和沙箱可用时的 `python_analysis`，另按个人配置发现 MCP。模型通过表、字段和只读 SQL 自主查数，按需执行 Python。数据连接仓储位于 `data_connections/`，保留已有配置、凭据、租户限制和默认连接初始化。普通 PostgreSQL 连接通过 `COMPASS_DATA_ALLOWED_ENDPOINTS` 配置允许端点，历史 Retail 连接沿用专用端点和只读身份。当前 MySQL 仅支持配置与连通性测试。
开发配置、课堂问题、对照数字、终止与真实 PostgreSQL 验收见 [课堂复现](../../docs/lesson-15-agent-run.md)。执行内核选择与后续边界见 [ADR 0002](../../docs/adr/0002-agent-execution-kernel.md)。

## 供应商协议与连接配置

生成模型复用当前锁定的 LiteLLM Core 协议和供应商注册表，目录包含 SDK 的 Chat / Completion / Responses 桥接模型，支持手工 `provider/model`。DeepSeek 的两款型号和阿里云决策模型是课程重点联调对象；接入与 Chat 没有型号白名单。SDK 负责原生协议、参数映射、认证、响应和流事件归一，Compass 负责配置/权限、期限/取消、可见结果边界及元记录。

Base URL 可留空使用 SDK 默认端点；自定义端点须在 `COMPASS_MODEL_ALLOWED_ORIGINS` 中配置 HTTPS origin，路径按供应商接口填写。API Key 可留空，使用供应商原生认证或 SDK 服务端凭据。添加窗口的供应商连接参数 JSON 直接承接 SDK 连接字段，例如 `api_version`、`aws_region_name`、`aws_access_key_id`、`aws_secret_access_key`、`vertex_project`、`vertex_location`、内联 `vertex_credentials`；已声明 boto3、google-auth、azure-identity 原生认证依赖，以及 Responses 桥接所需的 mcp 2.x；其他供应商可选依赖按 SDK 文档安装。执行参数、SDK 客户端/回调、模型绑定及重试字段由应用维护。

连接参数与 Key 使用已有 Fernet envelope 加密保存，旧密文兼容；接口仅返回是否配置。编辑留空保留，连接参数 `{}` 清空。SDK 自有连接池由 SDK 管理，Runtime 关闭自己持有的流。原生非流式响应由 SDK 缓冲，应用校验归一后结果和续接大小；旧 DeepSeek 分支保留锁定 SDK 版本的 HTTP 守卫，原生协议不套用 OpenAI 的流结束格式。

## 两级意图与模型路由

`POST /v1/analysis/runs` 是业务推理入口，根据场景与任务特征选模，再通过 Agent Runtime 推进模型和工具。请求通过 `selection` 选择固定 Provider 或自动路由，服务端通过 `run.routed` 返回实际模型、场景和工具可用状态。旧 `POST /v1/models/calls` 已下线，Provider 诊断和 `GET /v1/models/calls/{call_id}` 继续可用。

业务问题使用同一通用工具装配。Agent 在执行时检查当前工作区的授权连接，以及实际选中的模型是否支持工具调用。连接在 Web 的「数据连接」管理，模型工具能力在「平台模型管理」查看。纯闲聊仍引导回经营分析，明确的经营问题可执行已授权查询。

[路由模块说明](src/compass_agent/routing/README.md) 登记角色配置、字典派生和次数/时限。`SC-39` 仍使用 Flash。路由模块沿用既有模型与调用表。

## 第 13 课可靠执行

主生成最多两次尝试，分类/诊断/决策各一次；整轮 Chat 共用 180 秒父期限。进程默认 K=8、N=32。已输出片段的中断不自动重试，失败片段不进入后续上下文；取槽后到期正常形成超时摘要，关闭未确认时隔离执行槽。

平台管理员的 `/v1/models/reliability/scenarios` 和 `/v1/models/reliability/runs` 提供固定模拟演示，运行真实 SDK/Runtime/执行器，无真实供应商出站或业务表写入。入口由 BFF 平台权限及可信上下文保护；运行每进程最多一个、最多 20 条调用/20 秒。详见 [演示模块](src/compass_agent/reliability/README.md)。可靠性摘要由 0004 引入，当前数据库版本为 0019，升级和恢复见 [agent/main](db/main/README.md)。

## 第 14 课调用观测

Agent 使用 OTel 1.45.2 显式关联请求、分类、路由、逻辑调用、排队、尝试和收尾；观测为可选的进程内异步路径。服务鉴权后提取 W3C carrier，多实例通过唯一 instance_id 和同一项目/Trace 关联，Provider 请求不透传内部上下文。状态与固定模型聚合接口执行平台 manage 权限，普通用户只通过响应头获取本轮编号。

模块、采集边界和测试见 [observability](src/compass_agent/observability/README.md)，本地后端、凭据和持久卷见 [运行说明](../../deploy/observability/README.md)。调用记录由 0009 扩展持久尝试与加密内容；观测样本不承担正式账单或强审计职责。

## 第 16 课受控分析

分析 Run 支持进程内 Todo、逐次模型预算和停止策略。启用与课堂演示步骤见 [第 16 课运行说明](../../docs/lesson-16-controlled-autonomy.md)。

通用 Todo、数据库查询与 Python 分别位于 `tools/builtin/` 下各自的同名目录。`harness/AgentExecutor` 统一推进模型循环、调用工具和回填消息；`runtime/RunService` 装配路由、工具、运行状态与结果检查。可选核验钩子由调用方注册。最终分析正文来自模型回答，工具结果和下载产物独立发布。

服务端装配 `RunService` 时，可用 `verifiers` 注册全局检查，用 `task_verifiers` 按展示任务或场景 ID 注册检查。代码调用 `run(..., verifiers=(check,))` 可追加单次检查。钩子接收候选结果的副本和可信调用上下文，可抛出 `VerificationRejected` 拒绝。钩子应只读且可重复执行，持久 Run 恢复时可再次核验。默认钩子列表为空。

## 第 17 课混合工作流

第 17 课的两个 Chat 预置分别用于探索并保存、换周期复盘。模型调用 `create_workflow` 提交 JavaScript 编排、函数参数 JSON Schema 和试跑入参。QuickJS 运行脚本，`sub_agent` 在独立进程内使用自己的上下文和授权只读查询。用户确认后源码与工具契约写入个人资产库，新的 Run 按当前主体加载可用资产，由模型决定是否调用并生成新参数。

在「插件 → Workflow」查看源码、改名、启停或导出。旧零售契约资产保留管理和导出。课程场景资料见 [Showcase 目录](../../examples/showcases/README.md)。

切换版本前运行 `uv run --env-file .env --package compass-agent python apps/agent/scripts/check_runtime_upgrade.py`。有受影响的非终态 Run 时，该只读检查退出 1；先在旧版本完成或通过现有控制接口明确取消，再切换。该脚本不修改 Run 或用户资产。

## 执行过程公开投影

`runtime/execution_trace.py` 持有本次 Run 的统一执行节点，将主循环、现有工具、Workflow、Skill 和临时 SubAgent 的安全状态发布为 `compass.execution.node@1`。来源、环境和并行关系有独立字段，完整参数、私有推理与工具观察仍留在执行侧。QuickJS 任务函数的派发可携并行 groupId；完整工具执行及异步调度上下文在第 18 课完善。当前节点无需新增迁移，节点由第 19 课共享事件协议保存并恢复。

## 第 18 课统一工具执行

运行与配置见 [工具执行说明](../../docs/lesson-18-tool-runtime.md)。`routing/` 管意图、模型和确定性任务分流，`runtime/` 管 Run 生命周期及应用装配，`harness/` 管通用模型循环、上下文、系统提示词和核验钩子，`workflows/` 管理动态编排、执行与资产。[工具目录](src/compass_agent/tools/README.md)区分 `tools/builtin/` 通用工具与 `tools/business/` 业务工具，每个工具独立目录并在 `tools/catalog.py` 统一注册；`tools/mcp/`、`tools/sandbox/` 拥有远端连接和执行后端。

0008 增加个人 MCP 配置与 Sandbox 分配租约。OpenSandbox + NFS-Ganesha 本地启动入口为 `pnpm sandbox up`。工具的环境、副作用、审批和批次策略分别配置；审批与原 Run 接续由第 19 课持久服务处理。

在独立沙箱运行 `pnpm test:integration -F agent` 时，用 `COMPASS_SANDBOX_TEST_DOMAIN`、`COMPASS_SANDBOX_TEST_HOST`、`COMPASS_SANDBOX_TEST_VOLUME_PREFIX` 和 `COMPASS_SANDBOX_TEST_SLOTS` 指定控制端、主机和私有卷。启用 `COMPASS_SANDBOX_FAULT_TESTS=true` 后，可通过 `COMPASS_SANDBOX_TEST_CONTROL_CONTAINER` 与 `COMPASS_SANDBOX_TEST_NAS_CONTAINER` 指定中断测试的两个容器。测试会核对容器的 `compass.acceptance.owner` 标签与测试主机名。

第 20 课多副本消费、Scheduler、0015 迁移与恢复入口见 [运行说明](../../docs/lesson-20-production-runtime.md)。

会话首次受理时保存由首条消息生成的临时标题。Agent 后台用已选模型或 Auto 的 Flash 模型生成短标题，持久化到会话。手动改名优先，已结束会话支持软删除。数据库升级与恢复见 [agent/main](db/main/README.md)。

## 通用 Agent 装配

目录边界见 [Harness](src/compass_agent/harness/README.md) 和 [Runtime](src/compass_agent/runtime/README.md)。当前执行入口统一为 `runtime/service.py`；HTTP 路径中的 `/analysis/` 保持兼容。

`AgentDefinition` 提供系统提示词、`RegisteredTool` 集合和启用的内置能力。Runtime 默认定义为空工具集；产品 HTTP 入口通过 `compass_definition()` 显式装配 Todo、数据库、Python 与 Workflow 能力。任务可以提供自己的提示词与工具，个人 MCP 工具继续通过同一 Registry 加载。单纯使用自定义工具的 Agent 不加载 Retail 连接。

课程案例与手输问题使用同一请求结构和模型循环。新请求拒绝 showcase 和 constraints.verification。已受理的旧请求由仅回读回执的兼容边界处理；旧历史解码不重新启用已移除能力。通用工具策略、审批、预算和持久恢复继续共用 Runtime。

模型指令、场景 SOP 和工具描述遵循 [Prompt 资产格式规范](../../docs/prompt-format.md)，按 LangGPT 结构组织并注明模板变量；格式修订保持原有约束、输出协议与工具 Schema。

平台管理员可在 Web 的 Prompts 导入随包 Markdown 与 JSON 资产并整体发布首个版本。场景说明集中为一份 JSON，工具显示完整声明，参数 Schema 以代码为权威来源，在线可编辑描述、参数说明和调用示例。阶段、公共规则与前端示例按职责归组。每次发布生成完整生效清单，受管 Context Runtime 在新 Run 受理时固定该清单。[资产结构与显式数据升级](../../mai-specs/975-lesson-21-context-runtime-promptops/design/ASSET_MODEL_UPDATE.md)说明了历史保留、草稿迁移和旧格式快照恢复；表结构说明见 [Agent 数据库 0016](db/main/README.md#0016-提示词发布与运行上下文)。

## 第 22 课语义查询

Agent 在受理时固定工作区的发布清单。正式查询通过 Registry 解析指标版本、允许的维度和执行绑定，再使用当前主体的受限连接取数。模型根据工具回执继续查询、澄清或交付已有结果。缺少可执行绑定时，可按用户指定的定义从获准明细试算，并说明计算方式。报告和回看会核对实际权限。开放分析结果可经本人确认转为私有指标草稿。七个工具、启用条件和部署顺序见[工具目录](src/compass_agent/tools/README.md)和[第 22 课发布说明](../../deploy/README.md#工作区经营语义-registry)。

## 第 23 课跨源检索

普通 Chat 的新 Run 使用同一个 AgentLoop，自由选择语义、数据库、知识库、代码执行、MCP 和现有委派工具。没有搜索模式开关。Run 固定语义与 PromptOps 版本；文档工具首次调用时固定资料，空库返回空状态，访问权限继续由工具及来源 owner 执行。

工具结果通过注册的 evidence_adapter 接入 after_tool_batch。模型调用 final_answer 提交 plain / evidence 候选；before_delivery 检查结构、引用与计算，再由同主模型在既有 Loop 中执行 purpose=verify 审阅。缺口回到工作步骤；合格的部分回答可以交付。最终正文、引用、附件与出箱事件原子保存，前端按 answerMode 划分正文呈现。

新 Run 固定 answerProtocol=2，候选与 Review 使用既有私有运行记录。旧 Run 保留原协议恢复。模型上游需支持工具调用或结构化输出，提示词更新沿既有 PromptOps 发布入口进行。通用方法和工具说明随源码提供，运行前核对发布版本。

课堂资料、受限数据库、教学分析员的资产授权及独立金标准由[第 23 课课堂入口](../../examples/showcases/lesson-23-agentic-search/README.md)装配。当前实现与验证见[技术方案](../../mai-specs/973-lesson-23-agentic-search/design/TECH_DESIGN.md)。

## 第 24 课上下文选择与压缩

`COMPASS_CONTEXT_OPTIMIZATION_ENABLED=true` 为新 Run 固定优化版本。小工具结果直接追加；超过 2,048 的 UTF-8 token 上界时保存完整原件，模型收到至多 512 的回执，通过 `read_tool_result` 分页读取。数据库 SELECT 在同次快照中流式保存全部实际返回行。

历史达到 8 个完整轮或请求达到有效输入容量的 70% 时先用 JEV 选择旧轮，最近 3 轮受保护。JEV 不可用或完整共享背景超窗时保留未判断轮；仍有容量压力则剪枝、摘要并按预算回填最近完整轮。系统提示词和内置材料沿原装配逻辑保留。

JEV 复用平台启用的 decision Provider；可通过 `COMPASS_ROUTING_JEV_PROVIDER_ID` 显式指定。需在 `COMPASS_AUTONOMY_MODEL_PROFILES` 中登记对应 Provider、绑定版本、输入输出预算及费率；decision 的 `sdk_model` 可为 null。预算缺失只跳过可选选择调用。选择与摘要按原 Journal 结算、取消及恢复。

`COMPASS_CONTEXT_RESULTS_ROOT` 应设置为所有 Agent Worker 可访问的同一绝对路径；目录权限 0700、文件 0600，禁止映射为公开静态目录。原件引用随会话删除清理，跨 Run 回读重新检查身份、会话重置点及来源权限。关闭优化只影响新 Run；旧 Run 继续按受理时的版本恢复。本课新增 0 张表，修改 0 张表。

## 第 25 课长任务目标与委派

模型使用单个 `goal` 工具提出目标草案；用户确认后，Goal 控制循环按已交付终态结算并提交下一轮。默认上限为 12 轮、200 万 Token、2,000 万微美元。目标、验收项、约束、未完成待办与失败记录在每次主模型调用前重新装配，历史压缩保留这些内容。运行权限、来源权限和连接修订号在恢复与复用时重新核对。连续三轮无进展或失败会停止自动推进。

`goal` 支持 `propose / record / complete / block`。完成核验检查验收项支撑、必需待办、冲突和失效事实；缺口形成补救待办。查询与 Python 结果按工具指纹复用，证据复制到当前 Run 后继续通过既有交付核验。账本属于本会话，每个目标最多 400 条。

`sub_agent.tasks` 一次提交 1–4 个独立任务，同时运行至多 2 个，每个任务最多 3–8 步。每个子任务使用独立进程，只继承父任务允许的只读工具、连接和显式输入。`report_to_parent` 的 progress 更新进度，final 立即结束并单次交付；宿主核对证据归属后合入账本。历史 prompt 入参继续兼容。

父子任务共用 Run 的 Token 和费用余额；每次模型调用按配置上界预留，并把所有在途子任务的预留额计入总上限，返回后按实际用量结算，未知用量保守结算。子任务继续使用独立的步数和工具次数额度。预算准入拒绝的停止原因随私有模型回执保存，恢复时还原；旧回执中的 `autonomy_admission_denied` 也按预算失败恢复，保持子任务交付一致。

检查点不一致、损坏、私有状态无效或预算策略不兼容会将 Run 停在 `paused / blocked`，调度任务进入 `parked` 并保留原因，释放执行槽位。关联的进行中 Goal 同步进入受阻状态并给出说明，用户主动暂停的目标保持暂停。自动恢复不重复领取这些任务；普通租约到期和进程中断仍走原恢复路径。

目标控制接口为 `GET /v1/analysis/sessions/{id}/goal` 和 `POST /v1/analysis/sessions/{id}/goal/actions`。子任务详情通过 `GET /v1/analysis/runs/{id}/children/{childId}` 按当前权限读取独立执行子树与完整交付回答（`answer`）；回答从已接受的私有回执读取，并在每次请求复核来源权限。尚未交付时 `answer` 为 `null`，主 SSE 契约保持兼容。

升级至 [agent/main 0019](db/main/README.md#0019-目标与进度账本)，导入并发布随包 Goal、委派与子任务回传提示词。Goal 需要调度消费者、受控自主预算和答案协议 2 同时启用。课程对比实验见[三种上下文策略](../../examples/showcases/lesson25_context_strategies/README.md)。
