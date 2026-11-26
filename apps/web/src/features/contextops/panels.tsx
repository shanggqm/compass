"use client";
import { useEffect, useMemo, useState, type KeyboardEvent } from "react";
import { Modal } from "@/components/ui/dialog";
import { UiCodeBlock } from "@/components/ui/code-block";
import { Dumbbell } from "./charts";
import {
  actionText,
  clock,
  compactMs,
  duration,
  errorText,
  eventText,
  num,
  phaseShort,
  roots,
  rootTone,
  shortId,
  type Client,
  type FlowNode,
  type Members,
  type S,
} from "./client";

const activate = (handler: () => void) => (event: KeyboardEvent) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    handler();
  }
};
const palette = ["#1c5145", "#2b7b99", "#4a8d73", "#62716a", "#c5882a", "#a34236"];

function sourceBadge(row: S["ContextIntentBreakdown"]): [string, string] {
  const total = Math.max(1, row.count);
  const share = (n: number) => Math.round((n / total) * 100);
  if (row.fallback && row.fallback >= row.jev && row.fallback >= row.flash)
    return [`兜底 ${share(row.fallback)}%`, "danger"];
  if (row.jev && !row.flash && !row.fallback) return [`JEV ${share(row.jev)}% 直判`, "ok"];
  if (row.flash && !row.jev) return ["Flash 触发", "warn"];
  if (!row.jev && !row.flash) return ["来源未知", "neutral"];
  return [`JEV ${share(row.jev)}% · Flash ${share(row.flash)}%`, "info"];
}

export function IntentPanel({
  data,
  onOpenRun,
}: {
  data: S["ContextIntents"];
  onOpenRun: (runId: string) => void;
}) {
  const breakdown = useMemo(
    () =>
      data.breakdown?.length
        ? data.breakdown
        : data.distribution.map((d) => ({
            scene_id: d.name,
            name: d.name === "unclassified" ? "未分类" : d.name,
            category_id: d.name === "unclassified" ? "unclassified" : null,
            category_name: d.name === "unclassified" ? "未识别 / 兜底" : "其他分类",
            count: d.count,
            jev: 0,
            flash: 0,
            fallback: 0,
            unknown: d.count,
          })),
    [data],
  );
  const total = breakdown.reduce((s, r) => s + r.count, 0);
  const byScene = new Map(breakdown.map((r) => [r.scene_id, r]));
  const categories = useMemo(() => {
    const groups = new Map<
      string,
      { id: string; name: string; count: number; items: typeof breakdown }
    >();
    for (const row of breakdown) {
      const id = row.category_id ?? "other";
      const name =
        row.category_name ?? (row.scene_id === "unclassified" ? "未识别 / 兜底" : "其他分类");
      const entry = groups.get(id) ?? { id, name, count: 0, items: [] };
      entry.count += row.count;
      entry.items.push(row);
      groups.set(id, entry);
    }
    return [...groups.values()].sort(
      (a, b) => b.count - a.count || a.name.localeCompare(b.name, "zh-CN"),
    );
  }, [breakdown]);
  const maxCategory = Math.max(1, ...categories.map((g) => g.count));
  const maxScene = Math.max(1, ...breakdown.map((r) => r.count));
  const latest = (scene: string) => data.recent.find((r) => r.scene_id === scene)?.run_id;
  const durations = data.recent
    .map((r) => r.duration_ms)
    .filter((v): v is number => v != null)
    .sort((a, b) => a - b);
  const pick = (q: number) =>
    durations.length
      ? durations[Math.min(durations.length - 1, Math.floor((durations.length - 1) * q))]
      : null;
  const fallbackScenes = new Set(
    breakdown
      .filter((r) => r.scene_id === "unclassified" || sourceBadge(r)[1] === "danger")
      .map((r) => r.scene_id),
  );
  const rows = useMemo(
    () =>
      data.durations
        .slice()
        .sort((a, b) => (a.p95_ms ?? 0) - (b.p95_ms ?? 0))
        .map((d) => {
          const id = d.scene_id ?? d.name;
          const meta = byScene.get(id);
          const category = meta?.category_name;
          return {
            name: d.name,
            detail: category ? `${category} / ${d.name} (${id})` : `${d.name} (${id})`,
            p50: d.p50_ms,
            p95: d.p95_ms,
            danger: fallbackScenes.has(id),
          };
        }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- recomputed with the response
    [data],
  );
  const excluded = data.excluded_historical_count ?? 0;
  return (
    <>
      <div className="co-grid-2">
        <section className="co-panel">
          <div className="co-panel-head">
            <div>
              <h2 className="co-panel-title">意图分布</h2>
              <p className="co-panel-sub">
                按一级意图与二级场景聚合
                {excluded > 0 ? ` · 已排除 ${num(excluded, 0)} 条早期无投影历史运行` : ""}
              </p>
            </div>
            <span className="co-badge" data-tone="neutral">
              共 {num(total, 0)} 次
            </span>
          </div>
          <div className="co-intent-groups">
            {categories.map((group, groupIndex) => (
              <div className="co-intent-group" key={group.id}>
                <div className="co-intent-group-head">
                  <span>
                    <strong>{group.name}</strong>
                    <span className="co-muted"> · {group.items.length} 个二级场景</span>
                  </span>
                  <span>
                    <strong>{group.count} 次</strong> ·{" "}
                    {num((group.count / Math.max(1, total)) * 100)}%
                  </span>
                </div>
                <div className="co-bar-track co-intent-group-track">
                  <div
                    className="co-bar-fill"
                    style={{
                      width: `${(group.count / maxCategory) * 100}%`,
                      background: group.id === "unclassified" ? "#a34236" : palette[groupIndex % 5],
                    }}
                  />
                </div>
                <div className="co-intent-children">
                  {group.items.map((row, index) => {
                    const [badge, tone] = sourceBadge(row);
                    const run = latest(row.scene_id);
                    return (
                      <div
                        key={row.scene_id}
                        className="co-bar-row co-intent-child"
                        role={run ? "button" : undefined}
                        tabIndex={run ? 0 : undefined}
                        data-clickable={!!run || undefined}
                        aria-label={
                          run
                            ? `${group.name} / ${row.name} ${row.count} 次，查看最近运行`
                            : undefined
                        }
                        onClick={run ? () => onOpenRun(run) : undefined}
                        onKeyDown={run ? activate(() => onOpenRun(run)) : undefined}
                      >
                        <div className="co-bar-row-head">
                          <span className="co-intent-child-title">
                            <strong>{row.name}</strong>
                            <span className="co-badge co-mono" data-tone="neutral">
                              {row.scene_id}
                            </span>
                            <span className="co-badge" data-tone={tone}>
                              {badge}
                            </span>
                          </span>
                          <span>
                            <strong>{row.count} 次</strong> ·{" "}
                            {num((row.count / Math.max(1, total)) * 100)}%
                          </span>
                        </div>
                        <div className="co-bar-track">
                          <div
                            className="co-bar-fill"
                            style={{
                              width: `${(row.count / maxScene) * 86}%`,
                              background:
                                row.scene_id === "unclassified" || tone === "danger"
                                  ? "#a34236"
                                  : palette[(groupIndex + index) % 5],
                            }}
                          />
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
            {!categories.length && <p className="co-empty">当前筛选条件下暂无意图分类记录</p>}
          </div>
        </section>
        <section className="co-panel">
          <div className="co-panel-head">
            <div>
              <h2 className="co-panel-title">分类耗时</h2>
              <p className="co-panel-sub">按二级场景展示 P50 → P95 耗时</p>
            </div>
            <span className="co-badge" data-tone="neutral">
              P50 {compactMs(pick(0.5))} · P95 {compactMs(pick(0.95))}
            </span>
          </div>
          <Dumbbell rows={rows} label="意图分类耗时哑铃图（P50 → P95）" />
        </section>
      </div>
      <section className="co-panel">
        <div className="co-panel-head">
          <h2 className="co-panel-title">分类记录</h2>
        </div>
        <div className="co-table-wrap" data-scroll>
          <table className="co-table">
            <thead>
              <tr>
                <th>Run ID</th>
                <th>一级意图</th>
                <th>二级场景</th>
                <th>判决来源</th>
                <th>耗时</th>
                <th>绑定语义版本</th>
                <th>时间</th>
              </tr>
            </thead>
            <tbody>
              {data.recent.map((r) => (
                <tr
                  key={r.run_id}
                  tabIndex={0}
                  data-clickable
                  onClick={() => onOpenRun(r.run_id)}
                  onKeyDown={activate(() => onOpenRun(r.run_id))}
                >
                  <td className="co-mono">
                    <strong>{shortId(r.run_id)}</strong>
                  </td>
                  <td>
                    {r.category_name ?? (r.scene_id === "unclassified" ? "未识别 / 兜底" : "—")}
                  </td>
                  <td>
                    <span className="co-intent-cell">
                      <strong>
                        {r.scene_name ?? (r.scene_id === "unclassified" ? "未分类" : r.scene_id)}
                      </strong>
                      <span className="co-badge co-mono" data-tone="neutral">
                        {r.scene_id}
                      </span>
                    </span>
                  </td>
                  <td>
                    <span
                      className="co-badge"
                      data-tone={
                        r.source === "jev"
                          ? "ok"
                          : r.source === "flash"
                            ? "info"
                            : r.source === "fallback"
                              ? "danger"
                              : "neutral"
                      }
                    >
                      {r.source === "jev"
                        ? "intent_jev 直判"
                        : r.source === "fallback"
                          ? "JEV → Flash 复核"
                          : `intent_${r.source}`}
                    </span>
                  </td>
                  <td>{duration(r.duration_ms)}</td>
                  <td className="co-mono">{r.semantic_version ?? "—"}</td>
                  <td>{new Date(r.created_at).toLocaleString("zh-CN", { hour12: false })}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!data.recent.length && <p className="co-empty">当前筛选条件下暂无观测记录</p>}
        </div>
      </section>
    </>
  );
}

const reasonText: Record<string, [string, string]> = {
  run_failed: ["运行失败", "danger"],
  empty_result: ["检索空结果", "warn"],
  compaction_truncated: ["压缩截断", "warn"],
  node_failed: ["工具失败", "danger"],
};

export function AnomalyPanel({
  data,
  members,
  onOpenRun,
}: {
  data: S["ContextAnomalies"];
  members: Members;
  onOpenRun: (runId: string) => void;
}) {
  const [focus, setFocus] = useState("");
  const reasons = new Map((data.reasons ?? []).map((r) => [r.run_id, r]));
  const ranking = [
    {
      key: "compaction_truncated",
      name: "上下文压缩截断",
      count: data.truncation_count,
      tone: "danger",
    },
    { key: "empty_result", name: "检索空结果", count: data.empty_retrieval_count, tone: "warning" },
    ...data.failures.map((f) => ({
      key: `label:${f.name}`,
      name: `${f.name} 失败`,
      count: f.count,
      tone: "danger",
    })),
  ]
    .filter((r) => r.count > 0)
    .sort((a, b) => b.count - a.count);
  const max = Math.max(1, ...ranking.map((r) => r.count));
  const total = ranking.reduce((s, r) => s + r.count, 0);
  const matches = (runId: string) => {
    if (!focus) return true;
    const reason = reasons.get(runId);
    if (!reason) return false;
    return focus.startsWith("label:")
      ? (reason.failed_labels ?? []).includes(focus.slice(6))
      : reason.reasons.includes(focus as S["ContextAnomalyReason"]["reasons"][number]);
  };
  const phaseMax = Math.max(1, ...data.phase_durations.map((d) => d.p95_ms ?? 0));
  const phaseColor: Record<string, string> = {
    identify: "#2b7b99",
    research: "#c5882a",
    context: "#1c5145",
    answer: "#7e68b8",
  };
  const runs = data.runs.filter((r) => matches(r.run_id));
  return (
    <>
      <div className="co-grid-2">
        <section className="co-panel">
          <div className="co-panel-head">
            <h2 className="co-panel-title">异常排行</h2>
            <span className="co-badge" data-tone="danger">
              {num(total, 0)} 次
            </span>
          </div>
          <div className="co-bar-list">
            {ranking.map((r) => (
              <div
                key={r.key}
                className="co-bar-row"
                role="button"
                tabIndex={0}
                data-clickable
                aria-pressed={focus === r.key}
                onClick={() => setFocus(focus === r.key ? "" : r.key)}
                onKeyDown={activate(() => setFocus(focus === r.key ? "" : r.key))}
              >
                <div className="co-bar-row-head">
                  <strong>{r.name}</strong>
                  <span>{r.count} 次</span>
                </div>
                <div className="co-bar-track">
                  <div
                    className="co-bar-fill"
                    data-tone={r.tone}
                    style={{ width: `${(r.count / max) * 82}%` }}
                  />
                </div>
              </div>
            ))}
            {!ranking.length && <p className="co-empty">当前筛选条件下未发现异常</p>}
          </div>
        </section>
        <section className="co-panel">
          <div className="co-panel-head">
            <h2 className="co-panel-title">阶段耗时</h2>
            <span className="co-badge" data-tone="neutral">
              P50 / P95
            </span>
          </div>
          <div className="co-bar-list">
            {data.phase_durations.map((d) => (
              <div className="co-bar-row" key={d.name}>
                <div className="co-bar-row-head">
                  <strong>{phaseShort[d.name] ?? d.name}</strong>
                  <span>
                    {compactMs(d.p50_ms)} / {compactMs(d.p95_ms)}
                  </span>
                </div>
                <div className="co-bar-track">
                  <div
                    className="co-bar-fill"
                    style={{
                      width: `${((d.p95_ms ?? 0) / phaseMax) * 88}%`,
                      background: phaseColor[d.name] ?? "#1c5145",
                    }}
                  />
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>
      <section className="co-panel">
        <div className="co-panel-head">
          <h2 className="co-panel-title">异常记录</h2>
          {focus && (
            <button
              type="button"
              className="co-btn co-btn-quiet co-btn-sm"
              onClick={() => setFocus("")}
            >
              清除筛选
            </button>
          )}
        </div>
        <div className="co-table-wrap" data-scroll>
          <table className="co-table">
            <thead>
              <tr>
                <th>运行 ID</th>
                <th>成员</th>
                <th>异常</th>
                <th>阶段</th>
                <th>耗时</th>
                <th title="入窗上界 / 模型实测">Token</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => {
                const reason = reasons.get(r.run_id);
                return (
                  <tr
                    key={r.run_id}
                    tabIndex={0}
                    data-clickable
                    onClick={() => onOpenRun(r.run_id)}
                    onKeyDown={activate(() => onOpenRun(r.run_id))}
                  >
                    <td className="co-mono">
                      <strong>{shortId(r.run_id)}</strong>
                    </td>
                    <td>{members(r.actor_id)}</td>
                    <td>
                      <span className="co-badges">
                        {(
                          reason?.reasons ?? [r.status === "failed" ? "run_failed" : "node_failed"]
                        ).map((code) => (
                          <span
                            key={code}
                            className="co-badge"
                            data-tone={reasonText[code]?.[1] ?? "neutral"}
                          >
                            {code === "node_failed" && reason?.failed_labels?.length
                              ? `${(reason.failed_labels ?? []).join("、")} 失败`
                              : (reasonText[code]?.[0] ?? code)}
                          </span>
                        ))}
                        {r.security_blocked_count > 0 && (
                          <span className="co-badge" data-tone="danger">
                            安全拦截 ×{r.security_blocked_count}
                          </span>
                        )}
                      </span>
                    </td>
                    <td>{reason?.phase ? phaseShort[reason.phase] : "—"}</td>
                    <td>{duration(r.duration_ms)}</td>
                    <td>
                      {num(r.token_upper_bound, 0)} / {num(r.total_tokens, 0)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!runs.length && <p className="co-empty">当前筛选条件下暂无异常运行</p>}
        </div>
      </section>
    </>
  );
}

export function SecurityPanel({
  overview,
  events,
  eventType,
  members,
  loadingMore,
  onFilter,
  onMore,
  onRefresh,
  refreshing,
}: {
  overview: S["ContextOverview"] | null;
  events: S["ContextSecurityEvents"] | null;
  eventType: string;
  members: Members;
  loadingMore: boolean;
  onFilter: (type: string) => void;
  onMore: () => void;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const count = (...keys: string[]) =>
    keys.reduce(
      (s, k) => s + (overview?.security_distribution.find((e) => e.name === k)?.count ?? 0),
      0,
    );
  const enforce = overview?.security_mode !== "audit_only";
  const checks = [
    {
      key: "tenant_mismatch_rejected",
      name: "跨租户隔离",
      scene: "候选条目的租户或工作区与当前会话不一致",
      action: "同步拒绝",
      hits: count("tenant_mismatch_rejected"),
      status: ["强制生效", "ok"],
    },
    {
      key: "injection_blocked",
      name: "间接提示词注入",
      scene: "检索文档或工具结果含指令覆盖、角色伪造",
      action: enforce ? "在线阻断" : "仅审计",
      hits: count("injection_blocked", "injection_stripped"),
      status: enforce ? ["生效", "ok"] : ["仅审计", "warn"],
    },
    {
      key: "pii_redacted",
      name: "敏感信息脱敏",
      scene: "工具结果含手机号、邮箱、身份证或密钥",
      action: "自动掩码",
      hits: count("pii_redacted"),
      status: ["生效", "ok"],
    },
    {
      key: "trust_downgraded",
      name: "摘要权限继承",
      scene: "受限数据压缩生成摘要与快照",
      action: "继承最小权限",
      hits: count("trust_downgraded"),
      status: ["生效", "ok"],
    },
  ];
  return (
    <>
      <section className="co-panel co-panel-gap">
        <div className="co-panel-head">
          <div>
            <h2 className="co-panel-title">安全校验</h2>
            <p className="co-panel-sub">
              当前模式：
              {overview?.security_mode === "audit_only"
                ? "audit_only（仅审计）"
                : "enforce（同步硬拦截）"}{" "}
              · 点击行筛选审计记录
            </p>
          </div>
          <button
            type="button"
            className="co-btn co-btn-primary co-btn-sm"
            disabled={refreshing}
            onClick={onRefresh}
          >
            {refreshing ? "刷新中…" : "刷新状态"}
          </button>
        </div>
        <div className="co-table-wrap">
          <table className="co-table">
            <thead>
              <tr>
                <th>校验项</th>
                <th>场景</th>
                <th>动作</th>
                <th>命中</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {checks.map((c) => (
                <tr
                  key={c.key}
                  tabIndex={0}
                  data-clickable
                  aria-selected={eventType === c.key}
                  onClick={() => onFilter(eventType === c.key ? "" : c.key)}
                  onKeyDown={activate(() => onFilter(eventType === c.key ? "" : c.key))}
                >
                  <td>
                    <strong>{c.name}</strong>
                  </td>
                  <td>{c.scene}</td>
                  <td>{c.action}</td>
                  <td>{num(c.hits, 0)} 次</td>
                  <td>
                    <span className="co-badge" data-tone={c.status[1]}>
                      {c.status[0]}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="co-panel">
        <div className="co-panel-head">
          <h2 className="co-panel-title">审计记录</h2>
          {eventType && (
            <button
              type="button"
              className="co-btn co-btn-quiet co-btn-sm"
              onClick={() => onFilter("")}
            >
              {eventText[eventType]?.[0] ?? eventType} · 清除筛选
            </button>
          )}
        </div>
        <div className="co-table-wrap" data-scroll>
          <table className="co-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>类型</th>
                <th>动作</th>
                <th>规则</th>
                <th>成员</th>
                <th>对象</th>
              </tr>
            </thead>
            <tbody>
              {events?.items.map((e) => {
                const [label, tone] = eventText[e.event_type] ?? [e.event_type, "neutral"];
                return (
                  <tr key={e.id}>
                    <td title={new Date(e.created_at).toLocaleString("zh-CN", { hour12: false })}>
                      {clock(e.created_at)}
                    </td>
                    <td>
                      <span className="co-badge" data-tone={tone}>
                        {label}
                      </span>
                    </td>
                    <td>
                      {actionText[e.action] ?? e.action}
                      {e.hit_count > 1 ? ` ×${e.hit_count}` : ""}
                    </td>
                    <td className="co-mono">{e.rule_code}</td>
                    <td>{members(e.actor_id)}</td>
                    <td className="co-mono">
                      {[e.run_id ? shortId(e.run_id) : null, e.target_ref ?? e.source_kind]
                        .filter(Boolean)
                        .join(" / ") || "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!events?.items.length && <p className="co-empty">当前筛选条件下暂无审计记录</p>}
        </div>
        {events?.next_cursor && (
          <div className="co-more">
            <button
              type="button"
              className="co-btn co-btn-quiet co-btn-sm"
              disabled={loadingMore}
              onClick={onMore}
            >
              {loadingMore ? "正在加载…" : "加载更多审计"}
            </button>
          </div>
        )}
      </section>
    </>
  );
}

type Draft = {
  protect: boolean;
  budget: number | null;
  maximum: number | null;
  threshold: number | null;
};

export function ReplayPanel({
  client,
  badcases,
  root,
  onRoot,
  members,
  runOwner,
}: {
  client: Client;
  badcases: S["ContextBadcases"] | null;
  root: string;
  onRoot: (value: string) => void;
  members: Members;
  runOwner: (runId: string) => string | undefined;
}) {
  const items = useMemo(() => badcases?.items ?? [], [badcases]);
  const [chosen, setChosen] = useState("");
  const active = items.find((b) => b.id === chosen) ?? items[0];
  const [baseline, setBaseline] = useState<{
    id: string;
    value: S["ContextReplayComparison"];
  } | null>(null);
  const [result, setResult] = useState<S["ContextReplayComparison"] | null>(null);
  const [draft, setDraft] = useState<Draft>({
    protect: true,
    budget: null,
    maximum: null,
    threshold: null,
  });
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const activeId = active?.id,
    activeOpen = active?.status === "open";
  useEffect(() => {
    if (!activeId || !activeOpen) return;
    let alive = true;
    const load = async () => {
      setResult(null);
      setError("");
      try {
        const value = await client.post<S["ContextReplayComparison"]>(
          `badcases/${activeId}/replay`,
          { override: {} },
        );
        if (alive) setBaseline({ id: activeId, value });
      } catch (e) {
        if (alive) setError(errorText(e));
      }
    };
    void load();
    return () => {
      alive = false;
    };
  }, [client, activeId, activeOpen]);
  const base = baseline?.id === activeId ? baseline.value.baseline : null;
  const budgetMax = Math.max(16000, Math.ceil(((base?.tokens ?? 8000) * 2) / 1000) * 1000);
  async function run() {
    if (!active) return;
    setBusy(true);
    setError("");
    try {
      setResult(
        await client.post(`badcases/${active.id}/replay`, {
          override: {
            protect_dictionary: draft.protect,
            ...(draft.budget != null ? { max_input_tokens: draft.budget } : {}),
            ...(draft.threshold != null ? { compaction_threshold: draft.threshold } : {}),
            ...(draft.maximum != null ? { max_observation_items: draft.maximum } : {}),
          },
        }),
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const candidate = result?.candidate;
  return (
    <div className="co-grid-2">
      <section className="co-panel">
        <div className="co-panel-head">
          <h2 className="co-panel-title">问题列表</h2>
          <select
            aria-label="根因分类"
            className="co-select-sm"
            value={root}
            onChange={(e) => onRoot(e.target.value)}
          >
            <option value="">全部根因</option>
            {Object.entries(roots).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </div>
        <div className="co-decision-list co-decision-list-tall">
          {items.map((b) => (
            <button
              key={b.id}
              type="button"
              className="co-decision-card co-badcase"
              aria-pressed={active?.id === b.id}
              onClick={() => {
                setChosen(b.id);
                setResult(null);
              }}
            >
              <span className="co-decision-head">
                <span className="co-decision-id">
                  {shortId(b.run_id)} · {members(runOwner(b.run_id))}
                </span>
                <span className="co-badge" data-tone={rootTone[b.root_cause_category]}>
                  {roots[b.root_cause_category]}
                </span>
                {b.status !== "open" && (
                  <span className="co-badge" data-tone="danger">
                    {b.status === "authorization_unavailable" ? "权限暂不可核验" : "快照失效"}
                  </span>
                )}
              </span>
              <strong className="co-badcase-title">{b.title}</strong>
              <span className="co-decision-meta">
                <span>快照 {b.snapshot_ref.slice(0, 11)}</span>
                <span>策略 {b.policy_digest.slice(0, 8)}</span>
                <span>Prompt #{b.prompt_generation}</span>
              </span>
            </button>
          ))}
          {!items.length && (
            <p className="co-empty">暂无 Badcase，在轨迹中选择节点后点击「标为问题」。</p>
          )}
        </div>
      </section>
      <section className="co-panel" aria-label="策略回放">
        <div className="co-panel-head">
          <h2 className="co-panel-title">策略回放</h2>
          {active && (
            <span className="co-badge" data-tone="neutral">
              快照 {active.snapshot_ref.slice(0, 11)}
            </span>
          )}
        </div>
        {!active ? (
          <p className="co-empty">选择左侧问题后可在冻结候选集上比较装配策略。</p>
        ) : (
          <>
            <label className="co-control-row">
              <strong>保护业务语义口径</strong>
              <input
                type="checkbox"
                checked={draft.protect}
                onChange={(e) => setDraft({ ...draft, protect: e.target.checked })}
              />
            </label>
            <label className="co-control-row">
              <strong>
                输入预算：{draft.budget == null ? "沿用快照" : `${num(draft.budget, 0)} Token`}
              </strong>
              <input
                type="range"
                min={1000}
                max={budgetMax}
                step={500}
                value={draft.budget ?? Math.min(budgetMax, Math.max(1000, base?.tokens ?? 8000))}
                onChange={(e) => setDraft({ ...draft, budget: Number(e.target.value) })}
              />
            </label>
            <label className="co-control-row">
              <strong>
                单源上限：{draft.maximum == null ? "沿用快照" : `${draft.maximum} 条`}
              </strong>
              <input
                type="range"
                min={0}
                max={32}
                step={1}
                value={draft.maximum ?? 6}
                onChange={(e) => setDraft({ ...draft, maximum: Number(e.target.value) })}
              />
            </label>
            <label className="co-control-row">
              <strong>
                压缩阈值：{draft.threshold == null ? "沿用快照" : draft.threshold.toFixed(1)}
              </strong>
              <input
                type="range"
                min={0.1}
                max={1}
                step={0.1}
                value={draft.threshold ?? 1}
                onChange={(e) => setDraft({ ...draft, threshold: Number(e.target.value) })}
              />
            </label>
            <div className="co-replay-actions">
              <button
                type="button"
                className="co-btn co-btn-primary"
                disabled={busy || active.status !== "open"}
                aria-busy={busy || undefined}
                onClick={() => void run()}
              >
                {busy ? "回放中…" : "开始回放"}
              </button>
              <button
                type="button"
                className="co-btn co-btn-quiet co-btn-sm"
                disabled={busy}
                onClick={() => {
                  setDraft({ protect: true, budget: null, maximum: null, threshold: null });
                  setResult(null);
                }}
              >
                恢复默认
              </button>
            </div>
            {active.status !== "open" && (
              <p className="co-notice" data-tone="warning" role="alert">
                快照已过期、来源已撤权或权限暂时无法核验，无法回放。
              </p>
            )}
            {error && (
              <p className="co-notice" data-tone="danger" role="alert">
                {error}
              </p>
            )}
            <div className="co-diff-grid" role="region" aria-label="回放对照">
              <div className="co-diff-box">
                <div className="co-diff-label">原策略</div>
                <div className="co-diff-value">{base ? `${num(base.tokens, 0)} tok` : "—"}</div>
                <div className="co-diff-meta">
                  业务口径：
                  {base ? (
                    <span
                      className="co-badge"
                      data-tone={base.dictionary_preserved ? "ok" : "danger"}
                    >
                      {base.dictionary_preserved ? "完整保留" : "存在缺失"}
                    </span>
                  ) : (
                    "—"
                  )}
                </div>
                {baseline?.id === activeId && (
                  <div className="co-diff-meta co-muted">
                    {baseline.value.baseline_matches_snapshot
                      ? "基准与冻结快照一致"
                      : "基准与快照存在差异"}
                  </div>
                )}
              </div>
              <div className="co-diff-box" data-improved={!!candidate || undefined}>
                <div className="co-diff-label co-diff-label-strong">新策略</div>
                <div className="co-diff-value">
                  {candidate && result
                    ? `${num(candidate.tokens, 0)} tok (${result.token_delta > 0 ? "+" : ""}${num(result.token_delta, 0)})`
                    : "—"}
                </div>
                <div className="co-diff-meta">
                  权威口径：
                  {candidate ? (
                    <span
                      className="co-badge"
                      data-tone={candidate.dictionary_preserved ? "ok" : "danger"}
                    >
                      {candidate.dictionary_preserved ? "100% 完整保留" : "仍有缺失"}
                    </span>
                  ) : (
                    "调整参数后开始回放"
                  )}
                </div>
                {result && (
                  <div className="co-diff-meta co-muted">
                    新增 {result.newly_included_ids.length} 条 · 丢弃{" "}
                    {result.newly_omitted_ids.length} 条
                  </div>
                )}
              </div>
            </div>
            {result &&
              (result.newly_included_ids.length > 0 || result.newly_omitted_ids.length > 0) && (
                <UiCodeBlock
                  title="入窗条目差异"
                  language="json"
                  code={JSON.stringify(
                    { 新增: result.newly_included_ids, 丢弃: result.newly_omitted_ids },
                    null,
                    2,
                  )}
                />
              )}
          </>
        )}
      </section>
    </div>
  );
}

export function SnapshotDialog({
  client,
  reference,
  onClose,
}: {
  client: Client;
  reference: string;
  onClose: () => void;
}) {
  const [data, setData] = useState<S["ContextSnapshotView"] | null>(null),
    [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const load = () =>
      client
        .get<S["ContextSnapshotView"]>(`snapshots/${reference}`, {}, controller.signal)
        .then((value) => {
          if (!controller.signal.aborted) {
            setData(value);
            setError("");
          }
        })
        .catch((e) => {
          if (!controller.signal.aborted) {
            setData(null);
            setError(errorText(e));
          }
        });
    void load();
    const timer = setInterval(load, 30000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [client, reference, retry]);
  return (
    <Modal
      title={data ? "加密上下文快照查看（RBAC 实时复核通过）" : "加密上下文快照查看"}
      open
      wide
      onClose={onClose}
      footer={
        <button type="button" className="co-btn co-btn-secondary" onClick={onClose}>
          关闭
        </button>
      }
    >
      <div className="co-modal">
        {error ? (
          <div role="alert" className="co-notice" data-tone="danger">
            <p>{error}</p>
            <button
              type="button"
              className="co-btn co-btn-secondary co-btn-sm"
              onClick={() => {
                setError("");
                setRetry((r) => r + 1);
              }}
            >
              重试
            </button>
          </div>
        ) : !data ? (
          <p role="status" className="co-muted">
            正在校验权限与读取快照…
          </p>
        ) : (
          <>
            <div className="co-notice">
              已复核当前用户归属权限，本次查看已记入安全审计（<code>snapshot_viewed</code>
              ）；敏感字段已脱敏。
            </div>
            <p className="co-muted co-snapshot-meta">
              min_trust: {data.min_trust_level} · {data.data_classification} · 到期{" "}
              {new Date(data.expires_at).toLocaleString("zh-CN", { hour12: false })}
            </p>
            <UiCodeBlock
              title={`${reference}.decrypted.json`}
              language="json"
              code={JSON.stringify(data.payload, null, 2)}
            />
          </>
        )}
      </div>
    </Modal>
  );
}

export function BadcaseDialog({
  client,
  runId,
  node,
  onClose,
  onSaved,
}: {
  client: Client;
  runId: string;
  node: FlowNode;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [title, setTitle] = useState(""),
    [note, setNote] = useState("");
  const [root, setRoot] =
    useState<S["ContextBadcaseInput"]["root_cause_category"]>("compression_loss");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await client.post("badcases", {
        run_id: runId,
        step_id: node.step_id,
        root_cause_category: root,
        title,
        note: note || null,
      });
      onSaved();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="沉淀脱敏 Badcase" open onClose={onClose} busy={busy}>
      <form className="co-modal co-form" onSubmit={save}>
        <p className="co-muted">
          {shortId(runId)} · {phaseShort[node.phase] ?? node.phase} · 快照{" "}
          {node.snapshot_ref?.slice(0, 11) ?? "—"}
        </p>
        <label>
          标题
          <input
            required
            maxLength={160}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label>
          根因分类
          <select value={root} onChange={(e) => setRoot(e.target.value as typeof root)}>
            {Object.entries(roots).map(([k, v]) => (
              <option key={k} value={k}>
                {k}（{v}）
              </option>
            ))}
          </select>
        </label>
        <label>
          备注
          <textarea
            value={note}
            maxLength={2000}
            onChange={(e) => setNote(e.target.value)}
            rows={3}
          />
        </label>
        {error && (
          <p role="alert" className="co-notice" data-tone="danger">
            {error}
          </p>
        )}
        <div className="co-form-actions">
          <button type="button" className="co-btn co-btn-quiet" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button
            type="submit"
            className="co-btn co-btn-primary"
            disabled={busy || !title.trim()}
            aria-busy={busy || undefined}
          >
            {busy ? "保存中…" : "确认沉淀"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

const tools = [
  ["contextops_get_stats", "overview"],
  ["contextops_get_run_flow", "flow"],
  ["contextops_list_runs", "runs"],
  ["contextops_list_badcases", "badcases"],
] as const;

export function McpDrawer({
  client,
  window,
  runId,
  onClose,
}: {
  client: Client;
  window: string;
  runId: string;
  onClose: () => void;
}) {
  const [tool, setTool] = useState<(typeof tools)[number][0]>("contextops_get_stats");
  const [preview, setPreview] = useState<{ tool: string; value: unknown } | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    const source = tools.find(([name]) => name === tool)?.[1];
    const load = async () => {
      setError("");
      try {
        if (source === "flow" && !runId) throw new Error("请先在轨迹中选择一次运行");
        const value =
          source === "overview"
            ? await client.get("overview", { window }, controller.signal)
            : source === "flow"
              ? await client.get(`runs/${runId}/flow`, {}, controller.signal)
              : source === "runs"
                ? await client.get("runs", { window, limit: "5" }, controller.signal)
                : await client.get("badcases", { limit: "5" }, controller.signal);
        if (!controller.signal.aborted) setPreview({ tool, value });
      } catch (e) {
        if (!controller.signal.aborted) setError(errorText(e));
      }
    };
    void load();
    return () => controller.abort();
  }, [client, tool, window, runId]);
  const args =
    tool === "contextops_get_run_flow"
      ? { run_id: runId }
      : tool === "contextops_list_badcases"
        ? { limit: 5 }
        : { window };
  return (
    <Modal
      title="ContextOps MCP 只读接入与试跑"
      open
      wide
      placement="right"
      onClose={onClose}
      footer={
        <button type="button" className="co-btn co-btn-primary" onClick={onClose}>
          完成
        </button>
      }
    >
      <div className="co-modal">
        <div className="co-notice">
          端点 <code>POST /v1/contextops/mcp</code>{" "}
          严格只读、按租户隔离，仅返回脱敏摘要与统计，不暴露明文快照。
        </div>
        <div className="co-chip-bar" role="group" aria-label="只读工具">
          {tools.map(([name]) => (
            <button
              key={name}
              type="button"
              className="co-chip"
              aria-pressed={tool === name}
              onClick={() => setTool(name)}
            >
              {name}
            </button>
          ))}
        </div>
        {error ? (
          <p className="co-notice" data-tone="warning" role="alert">
            {error}
          </p>
        ) : preview?.tool === tool ? (
          <UiCodeBlock
            title={`JSON-RPC 2.0 · ${tool}`}
            language="json"
            code={JSON.stringify(
              {
                jsonrpc: "2.0",
                method: "tools/call",
                params: { name: tool, arguments: args },
                result: { structuredContent: preview.value, isError: false },
              },
              null,
              2,
            )}
          />
        ) : (
          <p role="status" className="co-muted">
            正在读取同源只读数据…
          </p>
        )}
        <p className="co-muted co-mcp-note">
          试跑结果由 Web 调用同一只读查询生成，返回结构与 MCP <code>structuredContent</code>{" "}
          一致。外部客户端请使用管理员签发的一小时凭据连接：
        </p>
        <UiCodeBlock
          title="客户端配置模板"
          language="json"
          code={JSON.stringify(
            {
              mcpServers: {
                compassContextOps: {
                  url: "<Agent 地址>/v1/contextops/mcp",
                  headers: { Authorization: "Bearer <专用 ContextOps 令牌>" },
                },
              },
            },
            null,
            2,
          )}
        />
      </div>
    </Modal>
  );
}
