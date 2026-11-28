# 第 27 课代码复评

日期：2026-11-27。Profile：`standard`。范围为 970 最新三通道方案的本次工作区实现，需求与 AC 取自 [PRD](../spec/PRD.md)。这是主实施者的自检与修复后复评；业务浏览器与 Tool Eval 另由独立测试执行，结果汇总于[实施与验收记录](../implementation/IMPLEMENTATION.md)。

## 结论

**有条件进入合并评审。** 本次发现的实现缺陷已修复并纳入回归，当前未保留已确认的 P0/P1 代码缺陷。AC-12 的参数合法率与错误恢复率严格提升目标尚未通过，Feature 保持 `review`，暂不按全部 AC 完成结项。历史 Retail 夹具和完整 Sandbox 基础设施故障组的验证范围见实施记录。

## 版本与范围

- 基线：`HEAD 5180841a9a73aea88c94a6f6635b3e40d6016b1b`，对照该提交与当前工作区；本次范围没有 staged 变更。
- 审查包括 `apps/agent`、`apps/web`、生成 HTTP 契约、相关 README/UI 规范，以及本 Feature 新增实施、测试和评审材料。新增未跟踪源码与测试纳入检查。
- 内容指纹保存于 `work/lesson-27/review-fingerprints.json`。未读取其他 Feature 的材料；全局 ROADMAP 仅定点更新 970 行。
- 高风险路径：声明到实际 Dispatcher 的参数契约、授权池到模型可见工具的交集、HITL 参数冻结、SQL 错误事务、子任务边界、流式事实交付、v1/v2 冻结恢复、Goal 后续轮次。

## 修复并关闭的发现

| 优先级 | 触发与影响                                                                                           | 修复位置                                                                                                                                                                                                                 | 复核                                                                                        |
| ------ | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| P1     | 手选工作流/MCP 的 UUID 进入持久 JSON 后，严格 Python 模型校验拒绝字符串；Goal 续轮和历史命令读取失败 | [schemas.py](../../../apps/agent/src/compass_agent/schemas.py) 的 ToolSelection 为 UUID 元素声明文本兼容，数组与其他字段保留严格约束                                                                                     | 新增 JSON 往返/非法 UUID 测试；真实 PG 从失败轮自动续轮，并检查首轮与下一轮 history_command |
| P1     | 换 Worker 后剩余时间变短，预算判断可能把已准备的 work 步骤变成最终回答，造成重放不一致               | [stream_answer_turn.py](../../../apps/agent/src/compass_agent/runtime/stream_answer_turn.py) 将每步阶段决定写入既有私有状态                                                                                              | v3 以新 epoch、20 秒剩余时间重放；先前工具效果仅执行一次                                    |
| P1     | 定时修改在审批后重新补齐版本，可能绕过提案时的乐观锁                                                 | [input_adapter.py](../../../apps/agent/src/compass_agent/tools/input_adapter.py)、[schedule_write/slim.py](../../../apps/agent/src/compass_agent/tools/builtin/schedule_write/slim.py) 冻结派生参数及输入摘要            | PG 真实 wait/decide/新 Journal 执行；并发修改冲突与重复确认只写一次                         |
| P1     | SQL 错列导致 PostgreSQL 事务进入失败状态，后续列建议查询无法完成；多连接探测需要先取当前权限交集     | [database_query](../../../apps/agent/src/compass_agent/tools/builtin/database_query/__init__.py)、[handler.py](../../../apps/agent/src/compass_agent/tools/builtin/database_query/handler.py) 使用保存点并只探测授权连接 | PG 错列 → 获准候选 → 修正成功；元数据聚合与无权路径                                         |
| P1     | 正常交互 Run 由 Scheduler 消费后，旧 text_delta 守卫会吞掉 v3 回答增量                               | [journal.py](../../../apps/agent/src/compass_agent/runtime/journal.py) 按协议和定时身份区分增量发布                                                                                                                      | PG Journal/投影测试及真实 Provider plain/evidence 流式观测                                  |
| P2     | 子任务的 report_to_parent 在组装后追加，遗漏单步槽位/字节预算；Trace 的授权数量也未计入              | [tool_assembly.py](../../../apps/agent/src/compass_agent/context_runtime/tool_assembly.py) 先登记监督器声明，再统一组装                                                                                                  | 新增 32 槽和 64 KiB 两种压力边界，含空工具 identify 阶段                                    |
| P2     | query_metric 返回结果未登记为后续 Python 可用的受权输入                                              | [semantic/tool.py](../../../apps/agent/src/compass_agent/semantic/tool.py) 记录 AuthorizedInput、sourceQueryId 别名和来源复核回调                                                                                        | 查询结果转 Python 正向与跨 Run 拒绝；真实 SQL → Python 另验                                 |
| P2     | 超多/大证据进入 Answer Prompt 会触发模板变量上限                                                     | [stream_answer.py](../../../apps/agent/src/compass_agent/context_runtime/stream_answer.py) 过滤并稳定排序 128 条来源，生成有界预览，保留存储中的完整结果                                                                 | 150 个大结果、多字节内容、反向插入顺序测试；引用 ordinal 稳定                               |
| P2     | 直接模型路由只固定物理 Prompt 文件，遗漏 JSON 内逻辑成员；真实 PG 路由回归失败                       | [routing/service.py](../../../apps/agent/src/compass_agent/routing/service.py) 同时读取 bundled 与 MEMBERS 路径                                                                                                          | 7 项 PG 模型集成通过；属于本轮综合回归发现的既有入口问题                                    |

上述问题在迭代中修复；复评按最终实现核对，旧失败日志保留以便追踪。相关旧测试的 Prompt 代次、受保护组织上下文、预算替身字段及受控上游响应已按当前契约调整；验收断言继续检查原业务不变量。

## 契约与测试有效性

- 新声明负责模型输入约束，确定性适配之后继续调用现有域模型、权限、审批和状态机；旧代次使用冻结声明和兼容入参。
- 授权先于场景与用户选择。扩展 ID 只缩小已有授权范围；普通成员、子任务和定时身份的过滤在服务端执行，Trace 对权限排除项只记录数量。
- 流式阶段使用独立 Prompt 和空工具列表；混批 final_answer 有匹配 call ID 的延期回执。接受正文前后复核来源，持久 acceptance 校验正文、引用和附件摘要。
- 真实 PG 测试调用生产 Runtime、Journal、Dispatcher、HITL 和事务服务，模型输出部分使用受控替身；真实模型专项另验决策和流式。测试范围在实施记录中逐项注明。
- A/B/C 使用真实 Provider 和合成工具反馈，固定题集/模型/预算并保留失败分母；诊断循环与完整生产 Run 的覆盖分别记录。单次评测未核销严格提升目标。
- 无新增迁移、跨应用源码导入或生产案例分支；测试评测资产留在 tests，生成契约沿用项目生成入口。

## 剩余条件

1. 处理 AC-12 的两项效果目标：当前基线已为 100%，需要保留原结果并在后续真实实验中增加有代表性的修复样本；如调整验收口径，应由需求负责人明确决定。
2. 若交付门禁要求旧 Retail 固定数据集与 Sandbox 全量故障组一并通过，需提供对应隔离夹具并执行原测试入口；本次结果不覆盖这些未执行切片。
3. 本次未提交或发布。应用上线沿用既有部署与 PromptOps 发布流程，旧冻结运行兼容路径已在本次测试中检查。

## 2026-11-27 共享 Sandbox 增量复评

范围为新共享后端、分配/命令租约、0021 迁移、runner/collector、来源复核、检查入口和配套说明。原有其他未提交改动保留。以最新用户授权与 [共享 Sandbox 补充](../design/SHARED_SANDBOX.md) 复核，当前共享复用交付范围无未处理阻断。

修复了这些通用边界：计算配额不再阻止历史回收；失联创建允许经权威观察收敛，发现远端ID后先持久化；共享文件引入独立执行目录与命令级取消；来源在执行前和交付前复核，失败回执不携带已收集敏感结果；失效环境排空后允许新工作；可变文件不参与仅依赖代码/input的结果复用；检查读取延长环境保护期，普通完成不能缩短该保护期。回收沿已有数据库锁与远端确认路径，没有引入跨服务事务、全局执行串行锁或额外调度系统。

验证包含9组真实PG/OpenSandbox/NFS专项、37个数据库专项、6个真实GPT业务Run、692个Agent单元用例及全项目检查/构建。UI取消未单独计通过，命令取消及子进程行为由真实专项覆盖。临时控制面超时保留unknown并按租约重试；验收使用相应有界等待，未删除确认步骤或放宽隔离约束。私有逐项记录在 `work/shared-sandbox/`。
