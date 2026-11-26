import { timingSafeEqual } from "node:crypto";
import { z } from "zod";
import { authorizeRuntime } from "@/server/composition";
import { uuid } from "@/contracts/web";
import { readJson } from "@/server/http/responses";
import { ApplicationError } from "@/server/platform/errors";

export const runtime = "nodejs";
const input = z.strictObject({
  context: z.strictObject({
    actor_user_id: uuid,
    scope: z.union([
      z.strictObject({ kind: z.literal("tenant"), tenant_id: uuid, workspace_id: uuid }),
      z.strictObject({ kind: z.literal("platform") }),
    ]),
    purpose: z.enum([
      "chat",
      "runtime_usage",
      "runtime_diagnostics",
      "contextops_read",
      "contextops_admin",
      "schedule_manage",
      "schedule_owner",
      "schedule_run",
    ]),
  }),
  approval: z.boolean(),
  visibility: z.boolean().optional(),
  capabilityGrants: z.boolean().optional(),
  runEnvironment: z.boolean().optional(),
});
export async function POST(request: Request) {
  const expected = process.env.COMPASS_AGENT_SERVICE_TOKEN;
  const actual = request.headers.get("authorization") ?? "";
  const bearer = `Bearer ${expected}`;
  const headers = { "Cache-Control": "no-store" };
  if (
    !expected ||
    Buffer.byteLength(actual) !== Buffer.byteLength(bearer) ||
    !timingSafeEqual(Buffer.from(actual), Buffer.from(bearer))
  )
    return Response.json({ allowed: false }, { status: 403, headers });
  try {
    const value = input.parse(await readJson(request, 4096));
    const { details, revision, capabilityGrants, runEnvironment } = await authorizeRuntime(
      value.context,
      value.approval,
      value.capabilityGrants,
      value.runEnvironment,
    );
    if (value.capabilityGrants && !capabilityGrants)
      return Response.json({ allowed: false }, { status: 403, headers });
    return Response.json(
      {
        allowed: true,
        ...(runEnvironment ? { runEnvironment } : {}),
        ...(value.visibility ? { visibility: { details, revision } } : {}),
        ...(value.capabilityGrants ? { capabilityGrants, revision } : {}),
      },
      { headers },
    );
  } catch (error) {
    const denied =
      error instanceof z.ZodError ||
      (error instanceof ApplicationError &&
        ["forbidden", "not_found", "validation_failed"].includes(error.code));
    return Response.json({ allowed: false }, { status: denied ? 403 : 503, headers });
  }
}
