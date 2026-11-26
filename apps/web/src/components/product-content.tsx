"use client";
import type { PagePayload } from "@/contracts/page";
import { OrganizationPages } from "@/features/organization/pages";
import { PersonalPages, GovernancePages } from "@/features/personal/pages";
import { AnalysisComposer } from "./analysis-composer";
import { AnalysisWorkflows } from "./analysis-workflows";
import { UiGuide } from "./ui/guide";
import { Blocks, CircleAlert } from "lucide-react";
import { RetailConnectionPage } from "@/features/data/retail-connection-page";
import { AutomationsPage } from "@/features/automations/page";
import { KnowledgePage } from "@/features/knowledge/page";
import { SemanticPage } from "@/features/semantic/page";
import { PromptOpsPage } from "@/features/promptops/page";
import { ContextOpsPage } from "@/features/contextops/page";
import { ModelsPages } from "@/features/models/pages";
import type { WorkflowAssetRef } from "@/client/analysis-run";
import { isModelContract } from "@/contracts/model-contract";

function workflowSelection(viewKey: string) {
  const query = new URL(viewKey, "https://compass.local").searchParams;
  const value = query.get("workflow");
  if (query.has("session") || !value || value.length > 1024) return undefined;
  try {
    const selected = JSON.parse(value) as { name?: unknown; ref?: unknown } | null;
    if (
      typeof selected?.name === "string" &&
      selected.name.trim().length > 0 &&
      selected.name.length <= 80 &&
      isModelContract("WorkflowAssetRef", selected.ref)
    )
      return { name: selected.name, ref: selected.ref as WorkflowAssetRef };
  } catch {
    return undefined;
  }
}

export function ProductContent({ payload }: { payload: PagePayload }) {
  const { page, session } = payload;
  const { tenantId, workspaceId } = session.scope;
  const content =
    page.id === "contextops" && tenantId && workspaceId ? (
      <ContextOpsPage payload={payload} scope={{ tenantId, workspaceId }} />
    ) : page.section === "analysis" ? (
      <AnalysisComposer payload={payload} workflow={workflowSelection(payload.viewKey)} />
    ) : page.section === "automations" && tenantId && workspaceId ? (
      <AutomationsPage payload={payload} scope={{ tenantId, workspaceId }} />
    ) : page.id === "plugins-workflows" ? (
      <section className="workflow-library" aria-label="Workflow">
        {tenantId &&
        workspaceId &&
        session.capabilities.includes("models.invoke") &&
        session.capabilities.includes("module.analysis") ? (
          <AnalysisWorkflows
            scope={{ tenantId, workspaceId }}
            analysisHref={session.navigation.find((entry) => entry.id === "analysis-new")?.href}
          />
        ) : (
          <div className="ui-panel">
            <p className="ui-notice" data-tone="warning">
              <CircleAlert size={17} aria-hidden="true" /> 当前账户尚未获得工作流访问权限。
            </p>
          </div>
        )}
      </section>
    ) : ["platform-models", "capabilities-models"].includes(page.id) ? (
      <ModelsPages payload={payload} />
    ) : page.id === "platform-promptops" ? (
      <PromptOpsPage />
    ) : page.id === "data-documents" ? (
      <KnowledgePage session={session} />
    ) : page.id === "data-metrics" ? (
      <SemanticPage key={tenantId + "/" + workspaceId} session={session} />
    ) : page.id === "data-sources" ? (
      <RetailConnectionPage session={payload.session} />
    ) : page.kind === "placeholder" ? (
      <section className="module-placeholder">
        <div className="placeholder-icon">
          <Blocks size={28} />
        </div>
        <span className="status status-muted">待接入</span>
        <h2>{page.title}</h2>
        <p>
          {page.section === "memory" || page.id === "my-memory"
            ? "记忆、文件上传与处理能力将在后续版本接入。"
            : `${page.title}功能将在后续版本开放。`}
        </p>
        <p className="muted">页面框架已就绪，当前暂无业务内容。</p>
      </section>
    ) : page.section === "organization" ||
      ["platform-tenants", "workspaces", "access"].includes(page.id) ? (
      <OrganizationPages payload={payload} />
    ) : page.section === "personal" ? (
      <PersonalPages payload={payload} />
    ) : page.id === "guide" ? (
      <UiGuide />
    ) : (
      <GovernancePages payload={payload} />
    );
  return content;
}
