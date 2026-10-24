const { test, expect } = require("@playwright/test");

test("returning to a tab keeps the dashboard mounted while its catalog refreshes", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("toposync.locale", "en");
    localStorage.setItem("toposync.render_mode.v1", "streams");
  });
  await page.route("**/api/extensions", route => route.fulfill({ json: [
    { id: "external.streaming", api_prefixes: ["/api/streams"] },
  ] }));
  let hold = false, requested = false, release;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route("**/api/streams/live-views", async route => {
    if (hold) { requested = true; await pending; }
    await route.fulfill({ json: [] });
  });
  await page.goto("/");
  const empty = page.getByText("No live streams enabled. Create one in Settings > Streams.");
  await expect(empty).toBeVisible();
  // Synthetic events isolate refresh behavior; physical hiding is covered by installed-browser validation.
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  hold = true;
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  try {
    await expect.poll(() => requested).toBe(true);
    await expect(empty).toBeVisible();
  } finally { release(); }
  await expect(empty).toBeVisible();
});

test("missing streaming waits for the catalog and never polls or mounts a player", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("toposync.locale", "en");
    localStorage.setItem("toposync.render_mode.v1", "streams");
  });
  let release;
  const catalog = new Promise(resolve => { release = resolve; });
  let streamRequests = 0;
  await page.route("**/api/extensions", async route => { await catalog; await route.fulfill({ json: [] }); });
  await page.route("**/api/streams/**", route => { streamRequests++; return route.fulfill({ json: [] }); });
  await page.clock.install();
  await page.goto("/");
  await expect(page.getByRole("status").filter({ hasText: "Loading…" })).toBeVisible();
  expect(streamRequests).toBe(0);
  release();
  await expect(page.locator(".streamsRoot").getByRole("status")).toHaveText("Streaming extension unavailable.");
  await page.clock.runFor(16000);
  expect(streamRequests).toBe(0);
  await expect(page.getByRole("button", { name: "View: Camera Grid", exact: true })).toBeVisible();
  await expect(page.locator("video")).toHaveCount(0);
  await page.goto("/streams/debug");
  await expect(page.locator(".streamsRoot").getByRole("status")).toHaveText("Streaming extension unavailable.");
  expect(streamRequests).toBe(0);
});

for (const catalogStatus of [200, 403]) {
  test(`streaming keeps its own authorization when catalog responds ${catalogStatus}`, async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem("toposync.locale", "en");
      localStorage.setItem("toposync.render_mode.v1", "streams");
    });
    await page.route("**/api/extensions", route => route.fulfill({ status: catalogStatus,
      json: catalogStatus === 200 ? [{ id: "external.streaming", api_prefixes: ["/api/streams"] }] : { detail: "catalog forbidden" } }));
    let streamStatus = 200;
    let requests = 0;
    await page.route("**/api/streams/live-views", route => {
      requests++;
      return route.fulfill({ status: streamStatus, json: streamStatus === 200 ? [] : { detail: "stream access revoked" } });
    });
    await page.clock.install();
    await page.goto("/");
    await expect.poll(() => requests).toBeGreaterThan(0);
    await expect(page.getByText("No live streams enabled. Create one in Settings > Streams.")).toBeVisible();
    streamStatus = 403;
    await page.clock.runFor(16000);
    await expect(page.getByText(/stream access revoked/)).toBeVisible();
  });
}
