const { test: base, expect } = require("@playwright/test");
const fs = require("node:fs");
const prefix = "/fixture-ingress";

const test = base.extend({
  visual: async ({ page, request, baseURL }, use) => {
    const outbound = [];
    await page.route("**/*", route => {
      const url = new URL(route.request().url());
      if (url.origin === baseURL || ["data:", "blob:"].includes(url.protocol)) return route.continue();
      outbound.push(url.origin);
      return route.abort("blockedbyclient");
    });
    const response = await request.post(`${prefix}/api/pipelines/visual_fixture/duplicate`);
    expect(response.status()).toBe(201);
    const pipeline = await response.json();
    expect(pipeline.enabled).toBe(false);
    await page.goto(`${prefix}/settings/pipelines/${pipeline.name}`);
    const node = page.locator('.react-flow__node[data-id="sensor"]');
    await expect(node).toBeAttached();
    await node.focus();
    await page.keyboard.press("Enter");
    const panel = page.locator(".visualStatePanel");
    await expect(panel).toBeVisible();
    await use({ panel, pipeline, prefix, request });
    expect(outbound).toEqual([]);
    const stats = await (await request.get("/fixture/stats")).json();
    expect(stats.synthetic).toBe(true);
    expect(stats.outbound_attempts).toBe(0);
    const saved = await (await request.get(`${prefix}/api/pipelines/${pipeline.name}`)).json();
    expect(saved.enabled).toBe(false);
  },
});

async function captureFirst(panel) {
  const capture = panel.getByRole("button", { name: "Capturar", exact: true }).first();
  await capture.focus();
  await capture.press("Enter");
  await expect(panel.locator(".visualStatePhoto")).toHaveCount(1);
  await expect(capture).toBeFocused();
  return capture;
}

async function saveVisualPipeline(page, visual) {
  const route = `${visual.prefix}/api/pipelines/${visual.pipeline.name}`;
  const saving = page.waitForResponse(response => response.request().method() === "PUT"
    && new URL(response.url()).pathname === route);
  await page.getByRole("button", { name: "Salvar", exact: true }).click();
  expect((await saving).ok()).toBe(true);
  const response = await visual.request.get(route);
  expect(response.ok()).toBe(true);
  const saved = await response.json();
  expect(saved.enabled).toBe(false);
  return saved;
}

const nativeZoomTest = test.extend({
  context: async ({ playwright }, use, testInfo) => {
    const extension = testInfo.outputPath("native-zoom-extension");
    fs.mkdirSync(extension, { recursive: true });
    fs.writeFileSync(`${extension}/manifest.json`, JSON.stringify({
      manifest_version: 3, name: "Isolated native zoom test", version: "1.0",
      permissions: ["tabs"], background: { service_worker: "background.js" },
    }));
    fs.writeFileSync(`${extension}/background.js`, "chrome.runtime.onInstalled.addListener(() => {});\n");
    const context = await playwright.chromium.launchPersistentContext(testInfo.outputPath("native-zoom-profile"), {
      channel: "chromium", headless: true, viewport: { width: 1440, height: 1000 }, locale: "pt-BR",
      args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`],
    });
    try { await use(context); } finally { await context.close(); }
  },
  setNativeZoom: async ({ context }, use) => {
    const worker = context.serviceWorkers()[0] || await context.waitForEvent("serviceworker");
    await use(async (page, factor) => {
      const actual = await worker.evaluate(async ({ url, factor }) => {
        const tabs = (await chrome.tabs.query({})).filter(tab => tab.url === url);
        if (tabs.length !== 1) throw new Error("Expected one matching tab in the isolated test profile");
        await chrome.tabs.setZoomSettings(tabs[0].id, { mode: "automatic", scope: "per-tab" });
        await chrome.tabs.setZoom(tabs[0].id, factor);
        return chrome.tabs.getZoom(tabs[0].id);
      }, { url: page.url(), factor });
      expect(actual).toBe(factor);
    });
  },
});

module.exports = { test, nativeZoomTest, expect, prefix, captureFirst, saveVisualPipeline };
