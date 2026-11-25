# Web / BFF

Next.js App Router + React + TypeScript。UI 与 BFF 在一个 Node 服务中部署。当前交付 [Feature 995](../../mai-specs/995-web-management-foundation/README.md)：本地账号、数据库会话、租户与工作区、直接成员、固定模板和个人权限、个人设置、通知与审计，以及原型完整导航框架。Feature 993 增加平台模型路由管理、测试与分析 Chat 模型选择；Feature 989 将 Chat 接入同步 Agent Run，展示通用工具过程、最终回答和产物，并支持终止。决策模型仍仅在管理端测试。通用文件上传、报告、看板、插件、定时及其余 Agent/Memory 管理保持待接入状态。

## 启动与账号

第 14 课在平台模型管理页增加“调用观测”、本实例导出状态与两模型只读对比；Trace 详情进入独立登录的本地 Langfuse。Provider 诊断收到响应头后可显示观测编号，分析 Chat 的观测编号可从 Run 响应头取得。平台 API 沿用管理员鉴权，浏览器 Trace Context 不进入内部链路。部署与数据边界见 [观测运行说明](../../deploy/observability/README.md)。

从 Compass 根目录复制 `.env.example` 到 `.env`，安装依赖后执行：

```bash
pnpm db:up
pnpm db:provision -F web
pnpm db:migrate -F web
pnpm db:manage -F web -- create-user --login-name local.admin --display-name 本地管理员 --platform-admin true
pnpm dev -F web
```

账号工具从隐藏终端输入读取密码（12–128 字符），流水线须显式使用 `--password-stdin`；密码不作为命令参数。打开 <http://127.0.0.1:8100/login>，平台管理员创建租户时指定已有账号为初始租户管理员。平台管理员可直接进入所有有效租户及其工作区，获得完整模块与管理权限；登录后恢复最近工作区或首个可用入口，平台页保留完整导航。租户停用、空间归档、资源归属及个人资料/会话边界继续校验。

本地演示可显式设置 `WEB_FIXTURE_PASSWORD` 后执行 `pnpm db:seed -F web -- --fixture teaching`，初始化 `teaching.platform/admin/analyst/viewer/auditor/outsider` 六个合成账号及两租户三空间。演示首选 `teaching.platform`，同时拥有平台管理员角色和两个教学租户的管理员成员身份；`teaching.admin` 管理远山零售。已有环境重复执行同一命令即可补齐缺失的演示管理员成员身份，无需重新设置密码。已有角色或停用状态会保留。默认 seed 不写演示数据。禁止在共享环境使用此 fixture。

教学身份就绪后，可用 `pnpm db:seed -F web -- --fixture retail-semantics` 安装两家零售租户的语义资产。来源、指标口径、独立 SQL 与重复执行说明见 [Web 主库](db/main/README.md#零售语义教学包)。

`WEB_DATABASE_URL` 仅供服务端运行；`WEB_MIGRATION_DATABASE_URL` 供迁移和受管账号工具；`WEB_PROVISION_DATABASE_URL` 仅供本地数据库供应。迁移与完整 SQL、权限、恢复见 [web/main](db/main/README.md)。`WEB_PUBLIC_ORIGIN` 必须与浏览器来源完全一致；正式环境使用 HTTPS，本地 HTTP 必须显式设置 `WEB_AUTH_ALLOW_LOCAL_HTTP=true` 并使用 loopback。会话固定有效期默认 12 小时，上限 12 小时。

受管工具另支持 `reset-password --login-name ...`、`set-status --login-name ... --status disabled|active`、`set-platform-admin --login-name ... --platform-admin true|false`，安全变更撤销该用户全部会话并保护最后有效管理员。`cleanup-sessions` 清理过期或撤销超过 30 天的会话。审计不自动清理；部署方须安排受控备份、容量监测和保留策略，普通运行角色不能修改或删除历史。

开发启动 `pnpm dev -F web` 会先停止目标端口的监听进程及当前 Web 目录的旧 Next 开发实例，再启动自身；先正常退出，5 秒后仍未退出则强制结束。默认端口 8100，支持 `PORT` 或 `-- --port 8105`。该入口依赖 macOS / Linux / WSL2 的 `lsof` 和 `ps`，会终止占用目标端口的其他应用。

页面地址使用稳定短标识，例如 `/t/yuanshan/w/huadong/analysis`。创建租户或空间时填写地址短标识；改名保持地址不变。旧 UUID 链接自动跳转，API 与权限上下文继续使用 UUID。分析侧栏提供新建按钮，会话按最近更新时间倒序分组，滚动接近底部时自动加载更早会话。侧栏顶部仅在分析模块显示“Compass / 分析空间”；其他模块直接显示一级导航标题，正文不重复标题。导航图标直接使用原型 SVG 图形，一级与二级菜单逐项映射；按钮、菜单行、浮层和移动抽屉按 [导航 UI 对照规范](../../mai-specs/995-web-management-foundation/design/NAVIGATION_UI.md) 维护。

## 工程边界

[服务端模块](src/server/README.md) 按 identity/access/audit/notifications 划分，运行时 SQL 和连接使用 `server-only`。受保护页面通过 Next App Router 局部导航，共享布局保留侧栏与顶部框架；搜索、分页、空间切换及保存后刷新保持 SPA 交互。首次进入与每次切页均校验当前权限后显示内容；断线、身份变化、超时立即清屏，恢复时重新加载文档。业务写入、审计与通知共用一个事务，普通查询使用可重复读快照，审计导出提交记录后才返回 CSV。

`GET /healthz` 检查进程，`GET /readyz` 检查配置及 Web 迁移版本；`/api/web/v1/*` 是严格校验的本地业务 API，响应 `private, no-store`。Web 基座运行只依赖自己的 PostgreSQL。Agent、Memory、Worker、IdP、模型和文件服务无需在线。

`/demo` 与 `/api/demo/overview` 保留原工程演示，需要 `COMPASS_DEMO_ENABLED=true`；后者经服务端 `AGENT_API_URL` 调用 Agent，超时或契约不兼容返回 503、始终 no-store。演示类型由根 `pnpm contracts` 生成，禁止直接修改 `src/generated`。Web build/typecheck 的静态契约生成仍需 Python 工具环境。

样式扫描限定在 `src/`，避免开发日志、测试产物与缓存变化触发热更新。

## UI 规范与组件演示

[Compass UI 样式规范](../../docs/ui-style-guide.md)统一基础样式、组件状态、代码展示和交互。启动 Web 并登录后，在「体验指南」（`/guide`）查看和操作实际组件。

## 验证

```bash
pnpm lint -F web
pnpm typecheck -F web
pnpm test -F web
pnpm build -F web
pnpm db:sql:check -F web
```

数据库与浏览器测试必须提供专用的 `WEB_TEST_DATABASE_URL`、`WEB_TEST_MIGRATION_DATABASE_URL`，库名限定 `compass_web_test_*`；迁移与恢复另需 `WEB_TEST_ADMIN_URL`。测试会重置专用测试库，绝不使用日常库。`WEB_TEST_PG_CONTAINER` 可指定同一测试 PostgreSQL 容器执行 17 版 pg_dump/pg_restore；未设置时使用宿主对应工具。

```bash
pnpm test:setup -F web
pnpm test:integration -F web
# 浏览器使用另一专用测试库，并设置 WEB_TEST_PASSWORD
pnpm test:setup -F web -- --fixture teaching
pnpm --filter @compass/web exec playwright install chromium
pnpm test:e2e -F web
```

浏览器测试自行启动 8110 端口 Web，截图与失败 trace 放入忽略 Git 的 `work/995-playwright/`。只在已明确启动对应测试服务器时设置 `WEB_TEST_EXTERNAL_SERVER=true`。实际运行结果与验收映射见 [实施记录](../../mai-specs/995-web-management-foundation/IMPLEMENTATION.md)。

## 模型 Runtime 集成

平台入口 `/platform/models` 不要求租户成员身份；租户「管理与治理 → 模型路由」提供开放摘要。「模型路由」与「代码环境」是治理侧栏的独立入口，共用 `module.admin.capabilities` 访问权限，地址分别为 `admin/capabilities/models` 和 `admin/capabilities/sandbox`。代码环境页当前展示待接入状态。Chat 同时要求分析入口与 `models.invoke`，升级不自动授予新动作。BFF 在授权事务结束后调用 Agent，使用服务端 `AGENT_API_URL` 与 `COMPASS_AGENT_SERVICE_TOKEN`，不接收浏览器声明的内部身份。Provider 凭据只由 Agent 加密保存，Web 数据库不保存副本。配置步骤见 [Agent 模型接入](../agent/README.md#模型接入)。

模型浏览器测试需要额外启动 `apps/agent/tests/e2e_app.py`，配置专用 Agent 测试数据库、同一服务令牌和加密密钥，并使 `AGENT_API_URL` 指向该宿主。它只替换上游 HTTP，Web、BFF、Agent API 与数据库均实际运行。Playwright 使用 `.next-e2e-<端口>` 独立输出目录，保留日常开发服务。真实供应商验收单独记录在 [993 实施记录](../../mai-specs/993-lesson-11-llm-runtime/IMPLEMENTATION.md)。

模型目录复用 LiteLLM 支持的生成模型协议，添加 Provider 支持手工 `provider/model` 标识。Base URL 留空使用 SDK 默认；API Key 与供应商连接参数均只写不回显。连接参数 JSON 可配置 Azure 版本、Bedrock 区域及原生认证等，留空保留，`{}` 清空。Chat 按生成模型类型、启用状态、平台可见性和用户权限开放。

## 切换应用与页面状态

切换 App 或标签页时界面、滚动位置、草稿、附件、聊天与在途生成保持原样。离开和返回不触发遮蔽、禁用、刷新或恢复复验；定时会话检查静默执行，临时失败不影响当前界面。登录失效、权限变更、离线及文档离开仍清理旧内容；分析会话从 Agent Message 恢复到稳定 URL；整页刷新保留已提交消息、执行过程、审批和产物，未提交草稿与附件仍保存在页面内存。参见 [第 19 课](../../docs/lesson-19-durable-runtime.md)。

## 意图分流与响应

分析输入框可选择静态主模型或“系统优化”，每次发送创建一个 Run。Agent 先分类和选模，再用 `run.routed` 返回实际模型、场景和工具启用状态。闲聊命中 `SC-39` 后使用独立提示词调用 Flash 婉拒，并询问业务数据任务。

旧 `POST /api/web/v1/models/calls` 已下线。Provider 诊断继续使用 `ModelEvent` / `CallResponse`，调用记录仍可按 ID 查询。浏览器路由验收位于 `tests/e2e/intent-routing.spec.ts`，使用独立 Web / Agent PostgreSQL 库和受控上游。

## Agent Run 与通用分析

分析 Chat 的发送入口为 `POST /api/web/v1/analysis/runs`，停止入口为 `POST /api/web/v1/analysis/runs/{runId}/cancel`。两者复用同源、CSRF、有效工作区、`module.analysis` 与 `models.invoke` 检查；授权事务提交后，BFF 才携服务端可信主体请求 Agent。浏览器不能传内部身份、Retail 账号或连接信息。

BFF 严格校验 `compass.run` v1 的 UTF-8 SSE、包 Schema、序号、Run / Step / ToolCall 生命周期、唯一终态、文件来源和干净 EOF，再重新编号转发。页面显示简短进度，按执行顺序展示查询卡片。完整 EOF 后提交成功终态，以 Markdown 渲染 `finalAnswer` 并展示可用文件，追问历史保存回答原文。重置对话、退出、权限版本变化或范围切换会中止活动请求并撤销下载 URL。

排查时从 Run 的 `step.started` 事件取得模型调用编号，按 ID 查询调用记录。

平台管理员可在工作区的「数据与语义 → 数据连接」新增、编辑和测试多条 PostgreSQL 或 MySQL 连接。配置保存在 Agent 应用库，Web 校验活跃工作区、数据模块权限及管理权限后转发请求。PostgreSQL 查询工具发现当前工作区获授权的连接与表，模型自行查询；现有 Retail 连接继续保留。当前部署要求 Agent 单进程、单副本。默认连接初始化、对照 SQL 和停止演示见 [第 15 课复现说明](../../docs/lesson-15-agent-run.md)。

## 可靠性模拟测试

平台管理员进入 `/platform/models`（Agent 与模型）即可运行 11 个固定场景：正常、429/503 恢复、次数耗尽、认证失败、超时、半途断流、Schema 拦截、取消、并发过载及关闭失败隔离。页面展示真实事件、预览、尝试摘要、未知用量、容量及可提交结果；无需模型密钥。普通租户用户不能访问模拟接口。

Chat Run 与流式 Provider 诊断均在唯一终态及干净 EOF 后提交成功结果。普通调用详情展示 execution 摘要，旧记录没有明细时保持原显示。浏览器验收位于 `tests/e2e/reliability.spec.ts`，使用独立 PostgreSQL 测试库及 Agent。

## 分析计划面板

分析 Chat 将当前 Run 的 Todo 显示在输入框上方。用「先列计划 · 渠道与商品诊断」填入预置问题。Agent 启用与课堂配置见 [第 16 课运行说明](../../docs/lesson-16-controlled-autonomy.md)。

## 统一执行过程

分析 Chat 使用 `UiExecutionTimeline` 展示运行事件。运行时默认折叠，单行显示最近更新的事件和用时。展开后按时间排列思考与工具等事件，支持独立查看公开详情。完成时自动折叠为完成状态和总用时。`/guide#ui-execution` 演示同一个组件的状态和详情交互。样式遵循[全局 UI 规范](../../docs/ui-style-guide.md)。

课程案例按钮只填普通问题和通用约束。默认工具为 Todo、数据库查询与可用时的 Python，另按用户配置加载 MCP。Workflow 页面保留历史资产查看、改名、启停和脚本导出，旧执行契约已停止。临时课程能力按 [Showcase 规范](../../examples/showcases/README.md) 独立管理。

第 21 课在新建分析输入框下提供两个案例，可用相同题面对照销售部与财务部的默认指标和实际取数，再由讲师单独发布一份经营问答提示词，比较同一周会填报请求的两版回复。[第 21 课课堂演示](../../docs/lesson-21-context-runtime.md)

## 平台提示词管理

平台管理员从「管理与治理 → PromptOps」进入资产库，也可访问 `/platform/promptops`。首次上线时，先导入 Agent 安装包中的 Markdown 与 JSON 资产，核对使用位置，再整体发布。场景目录集中在一个 JSON 中；工具编辑器展示完整声明，参数 Schema 由代码维护并只读，描述、参数说明与示例可在线编辑发布。阶段、公共规则和前端示例按职责归组。[资产结构与升级入口](../../mai-specs/975-lesson-21-context-runtime-promptops/design/ASSET_MODEL_UPDATE.md)

线上修改先进入草稿；Markdown 可试填模板，JSON 使用源码与代码预览，工具使用专用表单。发布前查看受影响资产和版本，示例参数由服务端按代码 Schema 校验。发布后，新任务、分析预填和模型示例使用发布版本。整库快照可预览并恢复，已受理任务保留原先绑定的版本。

PromptOps 使用左右布局，目录铺满可用高度并独立滚动，默认展开全部层级。悬停或键盘聚焦目录行时显示新建加号，触屏直接显示；新建表单固定所选相对目录，只填写文件名与 Markdown 正文。右侧工具栏和文件操作各自横向排列。Markdown 与 JSON 预览随内容展开，正文最大高度为 800 px，超过后内部滚动。

Web 通过 Agent HTTP 契约访问提示词库。分析页只读取当前租户和工作区有权使用的预填，模型测试页按平台测试权限读取预设。预填服务暂不可用时，仍可在原页面手工输入问题。

隔离的 PostgreSQL 测试库完成迁移后，使用 `COMPASS_PROMPTOPS_E2E=true` 运行 `tests/e2e/promptops.spec.ts`。该用例会在 Agent 测试库发布并恢复快照。四类交付的浏览器用例位于 `tests/e2e/context-delivery.spec.ts`，用 `COMPASS_CONTEXT_E2E=true` 启用。它使用已发布提示词、保留型 Redis、已授权零售测试连接与运行中的 Agent 消费者。测试模型宿主 `apps/agent/tests/context_e2e_app.py` 将模型请求转送至 `COMPASS_E2E_MODEL_PORT` 指定的本地端口，`apps/agent/tests/context_runtime_provider.py` 在该端口提供受控响应。两个套件共用上述 Web 登录夹具与 `WEB_TEST_EXTERNAL_SERVER=true` 配置。两个验收开关同时开启时，还会在意图识别进行中发布一份系统提示词，实测新 Run 生效用时、在途 Run 的旧版本以及历史会话和恢复后的回答。

## 第 22 课指标口径

在「新建资产」右侧点击「资产授权」。租户管理员可在左栏多选部门或成员，在右栏搜索并勾选多个已发布指标和实体，一次授予或撤销使用权。全选作用于当前搜索结果。成员可同时从本人和所在部门获得授权，撤销其中一项后仍可使用其他有效授权。

工作区的数据分析默认带入已发布共享术语的名称、别名与释义。新建的个人维护术语默认发布到当前工作区，历史个人私有术语仍按原范围读取。实体和指标按问题从当前可用资产中选取，查询数据时另行校验执行绑定与连接权限。

一次查询可产生多份来源回执。仅当模型提炼出值得复用且目录尚未覆盖的指标时，回答下方才展示保存建议，每轮最多三项。勾选指标后可统一保存到个人草稿，口径说明、计算公式和复用场景在勾选前可见。

「新建分析」的第 22 课分组提供四道题，分别用支付失败明细和部门销售额做混合分析、把一次临时查询保存为个人指标、在对话中预览并修订同一指标，以及在部门口径冲突时等待本人选择。[课堂步骤与同题对照](../../examples/showcases/lesson-22-semantic-governance/README.md)

进入工作区的「数据与语义 → 指标口径」按类型、业务域、维护归属和状态查找实体、术语或指标。点击任一行从右侧抽屉预览定义。术语指向和实体关系在同一抽屉内打开，可返回上一项。维护者切换到编辑视图修改定义、字段及关系，保存后按使用范围立即生效。删除后从目录和查询中移除，历史版本保留。执行绑定独立核验。Agent 沿用提问人指定的指标或算法。多种口径会改变结果时，提问人可以选择候选，或选择“都不合适”填写算法，在原轮次继续分析。无发布绑定的指标仍可解释定义，并按明确的算法查询获准明细。管理页分别显示计算公式、术语指向、实体字段和连接键。

安装原版教学包后，运行 `pnpm db:seed -F web -- --fixture retail-semantics-v2` 发布第 2 版语义定义。升级保留原版本和组织权限，遇到人工修订或正在灰度的目标会中止事务。接着运行 `pnpm db:seed -F web -- --fixture retail-semantics-v3`，修订两家教学租户的销售额释义。两次发布均可幂等重跑，保留旧版本。正式取数仍需当前工作区的有效绑定和授权连接。

管理页面和 Chat 浏览器用例分别位于 [治理验收](tests/e2e/semantics-governance.spec.ts) 和 [对话验收](tests/e2e/semantics-chat.spec.ts)。环境变量和可复跑的账号、数据准备步骤见[第 22 课测试计划](../../mai-specs/974-lesson-22-enterprise-semantic-governance/test/TEST_PLAN.md)。

## 第 23 课资料与回答引用

华东区域工作区的「华东区 GMV 为什么下降了 12%？」使用 12 篇课堂原文和独立的只读经营账本。已发布的向量存入 Web 主库 `web.document_vectors`。查询在权限和业务日期范围内按余弦距离取前十，与词法排名融合后返回三个章节。资料导入、索引重建和首页用法见[课堂案例](../../examples/showcases/lesson-23-east-gmv-rag/README.md)。

新运行默认可调用文档检索工具。首次检索固定该 Run 的资料版本；工作区没有知识库或没有已索引文档时返回明确空状态。在 Web 服务进程设置 `COMPASS_DOCUMENTS_ROOT` 和 `COMPASS_DOCUMENTS_PIN_DIR` 的绝对路径。前者为只读发布目录，含 `collections/<sha256>.json` 原件包及 `bindings/<tenantId>/<workspaceId>.json` 当前指针与访问政策。后者为 Web 独占的持久可写目录。多 Web 实例须共享两目录并支持同目录文件硬链接。Compose 已配置只读发布目录和持久固定记录卷，宿主路径与备份方式见[部署说明](../../deploy/README.md#工作区资料检索)。

使用资料包时先发布资料并配置受众工作区。分析成员只可搜索发布时与当前政策均准许的段落。受限受众按活动工作区的显式成员关系管理，商业报价可独立撤权。切换包时保留旧摘要文件和 Run 固定记录，恢复和回看仍按当前发布政策重新验证。新运行的权限由文档工具和 Web 所有者检查，撤回单份材料可移除 `policies` 中对应文号或将其 `enabled` 改为 false。

Chat 中的数字角标可用键盘或鼠标打开。详情展示结论、适用范围、来源与复算过程。回看、重连与附件下载均按当前身份复核材料权限。运行课堂用例见[跨源检索课堂入口](../../examples/showcases/lesson-23-agentic-search/README.md)。

## 知识文档

「检索测试」按钮打开右侧抽屉，支持预置关键词、选择知识库和自定义查询。复用 OpenAI embedding 服务，对已启用片段做 pgvector 余弦检索，展示前十个结果的文件、章节、正文、片段 ID、相关度及耗时。

平台管理员打开知识文档，默认可查看所有租户与工作区的知识库，以及每个文件的原文和分段。库卡片在悬停提示中显示归属，资料修改按所属工作区权限办理。普通成员的知识库列表与分析中的检索范围均遵循当前工作区授权。

在当前工作区进入「管理与治理 → 数据与语义 → 知识文档」。具备数据与语义模块权限的活跃租户成员可维护资料。支持 Markdown、TXT 和文本型 PDF，单文件最多 5 MB。点击分段查看全文，修改后保存会更新向量，开关决定该段是否进入检索。解析或向量化失败可在文件列表重试。Agent 服务提供受信的 OpenAI 向量生成接口，Web 自有数据库保存原文件、分段和 pgvector 向量。

已有华东课堂资料的环境先执行 Web 迁移，然后从仓库根运行 `uv run --env-file .env python examples/showcases/lesson-23-east-gmv-rag/import_library.py`。安装顺序及检索行为见[本课方案](../../mai-specs/973-lesson-23-agentic-search/design/VECTOR_SEARCH.md)。

## 第 25 课长任务目标

分析页在用户确认目标草案后展示目标面板，包含验收项、约束、预算用量与进度计数。可暂停、恢复、修改或取消。修改草案会拒绝当前提案并将修改要求带回输入框。自动续轮期间发送消息会先停止当前轮，终态后再提交插话；用户主动暂停的目标保持暂停。

子任务在主流程显示为带 Bot 图标的独立节点，点击打开右侧抽屉。抽屉复用主 Agent 的执行时间线与 Markdown 回答组件，读取获授权子树和完整交付回答，打开期间轮询更新并复核权限。短暂失败可重试，失权时清除过程与回答；关闭取消请求，Esc 与关闭按钮返回入口。桌面使用侧边抽屉，窄屏使用全宽抽屉。组件示例位于 `/guide#ui-execution`。

`tests/e2e/long-task-goal.spec.ts` 使用独立 Agent/Web PostgreSQL、Redis 和模型上游，设置 `COMPASS_GOAL_E2E=true` 执行。`apps/agent/tests/goal_e2e_app.py` 仅把测试凭据 `isolated-fixture` 转到本地受控上游，其余已配置 Provider 使用真实服务；追加 `COMPASS_GOAL_REAL_E2E=true` 执行真实模型的普通查询、目标确认与完成。`apps/agent/tests/goal_prompt_probe.py` 运行七项真实模型决策评测，报告写到 `work/983-real-prompt-report.json`。环境与验收范围见 [Feature 983 测试计划](../../mai-specs/983-lesson-25-long-task-context/test/TEST_PLAN.md)。
