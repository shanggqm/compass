import type { DocumentSession } from "@/client/request-scope";
import type { components } from "@/generated/api";
export type S = components["schemas"];
export type Flow = S["ContextRunFlow"];
export type FlowNode = S["ContextFlowNode"];
export type Run = S["ContextRunView"];
export type Window = "24h" | "7d" | "30d";
export function contextClient(
  session: DocumentSession,
  scope: { tenantId: string; workspaceId: string },
  admin: boolean,
) {
  const path = (suffix: string, query: Record<string, string> = {}) =>
    "contextops/" +
    suffix +
    "?" +
    new URLSearchParams({ ...scope, admin: String(admin), ...query });
  return {
    get<T>(suffix: string, query: Record<string, string> = {}, signal?: AbortSignal) {
      return session.request<T>(path(suffix, query), "GET", undefined, {
        signal,
        retainOnNotFound: true,
        retainOnForbidden: true,
      });
    },
    post<T>(suffix: string, input: unknown) {
      return session.request<T>(path(suffix), "POST", input, {
        retainOnNotFound: true,
        retainOnForbidden: true,
      });
    },
  };
}
export type Client = ReturnType<typeof contextClient>;
export const errorText = (e: unknown) => (e instanceof Error ? e.message : "读取失败，请重试。");
export const num = (n: number | null | undefined, digits = 1) =>
  n == null ? "—" : new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits }).format(n);
export const percent = (n: number | null | undefined) => (n == null ? "—" : num(n * 100) + "%");
/** KPI style, e.g. "3.42 s". */
export const duration = (n: number | null | undefined) =>
  n == null ? "—" : n >= 1000 ? (n / 1000).toFixed(2) + " s" : Math.round(n) + " ms";
/** Compact list style, e.g. "64ms" or "2.45s". */
export const compactMs = (n: number | null | undefined) =>
  n == null ? "—" : n >= 1000 ? (n / 1000).toFixed(2) + "s" : Math.round(n) + "ms";
export const clock = (value: string) =>
  new Date(value).toLocaleTimeString("zh-CN", { hour12: false });
export const shortId = (value: string, prefix = "run_") =>
  prefix + value.replaceAll("-", "").slice(0, 6);
export const phases: Record<string, string> = {
  identify: "意图识别",
  research: "研究与搜索",
  context: "上下文安全与编译",
  answer: "推理与回答生成",
};
export const phaseShort: Record<string, string> = {
  identify: "意图",
  research: "搜索",
  context: "上下文",
  answer: "回答",
};
export const statusText: Record<string, string> = {
  created: "已创建",
  running: "运行中",
  waiting: "等待确认",
  paused: "已暂停",
  cancelling: "取消中",
  completed: "已完成",
  failed: "失败",
  canceled: "已中断",
  empty: "空结果",
  blocked: "已拦截",
  error: "错误",
  skipped: "已跳过",
  succeeded: "已完成",
};
export const roots: Record<S["ContextBadcaseView"]["root_cause_category"], string> = {
  source_data: "源数据",
  semantic: "语义口径",
  retrieval: "检索",
  ranking_cutoff: "排序截断",
  compression_loss: "压缩失真",
  security_false_positive: "安全误拦",
  model_reasoning: "模型推理",
};
export const rootTone: Record<S["ContextBadcaseView"]["root_cause_category"], string> = {
  source_data: "neutral",
  semantic: "info",
  retrieval: "warn",
  ranking_cutoff: "info",
  compression_loss: "warn",
  security_false_positive: "danger",
  model_reasoning: "neutral",
};
export const eventText: Record<string, [string, string]> = {
  injection_blocked: ["注入拦截", "danger"],
  injection_stripped: ["注入剥离", "warn"],
  tenant_mismatch_rejected: ["越权拒绝", "danger"],
  pii_redacted: ["敏感脱敏", "info"],
  trust_downgraded: ["信任降级", "warn"],
  snapshot_viewed: ["快照查看", "neutral"],
  snapshot_denied: ["快照拒绝", "danger"],
  mcp_queried: ["MCP 查询", "neutral"],
};
export const actionText: Record<string, string> = {
  block: "阻断",
  strip: "剥离",
  redact: "脱敏",
  isolate: "隔离",
  deny: "拒绝",
  allow_audit: "审计放行",
};
export const sourceText: Record<string, string> = {
  jev: "JEV",
  flash: "Flash",
  fallback: "兜底",
  unknown: "未知",
};
export type Members = (id: string | null | undefined) => string;
