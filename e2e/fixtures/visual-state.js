const { test: base, expect } = require("@playwright/test");
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

module.exports = { test, expect, prefix, captureFirst };
