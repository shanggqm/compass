# 素材索引 — 969

| 文件 | 类型 | 来源与原名 | 关键内容摘要 | 收集日期 | 派生自 |
|---|---|---|---|---|---|
| [lesson_outline.md](./lesson_outline.md) | md | 课程仓库第 28 课单课大纲与第七篇重排计划冻结原文 | 定义第 28 课本课目标、8 项核心教学内容、Compass 插件市场与五步准入流水线实战任务及 6 维验收标准。 | 2026-11-26 | — |
| [task.md](./task.md) | md | 课程仓库第 28 课单课任务书 | 明确与第 27 课（ToolSpec/动态组装/Tool Eval）及第 29–31 课（Skill/MCP 网关）的衔接边界与待验证工程问题。 | 2026-11-26 | — |
| [lesson_outline_by_page.md](./lesson_outline_by_page.md) | md | 课程仓库第 28 课 18 页逐页大纲（review 草案） | 展开 C01–C18 逐页教学架构与 Compass 实战细节，包括 Plugin Manifest、Request Mapping、Response Shaping、五步准入流水线、分级市场、Kill Switch 与零售缺陷插件拦截修复演练剧本。 | 2026-11-26 | — |
| [claudecode_toolsearch.md](./claudecode_toolsearch.md) | md | 用户提供的 Claude Code Tool Search 技术原理参考 | 拆解渐进式披露、Eager/Deferred 分层加载、`ENABLE_TOOL_SEARCH` 阈值策略、`ToolSearch(query, max_results)`（关键词与 `select:` 精确选择）、纯文本 BM25/Regex 无向量库检索、Tool Reference 契约展开与 Prefix Cache 保护原则。 | 2026-11-27 | — |

## 关键发现

- Compass Web 导航目录 apps/web/src/lib/page-catalog.ts 已预留「插件 → 全部插件」（id: plugins-all, path: plugins, permission: module.plugins, 当前为 placeholder）。
- Compass Agent 工具运行时当前通过 tools/catalog.py（19 个内建/业务工具）、tools/registry.py（TOOL_CATALOG_LIMIT=32、64 KB schema 上限）、tools/loader.py、tools/dispatcher.py 与 tools/result_store.py（20,000 token 内联上限、512 token 原件回执）运行，持久化位于 agent/main（当前代码基线为 0020_context_ops）。
- Agent 已具备 SecretStore/HKDF 密文封装（models/secrets.py、context_ops/crypto.py）、SSRF 防护与固定 IP 拨号（tools/mcp/client.py 中的 EndpointPolicy 与 PinnedTransport）、OpenAI text-embedding-3-small 向量客户端（documents/embeddings.py）以及可信 BFF 授权框架（apps/web/src/server/modules/models/application/authorize.ts）。
- 第 27 课（Feature 970）尚未落地代码实现；第 28 课在设计上以第 27 课的三层 ToolSpec、场景化动态 Tool Assembler、search_tools 索引和 Golden Tool Eval Suite 为前置契约，并在独立代码基线下保留最小兼容接口。

## 开放问题

- 第 28 课对应迁移版本号需与第 27 课（Feature 970）实际是否新增 agent/main 迁移对齐后再锁定（当前代码基线头版本为 0020）。
- 插件防抢流回放的快速抽样集默认规模与默认推理模型（自动路由 Flash 模型或指定专用模型）待评审确认。
- 插件语义撞车检测在未配置 OpenAI Embedding Provider 时，降级为词法/结构重叠 + 规则检测作为阻断项还是强制要求 Embedding 可用，待评审确认。
