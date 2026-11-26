"use client";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { RefreshCw } from "lucide-react";
import type { PagePayload } from "@/contracts/page";
import { useDocumentSession } from "@/components/shell/session-boundary";
import { Sparkline } from "./charts";
import {
  contextClient,
  duration,
  errorText,
  num,
  percent,
  shortId,
  statusText,
  type Flow,
  type FlowNode,
  type Run,
  type S,
} from "./client";
import { Trajectory } from "./flow";
import {
  AnomalyPanel,
  BadcaseDialog,
  IntentPanel,
  McpDrawer,
  ReplayPanel,
  SecurityPanel,
  SnapshotDialog,
} from "./panels";
import "./contextops.css";

type Tab = "trajectory" | "intent" | "anomalies" | "security" | "replay";
type Overlay =
  | { kind: "snapshot"; reference: string }
  | { kind: "badcase"; node: FlowNode; runId: string }
  | { kind: "mcp" };
const securityKinds = [
  "injection_blocked",
  "injection_stripped",
  "tenant_mismatch_rejected",
  "pii_redacted",
];
const maxSessionRuns = 8;

export function ContextOpsPage({
  payload,
  scope,
}: {
  payload: PagePayload;
  scope: { tenantId: string; workspaceId: string };
}) {
  const session = useDocumentSession(),
    admin = payload.session.isPlatformAdmin;
  const { tenantId, workspaceId } = scope;
  const client = useMemo(
    () => contextClient(session, { tenantId, workspaceId }, admin),
    [session, tenantId, workspaceId, admin],
  );
  const initial = new URL(payload.viewKey, "https://compass.local").searchParams;
  const [tab, setTab] = useState<Tab>("trajectory"),
    [window, setWindow] = useState<"24h" | "7d" | "30d">("7d");
  const [actor, setActor] = useState(""),
    [policy, setPolicy] = useState("");
  const [selectedSession, setSelectedSession] = useState(""),
    [runChoice, setRunChoice] = useState(initial.get("run") ?? initial.get("run_id") ?? "all");
  const [refresh, setRefresh] = useState(0),
    [loading, setLoading] = useState(true),
    [error, setError] = useState("");
  const [runs, setRuns] = useState<Run[]>([]),
    [overview, setOverview] = useState<S["ContextOverview"] | null>(null);
  const [flows, setFlows] = useState<Flow[] | null>(null),
    [flowError, setFlowError] = useState("");
  const [intents, setIntents] = useState<S["ContextIntents"] | null>(null),
    [anomalies, setAnomalies] = useState<S["ContextAnomalies"] | null>(null);
  const [events, setEvents] = useState<S["ContextSecurityEvents"] | null>(null),
    [badcases, setBadcases] = useState<S["ContextBadcases"] | null>(null);
  const [tabLoading, setTabLoading] = useState(false),
    [tabError, setTabError] = useState("");
  const [overlay, setOverlay] = useState<Overlay | null>(null),
    [toast, setToast] = useState<{ text: string; id: number } | null>(null);
  const [eventType, setEventType] = useState(""),
    [root, setRoot] = useState("");
  const [runsCursor, setRunsCursor] = useState<string | null>(null),
    [moreLoading, setMoreLoading] = useState(false);
  const generation = useRef(0);
  const notify = useCallback((text: string) => setToast({ text, id: Date.now() }), []);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 2500);
    return () => clearTimeout(timer);
  }, [toast]);

  const names = useMemo(() => {
    const map = new Map(
      (payload.pageData.members?.items ?? []).map((m) => [m.userId, m.displayName]),
    );
    map.set(payload.session.user.id, payload.session.user.displayName);
    return map;
  }, [payload]);
  const members = useCallback(
    (id: string | null | undefined) => (id ? (names.get(id) ?? `成员 ${id.slice(0, 6)}`) : "—"),
    [names],
  );
  const query = useMemo(
    () => ({
      window,
      ...(actor ? { actor_id: actor } : {}),
      ...(policy ? { policy_digest: policy } : {}),
    }),
    [window, actor, policy],
  );

  useEffect(() => {
    const c = new AbortController();
    generation.current += 1;
    const load = async () => {
      setLoading(true);
      setError("");
      try {
        const [stats, page] = await Promise.all([
          client.get<S["ContextOverview"]>("overview", query, c.signal),
          client.get<S["ContextRuns"]>("runs", { ...query, limit: "100" }, c.signal),
        ]);
        if (c.signal.aborted) return;
        setOverview(stats);
        setRuns(page.items);
        setRunsCursor(page.next_cursor ?? null);
      } catch (e) {
        if (!c.signal.aborted) setError(errorText(e));
      } finally {
        if (!c.signal.aborted) setLoading(false);
      }
    };
    void load();
    return () => c.abort();
  }, [client, query, refresh]);

  useEffect(() => {
    const c = new AbortController();
    const load = async () => {
      setTabError("");
      if (tab === "trajectory" && !admin) return;
      setTabLoading(true);
      try {
        if (tab === "intent") setIntents(await client.get("intents", query, c.signal));
        if (tab === "anomalies") setAnomalies(await client.get("anomalies", query, c.signal));
        if (tab === "security" && admin)
          setEvents(
            await client.get(
              "security/events",
              {
                window,
                ...(actor ? { actor_id: actor } : {}),
                ...(eventType ? { event_type: eventType } : {}),
              },
              c.signal,
            ),
          );
        if (admin && (tab === "replay" || !badcases))
          setBadcases(
            await client.get(
              "badcases",
              root && tab === "replay" ? { root_cause_category: root } : {},
              c.signal,
            ),
          );
      } catch (e) {
        if (!c.signal.aborted) setTabError(errorText(e));
      } finally {
        if (!c.signal.aborted) setTabLoading(false);
      }
    };
    void load();
    return () => c.abort();
    // badcases is read only to decide the first count request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, query, window, actor, tab, eventType, root, refresh, admin]);

  const sessions = useMemo(() => [...new Map(runs.map((r) => [r.session_id, r])).values()], [runs]);
  const chosenRun = runChoice !== "all" ? runs.find((r) => r.run_id === runChoice) : undefined;
  const sessionId = selectedSession || chosenRun?.session_id || sessions[0]?.session_id || "";
  const sessionRuns = useMemo(
    () =>
      runs
        .filter((r) => r.session_id === sessionId)
        .sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at)),
    [runs, sessionId],
  );
  const flowIds = useMemo(
    () =>
      runChoice !== "all" ? [runChoice] : sessionRuns.slice(-maxSessionRuns).map((r) => r.run_id),
    [runChoice, sessionRuns],
  );
  const flowKey = flowIds.join(",");
  useEffect(() => {
    const c = new AbortController();
    const load = async () => {
      setFlowError("");
      setFlows(null);
      const ids = flowKey ? flowKey.split(",") : [];
      if (!ids.length) {
        setFlows([]);
        return;
      }
      try {
        const values = await Promise.all(
          ids.map((id) => client.get<Flow>(`runs/${id}/flow`, {}, c.signal)),
        );
        if (!c.signal.aborted) setFlows(values);
      } catch (e) {
        if (!c.signal.aborted) setFlowError(errorText(e));
      }
    };
    void load();
    return () => c.abort();
  }, [client, flowKey, refresh]);

  function resetSelection() {
    setSelectedSession("");
    setRunChoice("all");
  }
  function openRun(id: string) {
    const run = runs.find((r) => r.run_id === id);
    setSelectedSession(run?.session_id ?? "");
    setRunChoice(id);
    setTab("trajectory");
  }
  async function loadMoreRuns() {
    if (!runsCursor) return;
    const current = generation.current;
    setMoreLoading(true);
    try {
      const page = await client.get<S["ContextRuns"]>("runs", {
        ...query,
        limit: "100",
        cursor: runsCursor,
      });
      if (current !== generation.current) return;
      setRuns((items) => [...items, ...page.items]);
      setRunsCursor(page.next_cursor ?? null);
    } catch (e) {
      if (current === generation.current) notify(errorText(e));
    } finally {
      if (current === generation.current) setMoreLoading(false);
    }
  }
  async function loadMoreEvents() {
    if (!events?.next_cursor) return;
    const current = generation.current;
    setMoreLoading(true);
    try {
      const page = await client.get<S["ContextSecurityEvents"]>("security/events", {
        window,
        cursor: events.next_cursor,
        ...(actor ? { actor_id: actor } : {}),
        ...(eventType ? { event_type: eventType } : {}),
      });
      if (current !== generation.current) return;
      setEvents({ ...page, items: [...events.items, ...page.items] });
    } catch (e) {
      if (current === generation.current) notify(errorText(e));
    } finally {
      if (current === generation.current) setMoreLoading(false);
    }
  }

  const tabs: [Tab, string, number | null][] = [
    ["trajectory", "轨迹", null],
    ["intent", "意图", null],
    ["anomalies", "异常", null],
    ...(admin
      ? ([
          [
            "security",
            "安全",
            securityKinds.reduce(
              (s, k) => s + (overview?.security_distribution.find((e) => e.name === k)?.count ?? 0),
              0,
            ),
          ],
          ["replay", "回放", badcases?.items.length ?? null],
        ] as [Tab, string, number | null][])
      : []),
  ];
  const actorOptions = useMemo(() => {
    const ids = new Set(runs.map((r) => r.actor_id));
    for (const m of payload.pageData.members?.items ?? []) ids.add(m.userId);
    if (actor) ids.add(actor);
    return [...ids].sort((a, b) => members(a).localeCompare(members(b), "zh-CN"));
  }, [runs, payload, actor, members]);
  const policies = overview?.policy_versions ?? [];

  const selectors = (
    <>
      <label className="co-filter-group">
        <span className="co-filter-label">{admin ? "会话" : "我的会话"}</span>
        <select
          className="co-select-wide"
          value={sessionId}
          onChange={(e) => {
            setSelectedSession(e.target.value);
            setRunChoice("all");
          }}
        >
          {!sessions.length && <option value="">暂无会话</option>}
          {sessionId && !sessions.some((s) => s.session_id === sessionId) && (
            <option value={sessionId}>当前运行所属会话</option>
          )}
          {sessions.map((r) => (
            <option key={r.session_id} value={r.session_id}>
              {r.session_title}
              {admin ? ` · ${members(r.actor_id)}` : ""}
            </option>
          ))}
        </select>
      </label>
      <label className="co-filter-group">
        <span className="co-filter-label">运行</span>
        <select
          className="co-select-wider"
          value={runChoice}
          onChange={(e) => setRunChoice(e.target.value)}
        >
          <option value="all">
            全部（{sessionRuns.length} 次运行
            {sessionRuns.length > maxSessionRuns ? `，展示最近 ${maxSessionRuns} 次` : ""}）
          </option>
          {runChoice !== "all" && !sessionRuns.some((r) => r.run_id === runChoice) && (
            <option value={runChoice}>{shortId(runChoice)}</option>
          )}
          {sessionRuns.map((r) => (
            <option key={r.run_id} value={r.run_id}>
              {shortId(r.run_id)} ·{" "}
              {new Date(r.created_at).toLocaleTimeString("zh-CN", { hour12: false })} ·{" "}
              {statusText[r.status] ?? r.status}
            </option>
          ))}
        </select>
      </label>
      {runsCursor && (
        <button
          type="button"
          className="co-btn co-btn-quiet co-btn-sm"
          disabled={moreLoading}
          onClick={() => void loadMoreRuns()}
        >
          {moreLoading ? "正在加载…" : "加载更多运行"}
        </button>
      )}
    </>
  );

  let body: ReactNode = null;
  if (tab === "trajectory")
    body = flowError ? (
      <Failure text={flowError} onRetry={() => setRefresh((v) => v + 1)} />
    ) : flows === null ? (
      <>
        <div className="co-filter-bar">{selectors}</div>
        <p className="co-empty co-panel" role="status">
          正在读取运行轨迹…
        </p>
      </>
    ) : flows.length ? (
      <Trajectory
        key={flowKey}
        flows={flows}
        selectors={selectors}
        administrator={admin}
        onSnapshot={(reference) => setOverlay({ kind: "snapshot", reference })}
        onBadcase={(node, runId) => setOverlay({ kind: "badcase", node, runId })}
      />
    ) : (
      <>
        <div className="co-filter-bar">{selectors}</div>
        <p className="co-empty co-panel">当前筛选条件下暂无观测记录</p>
      </>
    );
  else if (tabError) body = <Failure text={tabError} onRetry={() => setRefresh((v) => v + 1)} />;
  else if (tab === "intent")
    body = intents ? <IntentPanel data={intents} onOpenRun={openRun} /> : <Pending />;
  else if (tab === "anomalies")
    body = anomalies ? (
      <AnomalyPanel data={anomalies} members={members} onOpenRun={openRun} />
    ) : (
      <Pending />
    );
  else if (tab === "security")
    body = (
      <SecurityPanel
        overview={overview}
        events={events}
        eventType={eventType}
        members={members}
        loadingMore={moreLoading}
        refreshing={tabLoading || loading}
        onFilter={setEventType}
        onMore={() => void loadMoreEvents()}
        onRefresh={() => {
          setRefresh((v) => v + 1);
          notify("已刷新防护状态与审计记录");
        }}
      />
    );
  else if (tab === "replay")
    body = (
      <ReplayPanel
        client={client}
        badcases={badcases}
        root={root}
        onRoot={setRoot}
        members={members}
        runOwner={(id) => runs.find((r) => r.run_id === id)?.actor_id}
      />
    );

  return (
    <div className="ctx-page">
      <header className="co-heading">
        <div className="co-heading-left">
          <h1>{admin ? "ContextOps" : "运行轨迹"}</h1>
          <p>{admin ? "上下文观测与安全治理" : "查看本人的会话与执行记录"}</p>
        </div>
        <div className="co-heading-actions">
          <LangfuseStatus status={overview?.langfuse_status} />
          {admin && (
            <button
              type="button"
              className="co-btn co-btn-secondary"
              onClick={() => setOverlay({ kind: "mcp" })}
            >
              MCP 接入
            </button>
          )}
          {admin &&
            (overview?.langfuse_url ? (
              <a
                className="co-btn co-btn-primary"
                href={overview.langfuse_url}
                target="_blank"
                rel="noreferrer"
              >
                Langfuse ↗
              </a>
            ) : (
              <button
                type="button"
                className="co-btn co-btn-primary"
                disabled
                title="未配置 Langfuse 公开项目地址"
              >
                Langfuse ↗
              </button>
            ))}
        </div>
      </header>
      <section aria-label="运行统计看板" aria-busy={loading}>
        <div className="co-filter-bar">
          <label className="co-filter-group">
            <span className="co-filter-label">时间</span>
            <select
              value={window}
              onChange={(e) => {
                setWindow(e.target.value as typeof window);
                resetSelection();
              }}
            >
              <option value="24h">近 24 小时</option>
              <option value="7d">近 7 天</option>
              <option value="30d">近 30 天</option>
            </select>
          </label>
          {admin && (
            <label className="co-filter-group">
              <span className="co-filter-label">成员</span>
              <select
                value={actor}
                onChange={(e) => {
                  setActor(e.target.value);
                  resetSelection();
                }}
              >
                <option value="">全部成员</option>
                {actorOptions.map((id) => (
                  <option key={id} value={id}>
                    {members(id)}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="co-filter-group">
            <span className="co-filter-label">策略</span>
            <select
              value={policy}
              onChange={(e) => {
                setPolicy(e.target.value);
                resetSelection();
              }}
            >
              <option value="">全部版本</option>
              {policy && !policies.some((p) => p.name === policy) && (
                <option value={policy}>{policy.slice(0, 8)}</option>
              )}
              {policies.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name.slice(0, 8)} · {p.count} 次运行
                </option>
              ))}
            </select>
          </label>
          <span className="co-sample-info">
            {overview
              ? `${num(overview.session_count, 0)} 会话 · ${num(overview.run_count, 0)} 次运行`
              : "—"}
          </span>
          <button
            type="button"
            className="co-icon-btn"
            aria-label="刷新"
            title="刷新"
            disabled={loading}
            onClick={() => setRefresh((v) => v + 1)}
          >
            <RefreshCw size={15} aria-hidden="true" className={loading ? "co-spin" : undefined} />
          </button>
        </div>
        {error ? (
          <Failure text={error} onRetry={() => setRefresh((v) => v + 1)} />
        ) : (
          <Kpis overview={overview} window={window} />
        )}
        {!!overview && (overview.projection_dropped > 0 || overview.projection_errors > 0) && (
          <p className="co-notice" data-tone="warning">
            观测写入失败 {overview.projection_errors} 次，队列丢弃 {overview.projection_dropped}{" "}
            次。部分轨迹可能缺失。
          </p>
        )}
      </section>
      <nav className="co-sub-tabs" role="tablist" aria-label="视图切换">
        {tabs.map(([key, text, badge]) => (
          <button
            key={key}
            id={`co-tab-${key}`}
            type="button"
            role="tab"
            aria-selected={tab === key}
            aria-controls="co-tab-panel"
            className="co-sub-tab"
            onClick={() => setTab(key)}
          >
            {text}
            {badge != null && <span className="co-tab-badge">{badge}</span>}
          </button>
        ))}
      </nav>
      <section
        id="co-tab-panel"
        role="tabpanel"
        aria-labelledby={`co-tab-${tab}`}
        aria-busy={tabLoading}
        className="co-tab-panel"
      >
        {body}
      </section>
      {overlay?.kind === "snapshot" && (
        <SnapshotDialog
          client={client}
          reference={overlay.reference}
          onClose={() => setOverlay(null)}
        />
      )}
      {overlay?.kind === "badcase" && (
        <BadcaseDialog
          client={client}
          runId={overlay.runId}
          node={overlay.node}
          onClose={() => setOverlay(null)}
          onSaved={() => {
            setOverlay(null);
            notify("已沉淀脱敏 Badcase");
            setBadcases(null);
            setTab("replay");
            setRefresh((v) => v + 1);
          }}
        />
      )}
      {overlay?.kind === "mcp" && (
        <McpDrawer
          client={client}
          window={window}
          runId={flowIds.at(-1) ?? ""}
          onClose={() => setOverlay(null)}
        />
      )}
      <div className="co-toast" role="status" aria-live="polite" data-show={!!toast || undefined}>
        {toast?.text}
      </div>
    </div>
  );
}

function Failure({ text, onRetry }: { text: string; onRetry: () => void }) {
  return (
    <div className="co-panel co-failure" role="alert">
      <p>{text}</p>
      <button type="button" className="co-btn co-btn-secondary co-btn-sm" onClick={onRetry}>
        重试
      </button>
    </div>
  );
}
function Pending() {
  return (
    <p className="co-panel co-empty" role="status">
      正在读取观测记录…
    </p>
  );
}

function LangfuseStatus({
  status,
}: {
  status: S["ContextOverview"]["langfuse_status"] | undefined;
}) {
  const [text, tone, hint] =
    status === "connected"
      ? ["Langfuse 已连接", "ok", "已接入本地 Langfuse，仅上报脱敏元数据"]
      : status === "unavailable"
        ? ["Langfuse 导出异常", "warn", "最近一次导出失败，本地观测仍可查看"]
        : ["Langfuse 未启用", "neutral", "未启用 Langfuse 导出，本地观测仍可查看"];
  return (
    <span className="co-status-pill" data-tone={tone} title={hint}>
      <span className="co-status-dot" aria-hidden="true" />
      {text}
    </span>
  );
}

function Kpis({
  overview,
  window,
}: {
  overview: S["ContextOverview"] | null;
  window: "24h" | "7d" | "30d";
}) {
  const empty = !overview?.run_count;
  const value = <T,>(v: T | null | undefined, format: (v: T) => string) =>
    empty || v == null ? "—" : format(v);
  const trend = overview?.trend ?? [];
  const labels = trend.map((p) => {
    const d = new Date(p.start);
    return window === "24h"
      ? `${d.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" })} ${String(d.getHours()).padStart(2, "0")}:00 起`
      : `${d.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" })} 起`;
  });
  const caption = window === "24h" ? "近 24h" : window === "30d" ? "近 30 日" : "近 7 日";
  const status = (name: string) =>
    overview?.turn_completion_breakdown.find((c) => c.name === name)?.count ?? 0;
  const count = (...keys: string[]) =>
    keys.reduce(
      (s, k) => s + (overview?.security_distribution.find((e) => e.name === k)?.count ?? 0),
      0,
    );
  const runs = overview?.run_count ?? 0;
  const canceled = status("canceled");
  const security = count(...securityKinds);
  const series = (pick: (p: S["ContextTrendPoint"]) => number | null) => trend.map(pick);
  const pctFmt = (v: number) => percent(v);
  const cards: {
    tag: string;
    value: string;
    meta: ReactNode;
    values: (number | null)[];
    color: string;
    format: (v: number) => string;
  }[] = [
    {
      tag: "上下文淘汰率",
      value: value(overview?.omission_rate, percent),
      meta: (
        <>
          压缩省 <b>{value(overview?.compaction_saving_rate, percent)}</b> · 缓存{" "}
          <b>{value(overview?.cache_hit_rate, percent)}</b>
        </>
      ),
      values: series((p) => p.omission_rate),
      color: "#c5882a",
      format: pctFmt,
    },
    {
      tag: "中位耗时",
      value: value(overview?.duration.p50_ms, duration),
      meta: (
        <>
          P95 <b>{value(overview?.duration.p95_ms, duration)}</b> · 准备{" "}
          <b>{value(overview?.context_duration_share, percent)}</b>
        </>
      ),
      values: series((p) => p.p50_ms),
      color: "#1c5145",
      format: duration,
    },
    {
      tag: "异常率",
      value: value(overview?.anomaly_count, (v) => percent(v / Math.max(1, runs))),
      meta: (
        <>
          错误 <b>{value(overview?.failure_count, (v) => num(v, 0))}</b> · 空结果{" "}
          <b>{value(overview?.empty_result_count, (v) => num(v, 0))}</b> · 中断{" "}
          <b>{empty ? "—" : canceled}</b>
        </>
      ),
      values: series((p) => p.anomaly_rate),
      color: "#a34236",
      format: pctFmt,
    },
    {
      tag: "安全拦截",
      value: empty ? "—" : `${num(security, 0)} 次`,
      meta: (
        <>
          注入 <b>{empty ? "—" : count("injection_blocked", "injection_stripped")}</b> · 越权{" "}
          <b>{empty ? "—" : count("tenant_mismatch_rejected")}</b> · 脱敏{" "}
          <b>{empty ? "—" : count("pii_redacted")}</b>
        </>
      ),
      values: series((p) => p.security_event_count),
      color: "#a34236",
      format: (v) => `${num(v, 0)} 次`,
    },
    {
      tag: "会话",
      value: overview ? num(overview.session_count, 0) : "—",
      meta: (
        <>
          <b>{overview ? num(overview.run_count, 0) : "—"}</b> 次运行
        </>
      ),
      values: series((p) => p.session_count),
      color: "#1c5145",
      format: (v) => `${num(v, 0)} 个会话`,
    },
    {
      tag: "平均轮数",
      value: value(overview?.avg_turns, (v) => num(v)),
      meta: (
        <>
          最小 <b>{value(overview?.min_turns, (v) => num(v, 0))}</b> · 最大{" "}
          <b>{value(overview?.max_turns, (v) => num(v, 0))}</b>
        </>
      ),
      values: series((p) => p.avg_turns),
      color: "#2b7b99",
      format: (v) => `${num(v)} 轮`,
    },
    {
      tag: "完成率",
      value: empty ? "—" : percent(status("completed") / Math.max(1, runs)),
      meta: (
        <>
          1–3 轮 <b>{value(overview?.short_session_rate, percent)}</b> · 中断{" "}
          <b>{empty ? "—" : percent(canceled / Math.max(1, runs))}</b>
        </>
      ),
      values: series((p) => p.completion_rate),
      color: "#1c5145",
      format: pctFmt,
    },
    {
      tag: "平均搜索",
      value: value(overview?.avg_search_turns, (v) => `${num(v)} 轮`),
      meta: <>每次运行检索轮次</>,
      values: series((p) => p.avg_search_turns),
      color: "#2b7b99",
      format: (v) => `${num(v)} 轮`,
    },
  ];
  return (
    <>
      <div className="co-kpi-grid">
        {cards.map((card) => (
          <article className="co-kpi-card" key={card.tag}>
            <div className="co-kpi-left">
              <span className="co-kpi-tag">{card.tag}</span>
              <div className="co-kpi-val">{card.value}</div>
              <div className="co-kpi-meta">{card.meta}</div>
            </div>
            <Sparkline
              name={card.tag}
              values={card.values}
              labels={labels}
              color={card.color}
              caption={caption}
              format={card.format}
            />
          </article>
        ))}
      </div>
      {!!overview?.run_count && (
        <details className="co-more-stats">
          <summary>窗口构成与终态分布</summary>
          <div className="co-more-grid">
            <div>
              <h3>运行状态</h3>
              <Counts
                items={overview.turn_completion_breakdown.map((c) => ({
                  ...c,
                  name: statusText[c.name] ?? c.name,
                }))}
              />
            </div>
            <div>
              <h3>终态产物</h3>
              <Counts items={overview.terminal_outcome_counts} />
            </div>
            <div>
              <h3>来源构成 · 入窗 / 候选 Token</h3>
              <Counts
                items={overview.source_breakdown.map((s) => ({
                  name: s.source,
                  count: s.selected_tokens,
                  detail: `${num(s.selected_tokens, 0)} / ${num(s.candidate_tokens, 0)}`,
                }))}
              />
            </div>
            <div>
              <h3>压缩与完成</h3>
              <dl className="co-kv">
                <dt>压缩完成率</dt>
                <dd>{percent(overview.compressed_completion_rate)}</dd>
                <dt>未压缩完成率</dt>
                <dd>{percent(overview.uncompressed_completion_rate)}</dd>
                <dt>预算上界</dt>
                <dd>{num(overview.token_upper_bound, 0)} tok</dd>
                <dt>实测 Token</dt>
                <dd>{num(overview.measured_tokens, 0)}</dd>
              </dl>
            </div>
          </div>
        </details>
      )}
    </>
  );
}

function Counts({ items }: { items: { name: string; count: number; detail?: string }[] }) {
  const max = Math.max(1, ...items.map((i) => i.count));
  return items.length ? (
    <div className="co-bar-list co-bar-list-compact">
      {items.map((i) => (
        <div className="co-bar-row" key={i.name}>
          <div className="co-bar-row-head">
            <span>{i.name}</span>
            <strong>{i.detail ?? num(i.count, 0)}</strong>
          </div>
          <div className="co-bar-track">
            <div className="co-bar-fill" style={{ width: `${(i.count / max) * 100}%` }} />
          </div>
        </div>
      ))}
    </div>
  ) : (
    <p className="co-empty">暂无记录</p>
  );
}
