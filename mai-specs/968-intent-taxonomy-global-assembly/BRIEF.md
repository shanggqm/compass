# 968: 意图分类体系极简重构与全局动态装配

| 项 | 值 |
|---|---|
| Phase | draft |
| Profile | standard |
| 优先级 | P1 |
| 负责人 | andy |
| 目标日期 | 待填 |
| 涉及子系统 | apps/agent,apps/web,contracts |

## 一句话描述

废弃 40 场景扁平字典与双轨意图模型，建立极简正交的任务意图、业务域与复杂度三维度分类体系，打通 PromptOps、语义检索、工具与 Skill 装配、Context Runtime、契约与 ContextOps 可观测全链路（不兼容历史数据，零历史债）。

## 背景

当前意图体系将 10 个一级分类与 40 个二级场景（SC-01..SC-40）做笛卡尔积式平铺，并与 Context Runtime 引入的 deliverable 形成双轨冲突；绝大多数细粒度场景在运行期空转，业务域未真正驱动语义/知识/工具装配，工具组装器被迫硬编码多组 SC 集合。本次不背负历史数据兼容包袱，直接面向全局视角重构极简意图分类与装配体系，保证每个意图与业务域均触发真实机制。

## 关键决策

- **阶段选择**：省略独立 PRD，以本 BRIEF 与 2026-11-27 架构评审会话为有效需求来源，直接在 [技术方案](design/TECH_DESIGN.md) 登记完整范围与 AC-01 至 AC-12。
- **正交意图模型**：废除 `SC-01..SC-40` 与 `deliverable` 双轨体系，收敛为 10 项 `task_intent`（6 业务作业 + 4 边界守卫）、7 项 `domain_id`（与 Web 语义 11 域全覆盖映射，单轮 0–2 项）与 `is_compound` 复杂度标志。
- **零历史债**：预上线状态不保留历史数据兼容与旧 `SC-*` 桥接，通过 `agent/main`（`0020 → 0021`）与 `web/main`（`0010_knowledge_library → 0011_workspace_default_domains`）完成干净切换。

## 上下文索引

- [素材索引](context/_INDEX.md)
- [技术方案 TECH_DESIGN.md](design/TECH_DESIGN.md)（[离线阅读版](design/TECH_DESIGN.html)）
- [代码设计附录 CODE_DESIGN.md](design/CODE_DESIGN.md)（[离线阅读版](design/CODE_DESIGN.html)）
