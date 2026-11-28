import { expect, test, type Page } from "@playwright/test";
import type { FileView } from "../../src/features/promptops/client";

const origin = "http://127.0.0.1:" + (process.env.WEB_TEST_PORT ?? 8110);

// UI fixtures keep this suite independent of the Agent's published prompt library.
async function prepare(page: Page) {
  const login = await page.request.post("/api/web/v1/auth/login", {
    headers: { origin },
    data: { loginName: "teaching.platform", password: process.env.WEB_TEST_PASSWORD },
  });
  expect(login.status()).toBe(200);
  const session = await page.request.get("/api/web/v1/session?pageId=platform-promptops");
  const { viewRevision } = await session.json();
  const files = new Map<string, FileView>();
  function add(path: string, body: string, draft = false) {
    files.set(path, {
      path,
      governance: null,
      body: draft ? null : body,
      draft_body: draft ? body : null,
      current_version: draft ? null : 1,
      version: draft ? null : 1,
      active_epoch: draft ? 0 : 1,
      draft_base_epoch: draft ? 0 : null,
      draft_base_version: null,
      edit_rev: 1,
      has_draft: draft,
      kind: path.endsWith(".json") ? "json" : "markdown",
      origin: "online",
      used: false,
      retired: false,
      contract_sha256: null,
      usages: [],
      variables: [],
    });
  }
  add("shared/fragments/short.md", "# 简短提示词\n\n正文。");
  add(
    "shared/fragments/long.md",
    Array.from({ length: 80 }, (_, i) => `第 ${i + 1} 段正文。`).join("\n\n"),
  );
  add(
    "shared/config/long.json",
    JSON.stringify({ rows: Array.from({ length: 100 }, (_, i) => `row ${i}`) }, null, 2),
  );
  add("shared/config/short.json", '{"message": "简短内容"}');
  for (let i = 0; i < 35; i++) add(`tasks/group-${i}/system.md`, "# 系统提示词");
  const writes: { path: string; body: string }[] = [];
  let resumeCreate: (() => void) | undefined;
  await page.route("**/api/web/v1/platform/promptops/**", async (route) => {
    const url = new URL(route.request().url());
    const kind = url.pathname.split("/").at(-1);
    let status = 200;
    let response: unknown;
    if (kind === "tree") {
      const q = url.searchParams.get("q") ?? "";
      response = {
        items: [...files.values()].filter((f) => f.path.includes(q)),
        next_cursor: null,
        generation: 1,
        snapshot_no: 1,
      };
    } else if (kind === "file") {
      response = files.get(url.searchParams.get("path") ?? "");
    } else if (kind === "file-draft") {
      const input = route.request().postDataJSON();
      writes.push(input);
      if (writes.length === 1) {
        await new Promise<void>((resolve) => {
          resumeCreate = resolve;
        });
        status = 422;
        response = { error: { code: "invalid_template", message: "正文校验失败，请修改后重试。" } };
      } else {
        add(input.path, input.body, true);
        response = { request_id: input.request_id, result: {} };
      }
    } else {
      throw new Error(`Unexpected prompt operation: ${kind}`);
    }
    await route.fulfill({
      status,
      headers: { "X-Compass-View-Revision": viewRevision },
      json: response,
    });
  });
  await page.goto("/platform/promptops");
  await expect(page.getByTitle("shared/fragments/short.md", { exact: true })).toBeVisible();
  return { writes, finishCreate: () => resumeCreate?.() };
}

test("expanded directories create in a fixed folder and keep full-height independent scrolling", async ({
  page,
}, info) => {
  const { writes, finishCreate } = await prepare(page);
  const folder = page.getByTitle("shared/fragments/", { exact: true });
  const plus = page.getByRole("button", { name: "在 shared/fragments/ 中新建提示词", exact: true });
  await expect(folder).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByTitle("tasks/group-34/", { exact: true })).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  await expect(plus).toHaveCSS("opacity", "0");
  await folder.hover();
  await expect(plus).toHaveCSS("opacity", "1");
  await expect(plus).toHaveCSS("border-top-width", "0px");
  await expect(folder).toHaveCSS("border-top-width", "0px");
  await folder.click();
  await expect(folder).toHaveAttribute("aria-expanded", "false");
  await plus.click();
  const dialog = page.getByRole("dialog", { name: "新建提示词" });
  await expect(dialog.getByLabel("相对路径")).toHaveValue("shared/fragments/");
  await expect(dialog.getByLabel("相对路径")).toHaveAttribute("readonly", "");
  await expect(dialog.getByLabel("文件名", { exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(plus).toBeFocused();
  await plus.press("Enter");
  await dialog.getByLabel("文件名", { exact: true }).fill("../escape.md");
  await dialog.getByRole("button", { name: "创建草稿" }).click();
  await expect(dialog.getByRole("alert")).toContainText("目录由所选位置确定");
  expect(writes).toHaveLength(0);
  await dialog.getByLabel("文件名", { exact: true }).fill("new.md");
  await dialog.getByLabel("Markdown 正文").fill("# 保留正文");
  await dialog.getByRole("button", { name: "创建草稿" }).click();
  await expect(dialog.getByRole("button", { name: "正在创建…" })).toBeDisabled();
  await expect(dialog.getByLabel("文件名", { exact: true })).toBeDisabled();
  finishCreate();
  await expect(dialog.getByRole("alert")).toContainText("正文校验失败");
  await expect(dialog.getByLabel("Markdown 正文")).toHaveValue("# 保留正文");
  await dialog.getByRole("button", { name: "创建草稿" }).click();
  await expect(dialog).toHaveCount(0);
  expect(writes.at(-1)).toMatchObject({ path: "shared/fragments/new.md", body: "# 保留正文" });
  await expect(page.getByLabel("文件编辑器")).toContainText("new.md");
  await folder.click();
  await page.getByTitle("shared/fragments/short.md", { exact: true }).click();
  const boxes = await page.locator(".prompt-file-actions button").evaluateAll((buttons) =>
    buttons.map((button) => ({
      y: button.getBoundingClientRect().y,
      icons: button.querySelectorAll("svg").length,
    })),
  );
  expect(new Set(boxes.map((b) => b.y)).size).toBe(1);
  expect(boxes.every((b) => b.icons === 1)).toBe(true);
  const bounds = await page.locator(".prompt-tree").boundingBox();
  expect(bounds!.y).toBe(70);
  expect(bounds!.y + bounds!.height).toBe(page.viewportSize()!.height);
  const independentScroll = await page.locator(".prompt-tree-scroll").evaluate((el) => {
    el.scrollTop = 500;
    return el.scrollTop;
  });
  expect(independentScroll).toBeGreaterThan(0);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  await page.locator(".prompt-tree-scroll").evaluate((el) => {
    el.scrollTop = 0;
  });
  await page.screenshot({ path: info.outputPath("promptops-desktop.png") });
});

test("Markdown and JSON previews grow to 800px and remain accessible on narrow screens", async ({
  page,
}, info) => {
  await prepare(page);
  for (const [path, label, preview] of [
    ["shared/fragments/short.md", "Markdown 预览", ".prompt-preview"],
    ["shared/config/short.json", "JSON 预览", ".prompt-json-preview .ui-code-scroll"],
    ["shared/fragments/long.md", "Markdown 预览", ".prompt-preview"],
    ["shared/config/long.json", "JSON 预览", ".prompt-json-preview .ui-code-scroll"],
  ]) {
    await page.getByTitle(path, { exact: true }).click();
    await page.getByRole("button", { name: label, exact: true }).click();
    const size = await page
      .locator(preview)
      .evaluate((el) => ({ height: el.clientHeight, scroll: el.scrollHeight }));
    if (path.includes("short")) {
      expect(size.height).toBeLessThan(200);
      expect(size.scroll).toBe(size.height);
    } else {
      expect(size.height).toBe(800);
      expect(size.scroll).toBeGreaterThan(800);
    }
  }
  await page.screenshot({ path: info.outputPath("promptops-json-preview.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "查看文件树" }).click();
  await page.getByTitle("shared/fragments/short.md", { exact: true }).click();
  await expect(page.getByLabel("文件编辑器")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  await page.getByRole("button", { name: "Markdown 预览", exact: true }).click();
  await expect(page.getByRole("heading", { name: "简短提示词" })).toBeVisible();
  await page.getByRole("button", { name: "查看文件树" }).click();
  const root = page.getByRole("button", { name: "在根目录新建提示词" });
  await root.focus();
  await root.press("Enter");
  await expect(page.getByLabel("相对路径")).toHaveValue("./");
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await expect(root).toBeFocused();
  await page.screenshot({ path: info.outputPath("promptops-mobile.png") });
});
