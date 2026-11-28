import "server-only";
import migration from "../../db/main/sql/manifest.json";
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { withTransaction, type TransactionMode, type SqlClient } from "./platform/db";
import { authConfiguration } from "./platform/config";
import { ApplicationError } from "./platform/errors";
import { bindIdentity, tokenHash } from "./modules/identity/infrastructure/pg-identity";
import { bindAccess } from "./modules/access/infrastructure/pg-access";
import { bindAudit } from "./modules/audit/pg-store";
import { bindNotifications } from "./modules/notifications/pg-store";
import { createIdentityService } from "./modules/identity/public";
import { allowed, createAccessService, requirePermission } from "./modules/access/public";
import { createModelAuthorizer } from "./modules/models/public";
import { createQueryService } from "./modules/access/application/query-service";
import { verifyPassword, DUMMY_PASSWORD_HASH } from "./modules/identity/infrastructure/password";
import { createLoginLimiter } from "./modules/identity/infrastructure/login-limit";
import type { IdentityContext } from "./modules/identity/public";
import type { AuditEntry } from "./platform/application-types";

const limiter = createLoginLimiter();
const digest = (value: string) => createHash("sha256").update(value).digest("hex");
function bind(client: SqlClient) {
  const identity = bindIdentity(client);
  return {
    identity,
    access: bindAccess(client),
    audit: bindAudit(client),
    notifications: bindNotifications(
      client,
      async (id) => (await identity.profile(id)).preferences.authorizationNotifications !== false,
    ),
  };
}
export async function recordFailure(
  action: string,
  error: unknown,
  actorUserId?: string,
  details: { targetId?: string; requestId?: string } = {},
) {
  const denied =
    error instanceof ApplicationError &&
    ["forbidden", "unauthenticated", "not_found"].includes(error.code);
  try {
    await withTransaction("command", async (client) => {
      const id = randomUUID();
      const entry: AuditEntry = {
        id,
        eventKey: id,
        actorUserId: actorUserId ?? null,
        tenantId: null,
        workspaceId: null,
        action,
        targetType: details.targetId ? "model_provider" : "request",
        targetId: details.targetId ?? null,
        outcome: denied ? "denied" : "failed",
        safeSummary: {
          code: error instanceof ApplicationError ? error.code : "unavailable",
          ...(details.requestId ? { requestId: details.requestId } : {}),
        },
      };
      await bindAudit(client).append(entry);
    });
  } catch {
    console.error("Web safety event could not be persisted.");
  }
}
const identityDependencies = {
  acquireLogin: (name: string) => limiter.acquire(name),
  verifyPassword,
  dummyHash: DUMMY_PASSWORD_HASH,
  newId: randomUUID,
  newToken: () => randomBytes(32).toString("base64url"),
  hashToken: tokenHash,
  ttlSeconds: () => authConfiguration().ttl,
  resolveRedirect: (token: string, path?: string) =>
    withTransaction("query", (client) =>
      createQueryService({ context: bind(client), digest, newId: randomUUID }).resolveRedirect(
        token,
        path,
      ),
    ),
  validateContext: (token: string, tenantId: string, workspaceId?: string) =>
    withTransaction("query", (client) =>
      createQueryService({ context: bind(client), digest, newId: randomUUID }).validateContext(
        token,
        tenantId,
        workspaceId,
      ),
    ),
  recordFailure,
};
export const authentication = createIdentityService({
  ...identityDependencies,
  query: <T>(work: (ctx: IdentityContext) => Promise<T>) =>
    withTransaction("query", (client) => work(bind(client))),
  command: <T>(work: (ctx: IdentityContext) => Promise<T>) =>
    withTransaction("command", (client) => work(bind(client))),
});

function bindUseCases(client: SqlClient) {
  const context = bind(client);
  const direct = {
    query: async <T>(work: (ctx: typeof context) => Promise<T>) => work(context),
    command: async <T>(work: (ctx: typeof context) => Promise<T>) => work(context),
  };
  const queries = createQueryService({ context, digest, newId: randomUUID });
  return {
    identity: createIdentityService({
      ...identityDependencies,
      ...direct,
      resolveRedirect: queries.resolveRedirect,
      validateContext: queries.validateContext,
    }),
    access: createAccessService({ ...direct, newId: randomUUID }),
    models: { authorize: createModelAuthorizer(context) },
    queries,
    db: client,
  };
}
export type WebUseCases = ReturnType<typeof bindUseCases>;

/** Recheck the initiating user at each durable execution boundary, without a browser session. */
export async function authorizeRuntime(
  context: {
    actor_user_id: string;
    purpose?: string;
    scope: { kind?: string; tenant_id?: string; workspace_id?: string };
  },
  approval: boolean,
  capabilityGrants = false,
  includeRunEnvironment = false,
) {
  return withTransaction("query", async (client) => {
    const { identity, access } = bind(client);
    const user = await identity.user(context.actor_user_id);
    if (user.status !== "active") throw new ApplicationError("forbidden", "账号已停用。");
    const actor = {
      userId: user.id,
      isPlatformAdmin: user.isPlatformAdmin,
      sessionId: "runtime",
      expiresAt: "",
    };
    if (context.purpose === "runtime_diagnostics") {
      if (!actor.isPlatformAdmin || context.scope.kind !== "platform")
        throw new ApplicationError("forbidden", "当前权限不足。");
      return { details: false, revision: "platform" };
    }
    if (!context.scope.tenant_id || !context.scope.workspace_id)
      throw new ApplicationError("forbidden", "当前权限不足。");
    const current = await access.current(actor, context.scope.tenant_id);
    const workspace = await access.workspace(context.scope.tenant_id, context.scope.workspace_id);
    if (
      workspace.tenantId !== context.scope.tenant_id ||
      workspace.status !== "active" ||
      (approval && context.purpose !== "schedule_owner" && current.role !== "admin")
    )
      throw new ApplicationError("forbidden", "当前权限不足。");
    const scope = { tenantId: context.scope.tenant_id, workspaceId: context.scope.workspace_id };
    if (context.purpose === "contextops_admin") {
      if (!actor.isPlatformAdmin) throw new ApplicationError("forbidden", "当前权限不足。");
    } else if (context.purpose === "contextops_read") {
      requirePermission(current, "module.analysis", scope);
    } else if (context.purpose === "runtime_usage") {
      if (current.role !== "admin" || !current.memberId)
        throw new ApplicationError("forbidden", "当前权限不足。");
    } else {
      const schedule = ["schedule_manage", "schedule_owner", "schedule_run"].includes(
        context.purpose ?? "",
      );
      if (schedule) requirePermission(current, "module.automations", scope);
      if (context.purpose !== "schedule_manage") {
        requirePermission(current, "module.analysis", scope);
        requirePermission(current, "models.invoke", scope);
      }
    }
    const tenant = includeRunEnvironment ? await access.tenant(scope.tenantId) : undefined;
    return {
      ...(tenant
        ? {
            runEnvironment: {
              tenantName: tenant.name,
              workspaceName: workspace.name,
              timezone: tenant.timezone,
            },
          }
        : {}),
      details: current.role === "admin",
      revision: current.authzVersion,
      ...(capabilityGrants
        ? {
            capabilityGrants: {
              analysis: allowed(current, "module.analysis", scope),
              model: allowed(current, "models.invoke", scope),
              plugins: allowed(current, "module.plugins", scope),
              semantic_maintainer:
                allowed(current, "semantics.draft", scope) ||
                allowed(current, "semantics.govern", scope),
            },
          }
        : {}),
    };
  });
}

/** One request owns one connection and snapshot. Revision is read before that transaction commits. */
export async function execute<T>(
  token: string,
  mode: TransactionMode,
  work: (useCases: WebUseCases) => Promise<T>,
): Promise<{ value: T; viewRevision: string }> {
  let actorUserId: string | undefined;
  try {
    return await withTransaction(mode, async (client) => {
      const actor = await bindIdentity(client).requireActor(token);
      actorUserId = actor.userId;
      const services = bindUseCases(client);
      const value = await work(services);
      const viewRevision = await services.queries.snapshotRevision(actor);
      return { value, viewRevision };
    });
  } catch (error) {
    await recordFailure(`web.${mode}`, error, actorUserId);
    throw error;
  }
}

export async function ready() {
  authConfiguration();
  return withTransaction("query", async (client) => {
    const { rows } = await client.query<{ name: string }>(
      "SELECT name FROM web_meta.pgmigrations ORDER BY id",
    );
    if (
      rows.length !== migration.sources.length ||
      rows.some((row, index) => migration.sources[index].source !== `migrations/${row.name}.sql`)
    )
      throw new ApplicationError("unavailable", "数据库版本不可用。");
    return { status: "ready" as const };
  });
}
