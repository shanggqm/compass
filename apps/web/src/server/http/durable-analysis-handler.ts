import "server-only";
import { z } from "zod";
import { uuid } from "../../contracts/web";
import { traceHeaders } from "../../contracts/trace-context";
import { agentResponse, readAgentResponses } from "../../contracts/agent-response";
import { isModelContract } from "../../contracts/model-contract";
import { execute } from "../composition";
import { reject } from "../platform/errors";
import { errorResponse, privateHeaders, readJson } from "./responses";
import { requireCsrf, requireSameOrigin } from "./security";

export function isDurableAnalysisRoute(route: string, request: Request) {
  return (
    route === "analysis/runtime/usage" ||
    /^analysis\/schedules(?:\/|$)/.test(route) ||
    /^analysis\/sessions(?:\/|$)/.test(route) ||
    /^analysis\/runs\/[^/]+\/(events|pause|resume|decisions|reconcile|waits|timeline|semantic-results|citations|children)(?:\/|$)/.test(
      route,
    ) ||
    (/^analysis\/runs\/[^/]+\/cancel$/.test(route) &&
      request.headers.get("x-compass-run-version") === "2")
  );
}
export async function handleDurableAnalysis(request: Request, token: string) {
  try {
    const url = new URL(request.url),
      route = url.pathname.slice("/api/web/v1/".length);
    const parts = route.split("/");
    const schedule = parts[1] === "schedules";
    let contract = "",
      result = "",
      streaming = false;
    if (schedule) {
      if (parts.length === 2 && request.method === "GET") result = "SchedulePage";
      else if (parts.length === 2 && request.method === "POST") {
        contract = "ScheduleCreate";
        result = "ScheduleWriteReceipt";
      } else if (parts.length === 3 && parts[2] === "preview" && request.method === "POST") {
        contract = "SchedulePreview";
        result = "SchedulePreviewResult";
      } else if (parts.length === 3) {
        uuid.parse(parts[2]);
        if (request.method === "GET") result = "ScheduleView";
        else if (request.method === "PATCH" || request.method === "DELETE") {
          contract = request.method === "PATCH" ? "ScheduleUpdate" : "ScheduleDelete";
          result = "ScheduleWriteReceipt";
        }
      } else if (parts.length === 4 && parts[3] === "triggers" && request.method === "GET") {
        uuid.parse(parts[2]);
        result = "TriggerPage";
      }
    } else if (route === "analysis/runtime/usage" && request.method === "GET")
      result = "RuntimeUsage";
    else if (route === "analysis/sessions" && request.method === "GET") result = "SessionList";
    else if (route === "analysis/sessions/submit" && request.method === "POST") {
      contract = "SessionSubmit";
      result = "SessionAccepted";
    } else if (parts.length === 3 && parts[1] === "sessions") {
      uuid.parse(parts[2]);
      if (request.method === "GET") result = "SessionListItem";
      else if (request.method === "PATCH") {
        contract = "SessionRename";
        result = "SessionListItem";
      } else if (request.method === "DELETE") {
        contract = "SessionMutation";
        result = "SessionDeleted";
      }
    } else if (parts.length === 4 && parts[1] === "sessions") {
      uuid.parse(parts[2]);
      if (parts[3] === "goal" && request.method === "GET") result = "SessionGoal";
      else if (parts[3] === "messages" && request.method === "GET") result = "AgentResponse";
      else if (parts[3] === "context-reset" && request.method === "POST") {
        contract = "ControlCommand";
        result = "ControlReceipt";
      }
    } else if (
      parts.length === 5 &&
      parts[1] === "sessions" &&
      parts[3] === "goal" &&
      parts[4] === "actions" &&
      request.method === "POST"
    ) {
      uuid.parse(parts[2]);
      contract = "GoalAction";
      result = "SessionGoal";
    } else if (parts[1] === "runs") {
      uuid.parse(parts[2]);
      if (parts[3] === "children" && request.method === "GET" && parts.length === 5) {
        uuid.parse(parts[4]);
        result = "ChildTraceSnapshot";
      } else if (parts[3] === "citations" && request.method === "GET" && parts.length === 4)
        result = "RunCitations";
      else if (parts[3] === "citations" && request.method === "GET" && parts.length === 5) {
        uuid.parse(parts[4]);
        result = "CitationDetail";
      } else if (
        parts.length === 4 &&
        parts[3] === "semantic-results" &&
        request.method === "POST"
      ) {
        contract = "SemanticResultDecision";
        result = "SemanticOpenResult";
      } else if (parts.length === 4 && parts[3] === "semantic-results" && request.method === "GET")
        result = "SemanticResults";
      else if (parts.length === 4 && parts[3] === "timeline" && request.method === "GET")
        result = "RuntimeTimeline";
      else if (parts.length === 4 && parts[3] === "events" && request.method === "GET") {
        result = "AgentResponse";
        streaming = true;
      } else if (
        parts.length === 4 &&
        ["pause", "resume", "cancel", "decisions", "reconcile"].includes(parts[3]) &&
        request.method === "POST"
      ) {
        contract =
          parts[3] === "decisions"
            ? "DecisionCommand"
            : parts[3] === "reconcile"
              ? "ReconcileCommand"
              : "ControlCommand";
        result = "ControlReceipt";
      } else if (parts.length === 5 && parts[3] === "waits" && request.method === "GET") {
        uuid.parse(parts[4]);
        result = "WaitView";
      }
    }
    if (!result) reject("not_found");
    if (request.method !== "GET") {
      requireSameOrigin(request);
      requireCsrf(request, token);
    }
    if (new Set(url.searchParams.keys()).size !== url.searchParams.size)
      reject("validation_failed");
    const raw =
      request.method === "GET" ? Object.fromEntries(url.searchParams) : await readJson(request);
    const scope = z
      .object({ tenantId: uuid, workspaceId: uuid })
      .parse(schedule ? Object.fromEntries(url.searchParams) : raw);
    const command = Object.fromEntries(
      Object.entries(raw as Record<string, unknown>).filter(
        ([key]) => !["tenantId", "workspaceId"].includes(key),
      ),
    );
    let historicalRetry = false;
    if (contract && !isModelContract(contract, command)) {
      const sanitized = { ...command };
      const hadCase = Object.hasOwn(sanitized, "showcase");
      delete sanitized.showcase;
      let hadVerification = false;
      if (sanitized.constraints && typeof sanitized.constraints === "object") {
        const constraints = { ...sanitized.constraints } as Record<string, unknown>;
        hadVerification = Object.hasOwn(constraints, "verification");
        delete constraints.verification;
        sanitized.constraints = Object.keys(constraints).length ? constraints : null;
      }
      historicalRetry =
        contract === "SessionSubmit" &&
        (hadCase || hadVerification) &&
        isModelContract(contract, sanitized);
      if (!historicalRetry) reject("validation_failed");
    }
    if (!contract) {
      const allowed =
        result === "SchedulePage"
          ? ["q", "status", "cursor"]
          : result === "TriggerPage"
            ? ["cursor"]
            : route === "analysis/sessions"
              ? ["limit", "cursor"]
              : parts[3] === "messages"
                ? ["cursor", "runId"]
                : streaming
                  ? ["after"]
                  : [];
      if (
        Object.keys(command).some((key) => !allowed.includes(key)) ||
        Object.values(command).some((value) => typeof value !== "string" || value.length > 500)
      )
        reject("validation_failed");
    }
    const { value: context, viewRevision } = await execute(token, "query", ({ models }) =>
      models.authorize(
        token,
        result === "RuntimeTimeline"
          ? { kind: "platform", purpose: "runtime_diagnostics" }
          : {
              kind: "tenant",
              purpose: schedule
                ? "schedule_manage"
                : result === "RuntimeUsage"
                  ? "runtime_usage"
                  : "chat",
              ...scope,
            },
      ),
    );
    const serviceToken = process.env.COMPASS_AGENT_SERVICE_TOKEN;
    if (!serviceToken) reject("unavailable");
    const target = new URL(
      `/v1/${historicalRetry ? "analysis/sessions/retry" : route}`,
      process.env.AGENT_API_URL ?? "http://127.0.0.1:8102",
    );
    if (!contract)
      for (const [key, value] of Object.entries(command))
        target.searchParams.set(key, String(value));
    const upstream = await fetch(target, {
      method: request.method,
      cache: "no-store",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(streaming ? 65000 : 15000)]),
      headers: {
        Authorization: `Bearer ${serviceToken}`,
        "X-Compass-Context": Buffer.from(JSON.stringify(context)).toString("base64url"),
        "Content-Type": "application/json",
      },
      ...(contract ? { body: JSON.stringify(command) } : {}),
    });
    const headers = {
      ...privateHeaders,
      ...traceHeaders(upstream.headers),
      "X-Compass-View-Revision": viewRevision,
    };
    if (!upstream.ok) {
      const body = await upstream.json().catch(() => null);
      if (
        upstream.status === 409 &&
        result === "ControlReceipt" &&
        isModelContract("ControlReceipt", body) &&
        body.code === "approval_expired"
      )
        return Response.json(body, { status: 409, headers });
      if (
        isModelContract("ModelErrorResponse", body) &&
        [403, 404, 409, 410, 422, 429].includes(upstream.status)
      )
        return Response.json(
          {
            error: {
              code: body.error.code,
              currentVersion: body.error.currentVersion,
              reasonCode: body.error.reasonCode,
              message: schedule
                ? body.error.message
                : body.error.code === "session_busy" && request.method === "DELETE"
                  ? "请先停止任务，等任务结束后再删除会话。"
                  : body.error.code === "invalid_session_title"
                    ? "请输入有效的会话标题。"
                    : upstream.status === 403
                      ? "当前权限不足。"
                      : upstream.status === 429
                        ? "当前任务已满，请稍后重试。你的问题已保留。"
                        : upstream.status === 404
                          ? "未找到会话或任务。"
                          : "任务状态已更新，请重新加载后操作。",
            },
          },
          {
            status: upstream.status,
            headers: {
              ...headers,
              ...(upstream.status === 429
                ? { "Retry-After": upstream.headers.get("Retry-After") ?? "1" }
                : {}),
            },
          },
        );
      reject("unavailable");
    }
    if (streaming) {
      if (!upstream.body || !upstream.headers.get("content-type")?.includes("text/event-stream"))
        reject("unavailable");
      const source = readAgentResponses(upstream.body, request.signal),
        encoder = new TextEncoder();
      const body = new ReadableStream<Uint8Array>({
        async pull(controller) {
          try {
            const item = await source.next();
            if (item.done) {
              controller.close();
              return;
            }
            if (item.value.cursors[0].runId !== parts[2]) throw new Error("Run mismatch");
            controller.enqueue(
              encoder.encode(
                `id: ${parts[2]}:${item.value.cursors[0].seq}\nevent: agent\ndata: ${JSON.stringify(item.value)}\n\n`,
              ),
            );
          } catch {
            controller.error(new Error("运行响应读取失败"));
          }
        },
        async cancel() {
          await source.return(undefined);
        },
      });
      return new Response(body, {
        headers: { ...headers, "Content-Type": "text/event-stream", "X-Accel-Buffering": "no" },
      });
    }
    const data = await readJson(
      new Request("http://internal/response", {
        method: "POST",
        body: upstream.body,
        duplex: "half",
      } as RequestInit),
      8 * 1024 * 1024,
    );
    if (!isModelContract(result, data)) reject("unavailable");
    if (result === "AgentResponse") agentResponse(data);
    return Response.json(data, { status: upstream.status, headers });
  } catch (error) {
    return errorResponse(error);
  }
}
