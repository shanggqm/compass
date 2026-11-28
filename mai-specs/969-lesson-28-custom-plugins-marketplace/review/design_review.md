# 设计评审报告：969 第 28 课｜自定义插件与插件市场

> 评审对象：[BRIEF.md](../BRIEF.md)、[PRD.md](../spec/PRD.md)、[TECH_DESIGN.md](../design/TECH_DESIGN.md)、[CODE_DESIGN.md](../design/CODE_DESIGN.md)、[plugins-prototype.html](../spec/plugins-prototype.html)
> 核对代码基线：`main @ 5180841`（含 2026-11-27 第 27 课 `assembler.py` / `ToolGovernance` / `ActionableToolError` / `stream_answer_turn.py` 工作区实现，未读取其他 Feature 目录）
> 评审日期：2026-11-27（含第 1 轮减法初评与第 2 轮目标对齐复评）
> 最新评审结论（第 2 轮）：**需要修改**（第 1 轮 4 项 P0 / 4 项 P1 均已解决；对照最新目标——“自定义工具默认经 `tool_search` 动态发现、自用接入过自动化机审质检即个人可用、上架共享走机审+管理员审核、补齐插件规范治理与未来资产包锚点”及架构选型图复核后，发现 **2 项 P0 偏离/缺口、3 项 P1 重要遗漏与 2 项 P2 建议**）

[返回需求简报](../BRIEF.md) · [产品需求 PRD](../spec/PRD.md) · [技术方案](../design/TECH_DESIGN.md) · [高保真原型](../spec/plugins-prototype.html)

---

## 第 2 轮评审（2026-11-27｜对照架构选型图、自用机审闭环与默认 Tool Search 复核）

### 1. 复评结论与上轮问题闭环核验

- **第 1 轮发现核销**：第 1 轮识别的 P0-1（去除 OpenAPI/HTTP 平行栈）、P0-2（补齐 L27 自动发现与 `ActionableToolError` 对齐）、P0-3（裁剪五步重型准入为两步确定性检查）、P0-4（修订 PRD 口径）及 P1-1 ～ P1-4 均已在 2026-11-27 修订后的 `PRD.md`、`TECH_DESIGN.md`、`CODE_DESIGN.md` 与 `plugins-prototype.html` 中闭环解决。
- **本轮总体结论**：**需要修改**。当前方案的轻量 MCP 底座、4 态市场上架骨架和本地 BM25/`select:` Tool Search 核心引擎方向扎实，但对照负责人最新明确的四项核心诉求与架构图逐项核验后，存在 **2 项 P0 级目标偏离/闭环缺口** 与 **3 项 P1 级规范及真实代码落地遗漏**，建议先做一次精准增补修订再进入编码。

### 2. 本轮关键发现（按优先级排序）

#### R2-P0-1（阻断）：自定义工具默认仍按 `auto ≤ 6` 直接 Eager 组装，偏离“自定义接入默认走 Tool Search 动态发现”

- **证据**：
  - 方案位置：`PRD.md` FR-02 / FR-07、`TECH_DESIGN.md` §2.2 / §2.3.5、`CODE_DESIGN.md` 模块 5（`partition_mcp_tools`）、`plugins-prototype.html` 接入抽屉默认项；架构图面板 05 同样保留了 `auto：≤ 6 工具且 ≤ 16 KiB 直接加载` 旁路。
  - 当前行为：新建 `mcp_connection.loading_mode` 默认为 `'auto'` 且全局 `COMPASS_TOOL_SEARCH_EAGER_LIMIT = 6`。当用户自定义接入一个含 2–6 个工具的公司内部 MCP（例如第 28 课基础零售演示 MCP 仅含 `get_promotion_schedule` 与 `get_competitor_prices`）时，命中 `2 <= 6` 会直接 Eager 组装进首轮模型工具列表，完全跳过 `tool_search`。
- **触发条件与影响**：既偏离负责人“自定义工具接入完成后默认通过 `tool_search` 动态发现、不默认直接组装”的明确要求，也导致主演示路径在接入单个公司内部 MCP 时无法直观验证 Tool Search 闭环。
- **可执行动作**：
  1. 将 `mcp_connection.loading_mode` 的数据库默认值、API 默认值与前端抽屉默认选项改为 **`'deferred'`**（延迟按需发现）；所有自定义接入（及默认市场接入）的 MCP 工具统一先进入 `deferred_pool`，由模型通过 `tool_search` 动态发现并尾部激活；
  2. 保留 `'always_load'` 作为用户显式可选的“高频常驻直出”逃生开关（受扩展槽位预算保护），并明确区分 `deferred`（默认）与 `always_load`（显式常驻），可精简或降级 `auto` 阈值分支以降低理解与测试成本；
  3. 同步更新 `PRD.md`、`TECH_DESIGN.md`、`CODE_DESIGN.md`、架构图面板 05 以及 `plugins-prototype.html` 的默认策略与负载条计算逻辑。

#### R2-P0-2（阻断）：个人“接入自用”缺少最小化自动化机审质检门禁与状态落库，治理存在双标漏洞

- **证据**：
  - 方案位置：`TECH_DESIGN.md` §2.3.2、`CODE_DESIGN.md` 模块 3 仅将 `run_lightweight_admission_check()` 绑定在市场提交链路（`/v1/plugins/catalog/{id}/check` → `plugin_catalog_item.last_check_json`）；
  - 个人接入链路（`POST/PATCH /v1/plugins/connections` → `mcp_connection`）目前仅在前端提供可选的「测试连通并拉取工具」（`probe`），服务端保存与开启 `enabled=True` 时未强制校验机审是否通过，也未在 `mcp_connection` 中持久化质检状态与检查报告。
- **触发条件与影响**：用户接入自定义 MCP 自用时可以跳过质检直接保存，遇到超长描述、非法 Schema、不可达地址或修改 Endpoint 替换目标服务时缺乏自动化拦截与可追溯报告，未形成负责人要求的“**自用接入过最小化自动化机审质检 → 通过即进入个人可用状态；市场共享走同一机审 + 管理员人工审批**”双轨治理闭环。
- **可执行动作**：
  1. 抽出统一最小化自动化机审引擎 `run_plugin_quality_gate()`，同时服务自用接入与市场上架两类入口，固化 5 项确定性机审规则：① URL/协议与禁云元数据/禁 3xx 安全底线；② Streamable HTTP 真实握手 `initialize` + `tools/list`（8 秒超时）；③ 工具 JSON Schema 合法性与单 Schema ≤ 32 KiB；④ 工具描述长度 ≤ 2,000 字符、单连接工具数上限（如 ≤ 50）与基本元信息规范；⑤ 与内建工具精确重名提示及极简高危提示注入模式静态规则（预留 `AdmissionHook` 给第 31 课深度安全规则）；
  2. 在 `mcp_connection` 增补最小 2 列：`check_status VARCHAR(16) NOT NULL DEFAULT 'unverified'`（`unverified` / `passed` / `failed`，历史存量迁移回填 `'passed'` 保兼容）与 `last_check_json JSONB NULL`；
  3. 强制门禁：自用接入首次保存或修改 `endpoint` / `auth_type` 时自动运行机审质检；无 `error` 级硬错则置 `check_status='passed'` 并立即进入个人可用状态（允许 `enabled=True`），存在 `error` 硬错则置 `'failed'`、禁止启用并返回逐项可读报告。

#### R2-P1-1（重要）：缺少显式的“插件规范治理契约”与面向未来资产包（Skill / 资产清单）的最小演进锚点

- **证据**：
  - 架构图面板 06 已显式标注：`当前采用表单元信息 + 远端工具发现；ZIP / manifest 文件上传规范：尚未定义`；
  - `TECH_DESIGN.md` §2.1.1 中 `plugin_catalog_item` 未包含 `asset_type` 字段，命名规范、工具契约边界、安全约束分散在 SQL 约束与散文段落中，缺少面向第 28 课教学与后续第 29–30 课扩展的统一规范治理章节。
- **触发条件与影响**：若第 28 课只落地“表单填 URL”，后续引入 Skill / Prompt / Workflow 资产包时，市场表结构、状态机与审核列表需破坏性重构；同时学习者难以从文档中一目了然掌握企业级插件市场的最小治理规范清单。
- **可执行动作（坚持 MVP，不做过度实现）**：
  1. 在 `PRD.md` 与 `TECH_DESIGN.md` 新增独立小节《插件规范治理契约与资产包演进边界》，把命名与身份规范、工具契约与命名空间隔离、统一机审/审批双轨流程、以及未来资产包（`manifest` + ZIP 上传规范）的接入边界集中成一张清晰的规范总表；
  2. 在 `plugin_catalog_item` 增加 1 个低成本演进锚点列 `asset_type VARCHAR(24) NOT NULL DEFAULT 'mcp_remote'`（CHECK 一期仅允许 `'mcp_remote'`），本期严禁实现 ZIP 上传解压、多文件存储或 Skill 装载逻辑。

#### R2-P1-2（重要）：核对真实代码发现 Tool Search 接入 `tool_assembly.py` 与 `stream_answer_turn.py` 存在 3 处具体实现遗漏

- **证据（真实代码逐行核验）**：
  1. **延迟服务目录摘要缺少无漂移注入位**：`apps/agent/src/compass_agent/context_runtime/tool_assembly.py:82-88` 当前只更新 `draft.tools`，并未触碰 `draft.messages`。方案 §2.3.5 提到向系统上下文注入“轻量延迟目录摘要”，但未写清注入在 `draft.messages` 的哪一位置、如何避免每轮重装重复追加以及如何与 `continuation_token` 配合保证 Prompt Prefix Cache 字节一致；
  2. **纯闲聊直出判断在 `stream_answer_turn.py` 有独立入口**：`apps/agent/src/compass_agent/runtime/stream_answer_turn.py:47-52` 直接检查 `route.scene_id in DIRECT_STREAM_SCENES and not any(selection.values())`（直接读 `journal.tool_selection`），未调用 `assembler.should_direct_stream()`。若实现时误把默认加载的 MCP 写回 `journal.tool_selection`，会直接破坏 `SC-39` 闲聊直出；
  3. **`tool_search` 边界反馈缺失**：未定义激活工具触及 32 槽位 / 64 KiB 预算上限时（`budget_exceeded`）以及关键词检索 0 命中时（返回 `availableServers` 引导换词或 `select:`）的结构化返回语义，存在模型误判工具已激活或死循环检索风险。
- **可执行动作**：
  1. 在 `freeze_work_entry()`（`tools/v3/work-entry/{path}`）中基于初始 `deferred_universe` 一次性生成并冻结 `deferred_catalog_hint` 字符串，在 `tool_assembly.assemble()` 中以幂等方式附加到 system prompt 固定尾部，既不重复追加、也不随激活状态改写前缀；
  2. 在 `TECH_DESIGN.md` §2.3.4 明确约束：后台默认加载的 MCP 仅存 `journal.default_mcp_server_ids`，严禁回写 `journal.tool_selection`，并同步检查 `stream_answer_turn.py:47-52` 与 `assembler.should_direct_stream()` 双处口径；
  3. 在 `CODE_DESIGN.md` 模块 5 补齐 `tool_search` 的两类边界响应：`budget_exceeded`（单 Run 激活上限与剩余槽位提示）与 `no_match`（返回当前已授权延迟服务名清单与 `select:` 提示）。

#### R2-P1-3（重要）：缺少“个人已过机审 MCP 一键转提交市场上架”与“修改 Endpoint 强制重跑机审”闭环规则

- **证据**：`PRD.md` FR-02 / FR-04 与 `plugins-prototype.html` 中「我的插件」与「提交插件上架」表单完全独立；PATCH `/v1/plugins/connections/{id}` 未显式规定修改 `endpoint` 或 `auth_type` 时必须重跑机审。
- **触发条件与影响**：用户自用调通内部 MCP 后想共享上架需重复录入；更严重的是，若允许改 Endpoint 后沿用旧 `enabled=True` 而不重跑机审，会绕过自用自动化质检门禁。
- **可执行动作**：
  1. 规则写入 `BR` 与 `TECH_DESIGN.md`：`PATCH /connections/{id}` 只要变更 `endpoint` 或 `auth_type`，必须重新执行 `run_plugin_quality_gate()`，通过后方可保持启用；
  2. 在「我的插件」对 `source='custom'` 且 `check_status='passed'` 的连接增加轻量操作「提交上架」，自动带入名称、Endpoint 与认证方式打开提交抽屉。

#### R2-P2-1（建议）：补齐 `tool_search` 在 `UiExecutionTimeline` 的脱敏展示字段约定

- **依据**：`PRD.md` FR-08 要求在执行时间线清晰展示工具搜索命中记录，但设计文档未约定对外展示的字段白名单。
- **建议**：在 `TECH_DESIGN.md` §2.3.5 补充明确：`UiExecutionTimeline` 仅展示 `query`、`matchKind`（`bm25` / `exact_select`）、命中项的 `serverName` + `remoteName`（不直接裸露内部 `mcp_<hex>_<hash>` 与内网 IP/Token）及 `elapsedMs`。

#### R2-P2-2（建议）：远端 MCP 工具清单变更导致 `mcp_selection_stale` 时的可读提示

- **依据**：`apps/agent/src/compass_agent/tools/mcp/service.py:86-87` 在远端删除/改名已勾选工具时会抛出 `mcp_selection_stale`，在 `required=False` 下被静默计入 `skipped`。
- **建议**：在「我的插件」记录最近一次发现状态摘要（`lastSyncWarning`），遇到远端工具清单漂移时温和提示用户“远端工具清单已变更，请点击重新探测并复核机审”，避免黑盒静默消失（真正的防恶意篡改 Rug Pull 哈希锁仍留第 31 课）。

### 3. 第 2 轮修订工作量评估与决策边界

上述 2 项 P0 + 3 项 P1 + 2 项 P2 均为高杠杆、低代码膨胀的精准修补（仅在 `mcp_connection` 新增 `check_status` / `last_check_json` 2 列、`plugin_catalog_item` 新增 `asset_type` 1 列，复用同一套 `run_plugin_quality_gate()` 引擎，不新增任何表、不引入外部依赖、不触碰第 31 课深度治理范围）。待负责人确认后即可统一回写 `PRD.md`、`TECH_DESIGN.md`、`CODE_DESIGN.md`、`TECH_DESIGN.html` 与 `plugins-prototype.html`。

---

## 第 1 轮评审（历史归档｜2026-11-27 首轮减法审查，结论已落地核销）

### 1. 总体结论与核心依据

当前 Feature `969` 方案围绕原大纲设计了一套功能完整但高度复杂的“声明式 OpenAPI/HTTP 插件编译引擎 + 五步重型准入 + SemVer 多版本市场（5 张新表、20 个接口、9 个前端模块）”。对照用户本轮明确的一期产品诉求（**简单可靠、仅支持云端 Streamable HTTP MCP、暂不做 Skill 导入、提供市场内置工具与用户自定义接入、开启即默认装载进工具上下文、保留极简必要准入与上架机制**）以及 Compass 代码库现状，核查得出三点核心结论：

1. **最大过度设计在于重造了第二套外部工具引擎**：Compass 早在迁移 `0008` 就已在 `apps/agent/src/compass_agent/tools/mcp/` 完整交付了云端 Streamable HTTP MCP 底座（含 SSRF 固定 IP 拨号、域分离加密仓储、实时握手探测、HITL 审批与在途修订监听）。当前方案绕开该底座，另建整套 OpenAPI 裁剪、模板映射 DSL、响应投影与独立 HTTP 执行后端，投入高且引入双轨维护债。
2. **与 2026-11-27 刚落地的第 27 课真实代码存在硬契约偏差**：核对 `tools/assembler.py` 与 `runtime/service.py` 发现，第 27 课并未实现 `search_tools`（仅预留 `deferred_pool`），且在 `answerProtocol >= 3` 下要求显式传入 `selected_mcp_server_ids` 才会加载 MCP——直接导致“用户开启插件后无法默认装载到工具上下文”，且原方案自造的错误结构与第 27 课已实现的 `ActionableToolError` 重复。
3. **五步重型准入与 SemVer 多版本治理显著超出当前阶段需要**：在线 Embedding 撞车计算（强依赖单一 OpenAI Provider）、实时并发调用 LLM 跑 14 题防抢流回放、独立 5 张表及版本 Diff/灰度回退系统，在一期都会制造高脆弱性与高开发成本，应果断裁剪或延后。

---

## 2. 关键发现清单（按优先级排序）

### P0（阻断级 — 必须修正或删改后再实施）

#### P0-1：整套声明式 HTTP/OpenAPI 编译执行栈偏离一期单一云端 MCP 方向，且与既有 `tools/mcp/` 重复建设

- **证据**：
  - 方案位置：`PRD.md`（FR-03、FR-04、FR-13）、`TECH_DESIGN.md`（§2.3.1、§2.3.2、§2.3.5）、`CODE_DESIGN.md`（模块 1、模块 2、模块 5）。
  - 真实代码：`apps/agent/src/compass_agent/tools/mcp/client.py` 已实现 `EndpointPolicy`（域名/协议/防私网与云元数据校验）、`PinnedTransport`（固定 IP 防 DNS Rebinding、禁 3xx、1 MB 响应流上限）与 `McpSession`（Streamable HTTP 握手与分页 `list_tools`）；`tools/mcp/repository.py` 已实现 `McpRepository` 与 `SecretStore` 域分离加密；`tools/mcp/service.py` 已实现 `McpRun`、`McpBinding`、`revalidate`、`watch_revision` 与 `ToolApprovalProposal`；`api/mcp.py` 已有 `POST /v1/mcp/connections/{id}/test`；`apps/web/src/server/http/mcp-handler.ts` 已有 `/api/mcp/connections` BFF 代理。
- **触发条件与影响**：按原方案推进需从零实现 `openapi_importer.py`、`mapper.py`、`http_client.py`、`PluginHttpBinding`/`PluginHttpBackend` 并重构 `outbound_http.py`（占总任务量 45%+），交付后系统并存“声明式 HTTP 插件”和“MCP”两套外部工具栈，测试与安全维护面翻倍，直接偏离“一期只支持云端 Streamable HTTP MCP”的目标。
- **可执行动作**：
  - 删除 OpenAPI 导入、Request Mapping DSL、Response Shaping 投影器、自定义 HTTP 错误码映射、独立 `PluginHttpBinding` / `PluginHttpBackend` 及 `outbound_http.py` 抽离任务；
  - 一期插件市场统一以 **云端 Streamable HTTP MCP** 为唯一接入协议，直接复用 `tools/mcp/` 既有运行时栈。

#### P0-2：与 2026-11-27 第 27 课真实代码契约不一致，且现有 MCP 机制无法满足“开启即默认装载到工具上下文”

- **证据**：
  - `TECH_DESIGN.md` §1.2、§2.3.5 与 `CODE_DESIGN.md` 模块 5 假设第 27 课已提供 `search_tools` 工具和 `PluginToolMeta` 接口，并假设迁移起点待定。
  - 真实代码核验：
    1. `apps/agent/src/compass_agent/tools/assembler.py:71-73` 显示第 27 课仅预留 `deferred_pool`，尚未实现 `search_tools`；
    2. `apps/agent/src/compass_agent/runtime/service.py:414-427` 在 `answerProtocol >= 3` 时仅读取 `journal.tool_selection.get("mcpServerIds", [])`，为空时直接 `return None` 跳过 MCP 发现；`tools/assembler.py:108-115` 也要求 `mcp_id in ctx.selected_mcp_server_ids` 才暴露工具；
    3. `apps/agent/src/compass_agent/tools/errors.py:15-39` 已引入标准 `ActionableToolError`（与原方案 `mapper.py` 返回的 `recoveryHint` 字段不一致）；
    4. `apps/agent/migrations/versions/` 仍停在 `0020_context_ops.py`（第 27 课未新增 Agent 数据库迁移）。
- **触发条件与影响**：若不针对第 27 课真实装配逻辑修订，用户接入并开启 MCP 插件后，在新对话中不手动勾选就不会被发现和注入模型工具上下文，直接违反一期核心诉求；同时接口签名与错误结构失配将阻断编码。
- **可执行动作**：
  - 修改 `runtime/service.py:_discover_tools()` 与 `context_runtime/tool_assembly.py` / `tools/assembler.py`：当 `grants.plugins is True` 且用户存在 `enabled=True` 的 MCP 连接时，默认将其纳入当前 Run 发现集合并作为启用的 `user_extension` 自动装载入工具上下文（继续遵守 `USER_SELECTED_EXTENSION_LIMIT = 12` 及 32 槽位 / 64 KiB 预算护栏，不依赖 `search_tools`）；
  - 错误语义统一复用 `tools/errors.py` 的 `ActionableToolError`；
  - 锁定 `agent/main` 迁移起点为 `0020`（先与第 27 课提交最终确认无新迁移后落为 `0021`）。

#### P0-3：五步重型准入（在线 Embedding 撞车 + 实时 LLM 防抢流回放）脆弱、耗时高，超出当前阶段必要范围

- **证据**：
  - `PRD.md`（FR-06、FR-08）、`TECH_DESIGN.md`（§2.3.3 `collision.py`、`replay_gate.py`、`plugin_version.tool_vectors_json`）、`CODE_DESIGN.md`（模块 3、模块 4）。
  - 真实代码依赖：`apps/agent/src/compass_agent/documents/embeddings.py:25-33` 强依赖单一启用的 OpenAI Provider（`text-embedding-3-small`），在仅配置 DeepSeek / 阿里云模型的环境下不可用；`replay_gate.py` 在交互式上架链路中并发触发 14+ 次模型调用，存在 10–20 秒等待、Token 费用开销和模型输出波动导致的偶发误拒。
- **触发条件与影响**：上架流程强绑定外部 Embedding 与实时 LLM 稳定性，开发复杂度高且极易在真实使用或演示中出现偶发卡顿与假阴性拦截，违背“简单可靠、最简化必要准入”原则。
- **可执行动作**：将五步准入收敛为**三步确定性必要门禁**：
  1. **静态契约与安全门禁（确定性、毫秒级）**：校验 HTTPS / `EndpointPolicy` 防 SSRF、工具名与 JSON Schema 合法性（复用 `tools/registry.py` 的 `validate_schema` 与 2,000 字符描述上限）、提示注入禁词静态规则、与 19 个内建工具及市场已上架工具的精确重名/高度重复拦截；
  2. **实时 MCP 握手探测（复用 `McpSession`）**：执行真实 Streamable HTTP `initialize` + `tools/list`，确认远端可达、工具数量受控，并固化发现的工具列表与 `schema_hash`；
  3. **管理员审核上架 + 一键停用**：审核页直接展示目标 Endpoint、静态检查通过状态与握手拉取的真实工具列表及风险标签；管理员一键批准上架或紧急下架停用。
  - 原 Embedding 撞车与在线 LLM 防抢流回放延后至后续阶段（触发条件见第 3 节）。

#### P0-4：PRD、技术方案与原第 28 课大纲之间存在产品范围冲突，需确认后同步更新有效需求源

- **证据**：原 `context/lesson_outline.md`、`spec/PRD.md` 规定第 28 课交付 OpenAPI/HTTP 声明式插件与五步准入，并将 MCP 留到第 31 课；用户本轮明确一期插件市场聚焦云端 Streamable HTTP MCP、暂不支持 Skill 导入、精简准入与上架。
- **触发条件与影响**：若直接跳过需求修订进入开发，会导致 `PRD.md`（AC-01 ～ AC-12）与实际实现完全背离，后续代码评审与验收无有效基线。
- **可执行动作**：由负责人确认采用精简方向后，先通过 `my-create-prd` 修订 `969` 的 `spec/PRD.md`（保留有效安全与隔离 AC，删减 OpenAPI/Response Shaping/SemVer/五步回放 AC），再通过 `my-create-design` 重写精简版 `TECH_DESIGN.md`、`CODE_DESIGN.md` 与 `TECH_DESIGN.html`；同时确认是否需要同步调整课程大纲第 28 课与第 31 课的分工边界。

---

### P1（重要级 — 显著过度设计或复用前提缺口）

#### P1-1：5 张新表与完整 SemVer 多版本/Diff/灰度回退体系过度拆分，且与既有 `mcp_connection` 重叠

- **证据**：`TECH_DESIGN.md` §2.1.1 新增 `plugin_package`、`plugin_version`、`plugin_admission_report`、`plugin_workspace_install`、`plugin_audit_event` 5 张表及 `BF-03`、`FR-10`、`AC-09` 版本 Diff 对比与回退体系；同时 `apps/agent/src/compass_agent/tools/mcp/entities.py` 已有 `mcp_connection` 表管理用户 MCP 连接、加密 Token、启停状态与工具选择。
- **触发条件与影响**：一期仅需“市场预置/上架插件目录 + 用户一键接入或自定义接入 + 开启即用 + 下架停用”，维护独立多版本仓库与 Diff 对比会引入 3 张冗余表、10+ 个额外接口和大量状态同步边界。
- **可执行动作（最小数据模型建议）**：
  - **新增 1 张核心表 `plugin_catalog_item`（归属 `agent/main`）**：保存市场内置预置（`source='builtin_preset'`）与提交上架的云端 MCP 模板（`id`, `slug`, `name`, `summary`, `endpoint`, `auth_type`, `visibility`, `owner_tenant_id`, `status` 四态 `draft|pending_review|published|disabled`, `discovered_tools_json`, `last_check_json`, `review_comment`, `disabled_reason`, `revision`, `created_by`, `reviewed_by`, `updated_at`）。
  - **扩展既有 `mcp_connection`（最小 2 列）**：增加可空 `catalog_item_id UUID NULL`（逻辑/外键指向 `plugin_catalog_item.id`）与 `source VARCHAR(24)`（`catalog` / `custom`）。用户从市场接入或自定义接入均落入同一张 `mcp_connection` 表，完全复用既有加密、修订监听与 Run 加载链路；当管理员下架 `plugin_catalog_item`（`status='disabled'`）时，装配与 `revalidate` 联查 `catalog_item_id` 即可同步生效平台级停用。
  - 延后独立 `plugin_version` 多版本历史、独立 `plugin_admission_report` 历史流水表与独立 `plugin_audit_event` 表（一期关键审批准入摘要存 `last_check_json`，管理操作写入既有日志或待后续合规阶段再拆分）。

#### P1-2：Skill 预留字段（`skillRefs`/`promptPresets`）、6 态状态机与 5 套细粒度权限开关属于提前建设

- **证据**：`CODE_DESIGN.md` 模块 1 中 `PluginManifestV1.skill_refs` / `prompt_presets`、`lifecycle.py` 6 态状态机、`authorize.ts` 5 种 `PluginPurpose`、`settings.py` 中 `COMPASS_PLUGIN_STRICT_DUAL_REVIEW`。
- **触发条件与影响**：用户已明确一期不做 Skill 导入；空壳字段和无消费场景的 `testing` / `deprecated` 状态只会增加表单校验、UI 分支与测试矩阵。
- **可执行动作**：
  - 彻底移除 `skillRefs`、`promptPresets` 及双人审批开关；
  - 状态机收敛为 4 态：`draft` → `pending_review` → `published` → `disabled`；
  - Web BFF 权限用途收敛为 2～3 个：`plugin_use`（持有 `module.plugins`：浏览市场、接入/自定义配置个人 MCP、提交市场草稿）与 `plugin_review`（租户管理员审本租户条目、平台管理员审公共条目及执行下架）。

#### P1-3：Web 前端 4 Tab + 4 步超长向导与多抽屉设计过重

- **证据**：`PRD.md` §5（UI-01 ～ UI-03）、`TECH_DESIGN.md` §2.3.6（规划 9 个前端文件、OpenAPI 解析表、样例 JSON 投影预览、防抢流逐题对比表、版本 Diff 抽屉）。
- **触发条件与影响**：前端代码与 E2E 用例膨胀，操作路径冗长，偏离“找工具 → 一键接入/填地址接入 → 开启即用”的简洁体验。
- **可执行动作**：将「插件 → 全部插件」精简为 **3 个视图 + 2 个抽屉**：
  - 视图 1「插件市场」：展示内置预置与已上架云端 MCP 卡片，点击「接入」弹窗/抽屉填 Token（如需）并勾选工具后保存即开启；
  - 视图 2「我的插件」：展示已接入的市场插件与自定义 MCP 列表，提供一键启停开关、连通测试、编辑与「+ 自定义 MCP 接入」按钮；
  - 视图 3「审核上架」（管理员可见）：查看待审条目（端点、静态校验、握手工具清单）执行批准/驳回，或对已上架条目一键下架；普通开发者可在「我的插件」或市场入口点击「提交新插件上架」。

#### P1-4：复用既有 `tools/mcp/` 时存在 3 处必须显式处理的代码约束

- **证据**（均为真实代码核验）：
  1. **协议版本硬锁定**：`apps/agent/src/compass_agent/tools/mcp/client.py:181` 写死 `if initialized.protocol_version != "2025-11-25": raise ToolRejected("mcp_protocol_unsupported")`，会拒绝大量使用 `2025-03-26` / `2025-06-18` 的现网云端 Streamable HTTP MCP；
  2. **域名白名单与市场审核的协同缺口**：`tools/mcp/client.py:47` 要求所有 endpoint origin 必须在环境变量 `COMPASS_MCP_ALLOWED_ORIGINS` 中，且 `client.py:38` 禁止 URL query 参数；若市场支持管理员审核上架新云端 MCP，却仍要求改 `.env` 重启 Agent，会打破“零重启上架”目标；
  3. **连接数量上限与默认工具策略**：`tools/mcp/repository.py:112` 与 `service.py:72` 限制单用户最多 4 条（按选定最多 8 条）连接，且 `service.py:90` 对未显式声明策略的远端工具默认强制 `ToolPolicy("unknown", "required")`（每次调用弹 HITL 审批）。
- **影响**：若精简方案直接声称“复用 MCP”却未处理上述 3 点，上线后会出现真实云端 MCP 连不上、上架新插件仍需改 `.env` 重启、每调一次只读 MCP 工具都弹审批卡等问题。
- **可执行动作**：在修订后的精简方案中显式设计：
  - 将 `McpSession` 协议版本放宽为受支持的 Streamable HTTP 版本集合（如 `{"2025-03-26", "2025-06-18", "2025-11-25"}`）；
  - 明确出站允许策略：**已上架 `published` 市场条目的精确 endpoint + 依然强制防私网/回环/云元数据/禁重定向 IP 校验**可直接放行（无需改 `.env` 重启）；用户自由填写的自定义 MCP 继续受 `COMPASS_MCP_ALLOWED_ORIGINS`（或租户/平台可信策略 + 防私网 SSRF 校验）约束；
  - 在市场条目与连接配置中保留清晰的 `read_only` / `external_write` 策略标记（写操作或未知副作用继续强制 HITL，明确声明并经审核/确认的只读查询可直接调用）。

---

### P2（建议级 — 简洁落地的补充建议）

- **P2-1：市场默认内置工具采用“受控初始化清单 + 同表上架行”而非独立硬编码分支**
  - **依据**：用户希望市场默认内置一批可直接接入的云端 MCP 工具。
  - **建议**：通过幂等 seed/初始化脚本向 `plugin_catalog_item` 写入 `source='builtin_preset', status='published'` 的预置云端 MCP 条目（含本地可演示的 Streamable HTTP MCP 夹具示例置于 `examples/showcases/lesson-28-mcp-plugins/`），避免在运行时引入“内置插件特殊 if 分支”。
- **P2-2：取消 `PluginSecretCipher` 与 `outbound_http.py` 提前抽离**
  - **依据**：复用 `tools/mcp/repository.py:68-69,128` 既有 `SecretStore` 域分离密文与 `tools/mcp/client.py` 既有 `EndpointPolicy` / `PinnedTransport` 即可满足一期需求，符合个人编码原则 #9（避免无第二真实消费者的提前抽象）。
- **P2-3：默认装载多个云端 MCP 时的容错保护**
  - **依据**：`tools/mcp/service.py:94-103` 对 `required=False` 的不可达连接会自动跳过并记 `skipped`，不阻塞 Run。
  - **建议**：市场/自定义接入的 MCP 默认 `required=False`，并将 `McpRun.open()` 发现超时控制在 5–8 秒、每用户并发启用上限保持在合理有界值（如 6–8 个），确保单个外部云端 MCP 宕机不拖垮主分析对话。

---

## 3. 范围取舍总表（保留 vs 删减/延后 vs 复用）

| 类别 | 具体能力/模块 | 处理结论 | 依据 / 未来触发条件 |
| --- | --- | --- | --- |
| **必须保留与补齐** | 云端 Streamable HTTP MCP 统一接入（市场预置一键接入 + 用户自定义接入） | **保留（一期核心）** | 直接命中用户核心诉求，复用现有 `tools/mcp/` |
| **必须保留与补齐** | 开启即默认装载进工具上下文（修复 L27 `selected_mcp_server_ids` 必须手选的缺口） | **补齐（P0-2）** | 用户明确要求“开启后默认装载到工具上下文”，需受 12/32 槽位与 `grants.plugins` 约束 |
| **必须保留与补齐** | 极简三步准入与上架（静态契约/重名/禁词检查 → 真实 MCP 握手 `list_tools` 探测 → 管理员审核上架与一键下架） | **保留并精简（P0-3）** | 用确定性低延迟门禁守住质量与安全底线 |
| **必须保留与补齐** | SSRF 防护、凭据只写不回显、写操作 HITL 确认与单点不可达跳过 | **保留复用（P1-4/P2-3）** | 生产必要安全与可靠性底线，几乎零新增开发成本 |
| **必须保留与补齐** | Web「插件 → 全部插件」精简 3 视图（市场 / 我的插件 / 审核上架） | **保留并精简（P1-3）** | 点亮既有导航 `plugins-all`，聚焦高频主路径 |
| **删除或延后** | OpenAPI 3.x 导入裁剪、Request Mapping 模板引擎、Response Shaping 投影器、自定义 HTTP 错误映射、独立 `PluginHttpBackend` | **删除/延后** | 一期收敛为云端 Streamable HTTP MCP；未来若确有大量无法封装 MCP 的遗留 REST 接口再单独评估 |
| **删除或延后** | Skill 导入、`skillRefs`、`promptPresets` 预留字段 | **删除/延后** | 用户明确暂不支持，留待第 29–30 课及未来独立 Registry 解决上下游兼容 |
| **删除或延后** | 在线 Embedding 语义撞车检测、词法降级双轨、`tool_vectors_json` | **删除/延后** | 触发条件：市场已上架插件超过 50+ 个且静态重名/人工审核出现明显误选 |
| **删除或延后** | 上架期在线 LLM 14 题防抢流回放门禁（`replay_gate.py`） | **删除/延后** | 触发条件：沉淀为离线 Tool Eval 回归套件或后续高阶治理课次，不阻塞实时上架 |
| **删除或延后** | `plugin_version` SemVer 多版本仓库、版本 Diff 对比、灰度升级/回退、独立审计/报告 3 张表、双人审批开关 | **删除/延后** | 触发条件：多租户大规模共享运营出现不兼容升级诉求时再按需演进 |
| **保持现状复用** | `McpSession`、`EndpointPolicy`、`PinnedTransport`、`McpRepository`、`SecretStore`、`McpRun`、`ActionableToolError`、`ToolResultStore` | **直接复用** | 均已在代码库落地且具备既有测试覆盖，仅做小步协议版本与白名单适配 |

---

## 4. 待用户确认事项与测试缺口

### 待确认的关键取舍（改写 PRD 与设计前需拍板）

1. **接入维度（个人级 vs 工作区级）**：用户表述为“每个用户自己接入之后，开启就可以默认装载到它的工具上下文”。现有 `mcp_connection` 天然是 **工作区内的个人维度 `(tenant_id, workspace_id, actor_user_id)`**。确认一期保持**个人接入维度**（不同成员各自接入并开启自己的市场/自定义插件，凭据互不可见），还是需要管理员为整个工作区统一安装共享凭据？
2. **自定义 MCP 的出站域名策略**：对于**市场已审核上架**的云端 MCP，建议审核通过后自动放行其精确 Endpoint（同时强制防私网/云元数据/禁重定向）；对于**用户自行填写的自定义云端 MCP**，一期是允许任意公网 HTTPS 域名（经严格防私网/元数据 SSRF 拦截），还是继续受环境变量 `COMPASS_MCP_ALLOWED_ORIGINS` 白名单限制？
3. **课程大纲同步边界**：确认本次先只按精简方向修订 Compass 中 Feature `969` 的 PRD 与技术方案，还是同步调整课程仓库第 28 课（以及第 31 课）的大纲范围？

### 业务测试缺口（交编排者按需安排）

- 精简重构后，需重点补齐 4 类核心行为验证（可在方案修订后通过 `my-create-test-plan` 建档）：
  1. **开启即默认装载与预算上限**：验证用户开启 1–N 个 MCP 后无需每次对话手选即可进入工具上下文，撤销 `module.plugins` 或超 12/32 槽位时按预期拦截/截断；
  2. **外部故障隔离**：验证已开启的某个云端 MCP 宕机或超时不阻塞主分析 Run；
  3. **极简三步准入与一键下架**：验证非法/私网地址、重名工具、握手失败被拦截，已上架条目下架后所有引用它的连接在新 Run 与在途 `revalidate` 中立即生效阻断；
  4. **数据库迁移矩阵**：验证 `agent/main` 从 `0020` 升级（新增 `plugin_catalog_item` + 扩展 `mcp_connection`）的空库、旧库保留既有个人 MCP 数据、权限与恢复演练。
