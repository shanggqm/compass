# 素材索引 — 983

| 文件                                                                                               | 类型 | 来源与原名                               | 关键内容摘要                                                       | 收集日期   | 派生自       |
| -------------------------------------------------------------------------------------------------- | ---- | ---------------------------------------- | ------------------------------------------------------------------ | ---------- | ------------ |
| [source_context.md](./source_context.md)                                                           | md   | V5 正式课总纲第 25 课（当前工作区节选）  | 长任务状态、阶段摘要、Artifact 与 Parent-Child Handoff。           | 2026-11-10 | —            |
| [codex_goal_implementation_design.md](./codex_goal_implementation_design.md)                       | md   | 指定会话《梳理 Codex Harness Goal 模式》 | Goal Controller、预算记账、跨窗口上下文与 MultiAgentV2 Handoff。   | 2026-11-24 | 会话分析整理 |
| [deepseek_harness_goal_implementation_design.md](./deepseek_harness_goal_implementation_design.md) | md   | 指定会话《梳理 Goal 模式实现机制》       | Goal 状态投影、续轮准入、上下文压缩、SubAgent Inbox 与双通道交付。 | 2026-11-24 | 会话分析整理 |

两份 Goal 技术方案状态为 `review`，用于本需求后续设计；各文档分别整理已有 Harness 机制与 Compass 扩展建议。当前需求与验收标准继续由 [PRD](../spec/PRD.md) 维护。

## 关键发现

- 本次确认先建设 Child Run 的并发执行与分析 Chat 下钻。父 Run 展示状态和摘要，可进入该子任务的授权执行轨迹。
- 第 20 课提供按权限投影的 Run Event Stream 和最终回答展示契约，第 25 课将其复用于 Child Run。

## 开放问题

## 当前课程输入

- [单课大纲与 Compass 实战范围](../../../../../../lessons/v5/formal/lesson_25_long_running_context_memory/brief/lesson_outline.md)
- [逐页草案与分阶段实施建议](../../../../../../lessons/v5/formal/lesson_25_long_running_context_memory/brief/lesson_outline_by_page.md)
