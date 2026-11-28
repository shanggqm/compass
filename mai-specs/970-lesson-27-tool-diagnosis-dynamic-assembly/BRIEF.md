# 970: 第 27 课｜内建工具体系诊断与场景化动态组装

| 项         | 值                                                                                                                                                                     |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Phase      | review                                                                                                                                                                 |
| Profile    | standard                                                                                                                                                               |
| 优先级     | P1                                                                                                                                                                     |
| 负责人     | andy                                                                                                                                                                   |
| 目标日期   | 待填                                                                                                                                                                   |
| 涉及子系统 | apps/agent：tools、runtime、context_runtime、promptops、context_ops 与测试；apps/web：Chat 工具与扩展选择器、ContextOps 工具组装 Trace、PromptOps 标签；课程交付与说明 |

## 一句话描述

围绕全部 20 个工具完成极简单一原语 ACI 契约与错误自愈重构，建设“内建默认常驻 + 场景分流触发 + 前端用户手动选择”三通道确定性工具组装机制，建立 Tool Eval 对照评测基线。

## 背景

Compass 已具备统一注册、授权审批、沙箱与持久恢复；当前工具供给将 19 个静态工具全量平铺，且存在复合多分支工具、机械确定性参数泄漏与参数级说明缺失等问题。结合第 27 课规划与工具定义深度诊断，本课聚焦契约瘦身与三通道确定性动态组装，将模型主动 `ToolSearch` 检索机制留至第 28 课随自定义插件与扩展体系一并建设。

## 关键决策

- **有效需求与验收唯一源**：[`spec/PRD.md`](spec/PRD.md)（标准规格 `standard`，维护 `FR-01`～`FR-12`、`BR-01`～`BR-10`、`NFR-01`～`NFR-05` 及 `AC-01`～`AC-12`）。
- **MVP 技术方案**：① 全部 20 个工具（19 个静态工具 + 子任务 `report_to_parent`）采用“外简内全”原则去复合化并清零 `connectionId`、`request_key` 等机械参数，19 个静态工具声明从 `42,719 B` 压缩至 `14,980 B (−65%)` 且无损兼容既有后端状态机，`final_answer(mode, artifactFormat?)` 在新建 `answerProtocol == 3` 下先过 `check_access` 再由工程层注入排序证据驱动双独立流式 Answer Prompt（兼容 `publish_citations` / `<CitedAnswer>` 与附件交付）；② 解耦 `ToolRegistry` 授权池上限（`128`，未选外部 MCP 按需探测）与单步模型可见上限（`32` / `64 KiB`），通过 `always_on`（11 个 DataAgent 核心常驻底座：控制 + 语义/SQL + 文档核对 + 定时读写，仅 `7,938 B / −81%`，`schedule_write` 受 HITL 与身份硬过滤保护）+ `scene_routed`（`todo_write`、`python_analysis`、`sub_agent` 深度分析增强包）+ `user_selected`（`RunConstraints.toolSelection` 手动勾选长任务、工作流编排、语义口径治理与个人 `workflow_*`/`mcp_*`）三通道在工作阶段入口冻结标志一次性确定工具序列，保持单次 Run 内 100% 恒定前缀、跨版本快照兼容恢复与零数据库 DDL 迁移，并预留 `deferred_pool` 接口衔接第 28 课 `ToolSearch`。详见 [`design/TECH_DESIGN.md`](design/TECH_DESIGN.md)（已按系统可靠性与兼容评审修订）。

## 阶段产物索引

- 素材索引：[context/\_INDEX.md](context/_INDEX.md)
- 工具定义问题深度诊断报告：[design/TOOL_DIAGNOSIS_REPORT.md](design/TOOL_DIAGNOSIS_REPORT.md)
- 产品需求文档（有效需求源）：[spec/PRD.md](spec/PRD.md)
- 技术方案文档：[design/TECH_DESIGN.md](design/TECH_DESIGN.md)（HTML 审阅版：[design/TECH_DESIGN.html](design/TECH_DESIGN.html)，已内置全量 20 个工具三通道归类与左右深色代码块 Schema Diff 对比）
- 全量 20 个工具极简 ACI 契约、三通道分类与 Schema 对照详单：[design/TOOL_CONTRACTS.md](design/TOOL_CONTRACTS.md)（HTML 专题阅读版：[design/TOOL_CONTRACTS.html](design/TOOL_CONTRACTS.html)）
- 供评审代码设计附录：[design/CODE_DESIGN.md](design/CODE_DESIGN.md)（HTML 审阅版：[design/CODE_DESIGN.html](design/CODE_DESIGN.html)）
- 分批实施与验收计划：[implementation/PLAN.md](implementation/PLAN.md)
- 实施与验收结果：[implementation/IMPLEMENTATION.md](implementation/IMPLEMENTATION.md)
- 业务测试计划与 Tool Eval 结果：[test/TEST_PLAN.md](test/TEST_PLAN.md)
- 代码自检与修复后复评：[review/code_review.md](review/code_review.md)
- 当课技术架构选型结论信息图：[design/ARCHITECTURE_CONCLUSION.png](design/ARCHITECTURE_CONCLUSION.png)（内置 image_gen；[生成提示词](implementation/INFOGRAPHIC_PROMPT.md)）

## 本次实施状态

2026-11-27，按用户连续授权完成六批实现和分批验证，进入 `review`。三通道组装、20 个模型契约与无损适配、v3 流式交付、选择器和观测入口均已落地。真实模型 A/B/C 已交付 28 题对照；AC-12 的参数合法率、错误恢复率本轮均为 100%，未达到严格提升目标，保留待验。完整测试结果及环境缺口见实施记录。源码未提交或推送；随后按用户授权启动本机最新代码并发布第 27 课 Prompt 资产，供手动测试。

2026-11-27 追加共享 Sandbox 授权已实现并实际验收，见 [补充设计与0021迁移](design/SHARED_SANDBOX.md) 和实施记录。原工具组装的无DDL范围保持；此次资源生命周期扩展新增独立命令租约。原本课其他待验条件保持原状态，不以本次Sandbox专项替代。
