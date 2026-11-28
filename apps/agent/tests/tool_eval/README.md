# 第 27 课 Tool Eval

从 Compass 根目录执行。本目录属于测试资产，生产源码和公开 Run 请求均不导入它，Python wheel 只打包 `src/compass_agent`。

`dataset.json` 固定 28 题：近义工具 4、预注入短路 3、SQL 元数据与明细 4、参数缺省与修复 5、读写防诱导 3、口径澄清 3、权限 3、48+ 工具池与手动选择 3。题目、上下文、授权、期望工具、参数条件和禁用工具均随数据版本保存。`pool-03` 刻意保留轻度场景误分类，并通过用户选中的 Python 能力完成任务。

## 三组与执行范围

| 组  | 声明与供给                                                   | 错误回执                   |
| --- | ------------------------------------------------------------ | -------------------------- |
| A   | 保留 `baseline_tools.json` 的原始复合 Schema，授权后全量平铺 | 错误码                     |
| B   | 读取当前 `ToolSpec.definition`，授权后全量平铺               | 实际 `ActionableToolError` |
| C   | B 的声明，经实际 `assemble_step` 三通道组装                  | 实际 `ActionableToolError` |

三组共享相同模型配置、题目、授权和合成业务结果。授权过滤保持一致。49 工具题使用预发现池，A/B 全平铺会命中现有 32 工具出口上限，报告保留 `tool_catalog_limit`；C 验证小于 32 的可见集。普通 Run 是否跳过未选择 MCP 的远端探测，由 Runtime 集成用例验证。

声明校验使用当前工具 Schema 与 JSON Schema validator，SQL 语法校验复用实际 `parse_read_sql`。业务执行使用明确标记的合成回执，真实模型负责选择工具、填写参数和根据回执修复。B/C 的 `final_answer` 随后进入一个工具列表为空的回答轮。此处使用一致的诊断提示词与固定数据，不启动完整持久 Run、PromptOps 发布、真实 SQL、Sandbox、审批、流式 SSE 或浏览器；这些路径保留独立集成验收。

指标和段落引用使用新旧契约均接受的 UUID。SQL 合成反馈通过独立内存 SQLite 小表计算，返回实际 SELECT 的列名、分组和数据，支持本题集使用的 PostgreSQL 日期字面量与简单类型转换；不支持的表达式返回明确错误。该内存库只产生模型诊断反馈，应用 PostgreSQL 迁移、权限与真实查询继续由独立集成用例验收。

## 离线执行

```bash
uv run --locked --package compass-agent pytest apps/agent/tests/tool_eval/test_tool_eval_metrics.py -q
uv run --locked --package compass-agent python -m apps.agent.tests.tool_eval.runner \
  --mode offline --out work/tool-eval/lesson27-offline.json
```

默认执行 28 × 3 个组装诊断，写入每组精确工具声明、UTF-8 序列化字节数和组装轨迹。离线报告的 `summaries` 为空。模型效果指标只由 `--mode model` 产生。输出文件已存在时退出，避免覆盖原始报告。

## 真实模型执行

使用已迁移的隔离 Agent PostgreSQL 库，库名必须以 `compass_agent_test_` 开头。通过项目现有 Provider 配置入口在该库中配置并启用一个 LLM Provider，保持模型加密配置、允许的上游地址与连接环境一致。评测入口不复制 Provider 或读取其他应用表。

将隔离环境变量保存在不提交 Git 的环境文件中。文件至少提供该库的 `DATABASE_URL`、Provider 所需的项目模型配置和 `COMPASS_TOOL_EVAL=true`；密钥仅由现有 `SecretStore` 解析。命令行仅接收 Provider ID 或别名：

```bash
uv run --locked --package compass-agent --env-file work/lesson27-tool-eval.env \
  python -m apps.agent.tests.tool_eval.runner \
  --mode model --provider-alias '第27课验收模型' \
  --temperature 0 --max-steps 6 --max-tokens 4096 --repetitions 1 --concurrency 3 \
  --out work/tool-eval/lesson27-model.json
```

先用 `--case near-02 --case repair-03 --group B C` 检查连接和修复路径，再执行完整题集。多次采样使用 `--repetitions 3`；报告逐次计算指标，轮换 A/B/C 次序。同一份对照期间应保持 Provider 绑定版本固定，绑定变化会拒绝后续调用。每个模型轮经实际 `create_model_service → prepare → run(tool_output_validation="agent_dispatch")` 执行，并在隔离 Agent 库记录模型调用。

`--concurrency` 支持 1～3，默认 1；同一道题的各组受限并行，全部完成后进入下一题。可直接从原始 case 记录离线重算，命令不访问 Provider 或数据库：

```bash
uv run --locked --package compass-agent python -m apps.agent.tests.tool_eval.metrics \
  work/tool-eval/lesson27-model.json
```

提供 `--input-usd-per-million` 与 `--output-usd-per-million` 后，费用护栏按实测 token 用量与指定单价估算。两个价格必须同时提供；未提供价格或 Provider 未返回完整用量时，费用为 `null`。报告不表示实际账单金额。

报告保存数据集和基线摘要、样本数、Provider ID、模型名与版本、温度、每轮调用 ID、精确工具声明、模型原始文本与工具调用、诊断回执和用量。模型密钥、Provider 行、完整连接地址及异常字符串不导出。每完成一题就更新报告；中断留下 `status=running` 的部分结果，可直接定位未完成项。

## 指标口径

| 指标                | 分子 / 分母                                                                                                     |
| ------------------- | --------------------------------------------------------------------------------------------------------------- |
| 工具选择准确率      | 调用全部期望工具且未调用禁用工具的题数 / 总题数；无需工具的澄清、拒绝题须达成其验收条件                         |
| 参数合法率          | 通过当前可见声明校验的调用数 / 总调用数；字段失效等业务回执错误单独计入修复                                     |
| 首轮成功率          | 首轮调用成功、命中至少一项期望工具且无禁用调用的题数 / 总题数；后续必要步骤由任务完成率统计                     |
| 冗余调用率          | 重复成功调用相同工具和参数、或成功调用题目禁用工具的次数 / 总调用数；修复重试不算重复成功                       |
| 错误恢复率          | 后续 1–2 个模型工具轮中同一工具成功恢复的错误数 / 可修复错误数；同批并行调用不计恢复，每次成功最多消费一个错误  |
| 工具声明 Token 占比 | 所有请求的声明估计 Token 之和 / 完整提示估计 Token 之和；两侧统一 `ceil(UTF-8 JSON bytes/4)`，Provider 用量另存 |

四个护栏为任务完成率、平均澄清次数、平均耗时、平均费用。完成条件包含成功调用、参数验收条件、收口、必要回答关键词及禁用工具约束。澄清计数采用最终回答提问特征与题目约束，原始轨迹供人工复核；未预期的澄清也计入护栏。耗时覆盖本题组装、模型调用和合成执行，不含排队等待前一题的时间。

调用、修复或 Token 分母为零时返回 `null`。缺失费用或 Token 覆盖时，对应整体聚合保持 `null` 并列出覆盖样本数。任务完成率是自动 rubric 的诊断结果，最终答案质量仍需复核；多个题目要求连续动作，首轮指标与完整任务完成率分别观察。

验收时检查每组原始轨迹、六项主指标和四项护栏的变化。真实模型未执行、工具容量拒绝、模型失败和缺失用量均按报告实际状态登记；确定性测试与离线组装通过情况单独记录。
