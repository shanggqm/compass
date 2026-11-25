# 第 25 课三种上下文策略离线验收

- 教学目标：在同一月度复盘材料和同一输入上限下，观察固定窗口、阶段摘要、目标与账本三种装配方式的事实保留、补查及上下文开销。
- 负责人：Compass Feature 983 实施与验收维护者。
- 依赖：仓库 uv 环境、PostgreSQL 17 专用测试连接；复用 Agent 的 `targets`、`data_source` 隔离夹具。
- 必要性：将 AC-05 的策略对照留在独立离线入口；生产 Runtime 不读取本目录。
- 退出条件：课程对照结束后删除；若纳入长期评测，迁入正式 EvalOps 需求并重新评审。

固定窗口调用生产 `pipeline.fit`；阶段摘要调用生产 `ContextOptimizer`、`select_history`、`summarize`；目标策略在相同优化器之前装配真实 PostgreSQL Goal、Ledger 和 `anchor`。策略只对应现有优化/目标开关，生产代码没有案例分支。

模型适配使用确定性 fixture：选择器保守地保留全部候选；摘要器从实际输入提取显式事实标记并合并先前摘要。主回答用相同的“缺少销售事实则补查”规则读取真实受限 PostgreSQL。它用于检查装配、持久化和数据保留机制，报告不评判真实模型的推理质量。

指标口径：完成率为一次任务最终六项验收事实齐备的比例；关键事实保留为每步已装配输入中六个标记的覆盖率；重复查询为首次读取后的额外物理 SQL 次数；Token 列记录与生产窗口估算相同的 UTF-8 字节保守上界，报告同时标注模型真实计费用量未适用。

从 Compass 根目录运行：

```bash
uv run --env-file work/983-postgres-test.env --locked --package compass-agent python examples/showcases/lesson25_context_strategies/runner.py --output work/983-context-strategies.json
```

入口创建并清理专用 `compass_agent_test_*` 临时库，不读取日常数据库，不调用外部模型。报告保存在私有 `work/`，包含每一步压缩、预算、摘要覆盖及目标锚点断言。

## 分析首页的真实模型案例

「第 25 课 · 长任务与多 Agent 协作」提供两个普通 Chat 预填问题：

- **经营变化归因**：比较 2025 年 11 月与 10 月实付金额，定位渠道和商品贡献，调查相关因素并交付渠道变化贡献图。
- **从经营诊断到行动方案**：确认目标后，持续完成三个重点问题的排序、归因调查及未来两周的经营行动安排。

预填真源位于 `apps/agent/src/compass_agent/prompts/support/starters/analysis/operating-attribution.md` 与 `operating-action-plan.md`，经 PromptOps 发布后在新建分析首页显示。点击将完整问题填入输入框，由用户发送；案例使用当前工作区授权的 Retail 数据、模型和通用工具。第二个案例需用户确认目标后继续。

运行前应具备 2025 年 10–11 月的 Retail 样本数据、相应数据连接授权与可用生成模型。首页业务验收与上述离线策略对照分别记录；本次首页验收见 [Showcase 验收记录](../../../mai-specs/983-lesson-25-long-task-context/test/SHOWCASE_REPORT.md)。
