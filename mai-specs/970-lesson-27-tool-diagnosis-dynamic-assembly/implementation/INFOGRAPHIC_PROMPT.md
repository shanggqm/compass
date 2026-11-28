# 第 27 课选型信息图生成说明

采用内置 image_gen 工具生成。内容依据当前 970 技术方案及实施结果，信息图聚焦本课技术选型；验收状态见 [IMPLEMENTATION.md](IMPLEMENTATION.md)。最终图保存为 [ARCHITECTURE_CONCLUSION.png](../design/ARCHITECTURE_CONCLUSION.png)，已检查中文、关键约束、数字及流程关系。

```text
Use case: infographic-diagram
Asset type: one polished Chinese course architecture conclusion infographic, landscape 16:10, high resolution, suitable for a course handout.
Primary request: summarize the final technical architecture choices of Compass Lesson 27, based on the implemented three-channel tool assembly design. This is a technical decision infographic, not a product UI screenshot.
Style: professional editorial information design. White/off-white background, charcoal type, muted teal for core/runtime, blue for scene routing, amber for manual extensions. Crisp technical lines and small simple pictograms. Large highly legible Chinese text, generous whitespace, restrained accents, no illustration clutter, no 3D. Use an orderly grid and one clear top-to-bottom execution spine. Preserve the exact technical names and numbers below. Do not invent other products or percentages.
Text/content, render in Chinese with English identifiers only where given:

TOP TITLE:
“COMPASS · 第 27 课”
“技术架构选型结论”
Small subtitle: “极简工具契约 · 三通道确定性装配 · 持久流式交付”

TOP DECISION STRIP, prominent:
“选型：自研 ToolAssembler，复用现有 Runtime”
Below smaller: “授权先行，入口冻结，按阶段交付”

MAIN ARCHITECTURE, top to bottom, three spacious horizontal tiers:

TIER 1 header “① 入口与授权”
Two connected blocks:
“Web / BFF” with “Next.js · Chat 工具与扩展选择器” and “toolSelection”
arrow to
“Agent Runtime” with “Python / FastAPI” and “授权工具池 ≤ 128 · 冻结工具与选择快照”
Add a compact gate tag before the next tier: “权限与身份硬过滤”

TIER 2 header “② 三通道装配”
Three parallel color-coded columns flowing together into “ToolAssembler”.
Teal column “常驻 always_on” with “11 个核心工具” and “语义与 SQL · 文档 · 定时读写 · 结果读取 · 收口”
Blue column “场景 scene_routed” with “深度分析增强” and “todo_write · python_analysis · sub_agent”
Amber column “手选 user_selected” with “长任务 · 工作流编排 · 口径治理” and “个人 workflow / MCP”
Under converged ToolAssembler, a clear capacity bar:
“单步 ≤ 32 工具 / 64 KiB    扩展 ≤ 12”
Small supporting line: “未选 MCP 不探测 · work 阶段工具前缀保持一致”

TIER 3 header “③ 执行与交付”
Left block “三层 ToolSpec” with 3 stacked lines:
“模型：极简 Schema”
“适配：补齐确定性参数”
“治理：通道与权限标签”
Middle block “沿用既有执行内核” with:
“Dispatcher · HITL · Sandbox”
“审批版本冻结 · 幂等 · 失败修复”
Right block, connected after middle:
“final_answer(mode, artifactFormat?)”
“plain / evidence 独立 Prompt”
“SSE 流式正文 → 引用 / 附件 → stop”
Small tag in right block “回答阶段 tools = []”

BOTTOM FOUNDATION, full width:
“PostgreSQL · 复用既有 JSONB”
“Run / Journal 持久恢复 · 兼容旧协议 v1 / v2 · 无新增 DDL”

BOTTOM FOOTER, two compact statements with visually separated columns:
“当前实现：声明 42,739 B → 15,054 B（−64.8%）”
“下一课：ToolSearch 与插件市场”

Typography hierarchy: title dominant, three-tier headers clear, block labels medium, detail text readable. Simple arrow connections should express Web→authorized pool→three channels→assembler→execution→answer; database foundation supports the execution flow. Do not draw any cross-app SQL access. Permission tag must precede assembly. The manual groups never imply bypass of permissions or approvals. No claims that all acceptance criteria passed, no test counts, no fake corporate logos, no extra subtitle copy.
```
