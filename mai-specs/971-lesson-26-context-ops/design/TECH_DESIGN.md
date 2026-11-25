# 第 26 课｜ContextOps 与上下文安全体系 技术方案

> Feature：971 | 有效需求来源：[PRD](../spec/PRD.md) | Profile：standard | 模板：[TECH_DESIGN.tmpl.md](../../_templates/TECH_DESIGN.tmpl.md) | 代码基线：`main @ 588ee4f` + 工作区 `0019_goal_ledger.py` | 状态：待评审

> Compass 项目规则：[数据所有权与 SQL 迁移管理规范](../../../docs/database-migrations.md)。本期数据库变更全部归属 `agent/main`（Alembic `0019 → 0020`），`web/main` 无需新增迁移。

[返回需求简报](../BRIEF.md)

## 需求依据与验收

本方案对应 [PRD](../spec/PRD.md) 中的 `FR-01` 至 `FR-08`、`FR-C01` 至 `FR-C02`、`BR-01` 至 `BR-06`、`NFR-01` 至 `NFR-04`，以及全部 8 项验收标准 `AC-971-01` 至 `AC-971-08`。

## 1. 方案概述

### 1.1 设计目标和设计原则

1. **零二开复用第 14 课观测底座**：保留原版 Docker Langfuse 与既有 `observability/` 模块（独立 OTel `TracerProvider`、显式 `Observation` 句柄、BSP 有界异步导出、`privacy.py` 白名单脱敏），仅向上扩展 Context 层的标准 OTel Span 类型（`retriever`、`guardrail`、`chain`、`event`）与闭集元数据，守住业务正文零进入 OTLP/Langfuse 的硬边界。
2. **一处埋点、双层消费，支撑图形化洞察与 AI-Native 读取**：原版 Langfuse 负责底层原始 Span 树深挖与长尾排障；Compass `agent/main` 新增轻量结构化读模型与加密快照表，直接驱动 Web 端 4 大交互可视化视图、4 维上下文统计看板与只读 ContextOps MCP 接口。
3. **安全策略内联同步硬拦截**：上下文信任分级、Indirect Prompt Injection 检测、敏感脱敏与多租户/派生数据权限继承在 `context_runtime/pipeline.py` 在线主链路同步执行，先拦截/脱敏再装配进窗，同步记录安全审计并异步导出 OTel `guardrail` Span。
4. **严格 MVP 与第九篇边界**：本期在 `agent/main` 交付脱敏 Badcase 沉淀与内存级确定性 Context-only Replay（不触发外部工具与写副作用）；系统级大规模 Benchmark、Multi-Grader、全链路 A/B 与 CI 发布门禁留给第九篇（第 36–40 课）。

### 1.2 整体架构

```mermaid
flowchart TB
    subgraph WEB["apps/web (Next.js 16 / React 19 / BFF)"]
        UI["ContextOps 控制台<br/>4 大视图 / 4 维统计 / 安全审计 / Badcase 回放"]
        BFF["BFF Proxy<br/>/api/web/v1/contextops/*<br/>会话鉴权 + Schema 校验 + no-store"]
        UI --> BFF
    end

    subgraph AGENT["apps/agent (Python 3.13 / FastAPI)"]
        CR["context_runtime/pipeline.py<br/>上下文编译主链路"]
        PEP["context_security/guard.py<br/>在线 PEP：信任边界 / 注入检测 / 脱敏 / 权限继承"]
        OTEL["observability/context_spans.py<br/>OTel 子 Span：retriever / guardrail / chain"]
        REC["context_ops/recorder.py<br/>故障隔离投影 + 加密快照"]
        API["api/contextops.py<br/>REST 查询 / 鉴权快照 / Badcase / Replay"]
        MCP["context_ops/mcp_server.py<br/>只读 ContextOps MCP (/v1/contextops/mcp)"]
        REP["context_ops/replay.py<br/>确定性 Context-only Replay"]

        CR --> PEP
        PEP --> CR
        CR -.OTel 零正文元数据.-> OTEL
        CR -.结构化摘要 + 脱敏快照.-> REC
        PEP -.同步安全审计.-> REC
        API --> REP
    end

    subgraph STORAGE["数据与观测底座"]
        PG[("agent/main PostgreSQL 17<br/>0020_context_ops<br/>摘要 / 加密快照 / 安全审计 / Badcase")]
        LF["原版 Docker Langfuse (零二开)<br/>127.0.0.1:3002"]
    end

    EXT["外部 AI / IDE Agent"] -->|"Bearer Token + 只读 JSON-RPC"| MCP
    BFF -->|"内部 HTTP + TrustedContext"| API
    OTEL -->|"OTLP HTTP/protobuf 有界异步"| LF
    REC --> PG
    API --> PG
    MCP --> PG
    UI -.一键跳转 trace_id.-> LF
```

### 1.3 关键决策与 Review 索引

| 决策 | 推荐及理由 | 代价与备选 | 待判断事项/确认状态 | 详情 |
| --- | --- | --- | --- | --- |
| Langfuse 零二开 + Compass 轻量 PG 读模型 | 保留原版 Langfuse 升级能力；PG 读模型精准服务 4 大图形视图、4 维统计与 MCP 低延迟查询，避免高成本魔改 Langfuse 前端或受限于其通用 JSON 树 | 需维护 4 张只追加/轻量业务表；备选“纯调 Langfuse Public API 拼装漏斗”延迟高、无法承载租户细粒度加密快照与权限复核 | 已确认 | §2.1.1, §2.3.2 |
| 加密 Context Snapshot 与 OTel 零正文解耦 | 沿用 L14 正文零泄露红线，OTel/Langfuse 只传 `snapshot_ref` 与 SHA-256；原文脱敏后用派生子密钥加密存 `agent/main`，查看前实时复核 RBAC 并落审计 | 增加一次服务端加密/解密开销（<2ms）；备选“将上下文全文放进 Langfuse”会击穿多租户权限边界 | 已确认 | §2.1.1, [CODE_DESIGN.md](CODE_DESIGN.md#3-加密快照与故障隔离读模型投影) |
| 在线安全 PEP 采用“确定性规则 + 权限引擎”为默认基线，预留可选检测器接口 | 本地零外部依赖、P95 < 15ms、低误杀、确定性可验收；同时通过 `InjectionDetector` 协议支持生产环境插拔轻量分类器或云 Guardrail | 对高度隐蔽的语义级越狱需靠后续接入分类器增强；备选“每次编译同步调外部 LLM 审查”延迟过高（+300ms 以上）且增加成本 | 已确认 | §2.3.1, [CODE_DESIGN.md](CODE_DESIGN.md#1-在线上下文安全拦截引擎pep与派生权限继承) |
| 复用既有权限点，`web/main` 零迁移 | 复用 `runtime_diagnostics`（platform）、`runtime_usage`（tenant）、`promptops_manage`（platform），减少跨库协调与迁移复杂度 | 权限粒度与运行诊断绑定；若未来需独立安全审计员角色，再在 `web/main` 单独追加权限位 | 已确认 | §2.1.3 |
| ContextOps MCP 严格只读且复用既有 FastAPI 服务 | 挂载 `/v1/contextops/mcp`（Streamable HTTP / JSON-RPC 2.0），与现有鉴权及租户上下文统一，不引入额外常驻进程 | 需严格白名单限制工具集合与返回字段，禁止暴露原文快照；备选“独立 Node MCP 进程直连数据库”违反跨进程数据所有权约束 | 已确认 | §2.3.4, [CODE_DESIGN.md](CODE_DESIGN.md#5-只读-contextops-mcp-服务接口) |

## 2. 详细设计方案

### 2.1 核心结构设计

#### 2.1.1 数据结构设计与数据库迁移登记

- **数据库归属**：`agent/main`（唯一 owner：`apps/agent`）
- **迁移工具与源路径**：Alembic，`apps/agent/migrations/versions/0020_context_ops.py`
- **版本依赖**：`down_revision = "0019"`（`0019_goal_ledger.py`）→ 目标版本 `0020`
- **SQL 交付路径**（实现阶段生成）：
  - 增量 SQL：`apps/agent/db/main/sql/0020_context_ops.sql`
  - 完整 SQL：按现有 `apps/agent/db/main/` 派生入口同步刷新
- **旧数据处理与回滚**：全部为新增独立表，不修改既有表结构；历史 Run 在 `context_decision_summary` 无记录时，前端按“历史记录缺少上下文决策摘要”优雅降级展示（兼容 `AC-01` 旧语义）。回滚只需 `alembic downgrade 0019` 删除 4 张新表，零影响历史业务数据。
- **`web/main` 数据库**：无变更。

新增 4 张表结构如下：

1. **`context_decision_summary`**（上下文编译决策摘要与漏斗读模型）
   - 主键：`id UUID PRIMARY KEY`
   - 关联字段：`tenant_id UUID NOT NULL`、`workspace_id UUID NOT NULL`、`run_id UUID NOT NULL`、`step_id UUID NOT NULL`、`call_id UUID NULL`、`trace_id VARCHAR(64) NULL`、`snapshot_ref VARCHAR(64) NULL`
   - 上下文与版本：`scene_id VARCHAR(64) NOT NULL`、`task VARCHAR(32) NOT NULL`、`phase VARCHAR(32) NOT NULL`、`policy_digest VARCHAR(64) NOT NULL`、`prompt_generation INT NOT NULL`、`semantic_version VARCHAR(64) NULL`
   - 性能与 Token 统计：`duration_ms INT NOT NULL`、`tokens_candidate INT NOT NULL`、`tokens_selected INT NOT NULL`、`tokens_after_compaction INT NOT NULL`、`token_limit INT NOT NULL`、`cache_hit_tokens INT NULL`
   - 漏斗计数：`candidate_count INT NOT NULL`、`selected_count INT NOT NULL`、`omitted_count INT NOT NULL`、`security_blocked_count INT NOT NULL`、`redacted_count INT NOT NULL`、`compaction_applied BOOLEAN NOT NULL`
   - 状态与结构化明细：`status VARCHAR(32) NOT NULL`、`error_code VARCHAR(64) NULL`、`source_breakdown_json JSONB NOT NULL`（按来源统计候选/入窗 Token）、`funnel_json JSONB NOT NULL`（5 阶段漏斗节点计数）、`decisions_json JSONB NOT NULL`（逐条脱敏决策元信息：item_id、kind、source、trust、decision、reason、byte_bound，不含明文）、`created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
   - 索引：
     - `idx_ctx_summary_tenant_created (tenant_id, workspace_id, created_at DESC)`（支撑总览统计与列表筛选）
     - `idx_ctx_summary_run_step (tenant_id, run_id, created_at ASC)`（支撑 Run 全流程视图）
     - `idx_ctx_summary_policy_scene (tenant_id, scene_id, policy_digest, created_at DESC)`（支撑按场景/策略聚合）

2. **`context_snapshot`**（加密上下文快照）
   - 主键：`snapshot_ref VARCHAR(64) PRIMARY KEY`（格式 `csnp_<uuid4_hex>`）
   - 归属与血缘：`tenant_id UUID NOT NULL`、`workspace_id UUID NOT NULL`、`run_id UUID NOT NULL`、`step_id UUID NOT NULL`、`content_sha256 VARCHAR(64) NOT NULL`、`min_trust_level VARCHAR(16) NOT NULL`（派生最低信任等级）、`access_scope_json JSONB NOT NULL`（派生最严格访问范围交集）、`data_classification VARCHAR(32) NOT NULL`
   - 加密载荷：`ciphertext BYTEA NOT NULL`（HKDF 派生子密钥 + Fernet 认证加密，内含脱敏后候选条目与装配结果 JSON）、`expires_at TIMESTAMPTZ NOT NULL`、`created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
   - 索引：`idx_ctx_snapshot_tenant_run (tenant_id, workspace_id, run_id)`、`idx_ctx_snapshot_expires (expires_at)`

3. **`context_security_event`**（上下文安全拦截与快照读取审计，append-only）
   - 主键：`id UUID PRIMARY KEY`
   - 字段：`tenant_id UUID NOT NULL`、`workspace_id UUID NOT NULL`、`actor_id UUID NULL`、`run_id UUID NULL`、`step_id UUID NULL`、`trace_id VARCHAR(64) NULL`、`event_type VARCHAR(32) NOT NULL`（`injection_blocked` / `tenant_mismatch_rejected` / `pii_redacted` / `trust_downgraded` / `snapshot_viewed` / `mcp_queried`）、`action VARCHAR(16) NOT NULL`（`block` / `redact` / `isolate` / `deny` / `allow_audit`）、`rule_code VARCHAR(64) NOT NULL`、`source_kind VARCHAR(32) NULL`、`trust_level VARCHAR(16) NULL`、`target_ref VARCHAR(128) NULL`、`metadata_json JSONB NOT NULL`（严禁含明文敏感值）、`created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
   - 索引：`idx_ctx_sec_tenant_time (tenant_id, workspace_id, created_at DESC)`、`idx_ctx_sec_event_type (tenant_id, event_type, created_at DESC)`

4. **`context_badcase`**（脱敏 Badcase 与局部回放基线）
   - 主键：`id UUID PRIMARY KEY`
   - 字段：`tenant_id UUID NOT NULL`、`workspace_id UUID NOT NULL`、`created_by UUID NOT NULL`、`run_id UUID NOT NULL`、`step_id UUID NOT NULL`、`snapshot_ref VARCHAR(64) NOT NULL`、`scene_id VARCHAR(64) NOT NULL`、`root_cause_category VARCHAR(32) NOT NULL`（`source_data` / `semantic` / `retrieval` / `ranking_cutoff` / `compression_loss` / `security_false_positive` / `model_reasoning`）、`title VARCHAR(160) NOT NULL`、`note TEXT NULL`、`policy_digest VARCHAR(64) NOT NULL`、`prompt_generation INT NOT NULL`、`semantic_version VARCHAR(64) NULL`、`baseline_metrics_json JSONB NOT NULL`、`status VARCHAR(24) NOT NULL DEFAULT 'open'`、`created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
   - 索引：`idx_ctx_badcase_tenant_created (tenant_id, workspace_id, created_at DESC)`

#### 2.1.2 数据流设计

1. **主链路（同步安全 + 异步观测）**：
   - `context_runtime/pipeline.py` 收集完候选 `ContextItem` 后，首先同步调用 `ContextSecurityGuard.enforce()`：
     - 校验每个 `ContextItem.access` 是否匹配当前 `TrustedContext` 的 `tenant_id`/`workspace_id`/授权范围；不匹配则直接踢出并记 `tenant_mismatch_rejected`。
     - 对 `observation` / `actor` 条目运行注入规则与结构检测；命中高危规则则拦截踢出并记 `injection_blocked`；未命中但属 `observation` 的条目显式加上不可信数据边界封装，防止升权。
     - 对候选文本执行 PII / 密钥脱敏，记录 `pii_redacted` 计数。
     - 若后续发生压缩/摘要，调用 `derive_lineage()` 计算派生结果的 `min_trust_level`（按 `managed > dictionary > actor > observation` 取最低）与 `access_scope` 交集，确保摘要既不洗白低信任指令、也不丢失权限边界。
   - 编译完成后：
     - 通过 `observability/context_spans.py` 在父级 `Observation` 下挂载 `compass.context.retrieve`（`retriever`）、`compass.context.guardrail`（`guardrail`）、`compass.context.compile`（`chain`），仅写闭集白名单标量与 `snapshot_ref`。
     - 调用 `ContextOpsRecorder.record()` 将摘要、加密快照与安全审计写入 `agent/main`。其中安全审计写入失败记录告警计数，观测摘要/快照写入采用独立事务和异常捕获，绝不阻塞模型调用返回。
2. **查询与回放链路**：
   - Web 前端请求 `/api/web/v1/contextops/*` → BFF 验会话与权限并注入 `TrustedContext` → Agent `/v1/contextops/*` 按 `tenant_id`/`workspace_id` 强制过滤返回。
   - 读取加密快照（`GET /v1/contextops/snapshots/{snapshot_ref}`）时，校验 `snapshot.tenant_id == context.tenant_id`、`snapshot.workspace_id == context.workspace_id` 且权限满足 `access_scope_json`，通过后先写入一条 `snapshot_viewed` 审计再返回解密后的脱敏快照。
   - 触发 Context-only Replay（`POST /v1/contextops/badcases/{id}/replay`）时，从 `context_snapshot` 解密冻结候选集，在纯内存中以相同输入分别运行基准策略与候选策略参数（仅运行过滤/排序/压缩/预算逻辑，零外部工具调用、零写副作用），返回确定性 Diff。

#### 2.1.3 接口设计

所有 Agent 接口定义于 `apps/agent/src/compass_agent/api/contextops.py` 与 `schemas.py`，经 `pnpm contracts` 同步生成 OpenAPI 与 Web TypeScript 类型；Web BFF 在 `apps/web/src/app/api/web/v1/contextops/[...path]/route.ts` 透传并做响应 Schema 校验与 `Cache-Control: no-store`。

| 接口 | 权限要求 | 核心输入 | 核心输出 |
| --- | --- | --- | --- |
| `GET /v1/contextops/overview` | `runtime_diagnostics` (platform，支持按 `actor_id` 查全员) 或 `runtime_usage` (tenant，服务端强制限定 `actor_id=ctx.actor_id`) | `window` (`24h`/`7d`/`30d`), `actor_id?`, `policy_digest?` | 顶部常驻看板载荷（仅真实可观测字段）：总体运行卡 + 具体统计（`session_count`、`run_count`、`avg_turns`、`min_turns`、`max_turns`、`turn_completion_breakdown`、`avg_search_turns`、`terminal_outcome_counts`） |
| `GET /v1/contextops/intents` | 同上 | `window`, `actor_id?`, `policy_digest?` | 意图识别观测 Tab 载荷：全局意图分类分布（含 JEV/Flash/兜底判决来源占比）、各意图分类耗时分布（`p50_ms`/`p95_ms`/`avg_ms`）与近期意图判决明细 |
| `GET /v1/contextops/anomalies` | 同上 | `window`, `actor_id?`, `policy_digest?` | 异常与耗时观测 Tab 载荷：工具执行错误排行、检索空结果与压缩截断统计、各业务阶段 P50/P95 耗时分布与异常 Run 明细 |
| `GET /v1/contextops/runs` | 同上（普通用户强制仅返回本人 Session/Run） | `window`, `actor_id?`, `session_id?`, `scene_id?`, `status?`, `has_security_event?`, `failed_stage?`, `limit`, `cursor?` | 级联会话与 Run 摘要列表（成员、会话标题、总步数、总耗时、总 Token、淘汰率、是否压缩、安全拦截数、trace_id） |
| `GET /v1/contextops/runs/{run_id}/flow` | 同上（强制同租户校验；普通用户额外校验 `run.actor_id == ctx.actor_id`） | `run_id` | 双视图统一载荷：技术语义三轨瀑布图（`input`/`model`/`tool`）+ 轮次轨迹流 + 四标签节点详情，以及业务语义漏斗数据、窗口构成占比、逐条决策卡片与 Langfuse 深链元信息 |
| `GET /v1/contextops/snapshots/{snapshot_ref}` | `runtime_diagnostics` (platform) 或 `runtime_usage` (tenant) + `access_scope` 复核 | `snapshot_ref` | 解密后的脱敏候选集与装配快照 + 血缘/信任/版本信息（强制写 `snapshot_viewed` 审计；跨租户返回 403） |
| `GET /v1/contextops/security/events` | 同上 | `window`, `event_type?`, `action?`, `limit` | 分页安全审计事件列表 |
| `POST /v1/contextops/badcases` | `promptops_manage` (platform) 或 `runtime_usage` (tenant) | `run_id`, `step_id`, `root_cause_category`, `title`, `note?` | 新建脱敏 `ContextBadcaseView` |
| `GET /v1/contextops/badcases` | 同上 | `root_cause_category?`, `scene_id?`, `limit` | Badcase 列表 |
| `POST /v1/contextops/badcases/{badcase_id}/replay` | `promptops_manage` (platform) 或 `runtime_usage` (tenant) | `override`（`max_input_tokens?`, `protect_dictionary?`, `compaction_threshold?`, `max_observation_items?`） | `ContextReplayComparison`（基准 vs 候选的漏斗计数、Token 差值、新增/丢弃条目列表、关键口径保留状态） |
| `POST /v1/contextops/mcp` | 已鉴权只读服务令牌 + 租户上下文 | JSON-RPC 2.0（`tools/list`、`tools/call`） | 仅允许 4 个只读工具：`contextops_list_runs`、`contextops_get_run_flow`、`contextops_get_stats`、`contextops_list_badcases` |

### 2.2 配置变更

优先复用已有配置，新增 3 项带安全默认值的可选配置项（位于 `apps/agent/src/compass_agent/settings.py`）：

| 配置项 | 当前用途与来源 | 默认值/缺失行为 | 生效时机 |
| --- | --- | --- | --- |
| `COMPASS_CONTEXT_SNAPSHOT_TTL_DAYS` | 加密 Context Snapshot 保留天数 | `14`（天） | 新快照写入时计算 `expires_at` |
| `COMPASS_CONTEXT_SECURITY_MODE` | 上下文安全拦截模式（`enforce` / `audit_only`） | `enforce`（默认硬拦截） | 进程启动加载 |
| `COMPASS_CONTEXTOPS_MCP_ENABLED` | 是否开放只读 `/v1/contextops/mcp` 端点 | `true`（受鉴权保护） | 路由处理校验 |

加密密钥直接从既有 `COMPASS_MODEL_ENCRYPTION_KEY` 通过 HKDF-SHA256（固定 info `b"compass-context-snapshot-v1"`）派生，不新增额外必填环境变量。

### 2.3 分模块详细设计

#### 2.3.1 在线上下文安全拦截引擎（`apps/agent/src/compass_agent/context_security/`）

- **`policy.py`**：定义信任等级严格偏序 `TRUST_RANK = {"observation": 0, "actor": 1, "dictionary": 2, "managed": 3}`；提供 `derive_lineage(items)` 返回最低信任等级与 `AccessRef` 交集，确保任何派生摘要/快照绝不高于最弱输入源。
- **`detector.py`**：定义 `InjectionDetector` 协议与默认 `RuleBasedInjectionDetector`，覆盖典型 Indirect Prompt Injection 规则（如指令覆盖声明、系统提示词提取诱导、角色伪装标记、越权工具/跨租户查询诱导）；仅扫描 `observation` 与 `actor` 级条目，不误伤 `managed` 系统指令。
- **`redactor.py`**：基于预编译正则识别手机号、身份证号、邮箱、Bearer/API Key、常见数据库 DSN 密码，统一替换为 `[REDACTED:<KIND>]` 并统计脱敏次数。
- **`guard.py`**：作为 `pipeline.py` 的同步前置拦截器，输出净化后的 `ContextItem` 列表、安全决策明细和 `SecurityEventRecord` 列表。

#### 2.3.2 OTel + Langfuse 零二开观测扩展（`apps/agent/src/compass_agent/observability/`）

- 扩展 `privacy.py` 的 `FIELDS` 与 `EVENT_FIELDS`，新增闭集字段：`snapshot_ref`、`trust_level`、`guardrail_action`、`guardrail_rule`、`candidate_count`、`selected_count`、`security_blocked_count`、`redacted_count`、`compaction_ratio`、`funnel_stage`。
- 新增 `context_spans.py`：在既有 `Observation` 上创建三个子 Span：
  - `compass.context.retrieve` → `span.native("type", "retriever")`
  - `compass.context.guardrail` → `span.native("type", "guardrail")`
  - `compass.context.compile` → `span.native("type", "chain")`
- 全部遵循 `Observation` 现有生命周期与 `finally` 关闭语义；所有属性值经过 `privacy.fields()` 白名单过滤，继续维持正文零泄露。

#### 2.3.3 轻量读模型、加密快照、统计与局部回放（`apps/agent/src/compass_agent/context_ops/`）

- **`crypto.py`**：使用 `cryptography.hazmat.primitives.kdf.hkdf.HKDF` 从 `compass_model_encryption_key` 派生 32 字节 Fernet 密钥，提供 `encrypt_payload()` / `decrypt_payload()`。
- **`recorder.py`**：在独立 DB 会话中写入 `context_decision_summary`、`context_snapshot` 和 `context_security_event`，捕获异常并递增本地自诊断计数 `contextops_record_errors`，确保故障不回流主链路。
- **`analytics.py`**：基于 `context_decision_summary`、`agent_step`、`model_attempt` 与 `context_security_event` 聚合 4 维统计指标与 Run 流程视图；所有查询强制带 `tenant_id` 与 `workspace_id` 过滤条件。
- **`replay.py`**：`ContextOnlyReplayEngine` 解密 `context_snapshot` 中冻结的候选上下文，复现原策略的选择/压缩/预算管道，并按传入的 `ReplayOverride` 运行候选管道，输出确定性对比结构。

#### 2.3.4 只读 ContextOps MCP 接口（`apps/agent/src/compass_agent/context_ops/mcp_server.py`）

- 实现精简 JSON-RPC 2.0 分发器（支持 `initialize`、`tools/list`、`tools/call`），硬编码 4 个只读工具白名单，所有工具直接调用 `analytics.py` 和 `badcase` 只读查询。
- 每次 `tools/call` 均校验 `TrustedContext` 的租户范围，记录一条 `mcp_queried` 审计事件；返回结果仅包含脱敏摘要与统计数值，不提供解密快照工具。

#### 2.3.5 Compass Web 控制台（`apps/web/src/features/contextops/`）

- 新增 4 个子页面/标签：`OverviewStatsPanel`、`RunFlowInspector`、`SecurityAuditPanel`、`BadcaseReplayPanel`，复用 `apps/web/src/components/ui/` 通用组件与设计变量。
- 图表使用纯 SVG / 轻量 React 组件渲染横向步骤流程图、上下文漏斗流向图、窗口堆叠占比条与失败热点条形图，避免引入重型图形引擎，保证桌面端与窄屏清晰对齐。

### 2.4 供评审的代码设计（for review check）

核心架构骨架、不变量分支与关键接口已整理到独立代码附录：[CODE_DESIGN.md](CODE_DESIGN.md)。

评审重点索引：
1. 在线 PEP 拦截顺序与派生信任/权限交集计算：[CODE_DESIGN.md#1-在线上下文安全拦截引擎pep与派生权限继承](CODE_DESIGN.md#1-在线上下文安全拦截引擎pep与派生权限继承)
2. OTel `retriever`/`guardrail`/`chain` 映射与白名单约束：[CODE_DESIGN.md#2-otel--langfuse-上下文-span-映射零二开](CODE_DESIGN.md#2-otel--langfuse-上下文-span-映射零二开)
3. 加密快照派生密钥、RBAC 解密复核与故障隔离投影：[CODE_DESIGN.md#3-加密快照与故障隔离读模型投影](CODE_DESIGN.md#3-加密快照与故障隔离读模型投影)
4. 确定性 Context-only Replay 对照引擎：[CODE_DESIGN.md#4-确定性-context-only-replay-引擎](CODE_DESIGN.md#4-确定性-context-only-replay-引擎)
5. 只读 ContextOps MCP 白名单分发与审计：[CODE_DESIGN.md#5-只读-contextops-mcp-服务接口](CODE_DESIGN.md#5-只读-contextops-mcp-服务接口)

## 3. 风险评估

| 风险与触发条件 | 影响 | 缓解措施 | 验证方式 |
| --- | --- | --- | --- |
| 注入检测规则误杀正常业务查询（如用户问“忽略退款后 GMV 是多少”） | 正常上下文被误拦截导致回答缺失 | 规则聚焦“指令覆盖 + 角色劫持 + 系统提示提取”组合特征，且仅扫描 `observation`/`actor`；支持 `audit_only` 回退与拦截原因可视化复核 | 正常电商问答回归集 + 4 类攻防测试集双测 |
| 高并发下读模型或快照写入拖慢主链路 | 模型响应延迟升高或任务失败 | 读模型投影采用独立短事务与异常隔离，快照采用紧凑 JSON + gzip 压缩后加密，默认保留 14 天可按过期索引清理 | 注入 DB 写入异常单测验证主链路零失败 |
| `context_snapshot` 密文泄露或跨租户越权查看 | 敏感业务数据泄露 | 入库前先做 PII 脱敏；加密密钥通过 HKDF 独立派生；读取接口强制复核 `tenant_id`/`workspace_id`/`access_scope` 并写 `snapshot_viewed` 审计 | 跨租户 403 单测 + DB 密文直查敏感哨兵为 0 |
| MCP 接口被用于越权探测或写操作 | 治理接口本身引入新攻击面 | 严格白名单仅 4 个只读工具，禁止返回快照原文，复用现有 `TrustedContext` 租户隔离并记录每次调用审计 | MCP 协议越权与非法方法测试 |

## 4. 兼容性考量

| 端与版本/协议组合 | 是否兼容及依据 | breaking change / 旧端行为 | 是否需配套发版与上线顺序 |
| --- | --- | --- | --- |
| 既有 `context_runtime` 调用方与第 21–25 课测试 | 完全兼容；`PreparedContext` 原字段不变，新增安全与观测数据走可选返回/内部上下文 | 无 breaking change | 无需改造旧调用方 |
| 历史 Run（`0019` 及之前数据） | 完全兼容；`context_decision_summary` 无记录时，Run 洞察页回退展示既有 `agent_step` 基础步骤并提示“缺少上下文决策明细” | 无 | 先执行 `pnpm db:migrate -F agent`（`0019 → 0020`），再启动 Agent 与 Web |
| Docker Langfuse（第 14 课已部署） | 完全兼容原版 Langfuse OTLP 接收器，零镜像改动 | 无 | Langfuse 可选启用，不启动时 Compass 视图照常可用 |

## 5. 任务分解

| 任务 | 关联模块/决策 | 变更文件（新增/修改/删除） | 前置依赖 | 交付物与验收条件 |
| --- | --- | --- | --- | --- |
| T1：数据库迁移与实体 | §2.1.1 | 新增 `apps/agent/migrations/versions/0020_context_ops.py`、`apps/agent/src/compass_agent/context_ops/entities.py`、增量/完整 SQL | `0019` 基线 | 空库安装与 `0019 → 0020` 升级/回滚验证通过 |
| T2：在线安全拦截引擎 PEP | §2.3.1 | 新增 `apps/agent/src/compass_agent/context_security/{policy,detector,redactor,guard}.py`；修改 `context_runtime/pipeline.py` | T1 | AC-971-04 四类安全攻防单测全部通过，P95 ≤ 15ms |
| T3：OTel 上下文埋点扩展 | §2.3.2 | 修改 `observability/privacy.py`；新增 `observability/context_spans.py`；接入 `pipeline.py` | T2 | AC-971-01 内存 SpanExporter 与 Langfuse 零正文验收通过 |
| T4：读模型、加密快照、统计与 Replay | §2.3.3 | 新增 `context_ops/{crypto,recorder,analytics,replay}.py` | T1, T2 | AC-971-05、AC-971-07 加密直查、403 复核与确定性回放单测通过 |
| T5：REST API、只读 MCP 与契约同步 | §2.1.3, §2.3.4 | 新增 `api/contextops.py`、`context_ops/mcp_server.py`；修改 `schemas.py`、`main.py`；运行 `pnpm contracts` | T4 | AC-971-06 MCP 协议测试通过；`pnpm contracts:check` 通过 |
| T6：Compass Web 4 大可视化视图与 BFF | §2.3.5, UI-01 | 新增 `apps/web/src/app/api/web/v1/contextops/[...path]/route.ts`、`apps/web/src/features/contextops/*`；修改侧边栏与对话卡片入口 | T5 | AC-971-02、AC-971-03 浏览器桌面/窄屏交互与空态/降级态验收通过 |
| T7：端到端集成验收与综合门禁 | 全部 AC | 新增 `apps/agent/tests/test_context_ops.py`、`test_context_security.py`、`apps/web` 相关测试 | T1–T6 | AC-971-01 至 AC-971-08、`pnpm check`、`pnpm build` 全部通过 |
