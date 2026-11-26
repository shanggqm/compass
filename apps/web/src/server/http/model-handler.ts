import "server-only";
import { publicLangfuseProjectUrl } from "./langfuse-url";
import { z } from "zod";
import { uuid } from "../../contracts/web";
import { isModelContract } from "../../contracts/model-contract";
import { execute, recordFailure } from "../composition";
import { requestModelAgent, modelErrorTraceHeaders } from "../agent-api";
import type { ModelScope, TrustedModelContext } from "../modules/models/public";
import { ApplicationError, reject } from "../platform/errors";
import { errorResponse, readJson } from "./responses";
import { requireCsrf, requireSameOrigin } from "./security";

const empty = z.strictObject({});
const scope = z.strictObject({ tenantId: uuid, workspaceId: uuid });
const page = z.strictObject({
  page: z.coerce.number().int().min(1).max(100000).default(1),
  page_size: z.coerce.number().int().min(1).max(100).default(20),
});
function business(schema: string, value: unknown): unknown {
  if (!isModelContract(schema, value)) reject("validation_failed");
  return value;
}
function queryString(value: Record<string, string | number>) {
  return `?${new URLSearchParams(Object.entries(value).map(([key, item]) => [key, String(item)]))}`;
}
export function isModelRoute(route: string) {
  return (
    route === "models" ||
    route.startsWith("models/") ||
    route === "platform/models" ||
    route.startsWith("platform/models/")
  );
}

/** HTTP validation and proxying are deliberately outside execute's retried DB callback. */
export async function handleModelRequest(request: Request, token: string): Promise<Response> {
  let authorizationFailed = false,
    context: TrustedModelContext | undefined,
    revision: string | undefined;
  let targetId: string | undefined;
  const audit = (error: unknown, requestId?: string) =>
    recordFailure("models.request", error, context?.actor_user_id, { targetId, requestId });
  try {
    const url = new URL(request.url),
      route = url.pathname.slice("/api/web/v1/".length),
      segments = route.split("/");
    const method = request.method;
    if (method !== "GET") {
      requireSameOrigin(request);
      requireCsrf(request, token);
    }
    const query: Record<string, string> = {};
    for (const [key, value] of url.searchParams) {
      if (Object.hasOwn(query, key)) reject("validation_failed");
      query[key] = value;
    }
    let access: ModelScope = { kind: "platform", purpose: "manage" };
    let path: string | undefined,
      schema = "",
      input: unknown,
      callId: string | undefined,
      stream = false;
    if (route === "platform/models/observability" && method === "GET") {
      empty.parse(query);
      path = "/observability";
      schema = "ObservabilityStatus";
    } else if (route === "platform/models/observability/summary" && method === "GET") {
      const value = z
        .strictObject({
          provider_ids: z.string().max(80),
          window: z.enum(["1h", "24h", "7d"]).default("24h"),
        })
        .parse(query);
      path = `/observability/summary${queryString(value)}`;
      schema = "ModelComparison";
    } else if (route === "platform/models/reliability/scenarios" && method === "GET") {
      empty.parse(query);
      path = "/reliability/scenarios";
      schema = "ShowcaseCatalog";
    } else if (route === "platform/models/reliability/runs" && method === "POST") {
      empty.parse(query);
      input = business("ShowcaseInput", await readJson(request));
      callId = (input as { call_id: string }).call_id;
      path = "/reliability/runs";
      schema = "ShowcaseReport";
      stream = true;
    } else if (route === "platform/models/catalog" && method === "GET") {
      const value = page
        .extend({ q: z.string().max(120).default(""), vendor: z.string().max(80).optional() })
        .parse(query);
      path = `/catalog${queryString(Object.fromEntries(Object.entries(value).filter(([, v]) => v !== undefined)) as Record<string, string | number>)}`;
      schema = "CatalogPage";
    } else if (route === "platform/models" && method === "GET") {
      const value = page
        .extend({
          q: z.string().max(120).default(""),
          kind: z.enum(["llm", "decision", "unsupported"]).optional(),
        })
        .parse(query);
      path = `/providers${queryString(Object.fromEntries(Object.entries(value).filter(([, v]) => v !== undefined)) as Record<string, string | number>)}`;
      schema = "ProviderPage";
    } else if (route === "platform/models" && method === "POST") {
      empty.parse(query);
      input = business("ProviderCreate", await readJson(request));
      path = "/providers";
      schema = "SavedProvider";
    } else if (route === "models/available" && method === "GET") {
      const { view, ...value } = scope.extend({ view: z.enum(["chat", "module"]) }).parse(query);
      access = { kind: "tenant", purpose: view === "chat" ? "chat" : "module_read", ...value };
      path = `/available${queryString({ view })}`;
      schema = "AvailableModels";
    } else if (
      segments.length === 3 &&
      segments[0] === "models" &&
      segments[1] === "calls" &&
      method === "GET"
    ) {
      const value = scope.parse(query);
      callId = uuid.parse(segments[2]);
      access = { kind: "tenant", purpose: "chat", ...value };
      path = `/calls/${callId}`;
      schema = "CallRecordView";
    } else if (segments[0] === "platform" && segments[1] === "models") {
      if (
        segments.length === 4 &&
        ["operations", "calls"].includes(segments[2]) &&
        method === "GET"
      ) {
        empty.parse(query);
        const id = uuid.parse(segments[3]);
        if (segments[2] === "operations") {
          path = `/operations/${id}`;
          schema = "SavedProvider";
        } else {
          access = { kind: "platform", purpose: "test" };
          path = `/test-calls/${id}`;
          callId = id;
          schema = "CallRecordView";
        }
      } else if (segments.length === 3 && ["GET", "PATCH"].includes(method)) {
        empty.parse(query);
        targetId = uuid.parse(segments[2]);
        path = `/providers/${targetId}`;
        if (method === "PATCH") {
          input = business("ProviderPatch", await readJson(request));
          schema = "SavedProvider";
        } else schema = "ProviderView";
      } else if (segments.length === 4 && segments[3] === "changes" && method === "GET") {
        const value = page.parse(query);
        targetId = uuid.parse(segments[2]);
        path = `/providers/${targetId}/changes${queryString(value)}`;
        schema = "ModelChangePage";
      } else if (segments.length === 4 && segments[3] === "tests" && method === "POST") {
        empty.parse(query);
        targetId = uuid.parse(segments[2]);
        input = business("TestCallInput", await readJson(request, 2 * 1024 * 1024));
        const command = input as { call_id: string; input: { stream?: boolean } };
        callId = command.call_id;
        stream = command.input.stream === true;
        access = { kind: "platform", purpose: "test" };
        path = `/providers/${targetId}/tests`;
        schema = "CallResponse";
      }
    }
    if (!path) reject("not_found");
    const authorized = await execute(token, "query", ({ models }) =>
      models.authorize(token, access),
    ).catch((error: unknown) => {
      authorizationFailed = true;
      throw error;
    });
    context = authorized.value;
    revision = authorized.viewRevision;
    const response = await requestModelAgent({
      path: `/v1/models${path}`,
      method: method as "GET" | "POST" | "PATCH",
      input,
      schema,
      context,
      revision,
      signal: request.signal,
      callId,
      providerId: callId ? targetId : undefined,
      stream,
      onStreamFailure: audit,
    });
    if (route === "platform/models/observability") {
      return Response.json(
        { ...(await response.json()), project_url: publicLangfuseProjectUrl() },
        { headers: response.headers },
      );
    }
    return response;
  } catch (error) {
    const failure =
      error instanceof z.ZodError
        ? new ApplicationError("validation_failed", "请检查输入字段与范围。")
        : error;
    const response = errorResponse(failure);
    for (const [key, value] of Object.entries(modelErrorTraceHeaders(failure)))
      response.headers.set(key, value);
    if (revision) response.headers.set("X-Compass-View-Revision", revision);
    if (!authorizationFailed) {
      const result = (await response.clone().json()) as { error: { requestId: string } };
      await audit(failure, result.error.requestId);
    }
    return response;
  }
}
