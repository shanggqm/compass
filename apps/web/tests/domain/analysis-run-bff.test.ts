import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  execute: vi.fn(),
  audit: vi.fn(),
  authorize: vi.fn(),
  fetch: vi.fn(),
  committed: false,
}));

vi.mock("server-only", () => ({}));
vi.mock("../../src/server/composition", () => ({
  execute: mocks.execute,
  recordFailure: mocks.audit,
}));

import {
  encodeRunPacket,
  RUN_STREAM_PROTOCOL,
  RUN_STREAM_LIMITS,
  RUN_STREAM_VERSION,
  type RunEvent,
} from "../../src/contracts/analysis-run";
import { handleAnalysisRunRequest } from "../../src/server/http/analysis-run-handler";
import { handleDurableAnalysis } from "../../src/server/http/durable-analysis-handler";
import { csrfToken } from "../../src/server/http/security";
import { ApplicationError } from "../../src/server/platform/errors";

const token = "a".repeat(43);
const origin = "http://127.0.0.1:8100";
const id = (n: number) => `98900000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const scope = { tenantId: id(1), workspaceId: id(2) };
const requestId = id(3);
const runId = id(4);
const stepId = id(5);
const modelCallId = id(6);
const startInput = {
  ...scope,
  requestId,
  question: "比较两个时段的渠道实付金额。",
  history: [{ question: "上一问", answer: "上一答" }],
  selection: { mode: "auto" as const },
};
const start: RunEvent = { type: "run.started", payload: {} };
const step: RunEvent = {
  type: "step.started",
  payload: { stepId, stepIndex: 1, modelCallId },
};
const stepDone: RunEvent = {
  type: "step.finished",
  payload: { stepId, status: "completed" },
};
const terminal: RunEvent = {
  type: "run.finished",
  payload: { status: "completed", finalAnswer: "完成。", file: { status: "none" } },
};

function frames(events: RunEvent[]) {
  return events.map((event, index) =>
    encodeRunPacket({
      protocol: RUN_STREAM_PROTOCOL,
      version: RUN_STREAM_VERSION,
      requestId,
      runId,
      seq: index + 1,
      event,
    }),
  );
}

function stream(chunks: Uint8Array[]) {
  const iterator = chunks.values();
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      const { done, value } = iterator.next();
      if (done) controller.close();
      else controller.enqueue(value);
    },
  });
}

function upstream(events: RunEvent[]) {
  return new Response(stream(frames(events)), {
    headers: { "content-type": "text/event-stream; charset=utf-8" },
  });
}

function request(
  path: string,
  body: unknown,
  headers: Record<string, string> = {},
  signal?: AbortSignal,
) {
  return handleAnalysisRunRequest(
    new Request(`${origin}/api/web/v1/${path}`, {
      method: "POST",
      headers: {
        origin,
        "content-type": "application/json",
        "x-csrf-token": csrfToken(token),
        ...headers,
      },
      body: JSON.stringify(body),
      signal,
    }),
    token,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubEnv("WEB_PUBLIC_ORIGIN", origin);
  vi.stubEnv("WEB_AUTH_ALLOW_LOCAL_HTTP", "true");
  vi.stubEnv("COMPASS_AGENT_SERVICE_TOKEN", "private-service-token");
  vi.stubEnv("AGENT_API_URL", "http://agent.test");
  vi.stubGlobal("fetch", mocks.fetch);
  mocks.committed = false;
  mocks.audit.mockResolvedValue(undefined);
  mocks.authorize.mockImplementation(async (_token, value) => ({
    actor_user_id: id(7),
    scope: {
      kind: "tenant",
      tenant_id: value.tenantId,
      workspace_id: value.workspaceId,
    },
    purpose: value.purpose,
  }));
  mocks.execute.mockImplementation(async (_token, _mode, work) => {
    const value = await work({ models: { authorize: mocks.authorize } });
    mocks.committed = true;
    return { value, viewRevision: "a".repeat(64) };
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("Workflow deletion BFF", () => {
  function remove(csrf: string) {
    return handleAnalysisRunRequest(
      new Request(
        `${origin}/api/web/v1/analysis/workflows/${id(12)}?${new URLSearchParams(scope)}`,
        {
          method: "DELETE",
          headers: { origin, "content-type": "application/json", "x-csrf-token": csrf },
          body: JSON.stringify({ metadataRevision: 2 }),
        },
      ),
      token,
    );
  }

  it("requires CSRF and sends the expected revision with the authorized delete", async () => {
    expect((await remove("invalid")).status).toBe(403);
    expect(mocks.fetch).not.toHaveBeenCalled();
    mocks.fetch.mockResolvedValue(new Response(null, { status: 204 }));
    const response = await remove(csrfToken(token));
    expect(response.status).toBe(204);
    expect(response.headers.get("X-Compass-View-Revision")).toBe("a".repeat(64));
    expect(response.headers.get("Cache-Control")).toContain("no-store");
    expect(mocks.authorize).toHaveBeenCalledWith(token, {
      kind: "tenant",
      purpose: "chat",
      ...scope,
    });
    const [url, init] = mocks.fetch.mock.calls[0];
    expect(url.pathname).toBe(`/v1/analysis/workflows/${id(12)}`);
    expect(init.method).toBe("DELETE");
    expect(JSON.parse(init.body)).toEqual({ metadataRevision: 2 });
  });
});

describe("analysis Session management BFF", () => {
  const sessionId = id(12);
  const item = {
    sessionId,
    title: "渠道周报",
    titleStatus: "manual",
    sessionType: "chat",
    iconKey: "chat",
    version: 3,
    updatedAt: "2026-11-14T08:00:00Z",
    latestRunId: runId,
    executionStatus: "completed",
  };

  function sessionRequest(method: "GET" | "PATCH" | "DELETE", csrf = csrfToken(token)) {
    const path = `${origin}/api/web/v1/analysis/sessions/${sessionId}`;
    return handleDurableAnalysis(
      method === "GET"
        ? new Request(`${path}?${new URLSearchParams(scope)}`)
        : new Request(path, {
            method,
            headers: { origin, "content-type": "application/json", "x-csrf-token": csrf },
            body: JSON.stringify({
              ...scope,
              requestId,
              ...(method === "PATCH" ? { title: item.title } : {}),
            }),
          }),
      token,
    );
  }

  it("forwards the submitted constraints and rejects budgets above the Run limits", async () => {
    const submit = (constraints: unknown) =>
      handleDurableAnalysis(
        new Request(`${origin}/api/web/v1/analysis/sessions/submit`, {
          method: "POST",
          headers: { origin, "content-type": "application/json", "x-csrf-token": csrfToken(token) },
          body: JSON.stringify({
            ...scope,
            requestId,
            question: startInput.question,
            selection: { mode: "auto", provider_id: null },
            constraints,
          }),
        }),
        token,
      );
    expect((await submit({ maxSteps: RUN_STREAM_LIMITS.steps + 1 })).status).toBe(422);
    expect((await submit({ timeoutSeconds: RUN_STREAM_LIMITS.timeoutSeconds + 1 })).status).toBe(
      422,
    );
    expect(mocks.fetch).not.toHaveBeenCalled();
    mocks.fetch.mockResolvedValueOnce(
      Response.json(
        { error: { code: "invalid_parameters", message: "无效请求。", requestId } },
        { status: 422 },
      ),
    );
    expect((await submit({ verification: { rules: ["require_product_evidence"] } })).status).toBe(
      422,
    );
    expect(new URL(mocks.fetch.mock.calls[0][0]).pathname).toBe("/v1/analysis/sessions/retry");
    mocks.fetch.mockClear();

    const constraints = {
      maxSteps: 2,
      timeoutSeconds: 5,
    };
    mocks.fetch.mockResolvedValueOnce(
      Response.json(
        { sessionId, runId, streamUrl: `/v1/analysis/runs/${runId}/events` },
        { status: 202 },
      ),
    );
    expect((await submit(constraints)).status).toBe(202);
    expect(JSON.parse(mocks.fetch.mock.calls[0][1].body).constraints).toEqual(constraints);
  });

  it("reads and renames metadata through the owner-scoped Agent endpoint", async () => {
    mocks.fetch.mockImplementation(async (url, init) => {
      expect(String(url)).toBe(`http://agent.test/v1/analysis/sessions/${sessionId}`);
      expect(init.cache).toBe("no-store");
      if (init.method === "PATCH")
        expect(JSON.parse(init.body)).toEqual({ requestId, title: item.title });
      return Response.json(item);
    });
    expect(await (await sessionRequest("GET")).json()).toEqual(item);
    const renamed = await sessionRequest("PATCH");
    expect(await renamed.json()).toEqual(item);
    expect(renamed.headers.get("cache-control")).toBe("private, no-store");
    expect(mocks.authorize).toHaveBeenCalledWith(token, {
      kind: "tenant",
      purpose: "chat",
      ...scope,
    });
  });

  it("requires CSRF and returns the running-task deletion conflict", async () => {
    expect((await sessionRequest("DELETE", "invalid")).status).toBe(403);
    expect(mocks.fetch).not.toHaveBeenCalled();
    mocks.fetch.mockResolvedValueOnce(
      Response.json(
        { error: { code: "session_busy", message: "private", requestId } },
        { status: 409 },
      ),
    );
    const blocked = await sessionRequest("DELETE");
    expect(blocked.status).toBe(409);
    expect(await blocked.json()).toMatchObject({
      error: { code: "session_busy", message: "请先停止任务，等任务结束后再删除会话。" },
    });
    expect(JSON.parse(mocks.fetch.mock.calls[0][1].body)).toEqual({ requestId });
    mocks.fetch.mockResolvedValueOnce(Response.json({ sessionId }));
    expect(await (await sessionRequest("DELETE")).json()).toEqual({ sessionId });
  });
});

describe("analysis Run BFF", () => {
  it("commits both Chat permissions before forwarding only the trusted Agent command", async () => {
    mocks.fetch.mockImplementation(async (url, init) => {
      expect(mocks.committed).toBe(true);
      expect(String(url)).toBe("http://agent.test/v1/analysis/runs");
      expect(init.method).toBe("POST");
      expect(init.redirect).toBe("error");
      expect(init.cache).toBe("no-store");
      expect(init.headers.Authorization).toBe("Bearer private-service-token");
      expect(
        JSON.parse(Buffer.from(init.headers["X-Compass-Context"], "base64url").toString()),
      ).toEqual({
        actor_user_id: id(7),
        scope: { kind: "tenant", tenant_id: scope.tenantId, workspace_id: scope.workspaceId },
        purpose: "chat",
      });
      expect(JSON.parse(init.body)).toEqual({
        requestId,
        question: startInput.question,
        history: startInput.history,
        selection: { mode: "auto", provider_id: null },
      });
      expect(init.headers.Cookie).toBeUndefined();
      expect(init.headers.traceparent).toBeUndefined();
      return upstream([start, step, stepDone, terminal]);
    });

    const response = await request("analysis/runs", startInput, {
      authorization: "Bearer browser-forgery",
      "x-compass-context": "forged",
      traceparent: "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    });
    expect(mocks.authorize).toHaveBeenCalledWith(token, {
      kind: "tenant",
      purpose: "chat",
      ...scope,
    });
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toContain("private");
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(response.headers.get("content-type")).toContain("text/event-stream");
    expect(response.headers.get("x-accel-buffering")).toBe("no");
    expect(response.headers.get("x-compass-view-revision")).toBe("a".repeat(64));
    const output = await response.text();
    expect(output).toContain('"type":"run.started"');
    expect(output).toContain('"type":"run.finished"');
    expect(mocks.fetch).toHaveBeenCalledTimes(1);
  });

  it("rejects forged scope fields, oversized history, invalid origin, and missing sessions", async () => {
    const variants = [
      { body: { ...startInput, actor_user_id: id(9) } },
      {
        body: {
          ...startInput,
          history: [{ question: "q", answer: "x".repeat(33 * 1024) }],
        },
      },
      { body: startInput, headers: { origin: "https://foreign.test" } },
    ];
    for (const value of variants)
      expect(
        (await request("analysis/runs", value.body, value.headers)).status,
      ).toBeGreaterThanOrEqual(400);
    const anonymous = await handleAnalysisRunRequest(
      new Request(`${origin}/api/web/v1/analysis/runs`, {
        method: "POST",
        headers: { origin, "content-type": "application/json" },
        body: JSON.stringify(startInput),
      }),
      "",
    );
    expect(anonymous.status).toBe(401);
    expect(mocks.execute).not.toHaveBeenCalled();
    expect(mocks.fetch).not.toHaveBeenCalled();
  });

  it("does not open a downstream stream until the Agent first packet passes preflight", async () => {
    mocks.fetch.mockResolvedValue(
      new Response(stream(frames([step])), {
        headers: { "content-type": "text/event-stream" },
      }),
    );
    const response = await request("analysis/runs", startInput);
    expect(response.status).toBe(503);
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(await response.text()).not.toContain("step.started");
  });

  it("withholds the terminal and aborts upstream when EOF is not clean", async () => {
    const chunks = [...frames([start, step, stepDone, terminal]), new TextEncoder().encode("tail")];
    let upstreamSignal: AbortSignal | undefined;
    mocks.fetch.mockImplementation(async (_url, init) => {
      upstreamSignal = init.signal;
      return new Response(stream(chunks), {
        headers: { "content-type": "text/event-stream" },
      });
    });
    const response = await request("analysis/runs", startInput);
    const reader = response.body!.getReader();
    const seen: string[] = [];
    await expect(async () => {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        seen.push(new TextDecoder().decode(value));
      }
    }).rejects.toThrow();
    expect(seen.join("")).toContain("run.started");
    expect(seen.join("")).not.toContain("run.finished");
    expect(upstreamSignal?.aborted).toBe(true);
    expect(mocks.audit).toHaveBeenCalledWith(
      "analysis.run.stream",
      expect.anything(),
      id(7),
      expect.objectContaining({ requestId }),
    );
  });

  it.each([
    [202, { runId, status: "accepted" }],
    [200, { runId, status: "already_terminal", terminalStatus: "canceled" }],
  ])("validates and relays a %s cancellation acknowledgement", async (status, ack) => {
    mocks.fetch.mockImplementation(async (url, init) => {
      expect(String(url)).toBe(`http://agent.test/v1/analysis/runs/${runId}/cancel`);
      expect(init.body).toBe("{}");
      expect(
        JSON.parse(
          init.headers["X-Compass-Context"]
            ? Buffer.from(init.headers["X-Compass-Context"], "base64url").toString()
            : "{}",
        ),
      ).toMatchObject({ purpose: "chat" });
      return Response.json(ack, { status });
    });
    const response = await request(`analysis/runs/${runId}/cancel`, scope);
    expect(response.status).toBe(status);
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(await response.json()).toEqual(ack);
  });

  it("rejects mismatched cancellation acks and safely maps not-found and capacity errors", async () => {
    mocks.fetch.mockResolvedValueOnce(
      Response.json({ runId: id(99), status: "accepted" }, { status: 202 }),
    );
    expect((await request(`analysis/runs/${runId}/cancel`, scope)).status).toBe(503);
    mocks.fetch.mockResolvedValueOnce(
      Response.json(
        { error: { code: "not_found", message: "private owner", requestId: id(8) } },
        { status: 404 },
      ),
    );
    const missing = await request(`analysis/runs/${runId}/cancel`, scope);
    expect(missing.status).toBe(404);
    expect(await missing.text()).not.toContain("private owner");
    mocks.fetch.mockResolvedValueOnce(
      Response.json(
        { error: { code: "private", message: "secret", requestId: id(8) } },
        { status: 429 },
      ),
    );
    const full = await request("analysis/runs", startInput);
    expect(full.status).toBe(429);
    expect(full.headers.get("retry-after")).toBe("60");
    expect(await full.text()).not.toContain("secret");
  });

  it("aborts the Agent fetch and releases the reader when the browser cancels", async () => {
    let upstreamSignal: AbortSignal | undefined;
    const canceled = vi.fn();
    mocks.fetch.mockImplementation(async (_url, init) => {
      upstreamSignal = init.signal;
      return new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(frames([start])[0]);
          },
          cancel: canceled,
        }),
        { headers: { "content-type": "text/event-stream" } },
      );
    });
    const response = await request("analysis/runs", startInput);
    const reader = response.body!.getReader();
    await reader.read();
    await reader.cancel();
    expect(upstreamSignal?.aborted).toBe(true);
    expect(canceled).toHaveBeenCalledTimes(1);
  });

  it("propagates a request abort after headers and does not retry the Run", async () => {
    const browser = new AbortController();
    let upstreamSignal: AbortSignal | undefined;
    mocks.fetch.mockImplementation(async (_url, init) => {
      upstreamSignal = init.signal;
      return new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(frames([start])[0]);
            init.signal.addEventListener(
              "abort",
              () => controller.error(new Error("private cancellation")),
              { once: true },
            );
          },
        }),
        { headers: { "content-type": "text/event-stream" } },
      );
    });
    const response = await request("analysis/runs", startInput, {}, browser.signal);
    const reader = response.body!.getReader();
    await reader.read();
    browser.abort();
    await expect(reader.read()).rejects.toThrow();
    expect(upstreamSignal?.aborted).toBe(true);
    expect(mocks.fetch).toHaveBeenCalledTimes(1);
  });

  it("bounds an Agent start that never returns without retrying it", async () => {
    vi.useFakeTimers();
    try {
      mocks.fetch.mockImplementation(
        (_url, init) =>
          new Promise((_resolve, reject) => {
            init.signal.addEventListener("abort", () => reject(new Error("private timeout")), {
              once: true,
            });
          }),
      );
      const pending = request("analysis/runs", startInput);
      await vi.advanceTimersByTimeAsync((RUN_STREAM_LIMITS.timeoutSeconds + 15) * 1_000);
      const response = await pending;
      expect(response.status).toBe(503);
      expect(await response.text()).not.toContain("private timeout");
      expect(mocks.fetch).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("bounds an Agent cancellation that never returns without retrying it", async () => {
    vi.useFakeTimers();
    try {
      mocks.fetch.mockImplementation(
        (_url, init) =>
          new Promise((_resolve, reject) => {
            init.signal.addEventListener("abort", () => reject(new Error("private timeout")), {
              once: true,
            });
          }),
      );
      const pending = request(`analysis/runs/${runId}/cancel`, scope);
      await vi.advanceTimersByTimeAsync(5_000);
      const response = await pending;
      expect(response.status).toBe(503);
      expect(await response.text()).not.toContain("private timeout");
      expect(mocks.fetch).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps authorization failures inside the existing transaction audit", async () => {
    mocks.execute.mockRejectedValue(new ApplicationError("forbidden", "无权访问。"));
    const response = await request("analysis/runs", startInput);
    expect(response.status).toBe(403);
    expect(mocks.fetch).not.toHaveBeenCalled();
    expect(mocks.audit).not.toHaveBeenCalled();
  });
});

describe("goal and authorized child trace proxy", () => {
  it.each([
    ["GET", `analysis/sessions/${id(20)}/goal`, { goal: null, runId: runId }],
    ["POST", `analysis/sessions/${id(20)}/goal/actions`, { goal: null, runId: null }],
    [
      "GET",
      `analysis/runs/${runId}/children/${id(21)}`,
      { childRunId: id(21), seq: "1-0", nodes: [], answer: "完整回答".repeat(200) },
    ],
  ])("validates %s %s and disables caching", async (method, path, output) => {
    mocks.fetch.mockResolvedValue(
      new Response(JSON.stringify(output), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const input =
      method === "GET"
        ? new Request(`${origin}/api/web/v1/${path}?${new URLSearchParams(scope)}`)
        : new Request(`${origin}/api/web/v1/${path}`, {
            method,
            headers: {
              origin,
              "content-type": "application/json",
              "x-csrf-token": csrfToken(token),
            },
            body: JSON.stringify({ ...scope, action: "pause", expectedRevision: 1, requestId }),
          });
    const result = await handleDurableAnalysis(input, token);
    expect(result?.status).toBe(200);
    expect(result?.headers.get("cache-control")).toContain("no-store");
    expect(await result?.json()).toEqual(output);
    expect(mocks.authorize).toHaveBeenCalled();
  });
  it("rejects an invalid goal action before calling the Agent", async () => {
    const result = await handleDurableAnalysis(
      new Request(`${origin}/api/web/v1/analysis/sessions/${id(20)}/goal/actions`, {
        method: "POST",
        headers: { origin, "content-type": "application/json", "x-csrf-token": csrfToken(token) },
        body: JSON.stringify({ ...scope, action: "complete", expectedRevision: 0, requestId }),
      }),
      token,
    );
    expect(result?.status).toBe(422);
    expect(mocks.fetch).not.toHaveBeenCalled();
  });
});
