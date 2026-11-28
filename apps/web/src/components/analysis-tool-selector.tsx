"use client";

import { useRef, useState } from "react";
import { SlidersHorizontal, X } from "lucide-react";
import type { components } from "@/generated/api";
import { isModelContract, parseModelContract } from "@/contracts/model-contract";
import { useDocumentSession } from "./shell/session-boundary";
import { Modal } from "./ui/dialog";

type S = components["schemas"];
type Selection = S["ToolSelection"];
const empty: Selection = { packs: [], workflowIds: [], mcpServerIds: [] };
const packs = [
  ["long_task", "长任务", "维护目标、验收条件与跨轮进度"],
  ["workflow_pack", "工作流", "将已验证的步骤保存为可复用流程"],
  ["semantic_govern", "指标维护", "创建、检查和提交指标定义"],
  ["python_sandbox", "Python 分析", "统计计算、数据处理与绘图"],
] as const;

export function AnalysisToolSelectionBadges({
  value,
  labels,
  disabled,
  onChange,
}: {
  value?: Selection | null;
  labels: Record<string, string>;
  disabled: boolean;
  onChange: (value: Selection) => void;
}) {
  const items = (["packs", "workflowIds", "mcpServerIds"] as const).flatMap((group) =>
    (value?.[group] ?? []).map((id) => ({
      group,
      id,
      label:
        labels[id] ??
        packs.find((p) => p[0] === id)?.[1] ??
        (group === "workflowIds" ? "工作流" : "MCP 连接"),
    })),
  );
  return items.length ? (
    <div className="tool-selection-badges" aria-label="已选任务工具">
      {items.map(({ group, id, label }) => (
        <button
          key={`${group}:${id}`}
          type="button"
          className="secondary compact"
          disabled={disabled}
          aria-label={`移除${label}`}
          onClick={() =>
            onChange({ ...value, [group]: value?.[group]?.filter((item) => item !== id) })
          }
        >
          {label}
          <X size={12} aria-hidden="true" />
        </button>
      ))}
    </div>
  ) : null;
}

export function selectedExtensionCount(value: Selection, counts: Record<string, number>) {
  return (
    (value.workflowIds?.length ?? 0) +
    (value.mcpServerIds ?? []).reduce((n, id) => n + (counts[id] ?? 0), 0)
  );
}

export function AnalysisToolSelector({
  value,
  onChange,
  onLabelsChange,
  scope,
  capabilities,
  disabled,
}: {
  value?: Selection | null;
  onChange: (value: Selection) => void;
  onLabelsChange: (labels: Record<string, string>) => void;
  scope: { tenantId: string; workspaceId: string };
  capabilities: readonly string[];
  disabled: boolean;
}) {
  const api = useDocumentSession();
  const opener = useRef<HTMLButtonElement>(null);
  const generation = useRef(0);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Selection>(empty);
  const [workflows, setWorkflows] = useState<S["WorkflowAssetView"][]>([]);
  const [connections, setConnections] = useState<S["McpConnectionView"][]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(false);
  const [probing, setProbing] = useState("");
  const [error, setError] = useState("");
  const plugins = capabilities.includes("module.plugins");
  const maintainer =
    capabilities.includes("semantics.draft") || capabilities.includes("semantics.govern");
  const query = new URLSearchParams(scope).toString();
  const count = selectedExtensionCount(draft, counts);
  const unknown = (draft.mcpServerIds ?? []).some((id) => counts[id] == null);
  const selected =
    (value?.packs?.length ?? 0) +
    (value?.workflowIds?.length ?? 0) +
    (value?.mcpServerIds?.length ?? 0);

  function close() {
    generation.current += 1;
    setProbing("");
    setOpen(false);
  }

  async function load(selection: Selection) {
    const ticket = ++generation.current;
    setLoading(true);
    setProbing("");
    setError("");
    try {
      if (plugins) {
        const all: S["WorkflowAssetView"][] = [];
        let cursor: string | null | undefined;
        do {
          const page = parseModelContract<S["WorkflowAssetList"]>(
            "WorkflowAssetList",
            await api.request(
              `analysis/workflows?${query}${cursor ? "&cursor=" + encodeURIComponent(cursor) : ""}`,
            ),
          );
          all.push(
            ...page.items.filter(
              (item) => item.status === "active" && item.entryContract === "agent_workflow@1",
            ),
          );
          cursor = page.nextCursor;
        } while (cursor && all.length < 128);
        const servers = parseModelContract<S["McpConnectionList"]>(
          "McpConnectionList",
          await api.request(`mcp/connections?${query}`),
        );
        if (ticket !== generation.current) return;
        setWorkflows(all);
        setConnections(servers.items.filter((item) => item.enabled));
        onLabelsChange(
          Object.fromEntries([
            ...all.map((item) => [item.assetId, item.name]),
            ...servers.items.map((item) => [item.id, item.name]),
          ]),
        );
        const nextCounts: Record<string, number> = {};
        for (const server of servers.items.filter((item) => item.enabled)) {
          if (server.selected_tools !== "all") nextCounts[server.id] = server.selected_tools.length;
          else if (selection.mcpServerIds?.includes(server.id)) {
            const result = parseModelContract<S["McpDiscoveryResult"]>(
              "McpDiscoveryResult",
              await api.request(`mcp/connections/${server.id}/test?${query}`, "POST", {}),
            );
            if (ticket !== generation.current) return;
            if (result.revision !== server.revision) throw Error();
            nextCounts[server.id] = result.tools.length;
          }
        }
        if (ticket !== generation.current) return;
        setCounts(nextCounts);
        setDraft((current) => ({
          ...current,
          workflowIds: current.workflowIds?.filter((id) => all.some((item) => item.assetId === id)),
          mcpServerIds: current.mcpServerIds?.filter((id) =>
            servers.items.some((item) => item.id === id && item.enabled),
          ),
        }));
      }
    } catch {
      if (ticket === generation.current) setError("扩展工具加载失败，请重试。");
    } finally {
      if (ticket === generation.current) setLoading(false);
    }
  }

  async function selectConnection(server: S["McpConnectionView"], checked: boolean) {
    const ticket = generation.current;
    setError("");
    if (!checked) {
      setDraft((current) => ({
        ...current,
        mcpServerIds: current.mcpServerIds?.filter((id) => id !== server.id),
      }));
      return;
    }
    setProbing(server.id);
    try {
      let toolCount =
        server.selected_tools === "all" ? counts[server.id] : server.selected_tools.length;
      if (server.selected_tools === "all") {
        const result = parseModelContract<S["McpDiscoveryResult"]>(
          "McpDiscoveryResult",
          await api.request(`mcp/connections/${server.id}/test?${query}`, "POST", {}),
        );
        if (result.revision !== server.revision) throw Error();
        toolCount = result.tools.length;
      }
      if (ticket !== generation.current) return;
      setCounts((current) => ({ ...current, [server.id]: toolCount }));
      setDraft((current) => ({
        ...current,
        mcpServerIds: [...new Set([...(current.mcpServerIds ?? []), server.id])],
      }));
    } catch {
      if (ticket === generation.current) setError("该连接暂时不可用，请检查连接配置后重试。");
    } finally {
      if (ticket === generation.current) setProbing("");
    }
  }

  return (
    <>
      <button
        ref={opener}
        type="button"
        className="composer-attach"
        disabled={disabled}
        aria-label={selected ? `工具选择，已选 ${selected} 项` : "工具选择"}
        title="选择本次任务工具"
        onClick={() => {
          const selection = structuredClone(value ?? empty);
          if (!maintainer)
            selection.packs = selection.packs?.filter((id) => id !== "semantic_govern");
          if (!plugins) {
            selection.workflowIds = [];
            selection.mcpServerIds = [];
          }
          setDraft(selection);
          setOpen(true);
          void load(selection);
        }}
      >
        <SlidersHorizontal size={21} aria-hidden="true" />
        {selected > 0 && <span className="tool-selection-count">{selected}</span>}
      </button>
      {open && (
        <Modal
          title="本次任务工具"
          description="常用查询工具自动提供，可按任务补充能力。"
          open
          onClose={close}
          returnFocus={() => opener.current}
          footer={
            <>
              <button type="button" className="secondary" onClick={close}>
                取消
              </button>
              <button
                type="button"
                disabled={loading || !!probing || unknown || count > 12}
                onClick={() => {
                  if (isModelContract("ToolSelection", draft)) {
                    onChange(draft);
                    close();
                  } else setError("选择项超出限制，请减少后重试。");
                }}
              >
                应用选择
              </button>
            </>
          }
        >
          <div className="tool-selection-list">
            {packs
              .filter(([id]) => id !== "semantic_govern" || maintainer)
              .map(([id, label, help]) => (
                <label key={id} className="tool-selection-row">
                  <input
                    type="checkbox"
                    checked={draft.packs?.includes(id) ?? false}
                    disabled={id === "semantic_govern" && !maintainer}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        packs: event.target.checked
                          ? [...(current.packs ?? []), id]
                          : current.packs?.filter((pack) => pack !== id),
                      }))
                    }
                  />
                  <span>
                    <strong>{label}</strong>
                    <small>
                      {id === "semantic_govern" && !maintainer ? "需要指标维护权限" : help}
                    </small>
                  </span>
                </label>
              ))}
          </div>
          {plugins && (
            <>
              <p className="muted" role="status">
                扩展工具 {count} / 12
                {count >= 12 ? " · 最多选择 12 个扩展工具，请减少选择后再添加" : ""}
              </p>
              {loading ? (
                <p role="status">正在加载扩展…</p>
              ) : (
                <div className="tool-selection-list">
                  {workflows.map((item) => (
                    <label className="tool-selection-row" key={item.assetId}>
                      <input
                        type="checkbox"
                        checked={draft.workflowIds?.includes(item.assetId) ?? false}
                        disabled={!draft.workflowIds?.includes(item.assetId) && count >= 12}
                        onChange={(event) =>
                          setDraft((current) => ({
                            ...current,
                            workflowIds: event.target.checked
                              ? [...(current.workflowIds ?? []), item.assetId]
                              : current.workflowIds?.filter((id) => id !== item.assetId),
                          }))
                        }
                      />
                      <span>
                        <strong>{item.name}</strong>
                        <small>工作流 · 1 个工具</small>
                      </span>
                    </label>
                  ))}
                  {connections.map((server) => (
                    <label className="tool-selection-row" key={server.id}>
                      <input
                        type="checkbox"
                        disabled={
                          !!probing ||
                          (!draft.mcpServerIds?.includes(server.id) &&
                            (count >= 12 || (draft.mcpServerIds?.length ?? 0) >= 8))
                        }
                        checked={draft.mcpServerIds?.includes(server.id) ?? false}
                        onChange={(event) => void selectConnection(server, event.target.checked)}
                      />
                      <span>
                        <strong>{server.name}</strong>
                        <small>
                          {probing === server.id
                            ? "正在检查工具…"
                            : counts[server.id] == null
                              ? "MCP · 选择时检查可用工具"
                              : `MCP · ${counts[server.id]} 个工具`}
                        </small>
                      </span>
                    </label>
                  ))}
                  {!workflows.length && !connections.length && (
                    <p className="muted">暂无已启用的扩展。</p>
                  )}
                </div>
              )}
            </>
          )}
          {error && (
            <p role="alert" className="ui-notice" data-tone="error">
              {error}{" "}
              <button type="button" className="secondary" onClick={() => void load(draft)}>
                重试
              </button>
            </p>
          )}
        </Modal>
      )}
    </>
  );
}
