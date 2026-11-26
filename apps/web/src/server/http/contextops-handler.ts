import "server-only";
import { z } from "zod";
import type { components } from "../../generated/api";
import { isModelContract } from "../../contracts/model-contract";
import { traceHeaders } from "../../contracts/trace-context";
import { uuid } from "../../contracts/web";
import { boundedJson, fetchAgent } from "../agent-transport";
import { execute } from "../composition";
import { ApplicationError, reject } from "../platform/errors";
import { privateHeaders, readJson } from "./responses";
import { requireCsrf, requireSameOrigin } from "./security";
import { publicLangfuseProjectUrl } from "./langfuse-url";

const scope = z.object({
  tenantId: uuid,
  workspaceId: uuid,
  admin: z.enum(["true", "false"]).optional(),
});
const counts = z.coerce.number().int().min(1).max(100).optional();
const filters = z.strictObject({
  window: z.enum(["24h", "7d", "30d"]).optional(),
  actor_id: uuid.optional(),
  session_id: uuid.optional(),
  scene_id: z.string().max(64).optional(),
  policy_digest: z
    .string()
    .regex(/^[0-9a-f]{64}$/)
    .optional(),
  status: z
    .enum([
      "created",
      "running",
      "waiting",
      "paused",
      "cancelling",
      "completed",
      "failed",
      "canceled",
    ])
    .optional(),
  has_security_event: z.enum(["true", "false"]).optional(),
  failed_stage: z.string().max(32).optional(),
  limit: counts,
  cursor: uuid.optional(),
});
const roots = z.enum([
  "source_data",
  "semantic",
  "retrieval",
  "ranking_cutoff",
  "compression_loss",
  "security_false_positive",
  "model_reasoning",
]);
const badcases = z.strictObject({
  root_cause_category: roots.optional(),
  scene_id: z.string().max(64).optional(),
  limit: counts,
});
const security = z.strictObject({
  window: z.enum(["24h", "7d", "30d"]).optional(),
  actor_id: uuid.optional(),
  event_type: z.string().max(32).optional(),
  action: z.string().max(16).optional(),
  limit: counts,
  cursor: uuid.optional(),
});
const reads: Record<string, string> = {
  overview: "ContextOverview",
  intents: "ContextIntents",
  anomalies: "ContextAnomalies",
  runs: "ContextRuns",
  badcases: "ContextBadcases",
  "security/events": "ContextSecurityEvents",
};

export async function handleContextOps(
  request: Request,
  token: string,
  route: string,
  query: Record<string, string>,
) {
  if (!token) reject("unauthenticated");
  const { tenantId, workspaceId, admin } = scope.parse(query);
  const params = Object.fromEntries(
    Object.entries(query).filter(([k]) => !["tenantId", "workspaceId", "admin"].includes(k)),
  );
  const suffix = route.slice("contextops/".length);
  let schema = "",
    inputSchema = "";
  if (request.method === "GET" && Object.hasOwn(reads, suffix)) {
    (suffix === "badcases" ? badcases : suffix === "security/events" ? security : filters).parse(
      params,
    );
    schema = reads[suffix];
  } else if (request.method === "GET" && /^runs\/[^/]+\/flow$/.test(suffix)) {
    uuid.parse(suffix.split("/")[1]);
    z.strictObject({}).parse(params);
    schema = "ContextRunFlow";
  } else if (request.method === "GET" && /^snapshots\/csnp_[0-9a-f]{32}$/.test(suffix)) {
    z.strictObject({}).parse(params);
    schema = "ContextSnapshotView";
  } else if (request.method === "POST" && suffix === "badcases") {
    schema = "ContextBadcaseView";
    inputSchema = "ContextBadcaseInput";
  } else if (request.method === "POST" && /^badcases\/[^/]+\/replay$/.test(suffix)) {
    uuid.parse(suffix.split("/")[1]);
    schema = "ContextReplayComparison";
    inputSchema = "ContextReplayInput";
  } else reject("not_found");
  let input: unknown;
  if (inputSchema) {
    requireSameOrigin(request);
    requireCsrf(request, token);
    z.strictObject({}).parse(params);
    input = await readJson(request);
    if (!isModelContract(inputSchema, input)) reject("validation_failed");
  }
  const granted = await execute(token, "query", async ({ models, queries }) => {
    const view = await queries.session(token, { tenantId, workspaceId });
    const manage = admin === "true" || (admin === undefined && view.isPlatformAdmin);
    return models.authorize(token, {
      kind: "tenant",
      tenantId,
      workspaceId,
      purpose: manage ? "contextops_admin" : "contextops_read",
    });
  });
  const unavailable = () => new ApplicationError("unavailable", "上下文观测暂时不可用，请重试。");
  const credential = process.env.COMPASS_AGENT_SERVICE_TOKEN;
  if (!credential) throw unavailable();
  try {
    const search = new URLSearchParams(params).toString();
    const response = await fetchAgent(
      {
        path: `/v1/contextops/${suffix}${search ? "?" + search : ""}`,
        context: granted.value,
        method: request.method,
        input,
      },
      credential,
      AbortSignal.any([request.signal, AbortSignal.timeout(15000)]),
    );
    if (!response.headers.get("content-type")?.includes("application/json")) {
      await response.body?.cancel();
      throw unavailable();
    }
    const value = await boundedJson(response, unavailable);
    const headers = {
      ...privateHeaders,
      ...traceHeaders(response.headers),
      "X-Compass-View-Revision": granted.viewRevision,
    };
    if (!response.ok) {
      if (
        ![403, 404, 409, 422].includes(response.status) ||
        !isModelContract("ModelErrorResponse", value)
      )
        throw unavailable();
      return Response.json(value, { status: response.status, headers });
    }
    if (!isModelContract(schema, value)) throw unavailable();
    if (schema === "ContextOverview") {
      const overview = value as components["schemas"]["ContextOverview"];
      const project = publicLangfuseProjectUrl();
      return Response.json(
        {
          ...overview,
          langfuse_url: overview.langfuse_status === "connected" && project ? project : null,
        },
        { status: response.status, headers },
      );
    }
    if (schema === "ContextRunFlow") {
      const flow = value as components["schemas"]["ContextRunFlow"];
      const project = publicLangfuseProjectUrl();
      const available = !!flow.langfuse_url && !!project && !!flow.run.trace_id;
      return Response.json(
        {
          ...flow,
          langfuse_url: available
            ? `${project}/traces/${encodeURIComponent(flow.run.trace_id!)}`
            : null,
          langfuse_reason: available
            ? null
            : flow.langfuse_reason || "未配置 Langfuse 公开项目地址",
        },
        { status: response.status, headers },
      );
    }
    return Response.json(value, { status: response.status, headers });
  } catch {
    throw unavailable();
  }
}
