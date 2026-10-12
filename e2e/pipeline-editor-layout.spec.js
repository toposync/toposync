const { test, expect } = require("@playwright/test");
const { installPipelineEditorFixture, openPipelineEditor } = require("./fixtures/pipeline-editor");

let fixture;
test.beforeEach(async ({ page }) => {
  fixture = await installPipelineEditorFixture(page);
  await page.addInitScript(() => localStorage.setItem("toposync.locale", "en"));
});
test.afterEach(() => expect(fixture.unexpected).toEqual([]));

for (const theme of ["topo-day", "topo-night"]) {
  for (const width of [390, 768, 1440]) {
    test(`${theme} at ${width}px keeps sections separated and storage readable`, async ({ page }, info) => {
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      await page.addInitScript(theme => localStorage.setItem("toposync.theme", theme), theme);
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1024 });
      await openPipelineEditor(page);
      await expect(page.locator(".pipelinesStorageLayerRow")).toHaveCount(2);
      await expect(page.locator(".pipelinesConnectionControls .pipelinesAlertRow")).toBeVisible();
      await expect(page.getByRole("button", { name: "Validate", exact: true })).toBeEnabled();
      await expect(page.getByText("Loading telemetry…", { exact: true })).toBeHidden();
      const geometry = await page.evaluate(() => {
        const boxes = [...document.querySelector(".pipelinesAdvancedContent").children].map(e => e.getBoundingClientRect());
        const storage = document.querySelector(".pipelinesStorageCard");
        const layer = document.querySelector(".pipelinesStorageLayerRow");
        return {
          gaps: boxes.slice(1).map((box, i) => box.top - boxes[i].bottom),
          overflow: document.querySelector(".pipelinesEditor").scrollWidth - document.querySelector(".pipelinesEditor").clientWidth,
          storageBackground: getComputedStyle(storage).backgroundColor,
          cardBackground: getComputedStyle(document.querySelector(".pipelinesAdvancedContent > .card")).backgroundColor,
          layerBackground: getComputedStyle(layer).backgroundColor,
          padding: parseFloat(getComputedStyle(storage).paddingLeft),
        };
      });
      expect(geometry.gaps.every(gap => gap >= (width <= 960 ? 16 : 24))).toBe(true);
      expect(geometry.overflow).toBeLessThanOrEqual(1);
      expect(geometry.storageBackground).toBe(geometry.cardBackground);
      expect(geometry.layerBackground).toBe("rgba(0, 0, 0, 0)");
      expect(geometry.padding).toBeGreaterThanOrEqual(16);
      await page.screenshot({ path: info.outputPath("header.png") });
      await page.locator(".pipelinesStorageCard").scrollIntoViewIfNeeded();
      await expect(page.locator("#pipeline-storage-limit-gib")).toBeVisible();
      await page.screenshot({ path: info.outputPath("storage.png") });
      const screenshot = await page.screenshot();
      const contrast = await page.evaluate(async base64 => {
        const image = new Image();
        image.src = `data:image/png;base64,${base64}`;
        await image.decode();
        const canvas = document.createElement("canvas");
        canvas.width = image.width;
        canvas.height = image.height;
        const context = canvas.getContext("2d");
        context.drawImage(image, 0, 0);
        const luminance = rgb => rgb.map(value => value / 255).map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
          .reduce((sum, value, i) => sum + value * [0.2126, 0.7152, 0.0722][i], 0);
        return [".pipelinesStorageTitle", ".pipelinesStorageCard .pipelinesStepHint", ".pipelinesStorageLayerName", ".pipelinesStorageLayerMeta", ".pipelinesStorageLimitField > span"].map(selector => {
          const element = document.querySelector(selector);
          const surface = element.closest(".pipelinesStorageLayerRow") || element.closest(".pipelinesStorageCard");
          const box = surface.getBoundingClientRect();
          const y = Math.max(1, Math.min(innerHeight - 1, box.top + 20));
          const background = [...context.getImageData(Math.round((box.left + 8) * devicePixelRatio), Math.round(y * devicePixelRatio), 1, 1).data].slice(0, 3);
          const color = getComputedStyle(element).color.match(/[\d.]+/g).map(Number);
          const foreground = color.slice(0, 3).map((value, i) => value * (color[3] ?? 1) + background[i] * (1 - (color[3] ?? 1)));
          const [light, dark] = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
          return { selector, ratio: (light + 0.05) / (dark + 0.05) };
        });
      }, screenshot.toString("base64"));
      for (const pair of contrast) expect(pair.ratio, pair.selector).toBeGreaterThanOrEqual(4.5);
      await info.attach("layout-and-contrast", { body: JSON.stringify({ geometry, contrast }, null, 2), contentType: "application/json" });
      expect(errors).toEqual([]);
    });
  }
}

test("keyboard disclosure keeps activation available and hidden content out of Tab order", async ({ page }) => {
  await openPipelineEditor(page);
  const summary = page.locator(".pipelinesAdvancedEditor > summary");
  await summary.focus();
  await page.keyboard.press("Space");
  await expect(page.locator(".pipelinesAdvancedContent")).toBeHidden();
  await expect(page.locator("#pipeline-enabled")).toBeVisible();
  await page.keyboard.press("Tab");
  expect(await page.locator(".pipelinesAdvancedContent").evaluate(e => e.contains(document.activeElement))).toBe(false);
  await summary.focus();
  await page.keyboard.press("Space");
  await expect(page.locator("#pipeline-processing-server")).toBeVisible();
  await page.locator("#pipeline-enabled").focus();
  await expect(page.locator("#pipeline-enabled")).toHaveCSS("outline-style", "solid");
  await page.keyboard.press("Space");
  await expect(page.locator("#pipeline-enabled")).toBeChecked();
  expect(fixture.saves).toHaveLength(0);
});

test("Portuguese at 200 percent zoom keeps actions and long storage labels inside the editor", async ({ page }, info) => {
  await page.addInitScript(() => {
    localStorage.setItem("toposync.locale", "pt-BR");
    localStorage.setItem("toposync.theme", "topo-day");
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openPipelineEditor(page);
  await page.evaluate(() => { document.documentElement.style.zoom = "2"; });
  await expect(page.getByRole("button", { name: "Salvar", exact: true })).toBeVisible();
  await page.locator(".pipelinesStorageCard").scrollIntoViewIfNeeded();
  await expect(page.getByRole("button", { name: "Limpar agora", exact: true })).toBeVisible();
  expect(await page.locator(".pipelinesEditor").evaluate(e => e.scrollWidth - e.clientWidth)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: info.outputPath("portuguese-zoom-200.png") });
});

test("storage loading and empty states do not remove the budget or actions", async ({ page }) => {
  let release;
  fixture.storageWait = new Promise(resolve => { release = resolve; });
  fixture.pipeline.graph.nodes = [];
  fixture.pipeline.graph.edges = [];
  fixture.storage.layers = [];
  await page.goto("/settings/pipelines/layout_fixture");
  const storage = page.locator(".pipelinesStorageCard");
  await expect(storage.getByText("Loading…", { exact: true })).toBeVisible();
  await expect(storage.locator("#pipeline-storage-limit-gib")).toBeVisible();
  release();
  await expect(storage.getByText("Loading…", { exact: true })).toBeHidden();
  await expect(storage).toContainText("No stored files yet.");
  await expect(storage.getByRole("button", { name: "Clean now", exact: true })).toBeEnabled();
});

for (const theme of ["topo-day", "topo-night"]) {
  test(`${theme} preserves the shared storage metrics inside the node inspector`, async ({ page }, info) => {
    await page.addInitScript(theme => localStorage.setItem("toposync.theme", theme), theme);
    await openPipelineEditor(page);
    await page.locator('.react-flow__node[data-id="store"]').click();
    const inline = page.locator(".pipelinesStorageInlinePanel");
    await expect(inline).toBeVisible();
    await expect(inline).toContainText("1.00 MB");
    await inline.scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath("inspector.png") });
  });
}

test("activation and storage budget remain draft edits until save and survive reload", async ({ page }) => {
  await openPipelineEditor(page);
  const originalNodes = structuredClone(fixture.pipeline.graph.nodes);
  await page.locator("#pipeline-enabled").check();
  await page.locator("#pipeline-storage-limit-gib").fill("6.25");
  await page.locator("#pipeline-storage-limit-gib").press("Tab");
  expect(fixture.saves).toHaveLength(0);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => fixture.saves.length).toBe(1);
  expect(fixture.saves[0].enabled).toBe(true);
  expect(fixture.saves[0].graph.limits.storage_max_bytes).toBe(6.25 * 1024 ** 3);
  expect(fixture.saves[0].graph.nodes.map(n => n.id)).toEqual(originalNodes.map(n => n.id));
  await page.reload();
  await expect(page.locator("#pipeline-enabled")).toBeChecked();
  await expect(page.locator("#pipeline-storage-limit-gib")).toHaveValue("6.25");
  fixture.saveError = true;
  await page.locator("#pipeline-enabled").uncheck();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Save rejected");
  await expect(page.locator("#pipeline-enabled")).not.toBeChecked();
  expect(fixture.pipeline.enabled).toBe(true);
});

test("generated pipelines retain read-only activation, budget and cleanup", async ({ page }) => {
  await openPipelineEditor(page, "implicit__fixture");
  for (const selector of ["#pipeline-enabled", "#pipeline-storage-limit-gib"]) await expect(page.locator(selector)).toBeDisabled();
  await expect(page.getByRole("button", { name: "Save", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Clean now", exact: true })).toBeDisabled();
});

test("storage handles warnings, errors, empty data and confirmed cleanup without real files", async ({ page }) => {
  fixture.storage.over_limit = true;
  fixture.storage.free_bytes = 0;
  await openPipelineEditor(page);
  const storage = page.locator(".pipelinesStorageCard");
  await expect(storage.locator(".isWarn.pipelinesStorageNotice")).toBeVisible();
  await expect(storage.locator(".isDanger.pipelinesStorageNotice")).toBeVisible();
  const clean = storage.getByRole("button", { name: "Clean now", exact: true });
  page.once("dialog", dialog => dialog.dismiss());
  await clean.click();
  expect(fixture.cleanups).toHaveLength(0);
  fixture.cleanupError = true;
  page.once("dialog", dialog => dialog.accept());
  await clean.click();
  await expect(storage).toContainText("Cleanup rejected");
  expect(fixture.cleanups).toEqual(["true"]);
  await expect(storage.locator(".pipelinesStorageLayerRow")).toHaveCount(2);
  fixture.cleanupError = false;
  page.once("dialog", dialog => dialog.accept());
  await clean.click();
  await expect(storage.locator(".pipelinesStorageUsageText")).toContainText("0 B");
  fixture.storageError = true;
  await storage.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(storage).toContainText("Storage unavailable");
});
