import { expect, test, type Page } from "@playwright/test";

const tenant = "99500000-0000-4000-8000-000000000010";
const workspace = "99500000-0000-4000-8000-000000000020";
const origin = `http://127.0.0.1:${process.env.WEB_TEST_PORT ?? 8110}`;
const query = new URLSearchParams({ tenantId: tenant, workspaceId: workspace });
const analysis = `/t/${tenant}/w/${workspace}/analysis`;
test.skip(
  process.env.COMPASS_GOAL_E2E !== "true",
  "Requires isolated Goal runtime and model upstream.",
);
test.setTimeout(180000);

async function login(page: Page) {
  const response = await page.request.post("/api/web/v1/auth/login", {
    headers: { origin },
    data: { loginName: "teaching.admin", password: process.env.WEB_TEST_PASSWORD },
  });
  expect(response.status(), await response.text()).toBe(200);
  await page.goto(analysis);
  await page.getByLabel("选择 Chat 主模型").selectOption(process.env.COMPASS_GOAL_E2E_PROVIDER_ID!);
}

async function propose(page: Page) {
  const response = page.waitForResponse(
    (r) => r.url().endsWith("/analysis/sessions/submit") && r.request().method() === "POST",
  );
  await page
    .getByLabel("分析问题")
    .fill("请持续完成月度销售与库存复盘，分别核对销售金额和库存数量。");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  const submitted = await response;
  expect(submitted.status(), await submitted.text()).toBe(202);
  const receipt = await submitted.json();
  await expect(page.getByRole("region", { name: "执行确认" })).toContainText("确认长任务目标", {
    timeout: 65000,
  });
  return receipt as { sessionId: string; runId: string };
}

test("确认目标后自动推进两个子任务，完成并刷新授权子树", async ({ page }) => {
  await login(page);
  const { sessionId } = await propose(page);
  const card = page.getByRole("region", { name: "执行确认" });
  await expect(card).toContainText("核对销售金额");
  await expect(card).toContainText("核对库存数量");
  await page.getByRole("button", { name: "确认并开始", exact: true }).click();
  const goal = page.getByRole("region", { name: "长任务目标" });
  await expect(goal).toContainText("进行中", { timeout: 30000 });
  await expect
    .poll(
      async () => {
        const response = await page.request.get(
          `/api/web/v1/analysis/sessions/${sessionId}/goal?${query}`,
        );
        expect(response.status(), await response.text()).toBe(200);
        const result = await response.json();
        if (result.goal?.status === "blocked") throw Error(JSON.stringify(result.goal));
        return result.goal?.status;
      },
      { timeout: 100000, intervals: [500, 1000] },
    )
    .toBe("completed");
  await page.reload();
  await expect(goal).toContainText("已完成");
  await expect(page.getByRole("region", { name: "最终回答" }).last()).toContainText("200");
  const process = page
    .getByRole("region", { name: "分析过程", exact: true })
    .last()
    .getByRole("button")
    .first();
  await process.click();
  const child = page.locator("button.ui-timeline-row").filter({ hasText: "销售核对" }).last();
  await child.focus();
  await page.keyboard.press("Enter");
  await expect(child).toHaveAttribute("aria-expanded", "true");
  const region = page.getByRole("dialog", { name: "销售核对", exact: true });
  await expect(region).toContainText("查询数据");
  await expect(region.getByRole("region", { name: "子任务回答" })).not.toContainText(
    "尚未生成回答",
  );
  await expect(region).not.toContainText("库存核对");
  await page.route("**/children/**", (route) =>
    route.fulfill({ status: 503, json: { error: { code: "service_unavailable" } } }),
  );
  await expect(region).toContainText("重试", { timeout: 10000 });
  await expect(region).not.toContainText("查询数据");
  await page.unroute("**/children/**");
  await region.getByRole("button", { name: "重试" }).click();
  await expect(region).toContainText("查询数据");
  const snapshot = await page.request.get(
    `/api/web/v1/analysis/sessions/${sessionId}/messages?${query}`,
  );
  const packets = (await snapshot.json()).packets;
  const children = packets.filter(
    (p: {
      event: {
        type: string;
        payload: { kind?: string; value?: { subagent?: { childRunId?: string } } };
      };
    }) =>
      p.event.type === "view.upsert" &&
      p.event.payload.kind === "execution.node" &&
      p.event.payload.value?.subagent?.childRunId,
  );
  expect(
    new Set(
      children.map(
        (p: { event: { payload: { value: { subagent: { childRunId: string } } } } }) =>
          p.event.payload.value.subagent.childRunId,
      ),
    ).size,
  ).toBe(2);
  await page.screenshot({ path: "../../work/983-goal-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "../../work/983-goal-mobile.png", fullPage: true });
  await page.route("**/children/**", (route) =>
    route.fulfill({ status: 403, json: { error: { code: "forbidden" } } }),
  );
  await page.waitForResponse(
    (response) => response.url().includes("/children/") && response.status() === 403,
  );
  await expect(region).toHaveCount(0, { timeout: 10000 });
  await expect(page.getByRole("region", { name: "子任务回答" })).toHaveCount(0);
});

test("窄屏拒绝目标草案后不启动自动目标", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  const { sessionId } = await propose(page);
  const reject = page
    .getByRole("region", { name: "执行确认" })
    .getByRole("button", { name: /拒绝/ });
  await reject.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("region", { name: "执行确认" })).toHaveCount(0);
  const response = await page.request.get(
    `/api/web/v1/analysis/sessions/${sessionId}/goal?${query}`,
  );
  expect((await response.json()).goal).toBeNull();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("目标草案修改、暂停错误恢复、取消可用键盘操作", async ({ page }) => {
  await login(page);
  const { sessionId } = await propose(page);
  const card = page.getByRole("region", { name: "执行确认" });
  await card.getByRole("button", { name: "修改目标", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(card).toHaveCount(0);
  await expect(page.getByLabel("分析问题")).toBeFocused();
  await expect(page.getByLabel("分析问题")).toHaveValue(/修改目标/);
  expect(
    (
      await (
        await page.request.get(`/api/web/v1/analysis/sessions/${sessionId}/goal?${query}`)
      ).json()
    ).goal,
  ).toBeNull();
  await page.goto(analysis);
  await page.getByLabel("选择 Chat 主模型").selectOption(process.env.COMPASS_GOAL_E2E_PROVIDER_ID!);
  await propose(page);
  await page.getByRole("button", { name: "确认并开始", exact: true }).click();
  const goal = page.getByRole("region", { name: "长任务目标" });
  await expect(goal).toContainText("进行中");
  await page.route(
    "**/goal/actions",
    (route) => route.fulfill({ status: 503, json: { error: { code: "service_unavailable" } } }),
    { times: 1 },
  );
  await goal.locator(".analysis-goal-heading").click();
  await goal.getByRole("button", { name: "暂停目标" }).click();
  await expect(goal).toContainText("操作失败");
  await goal.getByRole("button", { name: "暂停目标" }).focus();
  await page.keyboard.press("Enter");
  await expect(goal).toContainText("已暂停");
  await goal.getByRole("button", { name: "修改目标", exact: true }).click();
  await expect(page.getByLabel("分析问题")).toHaveValue("修改目标：");
  await goal.locator(".analysis-goal-heading").click();
  page.once("dialog", (dialog) => dialog.accept());
  await goal.getByRole("button", { name: "取消目标" }).focus();
  await page.keyboard.press("Enter");
  await expect(goal).toContainText("已取消");
});

test("自动运行中插话会等待停止并提交用户消息", async ({ page }) => {
  const fixture = `http://127.0.0.1:${process.env.COMPASS_E2E_MODEL_PORT ?? 8114}`;
  await page.request.post(`${fixture}/control/auto`, { data: { hold: true } });
  try {
    await login(page);
    const { sessionId } = await propose(page);
    await page.getByRole("button", { name: "确认并开始", exact: true }).click();
    await expect
      .poll(
        async () => (await (await page.request.get(`${fixture}/inspection`)).json()).pendingAuto,
        { timeout: 45000 },
      )
      .toBeGreaterThan(0);
    const message = "插话：继续核对当前范围，先给我已完成的进展。";
    await page.getByLabel("分析问题").fill(message);
    const interrupted = page.waitForResponse(
      (r) => r.url().endsWith("/goal/actions") && r.request().method() === "POST",
    );
    const submitted = page.waitForResponse(
      (r) => r.url().endsWith("/analysis/sessions/submit") && r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "发送消息", exact: true }).click();
    expect((await interrupted).status()).toBe(200);
    await page.request.post(`${fixture}/control/auto`, { data: { hold: false } });
    expect((await submitted).status()).toBe(202);
    await expect(page.getByText(message, { exact: true })).toBeVisible();
    await expect
      .poll(
        async () => {
          const response = await page.request.get(
            `/api/web/v1/analysis/sessions/${sessionId}/goal?${query}`,
          );
          return (await response.json()).goal?.status;
        },
        { timeout: 90000, intervals: [1000] },
      )
      .toBe("completed");
    await expect(page.getByRole("region", { name: "最终回答" }).last()).toContainText("200", {
      timeout: 30000,
    });
  } finally {
    await page.request.post(`${fixture}/control/auto`, { data: { hold: false } });
  }
});

test("真实模型通过普通查询与长任务目标完成经营核对", async ({ page }, info) => {
  test.skip(
    process.env.COMPASS_GOAL_REAL_E2E !== "true",
    "Requires explicitly enabled real-provider acceptance.",
  );
  test.setTimeout(420000);
  await login(page);
  await page
    .getByLabel("选择 Chat 主模型")
    .selectOption({ label: "983 真实模型 DeepSeek V4.1 Flash" });
  await page
    .getByLabel("分析问题")
    .fill(
      "请用当前已授权的只读数据库查询 core.channel_product_daily 的 amount 总额。这是一条简单查询，直接回答金额即可。",
    );
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.getByRole("region", { name: "最终回答" })).toContainText("200", {
    timeout: 120000,
  });
  await expect(page.getByRole("region", { name: "长任务目标" })).toHaveCount(0);
  await page.goto(analysis);
  await page
    .getByLabel("选择 Chat 主模型")
    .selectOption({ label: "983 真实模型 DeepSeek V4.1 Flash" });
  const submitted = page.waitForResponse(
    (response) =>
      response.url().endsWith("/analysis/sessions/submit") &&
      response.request().method() === "POST",
  );
  await page
    .getByLabel("分析问题")
    .fill(
      "请持续完成当前数据的销售与库存复盘，分阶段做完。先确认一个长任务目标，验收项为：core.channel_product_daily 的 amount 总额有数据来源、core.sku 的 amount 总量有数据来源。约束：只使用当前获准连接的只读数据。先销售再库存，结论记入账本，完整后申请完成目标。自动轮数不超过 3 轮。",
    );
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  const { sessionId } = await (await submitted).json();
  await expect(page.getByRole("region", { name: "执行确认" })).toContainText("确认长任务目标", {
    timeout: 120000,
  });
  await page.getByRole("button", { name: "确认并开始", exact: true }).click();
  let result: unknown;
  await expect
    .poll(
      async () => {
        const response = await page.request.get(
          `/api/web/v1/analysis/sessions/${sessionId}/goal?${query}`,
        );
        const value = await response.json();
        result = value;
        if (["blocked", "budget_limited"].includes(value.goal?.status))
          throw Error(JSON.stringify(value));
        return value.goal?.status;
      },
      { timeout: 240000, intervals: [2000] },
    )
    .toBe("completed");
  await info.attach("real-goal-result", {
    body: JSON.stringify(result, null, 2),
    contentType: "application/json",
  });
  await page.reload();
  await expect(page.getByRole("region", { name: "长任务目标" })).toContainText("已完成");
  await expect(page.getByRole("region", { name: "最终回答" }).last()).toContainText("200", {
    timeout: 90000,
  });
  await expect(page.getByRole("region", { name: "最终回答" }).last()).toContainText("50");
  await expect(page.getByText("这份结果的来源当前不可读取。", { exact: false })).toHaveCount(0);
  await page.screenshot({ path: "../../work/983-real-goal.png", fullPage: true });
});

test("历史子任务抽屉支持完整回答、重试和权限失效", async ({ page }) => {
  const sessionId = process.env.COMPASS_CHILD_TRACE_SESSION_ID;
  test.skip(!sessionId, "Requires a completed child trace in the isolated runtime.");
  await login(page);
  await page.goto(`${analysis}?session=${sessionId}`);
  const timeline = page.getByRole("region", { name: "分析过程", exact: true }).last();
  await timeline.getByRole("button").first().click();
  const child = page.locator("button.ui-child-trigger").filter({ hasText: "销售核对" }).last();
  await child.press("Enter");
  const drawer = page.getByRole("dialog", { name: "销售核对", exact: true });
  await expect(drawer).toContainText("查询数据");
  await expect(drawer).not.toContainText("库存核对");
  await expect(drawer.getByRole("region", { name: "子任务回答" })).toContainText("销售");
  await page.route("**/children/**", (route) =>
    route.fulfill({ status: 503, json: { error: { code: "service_unavailable" } } }),
  );
  await expect(drawer).toContainText("重试", { timeout: 10000 });
  await expect(drawer.getByRole("region", { name: "子任务回答" })).toHaveCount(0);
  await page.unroute("**/children/**");
  await drawer.getByRole("button", { name: "重试" }).click();
  await expect(drawer).toContainText("查询数据");
  await page.route("**/children/**", (route) =>
    route.fulfill({ status: 404, json: { error: { code: "not_found" } } }),
  );
  await expect(drawer).toContainText("此执行过程不可访问", { timeout: 10000 });
  await expect(drawer.getByRole("region", { name: "子任务回答" })).toHaveCount(0);
  await page.unroute("**/children/**");
  await drawer.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(child).toBeFocused();
  await child.press("Enter");
  await expect(drawer).toContainText("查询数据");
  await page.route("**/children/**", (route) =>
    route.fulfill({ status: 403, json: { error: { code: "forbidden" } } }),
  );
  await expect(drawer).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByRole("region", { name: "子任务回答" })).toHaveCount(0);
});
