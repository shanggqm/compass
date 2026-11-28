# 969: 第 28 课｜轻量插件市场与云端 MCP 接入

| 项 | 值 |
|---|---|
| Phase | draft |
| Profile | standard |
| 优先级 | P1 |
| 负责人 | andy |
| 目标日期 | 待填 |
| 涉及子系统 | apps/agent,apps/web,contracts |

## 一句话描述

点亮 Compass Web「插件 → 全部插件」，交付面向云端/内网 Streamable HTTP MCP 的轻量插件市场（内置预置 + 个人自定义接入、双轨统一自动化机审质检门禁、默认经 `tool_search` 按需动态发现与尾部激活），并补齐 Tool Search 在 ContextOps、OTel/Langfuse 与执行时间线的全链路观测及静态高保真原型 HTML。

## 背景

第 27 课完成 19 个内建工具契约重构、共享 Sandbox（迁移 `0021_shared_sandbox`）与三通道动态组装（`tools/assembler.py`，已预留 `deferred_pool` 扩展边界）后，Compass 需要以简单可靠的方式对外开放工具扩展。一期收敛为单一协议——远程 **Streamable HTTP MCP**，复用既有 `tools/mcp/` 底座，暂不支持 Skill 导入与 OpenAPI/HTTP 自研映射栈；确保标准云端 MCP 与公司内部自建 MCP 均可由用户以个人凭据快速接入。为防止外部工具数量不受控击穿 32 槽位 / 64 KiB 限制并破坏前缀缓存，新接入工具**默认采用 `deferred` 延迟模式进入 `deferred_pool`**，参考 Claude Code 由 **`tool_search` 元工具（关键词 BM25+Bigram / `select:` 精确选择）** 按需发现并单调尾部激活；治理侧抽取统一的最小化自动化机审引擎 `run_plugin_quality_gate()`（自用接入过机审即个人可用，市场共享走同一机审 + 管理员人工审核上架/一键下架），同时在 ContextOps、OTel Trace 与 `UiExecutionTimeline` 完整呈现延迟池规模、Token 压缩率、前缀缓存指纹与检索命中轨迹。

## 关键决策

- **单一接入协议与资产演进锚点**：一期仅支持远程 Streamable HTTP MCP，直接复用既有 `apps/agent/src/compass_agent/tools/mcp/` 底座；在 `plugin_catalog_item` 预留 `asset_type = 'mcp_remote'` 演进锚点并确立插件规范治理契约，将 ZIP/manifest 资产包与 Skill 导入留至第 29–30 课。
- **默认 `deferred` 走 Tool Search 动态发现**：严格按工作区内个人维度 `(tenant_id, workspace_id, actor_user_id)` 隔离保存个人凭据；用户开启 MCP 后自动纳入个人 Run 工具池（不写入显式 `tool_selection` 以保护 `SC-39` 纯闲聊直出），新接入默认 `loading_mode='deferred'` 进入 `deferred_pool`，由模型调用 `tool_search` 按需发现并单调尾部追加完整 Schema（保留 `always_load` 常驻逃生开关与 `auto` 阈值兼容），冻结延迟服务目录提示以 100% 保护 Prompt Prefix Cache。
- **自用与上架统一自动化机审质检**：抽出确定性 `run_plugin_quality_gate()` 固化 5 项底线检查（URL/禁云元数据/禁 3xx、真实 Streamable HTTP 握手、Schema ≤ 32 KiB 校验、描述与工具数上限、内建重名提示与高危提示注入静态拦截）；个人自用首次接入或修改 `endpoint`/`auth_type` 自动跑机审并落库 `check_status`/`last_check_json`，通过即个人可用并支持一键带入提交市场上架；市场共享走同一机审 + 管理员审核上架/一键下架。
- **单库极简数据模型（衔接 `0021_shared_sandbox`）**：`agent/main` 从 `0021` 升级至 `0022_plugin_marketplace_lite`，新增 1 张市场目录表 `plugin_catalog_item`（含 `asset_type`）并微扩既有 `mcp_connection` 5 列（`catalog_item_id`、`source`、`loading_mode`、`check_status`、`last_check_json`）；`web/main` 零迁移。
- **全链路可观测闭环**：在 ContextOps（单步 `ToolAssemblyTrace` 暴露 `tool_search_activated` 通道、声明 Token 节省率、前缀稳定摘要及工作区 `ContextOverview` 检索命中率/省流统计）、Langfuse/OTel（隐私白名单脱敏 Span）与 `UiExecutionTimeline`（脱敏检索词、匹配模式、服务/工具名与耗时）实现端到端可观测。
- **有效需求来源**：[产品规格 PRD](spec/PRD.md)（standard 档位，已吸纳第 2 轮评审结论与全链路观测要求）。

## 上下文索引

- [素材索引](context/_INDEX.md)
- [Claude Code Tool Search 技术原理参考](context/claudecode_toolsearch.md)
- [产品规格 PRD](spec/PRD.md)
- [插件市场静态高保真原型 HTML](spec/plugins-prototype.html)
- [设计评审报告（第 1 轮减法审查 + 第 2 轮目标对齐复核）](review/design_review.md)
- [技术方案 TECH_DESIGN](design/TECH_DESIGN.md)
- [代码设计附录 CODE_DESIGN](design/CODE_DESIGN.md)
- [技术方案离线 HTML 阅读版](design/TECH_DESIGN.html)（维护说明：[design/README.md](design/README.md)）
