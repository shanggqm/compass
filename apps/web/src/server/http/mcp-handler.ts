import "server-only";
import { z } from "zod";
import { isModelContract } from "../../contracts/model-contract";
import { uuid } from "../../contracts/web";
import { requestModelAgent } from "../agent-api";
import { execute } from "../composition";
import { reject } from "../platform/errors";
import { errorResponse, readJson } from "./responses";
import { requireCsrf, requireSameOrigin, tokenFromCookie } from "./security";

const scope = z.strictObject({ tenantId: uuid, workspaceId: uuid });

export async function handleMcpRequest(request: Request): Promise<Response> {
  try {
    const url = new URL(request.url);
    const prefix = url.pathname.startsWith("/api/web/v1/mcp/") ? "/api/web/v1/mcp/" : "/api/mcp/";
    const parts = url.pathname.slice(prefix.length).split("/");
    const [resource, identifier, action] = parts;
    if (resource !== "connections") reject("not_found");
    const method = request.method;
    let path = "/v1/mcp/connections";
    let inputSchema: string | undefined;
    let responseSchema: string;
    if (parts.length === 1 && method === "GET") responseSchema = "McpConnectionList";
    else if (parts.length === 1 && method === "POST") {
      inputSchema = "McpConnectionSave";
      responseSchema = "McpConnectionView";
    } else if (parts.length === 2 && method === "PATCH") {
      path += `/${uuid.parse(identifier)}`;
      inputSchema = "McpConnectionSave";
      responseSchema = "McpConnectionView";
    } else if (parts.length === 3 && action === "test" && method === "POST") {
      path += `/${uuid.parse(identifier)}/test`;
      responseSchema = "McpDiscoveryResult";
    } else reject("not_found");
    const token = tokenFromCookie(request.headers.get("cookie"));
    if (method !== "GET") {
      requireSameOrigin(request);
      requireCsrf(request, token);
    }
    const query: Record<string, string> = {};
    for (const [key, value] of url.searchParams) {
      if (Object.hasOwn(query, key)) reject("validation_failed");
      query[key] = value;
    }
    const target = scope.parse(query);
    const authorization = await execute(token, "query", ({ models }) =>
      models.authorize(token, { kind: "tenant", purpose: "mcp_manage", ...target }),
    );
    const input = method === "GET" ? undefined : await readJson(request, 64 * 1024);
    if (inputSchema && !isModelContract(inputSchema, input)) reject("validation_failed");
    if (method !== "GET" && !inputSchema) z.strictObject({}).parse(input);
    return await requestModelAgent({
      path,
      method,
      input,
      schema: responseSchema,
      context: authorization.value,
      revision: authorization.viewRevision,
      signal: request.signal,
      timeoutMs: 12_000,
    });
  } catch (error) {
    return errorResponse(error);
  }
}
