const { test, expect } = require("@playwright/test");
const fs = require("node:fs/promises");
const path = require("node:path");
const { resolveToposyncUrl, requestJson } = require("../frontend/packages/plugin-api/basePath");

const backend = "http://127.0.0.1:8127";
const cameraId = "panorama-simulated-camera";
const elementId = "panorama-simulated-element";
const panoramaPath = `/api/cameras/cameras/${cameraId}/panorama`;
const evidenceDirectory = process.env.TOPOSYNC_PANORAMA_EVIDENCE_DIR || path.resolve(__dirname, "../.toposync-data/panorama-validation/evidence");

async function screenshot(page, name) {
  await fs.mkdir(evidenceDirectory, { recursive: true });
  await page.screenshot({ path: path.join(evidenceDirectory, `${name}.png`), fullPage: true });
}

async function checkLayout(wizard, width) {
  const bounds = await wizard.locator("xpath=ancestor::*[@role='dialog']").evaluate(element => ({ width: element.getBoundingClientRect().width, overflow: element.scrollWidth - element.clientWidth }));
  expect(bounds.width).toBeLessThanOrEqual(width);
  expect(bounds.overflow).toBeLessThanOrEqual(1);
  const undersized = await wizard.locator("button:visible, input:visible, select:visible").evaluateAll(elements => elements.map(element => ({ label: element.getAttribute("aria-label") || element.textContent || element.id, rectangle: element.getBoundingClientRect() })).filter(({ rectangle }) => rectangle.height < 43.9 || rectangle.width < 43.9).map(({ label }) => label));
  expect(undersized).toEqual([]);
}

async function panoramaCenter(wizard) {
  return wizard.getByRole("group", { name: /^Panorama: use arrow keys/ }).evaluate(viewport => {
    const image = viewport.querySelector("img").getBoundingClientRect();
    const bounds = viewport.getBoundingClientRect();
    return { x: (bounds.left + viewport.clientWidth / 2 - image.left) / image.width, y: (bounds.top + viewport.clientHeight / 2 - image.top) / image.height };
  });
}

async function fixture(request) {
  const response = await request.get(`${backend}/api/__panorama_fixture`);
  expect(response.ok()).toBeTruthy();
  const data = await response.json();
  expect(data.synthetic).toBe(true);
  return data;
}

async function currentJob(request) {
  const response = await request.get(`${backend}${panoramaPath}?element_id=${elementId}`);
  expect(response.ok()).toBeTruthy();
  return (await response.json()).job;
}

async function openCameraEditor(page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  const tools = page.getByRole("button", { name: /^Tools and layers:/ });
  if (await tools.getAttribute("aria-expanded") === "false") await tools.click();
  await page.getByText("Câmera simulada", { exact: true }).dblclick();
}

async function openWizard(page) {
  await openCameraEditor(page);
  await page.getByRole("button", { name: /^(Map in composition|Continue mapping|View mapping)$/, exact: true }).click();
  const wizard = page.getByTestId("camera-panorama-mapping");
  await expect(wizard).toBeVisible();
  await expect(wizard.locator(".cameraPanoramaViews")).toHaveCount(1);
  if (await wizard.locator(".cameraPanoramaPairs").count() && await wizard.locator(".cameraPanoramaPair").count() === 0) await wizard.getByRole("button", {name: "Add point", exact: true}).click();
  return wizard;
}

async function seedLegacyPanorama(request, { startCapture = true } = {}) {
  const known = await fixture(request);
  // Prepare an old record through the real API and synthetic camera boundary.
  // New UI users never enter optical parameters or invoke this legacy endpoint.
  const created = await request.post(`${backend}${panoramaPath}`, { data: {
    element_id: elementId,
    source_id: "synthetic-wide",
    profile: known.profile,
    scan: {
      pan_min: known.profile.pan_axis.position_min,
      pan_max: known.profile.pan_axis.position_max,
      tilt_min: known.profile.tilt_axis.position_min,
      tilt_max: known.profile.tilt_axis.position_max,
      overlap: 0.4,
    },
  } });
  expect(created.ok()).toBeTruthy();
  const draft = await created.json();
  if (!startCapture) return draft;
  const started = await request.post(`${backend}${panoramaPath}/${draft.id}/capture`, { data: { revision: draft.revision } });
  expect(started.ok()).toBeTruthy();
  await expect.poll(async () => (await currentJob(request)).state, { timeout: 30_000 }).toBe("ready");
  return currentJob(request);
}

async function reviewLegacyPanorama(page, request, evidencePrefix = "recovery") {
  await seedLegacyPanorama(request);
  const wizard = await openWizard(page);
  await expect(wizard.locator("#panorama-fx, #panorama-scan-pan_min, #panorama-source")).toHaveCount(0);
  await expect(wizard.getByRole("button", { name: "Create panorama", exact: true })).toHaveCount(0);
  await expect(wizard.getByRole("img", { name: "Captured camera panorama", exact: true })).toBeVisible();
  await screenshot(page, `${evidencePrefix}-02-panorama-1440-day`);
  return wizard;
}

const draftKey = `toposync.panorama-point.${cameraId}.${elementId}`;
const panoramaPanel = wizard => wizard.getByRole("group", { name: /^Panorama: use arrow keys/ });
const planPanel = wizard => wizard.getByRole("group", { name: /^Composition: use arrow keys/ });
const saveButton = wizard => wizard.getByRole("button", { name: "Confirm pair", exact: true });
const pointNavigation = wizard => wizard.getByRole("navigation", { name: "Places", exact: true });
const pointButton = (wizard, number, role = "fit") => pointNavigation(wizard).getByRole("button", { name: `Adjust place ${number}`, exact: true });

async function browserDraft(page) {
  return page.evaluate(key => JSON.parse(localStorage.getItem(key)), draftKey);
}

async function openPointDetails(wizard) {
  const details = wizard.locator("details").filter({ hasText: "Help with marking and precision" });
  if ((await details.getAttribute("open")) === null) await details.locator("summary").click();
}

async function focusCamera(wizard) {
  await expect(planPanel(wizard)).toBeVisible();
  const whole = wizard.getByRole("button", { name: "Whole composition", exact: true });
  if (await whole.isVisible()) await whole.click();
  const focus = wizard.getByRole("button", { name: "Focus camera", exact: true });
  if (await focus.isVisible()) {
    await focus.click();
    const canvas = planPanel(wizard).locator("canvas");
    const bounds = await canvas.boundingBox();
    const page = wizard.page();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    // Zoom for precise marking, then pan to the middle of the synthetic floor.
    const initialScale = Math.max(4, (Math.min(bounds.width, bounds.height) - 32) / 24);
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.mouse.wheel(0, -420 * Math.log2(34 / initialScale));
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.mouse.down();
    await page.mouse.move(bounds.x + bounds.width / 2 - 170, bounds.y + bounds.height / 2, { steps: 6 });
    await page.mouse.up();
  }
  await expect(wizard.getByRole("button", { name: "Whole composition", exact: true })).toBeVisible();
}

async function planPosition(wizard, world, rotation = 0) {
  // The visible pan gesture centres the synthetic floor after fitting the camera surroundings.
  const bounds = await planPanel(wizard).locator("canvas").boundingBox();
  const scale = 34;
  const dx = (world.x - 5) * scale;
  const dz = world.z * scale;
  const [x, y] = rotation === 90 ? [-dz, dx] : rotation === 180 ? [-dx, -dz] : rotation === 270 ? [dz, -dx] : [dx, dz];
  const position = { x: bounds.width / 2 + x, y: bounds.height / 2 + y };
  expect(position.x).toBeGreaterThan(0);
  expect(position.x).toBeLessThan(bounds.width);
  expect(position.y).toBeGreaterThan(0);
  expect(position.y).toBeLessThan(bounds.height);
  return position;
}

async function markPlan(wizard, world) {
  await planPanel(wizard).locator("canvas").click({ position: await planPosition(wizard, world) });
}

async function imagePosition(wizard, point) {
  const image = wizard.getByRole("img", { name: "Captured camera panorama", exact: true });
  const bounds = await image.boundingBox();
  return { x: bounds.width * point.x, y: bounds.height * point.y };
}

async function markImage(wizard, point) {
  const image = wizard.getByRole("img", { name: "Captured camera panorama", exact: true });
  const page = wizard.page();
  // Navigate through the same gestures as a user. CSS transforms no longer use scrollbars,
  // and fixed-size marker targets require more magnification for neighbouring places.
  for (let attempt = 0; attempt < 8; attempt++) {
    const bounds = await image.boundingBox();
    const panel = await panoramaPanel(wizard).boundingBox();
    const target = { x: bounds.x + point.x * bounds.width, y: bounds.y + point.y * bounds.height };
    const center = { x: panel.x + panel.width / 2, y: panel.y + panel.height / 2 };
    if (target.x < panel.x + 30 || target.x > panel.x + panel.width - 30 || target.y < panel.y + 30 || target.y > panel.y + panel.height - 30) {
      await page.mouse.move(center.x, center.y);
      await page.mouse.down({ button: "middle" });
      await page.mouse.move(center.x + Math.max(-300, Math.min(300, center.x - target.x)), center.y + Math.max(-200, Math.min(200, center.y - target.y)), { steps: 5 });
      await page.mouse.up({ button: "middle" });
      continue;
    }
    const obscured = await wizard.locator("button.cameraPanoramaMarker").evaluateAll((markers, target) => markers.some(marker => {
      const bounds = marker.getBoundingClientRect();
      return target.x >= bounds.left && target.x <= bounds.right && target.y >= bounds.top && target.y <= bounds.bottom;
    }), target);
    if (!obscured) {
      await image.click({ position: await imagePosition(wizard, point) });
      return;
    }
    const before = bounds.width;
    await page.mouse.move(target.x, target.y);
    await page.mouse.wheel(0, -210);
    await expect.poll(async () => (await image.boundingBox()).width).toBeGreaterThan(before);
  }
  throw new Error("The target place could not be exposed through viewport navigation");
}

async function enterPoint(wizard, point, firstPanel = "panorama") {
  if (await pointNavigation(wizard).locator("button[aria-pressed=true]").count() === 0) {
    await wizard.getByRole("button", { name: "Add point", exact: true }).click();
  }
  if (firstPanel === "plan") {
    await markPlan(wizard, point.world);
    await expect(saveButton(wizard)).toBeDisabled();
    await markImage(wizard, point.panorama);
  } else {
    await markImage(wizard, point.panorama);
    await expect(saveButton(wizard)).toBeDisabled();
    await markPlan(wizard, point.world);
  }
  await expect(saveButton(wizard)).toBeEnabled();
}

async function savePoint(page, wizard) {
  const saved = page.waitForResponse(response => response.url().endsWith("/points") && response.request().method() === "PUT");
  await saveButton(wizard).click();
  expect((await saved).ok()).toBeTruthy();
}

async function seedPoints(request, points) {
  const job = await currentJob(request);
  const saved = await request.put(`${backend}${panoramaPath}/${job.id}/points`, { data: { revision: job.revision, points } });
  expect(saved.ok()).toBeTruthy();
  return saved.json();
}

test.beforeEach(async ({ page, request }) => {
  const reset = await request.post(`${backend}/api/__panorama_fixture/reset`);
  expect(reset.ok()).toBeTruthy();
  await page.addInitScript(() => {
    if (!localStorage.getItem("toposync.locale")) localStorage.setItem("toposync.locale", "en");
    if (!localStorage.getItem("toposync.theme")) localStorage.setItem("toposync.theme", "topo-day");
  });
});

for (const [input, width] of [["keyboard", 1280], ["touch", 390]]) {
  test.describe(`camera editing with ${input}`, () => {
    test.use({ viewport: { width, height: 900 }, hasTouch: input === "touch" });
    test("opens panorama mapping without double click and restores focus", async ({ page, request }, testInfo) => {
      const activate = async locator => {
        if (input === "touch") return locator.tap();
        for (let count = 0; count < 120; count++) {
          if (await locator.evaluate(element => element === document.activeElement)) {
            await page.keyboard.press("Enter");
            return;
          }
          await page.keyboard.press("Tab");
        }
        throw new Error(`Keyboard could not reach ${await locator.textContent()}`);
      };
      await page.goto("/");
      await activate(page.getByRole("button", { name: "Edit", exact: true }));
      const controls = page.getByRole("button", { name: /^Tools and layers:/ });
      if (await controls.getAttribute("aria-expanded") === "false") await activate(controls);
      const edit = page.getByRole("button", { name: "Edit Câmera simulada", exact: true });
      await expect(edit).toHaveCount(0);
      await activate(page.getByRole("button", { name: /^Câmera simulada/ }));
      await expect(edit).toBeVisible();
      expect(await edit.evaluate(element => {
        const button = element.getBoundingClientRect();
        const panel = element.closest(".overlayLeft").getBoundingClientRect();
        return button.height >= 44 && button.left >= panel.left && button.right <= panel.right;
      })).toBe(true);
      await page.screenshot({ path: testInfo.outputPath("selected-layer.png") });
      await activate(edit);
      const editor = page.getByRole("dialog", { name: "Edit: Câmera simulada", exact: true });
      await expect(editor).toBeVisible();
      await expect(editor.getByRole("button", { name: "Calibrate ground and metric geometry", exact: true })).toHaveCount(0);
      const calibrate = editor.getByRole("button", { name: "Map in composition", exact: true });
      const target = await calibrate.boundingBox();
      expect(target.height).toBeGreaterThanOrEqual(44);
      expect(await editor.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
      await page.screenshot({ path: testInfo.outputPath("camera-editor.png") });
      await activate(calibrate);
      const wizard = page.getByTestId("camera-panorama-mapping");
      await expect(wizard).toBeVisible();
      await expect.poll(() => wizard.locator("img").evaluateAll(images => images.some(image => image.complete && image.naturalWidth === 1024))).toBe(true);
      await expect(wizard.getByRole("button", {name: "Use this mapping", exact: true})).toBeDisabled();
      await page.screenshot({ path: testInfo.outputPath("mapping-entry.png") });
      await page.keyboard.press("Escape");
      await expect(calibrate).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(edit).toBeFocused();
      expect((await fixture(request)).commands).toEqual([]);
    });
  });
}

test("panorama mapping keeps paired image markers aligned across responsive layouts", async ({ page, request }) => {
  const known = await fixture(request);
  const wizard = await openWizard(page);
  const point = known.points[0].panorama;
  await markImage(wizard, point);
  const marked = (await browserDraft(page)).drafts[0].panorama;
  const image = wizard.getByRole("img", { name: "Captured camera panorama", exact: true });
  const marker = wizard.locator("span.cameraPanoramaMarker.isPending");
  await expect(marker).toBeVisible();
  for (const [width, height] of [[1440, 900], [1280, 720], [768, 900], [390, 844], [320, 844]]) {
    await page.setViewportSize({ width, height });
    await image.scrollIntoViewIfNeeded();
    await expect.poll(async () => {
      const bounds = await image.boundingBox();
      const actual = await marker.boundingBox();
      return Math.hypot(actual.x + actual.width / 2 - (bounds.x + bounds.width * marked.x), actual.y + actual.height / 2 - (bounds.y + bounds.height * marked.y));
    }).toBeLessThan(1);
    await checkLayout(wizard, width);
    const primary = await wizard.locator(".cameraPanoramaFooter").boundingBox();
    expect(primary.y).toBeGreaterThanOrEqual(0);
    expect(primary.y + primary.height).toBeLessThanOrEqual(height);
    const instruction = await wizard.locator(".cameraPanoramaInstruction").boundingBox();
    const content = await wizard.locator(".cameraPanoramaContent").boundingBox();
    expect(instruction.y).toBeGreaterThan(0);
    expect(instruction.y + instruction.height).toBeLessThanOrEqual(content.y + 1);
    expect(content.y + content.height).toBeLessThanOrEqual(primary.y + 1);
    expect((await browserDraft(page)).drafts[0].panorama).toEqual(marked);
    await screenshot(page, `mapping-responsive-${width}`);
  }
  expect((await fixture(request)).commands).toEqual([]);
});

test("four controls, visual acceptance, reopen, responsive navigation and source invalidation", async ({ page, request }) => {
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const known = await fixture(request);
  let wizard = await reviewLegacyPanorama(page, request, "flow");
  const compositionWrites = [];
  page.on("request", request => { if (new URL(request.url()).pathname === "/api/composition" && request.method() === "PUT") compositionWrites.push(request.postData()); });
  const captureCommands = (await fixture(request)).commands.length;
  expect(captureCommands).toBeGreaterThan(1);
  await expect(wizard.locator("input[type='number'], select")).toHaveCount(0);
  await expect(pointNavigation(wizard)).toBeVisible();
  await focusCamera(wizard);
  await wizard.getByRole("button", { name: "Enlarge image", exact: true }).click();
  await wizard.getByRole("button", { name: "Enlarge image", exact: true }).click();
  for (const [index, point] of known.points.slice(0, 4).entries()) {
    await enterPoint(wizard, point, index % 2 ? "plan" : "panorama");
    await savePoint(page, wizard);
  }
  await screenshot(page, "flow-03-four-linked");
  expect((await fixture(request)).commands).toHaveLength(captureCommands);
  await expect(wizard.getByRole("heading", { name: "Explore the current mapping", exact: true })).toBeVisible();
  expect((await currentJob(request)).solution.preview.eligible).toBe(true);
  expect(compositionWrites).toEqual([]);
  await wizard.getByRole("button", { name: "Use this mapping", exact: true }).click();
  await expect(wizard.getByRole("heading", { name: "Your camera is mapped", exact: true })).toBeVisible();
  expect((await currentJob(request)).active).toBe(true);
  const activeMarkers = wizard.locator('.cameraPanoramaImageContent span.cameraPanoramaMarker');
  await expect(activeMarkers).toHaveCount(4);
  expect(await activeMarkers.first().evaluate(element => ({opacity:getComputedStyle(element).opacity,filter:getComputedStyle(element).filter}))).toEqual({opacity:'1',filter:'none'});
  expect((await fixture(request)).commands).toHaveLength(captureCommands);

  // Visual evidence, not golden snapshots: exercise actual saved theme settings.
  for (const theme of ["day", "night"]) {
    if (theme === "night") {
      await page.evaluate(() => localStorage.setItem("toposync.theme", "topo-night"));
      wizard = await openWizard(page);
      await expect(wizard.getByRole("heading", { name: "Your camera is mapped", exact: true })).toBeVisible();
    }
    for (const width of [1440, 768, 375]) {
      await page.setViewportSize({ width, height: width === 375 ? 844 : 1000 });
      await checkLayout(wizard, width);
      await screenshot(page, `flow-05-active-${width}-${theme}`);
    }
  }

  // A viewport resize must preserve the user's magnification and visible ray.
  const panoramaView = wizard.locator(".cameraPanoramaView").first();
  const magnification = panoramaView.locator(".cameraPanoramaViewHeader .cardMeta");
  const initialMagnification = await magnification.innerText();
  await wizard.getByRole("button", { name: "Enlarge image", exact: true }).click();
  await expect(magnification).not.toHaveText(initialMagnification);
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const retainedImageWidth = (await wizard.getByRole("img", { name: "Captured camera panorama", exact: true }).boundingBox()).width;
  const retainedCenter = await panoramaCenter(wizard);
  for (const width of [1440, 375]) {
    await page.setViewportSize({ width, height: width === 375 ? 844 : 1000 });
    await expect.poll(async () => {
      const center = await panoramaCenter(wizard);
      return Math.max(Math.abs(center.x - retainedCenter.x), Math.abs(center.y - retainedCenter.y));
    }).toBeLessThanOrEqual(0.01);
    // Resize keeps absolute magnification; the percentage relative to fitting the new panel can change.
    expect((await wizard.getByRole("img", { name: "Captured camera panorama", exact: true }).boundingBox()).width).toBeCloseTo(retainedImageWidth, 1);
    await screenshot(page, `flow-06-retained-center-${width}-night`);
  }

  const getComposition = await request.get(`${backend}/api/composition`);
  const changed = await getComposition.json();
  changed.elements.find(element => element.id === elementId).position.y += 1;
  expect((await request.put(`${backend}/api/composition`, { data: changed })).ok()).toBeTruthy();
  const count = (await fixture(request)).commands.length;
  const rejected = await request.post(`${backend}${panoramaPath}/aim`, { data: { element_id: elementId, world: { x: 5, z: 0 } } });
  expect(rejected.status()).toBe(409);
  expect((await rejected.json()).detail.code).toBe("composition_changed");
  expect((await fixture(request)).commands).toHaveLength(count);
  expect(errors).toEqual([]);
});

test("point navigation preserves identifiers, corrections, undo and multiple unfinished pairs", async ({ page, request }) => {
  const known = await fixture(request);
  let wizard = await reviewLegacyPanorama(page, request);
  const commandCount = (await fixture(request)).commands.length;
  await focusCamera(wizard);
  await enterPoint(wizard, known.points[0], "plan");
  await savePoint(page, wizard);
  const savedPoint = (await currentJob(request)).points[0];
  await pointButton(wizard, 1).click();
  await markPlan(wizard, { x: 3.2, z: -3 });
  await savePoint(page, wizard);
  expect((await currentJob(request)).points[0].id).toBe(savedPoint.id);
  expect((await currentJob(request)).points[0].world.x).toBeCloseTo(3.2, 1);
  await openPointDetails(wizard);
  await wizard.getByRole("button", { name: "Undo last change", exact: true }).click();
  await expect.poll(async () => (await currentJob(request)).points[0].world.x).toBeCloseTo(savedPoint.world.x, 5);
  await pointButton(wizard, 1).click();
  await wizard.getByRole("button", { name: "Remove place 1", exact: true }).click();
  await expect.poll(async () => (await currentJob(request)).points.length).toBe(0);
  await openPointDetails(wizard);
  await wizard.getByRole("button", { name: "Undo last change", exact: true }).click();
  await expect.poll(async () => (await currentJob(request)).points[0]?.id).toBe(savedPoint.id);

  await wizard.getByRole("button", {name: "Add point", exact: true}).click();
  await markPlan(wizard, known.points[1].world);
  const secondId = (await browserDraft(page)).selectedPointId;
  await wizard.getByRole("button", { name: "Add point", exact: true }).click();
  await markImage(wizard, known.points[2].panorama);
  const thirdId = (await browserDraft(page)).selectedPointId;
  expect(thirdId).not.toBe(secondId);
  await wizard.getByRole("button", { name: "Previous point", exact: true }).click();
  await expect(pointButton(wizard, 2)).toHaveAttribute("aria-pressed", "true");
  await expect(saveButton(wizard)).toBeDisabled();
  await wizard.getByRole("button", { name: "Next point", exact: true }).click();
  await expect(pointButton(wizard, 3)).toHaveAttribute("aria-pressed", "true");
  await expect(saveButton(wizard)).toBeDisabled();
  // Selecting an image marker opens its saved pair without overwriting either unfinished pair.
  const savedMarker = await wizard.locator(".cameraPanoramaImageContent").getByRole("button", { name: "Adjust place 1", exact: true }).boundingBox();
  await page.mouse.click(savedMarker.x + savedMarker.width / 2, savedMarker.y + savedMarker.height / 2);
  await expect(pointButton(wizard, 1)).toHaveAttribute("aria-pressed", "true");
  await pointButton(wizard, 2).click();
  await page.reload();
  wizard = await openWizard(page);
  await expect(wizard.getByText("Your unfinished marks were restored. Continue where you left off.", { exact: true })).toBeVisible();
  await expect(pointButton(wizard, 2)).toHaveAttribute("aria-pressed", "true");
  const restored = await browserDraft(page);
  expect(restored.selectedPointId).toBe(secondId);
  expect(restored.drafts.find(point => point.id === secondId).panorama).toBeNull();
  expect(restored.drafts.find(point => point.id === thirdId).world).toBeNull();
  await markImage(wizard, known.points[1].panorama);
  await savePoint(page, wizard);
  expect((await currentJob(request)).points[1].id).toBe(secondId);
  await pointButton(wizard, 3).click();
  await expect(pointButton(wizard, 3)).toHaveAttribute("aria-pressed", "true");
  await expect(saveButton(wizard)).toBeDisabled();
  await focusCamera(wizard);
  await markPlan(wizard, known.points[2].world);
  await savePoint(page, wizard);
  expect((await currentJob(request)).points[2].id).toBe(thirdId);
  expect((await fixture(request)).commands).toHaveLength(commandCount);
});

test("keyboard works from either panel and a failed save preserves the pair for retry", async ({ page, request }) => {
  const wizard = await reviewLegacyPanorama(page, request, "keyboard");
  const commandCount = (await fixture(request)).commands.length;
  const compositionBefore = await (await request.get(`${backend}/api/composition`)).json();
  const compositionWrites = [];
  page.on("request", request => { if (new URL(request.url()).pathname === "/api/composition" && request.method() === "PUT") compositionWrites.push(request.postData()); });
  const plan = planPanel(wizard);
  await plan.focus();
  await plan.press("Shift+ArrowRight");
  await plan.press("Enter");
  await expect(saveButton(wizard)).toBeDisabled();
  const panorama = panoramaPanel(wizard);
  await panorama.focus();
  await panorama.press("ArrowRight");
  await panorama.press("ArrowDown");
  await panorama.press("Enter");
  await expect(saveButton(wizard)).toBeEnabled();
  const pending = (await browserDraft(page)).drafts[0];
  expect(pending.panorama.x).toBeGreaterThan(0);
  expect(pending.panorama.x).toBeLessThan(1);
  expect(pending.panorama.y).toBeGreaterThan(0);
  expect(pending.panorama.y).toBeLessThan(1);
  expect(pending.world).not.toBeNull();
  expect((await (await request.get(`${backend}/api/composition`)).json()).elements).toEqual(compositionBefore.elements);
  await page.route("**/panorama/*/points", route => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Synthetic save failure" }) }));
  await saveButton(wizard).click();
  await expect(wizard.getByRole("alert")).toContainText("Synthetic save failure");
  expect((await currentJob(request)).points).toHaveLength(0);
  expect((await browserDraft(page)).drafts[0]).toEqual(pending);
  await expect(saveButton(wizard)).toBeEnabled();
  await screenshot(page, "keyboard-02-save-failed-1440-day");
  await page.unroute("**/panorama/*/points");
  await savePoint(page, wizard);
  expect((await currentJob(request)).points[0]).toMatchObject(pending);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await wizard.getByRole("button", {name: "Add point", exact: true}).click();
  await expect(pointButton(wizard, 2)).toHaveAttribute("aria-pressed", "true");
  await checkLayout(wizard, 390);
  await screenshot(page, "keyboard-03-next-place-390-day");
  expect(compositionWrites).toEqual([]);
  expect((await fixture(request)).commands).toHaveLength(commandCount);
});

test("bidirectional provisional previews follow independent locations without saving or moving the camera", async ({ page, request }) => {
  const known = await fixture(request);
  await seedLegacyPanorama(request);
  const seeded = await seedPoints(request, known.points.slice(0, 6));
  expect(seeded.solution.preview.eligible).toBe(true);
  expect(seeded.solution.quality.status).toBe("ready");
  const wizard = await openWizard(page);
  await pointButton(wizard, 1).click();
  await focusCamera(wizard);
  const canvas = planPanel(wizard).locator("canvas");
  const ghost = wizard.getByTestId("panorama-ghost");
  const bounds = await canvas.boundingBox();
  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
  await page.mouse.wheel(0, -420);
  const hoverFloorReference = async (world, rotation = 0) => {
    const guess = await planPosition(wizard, world, rotation);
    const rectangle = await canvas.boundingBox();
    guess.x = rectangle.width / 2 + 2 * (guess.x - rectangle.width / 2);
    guess.y = rectangle.height / 2 + 2 * (guess.y - rectangle.height / 2);
    // Locate the independently rendered cyan floor reference instead of assuming an exact pan centre.
    const position = await canvas.evaluate((canvas, guess) => {
      const pixels = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
      const scale = canvas.width / canvas.clientWidth;
      const cyan = [];
      for (let index = 0; index < pixels.length; index += 4) {
        if (Math.abs(pixels[index] - 94) < 35 && Math.abs(pixels[index + 1] - 220) < 35 && Math.abs(pixels[index + 2] - 229) < 35) cyan.push({ x: (index / 4 % canvas.width + .5) / scale, y: (Math.floor(index / 4 / canvas.width) + .5) / scale });
      }
      if (!cyan.length) throw new Error("Independent floor reference is not visible");
      const nearest = cyan.reduce((best, pixel) => Math.hypot(pixel.x - guess.x, pixel.y - guess.y) < Math.hypot(best.x - guess.x, best.y - guess.y) ? pixel : best);
      const reference = cyan.filter(pixel => Math.hypot(pixel.x - nearest.x, pixel.y - nearest.y) < 30);
      return { x: reference.reduce((sum, pixel) => sum + pixel.x, 0) / reference.length, y: reference.reduce((sum, pixel) => sum + pixel.y, 0) / reference.length };
    }, guess);
    await canvas.hover({ position });
  };
  const originalDraft = await browserDraft(page);
  const commandCount = (await fixture(request)).commands.length;
  const requests = [];
  page.on("request", request => {
    if (request.url().includes(panoramaPath) && !request.url().includes("/images/")) requests.push(request.url());
  });
  // These independent points were deliberately excluded from the fitted model.
  for (const point of known.points.slice(6)) {
    await wizard.locator(".cameraPanoramaImageContent").hover({ position: await imagePosition(wizard, point.panorama) });
    await expect(canvas).toHaveAttribute("data-panorama-ghost-world", /./);
    const [x, z] = JSON.parse(await canvas.getAttribute("data-panorama-ghost-world"));
    expect(x).toBeCloseTo(point.world.x, 1);
    expect(z).toBeCloseTo(point.world.z, 1);
    await expect(ghost).toHaveCount(0);
    await hoverFloorReference(point.world);
    await expect(ghost).toBeVisible();
    await expect(canvas).not.toHaveAttribute("data-panorama-ghost-world", /./);
    const projected = await ghost.evaluate(marker => ({ x: Number.parseFloat(marker.style.left) / 100, y: Number.parseFloat(marker.style.top) / 100 }));
    expect(projected.x).toBeCloseTo(point.panorama.x, 3);
    expect(projected.y).toBeCloseTo(point.panorama.y, 3);
  }
  await screenshot(page, "preview-01-provisional-1440-day");
  for (const rotation of [90, 180, 270, 0]) {
    await wizard.getByRole("button", { name: "Rotate composition by 90 degrees", exact: true }).click();
    // Rotation redraws the canvas asynchronously; locate the reference again after each redraw.
    await expect.poll(async () => {
      await hoverFloorReference(known.points[6].world, rotation);
      if (!await ghost.count()) return Infinity;
      const projected = await ghost.evaluate(marker => ({ x: Number.parseFloat(marker.style.left) / 100, y: Number.parseFloat(marker.style.top) / 100 }));
      return Math.max(Math.abs(projected.x - known.points[6].panorama.x), Math.abs(projected.y - known.points[6].panorama.y));
    }).toBeLessThan(.0005);
  }
  await canvas.hover({ position: await planPosition(wizard, { x: -3, z: 0 }) });
  await expect(ghost).toBeVisible();
  await expect(wizard).toContainText("Outside the control region · extrapolated position");
  await screenshot(page, "preview-02-extrapolated-region-1440-day");
  await hoverFloorReference(known.points[6].world);
  await expect(ghost).toBeVisible();
  await pointNavigation(wizard).hover();
  await expect(ghost).toHaveCount(0);
  await wizard.locator(".cameraPanoramaImageContent").hover({ position: await imagePosition(wizard, known.points[6].panorama) });
  await expect(canvas).toHaveAttribute("data-panorama-ghost-world", /./);
  await page.mouse.wheel(0, 30);
  await expect(canvas).not.toHaveAttribute("data-panorama-ghost-world", /./);
  await wizard.locator(".cameraPanoramaImageContent").hover({ position: await imagePosition(wizard, known.points[6].panorama) });
  await expect(canvas).toHaveAttribute("data-panorama-ghost-world", /./);
  const imageBounds = await wizard.locator(".cameraPanoramaImageContent").boundingBox();
  const pointer = await imagePosition(wizard, known.points[6].panorama);
  await page.mouse.down();
  await expect(canvas).not.toHaveAttribute("data-panorama-ghost-world", /./);
  await page.mouse.move(imageBounds.x + pointer.x + 25, imageBounds.y + pointer.y, { steps: 5 });
  await page.mouse.up();
  await expect(canvas).not.toHaveAttribute("data-panorama-ghost-world", /./);
  expect((await browserDraft(page)).selectedPointId).toBe(originalDraft.selectedPointId);
  expect((await browserDraft(page)).drafts).toEqual(originalDraft.drafts);
  expect((await currentJob(request)).points).toEqual(seeded.points);
  expect((await fixture(request)).commands).toHaveLength(commandCount);
  expect(requests).toEqual([]);

  // The keyboard cursor uses the same projection and Escape only dismisses the preview.
  await planPanel(wizard).focus();
  await planPanel(wizard).press("Shift+ArrowRight");
  await expect(ghost).toBeVisible();
  await planPanel(wizard).press("Escape");
  await expect(ghost).toHaveCount(0);
  await expect(wizard).toBeVisible();
  await markPlan(wizard, { x: 3.2, z: -3 });
  await expect(wizard.getByTestId("panorama-preview-status")).not.toContainText("Updating");
  await wizard.locator(".cameraPanoramaImageContent").hover({ position: await imagePosition(wizard, known.points[6].panorama) });
  await expect(canvas).toHaveAttribute("data-panorama-ghost-world", /./);
  expect((await currentJob(request)).points).toEqual(seeded.points);
});

test("missing coverage and stale revision context never receive a suggestion", async ({ page, request }) => {
  const known = await fixture(request);
  await seedLegacyPanorama(request);
  await seedPoints(request, known.points);
  let wizard = await openWizard(page);
  await focusCamera(wizard);
  const commandCount = (await fixture(request)).commands.length;
  const job = await currentJob(request);
  const maskPath = new URL(job.coverage_url, backend).pathname;
  const maskRoute = url => url.pathname === maskPath;
  await page.route(maskRoute, route => route.abort());
  await pointButton(wizard, 1).click();
  await page.reload();
  wizard = await openWizard(page);
  await focusCamera(wizard);
  await wizard.locator(".cameraPanoramaImageContent").hover({ position: await imagePosition(wizard, known.points[6].panorama) });
  await expect(planPanel(wizard).locator("canvas")).not.toHaveAttribute("data-panorama-ghost-world", /./);
  await planPanel(wizard).locator("canvas").hover({ position: await planPosition(wizard, known.points[6].world) });
  await expect(wizard.getByTestId("panorama-ghost")).toHaveCount(0);
  await page.unroute(maskRoute);
  let staleResponses = 0;
  await page.route(url => url.pathname === panoramaPath, async route => {
    const response = await route.fetch();
    const body = await response.json();
    body.job.solution.preview.context.revision -= 1;
    staleResponses += 1;
    await route.fulfill({ response, json: body });
  });
  await page.reload();
  wizard = await openWizard(page);
  await focusCamera(wizard);
  expect(staleResponses).toBeGreaterThan(0);
  await wizard.locator(".cameraPanoramaImageContent").hover({ position: await imagePosition(wizard, known.points[6].panorama) });
  await expect(planPanel(wizard).locator("canvas")).not.toHaveAttribute("data-panorama-ghost-world", /./);
  await planPanel(wizard).locator("canvas").hover({ position: await planPosition(wizard, known.points[6].world) });
  await expect(wizard.getByTestId("panorama-ghost")).toHaveCount(0);
  expect((await fixture(request)).commands).toHaveLength(commandCount);
});

test("cancelling a failed save removes the rejected pair without leaving a phantom saved point", async ({ page, request }) => {
  const known = await fixture(request);
  const wizard = await reviewLegacyPanorama(page, request, "cancel-failure");
  await focusCamera(wizard);
  await enterPoint(wizard, known.points[0]);
  const pending = (await browserDraft(page)).drafts[0];
  const commandCount = (await fixture(request)).commands.length;
  await page.route("**/panorama/*/points", route => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Synthetic save failure" }) }));
  await saveButton(wizard).click();
  await expect(wizard.getByRole("alert")).toContainText("Synthetic save failure");
  await openPointDetails(wizard);
  await wizard.getByRole("button", { name: "Cancel this pair", exact: true }).click();
  await expect.poll(async () => (await browserDraft(page)).unsavedPoints).toBeNull();
  expect((await browserDraft(page)).drafts.some(point => point.id === pending.id)).toBe(false);
  expect((await currentJob(request)).points).toHaveLength(0);
  await expect(wizard.getByText("0 places marked · preview from 4", { exact: true })).toBeVisible();
  await page.unroute("**/panorama/*/points");
  await enterPoint(wizard, known.points[1]);
  await savePoint(page, wizard);
  const saved = (await currentJob(request)).points;
  expect(saved).toHaveLength(1);
  expect(saved[0].id).not.toBe(pending.id);
  expect((await fixture(request)).commands).toHaveLength(commandCount);
});

test("revision conflict reload preserves the rejected draft separately from the saved geometry", async ({ page, request }) => {
  const known = await fixture(request);
  await seedLegacyPanorama(request);
  await seedPoints(request, known.points.slice(0, 6));
  const wizard = await openWizard(page);
  await focusCamera(wizard);
  await wizard.getByRole("button", { name: "Add point", exact: true }).click();
  await enterPoint(wizard, known.points[6]);
  const pending = (await browserDraft(page)).drafts[0];
  const commandCount = (await fixture(request)).commands.length;
  const nextRevision = await seedPoints(request, known.points.slice(0, 6));
  const rejected = page.waitForResponse(response => response.url().endsWith("/points") && response.request().method() === "PUT");
  await saveButton(wizard).click();
  expect((await rejected).status()).toBe(409);
  await wizard.getByRole("button", { name: "Reload saved work", exact: true }).click();
  await expect(wizard.getByRole("alert")).toContainText("Your previous unfinished marks are preserved");
  expect((await browserDraft(page)).drafts.find(point => point.id === pending.id)).toEqual(pending);
  await expect(pointNavigation(wizard).getByRole("button", { name: "Add point", exact: true })).toBeDisabled();
  await expect(wizard.getByTestId("panorama-ghost")).toHaveCount(0);
  await expect(planPanel(wizard).locator("canvas")).not.toHaveAttribute("data-panorama-ghost-world", /./);
  await screenshot(page, "recovery-04-revision-conflict-1440-day");
  await wizard.getByRole("button", { name: "Continue with saved revision", exact: true }).click();
  await expect.poll(async () => (await browserDraft(page)).revision).toBe(nextRevision.revision);
  const preserved = await page.evaluate(key => JSON.parse(localStorage.getItem(`${key}.previous`)), draftKey);
  expect(preserved.drafts.find(point => point.id === pending.id)).toEqual(pending);
  expect((await currentJob(request)).points).toEqual(nextRevision.points);
  expect((await currentJob(request)).active).toBe(false);
  expect((await fixture(request)).commands).toHaveLength(commandCount);
});

test("a saved source panorama starts a point pair without capture or optical inputs", async ({ page, request }) => {
  const known = await fixture(request);
  await openCameraEditor(page);
  await page.getByRole("button", { name: /^(Map in composition|Continue mapping|View mapping)$/, exact: true }).click();
  const wizard = page.getByTestId("camera-panorama-mapping");
  await expect(wizard.getByRole("button", { name: "Use saved panorama", exact: true })).toHaveCount(0);
  await wizard.getByRole("button", {name: "Add point", exact: true}).click();
  await expect(pointButton(wizard, 1)).toHaveAttribute("aria-pressed", "true");
  await expect(wizard.locator("input[type='number']")).toHaveCount(0);
  await focusCamera(wizard);
  await wizard.getByRole("button", { name: "Enlarge image", exact: true }).click();
  await wizard.getByRole("button", { name: "Enlarge image", exact: true }).click();
  await enterPoint(wizard, known.points[0], "plan");
  await savePoint(page, wizard);
  const job = await currentJob(request);
  expect(job.source_panorama).toMatchObject({ id: known.source_artifact_id, revision: known.source_artifact_revision });
  expect(job.profile).toBeNull();
  expect(job.scan).toBeNull();
  expect(job.points).toHaveLength(1);
  expect(job.active).toBe(false);
  await expect(wizard.getByRole("button", { name: "Confirm pair", exact: true })).toHaveCount(0);
  await expect(pointNavigation(wizard).locator("button[aria-pressed=true]")).toHaveCount(0);
  expect((await fixture(request)).commands).toHaveLength(0);
  await screenshot(page, "source-01-first-saved-pair-1440-day");
  for (const point of known.points.slice(1, 4)) {
    await enterPoint(wizard, point);
    await savePoint(page, wizard);
  }
  expect((await currentJob(request)).points).toHaveLength(4);
  await screenshot(page, "source-four-adjustment-pairs");
  await expect(wizard.getByRole("button", { name: "Use this mapping", exact: true })).toBeEnabled();
  await screenshot(page, "simulation-01-four-points-ready");
  await wizard.getByRole("button", { name: "Use this mapping", exact: true }).click();
  const active = await currentJob(request);
  expect(active.active).toBe(true);
  expect(active.permissions.map_validated).toBe(true);
  expect(active.permissions.aim_enabled).toBe(false);
  await screenshot(page, "simulation-02-calibration-active");
  await expect(wizard.getByRole("button", { name: "Point camera", exact: true })).not.toBeVisible();
  await expect(wizard.locator(".cameraPanoramaViews")).toBeVisible();
  await wizard.getByRole("button", { name: "Fit image", exact: true }).click();
  await focusCamera(wizard);
  // Independent floor locations, absent from the four controls.
  // The oracle uses the synthetic scene's camera at height 3, not the fitted matrix.
  const mappingProbes = [];
  const canvas = planPanel(wizard).locator("canvas");
  for (const world of [{ x: 4, z: -2 }, { x: 6, z: 0 }, { x: 7.5, z: 2 }]) {
    const panorama = { x: (Math.atan2(world.z, world.x) / Math.PI + 1) / 2, y: .5 - Math.atan2(-3, Math.hypot(world.x, world.z)) / Math.PI };
    await wizard.locator(".cameraPanoramaImageContent").hover({ position: await imagePosition(wizard, panorama) });
    await expect(canvas).toHaveAttribute("data-panorama-ghost-world", /./);
    const [x, z] = JSON.parse(await canvas.getAttribute("data-panorama-ghost-world"));
    const errorMetres = Math.hypot(x - world.x, z - world.z);
    expect(errorMetres).toBeLessThan(.15);
    await canvas.hover({ position: await planPosition(wizard, world) });
    const ghost = wizard.getByTestId("panorama-ghost");
    await expect(ghost).toBeVisible();
    const projected = await ghost.evaluate(marker => ({ x: Number.parseFloat(marker.style.left) / 100, y: Number.parseFloat(marker.style.top) / 100 }));
    const reverseErrorPixels = Math.hypot((projected.x - panorama.x) * active.width, (projected.y - panorama.y) * active.height);
    expect(reverseErrorPixels).toBeLessThan(2);
    mappingProbes.push({ expected: world, observed: { x, z }, errorMetres, panorama, projected, reverseErrorPixels });
  }
  await screenshot(page, "simulation-03-independent-mapping-preview");
  const persisted = await currentJob(request);
  expect(persisted.points).toEqual(active.points);
  expect(persisted.revision).toBe(active.revision);
  await fs.writeFile(path.join(evidenceDirectory, "simulation-mapping-result.json"), JSON.stringify({ synthetic: true, job_id: active.id, active: active.active, points: active.points, quality: active.solution.quality, probes: mappingProbes, cameraCommands: (await fixture(request)).commands }, null, 2));
  await expect(wizard.getByRole("button", { name: "Point camera", exact: true })).toHaveCount(0);
  await expect(wizard.getByText("Verify camera pointing", { exact: true })).toHaveCount(0);
  expect((await fixture(request)).commands).toHaveLength(0);
  await screenshot(page, "source-02-active-map-before-pointing-1440-day");
  await openCameraEditor(page);
  await page.getByRole("button", { name: "View mapping", exact: true }).click();
  await expect(wizard).toBeVisible();
  await expect(wizard.getByRole("button", { name: "Edit points", exact: true })).toBeVisible();
  const reopened = await currentJob(request);
  expect(reopened.id).toBe(active.id);
  expect(reopened.active).toBe(true);
  expect(reopened.points).toEqual(active.points);
  expect(reopened.revision).toBe(active.revision);
  await screenshot(page, "source-active-map-reopened");
});

test("one calibration entry skips preparation and resumes the same draft", async ({ page, request }) => {
  let wizard = await openWizard(page);
  await expect(wizard.getByRole("img", { name: "Captured camera panorama", exact: true })).toBeVisible();
  const job = await currentJob(request);
  await expect(wizard.getByRole("button", { name: "Use saved panorama", exact: true })).toHaveCount(0);
  await expect(wizard.getByRole("img", { name: "Captured camera panorama", exact: true })).toBeVisible();
  wizard = await openWizard(page);
  expect((await currentJob(request)).id).toBe(job.id);
  await expect(wizard.getByText("Your pending markings were recovered. Continue where you left off.", { exact: true })).toHaveCount(0);
  expect((await fixture(request)).commands).toHaveLength(0);
});

test("an old unfinished preparation uses the available source panorama", async ({ page, request }) => {
  const draft = await seedLegacyPanorama(request, { startCapture: false });
  const wizard = await openWizard(page);
  await expect(wizard.locator("input[type='number']")).toHaveCount(0);
  await expect(wizard.getByRole("img", { name: "Captured camera panorama", exact: true })).toBeVisible();
  expect((await currentJob(request)).id).not.toBe(draft.id);
  expect((await currentJob(request)).state).toBe("ready");
  expect((await fixture(request)).commands).toHaveLength(0);
});

test("missing panorama links to camera preparation and returns to the same composition editor", async ({ page, request }) => {
  let published = false;
  const captureRequests = [];
  page.on("request", request => { if (request.method() === "POST" && request.url().includes("/sources/")) captureRequests.push(request.url()); });
  await page.route(`**${panoramaPath}?*`, async route => {
    const response = await route.fetch();
    const context = await response.json();
    if (!published) context.sources.forEach(source => { source.panorama = null; });
    await route.fulfill({ response, json: context });
  });
  await page.route(`**/sources/synthetic-wide/panorama`, async route => {
    const response = await route.fetch();
    const state = await response.json();
    if (!published) { state.active = null; state.candidate = null; state.job = null; }
    await route.fulfill({ response, json: state });
  });
  await openCameraEditor(page);
  await page.getByRole("button", { name: /^(Map in composition|Continue mapping|View mapping)$/, exact: true }).click();
  const wizard = page.getByTestId("camera-panorama-mapping");
  await expect(wizard.getByTestId("camera-source-panorama")).toHaveCount(0);
  await expect(wizard.getByRole("button", { name: "Generate panorama", exact: true })).toHaveCount(0);
  await expect(wizard.locator(".cameraPanoramaViews")).toHaveCount(0);
  await wizard.getByRole("button", { name: "Prepare panorama in camera settings", exact: true }).click();
  await expect(page).toHaveURL(/settings\?.*camera_id=panorama-simulated-camera.*source_id=synthetic-wide/);
  await expect(page.getByTestId("camera-source-panorama")).toBeVisible();
  expect(captureRequests).toEqual([]);
  published = true;
  // The absent state is injected; preparation below uses the real saved
  // synthetic artifact and its real crop endpoint, without acquiring images.
  await page.reload();
  await page.getByRole("button", { name: "Edit useful area", exact: true }).click();
  const savedSelection = page.waitForResponse(response => response.url().endsWith("/sources/synthetic-wide/panorama/crop") && response.request().method() === "PATCH");
  await page.getByRole("button", { name: "Save useful area", exact: true }).click();
  expect((await savedSelection).ok()).toBeTruthy();
  await page.getByRole("button", { name: "Return to composition", exact: true }).click();
  const editor = page.getByRole("dialog", { name: "Edit: Câmera simulada", exact: true });
  await expect(editor).toBeVisible();
  await expect(editor.getByRole("button", { name: "Calibrate ground and metric geometry", exact: true })).toHaveCount(0);
  await editor.getByRole("button", { name: "Map in composition", exact: true }).click();
  await expect(wizard.getByRole("img", { name: "Captured camera panorama", exact: true })).toBeVisible();
  expect(captureRequests).toEqual([]);
  expect((await fixture(request)).commands).toHaveLength(0);
});

test("review creates an editable draft and keeps the active calibration", async ({ page, request }) => {
  const known = await fixture(request);
  const response = await request.post(`${backend}${panoramaPath}`, { data: { element_id: elementId, source_id: "synthetic-wide", source_artifact_id: known.source_artifact_id, source_artifact_revision: known.source_artifact_revision } });
  const created = await response.json();
  const fit = await request.put(`${backend}${panoramaPath}/${created.id}/points`, { data: { revision: created.revision, points: known.points } });
  const fitted = await fit.json();
  expect((await request.post(`${backend}${panoramaPath}/${created.id}/activate`, { data: { revision: fitted.revision } })).ok()).toBeTruthy();
  const wizard = await openWizard(page);
  await expect(wizard.getByRole("img", { name: "Captured camera panorama", exact: true })).toBeVisible();
  await wizard.getByRole("button", { name: "Edit points", exact: true }).click();
  await expect(wizard.getByRole("img", { name: "Captured camera panorama", exact: true })).toBeVisible();
  const draft = await currentJob(request);
  expect(draft.id).not.toBe(created.id);
  expect(draft.points).toEqual(known.points);
  expect(draft.active).toBe(false);
  expect((await (await request.get(`${backend}${panoramaPath}/${created.id}`)).json()).active).toBe(true);
  await focusCamera(wizard);
  await pointButton(wizard, 1).click();
  await markPlan(wizard, { x: known.points[0].world.x + .1, z: known.points[0].world.z });
  await expect(saveButton(wizard)).toBeEnabled();
  await savePoint(page, wizard);
  expect((await (await request.get(`${backend}${panoramaPath}/${created.id}`)).json()).points).toEqual(known.points);
  expect((await fixture(request)).commands).toHaveLength(0);
});

test("incompatible panorama offers a camera shortcut without embedding preparation or writing", async ({ page, request }) => {
  await page.route(`**${panoramaPath}?*`, async route => {
    const response = await route.fetch();
    const context = await response.json();
    context.blockers = ["source_panorama_revision_conflict"];
    context.sources.forEach(source => { source.panorama = source.panorama ? { id: source.panorama.id, compatible: false, blockers: context.blockers } : null; });
    await route.fulfill({ response, json: context });
  });
  const writes = [];
  page.on("request", request => { if (request.method() === "POST" && request.url().includes("/panorama")) writes.push(request.url()); });
  await openCameraEditor(page);
  await page.getByRole("button", { name: /^(Map in composition|Continue mapping|View mapping)$/, exact: true }).click();
  const wizard = page.getByTestId("camera-panorama-mapping");
  await expect(wizard.getByTestId("camera-source-panorama")).toHaveCount(0);
  await expect(wizard.getByText("The saved panorama needs a compatibility review before it can be mapped. Its image and existing points are preserved.", { exact: true })).toBeVisible();
  await expect(wizard.getByRole("button", { name: "Prepare panorama in camera settings", exact: true })).toBeEnabled();
  await expect(wizard.locator(".cameraPanoramaViews")).toHaveCount(0);
  expect(await currentJob(request)).toBeNull();
  expect(writes).toEqual([]);
});

test("panorama request and image helpers retain the Home Assistant ingress prefix", async () => {
  const previousWindow = global.window;
  const previousFetch = global.fetch;
  const calls = [];
  global.window = { __TOPOSYNC_PUBLIC_BASE_PATH__: "/api/hassio_ingress/synthetic", location: { href: "http://localhost/api/hassio_ingress/synthetic/", origin: "http://localhost" } };
  global.fetch = async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({ synthetic: true }) }; };
  try {
    expect(resolveToposyncUrl(`/settings?panel=com.toposync.cameras&camera_id=${cameraId}&source_id=synthetic-wide`)).toBe(`/api/hassio_ingress/synthetic/settings?panel=com.toposync.cameras&camera_id=${cameraId}&source_id=synthetic-wide`);
    expect(resolveToposyncUrl(`${panoramaPath}/job/images/panorama.jpg`)).toBe(`/api/hassio_ingress/synthetic${panoramaPath}/job/images/panorama.jpg`);
    await requestJson(`${panoramaPath}/job/check`, { method: "POST", body: "{}" });
    expect(calls[0].url).toBe(`/api/hassio_ingress/synthetic${panoramaPath}/job/check`);
    expect(calls[0].options.method).toBe("POST");
  } finally {
    global.window = previousWindow;
    global.fetch = previousFetch;
  }
});

test("i18n preserves point drafts and localizes errors, help and pair progress", async ({ page, request }) => {
  const known = await fixture(request);
  await openCameraEditor(page);
  await page.getByRole("button", { name: /^(Map in composition|Continue mapping|View mapping)$/, exact: true }).click();
  let wizard = page.getByTestId("camera-panorama-mapping");
  await expect(wizard.getByRole("button", { name: "Use saved panorama", exact: true })).toHaveCount(0);
  await focusCamera(wizard);
  await enterPoint(wizard, known.points[0], "plan");
  const pending = (await browserDraft(page)).drafts[0];

  await page.evaluate(() => localStorage.setItem("toposync.locale", "pt-BR"));
  await page.reload();
  await page.getByRole("button", { name: "Editar", exact: true }).click();
  await page.getByText("Câmera simulada", { exact: true }).dblclick();
  await page.getByRole("button", { name: /^(Mapear na composição|Continuar mapeamento|Ver mapeamento)$/, exact: true }).click();
  wizard = page.getByTestId("camera-panorama-mapping");
  await expect(wizard.getByRole("navigation", { name: "Pontos", exact: true })).toBeVisible();
  await expect(wizard.getByRole("group", { name: /^Panorâmica: use as setas/ })).toBeVisible();
  await expect(wizard.getByRole("group", { name: /^Composição: use as setas/ })).toBeVisible();
  await expect(wizard.getByRole("button", { name: "Confirmar par", exact: true })).toBeEnabled();
  expect((await browserDraft(page)).drafts[0]).toEqual(pending);

  await page.route("**/panorama/*/points", route => route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: { code: "revision_conflict", message: "Synthetic revision conflict diagnostic" } }) }));
  await wizard.getByRole("button", { name: "Confirmar par", exact: true }).click();
  const alert = wizard.getByRole("alert");
  await expect(alert.locator(":scope > span")).toHaveText("O trabalho salvo mudou. Suas marcações foram preservadas; recarregue a revisão salva antes de tentar novamente.");
  await expect(alert.getByText("Synthetic revision conflict diagnostic", { exact: true })).not.toBeVisible();
  await alert.getByText("Ver detalhes", { exact: true }).click();
  await expect(alert.getByText("Synthetic revision conflict diagnostic", { exact: true })).toBeVisible();
  await page.unroute("**/panorama/*/points");
  await page.route("**/panorama/*/points", route => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: { code: "future_unknown_error", message: "Synthetic unknown diagnostic" } }) }));
  await wizard.getByRole("button", { name: "Tentar salvar novamente", exact: true }).click();
  await expect(alert.locator(":scope > span")).toHaveText("Não foi possível concluir esta ação. Tente novamente.");
  await page.unroute("**/panorama/*/points");
  await wizard.getByRole("button", { name: "Tentar salvar novamente", exact: true }).click();
  await expect(wizard.getByText("1 locais marcados · prévia a partir de 4", { exact: true })).toBeVisible();
  await expect(wizard).not.toContainText("coordenadas abaixo");
  await expect(wizard).not.toContainText("ext.cameras.");
  await screenshot(page, "i18n-01-points-pt-BR");

  await page.evaluate(() => localStorage.setItem("toposync.locale", "en"));
  wizard = await openWizard(page);
  await expect(wizard.getByText("1 places marked · preview from 4", { exact: true })).toBeVisible();
  await expect(wizard).not.toContainText("coordinates below");
  await expect(wizard).not.toContainText("ext.cameras.");
  expect((await currentJob(request)).points[0]).toMatchObject(pending);
  expect((await fixture(request)).commands).toHaveLength(0);
});


test("a saved navigation failure remains visible after reopening the point wizard", async ({ page, request }) => {
  const known = await fixture(request);
  const created = await request.post(`${backend}${panoramaPath}`, { data: {
    element_id: elementId, source_id: "synthetic-wide",
    source_artifact_id: known.source_artifact_id,
    source_artifact_revision: known.source_artifact_revision,
  } });
  expect(created.ok()).toBeTruthy();
  await page.route(`**${panoramaPath}?*`, async route => {
    const response = await route.fetch();
    const body = await response.json();
    body.job.navigation = { phase: "failed", physical_state: "stopped", can_return: true,
                            error_code: "return_framing_unconfirmed" };
    await route.fulfill({ response, json: body });
  });
  const wizard = await openWizard(page);
  await expect(wizard.getByText("The return was attempted, but the original framing could not be confirmed.", { exact: true })).toBeVisible();
  await expect(wizard.getByRole("button", { name: "Return to original framing", exact: true })).toBeVisible();
  expect((await fixture(request)).commands).toHaveLength(0);
});

for (const view of ["360 View 2D", "360 View 3D"]) {
  test(`panorama area selection clips rendered pixels and restores them in ${view}`, async ({ page, request }) => {
    const known = await fixture(request);
    const created = await request.post(`${backend}${panoramaPath}`, { data: {
      element_id: elementId, source_id: known.source_id, source_artifact_id: known.source_artifact_id,
      source_artifact_revision: known.source_artifact_revision,
    } });
    expect(created.ok()).toBeTruthy();
    const draft = await created.json();
    const saved = await request.put(`${backend}${panoramaPath}/${draft.id}/points`, { data: { revision: draft.revision, points: known.points } });
    expect(saved.ok()).toBeTruthy();
    const job = await saved.json();
    expect((await request.post(`${backend}${panoramaPath}/${job.id}/activate`, { data: { revision: job.revision } })).ok()).toBeTruthy();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    const panoramaPixels = async () => {
      const screenshot = await page.screenshot();
      return page.evaluate(async encoded => {
        const image = await createImageBitmap(await (await fetch(`data:image/png;base64,${encoded}`)).blob());
        const canvas = new OffscreenCanvas(image.width, image.height);
        const context = canvas.getContext("2d");
        context.drawImage(image, 0, 0); image.close();
        const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
        let count = 0;
        // These are the two fixture checkerboard colours, absent from the floor.
        for (let offset = 0; offset < pixels.length; offset += 4) {
          if ([[112,126,95], [132,145,112]].some(colour => colour.every((value,index) => Math.abs(pixels[offset+index]-value) <= 2))) count++;
        }
        return count;
      }, screenshot.toString("base64"));
    };
    await page.goto("/");
    await page.getByRole("button", { name: /^Rendering:/ }).click();
    await page.getByRole("dialog").getByRole("button", { name: new RegExp(`^${view} `) }).click();
    await expect(page.getByTestId("panorama-ground-status")).toBeVisible();
    await expect.poll(panoramaPixels).toBeGreaterThan(1000);
    const before = await panoramaPixels();
    await screenshot(page, `area-${view.endsWith("2D") ? "2d" : "3d"}-before`);
    await openCameraEditor(page);
    const editor = page.getByRole("dialog", { name: "Edit: Câmera simulada", exact: true });
    const area = editor.getByRole("combobox", { name: "360 area crop", exact: true });
    await expect(area).toBeEnabled();
    await area.click();
    await page.getByRole("option", { name: "Recorte de teste", exact: true }).click();
    await editor.getByRole("button", { name: "Close", exact: true }).last().click();
    await page.getByRole("button", { name: "Back", exact: true }).click();
    await expect(page.getByTestId("panorama-ground-status")).toBeVisible();
    await expect.poll(panoramaPixels).toBeLessThan(before * .7);
    const clipped = await panoramaPixels();
    expect(clipped).toBeGreaterThan(before * .1);
    await screenshot(page, `area-${view.endsWith("2D") ? "2d" : "3d"}-clipped`);
    await openCameraEditor(page);
    await expect(editor.getByText("Recorte de teste", { exact: true })).toBeVisible();
    await area.focus();
    await area.press("Backspace");
    await editor.getByRole("button", { name: "Close", exact: true }).last().click();
    await page.getByRole("button", { name: "Back", exact: true }).click();
    await expect.poll(panoramaPixels).toBeGreaterThan(before * .9);
    await screenshot(page, `area-${view.endsWith("2D") ? "2d" : "3d"}-restored`);
    if (view.endsWith("2D")) {
      const sourcePath = `${backend}/api/cameras/cameras/${cameraId}/sources/${known.source_id}/panorama`;
      const source = await (await request.get(sourcePath)).json();
      const cropped = await request.patch(`${sourcePath}/crop`, { data: {
        artifact_id: source.active.id, expected_revision: source.active.crop_revision,
        crop: { u_start: .48, u_width: .03, v_start: .55, v_height: .3 },
      } });
      expect(cropped.ok()).toBeTruthy();
      await page.reload();
      await expect(page.getByTestId("panorama-ground-status")).toBeVisible();
      await expect.poll(panoramaPixels).toBeLessThan(before * .7);
      expect(await panoramaPixels()).toBeGreaterThan(100);
      expect((await currentJob(request)).points).toEqual(known.points);
      await screenshot(page, "area-2d-presentation-crop");
      const reset = await request.patch(`${sourcePath}/crop`, { data: {
        artifact_id: source.active.id, expected_revision: (await cropped.json()).artifact.crop_revision, crop: source.active.crop,
      } });
      expect(reset.ok()).toBeTruthy();
      await page.reload();
      await expect.poll(panoramaPixels).toBeGreaterThan(before * .9);
    }
    expect(errors).toEqual([]);
    expect((await fixture(request)).commands).toHaveLength(0);
    await fs.writeFile(path.join(evidenceDirectory, `area-${view.endsWith("2D") ? "2d" : "3d"}.json`), JSON.stringify({ view, before, clipped, restored: await panoramaPixels(), synthetic: true, physicalCommands: 0 }, null, 2));
  });
}

test("image loading and failure explain recovery without losing the saved pairs", async ({ page, request }) => {
  const known = await fixture(request);
  const created = await request.post(`${backend}${panoramaPath}`, { data: { element_id: elementId, source_id: known.source_id, source_artifact_id: known.source_artifact_id, source_artifact_revision: known.source_artifact_revision } });
  const job = await created.json();
  const stored = await request.put(`${backend}${panoramaPath}/${job.id}/points`, { data: { revision: job.revision, points: known.points.slice(0, 1) } });
  expect(stored.ok()).toBeTruthy();
  let release;
  const imageGate = new Promise(resolve => { release = resolve; });
  await page.route("**/images/panorama.png", async route => { await imageGate; await route.abort(); });
  const wizard = await openWizard(page);
  await expect(wizard.getByText("Loading panorama image…", { exact: true })).toBeVisible();
  await expect(wizard.getByRole("button", {name: "Use this mapping", exact: true})).toBeDisabled();
  await screenshot(page, "state-image-loading");
  release();
  await expect(wizard.getByRole("alert")).toContainText("The saved image could not be loaded.");
  await screenshot(page, "state-image-failed");
  expect((await currentJob(request)).points).toEqual(known.points.slice(0, 1));
  await page.unroute("**/images/panorama.png");
  await wizard.getByRole("button", { name: "Reload saved work", exact: true }).click();
  await expect(wizard.getByRole("img", { name: "Captured camera panorama", exact: true })).toHaveJSProperty("naturalWidth", 1024);
  await expect(wizard.getByRole("alert")).toHaveCount(0);
  expect((await currentJob(request)).points).toEqual(known.points.slice(0, 1));
  expect((await fixture(request)).commands).toHaveLength(0);
});

test("camera preparation returns to the selected source even when another source has a panorama", async ({ page, request }) => {
  let returned = false;
  await page.route(`**${panoramaPath}?*`, async route => {
    const response = await route.fetch();
    const context = await response.json();
    if (!returned) context.sources.forEach(source => { source.panorama = null; });
    await route.fulfill({ response, json: context });
  });
  await openCameraEditor(page);
  await page.getByRole("button", { name: "Map in composition", exact: true }).click();
  const wizard = page.getByTestId("camera-panorama-mapping");
  await wizard.getByRole("combobox").selectOption("synthetic-detail");
  await wizard.getByRole("button", { name: "Prepare panorama in camera settings", exact: true }).click();
  await expect(page).toHaveURL(/source_id=synthetic-detail/);
  returned = true;
  await page.getByRole("button", { name: "Return to composition", exact: true }).click();
  await page.getByRole("button", { name: "Map in composition", exact: true }).click();
  await expect(wizard.getByRole("combobox")).toHaveValue("synthetic-detail");
  await expect(wizard.getByRole("button", { name: "Prepare panorama in camera settings", exact: true })).toBeVisible();
  await expect(wizard.locator(".cameraPanoramaViews")).toHaveCount(0);
  expect(await currentJob(request)).toBeNull();
  expect((await fixture(request)).commands).toHaveLength(0);
});

for (const [status, message] of [
  ["processing", "The panorama is being prepared."],
  ["failed", "Panorama preparation did not finish."],
]) {
  test(`source preparation ${status} offers context without starting another capture`, async ({ page, request }) => {
    await page.route(`**${panoramaPath}?*`, async route => {
      const response = await route.fetch();
      const context = await response.json();
      context.sources.forEach(source => { source.panorama = null; });
      await route.fulfill({ response, json: context });
    });
    await page.route("**/sources/synthetic-wide/panorama", async route => {
      const response = await route.fetch();
      const source = await response.json();
      await route.fulfill({ response, json: { ...source, active: null, candidate: null, job: { status } } });
    });
    const writes = [];
    page.on("request", request => { if (["POST", "PUT", "PATCH"].includes(request.method()) && request.url().includes("/panorama")) writes.push(request.url()); });
    await openCameraEditor(page);
    await page.getByRole("button", { name: "Map in composition", exact: true }).click();
    const wizard = page.getByTestId("camera-panorama-mapping");
    await expect(wizard.getByRole("status").filter({ hasText: message })).toBeVisible();
    await expect(wizard.getByRole("button", { name: "Prepare panorama in camera settings", exact: true })).toBeEnabled();
    expect(writes).toEqual([]);
    expect((await fixture(request)).commands).toEqual([]);
    await screenshot(page, `state-source-${status}`);
  });
}

test("mapping images, point saves, camera shortcut and return execute through an ingress prefix", async ({ page, request }) => {
  const prefix = "/api/hassio_ingress/panorama-validation";
  await page.addInitScript(value => { window.__TOPOSYNC_PUBLIC_BASE_PATH__ = value; }, prefix);
  const forwarded = [];
  await page.route(`**${prefix}/**`, async route => {
    const url = new URL(route.request().url());
    forwarded.push(url.pathname);
    url.pathname = url.pathname.slice(prefix.length) || "/";
    await route.continue({ url: url.href });
  });
  await page.goto(`${prefix}/`);
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByText("Câmera simulada", { exact: true }).dblclick();
  await page.getByRole("button", { name: "Map in composition", exact: true }).click();
  const wizard = page.getByTestId("camera-panorama-mapping");
  const image = wizard.getByRole("img", { name: "Captured camera panorama", exact: true });
  await expect(image).toHaveJSProperty("naturalWidth", 1024);
  await expect(image).toHaveAttribute("src", new RegExp(`^${prefix}/api/`));
  const known = await fixture(request);
  await focusCamera(wizard);
  await enterPoint(wizard, known.points[0]);
  await savePoint(page, wizard);
  expect((await currentJob(request)).points).toHaveLength(1);
  expect(forwarded.some(url => url.endsWith("/points"))).toBe(true);
  expect(forwarded.some(url => url.endsWith("/images/panorama.png"))).toBe(true);
  const fitted = await seedPoints(request, known.points);
  expect((await request.post(`${backend}${panoramaPath}/${fitted.id}/activate`, { data: { revision: fitted.revision } })).ok()).toBeTruthy();
  await page.reload();
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByText("Câmera simulada", { exact: true }).dblclick();
  await page.getByRole("button", { name: "View mapping", exact: true }).click();
  await wizard.locator("summary").filter({ hasText: /^See details$/ }).click();
  await wizard.getByRole("button", { name: "Prepare this source’s panorama", exact: true }).click();
  expect(new URL(page.url()).pathname).toBe(`${prefix}/settings`);
  await expect(page.getByTestId("camera-source-panorama")).toBeVisible();
  await page.getByRole("button", { name: "Return to composition", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Edit: Câmera simulada", exact: true })).toBeVisible();
  expect(new URL(page.url()).pathname).toBe(`${prefix}/`);
  expect((await fixture(request)).commands).toHaveLength(0);
});

test("an incompatible source exposes preserved pairs as read-only history", async ({ page, request }) => {
  const known = await fixture(request);
  const created = await request.post(`${backend}${panoramaPath}`, { data: { element_id: elementId, source_id: known.source_id, source_artifact_id: known.source_artifact_id, source_artifact_revision: known.source_artifact_revision } });
  const job = await created.json();
  expect((await request.put(`${backend}${panoramaPath}/${job.id}/points`, { data: { revision: job.revision, points: known.points.slice(0, 1) } })).ok()).toBeTruthy();
  await page.route(`**${panoramaPath}?*`, async route => {
    const response = await route.fetch();
    const context = await response.json();
    context.blockers = ["camera_configuration_changed"];
    context.sources.forEach(source => { if (source.panorama) source.panorama = { id: source.panorama.id, compatible: false, blockers: context.blockers }; });
    await route.fulfill({ response, json: context });
  });
  const writes = [];
  page.on("request", request => { if (["POST", "PUT", "PATCH"].includes(request.method()) && request.url().includes("/panorama")) writes.push(request.url()); });
  await openCameraEditor(page);
  await page.getByRole("button", { name: "Continue mapping", exact: true }).click();
  const wizard = page.getByTestId("camera-panorama-mapping");
  await wizard.locator("summary").filter({ hasText: /^Preserved points \(1\)$/ }).click();
  await expect(wizard.getByText(/These saved pairs are read-only/)).toBeVisible();
  await expect(wizard.locator("li")).toContainText("Adjustment");
  await expect(wizard.locator(".cameraPanoramaViews")).toHaveCount(0);
  expect((await currentJob(request)).points).toEqual(known.points.slice(0, 1));
  expect(writes).toEqual([]);
  await screenshot(page, "state-preserved-pairs");
});

test.describe("Touch mapping", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });
  test("touch and keyboard explore approximate fits without creating points", async ({ page, request }) => {
    const known = await fixture(request);
    await seedLegacyPanorama(request);
    const pairs = known.points.map(point => ({ ...point, panorama: { ...point.panorama } }));
    pairs[6].panorama.y += .04;
    const seeded = await seedPoints(request, pairs);
    const wizard = await openWizard(page);
    if (await wizard.getByRole("button", {name: "Finish adjustment", exact: true}).isVisible()) await wizard.getByRole("button", {name: "Finish adjustment", exact: true}).tap();
    await focusCamera(wizard);
    const image = wizard.locator(".cameraPanoramaImageContent");
    const canvas = planPanel(wizard).locator("canvas");
    await image.tap({ position: await imagePosition(wizard, known.points[6].panorama) });
    await expect(canvas).toHaveAttribute("data-panorama-ghost-world", /./);
    await canvas.tap({ position: await planPosition(wizard, known.points[6].world) });
    await expect(wizard.getByTestId("panorama-ghost")).toBeVisible();
    await expect(wizard.getByTestId("plan-probe")).toBeVisible();
    await planPanel(wizard).press("ArrowRight");
    await expect(wizard.getByTestId("panorama-ghost")).toBeVisible();
    await planPanel(wizard).press("Escape");
    await expect(wizard.getByTestId("panorama-ghost")).toHaveCount(0);
    await checkLayout(wizard, 390);
    expect((await currentJob(request)).revision).toBe(seeded.revision);
    expect((await currentJob(request)).points).toEqual(seeded.points);
    await screenshot(page, "ghost-touch-inspection");
  });
  test("completes four pairs with one confirmation and keeps review read-only", async ({ page, request }) => {
    const known = await fixture(request);
    await page.addInitScript(() => localStorage.setItem("toposync.locale", "pt-BR"));
    await page.goto("/");
    await page.getByRole("button", { name: "Editar", exact: true }).tap();
    const tools = page.getByRole("button", { name: /^Ferramentas e camadas:/ });
    if (await tools.getAttribute("aria-expanded") === "false") await tools.tap();
    await page.getByRole("button", { name: /^Câmera simulada/ }).tap();
    await page.getByRole("button", { name: "Editar Câmera simulada", exact: true }).tap();
    await page.getByRole("button", { name: "Mapear na composição", exact: true }).tap();
    const wizard = page.getByTestId("camera-panorama-mapping");
    const imagePanel = wizard.getByRole("group", { name: /^Panorâmica: use/ });
    const worldPanel = wizard.getByRole("group", { name: /^Composição: use/ });
    const image = imagePanel.locator("img");
    const confirm = wizard.getByRole("button", { name: "Confirmar par", exact: true });
    const client = await page.context().newCDPSession(page);
    const drag = async (x, y, dx, dy) => {
      await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y }] });
      for (let step = 1; step <= 8; step++) await client.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: x + dx * step / 8, y: y + dy * step / 8 }] });
      await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    };
    const fullyVisible = async locator => {
      expect(await locator.evaluate(element => {
        const rectangle = element.getBoundingClientRect();
        let left = 0, top = 0, right = innerWidth, bottom = innerHeight;
        for (let parent = element.parentElement; parent; parent = parent.parentElement) {
          const style = getComputedStyle(parent);
          if (/(auto|scroll|hidden|clip)/.test(style.overflowY + style.overflowX)) {
            const bounds = parent.getBoundingClientRect();
            left = Math.max(left, bounds.left); top = Math.max(top, bounds.top);
            right = Math.min(right, bounds.right); bottom = Math.min(bottom, bounds.bottom);
          }
        }
        return rectangle.left >= left - 1 && rectangle.top >= top - 1 && rectangle.right <= right + 1 && rectangle.bottom <= bottom + 1;
      })).toBe(true);
    };
    await expect(imagePanel).toBeVisible();
    await expect(worldPanel).toBeVisible();
    await imagePanel.scrollIntoViewIfNeeded();
    const untouched = await browserDraft(page);
    const initialPanel = await imagePanel.boundingBox();
    const touchA = { x: initialPanel.x + 100, y: initialPanel.y + 100 };
    const touchB = { x: initialPanel.x + 180, y: initialPanel.y + 100 };
    await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [touchA] });
    await client.send("Input.dispatchTouchEvent", { type: "touchCancel", touchPoints: [] });
    await client.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [touchA, touchB] });
    await client.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ ...touchA, x: touchA.x - 10 }, { ...touchB, x: touchB.x + 10 }] });
    await client.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    expect((await browserDraft(page))?.drafts).toEqual(untouched?.drafts);
    await worldPanel.scrollIntoViewIfNeeded();
    await wizard.getByRole("button", { name: "Composição inteira", exact: true }).tap();
    await wizard.getByRole("button", { name: "Focar câmera", exact: true }).tap();
    await worldPanel.scrollIntoViewIfNeeded();
    const canvas = worldPanel.locator("canvas");
    let bounds = await canvas.boundingBox();
    const initialPlanScale = Math.max(4, (Math.min(bounds.width, bounds.height) - 32) / 24);
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.mouse.wheel(0, -420 * Math.log2(34 / initialPlanScale));
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const planScale = 34;
    // Two short touch pans centre x=5; neither may produce a point.
    await drag(bounds.x + bounds.width * .75, bounds.y + bounds.height / 2, -2.5 * planScale, 0);
    await drag(bounds.x + bounds.width * .75, bounds.y + bounds.height / 2, -2.5 * planScale, 0);
    expect((await browserDraft(page))?.drafts?.[0]?.world ?? null).toBeNull();
    let planCenter = { x: 5, z: 0 };
    const visibleBounds = locator => locator.evaluate(element => {
      const rect = element.getBoundingClientRect();
      let left = rect.left, top = rect.top, right = rect.right, bottom = rect.bottom;
      for (let parent = element.parentElement; parent; parent = parent.parentElement) {
        const style = getComputedStyle(parent), bounds = parent.getBoundingClientRect();
        if (/(auto|scroll|hidden|clip)/.test(style.overflowY + style.overflowX)) {
          left = Math.max(left, bounds.left); top = Math.max(top, bounds.top);
          right = Math.min(right, bounds.right); bottom = Math.min(bottom, bounds.bottom);
        }
      }
      const header = element.closest(".cameraPanoramaView").querySelector(".cameraPanoramaViewHeader").getBoundingClientRect();
      top = Math.max(top, header.bottom);
      return { x: left, y: top, width: right - left, height: bottom - top };
    });
    for (let index = 0; index < 4; index++) {
      const point = known.points[index];
      await wizard.getByRole("button", {name: "Adicionar ponto", exact: true}).tap();
      await imagePanel.scrollIntoViewIfNeeded();
      // Use visible navigation controls, then touch-pan the reference into view.
      if (index === 0) for (let zoom = 0; zoom < 4; zoom++) await wizard.getByRole("button", { name: "Ampliar imagem", exact: true }).tap();
      for (let attempt = 0; attempt < 20; attempt++) {
        const img = await image.boundingBox();
        const panel = await visibleBounds(imagePanel);
        const x = img.x + point.panorama.x * img.width, y = img.y + point.panorama.y * img.height;
        const covered = await wizard.locator("button.cameraPanoramaMarker").evaluateAll((markers, point) => markers.some(marker => {
          const r = marker.getBoundingClientRect(); return r.width && point.x >= r.left && point.x <= r.right && point.y >= r.top && point.y <= r.bottom;
        }), { x, y });
        if (x > panel.x + 25 && x < panel.x + panel.width - 25 && y > panel.y + 25 && y < panel.y + panel.height - 25 && !covered) {
          await page.touchscreen.tap(x, y); break;
        }
        if (covered) await wizard.getByRole("button", { name: "Ampliar imagem", exact: true }).tap();
        else await drag(panel.x + panel.width / 2, panel.y + panel.height / 2, Math.max(-100, Math.min(100, panel.x + panel.width / 2 - x)), Math.max(-80, Math.min(80, panel.y + panel.height / 2 - y)));
      }
      await expect(confirm).toBeDisabled();
      await confirm.scrollIntoViewIfNeeded();
      await fullyVisible(confirm);
      await worldPanel.scrollIntoViewIfNeeded();
      bounds = await canvas.boundingBox();
      const visiblePlan = await visibleBounds(worldPanel);
      const centre = { x: visiblePlan.x + visiblePlan.width / 2, y: visiblePlan.y + visiblePlan.height / 2 };
      for (let attempt = 0; attempt < 6; attempt++) {
        const x = bounds.x + bounds.width / 2 + (point.world.x - planCenter.x) * planScale;
        const y = bounds.y + bounds.height / 2 + (point.world.z - planCenter.z) * planScale;
        const dx = Math.max(-80, Math.min(80, centre.x - x));
        const dy = Math.max(-40, Math.min(40, centre.y - y));
        if (Math.hypot(dx, dy) < 1) break;
        await drag(centre.x, centre.y, dx, dy);
        planCenter = { x: planCenter.x - dx / planScale, z: planCenter.z - dy / planScale };
      }
      await page.touchscreen.tap(centre.x, centre.y);
      await expect(confirm).toBeEnabled();
      await confirm.scrollIntoViewIfNeeded();
      await fullyVisible(confirm);
      if (index === 0) await screenshot(page, "touch-390-first-pair");
      const saved = page.waitForResponse(response => response.url().endsWith("/points") && response.request().method() === "PUT");
      await confirm.tap();
      expect((await saved).ok()).toBeTruthy();
    }
    const activate = wizard.getByRole("button", { name: "Usar este mapeamento", exact: true });
    await expect(activate).toBeEnabled();
    const before = await currentJob(request);
    expect(before.points).toHaveLength(4);
    await canvas.tap({ position: { x: 80, y: 100 } });
    expect((await currentJob(request)).revision).toBe(before.revision);
    await screenshot(page, "touch-390-review");
    await activate.tap();
    await expect(wizard.getByRole("button", { name: "Editar pontos", exact: true })).toBeVisible();
    const active = await currentJob(request);
    expect(active.active).toBe(true);
    expect(active.points).toEqual(before.points);
    expect((await fixture(request)).commands).toEqual([]);
    await screenshot(page, "touch-390-active");
    for (const [width, height] of [[1440, 900], [1280, 720]]) {
      await page.setViewportSize({ width, height });
      await fullyVisible(wizard.getByRole("button", { name: "Voltar às opções da câmera", exact: true }));
      await screenshot(page, `mapping-active-pt-${width}`);
    }
    await wizard.getByRole("button", { name: "Voltar às opções da câmera", exact: true }).tap();
    await page.getByRole("button", { name: "Ver mapeamento", exact: true }).tap();
    await expect(wizard.getByRole("button", { name: "Editar pontos", exact: true })).toBeVisible();
    expect((await currentJob(request)).points).toEqual(active.points);
    await client.detach();
  });
});

test("incompatible quick references explain recovery and disabled controls do not animate", async ({ page, request }) => {
  await page.route(`**${panoramaPath}/native-reference?*`, route => route.fulfill({ status: 409, json: { detail: { code: "source_panorama_changed", message: "Changed" } } }));
  await page.goto(`/settings?panel=com.toposync.cameras&camera_id=${cameraId}&source_id=synthetic-wide`);
  const section = page.getByTestId("panorama-native-references");
  await expect(section).toBeVisible();
  await section.locator("summary").click();
  await expect(section).toContainText("no longer matches the current camera configuration");
  await expect(section).not.toContainText("Wait for connection recovery");
  const button = section.getByRole("button", { name: "Prepare current position", exact: true });
  await expect(button).toBeDisabled();
  await button.hover({ force: true });
  const before = await button.evaluate(element => {
    const style = getComputedStyle(element);
    return { cursor: style.cursor, opacity: Number(style.opacity), transform: style.transform };
  });
  expect(before.cursor).toBe("not-allowed");
  expect(before.opacity).toBeLessThan(0.7);
  const box = await button.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  expect(await button.evaluate(element => getComputedStyle(element).transform)).toBe(before.transform);
  await page.mouse.up();
  expect((await fixture(request)).commands).toHaveLength(0);
  await screenshot(page, "quick-reference-incompatible-disabled");
});

test("approximate controls permit visual acceptance and live refinement without saving", async ({ page, request }) => {
  const known = await fixture(request);
  await seedLegacyPanorama(request);
  const pairs = known.points.map(point => ({ ...point, panorama: { ...point.panorama } }));
  pairs[0].panorama.y += .02;
  const seeded = await seedPoints(request, pairs);
  expect(seeded.solution.preview.eligible).toBe(true);
  expect(seeded.solution.quality.warnings).toContain("approximate_fit");
  const wizard = await openWizard(page);
  await focusCamera(wizard);
  const canvas = planPanel(wizard).locator("canvas");
  const commands = (await fixture(request)).commands.length;
  await wizard.locator(".cameraPanoramaImageContent").click({ position: await imagePosition(wizard, known.points[6].panorama) });
  await expect(canvas).toHaveAttribute("data-panorama-ghost-world", /./);
  const before = await canvas.getAttribute("data-panorama-ghost-world");
  await pointButton(wizard, 1).click();
  const previewResponse = page.waitForResponse(response => response.url().endsWith("/preview"));
  await markPlan(wizard, { x: 3.4, z: -3 });
  expect((await previewResponse).ok()).toBe(true);
  await expect(canvas).not.toHaveAttribute("data-panorama-ghost-world", before);
  expect((await currentJob(request)).revision).toBe(seeded.revision);
  expect((await currentJob(request)).points).toEqual(seeded.points);
  await expect(wizard.getByRole("button", { name: "Use this mapping", exact: true })).toBeEnabled();
  await wizard.getByRole("button", { name: "Use this mapping", exact: true }).click();
  await expect.poll(async () => (await currentJob(request)).active).toBe(true);
  expect((await currentJob(request)).revision).toBe(seeded.revision + 1);
  expect((await fixture(request)).commands).toHaveLength(commands);
  await screenshot(page, "visual-acceptance-refinement");
});

test("review preserves incomplete drafts and approximate fits can be accepted", async ({ page, request }) => {
  const known = await fixture(request);
  let wizard = await openWizard(page);
  await expect(pointNavigation(wizard)).toBeVisible();
  await expect(wizard.locator(".cameraPanoramaView")).toHaveCount(2);
  await expect(wizard.getByRole("button", {name: "Show both", exact: true})).toHaveCount(0);
  await markImage(wizard, known.points[0].panorama);
  const initial = await browserDraft(page);
  await wizard.getByRole("button", { name: "Add point", exact: true }).click();
  await wizard.getByRole("button", { name: "Remove place 2", exact: true }).click();
  await expect(pointButton(wizard, 2)).toHaveCount(0);
  expect((await browserDraft(page)).drafts[0]).toEqual(initial.drafts[0]);
  const activate = wizard.getByRole("button", { name: "Use this mapping", exact: true });
  await expect(activate).toBeDisabled();
  await expect(activate).toHaveAttribute("aria-describedby", "panorama-activation-status");
  expect(await activate.evaluate(button => getComputedStyle(button).cursor)).toBe("not-allowed");
  expect(Number(await activate.evaluate(button => getComputedStyle(button).opacity))).toBeLessThan(.6);
  expect((await browserDraft(page)).drafts[0]).toEqual(initial.drafts[0]);
  const pairs = known.points.map(point => ({ ...point, panorama: { ...point.panorama } }));
  pairs[6].panorama.y += .04;
  pairs[7].panorama.y += .03;
  const blocked = await seedPoints(request, pairs);
  expect(blocked.solution.quality.warnings).toContain("approximate_fit");
  await page.evaluate(key => localStorage.removeItem(key), draftKey);
  wizard = await openWizard(page);
  await expect(pointButton(wizard, 7, "check").locator(".cameraPanoramaPairState")).toHaveAttribute("aria-label", "Approximate fit — explore the result");
  await expect(pointButton(wizard, 1)).not.toContainText("✓");
  await expect(wizard.getByRole("button", { name: "Use this mapping", exact: true })).toBeEnabled();
  await wizard.locator(".cameraPanoramaDiagnostics summary").click();
  await expect(wizard.locator(".cameraPanoramaDiagnostics")).toContainText("Angular difference:");

  await screenshot(page, "mapping-blocked-actionable-diagnostic");
  await wizard.locator(".cameraPanoramaDiagnostics").getByRole("button", { name: "Adjust place 7", exact: true }).click();
  await expect(pointButton(wizard, 7, "check")).toHaveAttribute("aria-pressed", "true");
  await expect(wizard.getByTestId("panorama-ghost")).toHaveCount(0);
  expect((await fixture(request)).commands).toEqual([]);
});

test("composition rotation preserves marking coordinates and review preserves navigation", async ({ page, request }) => {
  const known = await fixture(request);
  const wizard = await openWizard(page);
  await focusCamera(wizard);
  await wizard.getByRole("button", { name: "Enlarge image", exact: true }).click();
  const center = await panoramaCenter(wizard);
  const zoom = await wizard.locator(".cameraPanoramaViewHeader").first().textContent();
  const original = await currentJob(request);
  const canvas = planPanel(wizard).locator("canvas");
  const world = { x: 5.8, z: 1.2 };
  for (const rotation of [90, 180, 270, 0]) {
    const before = await browserDraft(page);
    await wizard.getByRole("button", { name: "Rotate composition by 90 degrees", exact: true }).click();
    expect((await browserDraft(page)).drafts).toEqual(before.drafts);
    await canvas.click({ position: await planPosition(wizard, world, rotation) });
    const marked = (await browserDraft(page)).drafts[0].world;
    expect(marked.x).toBeCloseTo(world.x, 1);
    expect(marked.z).toBeCloseTo(world.z, 1);
    await planPanel(wizard).focus();
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("Enter");
    const keyboard = (await browserDraft(page)).drafts[0].world;
    expect(keyboard.x).toBeCloseTo(world.x, 1);
    expect(keyboard.z).toBeCloseTo(world.z, 1);
  }
  const returned = await panoramaCenter(wizard);
  expect(returned.x).toBeCloseTo(center.x, 6);
  expect(returned.y).toBeCloseTo(center.y, 6);
  expect(await wizard.locator(".cameraPanoramaViewHeader").first().textContent()).toBe(zoom);
  expect((await currentJob(request)).revision).toBe(original.revision);
  expect((await fixture(request)).commands).toEqual([]);
});

test("fourth unsaved pair starts ghosts, pinned probes survive navigation and incomplete additions", async ({ page, request }) => {
  const known = await fixture(request);
  await seedLegacyPanorama(request);
  const saved = await seedPoints(request, known.points.slice(0, 3));
  const wizard = await openWizard(page);
  await focusCamera(wizard);
  await enterPoint(wizard, known.points[3]);
  await expect(wizard.getByTestId("panorama-preview-status")).toHaveText("Move the pointer or tap to compare the two views");
  await wizard.getByRole("button", { name: "Finish adjustment", exact: true }).click();
  const canvas = planPanel(wizard).locator("canvas");
  await wizard.locator(".cameraPanoramaImageContent").click({ position: await imagePosition(wizard, known.points[6].panorama) });
  await pointNavigation(wizard).hover();
  await expect(canvas).toHaveAttribute("data-panorama-ghost-world", /./);
  await expect(wizard.getByTestId("plan-probe")).toBeVisible();
  const pinned = await canvas.getAttribute("data-panorama-ghost-world");
  const requests = [];
  page.on("request", request => { if (/\/(preview|points)$/.test(new URL(request.url()).pathname)) requests.push(request.url()); });
  for (let turn = 0; turn < 4; turn++) {
    await wizard.getByRole("button", { name: "Rotate composition by 90 degrees", exact: true }).click();
    await expect(canvas).toHaveAttribute("data-panorama-ghost-world", pinned);
  }
  await wizard.getByRole("button", { name: "Enlarge image", exact: true }).click();
  await expect(canvas).toHaveAttribute("data-panorama-ghost-world", pinned);
  await wizard.getByRole("button", { name: "Add point", exact: true }).click();
  await expect(canvas).toHaveAttribute("data-panorama-ghost-world", pinned);
  await wizard.getByRole("button", { name: "Remove place 5", exact: true }).click();
  await page.emulateMedia({ reducedMotion: "reduce" });
  expect(await wizard.getByTestId("plan-probe").evaluate(element => getComputedStyle(element, "::before").animationName)).toBe("none");
  expect(requests).toEqual([]);
  expect((await currentJob(request)).revision).toBe(saved.revision);
  expect((await currentJob(request)).points).toEqual(saved.points);
  await screenshot(page, "fourth-unsaved-persistent-probe");
});

test("live preview serializes requests, rejects stale results and recovers after failure", async ({ page, request }) => {
  const known = await fixture(request);
  await seedLegacyPanorama(request);
  const saved = await seedPoints(request, known.points.slice(0, 4));
  const wizard = await openWizard(page);
  await focusCamera(wizard);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let calls = 0;
  let started;
  const firstStarted = new Promise(resolve => { started = resolve; });
  await page.route("**/panorama/*/preview", async route => {
    calls++;
    if (calls === 1) {
      const response = await route.fetch();
      started();
      await held;
      await route.fulfill({ response });
    } else await route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"Synthetic preview failure"}' });
  });
  await pointButton(wizard, 1).click();
  await markPlan(wizard, { x: 3, z: -3 });
  await firstStarted;
  await markPlan(wizard, { x: 3.5, z: -3 });
  // Wait past the debounce using a rendered state, then verify only the held request exists.
  await expect(wizard.getByTestId("panorama-preview-status")).toContainText("Updating");
  await page.waitForTimeout(350);
  expect(calls).toBe(1);
  release();
  await expect(wizard.getByTestId("panorama-preview-status")).toContainText("Could not update");
  expect(calls).toBe(2);
  expect((await currentJob(request)).points).toEqual(saved.points);
  await expect(wizard.getByTestId("panorama-ghost")).toHaveCount(0);
  await page.unroute("**/panorama/*/preview");
  const response = page.waitForResponse(response => response.url().endsWith("/preview"));
  await markPlan(wizard, { x: 3.2, z: -3 });
  expect((await response).ok()).toBe(true);
  await wizard.locator(".cameraPanoramaImageContent").hover({ position: await imagePosition(wizard, known.points[6].panorama) });
  await expect(planPanel(wizard).locator("canvas")).toHaveAttribute("data-panorama-ghost-world", /./);
  expect((await currentJob(request)).revision).toBe(saved.revision);
});

test("hover shows the actual composition target and leaves both viewport rectangles stable", async ({ page, request }) => {
  const known = await fixture(request);
  await seedLegacyPanorama(request);
  await seedPoints(request, known.points.slice(0, 6));
  const wizard = await openWizard(page);
  await focusCamera(wizard);
  const canvas = planPanel(wizard).locator("canvas");
  const probe = wizard.getByTestId("plan-probe");
  const rectangles = () => wizard.locator(".cameraPanoramaViewport").evaluateAll(elements => elements.map(element => {
    const {x, y, width, height} = element.getBoundingClientRect(); return {x, y, width, height};
  }));
  const baseline = await rectangles();
  for (const point of known.points.slice(6)) {
    const position = await planPosition(wizard, point.world);
    await wizard.locator(".cameraPanoramaImageContent").hover({position: await imagePosition(wizard, point.panorama)});
    await expect(probe).toBeVisible();
    const panel = await canvas.boundingBox();
    await expect.poll(async () => {
      const marker = await probe.boundingBox();
      return marker ? Math.hypot(marker.x + marker.width / 2 - panel.x - position.x, marker.y + marker.height / 2 - panel.y - position.y) : Infinity;
    }).toBeLessThan(3);
    expect(await rectangles()).toEqual(baseline);
    const clip = await probe.boundingBox();
    const shown = await page.screenshot({clip, animations: "disabled"});
    await pointNavigation(wizard).hover();
    await expect(probe).toBeHidden();
    const hidden = await page.screenshot({clip, animations: "disabled"});
    const changedPixels = await page.evaluate(async ([shown, hidden]) => {
      const decode = async encoded => {
        const image = new Image(); image.src = `data:image/png;base64,${encoded}`; await image.decode();
        const canvas = document.createElement("canvas"); canvas.width = image.width; canvas.height = image.height;
        const context = canvas.getContext("2d"); context.drawImage(image, 0, 0);
        return context.getImageData(0, 0, image.width, image.height).data;
      };
      const [left, right] = await Promise.all([decode(shown), decode(hidden)]);
      let changed = 0;
      for (let index = 0; index < left.length; index += 4) {
        if (Math.abs(left[index]-right[index])+Math.abs(left[index+1]-right[index+1])+Math.abs(left[index+2]-right[index+2]) > 50) changed++;
      }
      return changed;
    }, [shown.toString("base64"), hidden.toString("base64")]);
    expect(changedPixels, "The target must be painted above the composition, not merely present in the DOM").toBeGreaterThan(60);
  }
});

test("continuous mapping keeps both views stable and centres only a pinned target without changing scale", async ({ page, request }) => {
  const known = await fixture(request);
  await seedLegacyPanorama(request);
  const saved = await seedPoints(request, known.points.slice(0, 6));
  const wizard = await openWizard(page);
  await focusCamera(wizard);
  await expect(wizard.locator('.cameraPanoramaStages, .cameraPanoramaViewSwitch')).toHaveCount(0);
  await expect(wizard.locator('.cameraPanoramaView')).toHaveCount(2);
  await expect(wizard.getByRole('button', {name: 'Use this mapping', exact: true})).toBeEnabled();
  const centerImage = wizard.getByRole('button', {name: 'Center target in panorama', exact: true});
  const centerPlan = wizard.getByRole('button', {name: 'Center target in composition', exact: true});
  await expect(centerImage).toBeDisabled();
  await expect(centerPlan).toBeDisabled();
  const image = wizard.locator('.cameraPanoramaImageContent');
  const canvas = planPanel(wizard).locator('canvas');
  const probe = wizard.getByTestId('plan-probe');
  const point = known.points[6];
  const neighbour = known.points[7];
  const markerPosition = async () => {
    const marker = await probe.boundingBox();
    const panel = await canvas.boundingBox();
    return {x: marker.x + marker.width / 2 - panel.x, y: marker.y + marker.height / 2 - panel.y};
  };
  await image.hover({position: await imagePosition(wizard, point.panorama)});
  await expect(probe).toBeVisible();
  await expect(centerPlan).toBeDisabled(); // Hover is never a navigation instruction.
  const first = await markerPosition();
  await image.hover({position: await imagePosition(wizard, neighbour.panorama)});
  await expect.poll(async () => Math.hypot((await markerPosition()).x - first.x, (await markerPosition()).y - first.y)).toBeGreaterThan(5);
  const second = await markerPosition();
  await image.click({position: await imagePosition(wizard, point.panorama)});
  await expect(centerPlan).toBeEnabled();
  const imageBefore = await panoramaCenter(wizard);
  const bounds = await canvas.boundingBox();
  for (let index = 0; index < 4; index++) {
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.mouse.down();
    await page.mouse.move(bounds.x + bounds.width / 2 - 120, bounds.y + bounds.height / 2, {steps: 4});
    await page.mouse.up();
  }
  await pointNavigation(wizard).hover();
  await expect(wizard.getByTestId('panorama-preview-status')).toContainText('Target outside');
  const geometry = () => wizard.locator('.cameraPanoramaViewport, .cameraPanoramaFooter').evaluateAll(elements => elements.map(element => {
    const {x,y,width,height}=element.getBoundingClientRect();
    return {x,y,width,height,scrollWidth:element.scrollWidth,scrollHeight:element.scrollHeight};
  }));
  const before = await geometry();
  for (const location of [point.panorama, neighbour.panorama, {x:.5,y:.1}, point.panorama]) {
    const target = await imagePosition(wizard, location);
    const imageBounds = await image.boundingBox();
    const view = await panoramaPanel(wizard).boundingBox();
    // Move within the visible image without automatic locator scrolling.
    const x = Math.max(view.x + 10, Math.min(view.x + view.width - 10, imageBounds.x + target.x));
    const y = Math.max(view.y + 10, Math.min(view.y + view.height - 10, imageBounds.y + target.y));
    await page.mouse.move(x, y);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    expect(await geometry()).toEqual(before);
  }
  await centerPlan.click();
  await expect.poll(async () => {const p=await markerPosition();return Math.hypot(p.x-bounds.width/2,p.y-bounds.height/2);}).toBeLessThan(1);
  expect(await panoramaCenter(wizard)).toEqual(imageBefore);
  await image.hover({position: await imagePosition(wizard, neighbour.panorama)});
  await expect.poll(async () => {const p=await markerPosition(); return Math.hypot(p.x-bounds.width/2-(second.x-first.x),p.y-bounds.height/2-(second.y-first.y));}).toBeLessThan(2);
  // A second centring request after another pan works even with the same target.
  await page.mouse.move(bounds.x+bounds.width/2,bounds.y+bounds.height/2);
  await page.mouse.down(); await page.mouse.move(bounds.x+bounds.width/2+50,bounds.y+bounds.height/2,{steps:4}); await page.mouse.up();
  await centerPlan.click();
  await expect.poll(async () => {const p=await markerPosition();return Math.hypot(p.x-bounds.width/2,p.y-bounds.height/2);}).toBeLessThan(1);
  expect((await currentJob(request)).revision).toBe(saved.revision);
  await screenshot(page,'continuous-mapping-pinned-target');
});

test("two-digit place controls stay centred and responsive without decorative saved dots", async ({ page, request }) => {
  await seedLegacyPanorama(request);
  const points = Array.from({length:12}, (_, index) => {
    const world = {x:3 + index % 4, z:-3 + Math.floor(index / 4) * 3};
    return {id:`place-${index+1}`,world,panorama:{x:(Math.atan2(world.z,world.x)/Math.PI+1)/2,y:.5-Math.atan2(-3,Math.hypot(world.x,world.z))/Math.PI}};
  });
  await seedPoints(request,points);
  const wizard=await openWizard(page);
  const number=pointButton(wizard,12).locator('.cameraPanoramaPairNumber');
  await number.scrollIntoViewIfNeeded();
  await expect(number).toHaveText('12');
  expect(await number.evaluate(element=>Number(getComputedStyle(element).fontWeight))).toBeGreaterThanOrEqual(700);
  await expect(pointButton(wizard,12).locator('.cameraPanoramaPairState')).toHaveCount(0);
  const centre=await number.evaluate(element=>{const label=element.getBoundingClientRect(),button=element.parentElement.getBoundingClientRect();return Math.hypot(label.x+label.width/2-button.x-button.width/2,label.y+label.height/2-button.y-button.height/2);});
  expect(centre).toBeLessThan(1);
  for(const width of [390,320]) {
    await page.setViewportSize({width,height:844});
    await number.scrollIntoViewIfNeeded();
    await checkLayout(wizard,width);
    const footer=await wizard.locator('.cameraPanoramaFooter').boundingBox();
    const content=await wizard.boundingBox();
    expect(footer.width).toBeCloseTo(content.width,0);
    await screenshot(page,`continuous-mapping-twelve-places-${width}`);
  }
});

async function openSettingsMapping(page, request) {
  const composition = await (await request.get(`${backend}/api/composition`)).json();
  await page.goto(`/settings?panel=com.toposync.cameras&camera_id=${cameraId}`);
  return page.getByRole("button", { name: `Map camera in composition ${composition.name}`, exact: true });
}

test("camera settings shortcut opens mapping once and preserves saved pairs without camera commands", async ({ page, request }) => {
  const known = await fixture(request);
  const mapping = await openSettingsMapping(page, request);
  await mapping.focus();
  await page.keyboard.press("Enter");
  const wizard = page.getByTestId("camera-panorama-mapping");
  await expect(wizard).toBeVisible();
  await expect(wizard.getByRole("img", { name: "Captured camera panorama", exact: true })).toBeVisible();
  await wizard.getByRole("button", { name: "Add point", exact: true }).click();
  await enterPoint(wizard, known.points[0]);
  await savePoint(page, wizard);
  await page.keyboard.press("Escape");
  await expect(wizard).toHaveCount(0);
  const editor = page.getByRole("dialog", { name: "Edit: Câmera simulada", exact: true });
  await editor.getByRole("button", { name: /^(Map in composition|Continue mapping|View mapping)$/ }).click();
  await expect(wizard).toBeVisible();
  await expect(wizard.locator(".cameraPanoramaPair")).toHaveCount(1);
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");
  await expect(editor).toHaveCount(0);
  await page.getByRole("button", { name: /^Câmera simulada/ }).click();
  await page.getByRole("button", { name: "Edit Câmera simulada", exact: true }).click();
  await expect(editor).toBeVisible();
  await expect(wizard).toHaveCount(0);
  expect((await fixture(request)).commands).toEqual([]);
});

test("camera settings mapping requires an explicit element choice when the camera appears twice", async ({ page, request }) => {
  const composition = await (await request.get(`${backend}/api/composition`)).json();
  const second = structuredClone(composition.elements.find(element => element.id === elementId));
  second.id = "second-synthetic-camera-element";
  second.name = "Second synthetic camera element";
  composition.elements.push(second);
  expect((await request.put(`${backend}/api/composition`, { data: composition })).ok()).toBeTruthy();
  const mapping = await openSettingsMapping(page, request);
  await expect(mapping).toBeDisabled();
  await page.getByRole("combobox", { name: `Camera element in composition ${composition.name}`, exact: true }).selectOption(second.id);
  await expect(mapping).toBeEnabled();
  await mapping.click();
  await expect(page.getByRole("dialog", { name: `Edit: ${second.name}`, exact: true })).toBeVisible();
  await expect(page.getByTestId("camera-panorama-mapping")).toBeVisible();
  expect((await fixture(request)).commands).toEqual([]);
});

test("camera settings mapping cancellation keeps the draft and ordinary editor navigation", async ({ page, request }) => {
  const composition = await (await request.get(`${backend}/api/composition`)).json();
  const mapping = await openSettingsMapping(page, request);
  const name = page.getByTestId("camera-device-settings").locator("input.input").first();
  await name.fill("Unsaved synthetic camera");
  await mapping.click();
  const confirmation = page.locator(".settingsConfirmPanel");
  await expect(confirmation).toBeVisible();
  await confirmation.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(name).toHaveValue("Unsaved synthetic camera");
  await expect(page.getByTestId("camera-panorama-mapping")).toHaveCount(0);
  await page.getByRole("button", { name: `Open composition editor for ${composition.name}`, exact: true }).click();
  await expect(confirmation).toBeVisible();
  await confirmation.locator(".dangerButton").click();
  await expect(page).not.toHaveURL(/settings/);
  await expect(page.getByTestId("camera-panorama-mapping")).toHaveCount(0);
  expect((await fixture(request)).commands).toEqual([]);
});

test("camera settings mapping activates the target composition before opening its element", async ({ page, request }) => {
  const original = await (await request.get(`${backend}/api/composition`)).json();
  const response = await request.post(`${backend}/api/compositions`, { data: { name: "Other synthetic mapping composition" } });
  expect(response.ok()).toBeTruthy();
  const other = await response.json();
  try {
    expect((await request.post(`${backend}/api/compositions/${other.id}/activate`)).ok()).toBeTruthy();
    const second = structuredClone(original.elements.find(element => element.id === elementId));
    second.id = "other-composition-camera-element";
    second.name = "Camera in other composition";
    other.elements = [second];
    expect((await request.put(`${backend}/api/composition`, { data: other })).ok()).toBeTruthy();
    expect((await request.post(`${backend}/api/compositions/${original.id}/activate`)).ok()).toBeTruthy();
    await page.goto(`/settings?panel=com.toposync.cameras&camera_id=${cameraId}`);
    await page.getByRole("button", { name: `Map camera in composition ${other.name}`, exact: true }).click();
    await expect(page.getByRole("dialog", { name: `Edit: ${second.name}`, exact: true })).toBeVisible();
    await expect(page.getByTestId("camera-panorama-mapping")).toBeVisible();
    expect((await (await request.get(`${backend}/api/composition`)).json()).id).toBe(other.id);
    expect((await fixture(request)).commands).toEqual([]);
  } finally {
    await request.post(`${backend}/api/compositions/${original.id}/activate`);
    await request.delete(`${backend}/api/compositions/${other.id}`);
  }
});


for (const width of [1440, 390]) test(`single-frame panorama maps four pairs, activates and reopens without camera control at ${width} pixels`, async ({ page, request }) => {
  await page.setViewportSize({ width, height: 1000 });
  expect((await request.post(`${backend}/api/__panorama_fixture/static`)).ok()).toBeTruthy();
  const sourcePath = `/api/cameras/cameras/${cameraId}/sources/synthetic-wide/panorama`;
  const capture = await request.post(`${backend}${sourcePath}/jobs`, { data: { operation: "capture_static", idempotency_key: "browser-static" } });
  expect(capture.ok()).toBeTruthy();
  await expect.poll(async () => (await (await request.get(`${backend}${sourcePath}`)).json()).job?.status).toBe("ready");
  const wizard = await openWizard(page);
  await focusCamera(wizard);
  const pairs = [
    { panorama: { x: .25, y: .25 }, world: { x: 3, z: -2 } },
    { panorama: { x: .75, y: .25 }, world: { x: 7, z: -2 } },
    { panorama: { x: .75, y: .75 }, world: { x: 7, z: 2 } },
    { panorama: { x: .25, y: .75 }, world: { x: 3, z: 2 } },
  ];
  for (const point of pairs) { await enterPoint(wizard, point); await savePoint(page, wizard); }
  await expect(wizard.getByRole("button", { name: "Use this mapping", exact: true })).toBeEnabled();
  await wizard.getByRole("button", { name: "Use this mapping", exact: true }).click();
  await expect.poll(async () => (await currentJob(request))?.active).toBe(true);
  const job = await currentJob(request);
  expect(job.solution.type).toBe("panorama_image_plane_v1");
  expect(job.permissions.can_verify_aim).toBe(false);
  expect((await fixture(request)).commands).toEqual([]);
  await page.reload();
  await openWizard(page);
  await expect(wizard.getByRole("heading", { name: "Your camera is mapped", exact: true })).toBeVisible();
  await screenshot(page, `single-frame-active-mapping-${width}`);
});
