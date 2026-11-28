# Agent 局部规则

修改 `apps/agent/` 下的工具、契约（ACI）、运行时装配或 PromptOps 工具资产前，必须阅读本文件并遵循 [工具目录与注册规范](src/compass_agent/tools/README.md)。

## 1. 工具契约（ACI）与输入适配红线

- **目录与唯一权威源**：通用工具放入 `tools/builtin/<tool_name>/`，业务工具放入 `tools/business/<tool_name>/`，目录名与模型可见工具名完全一致；每个静态工具导出 `SPEC: ToolSpec` 并登记到 `tools/catalog.py`，模型声明、预检与执行注册共用启用条件，禁止在 Runtime 散落追加工具分支。
- **单一通用原语，拒绝复合状态机**：优先采用 SQL、全量幂等数组、自由流式正文等通用原语；禁止为单步操作硬拆 `op`/`operation` 多子命令状态机，禁止要求模型双份记账（如同时填写重叠状态与明细）。
- **机械参数一律代码下沉**：凡服务端已知或可推导的字段——数据库连接 `connectionId`、幂等键 `request_key`、版本号 `baseVersion`/`expectedRevision`、时区 `timezone`、模型路由、组织范围、受理时点 `as_of` 及子任务继承权限——一律由服务端代码确定性处理，禁止暴露到模型 Schema。
- **宽进严出扁平 Schema**：模型可见参数 Schema 保持扁平，清零 `$defs`、`"title"`、`anyOf: [T, null]` 与易碎正则（如强卡英文或固定编号格式）；参数级 `description` 覆盖率须达 `100%`。
- **顶层与参数说明分工**：顶层 `description` 只写用途、触发与禁调边界（含预注入上下文短路规则、主备优先级）及最佳实践，禁止混入返回值算法长文或后端权限/事务内幕；参数 `description` 就近写明格式、单位、上下游句柄对应与体积上限。新资产描述与示例总长 `<= 1200` 字符、示例 `0..3` 条。
- **外简内全无损适配**：契约瘦身仅收窄模型可见界面，服务端通过确定性适配层无损对接既有状态机、乐观锁、引用/附件交付与历史检查点回放；查询结果保持 `resultId` / `sourceQueryId` 与元数据 `kind="metadata"` 兼容。
- **可行动结构化报错**：预检、准入与执行失败统一返回有界 `ActionableToolError`（`<= 4 KiB`，候选 `<= 8` 项，含 `code`/`category`/`field`/`candidates`/`nextAction`）；PostgreSQL 错列先经 `SAVEPOINT` 恢复事务态再仅投影当前账号具备 `SELECT` 权限的可见列，严禁折叠为无定位裸错误码或泄露未授权元数据。

## 2. 工具三层治理与运行时装配红线

- **三层 `ToolSpec` 解耦**：模型可见声明、Runtime 执行适配与策略（`ToolPolicy`）、服务端权威 `ToolGovernance`（`channel`、`pack`、权限与身份限制）分层维护；`runtime/`、`harness/` 与 `tools/builtin/` 禁止导入 `tools/business/` 或 `examples.*`，禁止硬编码课程案例 ID、固定日期或问题关键词分支。
- **硬权限先于一切通道**：每步装配必须先按租户开关、成员权限（如 `semantic_maintainer`）、连接授权、Sandbox 状态及调用身份构建授权池；前端 `RunConstraints.toolSelection` 严格与授权池取硬交集。`SubAgent` 仅继承父级可见的只读、无审批、非编排工具；定时 Run 永久剔除需人工确认的写工具。
- **三通道装配与容量解耦**：按 `always_on`（核心高频底座）+ `user_selected`（显式勾选的高风险/低频/扩展能力及合理场景兜底）+ `scene_routed`（复杂分析按需增强）合流；分离 Run 授权注册池上限（`128`）与单步模型可见上限（`<= 32` 项且 `<= 64 KiB`，手选扩展 `<= 12` 项），未勾选的外部 MCP 在新 Run 启动期跳过远端探测。
- **恒定前缀与跨版本恢复**：在 `work` 入口一次性冻结动态标志，工作循环内 `tools[]` 字节序列保持恒定以保护 Prefix Cache；预算收口仅留 `final_answer`，流式回答阶段清空工具；协议升级与跨进程恢复须保留冻结快照兼容，避免存量 Run 触发 `tool_catalog_changed`。

## 3. 变更必跑验证

- 工具目录、契约或装配改动：`pnpm lint -F agent`、`pnpm typecheck -F agent`、`pnpm test -F agent`（覆盖 `test_tool_catalog.py`、`test_showcase_isolation.py` 及相关行为用例）。
- 修改 Agent HTTP DTO 或路由后：执行 `pnpm contracts` 与 `pnpm contracts:check`。
- 涉及工具选择准确率、参数合法率或声明 Token 开销调整：同步维护并运行 `apps/agent/tests/tool_eval/` 消融评测，用实测数据验证收益。
