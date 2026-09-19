const { test, expect } = require("@playwright/test");
const fs = require("node:fs/promises");
const path = require("node:path");
const { resolveToposyncUrl, requestJson } = require("../frontend/packages/plugin-api/basePath");

const backend = "http://127.0.0.1:8127";
const cameraId = "panorama-simulated-camera";
const elementId = "panorama-simulated-element";
const panoramaPath = `/api/cameras/cameras/${cameraId}/panorama`;
const evidenceDirectory = path.resolve(__dirname, "../.toposync-data/panorama-validation/evidence");

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
  await page.getByText("Câmera simulada", { exact: true }).dblclick();
}

async function openWizard(page) {
  await openCameraEditor(page);
  await page.getByRole("button", { name: "Calibrate camera", exact: true }).click();
  const wizard = page.getByTestId("camera-panorama-mapping");
  await expect(wizard).toBeVisible();
  await expect(wizard.locator(".cameraPanoramaViews")).toHaveCount(1);
  const review = wizard.locator("summary").filter({ hasText: /^Review points$/ });
  if (await review.count()) await review.click();
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
const saveButton = wizard => wizard.getByRole("button", { name: "Confirm point", exact: true });
const pointNavigation = wizard => wizard.getByRole("navigation", { name: "Places", exact: true });
const pointButton = (wizard, number, role = "fit") => pointNavigation(wizard).getByRole("button", { name: `${role === "check" ? "Check" : "Adjust"} place ${number}`, exact: true });

async function browserDraft(page) {
  return page.evaluate(key => JSON.parse(localStorage.getItem(key)), draftKey);
}

async function openPointDetails(wizard) {
  const details = wizard.locator("details").filter({ hasText: "Help with marking and precision" });
  if ((await details.getAttribute("open")) === null) await details.locator("summary").click();
}

async function focusCamera(wizard) {
  await expect(planPanel(wizard)).toBeVisible();
  const focus = wizard.getByRole("button", { name: "Focus camera", exact: true });
  if (await focus.isVisible()) {
    await focus.click();
    const canvas = planPanel(wizard).locator("canvas");
    const bounds = await canvas.boundingBox();
    const page = wizard.page();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    // Pan from the camera to the middle of the synthetic floor, exercising navigation too.
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.mouse.down();
    await page.mouse.move(bounds.x + bounds.width / 2 - 170, bounds.y + bounds.height / 2, { steps: 6 });
    await page.mouse.up();
  }
  await expect(wizard.getByRole("button", { name: "Whole composition", exact: true })).toBeVisible();
}

async function planPosition(wizard, world, rotation = 0) {
  // Focus camera followed by the visible pan gesture centres x=5 at 34 pixels per metre.
  const bounds = await planPanel(wizard).locator("canvas").boundingBox();
  const dx = (world.x - 5) * 34;
  const dz = world.z * 34;
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

test("legacy panorama review, six fits, independent checks, activation, aim and geometry invalidation", async ({ page, request }) => {
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
  for (const [index, point] of known.points.slice(0, 6).entries()) {
    await enterPoint(wizard, point, index % 2 ? "plan" : "panorama");
    await savePoint(page, wizard);
  }
  await screenshot(page, "flow-03-six-linked-1440-day");
  expect((await fixture(request)).commands).toHaveLength(captureCommands);
  await expect(pointButton(wizard, 7, "check")).toHaveAttribute("aria-current", "step");
  await expect(saveButton(wizard)).toBeDisabled();
  await expect(wizard.getByRole("button", { name: "Review and activate", exact: true })).toHaveCount(0);
  for (const point of known.points.slice(6)) {
    await enterPoint(wizard, point);
    await savePoint(page, wizard);
  }
  expect((await currentJob(request)).solution.quality.status).toBe("ready");
  expect(compositionWrites).toEqual([]);
  const checks = (await currentJob(request)).points.filter(point => point.role === "check");
  for (const [index, point] of checks.entries()) {
    await pointButton(wizard, index + 7, "check").click();
    await expect(pointButton(wizard, index + 7, "check")).toHaveAttribute("aria-current", "step");
    await wizard.getByRole("button", { name: "Point camera", exact: true }).click();
    await expect(wizard.getByRole("img", { name: "Camera image after pointing to the check place", exact: true })).toBeVisible();
    if (index === 0) {
      await screenshot(page, "flow-04-independent-check-1440-day");
      await wizard.getByRole("button", { name: "I could not verify", exact: true }).click();
      await expect(wizard.getByRole("button", { name: "Review and activate", exact: true })).toBeDisabled();
      await expect.poll(async () => (await currentJob(request)).checks[0]?.result).toBe("unverifiable");
    }
    await wizard.getByRole("button", { name: "It is correct", exact: true }).click();
    await expect.poll(async () => (await currentJob(request)).checks.filter(check => check.result === "correct").length).toBe(index + 1);
  }
  await wizard.getByRole("button", { name: "Review and activate", exact: true }).click();
  await wizard.getByRole("button", { name: "Finish calibration", exact: true }).click();
  await expect(wizard.getByRole("heading", { name: "Your camera is mapped", exact: true })).toBeVisible();
  expect((await currentJob(request)).active).toBe(true);
  await wizard.locator("summary").filter({ hasText: /^Verify camera pointing$/ }).click();
  await markPlan(wizard, { x: 5, z: 0 });
  await wizard.getByRole("button", { name: "Point camera", exact: true }).click();
  await expect(wizard.getByText("The camera completed the movement.", { exact: true })).toBeVisible();
  expect((await fixture(request)).commands).toHaveLength(captureCommands + 3);

  // Visual evidence, not golden snapshots: exercise actual saved theme settings.
  for (const theme of ["day", "night"]) {
    if (theme === "night") {
      await page.evaluate(() => localStorage.setItem("toposync.theme", "topo-night"));
      wizard = await openWizard(page);
      await expect(wizard.getByRole("heading", { name: "Your camera is mapped", exact: true })).toBeVisible();
      await wizard.locator("summary").filter({ hasText: /^Verify camera pointing$/ }).click();
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

  await pointButton(wizard, 2).click();
  await markPlan(wizard, known.points[1].world);
  const secondId = (await browserDraft(page)).selectedPointId;
  await wizard.getByRole("button", { name: "Add point", exact: true }).click();
  await markImage(wizard, known.points[2].panorama);
  const thirdId = (await browserDraft(page)).selectedPointId;
  expect(thirdId).not.toBe(secondId);
  await wizard.getByRole("button", { name: "Previous point", exact: true }).click();
  await expect(pointButton(wizard, 2)).toHaveAttribute("aria-current", "step");
  await expect(saveButton(wizard)).toBeDisabled();
  await wizard.getByRole("button", { name: "Next point", exact: true }).click();
  await expect(pointButton(wizard, 3)).toHaveAttribute("aria-current", "step");
  await expect(saveButton(wizard)).toBeDisabled();
  // Selecting an image marker opens its saved pair without overwriting either unfinished pair.
  await wizard.locator(".cameraPanoramaImageContent").getByRole("button", { name: "Adjust place 1", exact: true }).click();
  await expect(pointButton(wizard, 1)).toHaveAttribute("aria-current", "step");
  await pointButton(wizard, 2).click();
  await page.reload();
  wizard = await openWizard(page);
  await expect(wizard.getByText("Your unfinished marks were restored. Continue where you left off.", { exact: true })).toBeVisible();
  await expect(pointButton(wizard, 2)).toHaveAttribute("aria-current", "step");
  const restored = await browserDraft(page);
  expect(restored.selectedPointId).toBe(secondId);
  expect(restored.drafts.find(point => point.id === secondId).panorama).toBeNull();
  expect(restored.drafts.find(point => point.id === thirdId).world).toBeNull();
  await markImage(wizard, known.points[1].panorama);
  await savePoint(page, wizard);
  expect((await currentJob(request)).points[1].id).toBe(secondId);
  await expect(pointButton(wizard, 3)).toHaveAttribute("aria-current", "step");
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
  await expect(pointButton(wizard, 2)).toHaveAttribute("aria-current", "step");
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
  expect(seeded.solution.quality.status).not.toBe("ready");
  const wizard = await openWizard(page);
  await pointButton(wizard, 1).click();
  await focusCamera(wizard);
  const canvas = planPanel(wizard).locator("canvas");
  const ghost = wizard.getByTestId("panorama-ghost");
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
    await canvas.hover({ position: await planPosition(wizard, point.world) });
    await expect(ghost).toBeVisible();
    await expect(canvas).not.toHaveAttribute("data-panorama-ghost-world", /./);
    const projected = await ghost.evaluate(marker => ({ x: Number.parseFloat(marker.style.left) / 100, y: Number.parseFloat(marker.style.top) / 100 }));
    expect(projected.x).toBeCloseTo(point.panorama.x, 3);
    expect(projected.y).toBeCloseTo(point.panorama.y, 3);
  }
  await screenshot(page, "preview-01-provisional-1440-day");
  for (const rotation of [90, 180, 270, 0]) {
    await wizard.getByRole("button", { name: "Rotate composition by 90 degrees", exact: true }).click();
    await canvas.hover({ position: await planPosition(wizard, known.points[6].world, rotation) });
    await expect(ghost).toBeVisible();
    const projected = await ghost.evaluate(marker => ({ x: Number.parseFloat(marker.style.left) / 100, y: Number.parseFloat(marker.style.top) / 100 }));
    expect(projected.x).toBeCloseTo(known.points[6].panorama.x, 3);
    expect(projected.y).toBeCloseTo(known.points[6].panorama.y, 3);
  }
  await canvas.hover({ position: await planPosition(wizard, { x: 1, z: 0 }) });
  await expect(ghost).toHaveCount(0);
  await expect(canvas).not.toHaveAttribute("data-panorama-ghost-world", /./);
  await screenshot(page, "preview-02-unsupported-region-1440-day");
  await canvas.hover({ position: await planPosition(wizard, known.points[6].world) });
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
  await wizard.locator(".cameraPanoramaImageContent").hover({ position: await imagePosition(wizard, known.points[6].panorama) });
  await expect(canvas).not.toHaveAttribute("data-panorama-ghost-world", /./);
  expect((await currentJob(request)).points).toEqual(seeded.points);
});

test("independent check places, missing coverage and stale revision context never receive a suggestion", async ({ page, request }) => {
  const known = await fixture(request);
  await seedLegacyPanorama(request);
  await seedPoints(request, known.points);
  let wizard = await openWizard(page);
  await focusCamera(wizard);
  const commandCount = (await fixture(request)).commands.length;
  await pointButton(wizard, 7, "check").click();
  await wizard.locator(".cameraPanoramaImageContent").hover({ position: await imagePosition(wizard, known.points[7].panorama) });
  await expect(planPanel(wizard).locator("canvas")).not.toHaveAttribute("data-panorama-ghost-world", /./);
  await planPanel(wizard).locator("canvas").hover({ position: await planPosition(wizard, known.points[7].world) });
  await expect(wizard.getByTestId("panorama-ghost")).toHaveCount(0);
  await expect(wizard.getByRole("button", { name: "Point camera", exact: true })).toBeEnabled();
  await screenshot(page, "preview-03-independent-check-1440-day");
  expect((await fixture(request)).commands).toHaveLength(commandCount);

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
  await expect(wizard.getByText("0 places linked · 0 check places", { exact: true })).toBeVisible();
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
  await page.getByRole("button", { name: "Calibrate camera", exact: true }).click();
  const wizard = page.getByTestId("camera-panorama-mapping");
  await expect(wizard.getByRole("button", { name: "Use saved panorama", exact: true })).toHaveCount(0);
  await wizard.locator("summary").filter({ hasText: /^Review points$/ }).click();
  await expect(pointButton(wizard, 1)).toHaveAttribute("aria-current", "step");
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
  await expect(wizard.getByText("Link six places and two independent checks. Lens and motor settings are not required.", { exact: true })).toBeVisible();
  expect((await fixture(request)).commands).toHaveLength(0);
  await screenshot(page, "source-01-first-saved-pair-1440-day");
  for (const point of known.points.slice(1, 6)) {
    await enterPoint(wizard, point);
    await savePoint(page, wizard);
  }
  for (const point of known.points.slice(6)) {
    await enterPoint(wizard, point);
    await savePoint(page, wizard);
  }
  await expect(wizard.getByRole("button", { name: "Finish calibration", exact: true })).toBeEnabled();
  await screenshot(page, "simulation-01-eight-points-ready");
  await wizard.getByRole("button", { name: "Finish calibration", exact: true }).click();
  const active = await currentJob(request);
  expect(active.active).toBe(true);
  expect(active.permissions.map_validated).toBe(true);
  expect(active.permissions.aim_enabled).toBe(false);
  await screenshot(page, "simulation-02-calibration-active");
  await expect(wizard.getByRole("button", { name: "Point camera", exact: true })).not.toBeVisible();
  await wizard.locator("summary").filter({ hasText: /^Verify camera pointing$/ }).click();
  await wizard.getByRole("button", { name: "Fit image", exact: true }).click();
  // Independent floor locations, absent from both the six fit and two check pairs.
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
  await expect(wizard.getByRole("button", { name: "Point camera", exact: true })).toBeDisabled();
  await wizard.getByRole("button", { name: "Verify camera pointing", exact: true }).click();
  await expect(wizard.getByRole("button", { name: "Point camera", exact: true })).toBeEnabled();
  expect((await fixture(request)).commands).toHaveLength(0);
  await screenshot(page, "source-02-active-map-before-pointing-1440-day");
});

test("one calibration entry skips preparation and resumes the same draft", async ({ page, request }) => {
  let wizard = await openWizard(page);
  await expect(wizard.getByRole("img", { name: "Captured camera panorama", exact: true })).toBeVisible();
  const job = await currentJob(request);
  await expect(wizard.getByRole("button", { name: "Use saved panorama", exact: true })).toHaveCount(0);
  await expect(wizard.getByRole("img", { name: "Captured camera panorama", exact: true })).toBeVisible();
  wizard = await openWizard(page);
  expect((await currentJob(request)).id).toBe(job.id);
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

test("preparation stays inside the assistant and continues when a panorama becomes available", async ({ page, request }) => {
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
  await page.getByRole("button", { name: "Calibrate camera", exact: true }).click();
  const wizard = page.getByTestId("camera-panorama-mapping");
  await expect(wizard.getByTestId("camera-source-panorama")).toBeVisible();
  await expect(wizard.getByRole("button", { name: "Generate panorama", exact: true })).toBeEnabled();
  expect(captureRequests).toEqual([]);
  published = true;
  await expect(wizard.getByRole("img", { name: "Captured camera panorama", exact: true })).toBeVisible({ timeout: 20_000 });
  await expect(wizard.getByTestId("camera-source-panorama")).toHaveCount(0);
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
  await expect(wizard.getByRole("img", { name: "Captured camera panorama", exact: true })).not.toBeVisible();
  await wizard.getByRole("button", { name: "Review calibration", exact: true }).click();
  await expect(wizard.getByRole("img", { name: "Captured camera panorama", exact: true })).toBeVisible();
  const draft = await currentJob(request);
  expect(draft.id).not.toBe(created.id);
  expect(draft.points).toEqual(known.points);
  expect(draft.active).toBe(false);
  expect((await (await request.get(`${backend}${panoramaPath}/${created.id}`)).json()).active).toBe(true);
  await focusCamera(wizard);
  await markPlan(wizard, { x: known.points[0].world.x + .1, z: known.points[0].world.z });
  await expect(saveButton(wizard)).toBeEnabled();
  await savePoint(page, wizard);
  expect((await (await request.get(`${backend}${panoramaPath}/${created.id}`)).json()).points).toEqual(known.points);
  expect((await fixture(request)).commands).toHaveLength(0);
});

test("incompatible panorama stays in preparation without creating or capturing", async ({ page, request }) => {
  await page.route(`**${panoramaPath}?*`, async route => {
    const response = await route.fetch();
    const context = await response.json();
    context.blockers = ["source_panorama_revision_conflict"];
    context.sources.forEach(source => { source.panorama = { id: source.panorama.id, compatible: false, blockers: context.blockers }; });
    await route.fulfill({ response, json: context });
  });
  const writes = [];
  page.on("request", request => { if (request.method() === "POST" && request.url().includes("/panorama")) writes.push(request.url()); });
  await openCameraEditor(page);
  await page.getByRole("button", { name: "Calibrate camera", exact: true }).click();
  const wizard = page.getByTestId("camera-panorama-mapping");
  await expect(wizard.getByTestId("camera-source-panorama")).toBeVisible();
  await expect(wizard.getByRole("group", { name: "Selected useful area of the camera panorama", exact: true })).toBeVisible();
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

test("i18n preserves point drafts and localizes errors, help and singular counts", async ({ page, request }) => {
  const known = await fixture(request);
  await openCameraEditor(page);
  await page.getByRole("button", { name: "Calibrate camera", exact: true }).click();
  let wizard = page.getByTestId("camera-panorama-mapping");
  await expect(wizard.getByRole("button", { name: "Use saved panorama", exact: true })).toHaveCount(0);
  await wizard.locator("summary").filter({ hasText: /^Review points$/ }).click();
  await focusCamera(wizard);
  await enterPoint(wizard, known.points[0], "plan");
  const pending = (await browserDraft(page)).drafts[0];

  await page.evaluate(() => localStorage.setItem("toposync.locale", "pt-BR"));
  await page.reload();
  await page.getByRole("button", { name: "Editar", exact: true }).click();
  await page.getByText("Câmera simulada", { exact: true }).dblclick();
  await page.getByRole("button", { name: "Calibrar câmera", exact: true }).click();
  wizard = page.getByTestId("camera-panorama-mapping");
  await wizard.locator("summary").filter({ hasText: /^Revisar pontos$/ }).click();
  await expect(wizard.getByRole("navigation", { name: "Pontos", exact: true })).toBeVisible();
  await expect(wizard.getByRole("group", { name: /^Panorâmica: use as setas/ })).toBeVisible();
  await expect(wizard.getByRole("group", { name: /^Composição: use as setas/ })).toBeVisible();
  await expect(wizard.getByRole("button", { name: "Confirmar ponto", exact: true })).toBeEnabled();
  expect((await browserDraft(page)).drafts[0]).toEqual(pending);

  await page.route("**/panorama/*/points", route => route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: { code: "revision_conflict", message: "Synthetic revision conflict diagnostic" } }) }));
  await wizard.getByRole("button", { name: "Confirmar ponto", exact: true }).click();
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
  await expect(wizard.getByText("1 lugar ligado · 0 lugares de conferência", { exact: true })).toBeVisible();
  await expect(wizard).not.toContainText("coordenadas abaixo");
  await expect(wizard).not.toContainText("ext.cameras.");
  await screenshot(page, "i18n-01-points-pt-BR");

  await page.evaluate(() => localStorage.setItem("toposync.locale", "en"));
  wizard = await openWizard(page);
  await expect(wizard.getByText("1 place linked · 0 check places", { exact: true })).toBeVisible();
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
