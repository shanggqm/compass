import type { IdentityStore } from "../../identity/public";
import { requirePermission, type AccessStore } from "../../access/public";
import { reject } from "../../../platform/errors";

export type ModelScope =
  | { kind: "platform"; purpose: "manage" | "test" | "runtime_diagnostics" | "promptops_manage" }
  | {
      kind: "tenant";
      purpose:
        | "chat"
        | "module_read"
        | "knowledge_manage"
        | "data_manage"
        | "mcp_manage"
        | "runtime_usage"
        | "contextops_read"
        | "contextops_admin"
        | "schedule_manage";
      tenantId: string;
      workspaceId: string;
    };
export type TrustedModelContext = {
  actor_user_id: string;
  scope: { kind: "platform" } | { kind: "tenant"; tenant_id: string; workspace_id: string };
  purpose: ModelScope["purpose"];
};
type ModelAccessContext = { identity: IdentityStore; access: AccessStore };

/** Only database work belongs here. Agent calls happen after execute() commits. */
export function createModelAuthorizer(ctx: ModelAccessContext) {
  return async function authorize(
    token: string,
    request: ModelScope,
  ): Promise<TrustedModelContext> {
    const actor = await ctx.identity.requireActor(token);
    if (request.kind === "platform") {
      if (!actor.isPlatformAdmin) reject("forbidden");
      return { actor_user_id: actor.userId, scope: { kind: "platform" }, purpose: request.purpose };
    }
    const access = await ctx.access.current(actor, request.tenantId);
    const workspace = await ctx.access.workspace(request.tenantId, request.workspaceId);
    if (workspace.tenantId !== request.tenantId || workspace.status !== "active")
      reject("forbidden");
    const scope = { tenantId: request.tenantId, workspaceId: request.workspaceId };
    if (request.purpose === "contextops_admin") {
      if (!actor.isPlatformAdmin) reject("forbidden");
    } else if (request.purpose === "contextops_read") {
      requirePermission(access, "module.analysis", scope);
    } else if (request.purpose === "runtime_usage") {
      if (access.role !== "admin" || !access.memberId) reject("forbidden");
    } else if (request.purpose === "schedule_manage") {
      requirePermission(access, "module.automations", scope);
    } else if (request.purpose === "chat") {
      requirePermission(access, "module.analysis", scope);
      requirePermission(access, "models.invoke", scope);
    } else if (request.purpose === "mcp_manage") {
      requirePermission(access, "module.analysis", scope);
    } else if (request.purpose === "knowledge_manage") {
      if (!access.memberId) reject("forbidden");
      requirePermission(access, "module.admin.data", scope);
    } else if (request.purpose === "data_manage") {
      if (!actor.isPlatformAdmin) reject("forbidden");
      requirePermission(access, "module.admin.data", scope);
    } else requirePermission(access, "module.admin.capabilities", scope);
    return {
      actor_user_id: actor.userId,
      scope: { kind: "tenant", tenant_id: request.tenantId, workspace_id: request.workspaceId },
      purpose: request.purpose,
    };
  };
}
