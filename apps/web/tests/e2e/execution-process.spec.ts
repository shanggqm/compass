import { expect, test } from "@playwright/test";

const origin = `http://127.0.0.1:${process.env.WEB_TEST_PORT ?? 8110}`;

test("子任务通过抽屉复用执行过程与回答，支持窄屏和键盘返回", async ({ page, context }) => {
  const login = await context.request.post("/api/web/v1/auth/login", {
    headers: { origin },
    data: {
      loginName: "teaching.admin",
      password: process.env.WEB_TEST_PASSWORD ?? "teaching-995-only-password",
    },
  });
  expect(login.status()).toBe(200);
  await page.goto("/guide#ui-execution");
  const guide = page.locator("#ui-execution");
  const toggle = guide.locator(".analysis-process-toggle");
  const list = guide.getByRole("list", { name: "执行过程" });
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(toggle).toContainText("独立口径复核");
  await toggle.press("Enter");
  await expect(list.locator(":scope > li")).toHaveCount(6);
  await expect(list.getByRole("button", { name: /查询商品明细/ })).toHaveCount(0);
  const child = list.getByRole("button", { name: /商品贡献分析/ });
  await expect(child).toHaveAttribute("aria-haspopup", "dialog");
  expect(await child.evaluate((el) => el.getBoundingClientRect().height)).toBeGreaterThanOrEqual(
    44,
  );
  await child.press("Space");
  const drawer = page.getByRole("dialog", { name: "商品贡献分析", exact: true });
  await expect(drawer).toBeVisible();
  await expect(drawer).toHaveAttribute("data-placement", "right");
  await expect(drawer.getByRole("list", { name: "执行过程" })).toBeVisible();
  await expect(drawer).not.toContainText("独立口径复核");
  await expect(drawer.getByRole("region", { name: "子任务回答" })).toContainText("+1,700");
  await expect(drawer.getByRole("table")).toBeVisible();
  const tool = drawer.getByRole("button", { name: /查询商品明细/ });
  await tool.press("Enter");
  await expect(drawer.getByText("MCP · Retail 数据服务")).toBeVisible();
  await page.screenshot({ path: "../../work/child-drawer-desktop.png", fullPage: false });

  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(await drawer.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
  const result = drawer.getByRole("region", { name: "调用结果", exact: true });
  await result.getByRole("button", { name: "自动换行" }).click();
  expect(await result.locator("pre").evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await result.getByRole("button", { name: "复制代码" }).click();
  await expect(result.getByRole("button", { name: "复制代码" })).toContainText("已复制");
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain('"rowCount":3');
  await page.screenshot({ path: "../../work/child-drawer-mobile.png", fullPage: false });
  await page.keyboard.press("Escape");
  await expect(drawer).toHaveCount(0);
  await expect(child).toBeFocused();

  await guide.getByRole("button", { name: "失败", exact: true }).click();
  const review = list.getByRole("button", { name: /独立口径复核/ });
  await expect(review).toContainText("失败");
  await review.press("Enter");
  const reviewDrawer = page.getByRole("dialog", { name: "独立口径复核", exact: true });
  await expect(reviewDrawer).toContainText("此子任务尚未生成回答");
  await reviewDrawer.getByRole("button", { name: /执行 Python 复核/ }).press("Enter");
  await expect(reviewDrawer).toContainText("Sandbox · Python");
  await reviewDrawer.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(review).toBeFocused();

  await guide.getByRole("button", { name: "成功", exact: true }).click();
  await expect(toggle).toHaveText("已完成，用时 2 分 18 秒");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.press("Enter");
  await review.press("Enter");
  await expect(reviewDrawer.getByRole("region", { name: "子任务回答" })).toContainText("128");
  const query = reviewDrawer.getByRole("button", { name: /查询数据 · 执行 SELECT/ });
  await expect(query).toContainText("返回 1 行 · orders=128");
  await query.press("Enter");
  await expect(query.locator("..").getByRole("region", { name: "调用入参" })).toContainText(
    "FROM sales",
  );
  await query.press("Space");
  await expect(query).toHaveAttribute("aria-expanded", "false");
  await page.keyboard.press("Escape");
  await expect(review).toBeFocused();
});
