# Lesson 28 任务：自定义插件与插件市场——打通工具接入、校验、测试、准入与装载

## 任务来源

- [单课大纲与重排计划原文](lesson_outline.md)
- [逐页评审稿](lesson_outline_by_page.md)
- [课程负责人确认的第七篇重排计划](../../../../../course/curriculum/v5/chapter7_outline_replan.md)

## 学员前置与课间衔接

学员已在第 27 课完成三层 `ToolSpec` 契约、场景化动态组装与 Tool Eval 评测基线。本课在其上开放外部业务能力接入，点亮 Compass Web「插件 → 全部插件」模块，并为第 29–30 课的 Skill 资产管理及第 31 课的自定义 MCP 治理建立统一插件准入框架。见[第 27 课工作区](../../lesson_27_tool_diagnosis_dynamic_assembly/README.md)。

## 学习目标

- 能设计涵盖元数据、资产清单、鉴权模板与风险声明的企业级 `Plugin Manifest` 模型。
- 能将高噪声后端 HTTP / OpenAPI 接口裁剪并映射为高调用准确率的 Agent 工具契约。
- 能实现包含契约校验、撞车扫描、沙箱试跑、防抢流回归与分级上架的插件准入与动态装载闭环。

## 待验证的工程问题

- 新接入的自定义插件描述与现有内置工具语义近似时，静态规则与向量相似度分别在什么阈值下能有效拦截撞车且减少误报？
- 如何在插件停用、升级或凭据轮换时，保证在途异步 Run 的一致性与新 Run 的即时隔离？

## Compass 实战：建设插件市场与自定义业务插件准入流水线

**计划交付**　`Plugin Manifest` 规范、声明式自定义插件执行适配器、五步插件准入检查流水线、Compass Web「插件市场与自定义插件中心」模块、零售扩展插件实战样本。

- 建设插件版本状态机与 Web「插件 → 全部插件」市场、创建向导及在线试跑台。
- 实现静态契约检查、全局工具撞车扫描与核心基准集防抢流回放门禁。
- 演练存在描述过宽和返回过重缺陷的自定义零售插件从拦截、修复上架到工作区装载调用的闭环。

## 下游交付要求

- 深度研究只写入 `research/synthesis.md` 和 `research/reports/`。
- 内容设计只写入 `design/slide_blueprint.md`。
- `outline_refinement_status` 未达到 `approved` 时不得开始深度研究。
- 定稿后下游不得修改 `brief/lesson_outline.md`；发现大纲与证据冲突时记录冲突并请求课程负责人决策。
