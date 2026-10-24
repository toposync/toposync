const { test, expect } = require("@playwright/test");
const fs = require("node:fs");
const path = require("node:path");

const identifier = "a".repeat(64);
const fallbackImage = Buffer.from("UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEAAUAmJaQAA3AA/v89WAAAAA==", "base64");
const sources = process.env.TOPOSYNC_CAPTURE_FIXTURES ? JSON.parse(fs.readFileSync(process.env.TOPOSYNC_CAPTURE_FIXTURES, "utf8")) : null;

async function fixture(page, rate, group) {
  const writes = [];
  const expiry = Date.now() / 1000 + 3600;
  const record = { id: "capture-notice", type: "occurrences.activity", title: "Saved event images", priority: "high", updatedAt: new Date().toISOString(), payload: { occurrence_id: identifier } };
  const images = Array.from({ length: rate * 4 }, (_, index) => ({
    id: String(index), sequence: index + 1, evidence_id: String(index), participant_id: "first",
    camera_id: "offline", source_id: "saved", clock_domain: "fixture", observed_at: index / rate,
    capture_instance: "fixture", capture_generation: 1, frame_sequence: index,
    position: [index / rate, 1], coordinate_reference: "spatial-fixture", mapping_revision: "synthetic-position",
    gap: false, kind: "rectangle", width: 160, height: 240, anchor: [0.5, 1], rate,
    expires_at: expiry, available: true, size_bytes: 100, image_href: `images/${index}`,
    members: group ? ["first", "second"] : [],
  }));
  if (group) images.push(...images.map(item => ({ ...item, id: `${item.id}-second`, participant_id: "second", position: [item.position[0], 2], sequence: item.sequence + rate * 4, image_href: `images/${item.id}-second` })));
  images.sort((a, b) => a.observed_at - b.observed_at || a.id.localeCompare(b.id));
  const document = { id: identifier, title: record.title, revision: 1, status: "CLOSE", started_at: 0,
    ended_at: 4, last_observed_at: 4, duration_seconds: 4, duration_status: "observed", clock_domain: "fixture",
    expires_at: expiry, enrichment_allowed: true, participants: Object.fromEntries((group ? ["first", "second"] : ["first"]).map(id => [id, { id, category: "person", identification: { status: "unknown" } }])),
    evidence: {}, artifacts: [], coverage: { gaps: false, continuous_video: false }, enrichments: [],
    retention: Object.fromEntries(["event_text", "images", "private_identity", "diagnostics", "video"].map(kind => [kind, { state: kind === "images" ? "retained" : "not_retained", sample_maximum_age_seconds: 1, session_lease_seconds: 1 }])),
  };
  await page.addInitScript(() => {
    localStorage.setItem("toposync.locale", "en"); localStorage.setItem("toposync.render_mode.v1", "2d");
    window.EventSource = class { constructor() { this.readyState = 1; } addEventListener() {} removeEventListener() {} close() {} };
  });
  await page.route("**/api/**", route => {
    if (!["GET", "HEAD"].includes(route.request().method())) { writes.push(route.request().url()); return route.abort(); }
    return route.continue();
  });
  await page.route("**/api/extensions", route => route.fulfill({ json: [] }));
  await page.route("**/api/files/exists?**", route => route.fulfill({ json: { exists: true } }));
  await page.route(/\/files\/render2d\/.*_manifest.json$/, route => route.fulfill({ json: { version: 9, compositionId: "spatial-fixture", signature: "fixture", widthPx: 1000, heightPx: 1000, bounds: { minX: -5, maxX: 5, minZ: -5, maxZ: 5 }, base: { url: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='1000' height='1000'%3E%3Crect width='1000' height='1000' fill='%23273543'/%3E%3C/svg%3E" }, overlays: [] } }));
  await page.route("**/api/compositions", route => route.fulfill({ json: { active_composition_id: "spatial-fixture", compositions: [{ id: "spatial-fixture", name: "Offline fixture" }] } }));
  await page.route("**/api/composition", route => route.fulfill({ json: { id: "spatial-fixture", name: "Offline fixture", elements: [{ id: "reference", type: "com.toposync.core.measurement.line", position: { x: 0, y: 0, z: 0 }, rotation: { x: 0, y: 0, z: 0 }, props: { points: [{ x: 0, z: 0 }, { x: 4, z: 0 }] } }] } }));
  await page.route("**/api/notifications/count", route => route.fulfill({ json: { total: 1, by_priority: { high: 1, medium: 0, low: 0 }, unread_total: 0, unread_by_priority: { high: 0, medium: 0, low: 0 } } }));
  await page.route(/\/api\/notifications(?:\?.*)?$/, route => route.fulfill({ json: { notifications: [record], next_cursor: null } }));
  await page.route("**/api/notifications/capture-notice", route => route.fulfill({ json: record }));
  await page.route(`**/api/activity/${identifier}`, route => route.fulfill({ json: { occurrence: document, permissions: { manage: false }, revisions: [], deliveries: [], maintenance_error: null } }));
  await page.route(`**/api/activity/${identifier}/images?*`, route => route.fulfill({ json: { images, next_cursor: null } }));
  await page.route(`**/api/activity/${identifier}/images/*`, route => {
    const index = Number(path.basename(new URL(route.request().url()).pathname).split("-")[0]);
    const files = sources?.[group ? "referencia_grupo" : "referencia_pessoa"];
    const file = files?.[index * (6 / rate)];
    return route.fulfill({ contentType: "image/webp", body: file ? fs.readFileSync(file) : fallbackImage });
  });
  return { writes, images };
}

for (const rate of [1, 2, 3]) for (const group of [false, true]) test(`${group ? "group" : "person"} at ${rate} images per second`, async ({ page }, info) => {
  const { writes, images } = await fixture(page, rate, group);
  await page.goto("/#/main?notification=capture-notice");
  await page.getByRole("button", { name: /Saved event images/ }).click();
  await page.getByRole("button", { name: "Open selected notification details" }).click();
  const sequence = page.getByRole("region", { name: "Event images" });
  await expect(sequence.locator("img")).toHaveCount(1);
  if (group) { await page.getByRole("button", { name: "Group · synchronized images" }).click(); await expect(sequence.locator("img")).toHaveCount(2); }
  await expect.poll(() => sequence.locator("img").evaluateAll(items => items.every(image => image.complete && image.naturalWidth > 0))).toBe(true);
  const slider = sequence.getByRole("slider");
  await expect(slider).toHaveAttribute("max", "4");
  await sequence.getByRole("button", { name: "Play", exact: true }).click();
  await expect.poll(() => slider.inputValue()).not.toBe("0");
  await sequence.getByRole("button", { name: "Pause", exact: true }).click();
  const stopped = await slider.inputValue();
  await page.waitForTimeout(150); expect(await slider.inputValue()).toBe(stopped);
  await slider.focus();
  await slider.press("End");
  expect(Number(await slider.inputValue())).toBe(4);
  await expect(sequence.locator("img")).toHaveCount(group ? 2 : 1);
  await expect(page.locator(".activityMapImage")).toHaveCount(group ? 2 : 1);
  await page.screenshot({ path: info.outputPath("desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(sequence.getByRole("button", { name: "Play", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath("mobile.png") });
  expect(writes).toEqual([]);
});

test("permission loss removes the loaded image", async ({ page }) => {
  await fixture(page, 2, false);
  await page.goto("/#/main?notification=capture-notice");
  await page.getByRole("button", { name: /Saved event images/ }).click();
  await page.getByRole("button", { name: "Open selected notification details" }).click();
  await expect(page.locator(".activitySequenceFrame img")).toHaveCount(1);
  await page.route(`**/api/activity/${identifier}/images?*`, route => route.fulfill({ status: 404, json: { detail: "Unavailable" } }));
  await expect(page.locator(".activitySequenceFrame img")).toHaveCount(0, { timeout: 10000 });
  await expect(page.locator(".activityMapImage")).toHaveCount(0);
});

test("synchronized group remains available while switching between plan and three-dimensional view", async ({ page }, info) => {
  await fixture(page, 2, true);
  await page.goto("/#/main?notification=capture-notice");
  await page.getByRole("button", { name: /Saved event images/ }).click();
  await page.getByRole("button", { name: "Open selected notification details" }).click();
  await page.getByRole("button", { name: "Group · synchronized images" }).click();
  await expect(page.locator(".activitySequenceFrame img")).toHaveCount(2);
  for (const mode of ["2D (Plan)", "3D"]) {
    await page.getByRole("button", { name: /^(View|Rendering):/ }).click();
    await page.getByRole("dialog").getByText(mode, { exact: true }).click();
    if (mode === "3D") await expect(page.locator(".notificationSpatialViewport canvas")).toBeVisible();
    else await expect(page.locator(".activityMapImage")).toHaveCount(2);
    await page.screenshot({ path: info.outputPath(mode === "3D" ? "three-dimensional.png" : "plan.png") });
  }
  await page.locator(".activityPlaybackDock").getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.locator(".activityPlaybackDock")).toHaveCount(0);
});
