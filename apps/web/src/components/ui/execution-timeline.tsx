"use client";

import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import {
  BookOpen,
  Bot,
  BrainCircuit,
  ChevronDown,
  ChevronRight,
  Database,
  GitFork,
  LoaderCircle,
  MessageSquareText,
  Plug,
  Terminal,
  Workflow,
  Wrench,
} from "lucide-react";
import { executionActive, type ExecutionNode } from "@/contracts/execution-node";
import { ApiError } from "@/client/request-scope";
import type { ChildTraceLoader } from "@/client/child-trace";
import { AnalysisMarkdown } from "../analysis-markdown";
import { Modal } from "./dialog";
import { UiButton } from "./button";
import { UiCodeBlock } from "./code-block";
import { withDatabaseQuerySummary } from "./database-query-summary";

const kinds = {
  model: { icon: BrainCircuit, label: "思考" },
  narration: { icon: MessageSquareText, label: "说明" },
  tool: { icon: Wrench, label: "工具" },
  workflow: { icon: Workflow, label: "工作流" },
  skill: { icon: BookOpen, label: "技能" },
  subagent: { icon: Bot, label: "子任务" },
  group: { icon: GitFork, label: "执行分组" },
};
const statuses = {
  preparing: "准备中",
  running: "执行中",
  completed: "成功",
  empty: "无数据",
  failed: "失败",
  canceled: "已终止",
  waiting: "待处理",
  incomplete: "结果待确认",
} satisfies Record<ExecutionNode["status"], string>;

export type ExecutionEntry = Pick<ExecutionNode, "id" | "kind" | "status" | "title" | "summary"> & {
  parentId?: string | null;
  childRunId?: string | null;
  deliveryStatus?: string | null;
  revision?: number;
  startedAtMs?: number;
  endedAtMs?: number | null;
  children?: ExecutionEntry[];
  context?: string[];
  input?: string | null;
  output?: string | null;
  queryResult?: string;
  thinking?: string | null;
  narration?: string | null;
  toolType?: "database" | "sandbox" | "mcp";
  language?: string;
};

export function executionEntries(
  nodes: readonly ExecutionNode[],
  active: boolean,
): ExecutionEntry[] {
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const entries = [...nodes]
    .filter(
      (node) =>
        node.kind !== "model" ||
        node.status !== "completed" ||
        node.summary ||
        node.detail?.thinking ||
        node.detail?.narration,
    )
    .sort((left, right) => left.startedAtMs - right.startedAtMs)
    .map((node) => {
      const context: string[] = [];
      const parent = node.parentId ? nodesById.get(node.parentId) : undefined;
      if (parent && parent.kind !== "model" && node.kind !== "model")
        context.push(`所属步骤 · ${parent.title}`);
      if (node.dependsOn?.length)
        context.push(
          `依赖 · ${node.dependsOn.map((id) => nodesById.get(id)?.title ?? "前序步骤").join("、")}`,
        );
      if (node.scheduling) context.push(node.scheduling === "parallel" ? "并行" : "顺序");
      if (node.origin?.kind === "mcp") {
        context.push(`MCP · ${node.origin.serverName ?? node.origin.serverId}`);
      }
      if (node.executor?.kind === "sandbox")
        context.push(`Sandbox${node.executor.runtime ? ` · ${node.executor.runtime}` : ""}`);
      if (node.executor?.kind === "quickjs") context.push("QuickJS");
      if (node.workflow) {
        const { mode, name, version } = node.workflow;
        const label =
          mode === "reused" && name
            ? `已复用「${name}」`
            : { generated: "本次生成", reused: "资产复用", fixed: "固定流程" }[mode];
        context.push(`${label}${version ? ` · v${version}` : ""}`);
      }
      if (node.skill)
        context.push(`v${node.skill.version} · ${node.skill.stage === "load" ? "加载" : "脚本"}`);
      const display = node.display?.policy === "preview" ? node.display : undefined;
      const toolType =
        node.origin?.kind === "mcp"
          ? "mcp"
          : node.executor?.kind === "sandbox"
            ? "sandbox"
            : ["retail_sales", "database_query"].includes(node.origin?.toolName ?? "")
              ? "database"
              : undefined;
      const thinking = node.detail?.thinking;
      const narration = node.detail?.narration;
      const entry: ExecutionEntry = {
        id: node.id,
        parentId: node.parentId,
        childRunId: node.subagent?.childRunId,
        deliveryStatus: node.subagent?.deliveryStatus,
        revision: node.revision,
        startedAtMs: node.startedAtMs,
        endedAtMs: node.endedAtMs,
        kind: node.kind,
        status: !active && executionActive(node) ? "incomplete" : node.status,
        title:
          node.kind === "model"
            ? node.summary
              ? "整理上下文"
              : ""
            : node.origin?.kind === "mcp"
              ? (node.origin.toolName ?? node.title)
              : node.title,
        summary: node.summary,
        context,
        input: node.detail?.input ?? display?.inputPreview,
        output: node.detail?.output ?? display?.outputPreview,
        language: node.detail?.input || node.detail?.output ? "json" : display?.language,
        thinking,
        narration,
        toolType,
      };
      return toolType === "database" && node.origin?.toolName === "database_query"
        ? withDatabaseQuerySummary(entry)
        : entry;
    });
  const byId = new Map(entries.map((entry) => [entry.id, entry]));
  const roots: ExecutionEntry[] = [];
  for (const entry of entries) {
    let ancestor = entry.parentId ? nodesById.get(entry.parentId) : undefined;
    const visited = new Set<string>();
    while (ancestor && !ancestor.subagent?.childRunId && !visited.has(ancestor.id)) {
      visited.add(ancestor.id);
      ancestor = ancestor.parentId ? nodesById.get(ancestor.parentId) : undefined;
    }
    const parent = ancestor ? byId.get(ancestor.id) : undefined;
    if (parent?.childRunId) (parent.children ??= []).push(entry);
    else roots.push(entry);
  }
  return roots;
}

const toolKinds = {
  database: { icon: Database, label: "查询数据" },
  sandbox: { icon: Terminal, label: "运行脚本" },
  mcp: { icon: Plug, label: "调用 MCP" },
};

function entryKind(entry: ExecutionEntry) {
  if (entry.toolType) return toolKinds[entry.toolType];
  if (entry.kind === "model" && !entry.thinking)
    return entry.narration ? kinds.narration : { icon: BrainCircuit, label: "分析" };
  return kinds[entry.kind];
}

function entryText(entry: ExecutionEntry) {
  return entry.thinking ?? entry.narration ?? entry.title;
}

function entryStatus(entry: ExecutionEntry) {
  if (entry.deliveryStatus === "partial") return "部分完成";
  if (entry.deliveryStatus === "blocked") return "受阻";
  return statuses[entry.status];
}

function EntryLine({ entry }: { entry: ExecutionEntry }) {
  const { icon: Icon, label } = entryKind(entry);
  const spinning = entry.status === "running" || entry.status === "preparing";
  const content = entryText(entry);
  return (
    <>
      <Icon size={16} aria-hidden="true" />
      <span
        className={entry.queryResult ? "ui-timeline-text ui-timeline-query" : "ui-timeline-text"}
      >
        <span>
          {label}
          {content && ` · ${content.replace(/\s+/g, " ")}`}
        </span>
        {entry.queryResult && (
          <span className="ui-timeline-query-result">→ {entry.queryResult}</span>
        )}
      </span>
      <span className="ui-timeline-status" data-status={entry.status}>
        {spinning && (
          <LoaderCircle className="analysis-loading-spinner" size={13} aria-hidden="true" />
        )}
        {entryStatus(entry)}
      </span>
    </>
  );
}

function ChildDrawer({
  entry,
  loadChild,
  onClose,
  returnFocus,
}: {
  entry: ExecutionEntry;
  loadChild?: ChildTraceLoader;
  onClose: () => void;
  returnFocus: () => HTMLElement | null;
}) {
  const [snapshot, setSnapshot] = useState<{ root: ExecutionEntry; answer?: string | null }>();
  const [state, setState] = useState<"loading" | "ready" | "error" | "denied">("loading");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!loadChild || !entry.childRunId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const read = async () => {
      try {
        const result = await loadChild(entry.childRunId!, controller.signal);
        if (controller.signal.aborted) return;
        const root = executionEntries(result.nodes, true).find(
          (node) => node.childRunId === entry.childRunId,
        );
        if (!root) throw new Error("Missing child execution");
        setSnapshot({ root, answer: result.answer });
        setState("ready");
      } catch (error) {
        if (controller.signal.aborted) return;
        setSnapshot(undefined);
        setState(
          error instanceof ApiError && [403, 404].includes(error.status) ? "denied" : "error",
        );
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(read, 5000);
      }
    };
    void read();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [loadChild, entry.childRunId, retry]);
  const ready = !loadChild || state === "ready";
  const root = loadChild ? snapshot?.root : entry;
  const answer = loadChild ? snapshot?.answer : entry.output;
  const running = root?.status === "running" || root?.status === "preparing";
  return (
    <Modal
      title={entry.title}
      description="子 Agent · 执行过程与回答"
      open
      placement="right"
      onClose={onClose}
      returnFocus={returnFocus}
    >
      <div className="ui-child-execution">
        {!ready && state === "loading" && <p role="status">正在加载执行过程…</p>}
        {!ready && state === "denied" && <p role="status">此执行过程不可访问</p>}
        {!ready && state === "error" && (
          <div role="alert">
            <p>暂时无法加载执行过程</p>
            <UiButton
              variant="secondary"
              onClick={() => {
                setState("loading");
                setRetry(retry + 1);
              }}
            >
              重试
            </UiButton>
          </div>
        )}
        {ready && root && (
          <>
            <UiExecutionTimeline
              entries={root.children ?? []}
              label={entryStatus(root)}
              running={running}
              completed={root.status === "completed"}
              submittedAtMs={root.startedAtMs}
              durationMs={
                root.endedAtMs != null && root.startedAtMs != null
                  ? Math.max(0, root.endedAtMs - root.startedAtMs)
                  : undefined
              }
              loadChild={loadChild}
              defaultExpanded
            />
            <section className="analysis-final-answer" aria-label="子任务回答">
              <h3>回答</h3>
              {answer ? (
                <AnalysisMarkdown source={answer} />
              ) : (
                <p className="ui-child-answer-empty">
                  {running ? "子任务执行中，回答将在完成后显示。" : "此子任务尚未生成回答。"}
                </p>
              )}
            </section>
          </>
        )}
      </div>
    </Modal>
  );
}

function ChildRow({ entry, loadChild }: { entry: ExecutionEntry; loadChild?: ChildTraceLoader }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  return (
    <li data-node-id={entry.id} data-kind="subagent" data-status={entry.status}>
      <button
        ref={trigger}
        type="button"
        className="ui-timeline-row ui-child-trigger"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
      >
        <EntryLine entry={entry} />
        <ChevronRight size={15} aria-hidden="true" />
      </button>
      {open && (
        <ChildDrawer
          entry={entry}
          loadChild={loadChild}
          onClose={() => setOpen(false)}
          returnFocus={() => {
            const button = trigger.current;
            return button?.closest("[hidden]")
              ? (button
                  .closest(".analysis-process")
                  ?.querySelector<HTMLButtonElement>(".analysis-process-toggle") ?? null)
              : button;
          }}
        />
      )}
    </li>
  );
}

function ExecutionRow({
  entry,
  loadChild,
}: {
  entry: ExecutionEntry;
  loadChild?: ChildTraceLoader;
}) {
  const detailId = useId();
  const [open, setOpen] = useState(false);
  const hasDetail = Boolean(
    entry.thinking ||
    entry.narration ||
    entry.input ||
    entry.output ||
    entry.summary ||
    entry.context?.length,
  );
  if (entry.childRunId) return <ChildRow entry={entry} loadChild={loadChild} />;
  if (!hasDetail) {
    return (
      <li data-node-id={entry.id} data-kind={entry.kind} data-status={entry.status}>
        <div className="ui-timeline-row">
          <EntryLine entry={entry} />
        </div>
      </li>
    );
  }
  return (
    <li data-node-id={entry.id} data-kind={entry.kind} data-status={entry.status}>
      <button
        type="button"
        className="ui-timeline-row"
        aria-expanded={open}
        aria-controls={detailId}
        onClick={() => setOpen(!open)}
      >
        <EntryLine entry={entry} />
        <ChevronDown className="ui-timeline-chevron" size={15} aria-hidden="true" />
      </button>
      <div id={detailId} className="ui-timeline-detail" hidden={!open}>
        {open && (
          <>
            {entry.thinking && <p className="ui-timeline-summary">{entry.thinking}</p>}
            {entry.narration && (
              <p className="ui-timeline-summary">
                {entry.thinking && <span className="ui-timeline-context">说明 · </span>}
                {entry.narration}
              </p>
            )}
            {entry.summary && <p className="ui-timeline-summary">{entry.summary}</p>}
            {entry.context?.map((line, index) => (
              <p key={index} className="ui-timeline-context">
                {line}
              </p>
            ))}
            {entry.input && (
              <UiCodeBlock title="调用入参" language={entry.language} code={entry.input} />
            )}
            {entry.output && (
              <UiCodeBlock title="调用结果" language={entry.language} code={entry.output} />
            )}
          </>
        )}
      </div>
    </li>
  );
}

function durationLabel(ms: number) {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(seconds / 60);
  return `用时 ${minutes ? `${minutes} 分 ` : ""}${seconds % 60} 秒`;
}

export function UiExecutionTimeline({
  entries,
  latestId,
  label,
  running,
  completed,
  submittedAtMs,
  durationMs,
  action,
  loadChild,
  defaultExpanded = false,
}: {
  entries: readonly ExecutionEntry[];
  latestId?: string;
  label: string;
  running: boolean;
  completed: boolean;
  submittedAtMs?: number;
  durationMs?: number;
  action?: ReactNode;
  defaultExpanded?: boolean;
  loadChild?: ChildTraceLoader;
}) {
  const listId = useId();
  const toggle = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLOListElement>(null);
  const followLatest = useRef(true);
  const wasCompleted = useRef(completed);
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [now, setNow] = useState(() => Date.now());
  const ticking = running && submittedAtMs !== undefined;
  const elapsed = ticking ? Math.max(0, now - submittedAtMs) : durationMs;
  const time = elapsed === undefined ? null : durationLabel(elapsed);
  const latest = entries.find((entry) => entry.id === latestId) ?? entries.at(-1);
  const showLatest = running && !expanded && latest;
  const latestLabel = latest
    ? [entryKind(latest).label, latest.title, latest.queryResult, statuses[latest.status]]
        .filter(Boolean)
        .join(" · ")
    : "";

  useEffect(() => {
    if (!ticking) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [ticking]);

  useLayoutEffect(() => {
    const justCompleted = completed && !wasCompleted.current;
    wasCompleted.current = completed;
    if (justCompleted && expanded) {
      if (list.current?.contains(document.activeElement)) toggle.current?.focus();
      setExpanded(false);
    }
  }, [completed, expanded]);

  useLayoutEffect(() => {
    const element = list.current;
    if (element && running && expanded && followLatest.current)
      element.scrollTop = element.scrollHeight;
  }, [entries, expanded, running]);

  return (
    <section className="analysis-process" aria-label="分析过程">
      <span className="sr-only" role="status">
        {running && latest ? latestLabel : label}
      </span>
      <div className="analysis-process-heading">
        {entries.length ? (
          <button
            ref={toggle}
            type="button"
            className="analysis-process-toggle"
            aria-expanded={expanded}
            aria-controls={listId}
            aria-label={`${label}${time ? `，${time}` : ""}${showLatest ? ` · ${latestLabel}` : ""}`}
            onClick={() => {
              if (!expanded) followLatest.current = true;
              setExpanded(!expanded);
            }}
          >
            <ChevronDown className="ui-timeline-chevron" size={16} aria-hidden="true" />
            {showLatest ? (
              <>
                <EntryLine entry={latest} />
                <span className="ui-timeline-time">
                  {latest.status !== "running" && latest.status !== "preparing" && (
                    <LoaderCircle
                      className="analysis-loading-spinner"
                      size={13}
                      aria-hidden="true"
                    />
                  )}
                  {time}
                </span>
              </>
            ) : (
              <>
                {running && (
                  <LoaderCircle className="analysis-loading-spinner" size={15} aria-hidden="true" />
                )}
                <span>
                  {label}
                  {time && `，${time}`}
                </span>
              </>
            )}
          </button>
        ) : (
          <div className="analysis-process-summary">
            {running && (
              <LoaderCircle className="analysis-loading-spinner" size={15} aria-hidden="true" />
            )}
            <span>
              {label}
              {time && `，${time}`}
            </span>
          </div>
        )}
        {action}
      </div>
      {entries.length > 0 && (
        <ol
          id={listId}
          ref={list}
          className="ui-execution-timeline"
          aria-label="执行过程"
          hidden={!expanded}
          onScroll={(event) => {
            const node = event.currentTarget;
            followLatest.current = node.scrollHeight - node.scrollTop - node.clientHeight < 24;
          }}
        >
          {entries.map((entry) => (
            <ExecutionRow key={entry.id} entry={entry} loadChild={loadChild} />
          ))}
        </ol>
      )}
    </section>
  );
}
