import type { ExecutionNode } from "@/contracts/execution-node";
import { parseModelContract } from "@/contracts/model-contract";
import type { DocumentSession } from "./request-scope";

export type ChildTrace = { nodes: ExecutionNode[]; answer?: string | null };
export type ChildTraceLoader = (childId: string, signal: AbortSignal) => Promise<ChildTrace>;

export function readChildTrace(
  api: DocumentSession,
  runId: string,
  scope: { tenantId: string; workspaceId: string },
  childId: string,
  signal: AbortSignal,
) {
  return api
    .request(
      `analysis/runs/${runId}/children/${childId}?${new URLSearchParams(scope)}`,
      "GET",
      undefined,
      { signal, retainOnNotFound: true },
    )
    .then((raw) => parseModelContract<ChildTrace>("ChildTraceSnapshot", raw));
}

export function childSubtree(nodes: readonly ExecutionNode[], rootId: string) {
  const ids = new Set([rootId]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const node of nodes)
      if (node.parentId && ids.has(node.parentId) && !ids.has(node.id)) {
        ids.add(node.id);
        changed = true;
      }
  }
  return nodes.filter((node) => ids.has(node.id));
}
