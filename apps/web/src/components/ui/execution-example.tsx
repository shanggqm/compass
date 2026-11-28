"use client";

import { useState } from "react";
import type { ExecutionNode } from "@/contracts/execution-node";
import { UiButton } from "./button";
import { executionEntries, UiExecutionTimeline } from "./execution-timeline";
import { AnalysisGoalPanel } from "../analysis-goal-panel";
import type { Goal } from "@/client/analysis-goal";

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;

function executionExample(status: ExecutionNode["status"], startedAt: number): ExecutionNode[] {
  const node = (
    n: number,
    kind: ExecutionNode["kind"],
    title: string,
    parent: number | null,
    fields: Partial<ExecutionNode> = {},
  ): ExecutionNode => ({
    id: id(n),
    parentId: parent === null ? null : id(parent),
    revision: 1,
    kind,
    title,
    status: "completed",
    startedAtMs: startedAt,
    endedAtMs: startedAt + 1400,
    dependsOn: [],
    display: { policy: "summary", language: "text" },
    ...fields,
  });
  const state = {
    status,
    endedAtMs: status === "running" || status === "preparing" ? null : startedAt + 4200,
  };
  return [
    node(1, "workflow", "渠道异动归因", null, {
      ...state,
      workflow: { mode: "reused", assetId: id(100), name: "渠道异动归因", version: 1 },
      executor: { kind: "quickjs", runtime: "JavaScript" },
      summary: "复用已保存的流程，按本次日期重新分析。",
    }),
    node(2, "narration", "已确认分析范围", 1, {
      summary: "先比较渠道，再并行分析商品贡献与核对口径。",
    }),
    node(3, "skill", "加载经营复盘方法", 1, {
      skill: { name: "weekly-retail-review", version: "1", stage: "load" },
      summary: "已加载周期选择与核验约束。",
    }),
    node(4, "group", "商品贡献与口径复核", 1, {
      ...state,
      scheduling: "parallel",
      dependsOn: [id(2)],
    }),
    node(5, "subagent", "商品贡献分析", 4, {
      subagent: { name: "product_breakdown", childRunId: id(105), deliveryStatus: "completed" },
      summary: "已取得前三项商品贡献。",
      detail: {
        output:
          "已完成商品贡献分析。\n\n| 商品 | 变化金额 |\n| --- | ---: |\n| 商品 A | +1,200 |\n| 商品 B | +800 |\n| 商品 C | −300 |\n\n三项合计变化 **+1,700**。以上为组件演示数据。",
      },
    }),
    node(6, "model", "分析商品贡献", 5, {
      detail: {
        thinking:
          "先比较各渠道的实付金额变化，再看变化最大的渠道。商品明细按实付差值排序，最后核对汇总金额与渠道结果是否一致。",
      },
    }),
    node(7, "tool", "查询商品明细", 6, {
      origin: {
        kind: "mcp",
        toolName: "查询商品明细",
        serverId: "retail",
        serverName: "Retail 数据服务",
      },
      executor: { kind: "service" },
      summary: "协议示例：返回 3 条记录。",
      display: {
        policy: "preview",
        language: "json",
        inputPreview: '{"channel":"POS","limit":3}',
        outputPreview:
          '{"rowCount":3,"scope":"当前工作区","note":"这里使用组件演示数据，MCP 的真实工具接入在第 18 课完成。"}',
      },
    }),
    node(8, "subagent", "独立口径复核", 4, {
      ...state,
      subagent: {
        name: "scope_review",
        childRunId: id(108),
        deliveryStatus: status === "incomplete" ? "partial" : undefined,
      },
      detail: {
        output:
          status === "completed"
            ? "已核对日期范围与金额变化方向。查询共返回 **128** 笔订单。"
            : undefined,
      },
      summary:
        status === "failed" ? "复核未完成，可检查数据范围后重试。" : "核对日期范围与金额变化方向。",
    }),
    node(9, "skill", "运行金额复核脚本", 8, {
      ...state,
      skill: { name: "retail-verification", version: "1", stage: "script" },
    }),
    node(10, "tool", "执行 Python 复核", 9, {
      ...state,
      origin: { kind: "builtin", toolName: "python_analysis" },
      executor: { kind: "sandbox", runtime: "Python", environmentId: "guide-example" },
      summary: "协议示例：Sandbox 执行环境由第 18 课接入。",
    }),
    node(11, "tool", "查询数据库", 8, {
      ...state,
      origin: { kind: "builtin", toolName: "database_query" },
      detail: {
        input: JSON.stringify({ op: "query", sql: "SELECT COUNT(*) AS orders FROM sales" }),
        output:
          status === "completed"
            ? JSON.stringify({ status: "ok", columns: ["orders"], rows: [[128]], truncated: false })
            : undefined,
      },
    }),
  ];
}

export function UiExecutionExample() {
  const [status, setStatus] = useState<ExecutionNode["status"]>("running");
  const [startedAt] = useState(() => Date.now());
  const [goalStatus, setGoalStatus] = useState<Goal["status"]>("active");
  const [editingGoal, setEditingGoal] = useState(false);
  const variants = [
    ["running", "进行中"],
    ["completed", "成功"],
    ["failed", "失败"],
    ["waiting", "待处理"],
    ["canceled", "取消"],
    ["incomplete", "中断"],
  ] as const;
  return (
    <div className="ui-guide-example">
      <p className="ui-panel-description">
        以下为协议与组件示例。MCP 和 Sandbox 使用演示数据，真实工具执行体系在第 18 课接入。
      </p>
      <div className="ui-actions" role="group" aria-label="切换执行状态">
        {variants.map(([value, label]) => (
          <UiButton
            key={value}
            variant={status === value ? "primary" : "secondary"}
            aria-pressed={status === value}
            onClick={() => setStatus(value)}
          >
            {label}
          </UiButton>
        ))}
      </div>
      <UiExecutionTimeline
        entries={executionEntries(executionExample(status, startedAt), status === "running")}
        label={
          {
            running: "正在分析",
            completed: "已完成",
            failed: "执行失败",
            waiting: "等待确认",
            canceled: "已终止",
            incomplete: "结果待确认",
            preparing: "准备中",
            empty: "无数据",
          }[status]
        }
        running={status === "running"}
        completed={status === "completed"}
        durationMs={status === "completed" ? 138000 : undefined}
      />
      <div className="analysis-goal-example">
        <p>目标进度显示在输入框上方，悬停或点击展开管理；对话中输入 / 可选择 Goal 模式。</p>
        <AnalysisGoalPanel
          goal={{
            goalId: id(200),
            revision: 1,
            status: goalStatus,
            reason: null,
            reasonCode: null,
            contract: {
              objective: "完成月度销售与库存复盘",
              acceptance: [
                { id: "a1", text: "销售结论附来源" },
                { id: "a2", text: "库存结论附来源" },
              ],
              constraints: ["仅查询当前获准数据"],
            },
            budget: { tokens: 2000000, costMicrousd: 20000000, maxRounds: 12 },
            usage: {
              rounds: 2,
              tokens: 10000,
              costMicrousd: 10000,
              tokensPct: 0.5,
              costPct: 0.05,
              overrun: false,
            },
            counts: { openTodos: 1, conflicts: 0, stale: 0, reusedCalls: 1 },
            satisfied: ["a1"],
          }}
          busy={false}
          error=""
          action={async (action) =>
            setGoalStatus(
              action === "pause" ? "paused" : action === "resume" ? "active" : "canceled",
            )
          }
          edit={() => setEditingGoal(true)}
        />
        <label className="ui-field">
          <span>目标模式演示输入</span>
          <textarea
            className="ui-input"
            readOnly
            placeholder="给 Compass 发送消息，输入 / 选择模式"
          />
        </label>
      </div>
      {editingGoal && (
        <label>
          修改目标
          <textarea autoFocus defaultValue="修改目标：" />
        </label>
      )}
    </div>
  );
}
