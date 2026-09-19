const { test, expect } = require("@playwright/test");
const resource = "/api/cameras/cameras/source-panorama-synthetic-camera/sources/synthetic-wide/panorama";
const originalCrop = { u_start: 0.1, u_width: 0.8, v_start: 0.1, v_height: 0.8 };
let writes;

test.beforeEach(async ({ page, request }) => {
  await request.post("http://127.0.0.1:8108/api/__source_panorama_fixture/reset");
  const source = await (await request.get(`http://127.0.0.1:8108${resource}`)).json();
  writes = [];
  await page.addInitScript(() => { localStorage.setItem("toposync.locale", "en"); });
  const artifact = {
    id: "a".repeat(32), revision: 1, status: "ready", created_at: Date.now() / 1000,
    width: 1600, height: 800, image_url: "/api/cameras/panorama-artifacts/" + "a".repeat(32) + "/files/panorama",
    crop: originalCrop, crop_revision: 1, quality: { status: "ready", reasons: [] }, quality_approved: true,
    presentation: { status: "verified" }, coverage: { acquisition_complete: true }, stale: false,
  };
  await page.route(`**${resource}`, route => route.fulfill({ json: { ...source, active: artifact, candidate: null, job: null } }));
  await page.route(`**${resource}/**`, route => { writes.push(route.request().postDataJSON()); return route.fulfill({ status: 409, json: { detail: { code: "test_no_write" } } }); });
  await page.route(`**${artifact.image_url}`, route => route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="800"><rect width="1600" height="800" fill="#345"/><path d="M0 400H1600M800 0V800" stroke="white" stroke-width="4"/></svg>' }));
  await page.goto("/settings");
  await page.getByRole("button", { name: /^Cameras\b/ }).click();
  await expect(page.locator(".sourcePanoramaPreviewViewport")).toBeVisible();
});

async function geometry(viewport, screen) {
  return viewport.evaluate((element, screen) => {
    const outer = element.getBoundingClientRect();
    const plane = element.querySelector("[data-viewport-content]").getBoundingClientRect();
    const x = screen?.x ?? outer.left + outer.width / 2;
    const y = screen?.y ?? outer.top + outer.height / 2;
    return { x: (x - plane.left) / plane.width, y: (y - plane.top) / plane.height, width: plane.width, height: plane.height, left: plane.left, top: plane.top };
  }, screen);
}
async function drag(page, start, end, button = "left") {
  await page.mouse.move(start.x, start.y); await page.mouse.down({ button });
  await page.mouse.move(end.x, end.y, { steps: 8 }); await page.mouse.up({ button });
}

test("shared viewport anchors wheel zoom, pans, resizes and supports keyboard without saving", async ({ page }) => {
  const viewport = page.locator(".sourcePanoramaPreviewViewport");
  await viewport.scrollIntoViewIfNeeded();
  const bounds = await viewport.boundingBox();
  const anchor = { x: Math.round(bounds.x + bounds.width * 0.6), y: Math.round(bounds.y + bounds.height * 0.45) };
  const before = await geometry(viewport, anchor);
  const scroll = await page.evaluate(() => [...document.querySelectorAll("*")].filter(element => element.scrollTop).map(element => element.scrollTop));
  await page.mouse.move(anchor.x, anchor.y); await page.mouse.wheel(0, -210);
  await expect.poll(async () => (await geometry(viewport)).width).toBeGreaterThan(before.width * 1.3);
  const zoomed = await geometry(viewport, anchor);
  expect(zoomed.x).toBeCloseTo(before.x, 5); expect(zoomed.y).toBeCloseTo(before.y, 5);
  expect(await page.evaluate(() => [...document.querySelectorAll("*")].filter(element => element.scrollTop).map(element => element.scrollTop))).toEqual(scroll);
  await drag(page, anchor, { x: anchor.x + 70, y: anchor.y + 25 });
  const panned = await geometry(viewport);
  expect(panned.left - zoomed.left).toBeCloseTo(70, 0); expect(panned.top - zoomed.top).toBeCloseTo(25, 0);
  for (const width of [1150, 375]) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(async () => Math.abs((await geometry(viewport)).x - panned.x)).toBeLessThan(0.001);
    const resized = await geometry(viewport);
    expect(resized.y).toBeCloseTo(panned.y, 4); expect(resized.width).toBeCloseTo(panned.width, 2);
    expect((await viewport.boundingBox()).width).toBeLessThanOrEqual(width);
  }
  const resized = await geometry(viewport);
  await viewport.focus(); await page.keyboard.press("+");
  await expect.poll(async () => (await geometry(viewport)).width).toBeGreaterThan(resized.width);
  await page.keyboard.press("0");
  await expect(viewport).toHaveAttribute("data-viewport-zoom", "1");
  expect(writes).toEqual([]);
});

test("crop tools retain canonical coordinates while navigating and cancel interrupted gestures", async ({ page }) => {
  await page.getByRole("button", { name: /Select useful area|Edit useful area/ }).click();
  const editor = page.getByTestId("panorama-crop-editor");
  const viewport = editor.locator(".sourcePanoramaCropViewport");
  const stage = editor.locator(".sourcePanoramaCropStage");
  const beforeLabel = await stage.getAttribute("aria-label");
  const selectionGeometry = () => editor.locator(".sourcePanoramaSelection").evaluateAll(elements => elements.map(element => ({ left: element.style.left, top: element.style.top, width: element.style.width, height: element.style.height })));
  const originalGeometry = await selectionGeometry();
  await editor.getByRole("button", { name: "Move view", exact: true }).click();
  await viewport.scrollIntoViewIfNeeded();
  const rectangle = await viewport.boundingBox();
  const center = { x: rectangle.x + rectangle.width / 2, y: rectangle.y + rectangle.height / 2 };
  await page.mouse.move(center.x, center.y); await page.mouse.wheel(0, -210);
  await expect.poll(async () => Number(await viewport.getAttribute("data-viewport-zoom"))).toBeGreaterThan(1.3);
  await drag(page, center, { x: center.x + 45, y: center.y - 20 });
  await expect(stage).toHaveAttribute("aria-label", beforeLabel);
  expect(await selectionGeometry()).toEqual(originalGeometry);
  await editor.getByRole("button", { name: "Fit image", exact: true }).click();
  await editor.getByRole("button", { name: "Draw an area", exact: true }).click();
  const image = await stage.boundingBox();
  const start = { x: image.x + image.width * 0.25, y: image.y + image.height * 0.25 };
  await drag(page, start, { x: image.x + image.width * 0.75, y: image.y + image.height * 0.75 });
  await expect(stage).toHaveAttribute("aria-label", /50% of image width and 50% of image height/);
  const handle = editor.getByRole("button", { name: "Resize right edge", exact: true });
  const hitBox = await handle.boundingBox();
  expect(hitBox.width).toBeCloseTo(44, 1);
  await editor.getByRole("button", { name: "Magnify image", exact: true }).click();
  expect((await handle.boundingBox()).width).toBeCloseTo(44, 1);
  // Middle-button navigation must not resize the underlying selection.
  const cropLabel = await stage.getAttribute("aria-label");
  const cropGeometry = await selectionGeometry();
  await drag(page, center, { x: center.x - 40, y: center.y + 20 }, "middle");
  await expect(stage).toHaveAttribute("aria-label", cropLabel);
  expect(await selectionGeometry()).toEqual(cropGeometry);
  // A real two-finger browser gesture cancels the first editing touch.
  const session = await page.context().newCDPSession(page);
  const touch = (x, y, id) => ({ x, y, id });
  await session.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [touch(center.x - 30, center.y, 1)] });
  await session.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [touch(center.x - 20, center.y + 10, 1)] });
  await session.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [touch(center.x - 20, center.y + 10, 1), touch(center.x + 30, center.y, 2)] });
  const widthBeforePinch = (await geometry(viewport)).width;
  await session.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [touch(center.x - 80, center.y + 10, 1), touch(center.x + 90, center.y, 2)] });
  await session.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await expect.poll(async () => (await geometry(viewport)).width).toBeGreaterThan(widthBeforePinch * 1.5);
  await expect(stage).toHaveAttribute("aria-label", cropLabel);
  await session.detach();
  expect(await selectionGeometry()).toEqual(cropGeometry);
  expect(writes).toEqual([]);
  await editor.getByRole("button", { name: "Save useful area", exact: true }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].artifact_id).toBe("a".repeat(32));
  for (const [key, expected] of Object.entries({ u_start: 0.25, v_start: 0.25, u_width: 0.5, v_height: 0.5 })) expect(writes[0].crop[key]).toBeCloseTo(expected, 2);
});
