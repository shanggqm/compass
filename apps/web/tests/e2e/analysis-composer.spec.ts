import { test, expect, type BrowserContext, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import {
  DurableAnalysisFixture,
  fixtureProviderId as providerId,
} from "./durable-analysis.fixture";

// UI scenarios use real Web login/authorization and a persistent per-test v2 Agent fixture.
const tenantId = "99500000-0000-4000-8000-000000000010";
const workspaceId = "99500000-0000-4000-8000-000000000020";
const origin = `http://127.0.0.1:${process.env.WEB_TEST_PORT ?? 8110}`;
const analysisPath = `/t/${tenantId}/w/${workspaceId}/analysis`;
const password = process.env.WEB_TEST_PASSWORD ?? "teaching-995-only-password";
test.use({ acceptDownloads: true, storageState: process.env.WEB_TEST_STORAGE_STATE });
let authenticatedCookies: Parameters<BrowserContext["addCookies"]>[0] | undefined;

async function enter(page: Page, context: BrowserContext) {
  if (authenticatedCookies) await context.addCookies(authenticatedCookies);
  const sessionPath = `/api/web/v1/session?${new URLSearchParams({ tenantId, workspaceId, pageId: "analysis-new" })}`,
    initial = await context.request.get(sessionPath);
  if (initial.status() === 401) {
    const login = await context.request.post("/api/web/v1/auth/login", {
      headers: { origin },
      data: { loginName: "teaching.admin", password },
    });
    expect(login.status()).toBe(200);
  }
  const session = initial.ok() ? initial : await context.request.get(sessionPath);
  expect(session.status()).toBe(200);
  authenticatedCookies = await context.cookies();
  const revision = session.headers()["x-compass-view-revision"];
  expect(revision).toBeTruthy();
  const supported = { state: "supported", reason: "UI fixture" };
  await page.route("**/api/web/v1/models/available?**", (route) =>
    route.fulfill({
      headers: { "X-Compass-View-Revision": revision, "Cache-Control": "no-store" },
      json: {
        items: [
          {
            id: providerId,
            alias: "DeepSeek V4 Pro",
            api_model: "deepseek-v4-pro",
            kind: "llm",
            capabilities: {
              sync: supported,
              stream: supported,
              tools: supported,
              thinking: supported,
              json_output: supported,
              schema_output: supported,
              reasoning_efforts: ["low", "high", "max"],
            },
          },
        ],
      },
    }),
  );
  const fixture = new DurableAnalysisFixture(page, revision);
  await fixture.install();
  await page.goto(analysisPath);
  await expect(page.locator("[data-protected-root]")).toBeVisible();
  await expect(page.getByLabel("选择 Chat 主模型")).toBeEnabled();
  return fixture;
}

async function expectNoOverflow(page: Page) {
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1),
  ).toBe(true);
  const form = await page.locator(".analysis-composer").boundingBox();
  expect(form).not.toBeNull();
  expect(form!.x).toBeGreaterThanOrEqual(0);
  expect(form!.x + form!.width).toBeLessThanOrEqual(page.viewportSize()!.width + 1);
}

const file = (index: number) => ({
  name: `经营分析资料-${String(index).padStart(2, "0")}.pptx`,
  mimeType: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  buffer: Buffer.from(`synthetic attachment ${index}`),
});

test("composer starts centered, docks on the first send, keeps history scrolling separate, and resets", async ({
  page,
  context,
}) => {
  const model = await enter(page, context);
  const state = page.locator("section[data-chat-state]");
  const form = page.locator(".analysis-composer");
  const input = page.getByLabel("分析问题");
  await expect(state).toHaveAttribute("data-chat-state", "empty");
  const initial = await form.boundingBox();
  const inputBox = await input.boundingBox();
  expect(inputBox!.height).toBeGreaterThanOrEqual(140);
  expect(initial!.y + initial!.height).toBeLessThan(page.viewportSize()!.height - 80);
  await expect(page.getByRole("button", { name: "发送消息", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: /深度思考|智能搜索/ })).toHaveCount(0);
  const select = page.getByLabel("选择 Chat 主模型");
  await expect(select).toHaveValue("auto");
  await expect(select.locator("option:checked")).toHaveText("Auto");
  await select.selectOption(providerId);
  await input.fill("分析销售趋势");
  await input.press("Shift+Enter");
  await expect(input).toHaveValue("分析销售趋势\n");
  expect(model.requests).toHaveLength(0);
  await input.dispatchEvent("keydown", { key: "Enter", code: "Enter", isComposing: true });
  expect(model.requests).toHaveLength(0);
  await expect(input).toHaveValue("分析销售趋势\n");
  model.holdNext();
  try {
    await input.press("Enter");
    await expect.poll(() => model.requests.length).toBe(1);
    expect(model.requests[0]).toMatchObject({
      tenantId,
      workspaceId,
      question: "分析销售趋势",
      sessionId: null,
      selection: { mode: "fixed", provider_id: providerId },
    });
    expect(model.requests[0]).not.toHaveProperty("history");
    await expect(state).toHaveAttribute("data-chat-state", "started");
    await expect(page.getByRole("button", { name: "停止任务", exact: true })).toBeVisible();
    await expect(page.locator(".analysis-process .analysis-loading-spinner:visible")).toBeVisible();
    const pending = await form.boundingBox();
    expect(pending!.y).toBeGreaterThan(initial!.y);
    expect(pending!.height).toBeLessThan(initial!.height);
    const dock = await page.locator(".analysis-composer-dock").boundingBox();
    expect(page.viewportSize()!.height - (dock!.y + dock!.height)).toBeLessThan(80);
  } finally {
    model.release();
  }
  const answer = page.getByRole("region", { name: "最终回答" });
  await expect(answer.locator(".analysis-markdown")).toHaveAttribute("data-revealing", "false");
  await expect(answer.getByRole("heading", { name: "销售表现", level: 2 })).toBeVisible();
  await expect(answer.getByRole("table").getByRole("cell", { name: "120 元" })).toBeVisible();
  await expect(answer.getByRole("link", { name: "分析说明" })).toHaveAttribute(
    "href",
    "https://example.com/analysis",
  );
  await expect(answer.getByRole("checkbox", { checked: true })).toBeDisabled();
  await expect(answer.locator("strong").first()).toHaveText("120 元");
  await expect(answer.locator("blockquote")).toContainText("按实付金额汇总。");
  await expect(answer.locator("pre code")).toContainText("paid_cent FROM sales;");
  await expect(answer.locator("ol > li")).toHaveCount(55);
  await expect(answer.locator(".analysis-markdown")).toHaveAttribute("data-revealing", "false");
  await expect(answer.locator("#answer-script")).toHaveCount(0);
  await expect(answer.locator('a[href^="javascript:"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: "停止任务", exact: true })).toHaveCount(0);
  const process = page.locator(".analysis-process-toggle");
  await expect(process).toHaveAttribute("aria-expanded", "false");
  await expect(process.locator(".analysis-loading-spinner")).toHaveCount(0);
  await process.click();
  await expect(process).toHaveAttribute("aria-expanded", "true");
  const toolCards = page.locator('.ui-execution-timeline > [data-kind="tool"]');
  await expect(toolCards.locator(".ui-timeline-text")).toHaveText([
    /比较渠道实付金额/,
    /查询 POS 渠道商品明细/,
  ]);
  await toolCards
    .first()
    .getByRole("button", { name: /比较渠道实付金额/ })
    .click();
  await expect(toolCards.first()).toContainText("返回 2 条记录。");
  await expect(toolCards.first()).toContainText("调用入参");
  await expect(toolCards.first()).toContainText('"paidCent":"12000"');
  await expect(page.locator(".model-chat-turn")).not.toContainText(
    /模型调用|调用信息|调用编号|轮工具调用|channel_query/,
  );
  await expect(page.getByRole("link", { name: /^下载数据 · retail-products-/ })).toBeVisible();
  const history = page.locator(".model-chat-history");
  await expect(page.getByText("我的工作流", { exact: true })).toHaveCount(0);
  await expect(form).toHaveCSS("backdrop-filter", /blur\(16px\)/);
  await expect(history).toHaveCSS("scrollbar-width", "none");
  expect(await history.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(
    true,
  );
  const docked = await form.boundingBox();
  const historyBounds = await history.boundingBox();
  expect(historyBounds!.y + historyBounds!.height).toBeGreaterThan(docked!.y + 40);
  await history.evaluate((element) => element.scrollTo({ top: 0 }));
  await expect.poll(() => history.evaluate((element) => element.scrollTop)).toBe(0);
  await history.focus();
  await history.press("PageDown");
  await expect.poll(() => history.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  const afterScroll = await form.boundingBox();
  expect(Math.abs(afterScroll!.y - docked!.y)).toBeLessThan(2);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(state).toHaveAttribute("data-chat-state", "empty");
  await expect(history).toHaveCount(0);
  await expect(input).toHaveValue("");
  await expect(select).toHaveValue(providerId);
});

test("model choice survives first send, follow-ups, new sessions and reloads", async ({
  page,
  context,
}) => {
  const fixture = await enter(page, context);
  const select = page.getByLabel("选择 Chat 主模型");
  const input = page.getByLabel("分析问题");
  await select.selectOption(providerId);
  await input.fill("记住指定模型");
  await input.press("Enter");
  await expect(select).toBeEnabled();
  await expect(select).toHaveValue(providerId);
  const firstUrl = page.url();
  await input.fill("继续用这个模型");
  await input.press("Enter");
  await expect.poll(() => fixture.requests.length).toBe(2);
  expect(fixture.requests[1].selection).toEqual({ mode: "fixed", provider_id: providerId });
  await expect(select).toBeEnabled();
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(select).toBeEnabled();
  await expect(select).toHaveValue(providerId);
  await select.selectOption("auto");
  await input.fill("记住自动选择");
  await input.press("Enter");
  await expect.poll(() => fixture.requests.length).toBe(3);
  await expect(select).toBeEnabled();
  expect(fixture.requests[2].selection).toEqual({ mode: "auto", provider_id: null });
  const secondUrl = page.url();
  await page.goto(firstUrl);
  await expect(select).toBeEnabled();
  await expect(select).toHaveValue(providerId);
  await page.reload();
  await expect(select).toBeEnabled();
  await expect(select).toHaveValue(providerId);
  await page.goto(secondUrl);
  await expect(select).toBeEnabled();
  await expect(select).toHaveValue("auto");
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(select).toBeEnabled();
  await expect(select).toHaveValue("auto");
});

for (const width of [1440, 390]) {
  test(`session navigation keeps the conversation page during slow reads and errors at ${width}px`, async ({
    page,
    context,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    const fixture = await enter(page, context);
    const input = page.getByLabel("分析问题");
    const select = page.getByLabel("选择 Chat 主模型");
    await input.fill("第一段会话");
    await input.press("Enter");
    await expect(select).toBeEnabled();
    const firstUrl = page.url();
    await page.getByRole("button", { name: "新建对话", exact: true }).click();
    await expect(page.locator(".analysis-intro")).toBeVisible();
    await input.fill("第二段会话");
    await input.press("Enter");
    await expect(select).toBeEnabled();
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let fail = true;
    await page.route("**/analysis/sessions/*/messages?**", async (route) => {
      await held;
      if (fail)
        return route.fulfill({
          status: 503,
          headers: { "X-Compass-View-Revision": fixture.revision },
          json: { error: { code: "unavailable", message: "会话读取暂时不可用" } },
        });
      await route.fallback();
    });
    await page.evaluate(() => {
      document.documentElement.dataset.newAnalysisFlashed = "false";
      new MutationObserver((records) => {
        if (
          records.some((record) =>
            Array.from(record.addedNodes).some(
              (node) =>
                node instanceof Element &&
                (node.matches(".analysis-intro, .analysis-suggestions") ||
                  node.querySelector(".analysis-intro, .analysis-suggestions")),
            ),
          )
        )
          document.documentElement.dataset.newAnalysisFlashed = "true";
      }).observe(document.body, { childList: true, subtree: true });
    });
    if (width === 390) await page.getByRole("button", { name: "展开导航" }).click();
    await page
      .locator(`a[href="${new URL(firstUrl).pathname + new URL(firstUrl).search}"]:visible`)
      .first()
      .click();
    await expect(page.getByText("正在读取会话…", { exact: true })).toBeVisible();
    await expect(page.locator("section[data-chat-state]")).toHaveAttribute(
      "data-chat-state",
      "started",
    );
    await expect(page.locator(".analysis-intro, .analysis-suggestions")).toHaveCount(0);
    await expectNoOverflow(page);
    release();
    const retry = page.getByRole("button", { name: "重新加载会话", exact: true });
    await expect(retry).toBeVisible();
    fail = false;
    await retry.focus();
    await page.keyboard.press("Enter");
    await expect(select).toBeEnabled();
    await expect(page.locator(".model-chat-history")).toContainText("第一段会话");
    await expect(page.locator("html")).toHaveAttribute("data-new-analysis-flashed", "false");
    await expectNoOverflow(page);
    await page.screenshot({ path: `../../work/chat-session-${width}.png`, fullPage: true });
  });
}

test("连续的数据库查询分别更新摘要，刷新会话后可查看对应的入参和结果", async ({
  page,
  context,
}) => {
  const fixture = await enter(page, context);
  fixture.holdNext();
  await page.getByLabel("分析问题").fill("核对付款记录和退款记录");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect.poll(() => fixture.runs.size).toBe(1);
  const toggle = page.locator(".analysis-process-toggle");
  const sql = "SELECT COUNT(*) AS n FROM payments";
  const first = {
    id: "98900000-0000-4000-8000-000000000321",
    revision: 1,
    kind: "tool",
    status: "running",
    title: "查询数据库",
    origin: { kind: "builtin", toolName: "database_query" },
    startedAtMs: 1000,
    display: { policy: "summary" },
    detail: { input: JSON.stringify({ op: "query", sql }) },
  } as const;
  fixture.publishExecution(first);
  await expect(toggle).toContainText(`执行 ${sql}`);
  await expect(toggle).not.toContainText("返回");
  const output = (n: number) =>
    JSON.stringify({ status: "ok", columns: ["n"], rows: [[n]], truncated: false });
  fixture.publishExecution({
    ...first,
    revision: 2,
    endedAtMs: 2000,
    status: "completed",
    detail: { ...first.detail, output: output(128) },
  });
  await expect(toggle).toContainText("返回 1 行 · n=128");
  fixture.publishExecution({
    ...first,
    id: "98900000-0000-4000-8000-000000000322",
    startedAtMs: 2100,
    endedAtMs: 2200,
    status: "completed",
    detail: {
      input: JSON.stringify({ op: "query", sql: "SELECT COUNT(*) AS n FROM refunds" }),
      output: output(3),
    },
  });
  fixture.release();
  await expect(toggle).toContainText("已完成");
  await page.reload();
  await toggle.press("Enter");
  const queries = page.locator('.ui-execution-timeline > [data-kind="tool"]');
  await expect(queries.locator(".ui-timeline-text")).toHaveText([
    /FROM payments.*返回 1 行 · n=128$/,
    /FROM refunds.*返回 1 行 · n=3$/,
  ]);
  await queries.last().getByRole("button").press("Enter");
  await expect(queries.last().getByRole("region", { name: "调用入参" })).toContainText(
    "FROM refunds",
  );
  await expect(queries.last().getByRole("region", { name: "调用结果" })).toContainText(
    '"rows":[[3]]',
  );
});

test("completed answers remain in server history and durable CSV downloads retain server bytes and filename", async ({
  page,
  context,
}) => {
  const model = await enter(page, context);
  const input = page.getByLabel("分析问题");
  await page.getByLabel("选择 Chat 主模型").selectOption(providerId);
  await input.fill("第一问：比较渠道");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.locator(".analysis-final-answer")).toHaveCount(1);

  const downloadLink = page.getByRole("link", { name: /^下载数据 · retail-products-/ });
  await expect(downloadLink).toHaveAttribute("download", /^retail-products-[a-f0-9-]{36}\.csv$/);
  await expect(downloadLink).toHaveAttribute("href", /\/analysis\/runs\/[^/]+\/artifacts\/[^?]+\?/);
  // Chromium's native download attribute bypasses Playwright routing. Preserve its contract
  // above, then let the fixture's Content-Disposition deliver the same filename and bytes.
  // The unchanged native link is exercised separately against the real Agent artifact API.
  await downloadLink.evaluate((link) => link.removeAttribute("download"));
  const downloadPromise = page.waitForEvent("download");
  await downloadLink.click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^retail-products-[a-f0-9-]{36}\.csv$/);
  await expect.poll(() => model.artifactDownloads).toBe(1);
  expect(await download.failure()).toBeNull();
  expect(await readFile((await download.path())!, "utf8")).toBe("product,amount\n示例商品,120\n");
  const firstSessionId = new URL(page.url()).searchParams.get("session");
  await page.reload();
  await expect(page.locator(".analysis-final-answer")).toHaveCount(1);

  await input.fill("第二问：继续解释商品贡献");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.locator(".analysis-final-answer")).toHaveCount(2);
  expect(model.requests).toHaveLength(2);
  expect(model.requests[1]).toMatchObject({
    question: "第二问：继续解释商品贡献",
    sessionId: firstSessionId,
  });
  expect(model.requests[1]).not.toHaveProperty("history");
  expect(model.histories[1]).toMatchObject([{ question: "第一问：比较渠道" }]);
  expect(model.histories[1][0].answer).toContain("55. 销售分析示例");
});

test("stop keeps unfinished tools uncertain until the canceled terminal confirms cleanup", async ({
  page,
  context,
}) => {
  const fixture = await enter(page, context);
  fixture.scenario("canceled_confirmed");
  await page.getByLabel("选择 Chat 主模型").selectOption(providerId);
  await page.getByLabel("分析问题").fill("停止这次分析");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  const toolStatus = page.locator(
    '.ui-execution-timeline > [data-kind="tool"] .ui-timeline-status',
  );
  await expect(toolStatus).toHaveText("执行中");

  await page.getByRole("button", { name: "停止任务", exact: true }).click();
  await expect.poll(() => fixture.cancelCount).toBe(1);
  await expect(toolStatus).toHaveText("结果待确认");
  await expect(toolStatus).not.toHaveText("已终止");

  fixture.releaseTerminal();
  await expect(page.locator(".analysis-run-notice")).toContainText("任务已终止");
  await expect(toolStatus).toHaveText("已终止");
});

test("cleanup_unconfirmed leaves unfinished tool cards uncertain", async ({ page, context }) => {
  const fixture = await enter(page, context);
  fixture.scenario("cleanup_unconfirmed");
  await page.getByLabel("选择 Chat 主模型").selectOption(providerId);
  await page.getByLabel("分析问题").fill("触发清理待确认");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  const preparing = page.locator('.ui-execution-timeline > [data-status="preparing"]');
  const process = page.locator(".analysis-process-toggle");
  await expect(process).toContainText("准备工具调用 1");
  await expect(process).toHaveAttribute("aria-expanded", "false");
  const progressSpinner = process.locator(".analysis-loading-spinner");
  await expect(progressSpinner).toBeVisible();
  await process.press("Enter");
  await expect(preparing).toBeVisible();
  fixture.releaseTool();
  await expect(preparing).toHaveCount(0);
  const running = page.locator(
    '.ui-execution-timeline > [data-kind="tool"][data-status="running"]',
  );
  await expect(running).toHaveCount(1);
  await expect(running.locator(".analysis-loading-spinner")).toBeVisible();
  await process.press("Enter");
  await expect(progressSpinner).toBeVisible();
  await expect(page.locator(".analysis-process-toggle")).toContainText("执行中");
  await page.getByRole("button", { name: "停止任务", exact: true }).click();

  await expect(
    page.locator('.ui-execution-timeline > [data-kind="tool"] .ui-timeline-status'),
  ).toHaveText("结果待确认");
  fixture.releaseTerminal();
  await expect(page.locator(".model-chat-turn")).toContainText("执行资源正在核对");
  await expect(page.getByRole("button", { name: "继续任务", exact: true })).toBeDisabled();
  await expect(progressSpinner).toHaveCount(0);
  await expect(
    page.locator('.ui-execution-timeline > [data-kind="tool"] .ui-timeline-status'),
  ).toHaveText("结果待确认");
  await expect(
    page.locator('.ui-execution-timeline > [data-kind="tool"] .ui-timeline-status'),
  ).not.toHaveText("已终止");
});

test("lost active stream reloads blocked Message without resubmitting or reconnecting", async ({
  page,
  context,
}) => {
  const fixture = await enter(page, context);
  fixture.loseFirstStream();
  await page.getByLabel("分析问题").fill("增量丢失后恢复已提交分析");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect.poll(() => fixture.lostStreamResponses).toBe(1);
  const status = page.getByRole("region", { name: "分析过程" });
  await expect(page.locator(".model-chat-turn")).toContainText(
    "增量记录不可用，请保留当前会话并联系管理员。",
  );
  await expect(status).toContainText("已暂停");
  await expect(status.getByRole("button", { name: "继续任务", exact: true })).toBeDisabled();
  await expect(page.locator(".model-chat-turn")).toHaveCount(1);
  const runId = [...fixture.runs.keys()][0];
  await expect(page.locator(".model-chat-turn")).toHaveAttribute("data-run-id", runId);
  await expect(page.locator(".model-chat-turn")).toContainText("增量丢失后恢复已提交分析");
  await expect(page.locator(".model-chat-turn")).toContainText(
    "已核对 POS 渠道，后续商品分析尚未完成。",
  );
  expect(fixture.snapshotReads).toBeGreaterThanOrEqual(2);
  const process = page.locator(".analysis-process-toggle");
  if ((await process.getAttribute("aria-expanded")) === "false") await process.click();
  await expect(page.locator('.ui-execution-timeline > [data-kind="tool"]')).toHaveCount(1);
  await expect(
    page.locator('.ui-execution-timeline > [data-kind="tool"] .ui-timeline-status'),
  ).toHaveText("成功");
  const reads = fixture.snapshotReads;
  // Move beyond multiple reconnect intervals after blocked history is restored.
  await page.clock.install();
  await page.clock.fastForward(6500);
  await expect(page.locator(".model-chat-turn")).toContainText("增量记录不可用");
  expect(fixture.snapshotReads).toBe(reads);
  expect(fixture.streamRequests).toBe(1);
  expect(fixture.requests).toHaveLength(1);
  expect(fixture.cancelCount).toBe(0);
});

test("model changes keep sending disabled until the persisted context reset completes", async ({
  page,
  context,
}) => {
  const fixture = await enter(page, context);
  const input = page.getByLabel("分析问题");
  const select = page.getByLabel("选择 Chat 主模型");
  await input.fill("切换前的分析");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.getByRole("region", { name: "最终回答" })).toContainText("销售分析示例");
  await input.fill("切换后保留的草稿");
  let release = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let resets = 0;
  await page.route("**/api/web/v1/analysis/sessions/*/context-reset", async (route) => {
    resets++;
    await gate;
    const body = route.request().postDataJSON();
    const sessionId = new URL(route.request().url()).pathname.split("/").at(-2);
    await route.fulfill({
      headers: { "X-Compass-View-Revision": fixture.revision, "Cache-Control": "no-store" },
      json: {
        runId: null,
        sessionId,
        version: body.expectedVersion + 1,
        status: "active",
        code: null,
      },
    });
  });
  page.once("dialog", (dialog) => dialog.accept());
  try {
    await select.selectOption(providerId);
    await expect.poll(() => resets).toBe(1);
    await expect(select).toBeDisabled();
    await expect(input).toBeDisabled();
    await expect(page.getByRole("button", { name: "发送消息", exact: true })).toHaveCount(0);
    expect(fixture.requests).toHaveLength(1);
  } finally {
    release();
  }
  await expect(select).toHaveValue(providerId);
  await expect(input).toHaveValue("切换后保留的草稿");
  await expect(page.getByRole("button", { name: "发送消息", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect.poll(() => fixture.requests.length).toBe(2);
  expect(fixture.requests[1]).toMatchObject({
    selection: { mode: "fixed", provider_id: providerId },
  });
});

test("cancel retries a conflicting version from Message and terminates the original run", async ({
  page,
  context,
}) => {
  const fixture = await enter(page, context);
  fixture.scenario("cancel_conflict");
  await page.getByLabel("分析问题").fill("状态更新期间停止任务");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(
    page.locator('.ui-execution-timeline > [data-kind="tool"] .ui-timeline-status'),
  ).toHaveText("执行中");
  const originalRun = await page.locator(".model-chat-turn").getAttribute("data-run-id");
  const beforeReads = fixture.snapshotReads;
  await page.getByRole("button", { name: "停止任务", exact: true }).click();
  await expect.poll(() => fixture.cancelCount).toBe(2);
  expect(fixture.snapshotReads).toBeGreaterThan(beforeReads);
  expect(fixture.controls.map((control) => control.runId)).toEqual([originalRun, originalRun]);
  expect(fixture.controls.map((control) => control.body.expectedVersion)).toEqual([1, 2]);
  expect(fixture.controls[0].body.requestId).not.toBe(fixture.controls[1].body.requestId);
  fixture.releaseTerminal();
  await expect(page.locator(".analysis-run-notice")).toContainText("任务已终止");
  await expect(
    page.locator('.ui-execution-timeline > [data-kind="tool"] .ui-timeline-status'),
  ).toHaveText("已终止");
  expect(fixture.requests).toHaveLength(1);
  await page.reload();
  await expect(page.locator(".model-chat-turn")).toHaveAttribute("data-run-id", originalRun!);
  await expect(page.locator(".analysis-run-notice")).toContainText("任务已终止");
});

test("saved and expired workflow cards restore their persisted state after refresh", async ({
  page,
  context,
}) => {
  const fixture = await enter(page, context);
  const send = async (question: string) => {
    await page.getByLabel("分析问题").fill(question);
    await page.getByRole("button", { name: "发送消息", exact: true }).click();
    await expect(page.getByRole("region", { name: "最终回答" })).toContainText("销售分析示例");
  };
  const card = page.getByRole("region", { name: "保存工作流", exact: true });
  await send("恢复已保存的工作流");
  fixture.publishWorkflow("saved", "每周渠道核对 · 自定义名称");
  await page.reload();
  await expect(card.getByRole("status")).toContainText("已保存「每周渠道核对 · 自定义名称」");
  await expect(card.getByRole("button", { name: "查看工作流详情" })).toHaveCount(0);
  expect(fixture.workflowReads).toBe(0);

  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await send("恢复已到期的保存邀请");
  fixture.publishWorkflow("offered", "过期草稿", {
    expiresAt: new Date(Date.now() - 60_000).toISOString(),
  });
  await page.reload();
  await expect(card.getByRole("status")).toHaveText("保存邀请已失效，分析结果与编排脚本已保留。");
  await expect(card.getByRole("button")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "最终回答" })).toContainText("销售分析示例");

  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await send("服务端已过期的保存邀请");
  fixture.publishWorkflow("offered", "旧保存邀请", { unavailable: true });
  await page.reload();
  await card.getByRole("button", { name: "查看工作流详情" }).click();
  await expect(card.getByRole("alert")).toHaveText("保存邀请已失效，请重新运行分析。");
  expect(fixture.workflowReads).toBe(1);
  expect(fixture.workflowConfirmations).toBe(0);
  expect(fixture.requests).toHaveLength(3);
});

test("dynamic workflow save offer displays the script and function arguments", async ({
  page,
  context,
}) => {
  await page.setViewportSize({ width: 390, height: 900 });
  const fixture = await enter(page, context);
  await page.getByRole("button", { name: "对比渠道实付并保存", exact: true }).click();
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.getByRole("region", { name: "最终回答" })).toBeVisible();
  const source = 'return await agent("sub_agent", { prompt: args.window });';
  fixture.publishWorkflow("offered", "渠道复盘", {
    definition: {
      entryContract: "agent_workflow@1",
      description: "比较输入日期范围内的渠道",
      parameters: { type: "object", properties: { window: { type: "string" } } },
      args: { window: "2025-11-17" },
      scriptSource: source,
    },
  });
  await page.reload();
  const card = page.getByRole("region", { name: "保存工作流", exact: true });
  await card.getByRole("button", { name: "查看工作流详情" }).press("Enter");
  await expect(card.getByLabel("工作流名称")).toBeFocused();
  await expect(card.getByText(source, { exact: true })).toBeVisible();
  await card.locator("summary").filter({ hasText: "查看工具参数" }).press("Enter");
  await expect(card.getByRole("region", { name: "试跑入参" })).toContainText("2025-11-17");
  await expect(card.getByRole("region", { name: "函数参数契约" })).toContainText("window");
  await expectNoOverflow(page);
});

test("the Workflow page preserves viewing and export of retired assets", async ({
  page,
  context,
}) => {
  const fixture = await enter(page, context);
  const asset = {
    assetId: "98700000-0000-4000-8000-000000000004",
    name: "门店每周复盘",
    status: "active",
    definitionVersion: 1,
    entryContract: "channel_attribution@1",
    metadataRevision: 1,
  };
  await page.route("**/api/web/v1/analysis/workflows**", async (route) => {
    const url = new URL(route.request().url());
    const headers = { "X-Compass-View-Revision": fixture.revision, "Cache-Control": "no-store" };
    await route.fulfill({
      headers,
      json: url.pathname.endsWith(asset.assetId)
        ? { ...asset, scriptSource: "return await parallel([]);" }
        : { items: [asset], nextCursor: null },
    });
  });
  await page.goto(`/t/${tenantId}/w/${workspaceId}/plugins/workflows`);
  await page.getByRole("button", { name: /门店每周复盘 · v1 · 已启用/ }).click();
  await expect(page.getByRole("heading", { name: asset.name, exact: true })).toBeFocused();
  await expect(page.getByText(/当前保留查看和导出/)).toBeVisible();
  await expect(page.getByRole("button", { name: "使用", exact: true })).toHaveCount(0);
  const downloadEvent = page.waitForEvent("download");
  const exportButton = page.getByRole("button", { name: "导出脚本" });
  await exportButton.focus();
  await exportButton.press("Enter");
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe(`workflow-${asset.assetId}.js`);
  expect(await readFile((await download.path())!, "utf8")).toBe("return await parallel([]);");
  expect(fixture.requests).toHaveLength(0);
});

test("deletes a workflow after confirmation and keeps the list in sync", async ({
  page,
  context,
}) => {
  const fixture = await enter(page, context);
  const asset = {
    assetId: "98700000-0000-4000-8000-000000000004",
    name: "门店每周复盘",
    status: "disabled",
    definitionVersion: 1,
    entryContract: "agent_workflow@1",
    metadataRevision: 2,
  };
  let deleteRequests = 0;
  let deleted = false;
  await page.route("**/api/web/v1/analysis/workflows**", async (route) => {
    const request = route.request();
    const headers = { "X-Compass-View-Revision": fixture.revision, "Cache-Control": "no-store" };
    if (request.method() === "DELETE") {
      expect(request.postDataJSON()).toEqual({ metadataRevision: 2 });
      deleteRequests += 1;
      if (deleteRequests === 1) {
        return route.fulfill({ status: 503, headers, json: { error: { message: "请重试。" } } });
      }
      deleted = true;
      return route.fulfill({ status: 204, headers });
    }
    return route.fulfill({
      headers,
      json: new URL(request.url()).pathname.endsWith(asset.assetId)
        ? asset
        : { items: deleted ? [] : [asset], nextCursor: null },
    });
  });
  await page.goto(`/t/${tenantId}/w/${workspaceId}/plugins/workflows`);
  const row = page.getByRole("button", { name: /门店每周复盘 · v1 · 已停用/ });
  await row.press("Enter");
  const remove = page.getByRole("button", { name: "删除", exact: true });
  await remove.press("Enter");
  const dialog = page.getByRole("dialog", { name: "删除工作流" });
  const cancel = dialog.getByRole("button", { name: "取消" });
  await expect(cancel).toBeFocused();
  await cancel.press("Escape");
  await expect(remove).toBeFocused();
  expect(deleteRequests).toBe(0);

  await page.setViewportSize({ width: 390, height: 820 });
  await remove.press("Enter");
  await expect(dialog).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(
    true,
  );
  const bounds = (await dialog.boundingBox())!;
  expect(bounds.x).toBeGreaterThanOrEqual(0);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(390);
  const confirm = dialog.getByRole("button", { name: "确认删除" });
  await confirm.click();
  await expect(dialog.getByRole("alert")).toHaveText("请重试。");
  await expect(dialog).toBeVisible();
  await confirm.click();
  await expect(dialog).toBeHidden();
  await expect(row).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "已保存工作流" })).toBeFocused();
  await expect(page.getByText("工作流已删除。", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("还没有保存的工作流。", { exact: true })).toBeVisible();
});

test("cancel racing completion reads the committed result from Message and preserves the original run", async ({
  page,
  context,
}) => {
  const fixture = await enter(page, context);
  fixture.scenario("already_terminal_completed");
  await page.getByLabel("选择 Chat 主模型").selectOption(providerId);
  await page.getByLabel("分析问题").fill("完成态竞态");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.locator('.ui-execution-timeline > [data-kind="tool"]')).toHaveCount(1);
  await page.getByRole("button", { name: "停止任务", exact: true }).click();

  await expect(page.locator(".analysis-final-answer")).toContainText("已持久保存的完整回答。");
  const processToggle = page.locator(".analysis-process-toggle");
  await expect(processToggle).toHaveAttribute("aria-expanded", "false");
  expect(fixture.cancelCount).toBe(1);
  expect(fixture.requests).toHaveLength(1);
  expect(fixture.controls[0].body).toMatchObject({ expectedVersion: 1 });
  await page.reload();
  await expect(page.locator(".analysis-final-answer")).toHaveCount(1);
  await expect(page.getByRole("region", { name: "分析过程" })).toContainText("已完成");
});

test("broken stream and unavailable Message withhold unconfirmed content until history reload recovers it", async ({
  page,
  context,
}) => {
  const fixture = await enter(page, context);
  fixture.scenario("already_terminal_disconnected");
  await page.getByLabel("选择 Chat 主模型").selectOption(providerId);
  await page.getByLabel("分析问题").fill("终态结果流断开");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.locator('.ui-execution-timeline > [data-kind="tool"]')).toHaveCount(1);
  await page.getByRole("button", { name: "停止任务", exact: true }).click();

  await expect(page.locator("section[data-chat-state]").getByRole("alert")).toContainText(
    "会话读取暂时不可用",
  );
  await expect(page.locator(".analysis-final-answer")).toHaveCount(0);
  await expect(page.getByRole("link", { name: /^下载数据/ })).toHaveCount(0);
  fixture.releaseTerminal();
  await expect(page.getByRole("button", { name: "重新加载会话", exact: true })).toHaveCount(0);
  await expect(page.locator(".analysis-error-toast")).toHaveCount(0, { timeout: 7000 });
  await page.reload();
  await expect(page.locator(".analysis-final-answer")).toContainText("已持久保存的完整回答。");
  expect(fixture.requests).toHaveLength(1);
  expect(fixture.cancelCount).toBe(1);
});

test("stream completion collapses the process and returns focus from a tool to its disclosure", async ({
  page,
  context,
}) => {
  const fixture = await enter(page, context);
  fixture.scenario("canceled_confirmed");
  await page.getByLabel("分析问题").fill("完成后返回过程焦点");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  const toggle = page.locator(".analysis-process-toggle");
  await toggle.press("Enter");
  const summary = page.locator('.ui-execution-timeline > [data-kind="tool"] .ui-timeline-row');
  await summary.focus();
  await expect(summary).toBeFocused();
  fixture.finishInteractive();
  await expect(page.locator(".analysis-final-answer")).toContainText("已持久保存的完整回答。");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(toggle).toBeFocused();
});

test("leaving a running session keeps background execution and restores its result without cancellation", async ({
  page,
  context,
}) => {
  const fixture = await enter(page, context);
  fixture.holdNext();
  await page.getByLabel("分析问题").fill("在后台继续分析");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.locator("section[data-chat-state]")).toHaveAttribute(
    "data-chat-state",
    "started",
  );
  const originalUrl = page.url();
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(page.locator("section[data-chat-state]")).toHaveAttribute(
    "data-chat-state",
    "empty",
  );
  expect(fixture.cancelCount).toBe(0);
  fixture.release();
  await page.goto(originalUrl);
  await expect(page.getByRole("region", { name: "最终回答" }).locator("ol > li")).toHaveCount(55);
  await expect(page.locator(".analysis-markdown")).toHaveAttribute("data-revealing", "false");
  expect(fixture.requests).toHaveLength(1);
  expect(fixture.cancelCount).toBe(0);
});

test("attachment rail accepts ten local files, scrolls in one row, removes and reselects without sending files", async ({
  page,
  context,
}) => {
  const model = await enter(page, context);
  await page.getByLabel("选择 Chat 主模型").selectOption(providerId);
  await page.getByLabel("分析问题").fill("查看附件");
  const chooser = page.locator("input[type=file]");
  await chooser.setInputFiles(Array.from({ length: 11 }, (_, index) => file(index + 1)));
  const cards = page.locator(".composer-attachment");
  await expect(cards).toHaveCount(10);
  await expect(page.locator(".analysis-composer-dock").getByRole("alert")).toContainText("10");
  await expect(page.locator(".analysis-composer-dock")).toContainText("暂不支持");
  const rail = page.locator(".composer-attachment-list");
  const positions = await cards.evaluateAll((elements) =>
    elements.map((element) => element.getBoundingClientRect().y),
  );
  expect(Math.max(...positions) - Math.min(...positions)).toBeLessThan(2);
  const arrows = page.locator(".composer-attachment-scroll");
  await expect(arrows).toHaveCount(2);
  await expect(arrows.first()).toBeDisabled();
  await expect(arrows.last()).toBeEnabled();
  await arrows.last().click();
  await expect.poll(() => rail.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
  async function scrollToEdge(direction: -1 | 1) {
    for (let index = 0; index < 12; index++) {
      const { left, end } = await rail.evaluate((element) => ({
        left: element.scrollLeft,
        end: element.scrollWidth - element.clientWidth,
      }));
      if (direction === 1 ? left >= end - 1 : left <= 1) return;
      const arrow = direction === 1 ? arrows.last() : arrows.first();
      await expect(arrow).toBeEnabled();
      await arrow.click();
      await expect.poll(() => rail.evaluate((element) => element.scrollLeft)).not.toBe(left);
    }
    throw new Error("Attachment rail did not reach its edge");
  }
  await scrollToEdge(1);
  await expect(arrows.last()).toBeDisabled();
  await expect(arrows.first()).toBeEnabled();
  await scrollToEdge(-1);
  await expect(arrows.first()).toBeDisabled();
  const send = page.getByRole("button", { name: "发送消息", exact: true });
  if (await send.isEnabled()) await send.click();
  expect(model.requests).toHaveLength(0);
  await cards.first().getByRole("button").click();
  await expect(cards).toHaveCount(9);
  await chooser.setInputFiles(file(1));
  await expect(cards).toHaveCount(10);
  while (await cards.count()) await cards.first().getByRole("button").click();
  await expect(cards).toHaveCount(0);
  await expect(send).toBeEnabled();
  await send.click();
  await expect(page.locator(".model-chat-history")).toContainText("销售分析示例");
  expect(model.requests).toHaveLength(1);
  expect(model.requests[0]).not.toHaveProperty("attachments");
  await chooser.setInputFiles(file(1));
  await cards.first().getByRole("button").click();
  await page.getByLabel("分析问题").fill("尚未发送的补充问题");
  const navigationDialog = page.waitForEvent("dialog");
  const navigating = page.getByRole("link", { name: "报告空间", exact: true }).click();
  const confirmation = await navigationDialog;
  expect(confirmation.type()).toBe("confirm");
  expect(confirmation.message()).toContain("离开");
  await confirmation.dismiss();
  await navigating;
  await expect(page.locator(".model-chat-history")).toContainText("销售分析示例");
  await chooser.setInputFiles(file(1));
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(cards).toHaveCount(0);
  await expect(page.locator("section[data-chat-state]")).toHaveAttribute(
    "data-chat-state",
    "empty",
  );
  await chooser.setInputFiles(file(1));
  await expect(cards).toHaveCount(1);
});

for (const width of [375, 768, 1440]) {
  test(`composer fits ${width}px with centered, attached and docked states`, async ({
    page,
    context,
  }) => {
    await page.setViewportSize({ width, height: width === 375 ? 812 : 1000 });
    await enter(page, context);
    await expectNoOverflow(page);
    const select = page.getByLabel("选择 Chat 主模型");
    const attach = page.getByRole("button", { name: "添加附件", exact: true });
    const send = page.getByRole("button", { name: "发送消息", exact: true });
    const selectBox = await select.boundingBox();
    const attachBox = await attach.boundingBox();
    const sendBox = await send.boundingBox();
    const main = await page.locator(".analysis-main").boundingBox();
    const composer = await page.locator(".analysis-composer").boundingBox();
    expect(composer!.y + composer!.height - (sendBox!.y + sendBox!.height)).toBeLessThanOrEqual(11);
    expect(main!.width - composer!.width).toBeLessThanOrEqual(54);
    await expect(page.locator(".analysis-welcome")).toHaveCSS("scrollbar-width", "none");
    expect(selectBox!.x).toBeLessThan(attachBox!.x);
    expect(attachBox!.x).toBeLessThan(sendBox!.x);
    expect(Math.abs(selectBox!.y - sendBox!.y)).toBeLessThan(14);
    await page.screenshot({
      path: `../../work/lesson-19/composer-empty-${width}.png`,
      fullPage: true,
    });
    await page
      .locator("input[type=file]")
      .setInputFiles(Array.from({ length: 10 }, (_, index) => file(index + 1)));
    await expect(page.locator(".composer-attachment")).toHaveCount(10);
    await expectNoOverflow(page);
    await page.screenshot({
      path: `../../work/lesson-19/composer-attachments-${width}.png`,
      fullPage: true,
    });
    const cards = page.locator(".composer-attachment");
    while (await cards.count()) await cards.first().getByRole("button").click();
    await select.selectOption(providerId);
    await page.getByLabel("分析问题").fill("分析本月销售趋势");
    await send.click();
    const answer = page.getByRole("region", { name: "最终回答" });
    await expect(answer.locator("ol > li")).toHaveCount(55);
    await expectNoOverflow(page);
    if (width === 375) {
      for (const content of [
        answer.getByRole("region", { name: "数据表格" }),
        answer.locator("pre"),
      ]) {
        expect(await content.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(
          true,
        );
      }
    }
    const dock = await page.locator(".analysis-composer-dock").boundingBox();
    expect(main!.width - dock!.width).toBeLessThanOrEqual(48);
    expect(page.viewportSize()!.height - (dock!.y + dock!.height)).toBeLessThan(90);
    await page.screenshot({
      path: `../../work/lesson-19/composer-started-${width}.png`,
      fullPage: true,
    });
  });
}

test("docked composer keeps attachment removal and send reachable in short mobile viewports", async ({
  page,
  context,
}) => {
  await page.setViewportSize({ width: 375, height: 350 });
  const model = await enter(page, context);
  await page.getByLabel("选择 Chat 主模型").selectOption(providerId);
  await page.getByLabel("分析问题").fill("分析销售趋势");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.locator(".model-chat-history")).toContainText("销售分析示例");
  await page.getByLabel("分析问题").fill("查看所选附件");
  await page
    .locator("input[type=file]")
    .setInputFiles(Array.from({ length: 10 }, (_, index) => file(index + 1)));
  for (const height of [300, 350]) {
    await page.setViewportSize({ width: 375, height });
    const dock = page.locator(".analysis-composer-dock");
    const dockBox = await dock.boundingBox();
    const welcomeBox = await page.locator(".analysis-welcome").boundingBox();
    const mainBox = await page.getByRole("main").boundingBox();
    expect(dockBox!.y + dockBox!.height).toBeLessThanOrEqual(
      welcomeBox!.y + welcomeBox!.height + 1,
    );
    expect(dockBox!.y + dockBox!.height).toBeLessThanOrEqual(mainBox!.y + mainBox!.height + 1);
    const send = page.getByRole("button", { name: "发送消息", exact: true });
    await send.scrollIntoViewIfNeeded();
    const sendBox = await send.boundingBox();
    expect(sendBox!.y).toBeGreaterThanOrEqual(0);
    expect(sendBox!.y + sendBox!.height).toBeLessThanOrEqual(height + 1);
    if (await send.isEnabled()) await send.click();
    expect(model.requests).toHaveLength(1);
    const remove = page.locator(".composer-attachment").first().getByRole("button");
    await remove.scrollIntoViewIfNeeded();
    const removeBox = await remove.boundingBox();
    expect(removeBox!.y).toBeGreaterThanOrEqual(0);
    expect(removeBox!.y + removeBox!.height).toBeLessThanOrEqual(height + 1);
    await remove.click();
    await expect(page.locator(".composer-attachment")).toHaveCount(height === 300 ? 9 : 8);
    const notice = page.locator(".composer-attachment-notice");
    await notice.scrollIntoViewIfNeeded();
    const noticeBox = await notice.boundingBox();
    const visibleDockBox = await dock.boundingBox();
    expect(noticeBox!.y).toBeGreaterThanOrEqual(visibleDockBox!.y - 1);
    expect(noticeBox!.y + noticeBox!.height).toBeLessThanOrEqual(
      visibleDockBox!.y + visibleDockBox!.height + 1,
    );
    expect(noticeBox!.y + noticeBox!.height).toBeLessThanOrEqual(height + 1);
    await expectNoOverflow(page);
    await page.screenshot({
      path: `../../work/lesson-19/composer-started-short-${height}.png`,
      fullPage: true,
    });
  }
});

test("planned Retail preset updates only the dock and hides completed tasks", async ({
  page,
  context,
}) => {
  const fixture = await enter(page, context);
  await page.emulateMedia({ reducedMotion: "reduce" });
  fixture.scenario("plan");

  const input = page.getByRole("textbox", { name: "分析问题" });
  await page.getByRole("button", { name: "先列计划 · 渠道与商品诊断" }).click();
  await expect(input).toHaveValue(/^比较 2025-11-17/);
  await expect(page.getByRole("checkbox", { name: "先列计划" })).toBeChecked();
  await expect(page.getByLabel("选择 Chat 主模型")).toHaveValue("auto");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect.poll(() => fixture.requests[0]?.constraints).toEqual({ plan: "required" });
  const panel = page.getByRole("region", { name: "分析计划" });
  const disclosure = panel.getByRole("button");
  const list = panel.getByRole("list", { name: "计划事项" });
  const composer = page.locator(".analysis-composer");
  const ring = panel.locator(".analysis-todo-ring-progress");
  await expect(disclosure).toHaveAttribute("aria-expanded", "false");
  await expect(disclosure).toHaveText("第 1 / 2 步·比较渠道实付金额");
  await expect(ring).toHaveAttribute("stroke-dashoffset", "1");
  await expect(list).toBeHidden();
  await expect(page.locator(".analysis-todo + .analysis-composer-dock")).toBeVisible();
  const composerBox = await composer.boundingBox();

  await disclosure.hover();
  await expect(disclosure).toHaveAttribute("aria-expanded", "true");
  await expect(list.getByRole("listitem")).toHaveCount(2);
  await expect(list.getByText("待处理", { exact: true })).toBeVisible();
  await expect(
    list.locator('[aria-current="step"]').getByText("进行中", { exact: true }),
  ).toBeVisible();
  await expect(list.locator('[aria-current="step"]')).toHaveCSS("animation-name", "none");
  expect((await list.boundingBox())!.y).toBeLessThan((await disclosure.boundingBox())!.y);
  expect(await composer.boundingBox()).toEqual(composerBox);
  await list.hover();
  await expect(list).toBeVisible();
  await page.getByRole("button", { name: "新建对话" }).hover();
  await expect(disclosure).toHaveAttribute("aria-expanded", "false");
  await expect(list).toBeHidden();

  await disclosure.focus();
  await page.keyboard.press("Enter");
  await expect(list).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(list).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(disclosure).toBeFocused();
  await expect(list).toBeHidden();

  fixture.advancePlan();
  await expect(disclosure).toHaveText("第 2 / 2 步·分析商品贡献");
  await expect(ring).toHaveAttribute("stroke-dashoffset", "0.5");
  await expect(list).toBeHidden();
  await page.setViewportSize({ width: 390, height: 520 });
  await disclosure.click();
  await expect(list.getByRole("listitem")).toHaveCount(2);
  await expect(list.locator('[data-status="verified"] .analysis-todo-label')).toHaveCSS(
    "text-decoration-line",
    "line-through",
  );
  await expect(list.locator('[aria-current="step"]')).toContainText("分析商品贡献");
  await expectNoOverflow(page);
  expect((await list.boundingBox())!.y).toBeGreaterThanOrEqual(0);
  await page.setViewportSize({ width: 667, height: 320 });
  const shortListBox = (await list.boundingBox())!;
  expect(shortListBox.y).toBeGreaterThan((await disclosure.boundingBox())!.y);
  expect(shortListBox.y + shortListBox.height).toBeLessThanOrEqual(320);
  await disclosure.click();
  await expect(list).toBeHidden();
  await expect(page.locator(".model-chat-turn")).not.toContainText("分析商品贡献");

  fixture.finishPlan();
  await expect(panel.getByRole("status")).toContainText("计划已完成");
  await expect(page.getByLabel("最终回答")).toContainText("已核对两周渠道变化与商品贡献。");
  await expect(panel).toBeHidden();
});

for (const [label, field, initial, edited, constraints] of [
  ["最多两轮 · 渠道与商品诊断", "最多轮次", "2", "3", { plan: "required", maxSteps: 3 }],
  ["5 秒超时 · 渠道与商品诊断", "时限（秒）", "5", "8", { timeoutSeconds: 8 }],
] as const) {
  test(`${label} sends the edited question and constraints`, async ({ page, context }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const fixture = await enter(page, context);
    const preset = page.getByRole("button", { name: label });
    await preset.focus();
    await page.keyboard.press("Enter");
    const control = page.getByRole("spinbutton", { name: field });
    await expect(control).toHaveValue(initial);
    await control.fill(edited);
    await page.getByRole("textbox", { name: "分析问题" }).fill("对比渠道数据并解释变化");
    await expectNoOverflow(page);
    await page.getByRole("button", { name: "发送消息" }).click();
    await expect
      .poll(() => fixture.requests[0])
      .toMatchObject({
        question: "对比渠道数据并解释变化",
        constraints,
      });
  });
}

for (const width of [1440, 390]) {
  test(`generic examples send ordinary questions with keyboard at ${width}px`, async ({
    page,
    context,
  }) => {
    await page.setViewportSize({ width, height: 1000 });
    const fixture = await enter(page, context);
    const example = page.getByRole("button", {
      name: width === 1440 ? "对比渠道实付并保存" : "换两周对比渠道实付",
      exact: true,
    });
    await example.focus();
    await example.press("Enter");
    const input = page.getByLabel("分析问题");
    await expect(input).toBeFocused();
    const question = await input.inputValue();
    await expectNoOverflow(page);
    await input.press("Enter");
    await expect.poll(() => fixture.requests.length).toBe(1);
    expect(fixture.requests[0]).toMatchObject({ question, sessionId: null });
    expect(fixture.requests[0]).not.toHaveProperty("showcase");
    expect(fixture.requests[0]).not.toHaveProperty("workflowAssetRef");
    expect(fixture.requests[0]).not.toHaveProperty("constraints.verification");
    await expect(page.getByRole("region", { name: "最终回答" })).toBeVisible();
    await page.screenshot({ path: `../../work/978-composer-${width}.png`, fullPage: true });
    await expectNoOverflow(page);
  });
}

for (const width of [1440, 390]) {
  test(`Goal slash mode and floating management work at ${width}px`, async ({ page, context }) => {
    await page.setViewportSize({ width, height: 900 });
    const fixture = await enter(page, context);
    const input = page.getByLabel("分析问题");
    const commands = page.getByRole("listbox", { name: "输入命令" });
    await input.fill("/");
    await expect(commands).toBeVisible();
    await page.screenshot({
      path: `../../work/chat-ui-fix/goal-menu-${width}.png`,
      fullPage: true,
    });
    await expect(commands.getByRole("option")).toContainText("Goal");
    await input.press("Escape");
    await expect(commands).toBeHidden();
    expect(fixture.requests).toHaveLength(0);
    await input.fill("销售/库存");
    await expect(commands).toBeHidden();
    await input.fill("/g");
    await input.dispatchEvent("keydown", { key: "Enter", isComposing: true });
    await expect(commands).toBeVisible();
    await input.press("ArrowDown");
    await input.press("Enter");
    const draft = page.getByRole("region", { name: "目标模式", exact: true });
    await expect(draft).toBeVisible();
    await expect(input).toBeFocused();
    await expect(input).toHaveValue("");
    await expect(page.getByRole("button", { name: "发送消息", exact: true })).toBeDisabled();
    await input.fill("完成本月销售与库存复盘");
    await expect(draft).toContainText("完成本月销售与库存复盘");
    await expect(draft.locator("svg.lucide-target")).toHaveCount(1);
    const form = page.locator(".analysis-composer");
    expect(
      (await draft.boundingBox())!.y + (await draft.boundingBox())!.height,
    ).toBeLessThanOrEqual((await form.boundingBox())!.y + 1);
    await draft.getByRole("button", { name: "退出目标模式" }).click();
    await expect(draft).toBeHidden();
    await expect(input).toHaveValue("完成本月销售与库存复盘");
    await expect(input).toBeFocused();
    await input.fill("/goal");
    if (width === 390) await commands.getByRole("option").click();
    else await input.press("Tab");
    await input.fill("完成本月销售与库存复盘");
    await page.route(
      "**/analysis/sessions/submit",
      (route) =>
        route.fulfill({
          status: 503,
          headers: { "X-Compass-View-Revision": fixture.revision },
          json: { error: { code: "unavailable", message: "提交暂时不可用，请重试" } },
        }),
      { times: 1 },
    );
    await input.press("Enter");
    await expect(page.locator(".analysis-welcome").getByRole("alert")).toContainText("请重试");
    await expect(draft).toBeVisible();
    await expect(input).toHaveValue("完成本月销售与库存复盘");

    type Goal = import("../../src/client/analysis-goal").Goal;
    let goal: Goal | null = null;
    let failAction = true;
    const actions: string[] = [];
    const headers = { "X-Compass-View-Revision": fixture.revision };
    await page.route("**/analysis/sessions/*/goal?**", (route) =>
      route.fulfill({ headers, json: { goal, runId: null } }),
    );
    await page.route("**/analysis/sessions/*/goal/actions", (route) => {
      const action = route.request().postDataJSON().action;
      if (failAction) {
        failAction = false;
        return route.fulfill({
          status: 503,
          headers,
          json: { error: { code: "unavailable", message: "稍后重试" } },
        });
      }
      actions.push(action);
      goal = {
        ...goal!,
        revision: goal!.revision + 1,
        status: action === "pause" ? "paused" : action === "resume" ? "active" : "canceled",
      };
      return route.fulfill({ headers, json: { goal, runId: null } });
    });
    fixture.scenario("plan");
    fixture.holdNext();
    await input.press("Enter");
    await expect.poll(() => fixture.requests.length).toBe(1);
    expect(fixture.requests[0].question).toContain("先拟定目标和验收条件供我确认");
    expect(fixture.requests[0].question).toContain("完成本月销售与库存复盘");
    expect(fixture.requests[0].constraints?.toolSelection?.packs).toContain("long_task");
    await expect(draft).toContainText("准备中");
    goal = {
      goalId: "99300000-0000-4000-8000-000000000401",
      revision: 1,
      status: "active",
      reason: null,
      reasonCode: null,
      contract: {
        objective: "完成本月销售与库存复盘",
        acceptance: [{ id: "a1", text: "核对销售与库存结论" }],
        constraints: [],
      },
      satisfied: [],
      budget: { maxRounds: 12, tokens: 2000000, costMicrousd: 20000000 },
      usage: {
        rounds: 2,
        tokens: 10000,
        costMicrousd: 10000,
        tokensPct: 0.5,
        costPct: 0.05,
        overrun: false,
      },
      counts: { openTodos: 1, conflicts: 0, stale: 0, reusedCalls: 0 },
    };
    const panel = page.getByRole("region", { name: "长任务目标", exact: true });
    const toggle = panel.locator(".analysis-goal-heading");
    await expect(toggle).toContainText("进行中");
    const plan = page.getByRole("region", { name: "分析计划", exact: true });
    await expect(plan).toBeVisible();
    expect((await panel.boundingBox())!.y).toBeLessThan((await plan.boundingBox())!.y);
    fixture.finishPlan();
    await expect(plan).toBeHidden();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(page.locator(".model-chat-history .analysis-goal-panel")).toHaveCount(0);
    await expect(panel.locator("svg.lucide-target")).toHaveCount(1);
    const panelBox = (await panel.boundingBox())!;
    const formBox = (await form.boundingBox())!;
    expect(panelBox.y + panelBox.height).toBeLessThanOrEqual(formBox.y + 1);
    expect(formBox.y - (panelBox.y + panelBox.height)).toBeLessThan(16);
    if (width === 1440) {
      await toggle.hover();
      await expect(toggle).toHaveAttribute("aria-expanded", "true");
      await page.getByRole("button", { name: "新建对话", exact: true }).hover();
      await expect(toggle).toHaveAttribute("aria-expanded", "false");
    }
    await toggle.focus();
    await toggle.press("Enter");
    await expect(panel.getByLabel("目标详情")).toBeVisible();
    const details = panel.locator(".analysis-goal-details");
    expect((await details.boundingBox())!.y).toBeGreaterThanOrEqual(0);
    await page.screenshot({ path: `../../work/chat-ui-fix/goal-${width}.png`, fullPage: true });
    await expectNoOverflow(page);
    await panel.getByRole("button", { name: "暂停目标" }).click();
    await expect(panel.getByRole("alert")).toContainText("目标操作失败");
    await panel.getByRole("button", { name: "暂停目标" }).focus();
    await page.keyboard.press("Enter");
    await expect(toggle).toContainText("已暂停");
    await panel.getByRole("button", { name: "恢复目标" }).click();
    await expect(toggle).toContainText("进行中");
    await panel.getByRole("button", { name: "修改目标", exact: true }).click();
    await expect(input).toBeFocused();
    await expect(input).toHaveValue("修改目标：");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await input.fill("");
    await toggle.focus();
    await toggle.press("Enter");
    await panel.getByRole("button", { name: "取消目标" }).focus();
    await page.keyboard.press("Escape");
    await expect(toggle).toBeFocused();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await toggle.press("Enter");
    page.once("dialog", (dialog) => dialog.dismiss());
    await panel.getByRole("button", { name: "取消目标" }).click();
    expect(actions).toEqual(["pause", "resume"]);
    page.once("dialog", (dialog) => dialog.accept());
    await panel.getByRole("button", { name: "取消目标" }).click();
    await expect(toggle).toContainText("已取消");
    expect(actions).toEqual(["pause", "resume", "cancel"]);
    await page.reload();
    await expect(toggle).toContainText("已取消");
    await expectNoOverflow(page);
  });
}
