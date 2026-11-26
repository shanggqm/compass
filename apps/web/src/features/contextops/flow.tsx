"use client";
import { useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import { UiCodeBlock } from "@/components/ui/code-block";
import {
  compactMs,
  duration,
  num,
  percent,
  phaseShort,
  phases,
  shortId,
  statusText,
  type Flow,
  type FlowNode,
  type S,
} from "./client";

type Lens = "tech" | "biz";
type Scale = "duration" | "turn" | "tokens";
export type Item = {
  key: string;
  node: FlowNode;
  flow: Flow;
  runIndex: number;
  turn: number;
  step: number;
  left: number;
  width: number;
  stage: string;
  tag: [string, string];
  otel: string;
  title: string;
  sub: string;
  divider: string;
};
type Band = {
  key: string;
  stage: string;
  phase: string;
  left: number;
  width: number;
  title: string;
  stat: string;
  tone: "" | "warn" | "danger";
};

const failed = (n: FlowNode) => ["failed", "error"].includes(n.status);
const isContext = (n: FlowNode) => n.id.startsWith("context:");
// Runtime step kinds that reach the trajectory as raw identifiers.
const kindText: Record<string, string> = {
  ctx_history: "历史上下文装配",
  routing_host: "路由与宿主装配",
  input: "用户输入",
  system: "系统提示装配",
  workflow: "工作流执行",
};
const windowCount = (n: FlowNode) => (n.funnel ?? []).find((f) => f.stage === "window")?.count;
const candidate = (n: FlowNode) => (n.funnel ?? []).find((f) => f.stage === "candidate")?.count;

function describe(
  node: FlowNode,
  run: Flow["run"],
): Omit<
  Item,
  "key" | "flow" | "runIndex" | "turn" | "step" | "left" | "width" | "stage" | "divider"
> {
  const state = statusText[node.status] ?? node.status;
  const tokens = node.tokens == null ? "" : ` · ${num(node.tokens, 0)} tok`;
  if (isContext(node)) {
    const secure = node.security_blocked_count > 0 || node.redacted_count > 0;
    const before = candidate(node),
      after = windowCount(node);
    const guard = [
      node.security_blocked_count ? `拦截 ${node.security_blocked_count}` : "",
      node.redacted_count ? `脱敏 ${node.redacted_count}` : "",
    ]
      .filter(Boolean)
      .join(" · ");
    return {
      node,
      tag: secure ? ["安全", "sec"] : ["上下文", "ctx"],
      otel: node.security_blocked_count ? "guardrail" : "chain",
      title:
        `上下文安全与编译：候选 ${before ?? "—"} 条 → 入窗 ${after ?? "—"} 条` +
        (guard ? `（${guard}）` : ""),
      sub: `${secure ? "guardrail" : "chain"} · ${state}${tokens}`,
    };
  }
  if (node.phase === "identify") {
    const label = run.scene_name
      ? `${run.category_name ? `${run.category_name} / ` : ""}${run.scene_name}（${run.scene_id}）`
      : (run.scene_id ?? "未分类");
    return {
      node,
      tag: ["意图", "intent"],
      otel: "generation",
      title: `意图分类命中：${label}`,
      sub: `${node.label} · ${state}`,
    };
  }
  if (node.track === "tool")
    return {
      node,
      tag: ["助手", "asst"],
      otel: "tool",
      title: `调用 ${node.label}${node.status === "empty" ? "（空结果）" : failed(node) ? "（失败）" : ""}`,
      sub: `${phaseShort[node.phase] ?? node.phase} · ${state}${node.error_code ? " · " + node.error_code : ""}`,
    };
  if (node.track === "model")
    return {
      node,
      tag: ["助手", "asst"],
      otel: node.label === "模型调用" ? "generation" : "span",
      title:
        node.label === "模型调用"
          ? node.phase === "answer"
            ? "最终回答生成"
            : "主模型推理"
          : node.label,
      sub: `${phaseShort[node.phase] ?? node.phase} · ${state}${tokens}${node.error_code ? " · " + node.error_code : ""}`,
    };
  return {
    node,
    tag: ["系统", "sys"],
    otel: "span",
    title: kindText[node.label] ?? node.label,
    sub: `${phaseShort[node.phase] ?? node.phase} · ${state}${tokens}`,
  };
}

/** Lay out one or more runs on a shared 0–100% axis; each run owns an equal slot. */
export function layout(flows: Flow[], scale: Scale): Item[] {
  const slot = 100 / Math.max(1, flows.length);
  return flows.flatMap((flow, runIndex) => {
    const nodes = flow.nodes;
    if (!nodes.length) return [];
    const start = Math.min(...nodes.map((n) => Date.parse(n.started_at)));
    const end = Math.max(
      ...nodes.map((n) => Date.parse(n.started_at) + (n.duration_ms ?? 0)),
      start + 1,
    );
    const tokenTotal = Math.max(
      1,
      nodes.reduce((s, n) => s + (n.tokens ?? 0), 0),
    );
    let before = 0,
      turn = 1,
      seen = false,
      step = 0;
    return nodes.map((node, index) => {
      if (flows.length === 1 && node.track === "model" && node.label === "模型调用") {
        if (seen) {
          turn += 1;
          step = 0;
        }
        seen = true;
      }
      step += 1;
      const [left, width] =
        scale === "duration"
          ? [
              (Date.parse(node.started_at) - start) / (end - start),
              (node.duration_ms ?? 0) / (end - start),
            ]
          : scale === "turn"
            ? [index / nodes.length, 1 / nodes.length]
            : [before / tokenTotal, (node.tokens ?? 0) / tokenTotal];
      before += node.tokens ?? 0;
      const base = runIndex * slot;
      const position = Math.min(base + slot - 0.8, base + left * slot);
      const turnNumber = flows.length === 1 ? turn : runIndex + 1;
      return {
        ...describe(node, flow.run),
        key: `${runIndex}:${node.id}`,
        flow,
        runIndex,
        turn: turnNumber,
        step,
        left: position,
        width: Math.max(0.8, Math.min(width * slot, base + slot - position)),
        stage: `${runIndex}:${node.phase}`,
        divider:
          flows.length === 1
            ? `第 ${turnNumber} 轮`
            : `第 ${turnNumber} 轮 · 运行 ${runIndex + 1}（${shortId(flow.run.run_id)}）`,
      };
    });
  });
}

function bands(items: Item[], multi: boolean): Band[] {
  const ordered = [...items].sort((a, b) => a.left - b.left);
  const groups: Item[][] = [];
  for (const item of ordered) {
    const last = groups.at(-1);
    if (last && last[0].stage === item.stage) last.push(item);
    else groups.push([item]);
  }
  return groups.map((group, index) => {
    const head = group[0];
    const left = Math.min(...group.map((i) => i.left));
    const right = Math.max(...group.map((i) => i.left + i.width));
    const nodes = group.map((i) => i.node);
    const time = nodes.reduce((s, n) => s + (n.duration_ms ?? 0), 0);
    const tokens = nodes.reduce((s, n) => s + (n.tokens ?? 0), 0);
    const tools = nodes.filter((n) => n.track === "tool");
    const compile = nodes.find(isContext);
    const empty = nodes.filter((n) => n.status === "empty").length;
    const blocked = nodes.reduce((s, n) => s + n.security_blocked_count, 0);
    const phase = head.node.phase;
    const stat =
      phase === "research"
        ? `${tools.length} 次工具${empty ? ` · 空结果 ${empty}` : ""}`
        : phase === "context" && compile
          ? `${candidate(compile) ?? "—"}→${windowCount(compile) ?? "—"}${blocked ? ` · 拦截 ${blocked}` : ""}`
          : phase === "answer"
            ? `${nodes.filter((n) => n.track === "model").length} 次模型${tokens ? ` · ${num(tokens, 0)} tok` : ""}`
            : `${nodes.length} 步`;
    return {
      key: `${head.stage}:${index}`,
      stage: head.stage,
      phase,
      left,
      width: Math.max(0.8, right - left),
      title: `${multi ? `R${head.runIndex + 1} ` : ""}${phaseShort[phase] ?? phase} · ${compactMs(time)}`,
      stat,
      tone: nodes.some(failed)
        ? "danger"
        : empty || blocked || nodes.some((n) => n.status === "blocked")
          ? "warn"
          : "",
    };
  });
}

function funnelSummary(node: FlowNode | undefined, multi: number) {
  if (!node)
    return multi > 1
      ? `会话汇总：共 ${multi} 次运行，暂无上下文编译记录`
      : "本次运行暂无上下文编译记录";
  const f = Object.fromEntries((node.funnel ?? []).map((s) => [s.stage, s.count]));
  const candidateTokens = (node.source_breakdown ?? []).reduce((s, x) => s + x.candidate_tokens, 0);
  const selected = (node.source_breakdown ?? []).reduce((s, x) => s + x.selected_tokens, 0);
  const share = (node.source_breakdown ?? [])
    .filter((x) => x.selected_tokens > 0)
    .map((x) => `${x.source} ${Math.round((x.selected_tokens / Math.max(1, selected)) * 100)}%`)
    .join(" / ");
  return (
    <>
      <strong>
        {multi > 1 ? `会话汇总（共 ${multi} 次运行）· 最近一次漏斗：` : "上下文漏斗摘要："}
      </strong>
      候选 <b>{f.candidate ?? "—"} 条</b>
      {candidateTokens ? ` (${num(candidateTokens, 0)} tok)` : ""} → 安全/权限过滤{" "}
      <b>{f.filtered ?? "—"} 条</b> → 排序截断 <b>{f.ranked ?? "—"} 条</b> → 压缩进窗{" "}
      <b>
        {f.window ?? f.compacted ?? "—"} 条
        {node.tokens != null ? ` (${num(node.tokens, 0)} tok)` : ""}
      </b>
      {share ? ` · 窗口占比：${share}` : ""}
    </>
  );
}

const activate = (handler: () => void) => (event: KeyboardEvent) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    handler();
  }
};

export function Trajectory({
  flows,
  selectors,
  administrator,
  onSnapshot,
  onBadcase,
}: {
  flows: Flow[];
  selectors: ReactNode;
  administrator: boolean;
  onSnapshot: (reference: string) => void;
  onBadcase: (node: FlowNode, runId: string) => void;
}) {
  const [lens, setLens] = useState<Lens>("tech"),
    [scale, setScale] = useState<Scale>("duration");
  const [search, setSearch] = useState(""),
    [stage, setStage] = useState("");
  const [selected, setSelected] = useState(""),
    [detail, setDetail] = useState("overview");
  const items = useMemo(() => layout(flows, scale), [flows, scale]);
  const stageBands = useMemo(() => bands(items, flows.length > 1), [items, flows.length]);
  const query = search.trim().toLowerCase();
  const visible = items.filter(
    (i) =>
      (!stage || i.stage === stage) &&
      (!query || `${i.title} ${i.sub} ${i.node.label}`.toLowerCase().includes(query)),
  );
  const current =
    items.find((i) => i.key === selected) ??
    [...items].reverse().find((i) => i.node.snapshot_ref) ??
    items[0];
  const contextItem = current?.node.snapshot_ref
    ? current
    : [...items].reverse().find((i) => i.node.snapshot_ref);
  const latestContext = [...items].reverse().find((i) => isContext(i.node))?.node;
  const flow = current?.flow ?? flows[0];
  const maxMs = Math.max(1, ...visible.map((i) => i.node.duration_ms ?? 0));
  const meta =
    flows.length > 1
      ? `整个会话 · 共 ${flows.length} 次运行 · ${items.length} 步`
      : flow
        ? [
            flow.run.trace_id ? `trace ${flow.run.trace_id.slice(0, 12)}` : null,
            duration(flow.run.duration_ms),
            `${Math.max(1, ...items.map((i) => i.turn))} 轮 · ${items.length} 步`,
          ]
            .filter(Boolean)
            .join(" · ")
        : "";
  return (
    <>
      <div className="co-filter-bar">
        {selectors}
        <div className="co-filter-actions">
          <button
            type="button"
            className="co-btn co-btn-quiet co-btn-sm"
            disabled={!contextItem?.node.snapshot_ref}
            onClick={() =>
              contextItem?.node.snapshot_ref && onSnapshot(contextItem.node.snapshot_ref)
            }
          >
            查看快照
          </button>
          {administrator && (
            <button
              type="button"
              className="co-btn co-btn-secondary co-btn-sm"
              disabled={!contextItem?.node.snapshot_ref}
              onClick={() =>
                contextItem && onBadcase(contextItem.node, contextItem.flow.run.run_id)
              }
            >
              标为问题
            </button>
          )}
          {administrator &&
            (flow?.langfuse_url ? (
              <a
                className="co-btn co-btn-quiet co-btn-sm"
                href={flow.langfuse_url}
                target="_blank"
                rel="noreferrer"
              >
                原始 Trace ↗
              </a>
            ) : (
              <button
                type="button"
                className="co-btn co-btn-quiet co-btn-sm"
                disabled
                title={flow?.langfuse_reason ?? "本次运行未记录可跳转的 Trace"}
              >
                原始 Trace ↗
              </button>
            ))}
        </div>
      </div>
      {flows.some((f) => f.run.historical) && (
        <p className="co-notice" data-tone="warning">
          历史运行尚无 Context 投影，当前展示已有执行记录。
        </p>
      )}
      <div className="co-waterfall-shell">
        <div className="co-wf-toolbar">
          <div className="co-wf-toolbar-left">
            <div className="co-lens" role="group" aria-label="轨道视角">
              {(
                [
                  ["tech", "通用轨"],
                  ["biz", "业务阶段"],
                ] as const
              ).map(([key, text]) => (
                <button
                  key={key}
                  type="button"
                  aria-pressed={lens === key}
                  onClick={() => {
                    setLens(key);
                    setStage("");
                  }}
                >
                  {text}
                </button>
              ))}
            </div>
            <div className="co-wf-modes" role="group" aria-label="瀑布尺度">
              {(
                [
                  ["duration", "时长"],
                  ["turn", "轮次"],
                  ["tokens", "Token"],
                ] as const
              ).map(([key, text]) => (
                <button
                  key={key}
                  type="button"
                  aria-pressed={scale === key}
                  onClick={() => setScale(key)}
                >
                  {text}
                </button>
              ))}
            </div>
          </div>
          <div className="co-wf-toolbar-right">
            <span className="co-run-meta">{meta}</span>
            <input
              type="search"
              aria-label="搜索轨迹"
              placeholder="搜索"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
        </div>
        <div className="co-wf-canvas">
          <div className="co-wf-scroll">
            {lens === "biz" && (
              <div className="co-wf-row co-wf-stage-row">
                <div className="co-wf-label">阶段</div>
                <div className="co-wf-stage-track">
                  {stageBands.map((band) => (
                    <button
                      key={band.key}
                      type="button"
                      className="co-stage-band"
                      data-tone={band.tone || undefined}
                      data-compact={band.width < 9 || undefined}
                      aria-pressed={stage === band.stage}
                      aria-label={`${phases[band.phase] ?? band.phase} 阶段：${band.title}，${band.stat}`}
                      title={`${band.title} · ${band.stat}`}
                      style={{ left: `${band.left}%`, width: `calc(${band.width}% - 2px)` }}
                      onClick={() => setStage(stage === band.stage ? "" : band.stage)}
                    >
                      {band.width >= 9 && (
                        <>
                          <span className="co-stage-title">{band.title}</span>
                          <span className="co-stage-stat">{band.stat}</span>
                        </>
                      )}
                    </button>
                  ))}
                </div>
              </div>
            )}
            <div className="co-wf-tracks">
              {(
                [
                  ["input", "输入"],
                  ["model", "模型"],
                  ["tool", "工具"],
                ] as const
              ).map(([track, text]) => (
                <div className="co-wf-row" key={track}>
                  <div className="co-wf-label">{text}</div>
                  <div className="co-wf-lane">
                    {lens === "biz" &&
                      stageBands.map((band, index) => (
                        <span
                          key={band.key}
                          className="co-wf-stage-bg"
                          data-tint={index % 4}
                          data-active={stage === band.stage || undefined}
                          style={{ left: `${band.left}%`, width: `${band.width}%` }}
                        />
                      ))}
                    {items
                      .filter((i) => i.node.track === track)
                      .map((i) => (
                        <button
                          key={i.key}
                          type="button"
                          className="co-wf-seg"
                          data-track={
                            failed(i.node) || i.node.security_blocked_count > 0 ? "error" : track
                          }
                          data-selected={current?.key === i.key || undefined}
                          data-dim={(stage && i.stage !== stage) || undefined}
                          style={{ left: `${i.left}%`, width: `${i.width}%` }}
                          title={`${i.title} (${compactMs(i.node.duration_ms)})`}
                          aria-label={`${i.title}，${compactMs(i.node.duration_ms)}`}
                          onClick={() => setSelected(i.key)}
                        />
                      ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
          {lens === "biz" && (
            <div className="co-funnel-mini">{funnelSummary(latestContext, flows.length)}</div>
          )}
        </div>
        <div className="co-split">
          <div className="co-traj-list" aria-label="轨迹节点列表">
            {visible.map((item, index) => {
              const divider = item.divider !== visible[index - 1]?.divider ? item.divider : "";
              const ms = item.node.duration_ms ?? 0;
              return (
                <div key={item.key}>
                  {divider && (
                    <div className="co-turn-divider">
                      <span>{divider}</span>
                      <span>{stage ? "已按阶段筛选" : ""}</span>
                    </div>
                  )}
                  <div
                    role="button"
                    tabIndex={0}
                    className="co-traj-row"
                    aria-pressed={current?.key === item.key}
                    onClick={() => setSelected(item.key)}
                    onKeyDown={activate(() => setSelected(item.key))}
                  >
                    <span
                      className="co-traj-bg"
                      data-kind={
                        item.tag[1] === "sec"
                          ? "sec"
                          : item.otel === "tool"
                            ? "tool"
                            : item.otel === "generation"
                              ? "model"
                              : "ctx"
                      }
                      style={{ width: `${Math.max(2, Math.round((ms / maxMs) * 100))}%` }}
                    />
                    <span className="co-tag" data-kind={item.tag[1]}>
                      {item.tag[0]}
                    </span>
                    <span className="co-traj-content">
                      <span className="co-traj-text">{item.title}</span>
                      <span className="co-traj-sub">{item.sub}</span>
                    </span>
                    <span className="co-traj-ms">{compactMs(item.node.duration_ms)}</span>
                  </div>
                </div>
              );
            })}
            {!visible.length && <p className="co-empty">无匹配轨迹</p>}
          </div>
          {current && (
            <Detail
              item={current}
              tab={detail}
              setTab={setDetail}
              administrator={administrator}
              onSnapshot={onSnapshot}
            />
          )}
        </div>
      </div>
    </>
  );
}

function Detail({
  item,
  tab,
  setTab,
  administrator,
  onSnapshot,
}: {
  item: Item;
  tab: string;
  setTab: (tab: string) => void;
  administrator: boolean;
  onSnapshot: (reference: string) => void;
}) {
  const node = item.node;
  const trust = Object.entries(
    (node.decisions ?? []).reduce<Record<string, number>>((acc, d) => {
      acc[d.trust] = (acc[d.trust] ?? 0) + 1;
      return acc;
    }, {}),
  );
  return (
    <section className="co-detail" aria-label="节点详情">
      <div className="co-detail-head">
        <div className="co-detail-head-top">
          <div className="co-detail-title">
            <span className="co-tag" data-kind={item.tag[1]}>
              {item.tag[0]}
            </span>
            <strong>
              第 {item.turn} 轮 · 第 {item.step} 步
            </strong>
          </div>
          <span className="co-badge" data-tone="neutral">
            {item.otel}
          </span>
        </div>
        <div className="co-detail-tabs" role="tablist" aria-label="节点详情标签">
          {(
            [
              ["overview", "概述"],
              ["security", "安全"],
              ["raw", "内容"],
              ["source", "来源"],
            ] as const
          ).map(([key, text]) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={tab === key}
              onClick={() => setTab(key)}
            >
              {text}
            </button>
          ))}
        </div>
      </div>
      <div className="co-detail-body" role="tabpanel">
        {tab === "overview" && (
          <>
            <dl className="co-kv">
              <dt>业务阶段</dt>
              <dd>{phases[node.phase] ?? node.phase}</dd>
              <dt>状态</dt>
              <dd>{statusText[node.status] ?? node.status}</dd>
              <dt>耗时</dt>
              <dd>{duration(node.duration_ms)}</dd>
              <dt>Token</dt>
              <dd>{node.tokens == null ? "—" : `${num(node.tokens, 0)} tok`}</dd>
              {node.policy_digest && (
                <>
                  <dt>策略</dt>
                  <dd className="co-mono">{node.policy_digest.slice(0, 12)}</dd>
                </>
              )}
              {node.prompt_generation != null && (
                <>
                  <dt>Prompt</dt>
                  <dd>#{node.prompt_generation}</dd>
                </>
              )}
              {node.semantic_version && (
                <>
                  <dt>语义版本</dt>
                  <dd className="co-mono">{node.semantic_version}</dd>
                </>
              )}
              {node.error_code && (
                <>
                  <dt>错误</dt>
                  <dd className="co-mono">{node.error_code}</dd>
                </>
              )}
            </dl>
            <p className="co-detail-desc">{item.title}</p>
          </>
        )}
        {tab === "security" && (
          <>
            <div className="co-notice">
              安全拦截 {node.security_blocked_count} 条 · 脱敏 {node.redacted_count} 处
              {trust.length ? ` · 信任分布：${trust.map(([k, v]) => `${k} ${v}`).join(" / ")}` : ""}
            </div>
            <div className="co-decision-list">
              {(node.decisions ?? []).map((d) => (
                <article className="co-decision-card" key={d.item_id}>
                  <div className="co-decision-head">
                    <span className="co-decision-id">{d.item_id}</span>
                    <span
                      className="co-badge"
                      data-tone={d.decision === "selected" ? "ok" : "warn"}
                    >
                      {d.decision === "selected" ? "入窗" : "淘汰"}
                    </span>
                    <span className="co-badge" data-tone="neutral">
                      {d.trust}
                    </span>
                  </div>
                  <div className="co-decision-reason">{d.reason}</div>
                  <div className="co-decision-meta">
                    <span>{d.kind}</span>
                    <span>{num(d.byte_bound, 0)} B 预算上界</span>
                  </div>
                </article>
              ))}
              {!(node.decisions ?? []).length && <p className="co-empty">该节点未记录上下文决策</p>}
            </div>
          </>
        )}
        {tab === "raw" && (
          <>
            <div className="co-notice">
              正文只保存在加密快照中，读取前实时复核权限并写入审计；此处不缓存明文。
            </div>
            {node.snapshot_ref ? (
              <button
                type="button"
                className="co-btn co-btn-secondary co-btn-sm"
                onClick={() => onSnapshot(node.snapshot_ref!)}
              >
                鉴权查看快照
              </button>
            ) : (
              <p className="co-empty">此节点没有可查看的快照。</p>
            )}
          </>
        )}
        {tab === "source" && (
          <>
            <dl className="co-kv">
              <dt>来源标识</dt>
              <dd className="co-mono">{node.snapshot_ref ?? `${node.track}:${node.label}`}</dd>
              <dt>当前视角</dt>
              <dd>{administrator ? "管理员全局审计" : "本人会话权限"}</dd>
              {!!(node.source_breakdown ?? []).length && (
                <>
                  <dt>入窗占比</dt>
                  <dd>
                    {percent(
                      (node.source_breakdown ?? []).reduce((s, x) => s + x.selected_tokens, 0) /
                        Math.max(
                          1,
                          (node.source_breakdown ?? []).reduce((s, x) => s + x.candidate_tokens, 0),
                        ),
                    )}
                  </dd>
                </>
              )}
            </dl>
            <SourceBars sources={node.source_breakdown ?? []} />
            {!!(node.decisions ?? []).length && (
              <UiCodeBlock
                title="来源与权限约束"
                language="json"
                code={JSON.stringify(
                  (node.decisions ?? []).map((d) => ({
                    item: d.item_id,
                    trust: d.trust,
                    source: d.source,
                    access: d.access,
                  })),
                  null,
                  2,
                )}
              />
            )}
          </>
        )}
      </div>
    </section>
  );
}

export function SourceBars({ sources }: { sources: S["ContextSourceShare"][] }) {
  const max = Math.max(1, ...sources.map((s) => s.candidate_tokens));
  return sources.length ? (
    <div className="co-bar-list co-bar-list-compact">
      {sources.map((s) => (
        <div className="co-bar-row" key={s.source}>
          <div className="co-bar-row-head">
            <strong>{s.source}</strong>
            <span>
              {num(s.selected_tokens, 0)} / {num(s.candidate_tokens, 0)} tok
            </span>
          </div>
          <div className="co-bar-track">
            <div className="co-bar-fill" style={{ width: `${(s.selected_tokens / max) * 100}%` }} />
          </div>
        </div>
      ))}
    </div>
  ) : null;
}
