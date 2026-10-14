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

async function openMap(page, pendingConsent = false) {
  await page.goto("/");
  await expect(page.getByTestId("geographic-map")).toBeVisible();
  if (!pendingConsent) await page.getByRole("dialog", { name: "Map background" }).getByRole("button", { name: "Continue without background", exact: true }).click();
  await expect(page.getByRole("button", { name: "First switch", exact: true })).toBeVisible();
}
async function zoomOut(page, steps = 1) {
  for (let index = 0; index < steps; index++) await page.locator('.geographicLeaflet').press('-');
}
async function openPositionEditor(page) {
  await page.getByRole('button', { name: 'Edit', exact: true }).click();
  await page.getByRole('button', { name: 'Position on map', exact: true }).click();
}
async function openSearchEditor(page) {
  await openPositionEditor(page);
  await page.getByText('Find a place', { exact: true }).click();
}
async function openOptions(page) {
  await page.getByLabel('Map options', { exact: true }).click();
}
async function screenshot(page, name) {
  await fs.mkdir(artifacts, { recursive: true });
  await page.screenshot({ path: path.join(artifacts, name), fullPage: true });
}
async function firstMetadata(request) {
  return (await (await request.get(`${backend}/api/maps/compositions`)).json()).find(item => item.id === "geographic-first").meta;
}

test("background confirmation is remembered and revocation stops requests without prompting again", async ({ page }) => {
  const tiles = [];
  page.on("request", request => { if (request.url().startsWith("https://tile.openstreetmap.org/")) tiles.push(request.url()); });
  await openMap(page, true);
  expect(tiles).toHaveLength(0);
  await page.getByRole("button", { name: "Allow external background", exact: true }).click();
  await expect.poll(() => tiles.length).toBeGreaterThan(0);
  await page.locator('.geographicLeaflet').press('+');
  await page.locator('.geographicLeaflet').press('+');
  expect(tiles.every(url => Number(new URL(url).pathname.split("/")[1]) <= 19)).toBe(true);
  await page.reload();
  await expect(page.getByTestId('geographic-map')).toBeVisible();
  await expect(page.getByRole('dialog', { name: 'Map background' })).toHaveCount(0);
  await expect(page.locator('.leaflet-tile')).not.toHaveCount(0);
  await screenshot(page, "map-desktop.png");
  await openOptions(page);
  await page.getByRole("button", { name: "Disable external background", exact: true }).click();
  await expect(page.locator('.leaflet-tile')).toHaveCount(0);
  const count = tiles.length;
  await zoomOut(page);
  await page.reload();
  await expect(page.getByTestId('geographic-map')).toBeVisible();
  await expect(page.getByRole('dialog', { name: 'Map background' })).toHaveCount(0);
  expect(tiles.length).toBe(count);
  await openOptions(page);
  await page.getByRole('button', { name: 'Allow external background', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Map background' })).toBeVisible();
  await page.getByRole('button', { name: 'Allow external background', exact: true }).click();
  await expect(page.locator('.leaflet-tile')).not.toHaveCount(0);
  expect(commands).toHaveLength(0);
});

test("address search is manual and navigation never changes composition metadata", async ({ page, request }) => {
  let searches = 0;
  await page.route("**/api/maps/search", route => { searches++; return route.fulfill({ json: [{ id: "public-square", label: "Public square", latitude: -27.595, longitude: -48.55 }] }); });
  const before = await firstMetadata(request);
  await openMap(page);
  await openSearchEditor(page);
  await page.getByLabel("Address or place", { exact: true }).fill("Public square");
  expect(searches).toBe(0);
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await page.getByRole("button", { name: "Allow and search", exact: true }).click();
  await expect(page.locator(".leaflet-control-attribution").getByRole("link", { name: "OpenStreetMap", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Public square", exact: true }).click();
  await expect(page.locator(".leaflet-control-attribution").getByRole("link", { name: "OpenStreetMap", exact: true })).toBeVisible();
  await openOptions(page);
  await page.getByRole("button", { name: "Allow external background", exact: true }).click();
  await page.getByRole("button", { name: "Allow external background", exact: true }).click();
  await openOptions(page);
  await page.getByRole("button", { name: "Disable external background", exact: true }).click();
  await expect(page.locator(".leaflet-control-attribution").getByRole("link", { name: "OpenStreetMap", exact: true })).toBeVisible();
  expect(await firstMetadata(request)).toEqual(before);
  await page.getByRole("button", { name: "Frame compositions", exact: true }).click();
  await page.getByRole("dialog", { name: "Position on map" }).getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(page.getByRole("button", { name: "First switch", exact: true })).toBeVisible();
  expect(searches).toBe(1);
  expect(commands).toHaveLength(0);
});

test("device location and denial keep metadata and manual navigation available", async ({ page, context, request }) => {
  const before = await firstMetadata(request);
  await context.grantPermissions(["geolocation"]);
  await context.setGeolocation({ latitude: -27.595, longitude: -48.55, accuracy: 7 });
  await openMap(page);
  await openSearchEditor(page);
  await page.getByRole("button", { name: "My location", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "approximate accuracy of 7 metres" })).toBeVisible();
  expect(await firstMetadata(request)).toEqual(before);
  await page.getByRole("button", { name: "Frame compositions", exact: true }).click();
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
  await owner.getByRole("button", { name: /^Synthetic detection ·/ }).click();
  await page.getByRole("button", { name: "Open selected notification details", exact: true }).click();
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
  await zoomOut(page, 2);
  await page.getByRole("button", { name: "Second switch", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Controls in another composition" })).toBeVisible();
  expect(commands).toHaveLength(1);
  await page.getByRole("button", { name: "Open composition controls", exact: true }).click();
  await expect(page.getByRole("dialog").filter({ hasText: "switch.geographic-second" })).toBeVisible();
  expect(commands).toHaveLength(1);
});

test("overlapping device markers offer a choice without operating entities", async ({ page, request }) => {
  const composition = await (await request.get(`${backend}/api/composition`)).json();
  const original = composition.elements.find(element => element.id === "same-switch");
  composition.elements.push({ ...original, id: "nearby-switch", name: "Nearby switch", position: { ...original.position, x: original.position.x + 0.1 } });
  expect((await request.put(`${backend}/api/composition`, { data: composition })).ok()).toBe(true);
  await page.goto('/');
  await page.getByRole('button', { name: 'Continue without background', exact: true }).click();
  await page.getByRole('button', { name: '2 devices', exact: true }).click();
  const choice = page.getByRole("dialog", { name: "Choose an element" });
  await expect(choice).toBeVisible();
  expect(commands).toHaveLength(0);
  await choice.getByRole("button", { name: /^Nearby switch/ }).click();
  await expect(page.getByRole("dialog").filter({ hasText: "switch.geographic-first" })).toBeVisible();
  expect(commands).toHaveLength(0);
});

test("mobile layout, keyboard access and editor reuse permission without moving the house", async ({ page, request }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await openMap(page, true);
  await page.getByRole("button", { name: "Allow external background", exact: true }).click();
  const before = await firstMetadata(request);
  await screenshot(page, "map-mobile.png");
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  await openPositionEditor(page);
  await expect(page.getByRole("dialog", { name: "Position on map" })).toBeVisible();
  await openOptions(page);
  await expect(page.getByRole("button", { name: "Disable external background", exact: true })).toBeVisible();
  await openOptions(page);
  await expect.poll(() => page.evaluate(() => {
    const layer = document.querySelector(".georeferenceMap .geographicCompositionLayer");
    const points = layer.querySelector("canvas").geographicTestVertices;
    const pin = layer.querySelector(".geographicPinPosition");
    if (!points?.length || !pin) return Infinity;
    const uniquePoints = Array.from(new Map(points.map(point => [`${point.x},${point.y}`, point])).values());
    const center = uniquePoints.reduce((total, point) => ({ x: total.x + point.x / uniquePoints.length, y: total.y + point.y / uniquePoints.length }), { x: 0, y: 0 });
    return Math.hypot(parseFloat(pin.style.left) - center.x, parseFloat(pin.style.top) - center.y);
  })).toBeLessThanOrEqual(1);
  await screenshot(page, "map-editor-mobile.png");
  await page.getByText("Find a place", { exact: true }).click();
  await page.getByLabel("Address or place", { exact: true }).focus();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Escape");
  expect(await firstMetadata(request)).toEqual(before);
  expect(commands).toHaveLength(0);
});

test("search failures, empty results and revoked permission keep local controls usable", async ({ page }) => {
  await page.route("**/api/maps/search", route => route.fulfill({ status: 429, json: { detail: "map_search_rate_limit" } }));
  await openMap(page);
  await openSearchEditor(page);
  await page.getByLabel("Address or place").fill("Public square");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await page.getByRole("button", { name: "Allow and search", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Wait two seconds" })).toBeVisible();
  await page.route("**/api/maps/search", route => route.fulfill({ json: [] }));
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "No places found" })).toBeVisible();
  await page.getByRole("button", { name: "Disable external address search", exact: true }).click();
  await expect(page.locator(".georeferenceMap canvas")).toBeVisible();
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
  await openSearchEditor(page);
  await page.evaluate(() => { navigator.geolocation.getCurrentPosition = (_ok, fail) => fail({ code: 3 }); });
  await page.getByRole("button", { name: "My location", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Location took too long" })).toBeVisible();
  await context.grantPermissions(["geolocation"]);
  await page.route("**/iframe-host", route => route.fulfill({ contentType: "text/html", body: '<iframe src="/" allow="geolocation \'none\'" style="width:100%;height:800px"></iframe>' }));
  await page.goto("/iframe-host");
  const frame = page.frameLocator("iframe");
  await frame.getByRole("button", { name: "Edit", exact: true }).click();
  await frame.getByRole("button", { name: "Position on map", exact: true }).click();
  await frame.getByText("Find a place", { exact: true }).click();
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
  await openPositionEditor(page);
  await page.getByText("Manual anchor and accuracy", { exact: true }).click();
  await page.getByLabel("Latitude", { exact: true }).fill("-27.5955");
  const response = await request.patch(`${backend}/api/compositions/geographic-first/meta`, { data: { changes: { unrelated: "retained" }, expected: { unrelated: null } } });
  expect(response.ok()).toBe(true);
  await page.getByRole("button", { name: "Save position", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Position on map" })).toHaveCount(0);
  let metadata = await firstMetadata(request);
  expect(metadata.georeference.latitude).toBe(-27.5955);
  const active = await (await request.get(`${backend}/api/composition`)).json();
  expect(active.meta.unrelated).toBe("retained");
  await page.getByRole("button", { name: "Position on map", exact: true }).click();
  await request.patch(`${backend}/api/compositions/geographic-first/meta`, { data: { changes: { georeference: { ...metadata.georeference, latitude: -27.5954 } }, expected: { georeference: metadata.georeference } } });
  await page.getByRole("button", { name: "Save position", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "composition changed elsewhere" })).toBeVisible();
  expect((await firstMetadata(request)).georeference.latitude).toBe(-27.5954);
  expect(commands).toHaveLength(0);
});

test("two tabs share background revocation and blocked storage uses the current session", async ({ page, context }) => {
  await openMap(page, true);
  await page.getByRole("button", { name: "Allow external background", exact: true }).click();
  const other = await context.newPage();
  await other.route("https://tile.openstreetmap.org/**", route => route.fulfill({ body: transparentTile, contentType: "image/png" }));
  await other.goto("/");
  await expect(other.locator('.leaflet-tile')).not.toHaveCount(0);
  await expect(other.getByRole('dialog', { name: 'Map background' })).toHaveCount(0);
  await openOptions(page);
  await page.getByRole("button", { name: "Disable external background", exact: true }).click();
  await expect(other.locator('.leaflet-tile')).toHaveCount(0);
  await expect(other.getByRole('dialog', { name: 'Map background' })).toHaveCount(0);
  await other.close();
  await page.evaluate(() => { const original = Storage.prototype.setItem; Storage.prototype.setItem = function(key, value) {
    if (key.startsWith("toposync.map.permission")) throw new DOMException("Test quota", "QuotaExceededError");
    return original.call(this, key, value);
  }; });
  await openOptions(page);
  await page.getByRole("button", { name: "Allow external background", exact: true }).click();
  await page.getByRole("button", { name: "Allow external background", exact: true }).click();
  await openOptions(page);
  await expect(page.getByRole("status").filter({ hasText: "could not save the permission" })).toBeVisible();
  await openOptions(page);
  await openPositionEditor(page);
  await expect(page.getByRole('dialog', { name: 'Map background' })).toHaveCount(0);
  await expect(page.locator('.georeferenceMap .leaflet-tile')).not.toHaveCount(0);
  expect(commands).toHaveLength(0);
});

test("two compositions, 200 elements and 100 loaded occurrences render only the selected trail through 20 view changes", async ({ page, request }) => {
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
  await page.getByRole("button", { name: "Continue without background", exact: true }).click();
  await expect(page.locator(".geographicCompositionLayer canvas")).toHaveCount(2);
  await expect(page.locator("[data-notification-id]")).toHaveCount(1);
  const initialSources = await page.evaluate(() => window.__geographicTestLiveSources);
  const debuggerSession = await page.context().newCDPSession(page);
  await debuggerSession.send("Tracing.start", { categories: "devtools.timeline,v8.execute,blink.user_timing", transferMode: "ReturnAsStream" });
  const durations = await page.evaluate(async () => {
    const samples = [];
    const map = document.querySelector(".geographicLeaflet"); map.focus();
    for (let index = 0; index < 20; index++) {
      const start = performance.now();
      map.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: index % 2 ? "-" : "+", keyCode: index % 2 ? 189 : 187 }));
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
    await page.getByRole("button", { name: /^View:/ }).click();
    await page.getByRole("dialog").getByRole("button", { name: /^2D \(Plan\)/ }).click();
    await expect(page.getByTestId("geographic-map")).toHaveCount(0);
    await page.getByRole("button", { name: /^View:/ }).click();
    await page.getByRole("dialog").getByRole("button", { name: /^Geographic map/ }).click();
    await expect(page.locator(".geographicCompositionLayer canvas")).toHaveCount(2);
    await expect(page.locator("[data-notification-id]")).toHaveCount(1);
  }
  const finalSources = await page.evaluate(() => window.__geographicTestLiveSources);
  expect(finalSources).toBe(initialSources);
  await fs.mkdir(artifacts, { recursive: true });
  await fs.writeFile(path.join(artifacts, "performance.json"), JSON.stringify({ elements: 200, detections: 100, pointsPerTrail: 100, changes: 20, percentile95, durations, initialSources, finalSources }, null, 2));
  expect(commands).toHaveLength(0);
});

test.describe('map continuity', () => {
  test('mobile selection survives expansion without truncating controls', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await openMap(page);
    await page.getByRole('button', { name: 'Open notifications', exact: true }).click();
    const summary = page.locator('.geographicSelection');
    await expect(summary).toContainText('Synthetic detection');
    await expect(page.locator('.railScroll')).toBeHidden();
    await expect(page.locator('.geographicToolbar')).toHaveCount(0);
    await page.getByRole('button', { name: 'Expand occurrence list' }).click();
    await expect(page.locator('.notificationCard.isActive')).toBeInViewport();
    await page.locator('.notificationCard.isActive').click();
    await expect(summary).toBeVisible();
    await expect(page.locator('.geographicError')).toHaveCount(0);
    await expect(page.getByRole('combobox', { name: 'Trajectories', exact: true })).toHaveCount(0);
    for (const width of [320, 375]) {
      await page.setViewportSize({ width, height: 812 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await expect(page.locator('.geographicSelectionDetails')).toBeInViewport();
    }
  });

  test('a long unlocated selection stays readable and reopening finds its row in a long list', async ({ page }) => {
    const title = 'A person remained near the entrance for a long time while another occurrence was recorded nearby';
    const records = Array.from({ length: 45 }, (_, index) => ({ id: `selection-${index}`, type: 'pipelines.tracking',
      title: index === 44 ? title : `Occurrence ${index}`, priority: 'high',
      createdAt: new Date(Date.UTC(2026, 9, 13, 12, 45 - index)).toISOString(),
      payload: { priority: 'high', composition_id: 'geographic-first', lifecycle: 'close' } }));
    await page.route('**/api/notifications/stream', route => route.fulfill({ body: '', contentType: 'text/event-stream' }));
    await page.route(/\/api\/notifications(?:\?.*)?$/, route => route.fulfill({ json: { notifications: records, next_cursor: null } }));
    await page.setViewportSize({ width: 375, height: 812 });
    await openMap(page);
    await page.getByRole('button', { name: 'Open notifications', exact: true }).click();
    await page.getByRole('button', { name: 'Expand occurrence list' }).click();
    await page.locator('.notificationCard').filter({ hasText: title }).click();
    await expect(page.locator('.geographicSelectionDetails')).toContainText(title);
    await expect(page.locator('[data-notification-id]')).toHaveCount(0);
    await page.getByRole('button', { name: 'Expand occurrence list' }).click();
    await expect(page.locator('.notificationCard.isActive')).toBeInViewport();
    expect(await page.locator('.railScroll').evaluate(element => element.scrollTop)).toBeGreaterThan(100);
    await page.getByRole('button', { name: 'Show map and occurrence summary' }).click();
    await expect(page.locator('.geographicSelectionDetails')).toContainText(title);
    await screenshot(page, 'continuity-long-selection.png');
  });

  test('editor exit restores map framing and settings origin', async ({ page }) => {
    await openMap(page);
    await zoomOut(page, 2);
    const marker = page.getByRole('button', { name: 'First switch', exact: true });
    const initial = await marker.boundingBox();
    await zoomOut(page);
    await expect.poll(async () => Math.abs((await marker.boundingBox()).x - initial.x)).toBeGreaterThan(2);
    const before = await marker.boundingBox();
    await page.getByRole('button', { name: 'Edit', exact: true }).click();
    await page.getByRole('button', { name: 'Back', exact: true }).click();
    await expect(marker).toBeVisible();
    await expect.poll(async () => {
      const after = await marker.boundingBox();
      return Math.abs(after.x - before.x) + Math.abs(after.y - before.y);
    }).toBeLessThan(2);
    await page.getByRole('button', { name: 'Settings', exact: true }).click();
    await page.getByRole('button', { name: /^Compositions\b/ }).click();
    await page.getByRole('button', { name: 'Open editor for First', exact: true }).click();
    await page.getByRole('button', { name: 'Back', exact: true }).click();
    await expect(page).toHaveURL(/\/settings$/);
    await expect(page.getByRole('button', { name: 'Open editor for First', exact: true })).toBeVisible();
  });

  test('position preview stays visible while controls scroll and invalid input is retained', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await openMap(page);
    await page.getByRole('button', { name: 'Edit', exact: true }).click();
    await page.getByRole('button', { name: 'Position on map', exact: true }).click();
    const preview = page.locator('.georeferenceMap .geographicMapStage');
    expect((await preview.boundingBox()).height).toBeGreaterThan(130);
    await page.getByLabel('Orientation', { exact: true }).fill('');
    await expect(page.getByRole('button', { name: 'Save position', exact: true })).toBeDisabled();
    await page.getByRole('button', { name: 'Undo adjustments', exact: true }).click();
    await page.getByText('Manual anchor and accuracy', { exact: true }).click();
    await page.getByLabel('Latitude', { exact: true }).fill('-27.5');
    await expect(preview).toBeInViewport();
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'You have unsaved adjustments. Discard them?' })).toBeVisible();
    await page.getByRole('button', { name: 'Keep editing', exact: true }).click();
    await expect(page.getByLabel('Latitude', { exact: true })).toHaveValue('-27.5');
    await screenshot(page, 'continuity-position-mobile.png');
    await page.setViewportSize({ width: 320, height: 480 });
    await page.getByLabel('Longitude', { exact: true }).fill('-48.5');
    await expect(page.getByRole('button', { name: 'Save position', exact: true })).toBeInViewport();
    await expect(preview).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
});

test.describe('simplified main map', () => {
  for (const action of ['dismiss', 'decline']) test(`background ${action} is saved without another automatic prompt`, async ({ page }) => {
    const tiles = [];
    page.on('request', request => { if (request.url().startsWith('https://tile.openstreetmap.org/')) tiles.push(request.url()); });
    await openMap(page, true);
    if (action === 'dismiss') await page.keyboard.press('Escape');
    else await page.getByRole('button', { name: 'Continue without background', exact: true }).click();
    await page.reload();
    await expect(page.getByTestId('geographic-map')).toBeVisible();
    await expect(page.getByRole('dialog', { name: 'Map background' })).toHaveCount(0);
    expect(tiles).toHaveLength(0);
    await expect(page.locator('.geographicToolbar,.geographicUnpositioned')).toHaveCount(0);
    await expect(page.getByText('Find a place', { exact: true })).toHaveCount(0);
  });

  test('only the selected notification produces a pin and trail, including outside the current results', async ({ page }) => {
    const records = ['first-occurrence', 'second-occurrence'].map((id, index) => ({ id, type: 'pipelines.tracking', title: id, priority: 'high',
      createdAt: new Date(Date.UTC(2026, 9, 14, 12 - index)).toISOString(),
      payload: { priority: 'high', composition_id: 'geographic-first', lifecycle: 'close', trail: [{ x: index + 3, z: 3 }, { x: index + 6, z: 6 }] } }));
    await page.route('**/api/notifications/stream', route => route.fulfill({ body: '', contentType: 'text/event-stream' }));
    await page.route(/\/api\/notifications(?:\?.*)?$/, route => route.fulfill({ json: { notifications: new URL(route.request().url()).searchParams.get('query') ? [] : records, next_cursor: null } }));
    await openMap(page);
    await expect(page.locator('[data-notification-id]')).toHaveCount(1);
    await expect(page.locator('[data-notification-id]')).toHaveAttribute('data-notification-id', records[0].id);
    await page.locator('.notificationCard').filter({ hasText: records[1].title }).click();
    await expect(page.locator('[data-notification-id]')).toHaveCount(1);
    await expect(page.locator('[data-notification-id]')).toHaveAttribute('data-notification-id', records[1].id);
    await expect(page.locator('.geographicCompositionLayer polyline')).toHaveCount(1);
    await expect(page.locator('.geographicEventCluster')).toHaveCount(0);
    await page.getByRole('button', { name: 'Search events', exact: true }).click();
    await page.getByLabel('Search', { exact: true }).fill('no matching occurrence');
    await expect(page.locator('.notificationCard')).toHaveCount(0);
    await expect(page.locator('[data-notification-id]')).toHaveAttribute('data-notification-id', records[1].id);
    await page.getByLabel('Search', { exact: true }).press('Escape');
    expect(commands).toHaveLength(0);
  });

  test('no selection leaves no notification evidence on the map', async ({ page }) => {
    await page.route('**/api/notifications/stream', route => route.fulfill({ body: '', contentType: 'text/event-stream' }));
    await page.route(/\/api\/notifications(?:\?.*)?$/, route => route.fulfill({ json: { notifications: [], next_cursor: null } }));
    await openMap(page);
    await expect(page.locator('.geographicPinPosition,.geographicCompositionLayer polyline')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'First switch', exact: true })).toBeVisible();
  });

  test('recenter appears after manual movement and realtime updates preserve the view', async ({ page, request }) => {
    await openMap(page);
    const map = page.locator('.geographicLeaflet');
    const recenter = page.getByRole('button', { name: 'Recenter', exact: true });
    await expect(recenter).toHaveCount(0);
    await page.setViewportSize({ width: 1280, height: 900 });
    await expect(recenter).toHaveCount(0);
    const initial = await page.getByRole('button', { name: 'First switch', exact: true }).boundingBox();
    await map.press('ArrowRight');
    await expect(recenter).toBeVisible();
    const manuallyMoved = await page.getByRole('button', { name: 'First switch', exact: true }).boundingBox();
    await request.post(`${backend}/api/__geographic_fixture/notification`, { data: { trail: [{ x: 3, z: 3 }, { x: 9, z: 8 }] } });
    await expect.poll(async () => {
      const after = await page.getByRole('button', { name: 'First switch', exact: true }).boundingBox();
      return Math.abs(after.x - manuallyMoved.x) + Math.abs(after.y - manuallyMoved.y);
    }).toBeLessThan(2);
    await expect(recenter).toBeVisible();
    await recenter.click();
    await expect(recenter).toHaveCount(0);
    await expect(page.locator('[data-notification-id]')).toHaveCount(1);
    await zoomOut(page);
    await expect(recenter).toBeVisible();
    await screenshot(page, 'map-recenter.png');
    await recenter.click();
    await expect(recenter).toHaveCount(0);
    expect(initial).not.toEqual(manuallyMoved);
    expect(commands).toHaveLength(0);
  });

  test('the composition selector centers the chosen composition and explains a missing position', async ({ page, request }) => {
    await openMap(page);
    const initial = await page.getByRole('button', { name: 'First switch', exact: true }).boundingBox();
    await page.getByRole('button', { name: 'Composition: First', exact: true }).click();
    await page.locator('.compositionSelectButton').filter({ hasText: 'Second' }).click();
    await expect(page.getByRole('button', { name: 'Composition: Second', exact: true })).toBeVisible();
    await expect(page.locator('[data-composition-id="geographic-second"].isSelected')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Second switch', exact: true })).toBeInViewport();
    const second = await page.getByRole('button', { name: 'Second switch', exact: true }).boundingBox();
    expect(Math.abs(second.x - initial.x)).toBeLessThan(200);
    expect((await (await request.get(`${backend}/api/composition`)).json()).id).toBe('geographic-second');
    await page.getByRole('button', { name: 'Composition: Second', exact: true }).click();
    await page.locator('.compositionSelectButton').filter({ hasText: 'First' }).click();
    await expect(page.getByRole('button', { name: 'First switch', exact: true })).toBeInViewport();
    await page.getByRole('button', { name: 'Composition: First', exact: true }).click();
    await page.locator('.compositionSelectButton').filter({ hasText: 'Unpositioned' }).click();
    await expect(page.getByRole('dialog').getByText(/Unpositioned has no geographic position/)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Position on map: Unpositioned', exact: true })).toBeVisible();
    expect((await (await request.get(`${backend}/api/composition`)).json()).id).toBe('geographic-first');
    expect(commands).toHaveLength(0);
  });
});
