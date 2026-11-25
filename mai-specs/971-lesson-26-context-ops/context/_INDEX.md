# 素材索引 — 971

| 文件 | 类型 | 来源与原名 | 关键内容摘要 | 收集日期 | 派生自 |
| --- | --- | --- | --- | --- | --- |
| `apps/agent/src/compass_agent/observability/` | 源码基线 | Compass `main` (`588ee4f`) | 既有独立 OTel `TracerProvider`、`Observation` 显式句柄、`privacy.py` 白名单与 `EVENT_FIELDS`、OTLP 有界异步导出、Langfuse Metrics v2 聚合 | 2026-11-25 | 源码核查 |
| `apps/agent/src/compass_agent/context_runtime/` | 源码基线 | Compass `main` (`588ee4f`) | 既有 `ContextItem`（含 `trust`/`access`/`source`/`byte_bound`）、`Selection`、`Compaction`、`PreparedContext.receipt` 与 `pipeline.py` | 2026-11-25 | 源码核查 |
| `apps/agent/migrations/versions/0019_goal_ledger.py` | 迁移基线 | Compass 工作区当前头版本 | `agent/main` 当前 Alembic 头版本为 `0019`；本期新增迁移从 `0019` 升至 `0020_context_ops` | 2026-11-25 | 源码核查 |
| `apps/agent/src/compass_agent/api/runtime_diagnostics.py` & `promptops.py` | 权限基线 | Compass `main` (`588ee4f`) | 已有 `runtime_diagnostics`（platform）、`runtime_usage`（tenant）、`promptops_manage`（platform）权限点，本期可直接复用，`web/main` 无需新增迁移 | 2026-11-25 | 源码核查 |

## 关键发现

1. `context_runtime/contracts.py` 已在 `ContextItem` 与 `Selection` 中定义了 `trust`（`managed` / `actor` / `observation` / `dictionary`）、`access`（`AccessRef`）和 `byte_bound`，为第 26 课上下文安全策略与漏斗统计提供了直接的结构基础。
2. `observability/privacy.py` 已采用显式 `FIELDS` 与 `EVENT_FIELDS` 白名单机制，本期只需按闭集规则扩展 ContextOps 与 Guardrail 标量字段，继续保证业务正文零进入 OTLP/Langfuse。
3. `observability/observer.py` 的 `Observation` 支持 `.child()`、`.native("type", ...)` 与 `.event()`，可直接映射 Langfuse 原生 `retriever`、`guardrail`、`chain`、`span` 与 `event` 类型，无需二次开发 Langfuse。

## 开放问题

- 无阻塞项；第九篇重型 EvalOps（Multi-Grader、全量 Golden Dataset 平台、CI 发布门禁）明确排除在本 Feature 之外。

## 当前课程输入

- [单课大纲与冻结原文](../../../../../../lessons/v5/formal/lesson_26_context_ops/brief/lesson_outline.md)
- [重构后的 20 页逐页大纲草案](../../../../../../lessons/v5/formal/lesson_26_context_ops/brief/lesson_outline_by_page.md)
