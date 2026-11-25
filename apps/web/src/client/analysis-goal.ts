import { useCallback, useEffect, useRef, useState } from "react";
import type { components } from "@/generated/api";
import { parseModelContract } from "@/contracts/model-contract";
import { reduceAgentResponse } from "@/contracts/agent-response";
import { terminalRun } from "./durable-analysis";
import { ApiError, type DocumentSession } from "./request-scope";

export type Goal = components["schemas"]["GoalView"];
type SessionGoal = components["schemas"]["SessionGoal"];
type Scope = { tenantId: string; workspaceId: string };

export function useAnalysisGoal(
  api: DocumentSession,
  sessionId: string | null,
  scope: Scope | undefined,
  latestRunId: string | undefined,
  reload: () => void,
) {
  const [state, setState] = useState<{ sessionId: string; goal: Goal | null }>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = useRef<{
    sessionId: string;
    action: string;
    revision: number;
    requestId: string;
  } | null>(null);
  const interrupting = useRef<AbortController | null>(null);
  const query = scope ? new URLSearchParams(scope).toString() : "";
  const latest = useRef(latestRunId);
  useEffect(() => {
    latest.current = latestRunId;
  }, [latestRunId]);
  useEffect(() => () => interrupting.current?.abort(), [sessionId]);
  useEffect(() => {
    if (!sessionId || !query) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const raw = await api.request(
          `analysis/sessions/${sessionId}/goal?${query}`,
          "GET",
          undefined,
          { signal: controller.signal, retainOnNotFound: true },
        );
        const value = parseModelContract("SessionGoal", raw) as SessionGoal;
        if (controller.signal.aborted) return;
        setState({ sessionId: sessionId!, goal: value.goal });
        if (value.goal && value.runId && latest.current && value.runId !== latest.current) {
          latest.current = value.runId;
          reload();
        }
      } catch (failure) {
        if (
          !controller.signal.aborted &&
          failure instanceof ApiError &&
          [403, 404].includes(failure.status)
        )
          setState({ sessionId: sessionId!, goal: null });
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, 2000);
      }
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [api, sessionId, query, reload]);
  const goal = state?.sessionId === sessionId ? state.goal : null;
  const act = useCallback(
    async (action: "pause" | "resume" | "cancel" | "interrupt") => {
      if (!sessionId || !goal || !scope) return;
      setBusy(true);
      setError("");
      if (
        pending.current?.sessionId !== sessionId ||
        pending.current.action !== action ||
        pending.current.revision !== goal.revision
      )
        pending.current = {
          sessionId,
          action,
          revision: goal.revision,
          requestId: crypto.randomUUID(),
        };
      try {
        const result = await api.request<SessionGoal>(
          `analysis/sessions/${sessionId}/goal/actions`,
          "POST",
          {
            ...scope,
            action,
            expectedRevision: goal.revision,
            requestId: pending.current.requestId,
          },
          { retainOnNotFound: true },
        );
        setState({ sessionId, goal: result.goal });
        pending.current = null;
        if (action !== "interrupt") reload();
        return result;
      } catch (failure) {
        if (failure instanceof ApiError && [403, 404, 409].includes(failure.status))
          pending.current = null;
        setError(
          failure instanceof ApiError && failure.status === 409
            ? "目标已在别处更新，请刷新后重试"
            : "目标操作失败，请重试。",
        );
        throw failure;
      } finally {
        setBusy(false);
      }
    },
    [api, goal, reload, scope, sessionId],
  );
  async function interrupt() {
    interrupting.current?.abort();
    const controller = new AbortController();
    interrupting.current = controller;
    const receipt = await act("interrupt");
    controller.signal.throwIfAborted();
    if (!receipt?.runId || !sessionId || !scope) return;
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      controller.signal.throwIfAborted();
      const value = reduceAgentResponse(
        null,
        await api.request(
          `analysis/sessions/${sessionId}/messages?${new URLSearchParams({ ...scope, runId: receipt.runId })}`,
          "GET",
          undefined,
          { signal: controller.signal },
        ),
      );
      const run = value.runs[receipt.runId];
      if (run && terminalRun(run)) return;
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
    throw new Error("当前轮仍在停止，请稍后重试。输入内容已保留。");
  }
  return { goal, busy, error, act, interrupt };
}
