"use client";

import { sourceAccessNotice } from "@/contracts/source-access";
import Image from "next/image";
import { memo, useEffect, useLayoutEffect, useMemo, useState } from "react";
import { Download, FileSpreadsheet, Sparkles } from "lucide-react";
import { AnalysisMarkdown } from "./analysis-markdown";
import type {
  AnalysisFileView,
  AnalysisRunScope,
  AnalysisToolView,
  AnalysisTurnView,
} from "@/client/analysis-run";
import type { RunView } from "@/contracts/agent-response";
import { CitedAnswer } from "./cited-answer";
import { DurableRunActions } from "./durable-run-actions";
import { WorkflowSaveCard } from "./workflow-save-card";
import { withDatabaseQuerySummary } from "./ui/database-query-summary";
import {
  executionEntries,
  UiExecutionTimeline,
  type ExecutionEntry,
} from "./ui/execution-timeline";

type ReadyFile = Extract<AnalysisFileView, { status: "ready" }>;

const runErrors: Record<string, string> = {
  input_token_limit_exceeded: "模型拒绝了超出输入或上下文窗口限制的请求。",
  output_token_limit_exceeded: "模型拒绝了请求的输出 token 上限。",
  invalid_model_input: "模型拒绝了本次请求的输入或生成参数。",
  model_rate_limited: "上游模型限流，请稍后重试或更换模型。",
  runtime_overloaded: "模型调用容量已满，请稍后重试。",
  run_budget_exhausted: "本次运行预算已耗尽，请核对分析范围后重试。",
  model_failed: "模型调用失败，本轮分析未完成。",
  model_incomplete: "模型返回结果不完整，本轮分析未完成。",
  model_unavailable: "所选模型当前不可用，请重新选择后再试。",
  run_timeout: "本轮分析已超时，请缩小问题范围后重试。",
  workflow_unavailable: "当前工作区或模型无法启动工作流。",
  workflow_failed: "工作流执行未完成，请展开分析过程查看进度后重试。",
  tool_catalog_limit: "本次任务的工具目录超过上限，请减少启用的 MCP 工具后重试。",
  tool_setup_failed: "工具加载失败，请检查必需的 MCP 连接及工具配置后重试。",
  context_limit: "当前对话上下文过长，请新建对话后重试。",
  step_limit: "本轮已达到最大分析轮数，未能形成有效结论。",
  internal_error: "Agent 执行遇到异常，请稍后重试。",
};

const toolErrors: Record<string, string> = {
  unknown_tool: "模型请求了未开放的工具。",
  invalid_parameters: "查询参数不符合当前数据范围。",
  retail_unavailable: "当前工作区未开放 Retail 查询。",
  retail_query_failed: "Retail 查询失败。",
  database_query_failed: "SQL 查询失败，请检查表名、字段和查询权限。",
  retail_query_timeout: "Retail 查询超时。",
  result_limit: "查询结果超过本轮限制。",
};

function processLabel(turn: AnalysisTurnView) {
  switch (turn.terminal?.status) {
    case "completed":
      return "已完成";
    case "failed":
      return "执行失败";
    case "canceled":
      return "已终止";
    case "waiting":
      return "等待确认";
    case "cleanup_unconfirmed":
      return "清理待确认";
  }
  if (turn.errorMessage) return "结果待确认";
  if (turn.stopState === "unconfirmed") return "终止请求未确认";
  if (turn.stopState === "already_terminal") return "已结束，等待结果";
  if (turn.cancellationBlocked) return "终止待核对";
  if (turn.stopState !== "idle") return "正在终止";
  switch (turn.runStatus) {
    case "created":
      return "等待执行";
    case "paused":
      return "已暂停";
    case "waiting":
      return "等待确认";
    case "cancelling":
      return "正在终止";
  }
  return turn.phase === "running" ? "正在分析" : "结果待确认";
}

function dateRange(period: { start: string; end: string }) {
  return `${period.start} 至 ${period.end}`;
}

function toolTitle(tool: AnalysisToolView) {
  if (tool.toolKind === "database_query") return "执行查询";
  if (!tool.params) return "检查查询请求";
  return tool.params.dimension === "channel"
    ? "比较渠道实付金额"
    : `查询 ${tool.params.channelCode} 渠道商品明细`;
}

function resultText(tool: AnalysisToolView) {
  if (tool.status === "running") return "查询正在执行。";
  if (tool.status === "ok") return `返回 ${tool.rowCount ?? 0} 条记录。`;
  if (tool.status === "empty") return "查询成功，没有匹配记录。";
  if (tool.status === "error") {
    if (tool.toolKind === "database_query" && tool.errorCode === "invalid_parameters")
      return "请使用当前连接可访问的表，并提交一条受支持的只读 SELECT。";
    return (
      sourceAccessNotice(tool.errorCode) ?? toolErrors[tool.errorCode ?? ""] ?? "工具调用失败。"
    );
  }
  if (tool.status === "canceled") return "本次调用已停止。";
  return "尚未确认本次调用结果。";
}

function markdownPrefix(source: string, end: number) {
  if (end === source.length) return source;
  const lineStart = source.lastIndexOf("\n", end - 1) + 1;
  const lineEnd = source.indexOf("\n", end);
  const line = source.slice(lineStart, lineEnd < 0 ? source.length : lineEnd);
  // Keep a Markdown table row intact, including the initial header and separator.
  if (line.trimStart().startsWith("|")) {
    if (/^\s*\|[\s:|-]+$/.test(line) && lineStart > 0)
      return source.slice(0, source.lastIndexOf("\n", lineStart - 2) + 1);
    return source.slice(0, lineStart);
  }
  return source.slice(0, end);
}

function StreamingAnswer({ source, onReveal }: { source: string; onReveal: () => void }) {
  const [visible, setVisible] = useState(() =>
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
      ? source
      : "",
  );

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const characters = Array.from(source);
    const duration = Math.min(1800, Math.max(280, characters.length * 4));
    const interval = source.length < 4000 ? 16 : 32;
    let start: number | undefined;
    let previous = -Infinity;
    let cursor = 0;
    let end = 0;
    let frame = requestAnimationFrame(tick);
    function tick(now: number) {
      start ??= now;
      const progress = Math.min(1, (now - start) / duration);
      if (progress === 1 || now - previous >= interval) {
        const target = Math.min(
          characters.length,
          Math.max(1, Math.ceil(characters.length * progress)),
        );
        while (cursor < target) end += characters[cursor++].length;
        setVisible(markdownPrefix(source, end));
        previous = now;
      }
      if (progress < 1) frame = requestAnimationFrame(tick);
    }
    return () => cancelAnimationFrame(frame);
  }, [source]);

  useLayoutEffect(() => {
    onReveal();
  }, [onReveal, visible]);

  return <AnalysisMarkdown source={visible} revealing={visible !== source} />;
}

const autonomyReasons = new Map<string, string>([
  ["autonomy_disabled", "当前 Agent 尚未启用计划执行，请联系平台管理员。"],
  ["retail_route_unavailable", "当前请求未进入零售分析，请明确要查询的指标和时段。"],
  ["retail_model_tools_unsupported", "所选模型不支持 Retail 工具调用，请更换主模型。"],
  ["retail_connection_unavailable", "当前工作区缺少可用的 Retail 只读连接，请检查数据连接。"],
  ["max_steps", "已达到本轮模型步骤上限。"],
  ["max_tool_calls", "已达到本轮工具调用上限。"],
  ["token_budget", "剩余 Token 额度不足以执行下一次模型调用。"],
  ["cost_budget", "剩余费用额度不足以执行下一次模型调用。"],
  ["deadline", "本轮已达到总时限。"],
  ["execution_timeout", "当前模型调用或零售查询超过执行时限。"],
  ["model_budget_unavailable", "所选模型没有可用的预算档案，请联系平台管理员。"],
  ["repeated_query", "同一范围的查询已达到尝试次数上限。"],
  ["consecutive_failures", "连续多次执行或核验失败。"],
  ["no_progress", "连续多次尝试未取得新的已验证结果。"],
  ["evidence_conflict", "同一查询范围出现冲突，请核对数据后再发起分析。"],
  ["usage_bound_violation", "模型回执超出预算档案的上界，请核对配置后再试。"],
  ["unverified_answer", "当前结果未完成核验。"],
  ["product_evidence_required", "本次规则要求商品贡献及对应查询证据，本次提交未通过。"],
  ["intent_invalid", "任务识别结果未通过校验。请用一句话写明指标、比较时段和要拆解的维度。"],
  ["intent_unavailable", "任务识别暂时无法完成。请稍后重试。"],
  ["workflow_scope_mismatch", "工作流的查询时段与本轮要求不一致。请核对日期后重试。"],
  ["script_failed", "模型生成的工作流脚本未能执行，请重新生成工作流。"],
  ["workflow_submission_required", "模型尚未提交已核验的工作流结果，请重新运行。"],
  ["invalid_periods", "请选择两个有效且不重叠的分析时段。"],
]);

function displayCent(value: string): string {
  const cents = BigInt(value);
  const absolute = cents < 0n ? -cents : cents;
  return `${cents < 0n ? "-" : ""}${(absolute / 100n).toLocaleString("zh-CN")}.${String(absolute % 100n).padStart(2, "0")} 元`;
}

const failureStages = {
  routing: "任务路由",
  tool_setup: "加载工具与工作流",
  execution: "执行分析",
  verification: "核验结果",
};

function TurnNotice({ turn, retry }: { turn: AnalysisTurnView; retry?: () => void }) {
  let title = "";
  let detail = "";
  let tone: "neutral" | "waiting" | "error" = "neutral";
  if (turn.terminal?.status === "failed") {
    title = turn.autonomy?.outcome === "human_required" ? "需要人工核对" : "本轮分析未完成";
    detail =
      sourceAccessNotice(turn.runtimeFailure?.reasonCode ?? turn.terminal.errorCode) ??
      (turn.autonomy && autonomyReasons.get(turn.autonomy.reason)) ??
      runErrors[turn.terminal.errorCode] ??
      "本轮分析执行失败。";
    if (turn.runtimeFailure?.reasonCode === "database_schema_mismatch")
      detail = "Agent 数据库结构与当前服务版本不一致，请检查数据库迁移。";
    if (turn.runtimeFailure?.reasonCode === "workflow_replay_mismatch")
      detail = "Agent 从检查点恢复时发现执行输入与已记录的输入不一致，已停止本轮执行。";
    tone = "error";
  } else if (turn.terminal?.status === "canceled") {
    title = "任务已终止";
    detail = "服务端已确认在途工作退出和资源清理。已完成的工具过程仍可查看。";
  } else if (turn.terminal?.status === "cleanup_unconfirmed") {
    title = "终止清理失败";
    detail = "任务已停止派发后续步骤，但服务端未能确认全部执行资源已完成清理。";
    tone = "error";
  } else if (turn.errorMessage) {
    title = turn.acknowledgedTerminalStatus
      ? "服务端终态已确认，但结果流不完整"
      : turn.stopState === "idle"
        ? "运行连接已中断"
        : "终止结果待确认";
    detail = turn.errorMessage;
    tone = turn.stopState === "idle" || turn.acknowledgedTerminalStatus ? "error" : "waiting";
  }
  if (!title) return null;
  return (
    <section className="analysis-run-notice" data-tone={tone} role="status">
      <strong>{title}</strong>
      <p>{detail}</p>
      {retry && turn.terminal?.status === "failed" && (
        <button type="button" className="secondary compact" onClick={retry}>
          调整原问题并重试
        </button>
      )}
      {turn.terminal?.status === "failed" && (
        <details>
          <summary>失败详情</summary>
          {turn.runtimeFailure && (
            <p>
              {failureStages[turn.runtimeFailure.stage]} ·{" "}
              <code>{turn.runtimeFailure.exceptionType}</code>
            </p>
          )}
          <p>
            错误码 <code>{turn.runtimeFailure?.reasonCode ?? turn.terminal.errorCode}</code>
          </p>
          {turn.runId && (
            <p>
              运行 ID <code>{turn.runId}</code>
            </p>
          )}
        </details>
      )}
      {turn.terminal?.status === "failed" && Boolean(turn.autonomy?.partial.length) && (
        <ul aria-label="已验证的部分结果">
          {turn.autonomy?.partial.map((item) => (
            <li key={item.queryId}>
              {item.dimension === "channel" ? "渠道汇总" : `商品拆分 · ${item.channelCode}`}
              {" · "} {item.period.start} 至 {item.period.end}（不含）实付{" "}
              {displayCent(item.paidCent)}
              {item.compareTo && item.comparePaidCent !== null && (
                <>
                  ，比较期 {item.compareTo.start} 至 {item.compareTo.end}（不含）实付{" "}
                  {displayCent(item.comparePaidCent)}
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function AnalysisFileCard({ file, onDownload }: { file: ReadyFile; onDownload: () => void }) {
  return (
    <div className="analysis-file-card">
      <span className="analysis-file-icon">
        <FileSpreadsheet size={19} aria-hidden="true" />
      </span>
      <span className="analysis-file-name">
        <strong>{file.filename}</strong>
        <small>
          CSV · {file.rowCount} 条记录 · {Math.max(1, Math.ceil(file.byteLength / 1024))} KiB
        </small>
      </span>
      <button type="button" className="analysis-file-download" onClick={onDownload}>
        <Download size={17} aria-hidden="true" />
        下载
      </button>
    </div>
  );
}

const workflowLabels: Record<string, string> = {
  generated: "本次生成的 JS 工作流",
  reused: "复用已保存的工作流",
  channel_compare: "渠道分析 SubAgent",
  product_breakdown: "商品贡献 SubAgent",
  scope_review: "口径复核 SubAgent",
  preflight: "预算提案预检",
  binding: "确认授权数据范围",
  periods: "核对分析时段",
  evidence: "核对预算提案证据",
  budget_decision: "人工决策",
  "weekly-retail-review@1": "每周经营复盘脚本 v1",
};
function legacyExecutionEntries(turn: AnalysisTurnView, active: boolean): ExecutionEntry[] {
  const workflow = (turn.workflow ?? [])
    .filter((node) => node.key !== "save_offer")
    .map((node): ExecutionEntry => ({
      id: `${node.source}:${node.key}`,
      kind: "workflow",
      title:
        node.key === "reused" && node.asset
          ? `已复用「${node.asset.name}」· v${node.asset.definitionVersion}`
          : (workflowLabels[node.key.split(":")[0]] ?? "工作流步骤"),
      status: !active && node.status === "running" ? "incomplete" : node.status,
      summary: node.detail,
    }));
  const steps = turn.steps.flatMap((step): ExecutionEntry[] => [
    ...(step.status === "completed"
      ? []
      : [
          {
            id: step.stepId,
            kind: "model" as const,
            status: !active && step.status === "running" ? ("incomplete" as const) : step.status,
            title: "",
          },
        ]),
    ...step.tools.map((tool): ExecutionEntry => {
      const entry: ExecutionEntry = {
        id: `${step.stepId}:${tool.toolIndex}`,
        kind: "tool",
        title: toolTitle(tool),
        status:
          tool.status === "ok"
            ? "completed"
            : tool.status === "error"
              ? "failed"
              : !active && tool.status === "running"
                ? "incomplete"
                : tool.status,
        summary: resultText(tool),
        context: [
          ...(tool.params ? [`时段 · ${dateRange(tool.params.period)}`] : []),
          ...(tool.params?.compareTo ? [`对比时段 · ${dateRange(tool.params.compareTo)}`] : []),
        ],
        input: tool.inputPreview,
        output: tool.resultPreview,
        language: "json",
      };
      return tool.toolKind === "database_query"
        ? withDatabaseQuerySummary(entry, tool.rowCount)
        : entry;
    }),
    ...(step.status === "running" ? (step.preparingTools ?? []) : [])
      .filter((index) => !step.tools.some((tool) => tool.toolIndex === index))
      .map((index): ExecutionEntry => ({
        id: `${step.stepId}:${index}`,
        kind: "tool",
        title: `准备工具调用 ${index}`,
        status: active ? "preparing" : "incomplete",
      })),
  ]);
  return [...workflow, ...steps];
}

export const AnalysisRunTurn = memo(function AnalysisRunTurn({
  turn,
  onDownload,
  onReveal,
  scope,
  pluginHref,
  saved = false,
  partialText,
  retry,
  durable,
}: {
  turn: AnalysisTurnView;
  onDownload: (file: ReadyFile) => void;
  onReveal: () => void;
  scope?: AnalysisRunScope;
  pluginHref?: string;
  saved?: boolean;
  partialText?: string;
  retry?: () => void;
  durable?: {
    run: RunView;
    sessionId: string;
    reload: () => void;
    editGoal?: (objective: string) => void;
  };
}) {
  const completed = turn.terminal?.status === "completed" ? turn.terminal : undefined;
  const waiting = turn.terminal?.status === "waiting" ? turn.terminal : undefined;
  const running =
    turn.phase === "running" &&
    !["created", "paused", "waiting", "cancelling"].includes(turn.runStatus ?? "");
  const entries = useMemo(
    () =>
      turn.execution?.length
        ? executionEntries(turn.execution, running)
        : legacyExecutionEntries(turn, running),
    [running, turn],
  );
  const draftId =
    turn.workflowDraft?.draftId ??
    turn.workflow?.find((node) => node.source === "dynamic" && node.key === "save_offer")?.detail;
  const readyFile = completed?.file.status === "ready" ? completed.file : undefined;
  const timeline = {
    entries,
    latestId: turn.latestExecutionId,
    label: processLabel(turn),
    running,
    completed: Boolean(completed),
    submittedAtMs: turn.submittedAtMs,
    durationMs: turn.durationMs,
  };
  const finalMessage =
    durable &&
    Object.values(durable.run.messages).find(
      (m) => m.role === "assistant" && (m.status === "completed" || m.status === "incomplete"),
    );
  const finalBody =
    completed &&
    (saved ? (
      <AnalysisMarkdown source={completed.finalAnswer} />
    ) : (
      <StreamingAnswer source={completed.finalAnswer} onReveal={onReveal} />
    ));
  const answer = (
    <>
      {!completed && partialText && (
        <section
          aria-label={finalMessage?.status === "incomplete" ? "已核实的部分答复" : "正在生成的回答"}
        >
          {finalMessage?.status === "incomplete" &&
          durable &&
          scope &&
          (finalMessage.content.answerMode === "evidence" ||
            (!finalMessage.content.answerMode && /\[\d+\]/.test(partialText))) ? (
            <CitedAnswer
              key={finalMessage.messageId + ":" + finalMessage.revision}
              source={partialText}
              message={finalMessage}
              runId={durable.run.runId}
              scope={scope}
            >
              <AnalysisMarkdown source={partialText} revealing={false} />
            </CitedAnswer>
          ) : (
            <AnalysisMarkdown source={partialText} revealing={false} />
          )}
        </section>
      )}
      {completed && (
        <section className="analysis-final-answer" aria-label="最终回答">
          {finalMessage?.content.completion === "partial" && (
            <p className="text-sm text-muted-foreground">部分回答</p>
          )}
          {finalMessage &&
          durable &&
          scope &&
          (finalMessage.content.answerMode === "evidence" ||
            (!finalMessage.content.answerMode && /\[\d+\]/.test(completed.finalAnswer))) ? (
            <CitedAnswer
              key={
                finalMessage.messageId + ":" + finalMessage.revision + ":" + completed.finalAnswer
              }
              message={finalMessage}
              runId={durable.run.runId}
              scope={scope}
              source={completed.finalAnswer}
            >
              {finalBody}
            </CitedAnswer>
          ) : (
            finalBody
          )}
          {readyFile && (
            <AnalysisFileCard file={readyFile} onDownload={() => onDownload(readyFile)} />
          )}
          {scope &&
            turn.runId &&
            completed.artifacts?.map((artifact) => {
              const query = new URLSearchParams({
                tenantId: scope.tenantId,
                workspaceId: scope.workspaceId,
              });
              const url = `/api/web/v1/analysis/runs/${turn.runId}/artifacts/${artifact.id}?${query}`;
              return (
                <figure className="analysis-chart" key={artifact.id}>
                  <Image src={url} unoptimized width={768} height={512} alt="Python 分析图表" />
                  <figcaption>
                    <span>分析图表 · PNG · {Math.ceil(artifact.byteSize / 1024)} KiB</span>
                    <a href={url} download="chart.png">
                      下载图表
                    </a>
                  </figcaption>
                </figure>
              );
            })}
          {completed.file.status === "unavailable" && (
            <p className="analysis-file-warning" role="status">
              {completed.file.code === "file_limit"
                ? "CSV 超过本轮交付限制，分析结论已保留。"
                : "CSV 生成失败，分析结论已保留。"}
            </p>
          )}
          {draftId && scope && (
            <WorkflowSaveCard
              key={draftId}
              draftId={draftId}
              persisted={turn.workflowDraft}
              scope={scope}
              pluginHref={pluginHref}
            />
          )}
        </section>
      )}

      {waiting && (
        <section className="analysis-final-answer" aria-label="等待人工决策">
          <strong>等待人工决策</strong>
          <p>
            {"deltaCent" in waiting.proposal
              ? `广告预算调整提案 · ${Number(waiting.proposal.deltaCent) / 100} 元`
              : "工具调用待审批"}
          </p>
          <p>提案编号 {waiting.proposal.proposalId}</p>
          <StreamingAnswer source={waiting.proposal.summary} onReveal={onReveal} />
        </section>
      )}
      <TurnNotice turn={turn} retry={retry} />
    </>
  );

  return (
    <article className="model-chat-turn" data-run-id={turn.runId}>
      <p className="model-chat-question">{turn.question}</p>
      <div className="model-chat-answer analysis-run-answer">
        <header className="analysis-run-header">
          <span className="analysis-run-icon">
            <Sparkles size={18} aria-hidden="true" />
          </span>
          <strong>Compass</strong>
        </header>
        {durable && scope ? (
          <DurableRunActions {...durable} scope={scope} timeline={timeline}>
            {answer}
          </DurableRunActions>
        ) : (
          <>
            <UiExecutionTimeline {...timeline} />
            {answer}
          </>
        )}
      </div>
    </article>
  );
});
