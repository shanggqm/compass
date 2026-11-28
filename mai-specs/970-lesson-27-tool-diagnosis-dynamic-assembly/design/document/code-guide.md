## 设计目标与审查重点

本附录汇集第 27 课技术方案的 6 组核心供评审代码设计（状态均为设计示意，待评审通过后落入对应源码与测试目录），覆盖三层 `ToolSpec` 契约、三通道确定性组装器 `ToolAssembler`、Chat 输入框「工具与扩展」手动选择器、确定性连接路由与流式 `final_answer(mode)` 收口、持久恢复兼容及 Tool Eval 六维评估器。

| 模块 | 目标源码路径 | 关键审查不变量 |
| --- | --- | --- |
| M1 三层契约与容量拆分 | `tools/catalog_contracts.py`, `tools/registry.py` | 写入或需审批工具禁止标记 `subagent_allowed=True`；授权池上限 `128` 与单步可见上限 `32` / `64 KiB` 严格分离 |
| M2 三通道确定性组装器 | `tools/assembler.py` | 权限硬过滤位于三通道合流之前；`work` 入口冻结会话标志保证前缀 100% 恒定；11 工具核心底座常驻、`SC-34` 自动补挂 `python_analysis`、`SC-33/37/39` 直通流式输出并产出 `deferred_pool` |
| M3 前端手动选择器与契约 | `schemas.py`, `components/analysis-composer.tsx` | `ToolSelection` 统一挂在 `RunConstraints` 下（跨长任务自动继承）；仅语义维护者可见「指标口径治理」；前后端限制最多手动装载 `12` 个扩展工具 |
| M4 外简内全适配、流式收口与报错 | `tools/schema_compact.py`, `tools/errors.py`, `database_query/__init__.py` | `database_query(sql)` 多连接元数据聚合与 `kind="metadata"` 标记；抢跑 `final_answer` 返回标准 `role="tool"` 延期回执；`build_stream_citations` 兼容 `publish_citations` / `<CitedAnswer>`；`SAVEPOINT` 防 PostgreSQL `25P02` 后投影可见列；`todo_write`/`goal` 外简内全无损映射 |
| M5 按需绑定与跨版本恢复兼容 | `runtime/tool_bindings.py`, `runtime/journal.py` | 未选外部 MCP 跳过启动期探测；恢复时从 `connection_id` / `workflow_<hex>` 还原扩展资产 ID；升级前旧快照按冻结 `declaration` 回退装载不触发 `tool_catalog_changed` |
| M6 评测指标与前端面板 | `tests/tool_eval/metrics.py`, `contextops/panels.tsx` | 零分母指标返回 `None` 拒绝虚构比率；ContextOps 区分 `[常驻]`/`[场景]`/`[手动]` 且隐藏未授权工具名 |

## 三层 ToolSpec 契约与容量解耦注册表

`ToolGovernance` 封装三通道（`always_on`、`scene_routed`、`user_selected`）与十类能力包标签，在 `ToolSpec.register()` 入口校验执行策略与子任务继承标记的一致性，并由 `registry.py` 拆分池级校验与步级可见校验：

{{code:m1_spec}}

{{code:m1_reg}}

## 三通道确定性组装器 ToolAssembler

`assemble_step()` 接收不可变 `AssemblyInput`，依次完成权限/维护者资格/范围/身份裁剪、阶段收敛门禁、`AlwaysOn + UserSelected + SceneRouted` 固定顺序合流以及 `32` 槽位 / `64 KiB` 字节预算保护，并将未挂载工具沉淀为 `deferred_pool` 预留给第 28 课 `ToolSearch`：

{{code:m2_asm}}

## 前端「工具与扩展」手动选择器契约与组件骨架

`ToolSelection` 统一挂在 `RunConstraints` 上（复用 `SessionSubmit.constraints` 并随长任务续跑自动继承），`ToolSelectionBar` 在 Chat 输入框底部提供高阶/写能力组与个人 `workflow_*`、`mcp_*` 勾选交互，并实施维护者可见性与 `12` 项扩展上限守卫：

{{code:m3_dto}}

{{code:m3_ui}}

## 外简内全输入适配、流式循证收口与结构化可自愈报错

`compact_json_schema` 剥离 Pydantic 冗余装饰，`route_connection_for_sql` 支持多连接元数据聚合与 `kind="metadata"` 标记，`partition_terminal_final_answer`、`assemble_ordered_evidences` 与 `build_stream_citations` 保障 `answerProtocol == 3` 下抢跑返回 `role="tool"` 延期回执、排序证据注入 `evidence.md`、`^[1]^` 角标回链至既有 `publish_citations` / `<CitedAnswer>` 并按 `artifactFormat` 复用 `build_artifact`；`adapt_slim_todos` 与 `adapt_slim_goal` 无损对接既有后端状态机；`ActionableToolError` 与 `SAVEPOINT` 防 `25P02` 安全投影可见列候选：

{{code:m4_slim}}

{{code:m4_err}}

{{code:m4_pg}}

## 运行工具绑定与跨版本持久恢复衔接

`bind_extension_entries` 实现未选 MCP 按需探测、从 `connection_id` 与 `workflow_<hex>` 还原资产 ID 以及升级前旧快照按冻结 `declaration` 回退兼容，`restore_tool_selection` 从 `RunConstraints.toolSelection` 还原用户本轮手动勾选状态：

{{code:m5_resume}}

## Tool Eval 六维评估器与 ContextOps 面板组件

`summarize_group` 统计六项核心指标与四项护栏指标；`ToolAssemblySection` 在 Web 端 ContextOps 步骤详情内展示 `[常驻]`、`[场景]`、`[手动]` 三通道装载明细：

{{code:m6_eval}}

{{code:m6_ui}}
