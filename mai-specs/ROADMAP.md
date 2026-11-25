# 需求路线图

## 规划中

| 需求 | 标题                                                                                                                           | 规格     | 优先级 | 说明                                                                                                                         |
| ---- | ------------------------------------------------------------------------------------------------------------------------------ | -------- | ------ | ---------------------------------------------------------------------------------------------------------------------------- |
| 999  | [Compass 产品立项与课程 Landing Page App](999-compass-product-course-landing-app/BRIEF.md)                                     | standard | P1     | 完成 Compass 电商 DataAgent 的产品需求定义与交互原型开发，并开发面向《企业级Agent工程架构实战营》的多个 Landing Page。       |
| 997  | [Home 优化与课程内容完善](997-home-course-content-improvement/BRIEF.md)                                                        | standard | P1     | 优化 Compass Home 的课程展示与学习指南，完善课程全貌、学习路径、实战任务及交付说明，提升整套课程内容的完整性与一致性。       |
| 993  | [第 11 课：企业级 LLM Runtime 架构选型、多模型接入与管理](993-lesson-11-llm-runtime/BRIEF.md)                                  | standard | P1     | 为 Compass 完成 LLM Runtime 技术架构选型、多模型统一接入与真实调用调通，并提供相应的模型配置、连接测试与基础管理能力。       |
| 989  | [第 15 课｜从 LLM Runtime 到 Agent Runtime：搭建 Agent 执行内核](989-lesson-15-agent-execution-kernel/BRIEF.md)                | standard | P1     | 在第四篇 LLM Runtime 之上实现单 Agent 的同步 ReAct 执行内核，支持多轮工具调用、异常收口与 Run/Step 记录。                    |
| 987  | [第 17 课｜从自主循环到混合编排：构建 Workflow + Agent 执行引擎](987-lesson-17-hybrid-workflow/BRIEF.md)                       | standard | P1     | 交付 JS 编排 SubAgent 与源码资产、固定流程双入口和复盘 Skill 脚本。                                                          |
| 986  | [第 18 课｜从 Tool Calling 到受控执行：构建 Tool Executor 与 Sandbox](986-lesson-18-tool-execution-sandbox/BRIEF.md)           | standard | P1     | 通过统一 Tool Executor 执行受限 SQL 和隔离 Python，规范结果、Artifact、超时、取消与错误。                                    |
| 985  | [第 19 课｜从内存循环到持久运行：构建 Run State、Checkpoint 与 Human-in-the-Loop](985-lesson-19-durable-runtime-hitl/BRIEF.md) | standard | P1     | 补齐前序 Session、消息、Agent/LLM、工具和 Workflow 持久化；Docker Redis 流与 SQL 投影、Message/Run 同协议、刷新恢复及 HITL。 |
| 984  | [第 20 课｜从单进程到企业级 Runtime：实现异步调度、资源治理与可靠性验证](984-lesson-20-production-agent-runtime/BRIEF.md)      | standard | P1     | 同 App 双角色，Scheduler 单实例触发交付，Agent 多副本统一准入、公平执行与接管。                                       |
| 981 | [分析会话标题与管理](981-session-management/BRIEF.md) | simple | P1 | 首次发送展示临时标题，Agent 异步生成并持久化语义标题，侧栏支持改名和确认删除。 |
| 973 | [第 23 课 Agentic Search：企业知识库与跨源经营诊断](973-lesson-23-agentic-search/BRIEF.md) | standard | P1 | 为学员提供合成企业知识库、可复算的零售样本和需要多轮跨源检索的经营任务。 |
| 972 | [第 24 课｜动态上下文选择与压缩](972-lesson-24-context-selection-compression/BRIEF.md) | standard | P1 | 为 SQL、异常归因与经营报告按预算选择和压缩上下文，验证关键事实与任务质量。 |
| 971 | [第 26 课｜ContextOps](971-lesson-26-context-ops/BRIEF.md) | standard | P1 | 从经营分析错误定位上下文决策，回放策略并经评测、灰度和回滚完成发布。 |

## 进行中

| 需求 | 标题                                                                                                       | 规格     | 优先级 | 说明                                                                                                           |
| ---- | ---------------------------------------------------------------------------------------------------------- | -------- | ------ | -------------------------------------------------------------------------------------------------------------- |
| 974 | [第 22 课｜企业经营语义治理](974-lesson-22-enterprise-semantic-governance/BRIEF.md) | standard | P1 | 交付 Compass 经营语义资产库、AI 治理、人工审批与发布、语义解析与受控查询，以及效果验收。 |
| 975 | [第 21 课｜系统提示词与 Context Runtime](975-lesson-21-context-runtime-promptops/BRIEF.md) | standard | P1 | 四类经营任务和 PromptOps 已完成开发与端到端验收，进入交付评审。 |
| 996  | [RAG 业务知识文档数据生产](996-rag-knowledge-document-seeding/BRIEF.md)                                    | standard | P1     | 23 篇文档已生成，附 206 条原句引用、101 个章节片段与 14 个演示案例；离线检查通过，待业务审阅，向量接入未执行。 |
| 994  | [本机 Docker 确定性部署入口](994-local-docker-deploy/BRIEF.md)                                             | simple   | P1     | 命令、隔离测试与构建已交付；本机 Docker I/O 异常，真实容器验收待完成。                                         |
| 988  | [第 16 课｜从 ReAct 到 Plan-and-Execute：建立受控自主执行机制](988-lesson-16-controlled-autonomy/BRIEF.md) | standard | P1     | 已实现受控规划、验证与预算，以及 Chat 输入框上方的 Todo 面板。付费模型档案待部署验收。                         |
| 977 | [第 17 课｜动态 Workflow 与 SubAgent](977-dynamic-workflow-subagent/BRIEF.md) | standard | P1 | 在 Chat 验证自主探索、工作流创建与保存，以及换参数后的模型自主复用。 |
| 976 | [Runtime 模型元数据与默认生成参数](976-runtime-model-metadata/BRIEF.md) | standard | P1 | LM Runtime 首期实现，覆盖 Provider 模型元数据、默认参数与上游错误回传。 |

## 已完成

| 需求 | 标题                                                                                        | 规格     | 优先级 | 说明                                                                                                                    |
| ---- | ------------------------------------------------------------------------------------------- | -------- | ------ | ----------------------------------------------------------------------------------------------------------------------- |
| 983  | [第 25 课｜长任务上下文治理：让数小时运行和多 Agent 协作不丢目标、不重做、不漂移](983-lesson-25-long-task-context/BRIEF.md)    | standard | P1     | 目标与账本、自动续轮、并发交接、授权下钻已实现；数据库、真实模型及端到端验收通过。 |
| 978 | [Showcase 隔离与 Agent 通用能力收敛](978-showcase-isolation/BRIEF.md) | simple | P1 | 通用工具收敛、Showcase 隔离与长期规范完成；静态检查、构建、真实模型与数据库、Sandbox、审批恢复及浏览器验收通过 |
| 979 | [Agent 工具目录与统一注册](979-tool-catalog/BRIEF.md) | simple | P1 | 16 个工具独立目录、统一清单、Todo/Python 去耦完成；375 项 Agent 测试及静态检查、构建通过 |
| 980 | [通用 Agent Runtime 与过期案例清理](980-generic-agent-runtime/BRIEF.md) | simple | P1 | 通用 Harness/Runtime 与显式能力装配完成；357 项单元、27 项 Worker、89 项集成通过，2 项服务重启测试未启用 |
| 990  | [第 14 课：模型调用可观测与运营治理](990-lesson-14-model-observability/BRIEF.md)            | standard | P1     | OTel、多实例关联、安全导出与平台对比已交付；Docker Langfuse、PG、浏览器、生产镜像及默认端口 smoke 通过，18 项 AC 完成。 |
| 991  | [第 13 课：建立高并发下可靠的模型执行机制](991-lesson-13-reliable-model-execution/BRIEF.md) | standard | P1     | 有界重试、共享并发、执行摘要及平台 11 场景演示已交付；单元/真实 PostgreSQL/浏览器/负载与构建验收通过，容器部署未执行。  |
| 992  | 第 12 课：多模型路由选型：静态绑定、规则路由与动态策略怎么选？（需求目录本地保留，已忽略）  | standard | P1     | 两级意图→字典特征→契约选模→Runtime推理、闲聊专用Flash引导及结构展示已交付；单元/集成/E2E验收通过，效果调优留后续。      |
| 995  | [Web 工程基座与完整界面框架](995-web-management-foundation/BRIEF.md)                        | standard | P1     | 8 表基座与原型完整框架已交付；21 项 AC 验收、代码复评通过，Agent/Memory 业务保留空页。                                  |
| 998  | [初始化业务数据生成与数据库导入工具](998-business-data-seeding-tool/BRIEF.md)               | standard | P1     | 诊断增量与学员单 SQL 完成；retail_v3 已发布，普通角色空库实导及三种子验收通过，旧版保留。                               |
| 982  | [数据连接管理与默认 Retail 接入](982-managed-data-connections/BRIEF.md)                     | standard | P1     | 已支持工作区多条 PostgreSQL/MySQL 连接，原本地 Retail 保留为默认。升级、浏览器、真实数据库和容器验收通过。              |
