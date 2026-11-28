# 第 27 课初始化上下文：工具体系诊断与 ACI 改造

整理日期：2026-11-26。来源：本次会话中用户要求的 Compass 工具调研、课程目标对照及 ACI 诊断。用户已授权以这些材料创建第 27 课 Feature；本文记录创建输入与待评估建议，后续规格阶段确定正式需求及验收编号。

## 目标与范围

围绕 19 个静态工具建立可复现诊断基线，改善面向 Agent 的选择边界、参数契约、状态可见性、结果衔接及错误自愈；在统一授权与执行底座上建设常驻核心、场景动态和按需发现三层供给，兼顾角色、执行阶段、缓存、持久恢复和观测。

本次只初始化 Feature 与归档上下文。工具重构、动态组装、评测建设及 UI 改动均属于后续阶段。

## 已观察的基线

- 静态目录有 10 个通用工具：final_answer、goal、todo_write、database_query、read_tool_result、python_analysis、sub_agent、create_workflow、schedule_read、schedule_write。
- 静态目录有 9 个业务工具：search_semantics、get_metric_definition、resolve_semantics、query_metric、discover_semantic_candidates、review_semantic_change、submit_semantic_change、search_documents、read_document。
- 个人工作流和 MCP 动态能力接入统一注册与执行链。现有权限复核、人工审批、沙箱、预算、超时、取消、检查点恢复及大结果原件分页应继续复用。
- Context Profile 已有阶段和范围过滤；新答案协议放开大部分阶段限制，答案循环读取注册表完整工具声明。应按当前真实装配逻辑建立基线。
- 目录上限为 32，并有声明体积约束；MCP 和个人工作流依剩余容量装载，当前尚无模型侧 search_tools 按需发现入口。
- ToolSpec、RegisteredTool、ToolPolicy 已承载声明、执行及副作用约束；场景、阶段、延迟加载等治理标签需增量完善。

## ACI 具体诊断与候选改造

1. database_query：多种数据库错误汇总为 database_query_failed，通用参数错误缺字段定位。建议安全投影错误类别、字段、合法候选和下一步动作，区分参数修复、有限重试、用户澄清、权限拒绝。
2. python_analysis：工具说明对 source_query_id 来源、data 结构、完整性、可用库及输入上限交代不足。执行有 128 KiB 数据输入限制和代码字节限制。建议由生产者提供可直接消费的数据引用、形状、完整性与大小，补最小运行示例及超限修复路径。
3. todo_write / goal：轮内计划与跨轮目标有不同职责，但模型维护版本、待办、账本和依赖的负担较重。明确唯一状态归属及同步关系；可信运行字段可由 Runtime 绑定，保留并发版本保护。
4. schedule_write：创建参数包含 Cron、时区、模型选择与运行配置。评估常见结构化日程和继承会话配置以减少转换负担，保留预计触发时间预览和所有者确认。
5. create_workflow：一次生成参数 Schema、JavaScript、试跑参数和子任务调用协议，存在跨结构一致性成本。优先提供可运行模板、精准校验和局部修复；草稿机制是否必要由后续设计评估。
6. final_answer：claims、blocks、来源位置、计算与目标覆盖的组装负担较重。优先复用来源句柄、自动派生机械映射并提供局部修正反馈，保持候选校验和交付权限复核。
7. read_tool_result 的 cursor 与 offset/limit 互斥主要在运行时校验；sub_agent 同时暴露旧 prompt 和新 tasks；部分长度校验存在字符与字节口径差异。让模型声明与执行约束对齐，旧协议在适配层兼容。
8. 本次查阅的 10 个通用工具源码声明 examples 为空；线上 PromptOps 发布版本未检查。后续补成功、边界及错误修复示例，并验证发布契约。
9. query_metric 与 database_query 需要清晰的标准指标、明细探索和用户临时算法选择规则。缺发布绑定时继续允许获授权的明细分析；权限拒绝保持受限操作边界。
10. 语义查询工具已支持已知指标直接解析、query_ref 参数复用。先减少重复发现与定义查询，再依据评测判断是否增加复合入口。

## 供给与恢复约束

- 权限硬过滤先于场景匹配和工具搜索；搜索结果不得暴露未授权能力。
- 区分完整授权工具池、本轮模型可见集、历史执行绑定；动态隐藏不影响已接受历史回执的核验与恢复。
- 角色同时考虑业务权限和主 Agent / 子 Agent / 定时运行身份；子 Agent 延续只读继承及父级统一交付。
- 当前 Profile 阶段 identify / plan / query / analyze / generate 与答案循环 work / verify 需明确映射与归属。
- 支持分析、核验回退补查及简单问题直接交付，保留受控自主分析能力。
- 稳定排序、按需激活与声明预算共同设计；缓存收益以实际测量为准。
- 治理标签与执行策略保持单一权威来源，PromptOps 可编辑文案和示例继续接受代码 Schema 校验。

## 评测与验收方向

- A 组：当前真实装配逻辑；B 组：契约和错误回执优化，装配不变；C 组：B 加场景阶段组装与按需发现。固定模型、样本、数据、权限与预算条件。
- 六项主指标：工具选择准确率、参数合法率、首轮调用成功率、冗余调用率、错误自愈率、工具声明 Token 占比。同步观察任务完成率、澄清次数、总耗时及调用成本。
- 题集覆盖近义工具边界、参数复杂度、缺参修复、读写混合、口径歧义、数据不完整、权限拒绝与无可用工具场景。
- 验证 40+ 工具池的受控单轮声明容量、动态发现、跨进程恢复、历史回执核验、排序稳定和权限变化。
- Trace 记录候选池、装载结果、隐藏原因、规则命中与声明开销，并与现有 Context Trace 衔接。
- 当前未运行模型对照实验。课程原文中的频繁误选、空转加剧等描述列为待验证假设；不填写虚构通过率或提升比例。

## 建设顺序与不做项

建议顺序：评测基线 → ACI 契约与自愈 → 结果和引用衔接 → 场景组装与按需发现 → 恢复、缓存及容量验收。

第 28–31 课的插件市场、完整 Skill 加载与自进化、看板资产平台、企业 MCP 网关本期仅保留兼容边界，不纳入本课建设。暂不预设独立向量检索或小模型工具路由服务。保留现有 Runtime 与安全控制，不新增案例专属生产分支。

若设计需要持久化组装快照或评测配置，应遵守项目数据库 owner、迁移、SQL 交付、发布与恢复规范；初始化阶段不预建迁移。

## 后续需明确

- 19 个工具逐项调整范围及历史调用兼容方式。
- 常驻工具、场景规则、阶段状态归属与按需发现兜底。
- 输出引用的最小统一契约及可信字段自动绑定范围。
- Tool Eval 样本量、阈值、模型预算与验收环境。
- Trace 是否需要 Web 新视图及字段级权限设计。

## 源码定位（本次会话查阅范围）

路径均相对 Compass 根目录：apps/agent/src/compass_agent/tools/{README.md,catalog.py,catalog_contracts.py,contracts.py,registry.py,dispatcher.py,batch.py,loader.py}；同目录 builtin/ 下对应工具；apps/agent/src/compass_agent/prompts/tools/ 下对应声明；context_runtime/profiles.py；runtime/{answer_turn.py,service.py,tool_bindings.py}。后续设计应复核届时源码及线上发布内容。
