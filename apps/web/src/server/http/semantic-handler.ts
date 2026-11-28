import "server-only";
import { timingSafeEqual } from "node:crypto";
import { z } from "zod";
import { scopeParams, id } from "../../contracts/semantic";
import { trial } from "./semantic-trial";
import { suggestions } from "./semantic-suggestions";
import { execute } from "../composition";
import { withTransaction } from "../platform/db";
import { reject } from "../platform/errors";
import { bindIdentity } from "../modules/identity/infrastructure/pg-identity";
import { semanticAccess } from "../modules/semantic/access";
import { list, read, write } from "../modules/semantic/service";
import { releases } from "../modules/semantic/catalog";
import { registry } from "../modules/semantic/runtime";
import { tokenFromCookie, requireCsrf, requireSameOrigin } from "./security";
import { errorResponse, jsonResponse, readJson } from "./responses";

export async function handleSemantic(request: Request, path: string[]) {
  try {
    const token = tokenFromCookie(request.headers.get("cookie"));
    if (!token) reject("unauthenticated");
    const url = new URL(request.url);
    const { tenantId, workspaceId, ...params } = Object.fromEntries(url.searchParams);
    const target = scopeParams.parse({ tenantId, workspaceId });
    const writing = request.method === "POST";
    if (!writing && request.method !== "GET") reject("not_found");
    if (writing) {
      requireSameOrigin(request);
      requireCsrf(request, token);
    }
    const body = writing ? await readJson(request) : undefined;
    if (writing && path.length === 1 && path[0] === "trial")
      return await trial(token, target, body, request.signal);
    if (writing && path.length === 1 && path[0] === "suggestions")
      return await suggestions(token, target, body, request.signal);
    const result = await execute(token, writing ? "command" : "query", async ({ db }) => {
      const actor = await bindIdentity(db).requireActor(token);
      const c = await semanticAccess(db, actor, target);
      return writing ? write(c, path, body) : read(c, path, params);
    });
    return jsonResponse(result.value, result.viewRevision);
  } catch (error) {
    return errorResponse(error);
  }
}
const internalSchema = z.strictObject({
  actor_user_id: id,
  tenant_id: id,
  workspace_id: id,
  run_id: id,
  pins: z.array(id).max(3).optional(),
  args: z.record(z.string(), z.unknown()).default({}),
});
export async function internalSemantic(request: Request, op: string) {
  try {
    const token = process.env.COMPASS_AGENT_SERVICE_TOKEN;
    const got = Buffer.from(request.headers.get("authorization") ?? "");
    const wanted = Buffer.from("Bearer " + token);
    if (!token || got.length !== wanted.length || !timingSafeEqual(got, wanted))
      reject("forbidden");
    const input = internalSchema.parse(await readJson(request));
    if (
      ![
        "manifest",
        "authorize",
        "context",
        "glossary",
        "definition",
        "resolve",
        "choice",
        "search",
        "binding",
        "reference",
        "discover",
        "validate",
        "submit",
        "drafts",
        "result-draft",
        "revision-lookup",
        "revision-preview",
        "revision-save",
        "equivalents",
        "scopes",
      ].includes(op)
    )
      reject("not_found");
    const command = ["reference", "submit", "drafts", "result-draft", "revision-save"].includes(op);
    const result = await withTransaction(command ? "command" : "query", async (db) => {
      const user = await bindIdentity(db).user(input.actor_user_id);
      if (user.status !== "active") reject("forbidden");
      const c = await semanticAccess(
        db,
        {
          userId: user.id,
          isPlatformAdmin: user.isPlatformAdmin,
          sessionId: "runtime",
          expiresAt: "",
        },
        { tenantId: input.tenant_id, workspaceId: input.workspace_id },
        ["manifest", "context", "glossary", "authorize"].includes(op),
      );
      if (input.pins) await releases(c, input.pins);
      if (["discover", "validate", "drafts", "result-draft"].includes(op) && !c.can("draft"))
        reject("forbidden");
      if (op === "submit" && !c.can("govern")) reject("forbidden");
      if (op === "scopes") return read(c, ["scopes"], {});
      if (op === "equivalents") {
        const hash = z.string().regex(/^[a-f0-9]{64}$/);
        const { hashes, candidates } = z
          .strictObject({
            hashes: z.array(hash).max(100),
            candidates: z
              .array(
                z.strictObject({
                  hash,
                  name: z.string().min(1).max(160),
                  summary: z.string().min(1).max(2400),
                  formula: z.string().min(1).max(2400),
                }),
              )
              .max(3)
              .optional(),
          })
          .parse(input.args);
        const { published } = await import("../modules/semantic/catalog");
        const versions = (await published(c)).map((v) => v.version_id);
        const { rows } = await db.query<{ hash: string }>(
          `SELECT DISTINCT s.material->>'definition_hash' AS hash FROM web.semantic_source s
          JOIN web.semantic_versions v USING(source_id) JOIN web.semantic_assets a USING(asset_id)
          WHERE s.source_kind='query_receipt' AND s.material->>'definition_hash'=ANY($1::text[])
          AND (v.version_id=ANY($2::uuid[]) OR (a.creator_user_id=$3 AND a.owner_scope_id=ANY($4::uuid[])))`,
          [hashes, versions, c.actor.userId, c.scopes.filter((s) => s.manage).map((s) => s.id)],
        );
        const known = new Set(rows.map((r) => r.hash));
        if (candidates?.length) {
          if (!c.can("draft") || !c.memberId) reject("forbidden");
          const metrics = (await list(c)).filter((m) => m.kind === "metric");
          const { normalize } = await import("../modules/semantic/drafts");
          for (const candidate of candidates) {
            if (
              metrics.some(
                (m) =>
                  [m.name, m.code, ...m.aliases].some(
                    (n) => normalize(n) === normalize(candidate.name),
                  ) ||
                  (normalize(m.summary) === normalize(candidate.summary) &&
                    typeof m.definition.formula === "string" &&
                    normalize(m.definition.formula) === normalize(candidate.formula)),
              )
            )
              known.add(candidate.hash);
          }
        }
        return { hashes: [...known] };
      }
      if (op === "revision-lookup" || op === "revision-preview" || op === "revision-save") {
        const { lookupRevision, previewRevision, saveRevision } =
          await import("../modules/semantic/chat-revisions");
        if (op === "revision-lookup") return lookupRevision(c, input.args);
        return op === "revision-preview"
          ? previewRevision(c, input.args)
          : saveRevision(c, input.args);
      }
      if (op === "discover") {
        const { discover } = await import("../modules/semantic/drafts");
        const f = z
          .strictObject({
            source_refs: z.array(id).min(1).max(8),
            owner_scope_id: id,
            usage_scope_id: id.optional(),
          })
          .parse(input.args);
        return {
          sources: await discover(
            c,
            f.source_refs,
            f.owner_scope_id,
            f.usage_scope_id ?? f.owner_scope_id,
          ),
        };
      }
      if (op === "validate" || op === "submit") {
        const { version_id, ...args } = input.args;
        const key = id.parse(version_id);
        if (op === "validate") {
          const { inspect } = await import("../modules/semantic/governance");
          const { version } = await import("../modules/semantic/catalog");
          const v = await version(c, key);
          const view = await inspect(c, v);
          return {
            digest: v.digest,
            findings: view.checks,
            impact: view.impact,
            state: view.state,
          };
        }
        return write(c, ["revisions", key, "submit"], args);
      }
      if (op === "drafts") return write(c, ["drafts"], input.args);
      if (op === "result-draft") {
        const { saveResultDraft } = await import("../modules/semantic/drafts");
        return saveResultDraft(c, input.args);
      }
      return registry(c, op, input.args, input.pins);
    });
    return jsonResponse(result);
  } catch (error) {
    return errorResponse(error);
  }
}
