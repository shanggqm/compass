import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { components } from "../../src/generated/api";

const mocks = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock("server-only", () => ({}));
vi.mock("../../src/server/composition", () => ({
  execute: async (
    _token: string,
    _kind: string,
    action: (services: unknown) => Promise<unknown>,
  ) => ({
    value: await action({
      models: { authorize: async () => ({ scope: { kind: "tenant" } }) },
      queries: { session: async () => ({ isPlatformAdmin: true }) },
    }),
    viewRevision: "revision",
  }),
}));
vi.mock("../../src/server/agent-transport", () => ({
  fetchAgent: mocks.fetch,
  boundedJson: (response: Response) => response.json(),
}));
import { handleContextOps } from "../../src/server/http/contextops-handler";

const id = "97100000-0000-4000-8000-000000000001";
const trace = "a".repeat(32);
function flow(): components["schemas"]["ContextRunFlow"] {
  return {
    run: {
      run_id: id,
      session_id: id,
      actor_id: id,
      session_title: "运行",
      status: "completed",
      scene_id: null,
      scene_name: null,
      category_name: null,
      created_at: "2026-11-25T00:00:00Z",
      step_count: 1,
      duration_ms: 10,
      total_tokens: 20,
      token_upper_bound: 40,
      omission_rate: 0,
      compaction_applied: false,
      security_blocked_count: 0,
      trace_id: trace,
      historical: false,
    },
    nodes: [],
    budget_method: "utf8_upper_bound",
    langfuse_url: `http://langfuse-web:3000/trace/${trace}`,
    langfuse_reason: null,
  };
}
async function read(value = flow()) {
  mocks.fetch.mockResolvedValue(Response.json(value));
  return handleContextOps(
    new Request(`http://localhost/api/web/v1/contextops/runs/${id}/flow`),
    "local-test-session",
    `contextops/runs/${id}/flow`,
    { tenantId: id, workspaceId: id },
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.stubEnv("COMPASS_AGENT_SERVICE_TOKEN", "isolated-test-service-token");
  vi.stubEnv("COMPASS_LANGFUSE_PROJECT_URL", "http://127.0.0.1:8104/project/local");
});
afterEach(() => vi.unstubAllEnvs());

it("links the exact trace in the public project and keeps the response private", async () => {
  const response = await read();
  expect(response.headers.get("cache-control")).toBe("private, no-store");
  const value = await response.json();
  expect(value.langfuse_url).toBe(`http://127.0.0.1:8104/project/local/traces/${trace}`);
  expect(JSON.stringify(value)).not.toContain("langfuse-web:3000");
});

it("retains an unavailable export state even with a configured public project", async () => {
  const value = flow();
  value.langfuse_url = null;
  value.langfuse_reason = "Langfuse 导出暂时不可用，本地观测仍可查看";
  const response = await read(value);
  expect(await response.json()).toMatchObject({
    langfuse_url: null,
    langfuse_reason: value.langfuse_reason,
  });
});

it.each(["", "https://name:secret@example.com/project/local", "http://internal/project/local"])(
  "disables the entry for unsafe or missing public configuration: %s",
  async (project) => {
    vi.stubEnv("COMPASS_LANGFUSE_PROJECT_URL", project);
    const response = await read();
    const value = await response.json();
    expect(value.langfuse_url).toBeNull();
    expect(value.langfuse_reason).toBe("未配置 Langfuse 公开项目地址");
    expect(JSON.stringify(value)).not.toContain("langfuse-web:3000");
  },
);
