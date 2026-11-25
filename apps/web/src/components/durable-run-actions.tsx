"use client";
import Image from "next/image";
import { SemanticChoiceCard } from "@/features/semantic/choice-card";
import { SemanticResultActions } from "@/features/semantic/result-actions";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ComponentProps,
  type ReactNode,
} from "react";
import { readChildTrace } from "@/client/child-trace";
import { ApiError } from "@/client/request-scope";
import type { RunView } from "@/contracts/agent-response";
import { requestDurableControl } from "@/client/durable-analysis";
import { useDocumentSession } from "./shell/session-boundary";
import { parseModelContract } from "@/contracts/model-contract";
import type { components } from "@/generated/api";
import { AnalysisMarkdown } from "./analysis-markdown";
import { DeliveryAttachment } from "./delivery-attachment";
import { UiButton } from "./ui/button";
import { UiExecutionTimeline } from "./ui/execution-timeline";

const reasons: Record<string, string> = {
  lease_expired: "执行连接中断，正在检查恢复位置。",
  agent_restarted: "服务已恢复，正在检查执行记录。",
  worker_released: "正在释放执行资源，随后继续任务。",
  process_interrupted: "执行进程已重启，可从已保存的位置继续。",
  stream_data_lost: "增量记录不可用，请保留当前会话并联系管理员。",
  execution_unknown: "上次调用结果待核对，暂停后续执行。",
  approval_expired: "确认已过期，已保留只读分析。",
  approval_rejected: "已拒绝执行，已保留只读分析。",
  cleanup_unconfirmed: "执行资源正在核对，确认完成前无法继续。",
};
const revisionLabels: Record<string, string> = {
  name: "指标名称",
  summary: "业务含义",
  formula: "公式",
  unit: "单位",
  grain: "统计粒度",
  event_time: "业务时间",
  timezone: "时区",
  aggregation: "聚合规则",
  responsible: "负责人",
};

export function DurableRunActions({
  run,
  sessionId,
  scope,
  reload,
  timeline,
  children,
  editGoal,
}: {
  run: RunView;
  sessionId: string;
  scope: { tenantId: string; workspaceId: string };
  reload: () => void;
  timeline: ComponentProps<typeof UiExecutionTimeline>;
  children: ReactNode;
  editGoal?: (objective: string) => void;
}) {
  const api = useDocumentSession(),
    pending = useRef<{ kind: string; body: Record<string, unknown> } | null>(null);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [resolvedWait, setResolvedWait] = useState<string>();
  const approval = Object.values(run.objects)
    .filter((object) => object.kind === "approval")
    .sort(
      (a, b) =>
        Number(a.value.status === "pending") - Number(b.value.status === "pending") ||
        (a.value.expiresAt ?? "").localeCompare(b.value.expiresAt ?? ""),
    )
    .at(-1)?.value;
  const scheduleDetail = approval?.summary.match(
    /^((?:创建|修改)「[^\r\n]+?」[。][\s\S]*?)分析内容[：:]([\s\S]+)$/,
  );
  const [wait, setWait] = useState<components["schemas"]["WaitView"] | null>(null);
  const [edits, setEdits] = useState<{ wait: string; data: Record<string, string> }>();
  const [waitRefresh, setWaitRefresh] = useState(0);
  const waitId = approval?.waitId;
  const scopeQuery = new URLSearchParams(scope).toString();
  const loadChild = useCallback(
    (id: string, signal: AbortSignal) =>
      readChildTrace(
        api,
        run.runId,
        { tenantId: scope.tenantId, workspaceId: scope.workspaceId },
        id,
        signal,
      ),
    [api, run.runId, scope.tenantId, scope.workspaceId],
  );
  useEffect(() => {
    if (!waitId) return;
    const controller = new AbortController();
    void api
      .request(`analysis/runs/${run.runId}/waits/${waitId}?${scopeQuery}`, "GET", undefined, {
        signal: controller.signal,
        retainOnNotFound: true,
      })
      .then((value) => {
        if (!controller.signal.aborted) setWait(parseModelContract("WaitView", value));
      })
      .catch(() => {
        if (!controller.signal.aborted) setWait(null);
      });
    return () => controller.abort();
  }, [api, run.runId, run.run?.version, scopeQuery, waitId, waitRefresh]);
  const [clock, setClock] = useState(0);
  const pendingExpiration = approval?.status === "pending" ? approval.expiresAt : undefined;
  useEffect(() => {
    if (!pendingExpiration) return;
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [pendingExpiration]);
  const value = run.run;
  const expired = !!approval?.expiresAt && Date.parse(approval.expiresAt) <= clock;
  const reason = value?.reasonCode && reasons[value.reasonCode];
  async function command(kind: string, extra: Record<string, unknown> = {}) {
    if (busy || !value) return;
    if (
      !pending.current ||
      pending.current.kind !== kind ||
      (extra.decision &&
        (pending.current.body.decision !== extra.decision ||
          pending.current.body.choiceId !== extra.choiceId ||
          pending.current.body.customDefinition !== extra.customDefinition ||
          pending.current.body.waitId !== extra.waitId))
    )
      pending.current = {
        kind,
        body: {
          ...scope,
          requestId: crypto.randomUUID(),
          expectedVersion: value.version,
          ...extra,
        },
      };
    setBusy(true);
    setError("");
    try {
      await requestDurableControl(api, sessionId, run.runId, kind, pending.current.body);
      if (kind === "decisions" && waitId) setResolvedWait(waitId);
      pending.current = null;
      reload();
      return true;
    } catch (failure) {
      if (failure instanceof ApiError && [409, 410].includes(failure.status)) {
        pending.current = null;
        if (kind === "decisions") setWaitRefresh((n) => n + 1);
        reload();
      }
      setError(failure instanceof Error ? failure.message : "操作未确认，请重试。");
    } finally {
      setBusy(false);
    }
  }
  const revisionChanges = wait?.revisionPreview?.changes.map(({ field, before, after }) => ({
    before,
    after: ((edits?.wait === waitId ? edits?.data[field] : undefined) ?? after).trim(),
  }));
  const revisionReady =
    revisionChanges?.every(({ after }) => after.length > 0) &&
    revisionChanges.some(({ before, after }) => before !== after);
  const canPause = value?.status === "running";
  const canResume = value?.status === "paused" && value.reasonCode !== "queued";
  const action =
    canPause || canResume ? (
      <UiButton
        variant="quiet"
        className="analysis-process-action"
        busy={busy}
        disabled={
          canResume && (value.recoveryStatus === "blocked" || value.cleanupStatus === "unconfirmed")
        }
        aria-label={canPause ? "暂停任务" : "继续任务"}
        onClick={() => command(canPause ? "pause" : "resume")}
      >
        {canPause ? "暂停" : "继续"}
      </UiButton>
    ) : undefined;

  return (
    <>
      <UiExecutionTimeline loadChild={loadChild} {...timeline} action={action} />
      {reason && <p className="analysis-run-control-note">{reason}</p>}
      {value?.status === "paused" && value.reasonCode && value.reasonCode !== "queued" && (
        <details className="analysis-run-control-note">
          <summary>暂停详情</summary>
          <p>
            错误码 <code>{value.reasonCode}</code>
          </p>
          <p>
            运行 ID <code>{run.runId}</code>
          </p>
        </details>
      )}
      {children}
      {value?.status === "completed" && <SemanticResultActions runId={run.runId} {...scope} />}
      {value &&
        approval?.status === "pending" &&
        approval.waitId !== resolvedWait &&
        !expired &&
        !["completed", "failed", "canceled"].includes(value.status) &&
        (wait?.waitId === approval.waitId &&
        (wait.semanticKind === "choice" || wait.semanticKind === "result") ? (
          <SemanticChoiceCard
            key={wait.waitId}
            kind={wait.semanticKind}
            options={wait.options ?? []}
            waitId={wait.waitId}
            expiresAt={approval.expiresAt}
            busy={busy}
            canApprove={wait.allowedActions.includes("approve")}
            canReject={wait.allowedActions.includes("reject")}
            onApprove={(answer) =>
              command("decisions", {
                waitId: approval.waitId,
                proposalId: approval.proposalId,
                proposalRevision: approval.proposalRevision,
                proposalDigest: approval.proposalDigest,
                decisionId: crypto.randomUUID(),
                decision: "approve",
                ...answer,
              })
            }
            onReject={() =>
              command("decisions", {
                waitId: approval.waitId,
                proposalId: approval.proposalId,
                proposalRevision: approval.proposalRevision,
                proposalDigest: approval.proposalDigest,
                decisionId: crypto.randomUUID(),
                decision: "reject",
              })
            }
          />
        ) : (
          <section className="durable-approval" aria-label="执行确认">
            <strong>{approval.goal ? "确认长任务目标" : "等待你的决定"}</strong>
            {approval.goal && (
              <div className="analysis-goal-proposal">
                <ul>
                  {approval.goal.contract.acceptance.map((item) => (
                    <li key={item.id}>{item.text}</li>
                  ))}
                </ul>
                {!!approval.goal.contract.constraints?.length && (
                  <p>约束：{approval.goal.contract.constraints.join("；")}</p>
                )}
                <p>
                  最多 {approval.goal.budget?.maxRounds ?? 12} 轮 ·{" "}
                  {(approval.goal.budget?.tokens ?? 2000000).toLocaleString()} Token · $
                  {((approval.goal.budget?.costMicrousd ?? 20000000) / 1000000).toFixed(2)}
                </p>
              </div>
            )}
            {scheduleDetail ? (
              <>
                <p className="durable-approval-summary">{scheduleDetail[1].trim()}</p>
                <details className="durable-approval-detail">
                  <summary>查看分析内容</summary>
                  <AnalysisMarkdown source={scheduleDetail[2].trim()} embedded />
                </details>
              </>
            ) : (
              <AnalysisMarkdown source={approval.summary} embedded />
            )}
            {approval.expiresAt && (
              <p className="durable-approval-expiry">
                有效期至 {new Date(approval.expiresAt).toLocaleString("zh-CN")}
              </p>
            )}
            {busy && !action && <p role="status">正在提交…</p>}
            {wait && wait.waitId === waitId && (
              <div className="actions">
                {wait.semanticKind === "revision" &&
                  (wait.revisionPreview ? (
                    <fieldset className="semantic-revision-preview" disabled={busy}>
                      <legend>修订「{wait.revisionPreview.name}」</legend>
                      <p>{wait.revisionPreview.code}</p>
                      {wait.revisionPreview.changes.map(({ field, before, after }) => (
                        <label key={field}>
                          <strong>{revisionLabels[field] ?? field}</strong>
                          <span>当前值 · {before || "未填写"}</span>
                          <span>修订为</span>
                          <textarea
                            aria-label={`修订后的${revisionLabels[field] ?? field}`}
                            className="ui-input"
                            rows={2}
                            maxLength={["summary", "formula"].includes(field) ? 2400 : 160}
                            value={
                              (edits?.wait === waitId ? edits?.data[field] : undefined) ?? after
                            }
                            onChange={(e) =>
                              setEdits({
                                wait: wait.waitId,
                                data: {
                                  ...(edits?.wait === waitId ? edits?.data : {}),
                                  [field]: e.target.value,
                                },
                              })
                            }
                          />
                        </label>
                      ))}
                    </fieldset>
                  ) : (
                    <p role="status">指标或维护权限已变化。请取消本次修订，重新发起修改。</p>
                  ))}
                {wait.semanticKind === "draft" && (
                  <fieldset>
                    <legend>指标草稿预览</legend>
                    {Object.entries({
                      name: "指标名称",
                      code: "指标代码",
                      domain: "业务域",
                      timezone: "时区",
                      aggregation: "聚合规则",
                      summary: "业务含义",
                      responsible: "负责人",
                      formula: "公式",
                      event_time: "业务时间",
                      grain: "统计粒度",
                      unit: "单位",
                    }).map(([key, label]) => (
                      <label key={key}>
                        {label}
                        <input
                          value={
                            (edits?.wait === wait.waitId ? edits.data[key] : undefined) ??
                            String(wait.fields?.[key] ?? "")
                          }
                          onChange={(e) =>
                            setEdits({
                              wait: wait.waitId,
                              data: {
                                ...(edits?.wait === wait.waitId ? edits.data : {}),
                                [key]: e.target.value,
                              },
                            })
                          }
                        />
                      </label>
                    ))}
                    {(["owner_scope_id", "usage_scope_id"] as const).map((key) => (
                      <label key={key}>
                        {key === "owner_scope_id" ? "维护归属" : "发布后谁可用"}
                        <select
                          value={String(
                            (edits?.wait === wait.waitId ? edits.data[key] : undefined) ??
                              wait.fields?.[key] ??
                              "",
                          )}
                          onChange={(e) =>
                            setEdits({
                              wait: wait.waitId,
                              data: {
                                ...(edits?.wait === wait.waitId ? edits.data : {}),
                                [key]: e.target.value,
                              },
                            })
                          }
                        >
                          <option value="">本人或当前归属</option>
                          {wait.scopes
                            ?.filter((s) => (key === "owner_scope_id" ? s.manage : s.share))
                            .map((s) => (
                              <option key={String(s.id)} value={String(s.id)}>
                                {String(s.label)}
                              </option>
                            ))}
                        </select>
                      </label>
                    ))}
                  </fieldset>
                )}
                {wait.allowedActions.includes("approve") && (
                  <button
                    disabled={busy || (wait.semanticKind === "revision" && !revisionReady)}
                    onClick={() =>
                      command("decisions", {
                        waitId: approval.waitId,
                        proposalId: approval.proposalId,
                        proposalRevision: approval.proposalRevision,
                        proposalDigest: approval.proposalDigest,
                        decisionId: crypto.randomUUID(),
                        decision: "approve",
                        ...((wait.semanticKind === "draft" || wait.semanticKind === "revision") &&
                        edits?.wait === wait.waitId
                          ? { draftEdits: edits.data }
                          : {}),
                      })
                    }
                  >
                    {approval.goal
                      ? "确认并开始"
                      : wait.semanticKind === "draft"
                        ? "确认保存草稿"
                        : wait.semanticKind === "revision"
                          ? "确认保存修订"
                          : "批准执行"}
                  </button>
                )}
                {wait.allowedActions.includes("reject") && (
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={() =>
                      command("decisions", {
                        waitId: approval.waitId,
                        proposalId: approval.proposalId,
                        proposalRevision: approval.proposalRevision,
                        proposalDigest: approval.proposalDigest,
                        decisionId: crypto.randomUUID(),
                        decision: "reject",
                      })
                    }
                  >
                    {wait.semanticKind === "draft"
                      ? "暂不保存"
                      : wait.semanticKind === "revision"
                        ? "取消修订"
                        : "拒绝执行"}
                  </button>
                )}
                {approval.goal && editGoal && wait.allowedActions.includes("reject") && (
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={async () => {
                      const accepted = await command("decisions", {
                        waitId: approval.waitId,
                        proposalId: approval.proposalId,
                        proposalRevision: approval.proposalRevision,
                        proposalDigest: approval.proposalDigest,
                        decisionId: crypto.randomUUID(),
                        decision: "reject",
                      });
                      if (accepted) editGoal(approval.goal!.contract.objective);
                    }}
                  >
                    修改目标
                  </button>
                )}
                {wait.allowedActions.includes("reconcile") && (
                  <button
                    disabled={busy}
                    onClick={() =>
                      command("reconcile", {
                        waitId: approval.waitId,
                        actionId: approval.actionId,
                      })
                    }
                  >
                    核对执行结果
                  </button>
                )}
              </div>
            )}
          </section>
        ))}
      {Object.values(run.objects)
        .filter((o) => o.kind === "artifact")
        .map(({ value: artifact }) => {
          const href = `/api/web/v1/analysis/runs/${run.runId}/artifacts/${artifact.id}?${new URLSearchParams(scope)}`;
          if (artifact.mediaType === "application/json" || artifact.mediaType === "text/markdown") {
            return (
              <DeliveryAttachment
                key={artifact.id}
                href={href}
                name={artifact.name}
                digest={artifact.sha256}
                dashboard={artifact.mediaType === "application/json"}
              />
            );
          }
          if (artifact.mediaType === "image/png")
            return (
              <figure className="analysis-chart" key={artifact.id}>
                <Image src={href} unoptimized width={768} height={512} alt="Python 分析图表" />
                <figcaption>
                  <span>分析图表 · PNG · {Math.ceil(artifact.byteSize / 1024)} KiB</span>
                  <a href={href} download={artifact.name}>
                    下载图表
                  </a>
                </figcaption>
              </figure>
            );
          return (
            <p key={artifact.id}>
              <a href={href} download={artifact.name}>
                下载数据 · {artifact.name}
              </a>
            </p>
          );
        })}
      {error && (
        <p role="alert">
          {error}
          <button
            className="secondary"
            disabled={busy}
            onClick={() => pending.current && command(pending.current.kind)}
          >
            重试操作
          </button>
          <button className="secondary" onClick={reload}>
            刷新状态
          </button>
        </p>
      )}
    </>
  );
}
