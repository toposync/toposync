const { test, expect } = require("@playwright/test");
const path = require("node:path");
const os = require("node:os");
const fs = require("node:fs/promises");
const backend = "http://127.0.0.1:8137";
const artifacts = process.env.TOPOSYNC_GEOGRAPHIC_ARTIFACT_DIR || path.join(os.homedir(), ".codex/task-artifacts/toposync/ajustes-configuracao-cameras-2026-10-10/mapa-itens-16-22/validacao/browser");
const transparentTile = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jBf0AAAAASUVORK5CYII=", "base64");
let commands, pageErrors;
test.beforeEach(async ({ page, request }) => {
  await request.post(`${backend}/api/__geographic_fixture/reset`);
  commands = [];
  pageErrors = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  await page.addInitScript(() => { localStorage.setItem("toposync.locale", "en"); localStorage.setItem("toposync.render_mode.v1", "geographic"); });
  await page.addInitScript(() => {
    for (const method of ["moveTo", "lineTo"]) {
      const original = CanvasRenderingContext2D.prototype[method];
      CanvasRenderingContext2D.prototype[method] = function(x, y) {
        if (this.canvas.closest(".geographicCompositionLayer")) {
          if (method === "moveTo") this.canvas.geographicTestVertices = [];
          const point = new DOMPoint(x, y).matrixTransform(this.getTransform());
          this.canvas.geographicTestVertices.push({ x: point.x / devicePixelRatio, y: point.y / devicePixelRatio });
        }
        return original.call(this, x, y);
      };
    }
  });
  await page.route("https://tile.openstreetmap.org/**", route => route.fulfill({ body: transparentTile, contentType: "image/png" }));
  await page.route("**/api/home_assistant/**", route => {
    if (route.request().url().endsWith("/states")) return route.fulfill({ json: { "switch.geographic-first": { entity_id: "switch.geographic-first", state: "off" }, "switch.geographic-second": { entity_id: "switch.geographic-second", state: "off" } } });
    if (route.request().url().includes("/stream")) return route.fulfill({ status: 200, body: "", contentType: "text/event-stream" });
    return route.fulfill({ json: [] });
  });
  await page.route("**/api/events/home_assistant.**", route => {
    commands.push(JSON.parse(route.request().postData()));
    return route.fulfill({ json: { payload: {}, result: { state: "on" }, prevented_default: false, stopped: false } });
  });
});
test.afterEach(() => { expect(pageErrors).toEqual([]); });

async function openMap(page) {
  await page.goto("/");
  await expect(page.getByTestId("geographic-map")).toBeVisible();
  await expect(page.getByRole("button", { name: "First switch", exact: true })).toBeVisible();
}
async function screenshot(page, name) {
  await fs.mkdir(artifacts, { recursive: true });
  await page.screenshot({ path: path.join(artifacts, name), fullPage: true });
}
async function firstMetadata(request) {
  return (await (await request.get(`${backend}/api/maps/compositions`)).json()).find(item => item.id === "geographic-first").meta;
}

test("background consent persists, higher zoom requests only native tiles, and revocation stops requests", async ({ page }) => {
  const tiles = [];
  page.on("request", request => { if (request.url().startsWith("https://tile.openstreetmap.org/")) tiles.push(request.url()); });
  await openMap(page);
  expect(tiles).toHaveLength(0);
  await page.getByRole("button", { name: "Allow external background", exact: true }).click();
  await expect.poll(() => tiles.length).toBeGreaterThan(0);
  const zoom = page.getByRole("button", { name: "Zoom in", exact: true });
  for (let step = 0; step < 3 && await zoom.getAttribute("aria-disabled") !== "true"; step++) await zoom.click();
  await expect(zoom).toHaveAttribute("aria-disabled", "true");
  expect(tiles.every(url => Number(new URL(url).pathname.split("/")[1]) <= 19)).toBe(true);
  await page.reload();
  await expect(page.getByRole("button", { name: "Disable external background", exact: true })).toBeVisible();
  await screenshot(page, "map-desktop.png");
  await page.getByRole("button", { name: "Disable external background", exact: true }).click();
  const count = tiles.length;
  await page.getByRole("button", { name: "Zoom out", exact: true }).click();
  await expect(page.getByRole("button", { name: "Allow external background", exact: true })).toBeVisible();
  expect(tiles.length).toBe(count);
  expect(commands).toHaveLength(0);
});

test("address search is manual and navigation never changes composition metadata", async ({ page, request }) => {
  let searches = 0;
  await page.route("**/api/maps/search", route => { searches++; return route.fulfill({ json: [{ id: "public-square", label: "Public square", latitude: -27.595, longitude: -48.55 }] }); });
  const before = await firstMetadata(request);
  await openMap(page);
  await page.getByLabel("Address or place", { exact: true }).fill("Public square");
  expect(searches).toBe(0);
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await page.getByRole("button", { name: "Allow and search", exact: true }).click();
  await expect(page.locator(".leaflet-control-attribution").getByRole("link", { name: "OpenStreetMap", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Public square", exact: true }).click();
  await expect(page.locator(".leaflet-control-attribution").getByRole("link", { name: "OpenStreetMap", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Allow external background", exact: true }).click();
  await page.getByRole("button", { name: "Disable external background", exact: true }).click();
  await expect(page.locator(".leaflet-control-attribution").getByRole("link", { name: "OpenStreetMap", exact: true })).toBeVisible();
  expect(await firstMetadata(request)).toEqual(before);
  await page.getByRole("button", { name: "Back to compositions", exact: true }).click();
  await expect(page.getByRole("button", { name: "First switch", exact: true })).toBeVisible();
  expect(searches).toBe(1);
  expect(commands).toHaveLength(0);
});

test("device location and denial keep metadata and manual navigation available", async ({ page, context, request }) => {
  const before = await firstMetadata(request);
  await context.grantPermissions(["geolocation"]);
  await context.setGeolocation({ latitude: -27.595, longitude: -48.55, accuracy: 7 });
  await openMap(page);
  await page.getByRole("button", { name: "My location", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "approximate accuracy of 7 metres" })).toBeVisible();
  expect(await firstMetadata(request)).toEqual(before);
  await page.getByRole("button", { name: "Back to compositions", exact: true }).click();
  await context.clearPermissions();
  await page.evaluate(() => { navigator.geolocation.getCurrentPosition = (_ok, fail) => fail({ code: 1 }); });
  await page.getByRole("button", { name: "My location", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "permission was denied" })).toBeVisible();
  expect(commands).toHaveLength(0);
});

test("detections, trails and details use their owning composition and update without duplicate pins", async ({ page, request }) => {
  await openMap(page);
  const owner = page.locator('.geographicCompositionLayer[data-composition-id="geographic-first"]');
  await expect(owner.locator("[data-notification-id]")).toHaveCount(1);
  await expect(owner.locator("polyline")).toHaveCount(1);
  await expect(page.locator('.geographicCompositionLayer[data-composition-id="geographic-second"] [data-notification-id]')).toHaveCount(0);
  const previous = await owner.locator("polyline").getAttribute("points");
  await request.post(`${backend}/api/__geographic_fixture/notification`, { data: { trail: [{ x: 3, z: 3 }, { x: 6, z: 3 }, { x: 8, z: 8 }] } });
  await expect.poll(() => owner.locator("polyline").getAttribute("points")).not.toBe(previous);
  await expect(owner.locator("[data-notification-id]")).toHaveCount(1);
  await owner.getByRole("button", { name: "Synthetic detection", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  expect(commands).toHaveLength(0);
});

test("only explicit controls operate an entity and another composition opens its own controls", async ({ page }) => {
  await openMap(page);
  await page.getByRole("button", { name: "First switch", exact: true }).click();
  await expect(page.getByRole("dialog").filter({ hasText: "switch.geographic-first" })).toBeVisible();
  expect(commands).toHaveLength(0);
  await page.getByRole("dialog").getByRole("button", { name: "Toggle", exact: true }).click();
  await expect.poll(() => commands.length).toBe(1);
  expect(commands[0].payload.entity_id).toBe("switch.geographic-first");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Second switch", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Controls in another composition" })).toBeVisible();
  expect(commands).toHaveLength(1);
  await page.getByRole("button", { name: "Open composition controls", exact: true }).click();
  await expect(page.getByRole("dialog").filter({ hasText: "switch.geographic-second" })).toBeVisible();
  expect(commands).toHaveLength(1);
});

test("overlapping 44-pixel buttons offer a choice when centers are more than 22 pixels apart", async ({ page, request }) => {
  const composition = await (await request.get(`${backend}/api/composition`)).json();
  const original = composition.elements.find(element => element.id === "same-switch");
  composition.elements.push({ ...original, id: "nearby-switch", name: "Nearby switch", position: { ...original.position, x: original.position.x + 3 } });
  expect((await request.put(`${backend}/api/composition`, { data: composition })).ok()).toBe(true);
  await page.route(/\/api\/notifications\?/, route => route.fulfill({ json: { notifications: [], next_cursor: null } }));
  await openMap(page);
  const first = await page.getByRole("button", { name: "First switch", exact: true }).boundingBox();
  const nearby = await page.getByRole("button", { name: "Nearby switch", exact: true }).boundingBox();
  const distance = Math.hypot(first.x - nearby.x, first.y - nearby.y);
  expect(distance).toBeGreaterThan(22);
  expect(distance).toBeLessThan(44);
  await page.mouse.click((first.x + nearby.x + first.width) / 2, (first.y + nearby.y + first.height) / 2);
  const choice = page.getByRole("dialog", { name: "Choose an element" });
  await expect(choice).toBeVisible();
  expect(commands).toHaveLength(0);
  await choice.getByRole("button", { name: "First · nearby-switch", exact: true }).click();
  await expect(page.getByRole("dialog").filter({ hasText: "switch.geographic-first" })).toBeVisible();
  expect(commands).toHaveLength(0);
});

test("mobile layout, keyboard access and editor reuse permission without moving the house", async ({ page, request }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await openMap(page);
  await page.getByRole("button", { name: "Allow external background", exact: true }).click();
  const before = await firstMetadata(request);
  await screenshot(page, "map-mobile.png");
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByRole("button", { name: "Map", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Composition on the map" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Disable external background", exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => {
    const layer = document.querySelector(".georeferenceMap .geographicCompositionLayer");
    const points = layer.querySelector("canvas").geographicTestVertices;
    const pin = layer.querySelector(".geographicPinPosition");
    if (!points?.length || !pin) return Infinity;
    const center = points.reduce((total, point) => ({ x: total.x + point.x / points.length, y: total.y + point.y / points.length }), { x: 0, y: 0 });
    return Math.hypot(parseFloat(pin.style.left) - center.x, parseFloat(pin.style.top) - center.y);
  })).toBeLessThanOrEqual(1);
  await screenshot(page, "map-editor-mobile.png");
  await page.getByLabel("Address or place", { exact: true }).focus();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Escape");
  expect(await firstMetadata(request)).toEqual(before);
  expect(commands).toHaveLength(0);
});

test("search failures, empty results and revoked permission keep local controls usable", async ({ page }) => {
  await page.route("**/api/maps/search", route => route.fulfill({ status: 429, json: { detail: "map_search_rate_limit" } }));
  await openMap(page);
  await page.getByLabel("Address or place").fill("Public square");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await page.getByRole("button", { name: "Allow and search", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Wait two seconds" })).toBeVisible();
  await page.route("**/api/maps/search", route => route.fulfill({ json: [] }));
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "No places found" })).toBeVisible();
  await page.getByRole("button", { name: "Disable external address search", exact: true }).click();
  await expect(page.getByRole("button", { name: "First switch", exact: true })).toBeVisible();
  expect(commands).toHaveLength(0);
});

test("double click, long press and denied commands preserve the existing action modal", async ({ page }) => {
  await openMap(page);
  const target = page.getByRole("button", { name: "First switch", exact: true });
  await target.dblclick({ delay: 350 });
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(commands).toHaveLength(0);
  await page.keyboard.press("Escape");
  const bounds = await target.boundingBox();
  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
  await page.mouse.down();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.mouse.up();
  expect(commands).toHaveLength(0);
  await page.keyboard.press("Escape");
  await target.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(commands).toHaveLength(0);
  await page.keyboard.press("Escape");
  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width / 2 + 40, bounds.y + bounds.height / 2, { steps: 4 });
  await page.mouse.up();
  await page.waitForTimeout(600);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(commands).toHaveLength(0);
  await page.route("**/api/events/home_assistant.**", route => { commands.push(JSON.parse(route.request().postData())); return route.fulfill({ status: 403, json: { detail: "Permission denied" } }); });
  await target.click();
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(commands).toHaveLength(0);
  await page.getByRole("dialog").getByRole("button", { name: "Toggle", exact: true }).click();
  await expect(page.getByRole("dialog").getByText("Permission denied", { exact: true })).toBeVisible();
  expect(commands).toHaveLength(1);
});

test("device timeout and iframe permissions policy leave address search available", async ({ page, context }) => {
  await openMap(page);
  await page.evaluate(() => { navigator.geolocation.getCurrentPosition = (_ok, fail) => fail({ code: 3 }); });
  await page.getByRole("button", { name: "My location", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Location took too long" })).toBeVisible();
  await context.grantPermissions(["geolocation"]);
  await page.route("**/iframe-host", route => route.fulfill({ contentType: "text/html", body: '<iframe src="/" allow="geolocation \'none\'" style="width:100%;height:800px"></iframe>' }));
  await page.goto("/iframe-host");
  const frame = page.frameLocator("iframe");
  await frame.getByRole("button", { name: "My location", exact: true }).click();
  await expect(frame.getByRole("status").filter({ hasText: "permission was denied" })).toBeVisible();
  await expect(frame.getByLabel("Address or place")).toBeVisible();
  expect(commands).toHaveLength(0);
});

test("map reads and controls retain the Home Assistant ingress prefix", async ({ page }) => {
  const requests = [];
  await page.addInitScript(() => { window.__TOPOSYNC_PUBLIC_BASE_PATH__ = "/test-ingress"; });
  await page.route("**/test-ingress/**", async route => {
    requests.push(route.request().url());
    const url = new URL(route.request().url()); url.pathname = url.pathname.replace("/test-ingress", "");
    if (url.pathname.includes("/stream")) return route.fulfill({ body: "", contentType: "text/event-stream" });
    const response = await route.fetch({ url: url.toString() });
    await route.fulfill({ response });
  });
  await openMap(page);
  expect(requests.some(url => url.includes("/test-ingress/api/maps/config"))).toBe(true);
  expect(requests.some(url => url.includes("/test-ingress/api/maps/compositions"))).toBe(true);
  expect(commands).toHaveLength(0);
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

test("editor saving preserves unrelated metadata and rejects a concurrent anchor change", async ({ page, request }) => {
  await openMap(page);
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByRole("button", { name: "Map", exact: true }).click();
  await page.getByText("Manual anchor and accuracy", { exact: true }).click();
  await page.getByLabel("Latitude", { exact: true }).fill("-27.5955");
  const response = await request.patch(`${backend}/api/compositions/geographic-first/meta`, { data: { changes: { unrelated: "retained" }, expected: { unrelated: null } } });
  expect(response.ok()).toBe(true);
  await page.getByRole("button", { name: "Save position", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Composition on the map" })).toHaveCount(0);
  let metadata = await firstMetadata(request);
  expect(metadata.georeference.latitude).toBe(-27.5955);
  const active = await (await request.get(`${backend}/api/composition`)).json();
  expect(active.meta.unrelated).toBe("retained");
  await page.getByRole("button", { name: "Map", exact: true }).click();
  await request.patch(`${backend}/api/compositions/geographic-first/meta`, { data: { changes: { georeference: { ...metadata.georeference, latitude: -27.5954 } }, expected: { georeference: metadata.georeference } } });
  await page.getByRole("button", { name: "Save position", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Could not save the position" })).toBeVisible();
  expect((await firstMetadata(request)).georeference.latitude).toBe(-27.5954);
  expect(commands).toHaveLength(0);
});

test("two tabs share background revocation and blocked storage falls back to the browser session", async ({ page, context }) => {
  await openMap(page);
  await page.getByRole("button", { name: "Allow external background", exact: true }).click();
  const other = await context.newPage();
  await other.route("https://tile.openstreetmap.org/**", route => route.fulfill({ body: transparentTile, contentType: "image/png" }));
  await other.goto("/");
  await expect(other.getByTestId("geographic-map")).toBeVisible();
  await expect(other.getByRole("button", { name: "Disable external background", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Disable external background", exact: true }).click();
  await expect(other.getByRole("button", { name: "Allow external background", exact: true })).toBeVisible();
  await other.close();
  await page.evaluate(() => { const original = Storage.prototype.setItem; Storage.prototype.setItem = function(key, value) {
    if (key.startsWith("toposync.map.permission")) throw new DOMException("Test quota", "QuotaExceededError");
    return original.call(this, key, value);
  }; });
  await page.getByRole("button", { name: "Allow external background", exact: true }).click();
  await page.getByRole("button", { name: "Allow external background", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "could not save the permission" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Disable external background", exact: true })).toBeVisible();
  expect(commands).toHaveLength(0);
});

test("two compositions, 200 elements and 100 trails stay responsive through 20 view changes", async ({ page, request }) => {
  test.setTimeout(120000);
  const compositions = (await (await request.get(`${backend}/api/maps/compositions`)).json()).filter(item => item.id !== "unpositioned");
  for (const composition of compositions) {
    composition.elements = Array.from({ length: 100 }, (_, index) => {
      const x = index % 10 * 2, z = Math.floor(index / 10) * 2;
      return { id: `element-${index}`, name: `Synthetic element ${index}`, type: "com.toposync.structural.area", position: { x, z }, props: {
        vertices: [{ x, z }, { x: x + 1, z }, { x: x + 1, z: z + 1 }, { x, z: z + 1 }], fill: "#67887b", opacity: 1,
      } };
    });
    await request.post(`${backend}/api/compositions/${composition.id}/activate`);
    await request.put(`${backend}/api/composition`, { data: composition });
  }
  await request.post(`${backend}/api/compositions/geographic-first/activate`);
  const notifications = Array.from({ length: 100 }, (_, index) => ({ id: `occurrence-${index}`, type: "pipelines.tracking", title: `Synthetic occurrence ${index}`, priority: "high", createdAt: new Date().toISOString(), payload: {
    composition_id: compositions[index % 2].id, priority: "high", lifecycle: "close",
    trail: Array.from({ length: 100 }, (_, point) => ({ x: index % 10 + point / 50, z: Math.floor(index / 10) + point / 80 })),
  } }));
  await page.route(/\/api\/notifications\?/, route => route.fulfill({ json: { notifications, next_cursor: null } }));
  await page.addInitScript(() => {
    const Original = window.EventSource;
    window.__geographicTestLiveSources = 0;
    window.EventSource = class extends Original {
      constructor(...args) { super(...args); this.closedForTest = false; window.__geographicTestLiveSources++; }
      close() { if (!this.closedForTest) { this.closedForTest = true; window.__geographicTestLiveSources--; } super.close(); }
    };
  });
  await page.goto("/");
  await expect(page.locator(".geographicMarker")).toHaveCount(200);
  await expect(page.locator("[data-notification-id]")).toHaveCount(100);
  const initialSources = await page.evaluate(() => window.__geographicTestLiveSources);
  const debuggerSession = await page.context().newCDPSession(page);
  await debuggerSession.send("Tracing.start", { categories: "devtools.timeline,v8.execute,blink.user_timing", transferMode: "ReturnAsStream" });
  const durations = await page.evaluate(async () => {
    const samples = [];
    for (let index = 0; index < 20; index++) {
      const start = performance.now();
      document.querySelector(index % 2 ? ".leaflet-control-zoom-out" : ".leaflet-control-zoom-in").click();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      samples.push(performance.now() - start);
    }
    return samples.sort((left, right) => left - right);
  });
  const percentile95 = durations[Math.ceil(durations.length * 0.95) - 1];
  const tracingComplete = new Promise(resolve => debuggerSession.once("Tracing.tracingComplete", resolve));
  await debuggerSession.send("Tracing.end");
  const trace = await tracingComplete;
  let traceData = "";
  while (true) {
    const chunk = await debuggerSession.send("IO.read", { handle: trace.stream });
    traceData += chunk.data;
    if (chunk.eof) break;
  }
  await debuggerSession.send("IO.close", { handle: trace.stream });
  await debuggerSession.detach();
  await fs.mkdir(artifacts, { recursive: true });
  await fs.writeFile(path.join(artifacts, "performance-trace.json"), traceData);
  await fs.writeFile(path.join(artifacts, "interaction-samples.json"), JSON.stringify({ percentile95, durations }, null, 2));
  expect(percentile95).toBeLessThan(100);
  for (let index = 0; index < 20; index++) {
    await page.getByRole("button", { name: "Rendering: Map", exact: true }).click();
    await page.getByRole("dialog").getByText("2D (Plan)", { exact: true }).click();
    await expect(page.getByTestId("geographic-map")).toHaveCount(0);
    await page.getByRole("button", { name: "Rendering: 2D (Plan)", exact: true }).click();
    await page.getByRole("dialog").getByText("Map", { exact: true }).click();
    await expect(page.locator(".geographicMarker")).toHaveCount(200);
    await expect(page.locator("[data-notification-id]")).toHaveCount(100);
  }
  const finalSources = await page.evaluate(() => window.__geographicTestLiveSources);
  expect(finalSources).toBe(initialSources);
  await fs.mkdir(artifacts, { recursive: true });
  await fs.writeFile(path.join(artifacts, "performance.json"), JSON.stringify({ elements: 200, detections: 100, pointsPerTrail: 100, changes: 20, percentile95, durations, initialSources, finalSources }, null, 2));
  expect(commands).toHaveLength(0);
});
