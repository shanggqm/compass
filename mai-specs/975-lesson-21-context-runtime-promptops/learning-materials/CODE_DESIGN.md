# 代码附录 · 提示词资产迁移与跨入口验收

[Context Runtime 技术方案](TECH_DESIGN.md) · [PromptOps 技术方案](PROMPTOPS_TECH_DESIGN.md) · Feature 简报

本附录补充两个主设计共用的清单和代码评审切面。源码定位按当前 `d5d36e5`，课程基线 `compass/L20-complete^{commit}` 为 `b547e91`。下文的资产路径、读取操作与新增测试为设计稿。数据库写入、运行绑定和数据授权分别以两份主设计为准。

## 1. 一个文件树，三类数据来源

Agent wheel 的拟新增目录 `apps/agent/src/compass_agent/prompts/` 是随包初始内容源，导入时以此目录的相对路径建立数据库文件。已发布正文由 Agent 按 generation 精确读取。原型里的教学文件用于核对相对路径和浏览效果，生产首发以真实调用点盘点和 wheel 中的完整文件树为准。四类任务的方法、指标语义召回与产物边界见[场景扩展规格](PRD.md)。

| 内容                                           | 维护者                                                      | 调用时的约束                                                                                                 |
| ---------------------------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Compass 固定指令、模型可读的说明、固定预填文本 | 随包 Markdown 为初版，平台管理员可在 PromptOps 发布后续版本 | 代码选择模板、声明变量和允许的用途。预填只能作为该角色的输入。                                               |
| 可执行合同与权限                               | Agent 的 DTO、路由目录、注册和执行逻辑                      | 工具名、参数类型、枚举、scope、真实授权、预算和校验由代码决定。提示词改版不能增加工具权限或更改响应 Schema。 |
| 用户、业务与第三方内容                         | 用户输入、数据连接、会话、个人 MCP 或个人工作流             | 保持原来源、角色、隔离与权限。每次任务使用的数据、已填写的模板、密钥及历史消息不进入公共文件树。             |

### 1.1 生产调用点与迁移归属

下表 Python 路径相对 `apps/agent/src/compass_agent/`，Web 路径相对 `apps/web/src/`。路径族中的不同文件须在实施时逐个对应实际使用方。`support/` 放辅助调用和输入模板，`shared/` 放跨任务片段，`tasks/` 放具体任务与场景使用的内容。

| 现有入口 / 符号                                                                                     | 拟归集的内容                                                                                                                                                                                                                           | 迁移时需保留的行为                                                                                                                                                                                                              |
| --------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `harness/prompts.py`、`harness/context.py::AgentDefinition`、`runtime/context.py::initial_messages` | `shared/fragments/base.md`、数据库使用指南、计划必需／工作流指导                                                                                                                                                                       | 当前历史仍为 user/assistant 序列。计划约束与工作流启用条件由代码判断，轮次限制由真实运行配置传入。                                                                                                                              |
| `runtime/service.py` 构造 `RunRoute` 后追加的定时任务指导                                           | `shared/tools/schedule-guidance.md`、`schedule-unavailable.md`                                                                                                                                                                         | 支持工具和需要转到 Web 的模型分别适用。代码继续检查工具能力、查询目标和变更确认。                                                                                                                                               |
| `routing/intent.py::decision_input`、`flash_input`、`routing/dictionary.py::descriptions`           | `support/routing/` 下分开的 JEV 问题说明与 Flash 分类模板。模型所读的固定标签释义归入受管文字                                                                                                                                          | `routing/scenes.json` 的场景 ID、类别归属、标签集和响应模式由代码持有。模板插入已校验的标签目录，候选释义须和 ID 一一对应。保留 Choice 与 JSON 两种不同的协议、置信度／回退规则。                                               |
| `routing/prompts.py::MODE_PROMPTS`、`OFF_TOPIC_PROMPT`，`routing/service.py::select_route`、`run`   | `support/routing/{business,clarify,restricted,handoff,off-topic}.md`                                                                                                                                                                   | 业务 Run 和直接路由生成均接入；澄清、受限、转交、离题沿原模式完成。不能将宽泛或受限意图强行分配给一个业务执行模板。                                                                                                             |
| `runtime/subagent.py::_process`、`_model`，`harness/subagent_process.py::run`                       | `support/subagent/analysis.md` 与该次子代理实际允许的工具说明                                                                                                                                                                          | 父进程固定 Run 版本、读取并渲染子任务指导，放入既有有界启动帧；子任务题面仍是 user 消息。子进程不持有 Agent 库凭据；模型与工具派发继续回到父进程。                                                                              |
| `runtime/session_management.py::claim_title`、`generate_title`                                      | `support/session/title.md`                                                                                                                                                                                                             | 标题 job 已联查本会话第一笔 Run 的 `config_snapshot` 与首条用户消息。`TitleJob` 携带该 Run 的 generation；租约重领读取同版。沿用重命名竞争检查、标题长度和失败回退。                                                            |
| `llm/litellm_chat.py::LiteLLMChatAdapter._invoke`                                                   | `shared/fragments/output-json.md`、`output-schema.md` 的固定自然语言提示                                                                                                                                                               | JSON Schema 来自当前请求的结构化合同，最终字节计入模型输入预算。生成调用、分类及模型测试均覆盖。工具结果验参和完成后的 JSON 校验仍在代码。                                                                                      |
| `tools/catalog.py` 登记的七种 `tools/builtin/<name>/` 及 `tools/catalog_contracts.py`               | `database_query`、`python_analysis`、`todo_write`、`sub_agent`、`create_workflow`、`schedule_read`、`schedule_write` 的模型可读说明。`create_workflow` 的参数帮助文字也作为受管内容；工具 JSON Schema 的类型、字段和必填项保持代码真源 | 区分函数描述、参数帮助、系统层工具指南，分别渲染到原位置。`factory`、`context_factory` 和普通 `handler` 三条注册路径共享同一份最终声明，保留原 handler、授权、审批与 schema。                                                   |
| `workflows/catalog.py::registration`、`tools/mcp/service.py` 生成动态声明                           | `shared/tools/workflow-label.md` 等固定包装文字（有语义的提示词部分）                                                                                                                                                                  | 个人工作流描述/名称/版本与 MCP 远端 description/input_schema 从本次授权绑定取。外部文本只留在对应工具声明；固定分隔符和格式序列化由代码维护。在受控 Run 中保存实际使用的声明与哈希。                                            |
| Web `components/analysis-composer.tsx` 的 `workflowSuggestions`、`diagnoseRetail`、`promptCards`    | 每份可点击预填正文一个 `support/starters/analysis/` 资产，共用正文的卡片可指向同一路径                                                                                                                                                 | 现有通用卡片、课程卡片、含 SQL 的用户侧示例均可预填和编辑。前端的身份、权限、`RunConstraints` 与任务限制按固定卡片 ID 绑定。示例中的指定工具顺序和日期只用于预填。                                                              |
| Web `features/models/decision-examples.ts`、`playground.tsx`                                        | `support/starters/models/` 下的示例 state、问题 instructions、固定题目的自然语言 criteria。结构化 state 和 criteria 按受约束格式渲染并校验                                                                                             | 同时迁移新增决策题的默认 criteria 和生成模型测试的初始工具说明。保留四组可编辑示例及 multi-question 各题，Choice/Noul/Score 类型、题目 ID、示例工具的机器参数和协议形状由代码提供。用户修改后走既有模型测试授权，原编辑值保留。 |
| `reliability/service.py::run_showcase`                                                              | `support/diagnostics/reliability-request.md` 的固定模拟请求                                                                                                                                                                            | 这条入口用 `ScriptedProvider` 和模拟 HTTP；模拟结果、故障脚本与判断条件仍属测试逻辑。独立诊断可使用同一随包文件测试本地适配器，在线产品入口按生效版本取固定题面。                                                               |
| 本期新增四类任务和业务 Skill                                                                        | `tasks/{business_qa,data_analysis,dashboard,business_report}/system.md`、各自 `scenes/`，`shared/stages/`、`shared/skills/`                                                                                                            | 依当前任务、阶段、工作空间和授权选择。现有 `schemas.py` 有执行明细的 Skill 类型，正式业务 Skill 的选择、工具依赖及权限检查需在本次实现。开发用 `.agents/skills/` 和 Showcase 文件维持各自的用途与发布边界。                     |

模型调用可直接携带用户自由输入。`runtime/store.py` 的历史、`scheduler` 保存的用户问题、个人工作流生成的子任务、Playground 手工编辑及用户上传的材料，均属于运行数据。HTTP 服务地址、演示脚本的模拟响应、界面标签、异常消息和运维日志另按现有约定维护。平台目录显示的是**产品提供的输入和指令**；界面里同时作为显示标签和预填正文的通用问题，标签可取已授权的预填正文。

### 1.2 完整性核对

1. 先给上表中的每个分支登记调用者、用途、逻辑路径、未发布时的行为、动态参数和验证样例。两种分类模式、结构化输出、定时与受控分支分别登记。`promptops/catalog.py` 和 Runtime 代码选择关系提供引用位置；新建但未引用的文件仍能导入，管理页显示“尚未接入”。
2. 通过 Python 和 TypeScript 的实际生产入口再做一次反向扫描。从消息构造、`DecisionQuestion.instructions/criteria`、`ToolFunction.description`、动态 system 拼接、生成请求适配及表单预填追查到源文件。未知字面量逐项分类，查询语句、外部工具回执和协议枚举只登记其角色。新增固定提示入口与对应使用关系随同一变更交付。
3. 从**构建好的 wheel**枚举 `prompts/**/*.md` 并与源码树的相对路径、正文摘要核对。初次导入及整体发布后，遍历所有已登记的业务引用和条件分支，逐路径精确读取并与随包资产对比。额外检查同 basename 不同目录、新增未接入文件与前端预填白名单。功能入口或资产变化时更新覆盖表。AC-12 按实际工作树、分发包、数据库与调用点四方逐项核对。

## 2. 关键调用面示意

本节是拟实现的 Python 风格代码，展示分支与数据去向。加密、连接管理、DTO 完整字段及 HTTP 错误转换按主文和现有模块执行。`PromptReader` 的接口归 PromptOps 主文，四类场景与 stage 的完整组合归 Runtime 主文。

### 2.1 预填读取与最终提交

`GET /v1/prompt-starters`（Agent）由 Web BFF 的分析入口 `/api/web/v1/analysis/prompt-starters` 及平台模型测试入口 `/api/web/v1/platform/models/prompt-starters` 调用。`promptops/catalog.py` 将只读 UI ID 映射为许可的 starter 文件组，允许一张决策示例卡消费多份文本。浏览器只得到其有权看到的卡片正文、结构化题目和展示信息。服务端不接受客户端传入路径、generation 或任意 scope。下面的 `purpose`、`scope` 来自 Web 认证及服务级凭据验证后的可信上下文。

```python
def starter_cards(tx, trusted, reader, catalog):
    if trusted.purpose == "chat":
        require_current_analysis_and_model_access(trusted)
        cards = catalog.analysis_cards(trusted.scope)
    elif trusted.purpose == "test":
        require_platform_model_test_access(trusted)
        cards = catalog.model_test_cards()
    else:
        raise ForbiddenPurpose()

    generation = reader.pin_current(tx)
    paths = tuple(dict.fromkeys(path for card in cards for path in card.paths))
    files = reader.load_exact(tx, generation, paths)
    by_path = {item.path: item for item in files}
    return [card.project_validated(by_path) for card in cards]
```

`project_validated` 只按该卡片的已登记用途取已发布正文。Chat 返回可编辑 question；模型测试以静态题目类型和选择项标识校验 `state`、`criteria` 等数据再返回。两端缓存沿用 `no-store`，预填读取失败时显示重试，仍可手动输入。输入框已有修改时刷新卡片列表不会覆盖草稿。点击发送只提交用户最终看到的文字，受理时再固定此次 Run 的 generation。后台发布不能在用户按发送时替换对话中的文字。`RunConstraints` 在卡片展示和实际提交两端继续按原平台契约处理。

### 2.2 工具说明随版本，执行合同随注册项

现有 `ToolSpec.register()` 有使用工厂直接返回 `RegisteredTool` 的分支，单改 `definitions()` 会让模型声明与 Registry 快照出现两套版本。在拟新增适配层中先获取真实可用注册，再生成一份私有、该 Run 专用的声明，供 Registry、模型及 Journal 共同引用。以下示意只展开工具自然语言描述替换；参数帮助文字依显式字段路径更新 `description` 叶子，覆盖后复核其余 JSON Schema 相等。

```python
def bind_builtin(spec, entry, selection, texts):
    if entry.name not in selection.allowed_tool_names:
        return None
    if entry.definition is None:
        return entry  # 保留已注册的宿主内部能力，仅有声明的工具会暴露给模型
    if spec.name != entry.name:
        raise CatalogMismatch()
    description_path = selection.description_path_for(spec.name)
    description = texts[description_path].body
    wire = entry.definition.model_copy(deep=True)
    wire.function.description = description
    return replace(entry, definition=wire)
```

`entry` 来自实际启用条件、可用后端和授权检查。模型工具清单从此组绑定导出。分发与恢复继续复核当前授权；新组装必须剔除依赖已失效工具的 Skill。动态工具保持来源隔离、声明字节限制和已绑定的配置版本。运行中撤权先阻断受影响的访问，再决定可以重建哪些材料。管理端变更文字不能使原本不可调用的工具进入执行路径。

### 2.3 子进程、续接和标题

- **子 Agent。** `runtime/subagent.py::_process` 在新进程启动前，用父 Run 的 generation 和子代理可用清单读取 `support/subagent/analysis.md` 及适用指南。启动帧增加一个有界 `system_text`，dynamic `prompt` 独立放在 user 消息。父进程校验子进程返回的 tool names、scope 和请求上限，并沿 `_model` 原路径进入父进程受控派发。帧序列化按 UTF-8 实际长度检查，触及既有上限时先收窄可选材料，必需内容仍超限则终止该子任务并报告原因。
- **结构化生成。** 对非 text 请求，受管输出格式文字与请求的结构化 schema 由同一编译结果携带到预算检查和 adapter，适配器只插入一次。现有 `llm/continuation.py::restore` 按原可见消息前缀校验摘要、工具调用 ID 和私有位置。Provider 协议前缀在还原后添加，预算预检和实际派发共用相同投影。调整指令、压缩或移除旧历史只能在一组工具续接完整结束后进行。涉及输出格式及前缀的固定内容和变量摘要应计入准备记录，接管按同一份记录恢复。
- **标题。** 新 Run 的首条消息触发标题生成时，`claim_title` 在既有联查中读取 generation 并随 `TitleJob` 传递。重试使用该 generation 的标题文件；用户已手动重命名则以既有状态竞争结果为准。历史旧 Run 的兼容读取沿 Runtime 主设计，当前生效文件缺失时不得替换旧版正文。后台失败继续使用现有标题重试／回退流程，不改写会话中的业务答案。

## 3. 迁移验证与旧版评审的有效项

以下均为实施阶段须执行的验收，当前交付只提供设计。先用一个可授权的真实业务问题串起受理、取数、组装、模型、派生产物和再打开，再扩至四种交付意图以及管理端辅助入口。

| 关联要求                       | 执行安排                                                                                                                                                                                                          | 判定                                                                                                                                                                                                    |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| AC-02、12、16 · 清单与范围     | 导入实际 wheel 全树，逐项覆盖主 Run、JEV/Flash、旧路由、标题、子任务、工具说明、两种前端预填及未接入文件；在两个租户及同租户两个部门重复。                                                                        | 每个受管调用读到已登记路径及固定版本，管理页能说明使用方。非管理员的业务读取只得到本用途卡片，管理文件任意访问被拒绝。                                                                                  |
| AC-04、06、07、15 · 发布与切换 | 在旧 Run 有工具待回执期间，单发报告文件，再创建新的任务、恢复先前 Run、重领标题及启动子任务。                                                                                                                     | 旧 Run 及子任务读取受理组合，新任务读取已生效文件，重做同一发布或受理没有重复副作用。旧卡片里用户已编辑的文字完整保留。                                                                                 |
| AC-02、07、11 · 单一模型派发   | 在 Journal 准备／已准备未派发／已接收结果三处注入故障，覆盖工具续接与 JSON 格式的完整调用。使用 Provider stub 记录实际调用次数及预算；复查 `test_durable_runtime_faults.py`、`test_sdk_protocols.py` 的覆盖缺口。 | `AgentExecutor._invoke_model` 与 `ExecutionJournal.call` 只准备一次；已接收输入／结果可从原 attempt 还原。字面内容不进入新增 OTel 属性；判定实际的 generation、路径版本与压缩原因。                     |
| AC-03、08、13 · 源数据撤权     | 在会话仍有权限时撤销来源连接，分别访问消息列表、追问、恢复未派发 step、SSE 重放、产物打开及下载。包含旧历史没有来源信息的用例。                                                                                   | 已失效来源及依赖 Skill 不参加新组装。已撤权派生内容被过滤或在当前权限下重新取数；受限信息不会从完成事件、元数据或下载流旁路泄露。                                                                       |
| AC-01、09、10 · 真实浏览器交付 | 覆盖 Web 业务预填、模板发布、数据分析导出、看板重开、报告下载。核对 Agent 内容类型与尺寸、生成契约、BFF 代理、浏览器文件名及编码。                                                                                | PNG/CSV 原有路径继续工作；现有 BFF 只接受 PNG/CSV 并限 512 KiB，按 Runtime 主设计扩展 MIME 及明确的端到端上限。摘要、展示与下载保持同一结果，故障可重试。完整回归按四类 80 条组装和 20 条行为用例判定。 |

旧评审要求的 Journal 单入口、源数据撤权、产物 MIME/大小一致性、即时与 Scheduler 共同受理均在上表继续核验。该评审基于较早的场景数、灰度和审批设计，当前发布行为以[PromptOps PRD](PRD.md)为准。原设计评审
