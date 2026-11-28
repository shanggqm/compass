import { expect, test, type Page, type Route } from "@playwright/test";
import { randomUUID } from "node:crypto";
import pg from "pg";
import type { components } from "../../src/generated/api";
import { DurableAnalysisFixture, fixtureProviderId } from "./durable-analysis.fixture";

// Real Web login, authorization, pages and interactions; Agent/plugin replies are isolated fixtures.
type S = components["schemas"];
const tenantId = "99500000-0000-4000-8000-000000000010";
const workspaceId = "99500000-0000-4000-8000-000000000020";
const origin = `http://127.0.0.1:${process.env.WEB_TEST_PORT ?? 8110}`;
const scope = { tenantId, workspaceId };
const analysisPath = `/t/${tenantId}/w/${workspaceId}/analysis`;
const id = (n: number) => `97000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const workflowId = id(1),
  fixedMcpId = id(11),
  discoveredMcpId = id(12);

test.use({ viewport: { width: 1280, height: 900 } });

async function login(page: Page, user: string, pageId: string) {
  const response = await page.request.post("/api/web/v1/auth/login", {
    headers: { origin },
    data: {
      loginName: user.includes(".") ? user : `teaching.${user}`,
      password: process.env.WEB_TEST_PASSWORD,
    },
  });
  expect(response.status()).toBe(200);
  const session = await page.request.get("/api/web/v1/session", {
    params: pageId === "platform-promptops" ? { pageId } : { ...scope, pageId },
  });
  expect(session.status()).toBe(200);
  const revision = session.headers()["x-compass-view-revision"];
  expect(revision).toBeTruthy();
  return revision;
}

function reply(route: Route, revision: string, json: unknown, status = 200) {
  return route.fulfill({
    status,
    json,
    headers: { "X-Compass-View-Revision": revision, "Cache-Control": "no-store" },
  });
}

async function noOverflow(page: Page) {
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1),
  ).toBe(true);
}

async function enterAnalysis(page: Page, user = "admin") {
  const revision = await login(page, user, "analysis-new");
  const supported = { state: "supported", reason: "Lesson 27 UI fixture" };
  await page.route("**/api/web/v1/models/available?**", (route) =>
    reply(route, revision, {
      items: [
        {
          id: fixtureProviderId,
          alias: "第27课隔离模型",
          api_model: "lesson27-fixture",
          kind: "llm",
          capabilities: {
            sync: supported,
            stream: supported,
            tools: supported,
            thinking: supported,
            json_output: supported,
            schema_output: supported,
            reasoning_efforts: [],
          },
        },
      ],
    }),
  );
  const fixture = new DurableAnalysisFixture(page, revision);
  await fixture.install();
  await page.route("**/api/web/v1/analysis/prompt-starters?**", (route) =>
    reply(route, revision, {
      generation: 1,
      analysis: [],
      models: [],
      default_choice: null,
      tool_description: null,
      workflow_parts: [],
    } satisfies S["PromptStarterCards"]),
  );
  const workflows: S["WorkflowAssetView"][] = [
    {
      assetId: workflowId,
      name: "月度复盘",
      description: "合成工作流",
      definitionVersion: 1,
      entryContract: "agent_workflow@1",
      metadataRevision: 1,
      parameters: null,
      scriptSource: null,
      status: "active",
    },
    {
      assetId: id(2),
      name: "停用工作流",
      description: null,
      definitionVersion: 1,
      entryContract: "agent_workflow@1",
      metadataRevision: 1,
      parameters: null,
      scriptSource: null,
      status: "disabled",
    },
  ];
  const servers: S["McpConnectionView"][] = [
    {
      id: fixedMcpId,
      name: "经营工具集",
      endpoint: "https://fixture.invalid/mcp",
      enabled: true,
      has_token: false,
      max_inflight: 1,
      required: false,
      revision: 1,
      selected_tools: Array.from({ length: 11 }, (_, n) => `read_${n}`),
      tool_policies: [],
    },
    {
      id: discoveredMcpId,
      name: "库存连接",
      endpoint: "https://fixture.invalid/stock",
      enabled: true,
      has_token: false,
      max_inflight: 1,
      required: false,
      revision: 1,
      selected_tools: "all",
      tool_policies: [],
    },
  ];
  await page.route("**/api/web/v1/analysis/workflows?**", (route) =>
    reply(route, revision, { items: workflows, nextCursor: null }),
  );
  const probes: string[] = [];
  let failProbe = false;
  let pendingProbe: Promise<void> | null = null;
  let resumeProbe: (() => void) | undefined;
  await page.route("**/api/web/v1/mcp/connections**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/test")) {
      probes.push(path);
      if (pendingProbe) await pendingProbe;
      if (failProbe)
        return reply(
          route,
          revision,
          { error: { code: "unavailable", message: "连接暂时不可用" } },
          503,
        );
      const result: S["McpDiscoveryResult"] = {
        revision: 1,
        tools: ["inventory", "warehouses"].map((name) => ({
          name,
          description: "合成只读工具",
          input_schema: { type: "object" },
          output_schema: null,
        })),
      };
      return reply(route, revision, result);
    }
    return reply(route, revision, { items: servers });
  });
  await page.goto(analysisPath);
  await expect(page.getByLabel("选择 Chat 主模型")).toBeEnabled();
  return {
    fixture,
    probes,
    failProbe: (value: boolean) => {
      failProbe = value;
    },
    holdProbe: () => {
      pendingProbe = new Promise<void>((resolve) => {
        resumeProbe = resolve;
      });
    },
    releaseProbe: () => {
      resumeProbe?.();
      pendingProbe = null;
    },
  };
}

async function createNonMaintainer() {
  const dsn = process.env.WEB_TEST_MIGRATION_DATABASE_URL;
  if (!dsn || new URL(dsn).pathname !== "/compass_web_test_lesson27")
    throw Error("The isolated Lesson 27 Web test database is required");
  const account = randomUUID(),
    membership = randomUUID();
  const handle = `lesson27.tools.${account.slice(0, 8)}`;
  const db = new pg.Client({ connectionString: dsn });
  await db.connect();
  try {
    await db.query("BEGIN");
    await db.query("SET LOCAL ROLE compass_web_owner");
    const added = await db.query(
      `INSERT INTO web.app_user(id,login_name,display_name,password_hash)
       SELECT $1,$2,'工具选择验收员',password_hash FROM web.app_user WHERE login_name='teaching.admin'`,
      [account, handle],
    );
    expect(added.rowCount).toBe(1);
    await db.query(
      "INSERT INTO web.tenant_member(id,tenant_id,user_id,role_key) VALUES($1,$2,$3,'member')",
      [membership, tenantId, account],
    );
    await db.query(
      `INSERT INTO web.workspace_member(id,tenant_id,workspace_id,tenant_member_id,role_key,permission_keys)
       VALUES($1,$2,$3,$4,'analyst',$5)`,
      [
        randomUUID(),
        tenantId,
        workspaceId,
        membership,
        ["module.analysis", "models.invoke", "module.plugins", "workspaces.read"],
      ],
    );
    await db.query("COMMIT");
  } finally {
    await db.end();
  }
  return handle;
}

test("工具选择按实际工具数执行12上限，取消不保存，键盘应用带入Run", async ({ page }, info) => {
  const { fixture, probes } = await enterAnalysis(page);
  const opener = page.getByRole("button", { name: "工具选择", exact: true });
  await opener.focus();
  await opener.press("Enter");
  const dialog = page.getByRole("dialog", { name: "本次任务工具" });
  await expect(dialog.getByRole("checkbox", { name: /月度复盘/ })).toBeVisible();
  await expect(dialog.locator(".tool-selection-row").first()).toHaveCSS("flex-direction", "row");
  expect(probes).toHaveLength(0);
  await expect(dialog.getByRole("checkbox", { name: /停用工作流/ })).toHaveCount(0);
  await expect(dialog.getByRole("checkbox", { name: /指标维护/ })).toBeEnabled();
  await dialog.getByRole("checkbox", { name: /长任务/ }).press("Space");
  await page.keyboard.press("Escape");
  await expect(opener).toBeFocused();
  await opener.press("Enter");
  await expect(dialog.getByRole("checkbox", { name: /长任务/ })).not.toBeChecked();
  await dialog.getByRole("checkbox", { name: /长任务/ }).check();
  await dialog.getByRole("checkbox", { name: /经营工具集/ }).check();
  await expect(dialog).toContainText("扩展工具 11 / 12");
  expect(probes).toHaveLength(0);
  await dialog.getByRole("checkbox", { name: /月度复盘/ }).check();
  await expect(dialog).toContainText("扩展工具 12 / 12");
  await expect(dialog.getByRole("button", { name: "应用选择" })).toBeEnabled();
  await expect(dialog.getByRole("checkbox", { name: /库存连接/ })).toBeDisabled();
  await dialog.getByRole("checkbox", { name: /月度复盘/ }).uncheck();
  await dialog.getByRole("checkbox", { name: /库存连接/ }).click();
  await expect(dialog).toContainText("扩展工具 13 / 12");
  expect(probes).toHaveLength(1);
  await expect(dialog.getByRole("button", { name: "应用选择" })).toBeDisabled();
  await noOverflow(page);
  await page.screenshot({ path: info.outputPath("lesson27-selector-desktop.png") });
  await dialog.getByRole("checkbox", { name: /经营工具集/ }).uncheck();
  await dialog.getByRole("checkbox", { name: /月度复盘/ }).check();
  await expect(dialog).toContainText("扩展工具 3 / 12");
  const apply = dialog.getByRole("button", { name: "应用选择" });
  await apply.focus();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "关闭", exact: true })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(apply).toBeFocused();
  await apply.press("Enter");
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "工具选择，已选 3 项" })).toBeFocused();
  await expect(page.getByLabel("已选任务工具")).toContainText("长任务");
  await page.getByRole("button", { name: "移除月度复盘", exact: true }).press("Enter");
  await page.getByRole("button", { name: "工具选择，已选 2 项" }).click();
  await expect(dialog).toContainText("扩展工具 2 / 12");
  await expect(dialog.getByRole("checkbox", { name: /月度复盘/ })).not.toBeChecked();
  await dialog.getByRole("checkbox", { name: /月度复盘/ }).check();
  await dialog.getByRole("button", { name: "应用选择" }).click();
  await page.getByLabel("分析问题").fill("对比库存和销售情况");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect.poll(() => fixture.requests.length).toBe(1);
  expect(fixture.requests[0].constraints?.toolSelection).toEqual({
    packs: ["long_task"],
    workflowIds: [workflowId],
    mcpServerIds: [discoveredMcpId],
  });
  await expect(page.getByRole("region", { name: "最终回答", exact: true })).toBeVisible();
});

test("390px工具选择可读、取消回焦且未提交草稿不进入Run", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { fixture } = await enterAnalysis(page);
  const opener = page.getByRole("button", { name: "工具选择", exact: true });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "本次任务工具" });
  await dialog.getByRole("checkbox", { name: /经营工具集/ }).check();
  await expect(dialog.locator(".tool-selection-row").first()).toHaveCSS("flex-direction", "row");
  await dialog.getByRole("checkbox", { name: /月度复盘/ }).check();
  await expect(dialog).toContainText("扩展工具 12 / 12");
  await noOverflow(page);
  const box = await dialog.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(390);
  await page.screenshot({ path: info.outputPath("lesson27-selector-mobile.png") });
  await dialog.getByRole("button", { name: "取消", exact: true }).press("Enter");
  await expect(opener).toBeFocused();
  await page.getByLabel("分析问题").fill("查看本月经营情况");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect.poll(() => fixture.requests.length).toBe(1);
  expect(fixture.requests[0].constraints?.toolSelection).toBeFalsy();
  await expect(page.getByRole("region", { name: "最终回答", exact: true })).toBeVisible();
  await noOverflow(page);
});

test("非维护者无法选中指标维护，MCP失败后可重试并恢复选择", async ({ page }) => {
  const tools = await enterAnalysis(page, await createNonMaintainer());
  await page.getByRole("button", { name: "工具选择", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "本次任务工具" });
  await expect(dialog.getByRole("checkbox", { name: /指标维护/ })).toHaveCount(0);
  tools.failProbe(true);
  await dialog.getByRole("checkbox", { name: /库存连接/ }).click();
  await expect(dialog.getByRole("alert")).toContainText("该连接暂时不可用");
  await expect(dialog.getByRole("checkbox", { name: /库存连接/ })).not.toBeChecked();
  await expect(dialog).toContainText("扩展工具 0 / 12");
  tools.failProbe(false);
  await dialog.getByRole("button", { name: "重试", exact: true }).click();
  await dialog.getByRole("checkbox", { name: /库存连接/ }).click();
  await expect(dialog).toContainText("扩展工具 2 / 12");
  await expect(dialog.getByRole("alert")).toHaveCount(0);
  await dialog.getByRole("button", { name: "应用选择" }).press("Enter");
  await page.getByLabel("分析问题").fill("查询库存");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect.poll(() => tools.fixture.requests.length).toBe(1);
  expect(tools.fixture.requests[0].constraints?.toolSelection).toEqual({
    packs: [],
    workflowIds: [],
    mcpServerIds: [discoveredMcpId],
  });
  await expect(page.getByRole("region", { name: "最终回答", exact: true })).toBeVisible();
});

test("检查MCP时取消并重新打开，迟到回执不会恢复已取消选择", async ({ page }) => {
  const tools = await enterAnalysis(page);
  tools.holdProbe();
  const opener = page.getByRole("button", { name: "工具选择", exact: true });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "本次任务工具" });
  try {
    await dialog.getByRole("checkbox", { name: /库存连接/ }).click();
    await expect(dialog).toContainText("正在检查工具");
    await expect(dialog.getByRole("button", { name: "应用选择" })).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(opener).toBeFocused();
    await opener.press("Enter");
    await expect(dialog.getByRole("checkbox", { name: /库存连接/ })).not.toBeChecked();
    const response = page.waitForResponse((res) => res.url().includes(`${discoveredMcpId}/test`));
    tools.releaseProbe();
    await response;
    await expect(dialog.getByRole("checkbox", { name: /库存连接/ })).not.toBeChecked();
    await expect(dialog).toContainText("扩展工具 0 / 12");
    await expect(dialog.getByRole("button", { name: "应用选择" })).toBeEnabled();
    await page.keyboard.press("Escape");
    await expect(opener).toBeFocused();
  } finally {
    tools.releaseProbe();
  }
});

function flow(): S["ContextRunFlow"] {
  return {
    run: {
      run_id: id(101),
      session_id: id(102),
      actor_id: id(103),
      session_title: "第27课工具装配验收",
      status: "completed",
      scene_id: "SC-03",
      scene_name: "经营归因",
      category_name: "经营分析",
      created_at: "2026-11-27T01:00:00Z",
      step_count: 1,
      duration_ms: 240,
      total_tokens: 500,
      token_upper_bound: 700,
      omission_rate: 0,
      compaction_applied: false,
      security_blocked_count: 0,
      trace_id: null,
      historical: false,
    },
    budget_method: "utf8_upper_bound",
    langfuse_url: null,
    langfuse_reason: "本地隔离验收",
    nodes: [
      {
        id: "model:lesson27",
        label: "模型调用",
        ordinal: 1,
        phase: "research",
        track: "model",
        step_id: id(104),
        started_at: "2026-11-27T01:00:00Z",
        status: "completed",
        duration_ms: 240,
        tokens: 500,
        error_code: null,
        policy_digest: null,
        prompt_generation: 1,
        redacted_count: 0,
        security_blocked_count: 0,
        semantic_version: null,
        snapshot_ref: null,
        toolAssembly: {
          authorized_count: 6,
          exposed_count: 3,
          deferred_count: 2,
          excluded_count: 1,
          permission_filtered_count: 3,
          declaration_bytes: 2048,
          declaration_tokens: 512,
          exposed: [
            { name: "query_metric", label: "指标查询", channel: "always_on", pack: "data_base" },
            {
              name: "python_analysis",
              label: "Python 分析",
              channel: "scene_routed",
              pack: "deep_analysis_pack",
            },
            {
              name: "workflow_monthly",
              label: "月度复盘",
              channel: "user_selected",
              pack: "user_extension",
            },
          ],
          deferred: ["goal", "create_workflow"],
          excluded: [{ name: "todo_write", reason: "scene" }],
        },
      },
    ],
  };
}

async function enterContextOps(page: Page) {
  const revision = await login(page, "platform", "contextops");
  const data = flow();
  const overview: S["ContextOverview"] = {
    administrator: true,
    window: "7d",
    run_count: 1,
    session_count: 1,
    failure_count: 0,
    anomaly_count: 0,
    avg_search_turns: 1,
    avg_turns: 1,
    cache_hit_rate: 0,
    compaction_rate: 0,
    compaction_saving_rate: 0,
    compressed_completion_rate: null,
    context_duration_share: 0,
    empty_result_count: 0,
    langfuse_status: "disabled",
    langfuse_url: null,
    max_turns: 1,
    measured_tokens: 500,
    min_turns: 1,
    omission_rate: 0,
    projection_dropped: 0,
    projection_errors: 0,
    redacted_count: 0,
    security_blocked_count: 0,
    security_distribution: [],
    security_mode: "enforce",
    short_session_rate: 1,
    source_breakdown: [],
    terminal_outcome_counts: [],
    token_upper_bound: 700,
    turn_completion_breakdown: [],
    uncompressed_completion_rate: 1,
    duration: { avg_ms: 240, count: 1, name: "运行耗时", p50_ms: 240, p95_ms: 240, scene_id: null },
  };
  await page.route("**/api/web/v1/contextops/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/overview")) return reply(route, revision, overview);
    if (path.endsWith("/flow")) return reply(route, revision, data);
    if (path.endsWith("/runs"))
      return reply(route, revision, { administrator: true, items: [data.run], next_cursor: null });
    if (path.endsWith("/badcases")) return reply(route, revision, { items: [], next_cursor: null });
    throw Error(`Unexpected ContextOps request: ${path}`);
  });
  await page.goto(`/t/${tenantId}/w/${workspaceId}/contextops?run=${data.run.run_id}`);
  const row = page.getByLabel("轨迹节点列表").getByRole("button");
  await expect(row).toBeVisible();
  await row.press("Enter");
}

test("ContextOps三通道装配明细在桌面与390px展示且支持键盘折叠", async ({ page }, info) => {
  await enterContextOps(page);
  const assembly = page.getByRole("region", { name: "工具装配", exact: true });
  const details = assembly.locator("details");
  const toggle = details.locator("summary");
  const counts = assembly.getByText("已授权 6 · 本步 3 · 待用 2 · 排除 1 · 权限过滤 3");
  await expect(toggle).toHaveText("工具装配");
  await expect(details).toHaveJSProperty("open", true);
  await expect(counts).toBeVisible();
  await toggle.focus();
  await toggle.press("Enter");
  await expect(details).toHaveJSProperty("open", false);
  await expect(counts).toBeHidden();
  await expect(toggle).toBeFocused();
  await toggle.press("Enter");
  await expect(counts).toBeVisible();
  await expect(assembly).toContainText("已授权 6 · 本步 3 · 待用 2 · 排除 1 · 权限过滤 3");
  await expect(assembly).toContainText("2,048 B · 约 512 tok");
  for (const label of [
    "指标查询 · 默认常驻",
    "Python 分析 · 按场景",
    "月度复盘 · 用户选择",
    "todo_write · 当前场景",
  ])
    await expect(assembly).toContainText(label);
  await expect(assembly).not.toContainText("submit_semantic_change");
  await noOverflow(page);
  await page.screenshot({ path: info.outputPath("lesson27-contextops-desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await assembly.scrollIntoViewIfNeeded();
  await expect(assembly).toBeVisible();
  await toggle.focus();
  await toggle.press("Space");
  await expect(details).toHaveJSProperty("open", false);
  await expect(counts).toBeHidden();
  await toggle.press("Space");
  await expect(details).toHaveJSProperty("open", true);
  await expect(counts).toBeVisible();
  await noOverflow(page);
  const box = await assembly.boundingBox();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(391);
  await page.screenshot({ path: info.outputPath("lesson27-contextops-mobile.png") });
});

test("PromptOps工具治理标签保留三种通道与审批状态，390px不溢出", async ({ page }, info) => {
  const revision = await login(page, "platform", "platform-promptops");
  const governance: S["ToolGovernanceView"][] = [
    { channel: "always_on", pack: "data_base", effect: "read_only", approval: "none" },
    {
      channel: "scene_routed",
      pack: "deep_analysis_pack",
      effect: "local_artifact",
      approval: "none",
    },
    {
      channel: "user_selected",
      pack: "semantic_govern",
      effect: "external_write",
      approval: "required",
    },
  ];
  const names = ["query_metric", "python_analysis", "submit_semantic_change"];
  const files: S["PromptFileDetail"][] = names.map((name, index) => ({
    path: `tools/${name}.json`,
    governance: governance[index],
    body: JSON.stringify({
      type: "function",
      function: {
        name,
        description: "第27课工具说明",
        parameters: { type: "object", properties: {} },
      },
      examples: [],
    }),
    draft_body: null,
    current_version: 1,
    version: 1,
    active_epoch: 1,
    draft_base_epoch: null,
    draft_base_version: null,
    edit_rev: 1,
    has_draft: false,
    kind: "tool",
    origin: "bundled",
    used: true,
    retired: false,
    contract_sha256: null,
    usages: ["runtime"],
    variables: [],
  }));
  await page.route("**/api/web/v1/platform/promptops/**", (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/tree"))
      return reply(route, revision, {
        items: files,
        next_cursor: null,
        generation: 1,
        snapshot_no: 1,
      });
    if (url.pathname.endsWith("/file"))
      return reply(
        route,
        revision,
        files.find((file) => file.path === url.searchParams.get("path")),
      );
    throw Error(`Unexpected PromptOps request: ${url.pathname}`);
  });
  await page.goto("/platform/promptops");
  const labels = ["默认常驻", "按场景", "用户选择"];
  for (let index = 0; index < files.length; index++) {
    await page.getByTitle(files[index].path, { exact: true }).press("Enter");
    const label = page.getByLabel("工具治理信息");
    await expect(label).toContainText(`加载：${labels[index]}`);
    await expect(label).toContainText(`能力包：${governance[index].pack}`);
    await expect(label).toContainText(`审批：${index === 2 ? "需要确认" : "无需确认"}`);
  }
  await noOverflow(page);
  await page.screenshot({ path: info.outputPath("lesson27-promptops-desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByLabel("工具治理信息")).toBeVisible();
  await noOverflow(page);
  await page.screenshot({ path: info.outputPath("lesson27-promptops-mobile.png") });
});
