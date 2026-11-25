# 971: 第 26 课｜ContextOps 与上下文安全体系

| 项         | 值                                                                                                                           |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Phase      | design                                                                                                                       |
| Profile    | standard                                                                                                                     |
| 优先级     | P1                                                                                                                           |
| 负责人     | andy                                                                                                                         |
| 目标日期   | 待填                                                                                                                         |
| 涉及子系统 | apps/agent（ContextOps 观测扩展、在线安全拦截 PEP、PG 读模型与加密快照、只读 MCP Server、轻量 Replay）、apps/web（ContextOps 控制台与 BFF）、contracts |

## 一句话描述

基于第 14 课已落地的 OTel + 原版 Langfuse 底座，为 Compass 建立图形化 ContextOps 洞察与统计体系、只读 ContextOps MCP 接口、企业级上下文安全防护（信任边界/注入拦截/脱敏/派生数据权限继承）以及 Badcase 到 Context-only Replay 的轻量迭代闭环。

## 背景

Compass 在第 14 课已建成 OTel SDK + OTLP 有界异步导出 + 本地 Docker Langfuse 的模型调用观测底座（含入口白名单脱敏与正文零泄露约束），并在第 21 至 25 课落地了 Context Runtime、PromptOps、企业经营语义层、Agentic Search、上下文选择与压缩及长任务目标账本。当前系统缺少面向“上下文工程决策流”的可视化洞察、聚合统计、AI 可读排障接口以及上下文全生命周期安全硬拦截，导致性能、效果与成本退化难以一眼定位，也缺乏支撑后续 RSI（受控自进化）的高信噪比反馈基建。

## 关键决策

- **2026-11-25 范围重塑**：直接承接第 14 课的 OTel + 原版 Langfuse 底座，坚持 **Langfuse 零源码二次开发**，采用“一处 OTel 埋点，双层消费（原版 Langfuse 承接底层 Span 树深挖 + Compass PostgreSQL 轻量读模型驱动 4 大交互可视化与统计看板）”。
- **隐私与排障解耦**：严守第 14 课正文零泄露红线，OTel / Langfuse 仅记录脱敏决策元数据与 `snapshot_ref`；完整上下文快照加密存储于 Compass `agent/main`，经 RBAC 复核与审计后按需解密查看。
- **安全内联硬拦截**：上下文信任分级、Indirect Prompt Injection 防护、敏感字段脱敏与派生数据权限继承在 Context Runtime 在线主链路同步生效，并同步发射 OTel `guardrail` Observation 与审计记录。
- **与第九篇清晰分工**：本期只交付上下文层统计、脱敏 Badcase 沉淀与 Context-only Replay 局部验证闭环；系统级大规模 Benchmark、Multi-Grader、全链路 A/B 与 CI 发布门禁留给第九篇（第 36–40 课）。

## 上下文索引

- [素材索引](context/_INDEX.md)
- [产品需求文档（PRD）](spec/PRD.md)
- [高保真 HTML 静态原型](spec/contextops-prototype.html)
- [技术方案（TECH_DESIGN）](design/TECH_DESIGN.md)
- [供评审的代码设计（CODE_DESIGN）](design/CODE_DESIGN.md)
