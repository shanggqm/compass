import { beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("server-only", () => ({}));
const mocks = vi.hoisted(() => ({ authorize: vi.fn(), upstream: vi.fn() }));
vi.mock("../../src/server/composition", () => ({
  execute: async (
    _token: string,
    _mode: string,
    work: (services: { models: { authorize: typeof mocks.authorize } }) => Promise<unknown>,
  ) => ({ value: await work({ models: { authorize: mocks.authorize } }), viewRevision: "1" }),
}));
vi.mock("../../src/server/agent-api", () => ({ requestModelAgent: mocks.upstream }));
import { handleMcpRequest } from "../../src/server/http/mcp-handler";
import { handleWebRequest } from "../../src/server/http/handler";
import { ApplicationError } from "../../src/server/platform/errors";
import { csrfToken } from "../../src/server/http/security";

const tenant = "10000000-0000-4000-8000-000000000001";
const workspace = "20000000-0000-4000-8000-000000000002";
const token = "a".repeat(43);
const query = `tenantId=${tenant}&workspaceId=${workspace}`;
const command = { revision: 0, name: "我的 MCP", endpoint: "https://fixture.example/mcp" };
function request(method: string, body?: unknown, headers: Record<string, string> = {}) {
  return new Request(`https://compass.example/api/mcp/connections?${query}`, {
    method,
    headers: {
      cookie: `__Host-compass-session=${token}`,
      origin: "https://compass.example",
      "content-type": "application/json",
      "x-csrf-token": csrfToken(token),
      ...headers,
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
}
beforeEach(() => {
  vi.stubEnv("WEB_PUBLIC_ORIGIN", "https://compass.example");
  vi.clearAllMocks();
  mocks.authorize.mockResolvedValue({
    actor_user_id: "current-session-user",
    scope: { kind: "tenant", tenant_id: tenant, workspace_id: workspace },
    purpose: "mcp_manage",
  });
  mocks.upstream.mockResolvedValue(Response.json({ items: [] }));
});
describe("personal MCP BFF", () => {
  it("serves the analysis tool selector through the Web API with the same authorization", async () => {
    const original = request("GET");
    const response = await handleWebRequest(
      new Request(original.url.replace("/api/mcp/", "/api/web/v1/mcp/"), original),
    );
    expect(response.status).toBe(200);
    expect(mocks.authorize).toHaveBeenCalledWith(token, {
      kind: "tenant",
      purpose: "mcp_manage",
      tenantId: tenant,
      workspaceId: workspace,
    });
    expect(mocks.upstream).toHaveBeenCalledWith(
      expect.objectContaining({ path: "/v1/mcp/connections", schema: "McpConnectionList" }),
    );
  });
  it("derives the actor through session authorization and forwards the personal purpose", async () => {
    expect((await handleMcpRequest(request("GET"))).status).toBe(200);
    expect(mocks.authorize).toHaveBeenCalledWith(token, {
      kind: "tenant",
      purpose: "mcp_manage",
      tenantId: tenant,
      workspaceId: workspace,
    });
    expect(mocks.upstream).toHaveBeenCalledWith(
      expect.objectContaining({
        path: "/v1/mcp/connections",
        schema: "McpConnectionList",
        context: expect.objectContaining({
          actor_user_id: "current-session-user",
          purpose: "mcp_manage",
        }),
      }),
    );
  });
  it("blocks cross-origin writes and missing CSRF before making an Agent request", async () => {
    for (const headers of <Record<string, string>[]>[
      { origin: "https://attacker.example" },
      { "x-csrf-token": "" },
    ]) {
      expect((await handleMcpRequest(request("POST", command, headers))).status).toBe(403);
    }
    expect(mocks.upstream).not.toHaveBeenCalled();
  });
  it("rejects forged identity fields while allowing a schema-valid connection", async () => {
    expect(
      (await handleMcpRequest(request("POST", { ...command, actor_user_id: "other" }))).status,
    ).toBe(422);
    expect(mocks.upstream).not.toHaveBeenCalled();
    expect((await handleMcpRequest(request("POST", command))).status).toBe(200);
    expect(mocks.upstream).toHaveBeenCalledWith(
      expect.objectContaining({
        method: "POST",
        input: command,
        schema: "McpConnectionView",
      }),
    );
  });
  it("does not call Agent after access revocation", async () => {
    mocks.authorize.mockRejectedValue(new ApplicationError("forbidden", "禁止访问"));
    expect((await handleMcpRequest(request("GET"))).status).toBe(403);
    expect(mocks.upstream).not.toHaveBeenCalled();
  });
});
