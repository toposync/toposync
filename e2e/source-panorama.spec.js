const { test, expect } = require("@playwright/test");
const fs = require("node:fs/promises");
const path = require("node:path");
const { resolveToposyncUrl, requestJson } = require("../frontend/packages/plugin-api/basePath");

// Loopback fixture routes + real application UI. No real cameras or user data.
const backend = "http://127.0.0.1:8108";
const resource = "/api/cameras/cameras/source-panorama-synthetic-camera/sources/synthetic-wide/panorama";
const fixturePath = "/api/__source_panorama_fixture";
const evidenceDirectory = process.env.TOPOSYNC_SOURCE_PANORAMA_EVIDENCE_DIRECTORY
  ? path.resolve(process.env.TOPOSYNC_SOURCE_PANORAMA_EVIDENCE_DIRECTORY)
  : path.resolve(__dirname, "../.toposync-data/source-panorama-validation");

async function fixture(request, action = "", data) {
  const response = action ? await request.post(`${backend}${fixturePath}/${action}`, { data }) : await request.get(`${backend}${fixturePath}`);
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  expect(body.synthetic).toBe(true);
  return body;
}

async function state(request) {
  const response = await request.get(`${backend}${resource}`);
  expect(response.ok()).toBeTruthy();
  return response.json();
}

async function openPanorama(page) {
  await page.goto("/settings");
  if (await page.locator(".settingsMobileNavigation select").isVisible()) {
    await page.locator(".settingsMobileNavigation select").selectOption("com.toposync.cameras");
  } else {
    await page.getByRole("button", { name: /^Cameras\b/ }).click();
  }
  const section = page.getByTestId("camera-source-panorama");
  await expect(section).toBeVisible();
  await section.scrollIntoViewIfNeeded();
  await expect(section.getByText("Synthetic courtyard · Synthetic wide stream", { exact: true })).toBeVisible();
  return section;
}

async function generate(page, request, scenario = "ready") {
  await fixture(request, "scenario", { scenario });
  const section = await openPanorama(page);
  await expect(section.locator("input, select, textarea")).toHaveCount(0);
  await section.getByRole("button", { name: /^Generate (a new )?panorama$/ }).click();
  await expect.poll(async () => (await state(request)).job?.captures_accepted).toBeGreaterThan(0);
  return section;
}

async function ready(page, request) {
  const section = await generate(page, request);
  await expect(section.getByRole("img", { name: "Saved camera panorama", exact: true })).toBeVisible();
  await expect.poll(async () => (await state(request)).job.status).toBe("ready");
  return section;
}

async function screenshot(page, name) {
  await fs.mkdir(evidenceDirectory, { recursive: true });
  await page.screenshot({ path: path.join(evidenceDirectory, `${name}.png`), fullPage: true });
}

async function checkLayout(section, width) {
  const bounds = await section.evaluate(element => ({
    width: element.getBoundingClientRect().width,
    overflow: element.scrollWidth - element.clientWidth,
  }));
  expect(bounds.width).toBeLessThanOrEqual(width);
  expect(bounds.overflow).toBeLessThanOrEqual(1);
  const undersized = await section.locator("button:visible, a:visible").evaluateAll(elements => elements.map(element => ({
    label: element.getAttribute("aria-label") || element.textContent,
    rectangle: element.getBoundingClientRect(),
  })).filter(({ rectangle }) => rectangle.width < 43.9 || rectangle.height < 43.9).map(({ label }) => label));
  expect(undersized).toEqual([]);
}

test.beforeEach(async ({ page, request }) => {
  await fixture(request, "reset");
  await page.addInitScript(() => {
    if (!localStorage.getItem("toposync.locale")) localStorage.setItem("toposync.locale", "en");
    if (!localStorage.getItem("toposync.theme")) localStorage.setItem("toposync.theme", "topo-day");
  });
});

for (const partial of [false, true]) test(`completed capture becomes the single current panorama and collapses process details${partial ? " with return pending" : ""}`, async ({ page, request }) => {
  const source = await state(request);
  const artifact = {
    id: "a".repeat(32), revision: 1, status: "ready", created_at: Date.now() / 1000,
    width: 2, height: 2, image_url: "/api/cameras/panorama-artifacts/" + "a".repeat(32) + "/files/panorama",
    coverage_url: "/api/cameras/panorama-artifacts/" + "a".repeat(32) + "/files/coverage",
    crop: { u_start: 0, u_width: 1, v_start: 0, v_height: 1 }, crop_revision: 1,
    coverage: { acquisition_complete: !partial }, quality: { status: "ready", reasons: [] },
    quality_approved: true, presentation: { status: "verified" }, stale: false,
  };
  const job = {
    id: "b".repeat(32), camera_id: source.camera_id, source_id: source.source_id,
    status: partial ? "partial" : "ready", phase: "complete", operation: "capture", captures_accepted: 12,
    physical_state: partial ? "stopped" : "restored", artifact_id: artifact.id, can_resume: false,
    can_reconstruct: partial, can_return: partial, can_cleanup: false,
    outcomes: { acquisition: partial ? "incomplete" : "completed", reconstruction: "ready", return: partial ? "unverified" : "verified" },
    created_at: Date.now() / 1000, updated_at: Date.now() / 1000, issues: [],
  };
  await page.route(`**${resource}`, route => route.fulfill({ json: { ...source, active: artifact, candidate: null, job } }));
  await page.route(`**${artifact.image_url}`, route => route.fulfill({
    contentType: "image/png",
    body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR42mNkYGD4z8DAwMDAxAADAA0HAQFLmVKMAAAAAElFTkSuQmCC", "base64"),
  }));
  const section = await openPanorama(page);

  if (!partial) await expect(section.getByText("Panorama updated.", { exact: true })).toBeVisible();
  if (partial) {
    const limitations = section.getByTestId("panorama-limitations");
    await expect(limitations).toBeVisible();
    await limitations.locator("summary").click();
    await expect(section.getByRole("button", { name: "Return to the starting view", exact: true })).toBeVisible();
    await expect(section.getByTestId("panorama-independent-outcomes")).not.toBeVisible();
  }
  await expect(section.getByRole("button", { name: "Update panorama", exact: true })).toBeVisible();
  await expect(section.getByRole("button", { name: "Current panorama", exact: true })).toHaveCount(0);
  await expect(section.getByRole("button", { name: "New panorama", exact: true })).toHaveCount(0);
  await expect(section.getByRole("list", { name: "Panorama progress", exact: true })).toHaveCount(0);
  await expect(section.getByRole("link", { name: "Download original image", exact: true })).toHaveCount(0);

  await section.getByText("Details", { exact: true }).click();
  await expect(section.getByRole("link", { name: "Download original image", exact: true })).toBeVisible();
  await expect(section.getByTestId("panorama-result-facts")).toBeVisible();
  await expect(section.getByTestId("panorama-independent-outcomes")).toBeVisible();
});

test("a candidate replaces the current panorama only after explicit review", async ({ page, request }) => {
  const source = await state(request);
  const artifact = (id, revision) => ({
    id, revision, status: "ready", created_at: Date.now() / 1000,
    width: 640, height: 240, image_url: `/api/cameras/panorama-artifacts/${id}/files/panorama`,
    coverage_url: `/api/cameras/panorama-artifacts/${id}/files/coverage`,
    crop: { u_start: 0, u_width: 1, v_start: 0, v_height: 1 }, crop_revision: 1,
    coverage: { acquisition_complete: true }, quality: { status: "ready", reasons: [] },
    quality_approved: true, presentation: { status: "verified" }, stale: false,
  });
  const previous = artifact("c".repeat(32), 1);
  const candidate = artifact("d".repeat(32), 2);
  const job = {
    id: "e".repeat(32), camera_id: source.camera_id, source_id: source.source_id,
    status: "ready", phase: "complete", operation: "capture", captures_accepted: 12,
    physical_state: "restored", artifact_id: candidate.id, can_resume: false,
    can_reconstruct: false, can_return: false, can_cleanup: false,
    created_at: Date.now() / 1000, updated_at: Date.now() / 1000, issues: [],
  };
  let finalized = 0;
  await page.route(`**${resource}/finalize`, route => {
    expect(route.request().headers()["content-type"]).toBe("application/json");
    expect(route.request().postDataJSON()).toEqual({ artifact_id: candidate.id, expected_revision: 7 });
    finalized += 1;
    return route.fulfill({ json: { ...source, revision: 8, active: candidate, previous, candidate: null, replacement_pending: false, job } });
  });
  await page.route(`**${resource}`, route => route.fulfill({ json: { ...source, revision: finalized ? 8 : 7, active: finalized ? candidate : previous, candidate: finalized ? null : candidate, candidate_selectable: true, replacement_pending: false, job } }));
  await page.route("**/api/cameras/panorama-artifacts/*/files/panorama", route => route.fulfill({
    contentType: "image/svg+xml",
    body: '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="240"><rect width="640" height="240" fill="#9fcdd7"/><path d="M0 180 L100 120 L220 160 L390 80 L640 180 V240 H0Z" fill="#698d69"/></svg>',
  }));

  const section = await openPanorama(page);
  await section.getByRole("button", { name: "New panorama", exact: true }).click();
  await expect(section.getByRole("button", { name: "Use this panorama", exact: true })).toBeEnabled();
  await expect(section.getByRole("button", { name: "New panorama", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(section.getByText("Panorama updated.", { exact: true })).toHaveCount(0);
  await expect(section.getByTestId("panorama-native-references")).toHaveCount(0);
  expect(finalized).toBe(0);
  await section.getByRole("button", { name: "Use this panorama", exact: true }).scrollIntoViewIfNeeded();
  await screenshot(page, "candidate-review-desktop");
  await page.setViewportSize({ width: 390, height: 844 });
  await checkLayout(section, 390);
  await section.getByRole("button", { name: "Use this panorama", exact: true }).scrollIntoViewIfNeeded();
  await screenshot(page, "candidate-review-mobile");
  await section.getByRole("button", { name: "Use this panorama", exact: true }).click();
  await expect.poll(() => finalized).toBe(1);
  await expect(section.getByText("Panorama updated.", { exact: true })).toBeVisible();
  await expect(section.getByRole("button", { name: "Current panorama", exact: true })).toHaveCount(0);
  await expect(section.getByRole("button", { name: "New panorama", exact: true })).toHaveCount(0);
});

test("zero geometry fields, background job survives navigation, saved artifact persists", async ({ page, request }) => {
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  let section = await generate(page, request, "hold");
  await expect(section.getByRole("img", { name: "Last accepted photograph from the camera capture", exact: true })).toBeVisible();
  await expect(section.getByText("Waiting for the image to settle", { exact: true })).toBeVisible();
  await expect(section.getByRole("button", { name: "Stop", exact: true })).toBeEnabled();
  const initial = await state(request);
  await screenshot(page, "01-capturing-1440-day");
  await page.goto("/");
  section = await openPanorama(page);
  expect((await state(request)).job.id).toBe(initial.job.id);
  expect((await fixture(request)).events.filter(event => event.operation === "scan")).toHaveLength(1);
  await fixture(request, "release");
  await expect(section.getByRole("img", { name: "Saved camera panorama", exact: true })).toBeVisible();
  await expect(section.getByText("The starting view was restored and checked.", { exact: true })).toBeVisible();
  const completed = await state(request);
  expect(completed.active.positioning_status).toBe("not_validated");
  await page.reload();
  await expect(page.getByTestId("camera-source-panorama").getByRole("img", { name: "Saved camera panorama", exact: true })).toBeVisible();
  expect((await state(request)).active.id).toBe(completed.active.id);
  expect(errors).toEqual([]);
});

test("360 crop by two corners and keyboard round-trips without camera effects", async ({ page, request }) => {
  const section = await ready(page, request);
  const effects = (await fixture(request)).events.length;
  await section.getByRole("button", { name: "Select useful area", exact: true }).click();
  let editor = page.getByTestId("panorama-crop-editor");
  await editor.getByRole("button", { name: "Shift image to the right", exact: true }).click();
  const stage = editor.getByRole("group", { name: /^Useful area:/ });
  const bounds = await stage.boundingBox();
  await stage.click({ position: { x: bounds.width * .05, y: bounds.height * .2 } });
  await expect(editor.getByText("Now select the opposite corner.", { exact: true })).toBeVisible();
  await stage.click({ position: { x: bounds.width * .4, y: bounds.height * .8 } });
  await stage.focus();
  await stage.press("ArrowRight");
  await editor.getByRole("button", { name: "Resize bottom edge", exact: true }).focus();
  await page.keyboard.press("ArrowUp");
  const saved = page.waitForResponse(response => response.url().endsWith(`${resource}/crop`) && response.request().method() === "PATCH");
  await editor.getByRole("button", { name: "Save useful area", exact: true }).click();
  expect((await saved).ok()).toBe(true);
  await expect(section.getByRole("img", { name: "Selected useful area of the camera panorama", exact: true })).toBeVisible();
  const first = (await state(request)).active;
  expect(first.crop.u_start).toBeCloseTo(.93, 2);
  expect(first.crop.u_width).toBeCloseTo(.35, 2);
  expect(first.crop.v_start).toBeCloseTo(.2, 2);
  expect(first.crop.v_height).toBeCloseTo(.595, 2);
  expect(first.crop.u_start + first.crop.u_width).toBeGreaterThan(1);
  await section.getByRole("button", { name: "Edit useful area", exact: true }).click();
  editor = page.getByTestId("panorama-crop-editor");
  await editor.getByRole("button", { name: "Save useful area", exact: true }).click();
  await expect(section.getByRole("img", { name: "Selected useful area of the camera panorama", exact: true })).toBeVisible();
  expect((await state(request)).active.crop).toEqual(first.crop);
  expect((await fixture(request)).events).toHaveLength(effects);
  await section.getByTestId("panorama-diagnostics").locator("summary").first().click();
  const download = page.waitForEvent("download");
  await section.getByRole("button", { name: "Download useful area", exact: true }).click();
  await fs.mkdir(evidenceDirectory, { recursive: true });
  const exportPath = path.join(evidenceDirectory, "wrapped-crop-export.png");
  await (await download).saveAs(exportPath);
  const exportPixels = await page.evaluate(async data => {
    const image = new Image();
    image.src = `data:image/png;base64,${data}`;
    await image.decode();
    const canvas = document.createElement("canvas");
    canvas.width = image.width; canvas.height = image.height;
    const context = canvas.getContext("2d");
    context.drawImage(image, 0, 0);
    return { width: image.width, height: image.height, left: [...context.getImageData(0, 0, 1, 1).data], right: [...context.getImageData(image.width - 1, 0, 1, 1).data] };
  }, (await fs.readFile(exportPath)).toString("base64"));
  // Independent colored-band oracle: crossing the joining edge must concatenate
  // band 7 through band 2, with no stretched image or black filler.
  expect(exportPixels.width).toBe(Math.round(1280 * first.crop.u_width));
  expect(exportPixels.height).toBe(Math.round(320 * first.crop.v_height));
  expect(exportPixels.left).toEqual([210, 70, 166, 255]);
  expect(exportPixels.right).toEqual([110, 120, 76, 255]);
  await screenshot(page, "02-wrapped-crop-saved-1440-day");
});

test("stopping acquisition never triggers an automatic return and resume reuses the job", async ({ page, request }) => {
  let section = await generate(page, request, "hold");
  await expect.poll(async () => (await state(request)).job.captures_accepted).toBeGreaterThanOrEqual(2);
  await expect(section.getByText("Waiting for the image to settle", { exact: true })).toBeVisible();
  const identifier = (await state(request)).job.id;
  await section.getByRole("button", { name: "Stop", exact: true }).click();
  await expect(section.getByText("Capture interrupted", { exact: true })).toBeVisible();
  await expect.poll(async () => (await state(request)).job.physical_state).toBe("stopped");
  expect((await fixture(request)).events.filter(event => event.operation === "return")).toHaveLength(0);
  await expect(section.getByRole("button", { name: "Return to the starting view", exact: true })).toBeVisible();
  await fixture(request, "scenario", { scenario: "ready" });
  await section.getByRole("button", { name: "Resume capture", exact: true }).click();
  await expect(section.getByRole("img", { name: "Saved camera panorama", exact: true })).toBeVisible();
  expect((await state(request)).job.id).toBe(identifier);
  expect((await fixture(request)).events.filter(event => event.operation === "scan")).toHaveLength(2);
});

test("new partial result preserves the current complete panorama", async ({ page, request }) => {
  await ready(page, request);
  const initial = (await state(request)).active;
  const section = await generate(page, request, "partial");
  await expect(section.getByRole("button", { name: "New panorama", exact: true })).toBeVisible();
  const completed = await state(request);
  expect(completed.active.id).toBe(initial.id);
  expect(completed.candidate.id).not.toBe(initial.id);
  expect(completed.candidate.status).toBe("partial");
  await expect(section.locator('[data-complete="true"]')).toHaveCount(0);
  await expect(section.getByText("3 photographs saved", { exact: true })).toBeVisible();
  await section.getByRole("button", { name: "New panorama", exact: true }).click();
  await expect(section.getByText("Partial panorama. Some areas are missing; the selection does not fill them in.", { exact: true })).toBeVisible();
  await expect(section.getByRole("link", { name: "Download original image", exact: true })).toBeVisible();
  await section.getByRole("button", { name: "Select useful area", exact: true }).click();
  const editor = page.getByTestId("panorama-crop-editor");
  await editor.locator("summary").filter({ hasText: "Adjust selection without dragging" }).click();
  await editor.getByRole("button", { name: "Narrower", exact: true }).click();
  await editor.getByRole("button", { name: "Save useful area", exact: true }).click();
  const uncropped = section.getByRole("button", { name: "Uncropped image", exact: true });
  await expect(uncropped).toBeVisible();
  await expect(section.getByRole("button", { name: "Full panorama", exact: true })).toHaveCount(0);
  await uncropped.click();
  await expect(uncropped).toHaveAttribute("aria-pressed", "true");
  await expect(section.locator('[data-complete="true"]')).toHaveCount(0);
  await screenshot(page, "03-partial-preserved-1440-day");
  await page.reload();
  expect((await state(request)).active.id).toBe(initial.id);
});

test("failed acquisition offers recovery and does not publish an artifact", async ({ page, request }) => {
  await fixture(request, "scenario", { scenario: "failed" });
  const section = await openPanorama(page);
  await section.getByRole("button", { name: "Generate panorama", exact: true }).click();
  await expect(section.getByText("The panorama could not be completed", { exact: true })).toBeVisible();
  await expect(section.getByText("0 photographs saved", { exact: true })).toBeVisible();
  await expect(section.getByText("No photographs could be confirmed. Review the details and try again.", { exact: true })).toBeVisible();
  await expect(section).not.toContainText("photographs were kept");
  await expect(section.getByRole("button", { name: "Resume capture", exact: true })).toHaveCount(0);
  expect((await state(request)).active).toBeNull();
  await screenshot(page, "04-acquisition-error-1440-day");
  await fixture(request, "scenario", { scenario: "ready" });
  await section.getByRole("button", { name: "Generate panorama", exact: true }).click();
  await expect(section.getByRole("img", { name: "Saved camera panorama", exact: true })).toBeVisible();
});

for (const [scenario, count, saved, help] of [
  ["failed_after_one", 1, "1 photograph saved", "The usable photograph was kept. Review the details to continue."],
  ["failed_after_three", 3, "3 photographs saved", "The usable photographs were kept. Review the details to continue."],
]) {
  test(`capture failure accurately describes ${count} saved photographs`, async ({ page, request }) => {
    const section = await generate(page, request, scenario);
    await expect.poll(async () => (await state(request)).job.status).toBe("failed");
    await expect(section.getByText(saved, { exact: true })).toBeVisible();
    await expect(section.getByText(help, { exact: true })).toBeVisible();
    const failed = await state(request);
    expect(failed.job.captures_accepted).toBe(count);
    expect(failed.job.can_reconstruct).toBe(count >= 2);
    expect(failed.active).toBeNull();
  });
}

test("failed relocalization explains unavailable resume without offering a dead retry", async ({ page, request }) => {
  const section = await generate(page, request, "relocalization_failed");
  await expect.poll(async () => (await state(request)).job.status).toBe("failed");
  const failed = await state(request);
  expect(failed.job.captures_accepted).toBe(3);
  expect(failed.job.can_reconstruct).toBe(true);
  expect(failed.job.can_resume).toBe(false);
  expect(failed.job.resume_unavailable_code).toBe("relocalization_required");
  await expect(section.getByTestId("panorama-resume-unavailable")).toHaveText("The view for the pending region could not be confirmed. You can generate a new panorama.");
  await expect(section.getByTestId("panorama-resume-unavailable")).toBeVisible();
  await expect(section.getByRole("button", { name: "Resume capture", exact: true })).toHaveCount(0);
  await expect(section.getByRole("button", { name: "Try to complete capture", exact: true })).toHaveCount(0);
  await expect(section.getByRole("button", { name: "Rebuild panorama", exact: true })).toBeEnabled();
  const before = await fixture(request);
  expect(before.events.filter(event => event.operation === "relocalization")).toEqual([{ operation: "relocalization", outcome: "unconfirmed" }]);
  const retry = await request.post(`${backend}/api/cameras/panorama-jobs/${failed.job.id}/resume`);
  expect(retry.status()).toBe(409);
  expect((await retry.json()).detail.code).toBe("relocalization_required");
  expect((await fixture(request)).events).toEqual(before.events);
  expect((await state(request)).job.captures_accepted).toBe(3);
});

test("temporary positions can be explicitly cleaned without movement and retried after a removal failure", async ({ page, request }) => {
  let section = await generate(page, request, "cleanup_pending");
  await expect.poll(async () => (await state(request)).job.status).toBe("failed");
  const original = await state(request);
  expect(original.job.can_cleanup).toBe(true);
  expect(original.job.can_resume).toBe(false);
  expect(original.job.can_return).toBe(true);
  const before = await fixture(request);
  expect(before.presets).toHaveLength(1);
  let diagnostics = section.getByTestId("panorama-diagnostics");
  await expect(diagnostics.getByRole("button", { name: "Remove temporary positions", exact: true })).not.toBeVisible();
  await diagnostics.locator(":scope > summary").click();
  await expect(diagnostics.getByText("Removes only unused positions created by this capture. The camera does not move.", { exact: true })).toBeVisible();
  await fixture(request, "scenario", { scenario: "cleanup_failed" });
  await diagnostics.getByRole("button", { name: "Remove temporary positions", exact: true }).click();
  await expect(diagnostics.getByText("Some temporary positions could not be removed. You can try again.", { exact: true })).toBeVisible();
  await expect(diagnostics.locator(":scope > summary")).toBeFocused();
  expect((await state(request)).job.can_cleanup).toBe(true);
  expect((await fixture(request)).presets).toEqual(before.presets);

  await page.evaluate(() => localStorage.setItem("toposync.locale", "pt-BR"));
  await page.reload();
  await page.getByRole("button", { name: /^Câmeras\b/ }).click();
  section = page.getByTestId("camera-source-panorama");
  diagnostics = section.getByTestId("panorama-diagnostics");
  await diagnostics.locator(":scope > summary").click();
  await expect(diagnostics.getByText("Remove apenas posições sem uso criadas por esta captura. A câmera não se movimenta.", { exact: true })).toBeVisible();
  await fixture(request, "scenario", { scenario: "ready" });
  const cleanupPath = `/api/cameras/panorama-jobs/${original.job.id}/cleanup`;
  let releaseResponse;
  const responseGate = new Promise(resolve => { releaseResponse = resolve; });
  let capturedResponse = false;
  await page.route(`**${cleanupPath}`, async route => {
    const response = await route.fetch();
    capturedResponse = true;
    await responseGate;
    await route.fulfill({ response });
  });
  try {
    await diagnostics.getByRole("button", { name: "Remover posições temporárias", exact: true }).click();
    await expect.poll(() => capturedResponse).toBe(true);
    await expect(diagnostics.getByRole("button", { name: "Removendo posições temporárias…", exact: true })).toBeDisabled();
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect.poll(async () => (await state(request)).job.can_cleanup).toBe(false);
    await expect(diagnostics.getByRole("button", { name: "Removendo posições temporárias…", exact: true })).toBeVisible();
    releaseResponse();
    await expect(diagnostics.getByText("Não há mais posições temporárias com remoção pendente.", { exact: true })).toBeVisible();
    await expect(diagnostics.getByRole("button", { name: /Remov(er|endo) posições temporárias/ })).toHaveCount(0);
    await expect(diagnostics.locator(":scope > summary")).toBeFocused();
    await expect(section).not.toContainText("ext.cameras.");
  } finally {
    releaseResponse();
    await page.unroute(`**${cleanupPath}`);
  }
  const after = await fixture(request);
  expect(after.presets).toEqual([]);
  const effects = after.events.slice(before.events.length);
  expect(effects.filter(event => ["scan", "stop", "return"].includes(event.operation))).toEqual([]);
  expect(effects.filter(event => event.operation === "remove_return").map(event => event.outcome)).toEqual(["failed", "removed"]);
  const final = await state(request);
  expect(final.job.id).toBe(original.job.id);
  expect(final.job.status).toBe(original.job.status);
  expect(final.job.physical_state).toBe(original.job.physical_state);
  expect(final.job.captures_accepted).toBe(original.job.captures_accepted);
  expect(final.job.can_return).toBe(true);
  expect(final.active).toEqual(original.active);
  await screenshot(page, "temporary-position-cleanup-1440-day");
});

test("concurrent crop update preserves the draft until the user loads the saved selection", async ({ page, request }) => {
  const section = await ready(page, request);
  const active = (await state(request)).active;
  const effects = (await fixture(request)).events.length;
  await section.getByRole("button", { name: "Select useful area", exact: true }).click();
  let editor = page.getByTestId("panorama-crop-editor");
  await editor.locator("summary").filter({ hasText: "Adjust selection without dragging" }).click();
  await editor.getByRole("button", { name: "Narrower", exact: true }).click();
  await expect(editor.getByRole("group", { name: /^Useful area: 99%/ })).toBeVisible();
  const externalCrop = { u_start: .8, u_width: .4, v_start: .2, v_height: .5 };
  const response = await request.patch(`${backend}${resource}/crop`, { data: {
    artifact_id: active.id, expected_revision: active.crop_revision, crop: externalCrop,
  } });
  expect(response.ok()).toBeTruthy();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.getByRole("button", { name: "Load saved selection", exact: true })).toBeVisible();
  await expect(editor.getByRole("group", { name: /^Useful area: 99%/ })).toBeVisible();
  await expect(editor.getByRole("button", { name: "Save useful area", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Load saved selection", exact: true }).click();
  editor = page.getByTestId("panorama-crop-editor");
  await expect(editor.getByRole("group", { name: /^Useful area: 40%/ })).toBeVisible();
  expect((await state(request)).active.crop).toEqual(externalCrop);
  expect((await fixture(request)).events).toHaveLength(effects);
});

test("crop controls fit 375, 768 and 1440 in both themes with visible keyboard focus", async ({ page, request }) => {
  await ready(page, request);
  for (const theme of ["day", "night"]) {
    await page.evaluate(value => localStorage.setItem("toposync.theme", `topo-${value}`), theme);
    let section = await openPanorama(page);
    await section.getByRole("button", { name: "Select useful area", exact: true }).click();
    const editor = page.getByTestId("panorama-crop-editor");
    for (const width of [1440, 768, 375]) {
      await page.setViewportSize({ width, height: width === 375 ? 844 : 1000 });
      await editor.scrollIntoViewIfNeeded();
      await checkLayout(editor, width);
      const button = editor.getByRole("button", { name: "Save useful area", exact: true });
      await button.focus();
      await expect(button).toBeFocused();
      const focusStyle = await button.evaluate(element => ({ outline: getComputedStyle(element).outlineStyle, shadow: getComputedStyle(element).boxShadow }));
      expect(focusStyle.outline !== "none" || focusStyle.shadow !== "none").toBe(true);
      await screenshot(page, `05-crop-${width}-${theme}`);
    }
  }
});

test("panorama supports doubled text and reduced motion without clipping controls", async ({ page, request }) => {
  const section = await ready(page, request);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 375, height: 844 });
  await section.getByRole("button", { name: "Select useful area", exact: true }).click();
  const editor = page.getByTestId("panorama-crop-editor");
  // Scale computed type sizes once, avoiding multiplicative inheritance.
  await editor.evaluate(element => {
    const nodes = [element, ...element.querySelectorAll("*")];
    const values = nodes.map(node => Number.parseFloat(getComputedStyle(node).fontSize));
    nodes.forEach((node, index) => { node.style.fontSize = `${values[index] * 2}px`; });
  });
  await checkLayout(editor, 375);
  const clipped = await editor.locator("button:visible, p:visible, h2:visible, h3:visible").evaluateAll(elements => elements.filter(element => element.scrollWidth - element.clientWidth > 1).map(element => element.textContent));
  expect(clipped).toEqual([]);
  await editor.scrollIntoViewIfNeeded();
  await screenshot(page, "06-text-200-percent-375-day");
  await expect(editor.getByRole("button", { name: "Save useful area", exact: true })).toBeEnabled();
});

test("crop dialog retains keyboard focus and protects an unsaved selection on Escape", async ({ page, request }) => {
  const section = await ready(page, request);
  const effects = (await fixture(request)).events.length;
  await page.setViewportSize({ width: 375, height: 844 });
  const trigger = section.getByRole("button", { name: "Select useful area", exact: true });
  await trigger.click();
  const editor = page.getByTestId("panorama-crop-editor");
  const dialog = page.getByTestId("panorama-crop-dialog");
  await expect(dialog).toBeVisible();
  await editor.getByRole("button", { name: "Draw an area", exact: true }).focus();
  const focusSequence = [];
  for (let index = 0; index < 20; index += 1) {
    await page.keyboard.press("Tab");
    const focus = await dialog.evaluate(element => ({ inside: element.contains(document.activeElement), pageFocused: document.hasFocus(), tag: document.activeElement?.tagName, label: document.activeElement?.getAttribute("aria-label"), text: document.activeElement?.textContent?.slice(0, 120) }));
    focusSequence.push({ tab: index + 1, ...focus });
  }
  await fs.mkdir(evidenceDirectory, { recursive: true });
  await fs.writeFile(path.join(evidenceDirectory, "dialog-focus-sequence.json"), JSON.stringify(focusSequence, null, 2));
  // Native dialogs can yield to browser chrome. No application control outside
  // the modal may receive focus while it is open.
  expect(focusSequence.filter(focus => !focus.inside && (focus.pageFocused || focus.tag !== "BODY"))).toEqual([]);
  await editor.locator("summary").filter({ hasText: "Adjust selection without dragging" }).click();
  await editor.getByRole("button", { name: "Narrower", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  await expect(editor.getByText("Your selection has not been saved yet.", { exact: true })).toBeVisible();
  await editor.getByRole("button", { name: "Close without saving", exact: true }).click();
  await expect(editor).not.toBeVisible();
  await expect(trigger).toBeFocused();
  expect((await state(request)).active.crop).toEqual({ u_start: 0, u_width: 1, v_start: 0, v_height: 1 });
  expect((await fixture(request)).events).toHaveLength(effects);
});

test("panorama URLs retain the Home Assistant ingress prefix", async () => {
  const previousWindow = global.window;
  const previousFetch = global.fetch;
  const calls = [];
  global.window = { __TOPOSYNC_PUBLIC_BASE_PATH__: "/api/hassio_ingress/synthetic", location: { href: "http://localhost/api/hassio_ingress/synthetic/settings", origin: "http://localhost" } };
  global.fetch = async (url, options) => { calls.push({ url, options }); return { ok: true, status: 200, json: async () => ({}) }; };
  try {
    expect(resolveToposyncUrl(resource)).toBe(`/api/hassio_ingress/synthetic${resource}`);
    expect(resolveToposyncUrl("/api/cameras/panorama-artifacts/abc/files/panorama")).toBe("/api/hassio_ingress/synthetic/api/cameras/panorama-artifacts/abc/files/panorama");
    await requestJson(`${resource}/jobs`, { method: "POST", body: JSON.stringify({ idempotency_key: "synthetic-only" }) });
    expect(calls[0].url).toBe(`/api/hassio_ingress/synthetic${resource}/jobs`);
  } finally { global.window = previousWindow; global.fetch = previousFetch; }
});

test("settings deep link keeps acquisition and publication on the selected camera and source", async ({ page, request }) => {
  await page.goto("/settings?panel=com.toposync.cameras&camera_id=source-panorama-other-camera&source_id=synthetic-tele");
  const section = page.getByTestId("camera-source-panorama");
  await expect(section).toBeVisible();
  await expect(section.getByText("Another synthetic camera · Synthetic tele stream", { exact: true })).toBeVisible();
  await expect(section.locator("input, select, textarea")).toHaveCount(0);
  await section.getByRole("button", { name: "Generate panorama", exact: true }).click();
  await expect(section.getByRole("img", { name: "Saved camera panorama", exact: true })).toBeVisible();
  const response = await request.get(`${backend}${resource.replace("source-panorama-synthetic-camera", "source-panorama-other-camera").replace("synthetic-wide", "synthetic-tele")}`);
  expect(response.ok()).toBeTruthy();
  const selected = await response.json();
  expect(selected.active.source_id).toBe("synthetic-tele");
  expect(selected.active.camera_id).toBe("source-panorama-other-camera");
  expect((await state(request)).active).toBeNull();
  expect((await fixture(request)).events.find(event => event.operation === "open_camera").source_id).toBe("synthetic-tele");
  expect((await fixture(request)).events.find(event => event.operation === "open_camera").camera_id).toBe("source-panorama-other-camera");
});

test("Stop remains available when the selected source is disabled during capture", async ({ page, request }) => {
  const section = await generate(page, request, "hold");
  const identifier = (await state(request)).job.id;
  try {
    const observed = page.waitForResponse(response => response.url().endsWith(`/api/cameras/panorama-jobs/${identifier}`) && response.request().method() === "GET");
    await page.getByRole("checkbox", { name: "Enabled", exact: true }).uncheck();
    expect((await observed).ok()).toBeTruthy();
    const stop = section.getByRole("button", { name: "Stop", exact: true });
    await expect(stop).toBeEnabled();
    await stop.click();
    const job = async () => {
      const response = await request.get(`${backend}/api/cameras/panorama-jobs/${identifier}`);
      expect(response.ok()).toBeTruthy();
      return (await response.json()).job;
    };
    await expect.poll(async () => (await job()).physical_state).toBe("stopped");
    expect((await fixture(request)).events.filter(event => event.operation === "return")).toHaveLength(0);
  } finally {
    await request.post(`${backend}/api/cameras/panorama-jobs/${identifier}/stop`);
  }
});

test("a delayed create response cannot prevent Stop after polling discovers the job", async ({ page, request }) => {
  await fixture(request, "scenario", { scenario: "hold" });
  const section = await openPanorama(page);
  let releaseResponse;
  const responseGate = new Promise(resolve => { releaseResponse = resolve; });
  let capturedResponse = false;
  await page.route(`**${resource}/jobs`, async route => {
    const response = await route.fetch();
    capturedResponse = true;
    await responseGate;
    await route.fulfill({ response });
  });
  let identifier;
  try {
    await section.getByRole("button", { name: "Generate panorama", exact: true }).click();
    await expect.poll(() => capturedResponse).toBe(true);
    await expect.poll(async () => (await state(request)).job?.captures_accepted).toBeGreaterThanOrEqual(2);
    identifier = (await state(request)).job.id;
    // The browser has not received the POST response yet. An independent GET
    // must still make this server-owned capture observable and stoppable.
    const stop = section.getByRole("button", { name: "Stop", exact: true });
    await expect(stop).toBeVisible();
    await expect(stop).toBeEnabled();
    await stop.click();
    await expect.poll(async () => (await state(request)).job.status).toBe("interrupted");
    expect((await fixture(request)).events.filter(event => event.operation === "return")).toHaveLength(0);
    const delivered = page.waitForResponse(response => response.url().endsWith(`${resource}/jobs`) && response.request().method() === "POST");
    releaseResponse();
    await (await delivered).finished();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await expect(section.getByText("Capture interrupted", { exact: true })).toBeVisible();
    expect((await state(request)).job.id).toBe(identifier);
  } finally {
    releaseResponse();
    if (identifier) await request.post(`${backend}/api/cameras/panorama-jobs/${identifier}/stop`);
    await page.unroute(`**${resource}/jobs`);
  }
});

test("movement diagnostic renders 128 observations without inventing speed when media time is absent", async ({ page, request }) => {
  await fixture(request, "scenario", { scenario: "hold", telemetry: true });
  const section = await openPanorama(page);
  await section.getByRole("button", { name: "Generate panorama", exact: true }).click();
  await expect.poll(async () => (await state(request)).job?.telemetry?.samples?.length).toBe(128);
  const data = (await state(request)).job.telemetry;
  expect(data.timing_basis).toBe("local_observation");
  expect(data.samples.every(sample => sample.media_time === null && sample.speed_px_s === null)).toBe(true);
  expect(data.samples[127].elapsed_seconds).toBe(15.875);
  await page.setViewportSize({ width: 375, height: 844 });
  const diagnostic = page.getByTestId("panorama-telemetry");
  const details = section.locator("details").filter({ has: diagnostic });
  const summary = details.locator(":scope > summary");
  await summary.focus();
  await expect(summary).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(diagnostic.getByRole("heading", { name: "Last observed movement", exact: true })).toBeVisible();
  await expect(diagnostic.locator("svg")).toHaveAttribute("role", "img");
  await expect(diagnostic.locator("svg")).toHaveAccessibleName(/.+/);
  const plot = diagnostic.getByRole("region", { name: "Movement and visual support chart. Scroll horizontally if needed.", exact: true });
  await plot.focus();
  await expect(plot).toBeFocused();
  const focus = await plot.evaluate(element => ({ outline: getComputedStyle(element).outlineStyle, shadow: getComputedStyle(element).boxShadow }));
  expect(focus.outline !== "none" || focus.shadow !== "none").toBe(true);
  await plot.press("ArrowRight");
  await expect.poll(() => plot.evaluate(element => element.scrollLeft)).toBeGreaterThan(0);
  await plot.press("ArrowLeft");
  await expect.poll(() => plot.evaluate(element => element.scrollLeft)).toBe(0);
  await expect(diagnostic.getByText("Image speed is unavailable without valid media timing. Local observation time is not used to infer it.", { exact: true })).toBeVisible();
  await checkLayout(diagnostic, 375);
  await expect(section.locator("input, select, textarea")).toHaveCount(0);
  await plot.scrollIntoViewIfNeeded();
  await plot.focus();
  await screenshot(page, "telemetry-128-observations-375-day-plot");
  const observationSummary = diagnostic.locator("summary").filter({ hasText: "View observation data" });
  await observationSummary.focus();
  await expect(observationSummary).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(diagnostic.locator("tbody tr")).toHaveCount(128);
  await expect(diagnostic.getByRole("columnheader", { name: "Image speed (px/s)", exact: true })).toHaveCount(0);
  await expect(diagnostic.getByText("Image speed (px/s)", { exact: true })).toHaveCount(0);
  await checkLayout(diagnostic, 375);
  await observationSummary.press("Enter");
  await diagnostic.scrollIntoViewIfNeeded();
  await screenshot(page, "telemetry-128-observations-375-day");
  await expect(section.getByRole("button", { name: "Stop", exact: true })).toBeEnabled();
  await fixture(request, "release");
  await expect.poll(async () => (await state(request)).job.status).toBe("ready");
  await expect(section.getByText("Panorama ready", { exact: true })).toBeVisible();
  await expect(diagnostic).toBeVisible();
  await fixture(request, "scenario", { scenario: "failed", telemetry: true });
  await section.getByRole("button", { name: "Generate a new panorama", exact: true }).click();
  await expect.poll(async () => (await state(request)).job.status).toBe("failed");
  await expect(section.getByText("The panorama could not be completed", { exact: true })).toBeVisible();
  await expect(diagnostic).toBeVisible();
  await expect(diagnostic.getByRole("heading", { name: "Last observed movement", exact: true })).toBeVisible();
  expect((await state(request)).job.telemetry.samples).toHaveLength(128);
  await expect(section.locator("input, select, textarea")).toHaveCount(0);
});

test("saved-image reconstruction can recover the same job without opening a camera", async ({ page, request }) => {
  const section = await generate(page, request, "reconstruction_failed");
  await expect(section.getByText("The panorama could not be completed", { exact: true })).toBeVisible();
  const failed = await state(request);
  expect(failed.job.status).toBe("failed");
  expect(failed.job.captures_accepted).toBe(3);
  expect(failed.job.can_reconstruct).toBe(true);
  expect(failed.active).toBeNull();
  const before = await fixture(request);
  expect(before.processing_events).toHaveLength(1);
  expect(before.processing_events[0].outcome).toBe("failed");
  await expect(section.getByText("Uses saved photographs without moving the camera.", { exact: true })).toBeVisible();
  await fixture(request, "scenario", { scenario: "ready" });
  await section.getByRole("button", { name: "Rebuild panorama", exact: true }).click();
  await expect(section.getByText("Panorama ready", { exact: true })).toBeVisible();
  const recovered = await state(request);
  expect(recovered.job.id).toBe(failed.job.id);
  expect(recovered.job.physical_state).toBe(failed.job.physical_state);
  expect(recovered.job.captures_accepted).toBe(3);
  expect(recovered.active.id).toBe(recovered.job.artifact_id);
  expect(recovered.active.source_id).toBe(failed.job.source_id);
  const after = await fixture(request);
  expect(after.events).toEqual(before.events);
  expect(after.processing_events).toHaveLength(2);
  expect(after.processing_events[1].capture_sha256).toEqual(before.processing_events[0].capture_sha256);
  expect(after.processing_events[1].outcome).toBe("ready");
  await screenshot(page, "reconstruct-saved-images-ready-1440-day");
  await page.reload();
  await expect(page.getByTestId("camera-source-panorama").getByText("Panorama ready", { exact: true })).toBeVisible();
  expect((await state(request)).active.id).toBe(recovered.active.id);
  expect((await fixture(request)).events).toEqual(before.events);
});

test("saved-image reconstruction stays stoppable during source outage before its POST response arrives", async ({ page, request }) => {
  const section = await generate(page, request, "reconstruction_failed");
  await expect(section.getByText("The panorama could not be completed", { exact: true })).toBeVisible();
  const original = await state(request);
  const before = await fixture(request);
  const rebuildPath = `/api/cameras/panorama-jobs/${original.job.id}/reconstruct`;
  await fixture(request, "scenario", { scenario: "reconstruction_hold" });
  let releaseResponse;
  const responseGate = new Promise(resolve => { releaseResponse = resolve; });
  let capturedResponse = false;
  await page.route(`**${rebuildPath}`, async route => {
    const response = await route.fetch();
    capturedResponse = true;
    await responseGate;
    await route.fulfill({ response });
  });
  // The job endpoint must remain independently observable if source-context
  // refresh fails while a known job is being rebuilt.
  let failedSourceRefreshes = 0;
  await page.route(`**${resource}`, async route => {
    if (route.request().method() === "GET") {
      failedSourceRefreshes += 1;
      await route.fulfill({ status: 503, json: { detail: { code: "synthetic_source_unavailable", message: "Synthetic source-context outage." } } });
    } else {
      await route.continue();
    }
  });
  try {
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect.poll(() => failedSourceRefreshes).toBeGreaterThan(0);
    await expect(section.getByText("We could not refresh the saved panorama. Showing the last confirmed state.", { exact: true })).toBeVisible();
    await screenshot(page, "reconstruct-saved-images-failed-1440-day");
    await section.getByRole("button", { name: "Rebuild panorama", exact: true }).click();
    await expect.poll(() => capturedResponse).toBe(true);
    await expect.poll(async () => (await state(request)).job.phase).toBe("reconstructing");
    const stop = section.getByRole("button", { name: "Stop processing", exact: true });
    await expect(stop).toBeEnabled();
    await stop.click();
    await expect.poll(async () => (await state(request)).job.status).toBe("interrupted");
    await expect(section.getByText("Assembly interrupted", { exact: true })).toBeVisible();
    await expect(section.getByRole("button", { name: "Rebuild panorama", exact: true })).toBeVisible();
    const interrupted = await state(request);
    expect(interrupted.job.can_reconstruct).toBe(true);
    expect(interrupted.job.id).toBe(original.job.id);
    expect(interrupted.job.physical_state).toBe(original.job.physical_state);
    expect(interrupted.job.captures_accepted).toBe(3);
    expect(interrupted.active).toBeNull();
    const after = await fixture(request);
    expect(after.events).toEqual(before.events);
    expect(after.processing_events).toHaveLength(2);
    expect(after.processing_events[1].capture_sha256).toEqual(before.processing_events[0].capture_sha256);
    expect(after.processing_events[1].outcome).toBe("cancelled");
    const delivered = page.waitForResponse(response => response.url().endsWith(rebuildPath) && response.request().method() === "POST");
    releaseResponse();
    await (await delivered).finished();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await expect(section.getByText("Assembly interrupted", { exact: true })).toBeVisible();
    await expect(section.getByText("We could not refresh the saved panorama. Showing the last confirmed state.", { exact: true })).toBeVisible();
    await expect(section.getByRole("button", { name: "Rebuild panorama", exact: true })).toBeVisible();
    await expect(section.getByRole("button", { name: "Stop processing", exact: true })).toHaveCount(0);
    expect((await state(request)).job.status).toBe("interrupted");
    expect((await fixture(request)).events).toEqual(before.events);
    await screenshot(page, "reconstruct-saved-images-interrupted-1440-day");
  } finally {
    releaseResponse();
    await request.post(`${backend}/api/cameras/panorama-jobs/${original.job.id}/stop`);
    await page.unroute(`**${rebuildPath}`);
    await page.unroute(`**${resource}`);
  }
});

test("panorama quality review remains visible through editing and saving the useful area", async ({ page, request }) => {
  for (const quality of ["independent_alignment_error", "optimizer_budget_reached", "review", "malformed_reasons"]) {
    const warning = quality === "independent_alignment_error"
      ? "Some parts of this image did not align well. Review the result before using it; cropping does not correct the alignment."
      : "The assembly could not be fully verified. Review the image before using it; cropping only changes the area shown.";
    const jobWarning = quality === "independent_alignment_error"
      ? "Some parts of the latest assembly did not align well. This result was kept for review."
      : "The latest assembly could not be fully verified. This result was kept for review.";
    await fixture(request, "reset");
    await fixture(request, "scenario", { scenario: "ready", quality });
    const section = await openPanorama(page);
    await section.getByRole("button", { name: "Generate panorama", exact: true }).click();
    await expect(section.getByText("Panorama available for review", { exact: true })).toBeVisible();
    const captured = await state(request);
    expect(captured.active).toBeNull();
    expect(captured.candidate.quality_approved).toBe(false);
    expect(captured.candidate.quality.status).toBe(quality === "malformed_reasons" ? "ready" : "review");
    expect(captured.candidate.quality.reasons).toEqual(["review", "malformed_reasons"].includes(quality) ? [] : [quality]);
    await expect(section.locator('[data-complete="true"]')).toHaveCount(0);
    await expect(section.getByTestId("panorama-quality-warning")).toBeVisible();
    await expect(section.getByTestId("panorama-quality-warning")).toHaveText(warning);
    await expect(section.getByTestId("panorama-job-quality-warning")).toHaveText(jobWarning);
    await expect(section.getByText(/Some areas are missing/)).toHaveCount(0);
    await expect(section.getByRole("img", { name: "Saved camera panorama", exact: true })).toBeVisible();
    await expect(section.getByRole("link", { name: "Download original image", exact: true })).toBeVisible();
    const before = await fixture(request);
    await section.getByRole("button", { name: "Select useful area", exact: true }).click();
    const editor = page.getByTestId("panorama-crop-editor");
    await expect(editor.getByTestId("panorama-crop-quality-warning")).toBeVisible();
    await expect(editor.getByTestId("panorama-crop-quality-warning")).toHaveText(warning);
    if (quality === "independent_alignment_error") {
      await screenshot(page, "quality-independent-alignment-inside-crop-1440-day");
    }
    await editor.locator("summary").filter({ hasText: "Adjust selection without dragging" }).click();
    await editor.getByRole("button", { name: "Narrower", exact: true }).click();
    await expect(editor.getByRole("button", { name: "Save useful area", exact: true })).toBeEnabled();
    await editor.getByRole("button", { name: "Save useful area", exact: true }).click();
    await expect(section.getByTestId("panorama-quality-warning")).toBeVisible();
    await expect(section.getByRole("button", { name: "Download useful area", exact: true })).toBeEnabled();
    await expect(section.getByText("Panorama available for review", { exact: true })).toBeVisible();
    expect((await state(request)).candidate.crop.u_width).toBeCloseTo(.99);
    expect((await state(request)).candidate.quality).toEqual(captured.candidate.quality);
    expect((await fixture(request)).events).toEqual(before.events);
    await section.getByTestId("panorama-quality-warning").scrollIntoViewIfNeeded();
    await screenshot(page, `quality-${quality}-cropped-1440-day`);
    await page.reload();
    await expect(page.getByTestId("camera-source-panorama").getByTestId("panorama-quality-warning")).toBeVisible();
    expect((await state(request)).active).toBeNull();
    expect((await state(request)).candidate.id).toBe(captured.candidate.id);
  }
});

test("panorama quality review applies to the selected candidate and preserves the good current panorama", async ({ page, request }) => {
  const warning = "Some parts of this image did not align well. Review the result before using it; cropping does not correct the alignment.";
  const section = await ready(page, request);
  const initial = (await state(request)).active;
  await expect(section.getByTestId("panorama-quality-warning")).toHaveCount(0);
  await fixture(request, "scenario", { scenario: "ready", quality: "independent_alignment_error" });
  await section.getByRole("button", { name: "Generate a new panorama", exact: true }).click();
  await expect(section.getByText("Panorama available for review", { exact: true })).toBeVisible();
  const captured = await state(request);
  expect(captured.active.id).toBe(initial.id);
  expect(captured.active.quality).toEqual(initial.quality);
  expect(captured.candidate.quality.reasons).toEqual(["independent_alignment_error"]);
  await expect(section.getByRole("button", { name: "Current panorama", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(section.getByTestId("panorama-quality-warning")).toHaveCount(0);
  await expect(section.getByTestId("panorama-job-quality-warning")).toHaveText("Some parts of the latest assembly did not align well. This result was kept for review.");
  await section.getByRole("button", { name: "Select useful area", exact: true }).click();
  let editor = page.getByTestId("panorama-crop-editor");
  await expect(editor.getByTestId("panorama-crop-quality-warning")).toHaveCount(0);
  await editor.getByRole("button", { name: "Close editor", exact: true }).last().click();
  await section.getByRole("button", { name: "New panorama", exact: true }).click();
  await expect(section.getByTestId("panorama-quality-warning")).toBeVisible();
  await expect(section.getByTestId("panorama-quality-warning")).toHaveText(warning);
  const before = await fixture(request);
  await section.getByRole("button", { name: "Select useful area", exact: true }).click();
  editor = page.getByTestId("panorama-crop-editor");
  await expect(editor.getByTestId("panorama-crop-quality-warning")).toBeVisible();
  await expect(editor.getByTestId("panorama-crop-quality-warning")).toHaveText(warning);
  await editor.locator("summary").filter({ hasText: "Adjust selection without dragging" }).click();
  await editor.getByRole("button", { name: "Narrower", exact: true }).click();
  await editor.getByRole("button", { name: "Save useful area", exact: true }).click();
  await expect(section.getByTestId("panorama-quality-warning")).toBeVisible();
  expect((await state(request)).candidate.crop.u_width).toBeCloseTo(.99);
  expect((await state(request)).active.id).toBe(initial.id);
  expect((await fixture(request)).events).toEqual(before.events);
  await screenshot(page, "quality-candidate-cropped-1440-day");
  await section.getByRole("button", { name: "Current panorama", exact: true }).click();
  await expect(section.getByTestId("panorama-quality-warning")).toHaveCount(0);
  await expect(section.getByRole("img", { name: "Saved camera panorama", exact: true })).toBeVisible();
  await screenshot(page, "quality-good-current-with-review-candidate-1440-day");
});

test("scanner stage describes recovery and vertical exploration while the reference band remains incomplete", async ({ page, request }) => {
  const section = await ready(page, request);
  const source = await state(request);
  const evidence = await fixture(request);
  const job = {
    ...source.job, status: "capturing", phase: "waiting_for_stability", physical_state: "stopped",
    coverage_progress: { primary_complete: false, bands_completed: 0, current_band: 0,
      stage: "pan", regions_pending: 1, continued_after_recovery: false },
  };
  await page.route(`**${resource}`, route => route.fulfill({ json: { ...source, job } }));
  await page.route(`**/api/cameras/panorama-jobs/${job.id}`, route => route.fulfill({ json: { job } }));
  async function refresh() { await page.evaluate(() => window.dispatchEvent(new Event("focus"))); }
  await refresh();
  await expect(section.getByText("Capturing the reference band", { exact: true })).toBeVisible();
  await expect(section.getByTestId("panorama-continuing-with-gaps")).toHaveCount(0);
  job.coverage_progress.stage = "return_reference";
  await refresh();
  await expect(section.getByText("Recovering a confirmed reference", { exact: true })).toBeVisible();
  await expect(section.getByTestId("panorama-continuing-with-gaps")).toHaveCount(0);
  job.coverage_progress.stage = "step";
  await refresh();
  await expect(section.getByText("Exploring another height", { exact: true })).toBeVisible();
  await expect(section.getByTestId("panorama-continuing-with-gaps")).toHaveCount(0);
  job.coverage_progress.continued_after_recovery = true;
  await refresh();
  await expect(section.getByTestId("panorama-continuing-with-gaps")).toBeVisible();
  await expect(section.getByText("Reference band captured", { exact: true })).toHaveCount(0);
  job.coverage_progress.stage = "pan";
  job.coverage_progress.current_band = 1;
  await refresh();
  await expect(section.getByText("Capturing this horizontal band", { exact: true })).toBeVisible();
  await expect(section.getByRole("button", { name: "Stop", exact: true })).toBeEnabled();
  await expect(section.getByTestId("panorama-camera-state")).toHaveText("Capture in progress.");
  expect((await fixture(request)).events).toEqual(evidence.events);
});

test("displayed revision keeps image quality, coverage and current camera state independent", async ({ page, request }) => {
  const section = await ready(page, request);
  const source = await state(request);
  const active = { ...source.active, status: "partial", coverage: { ...source.active.coverage,
    acquisition_complete: false, acquisition: { bands: { 0: { complete: true }, 1: { complete: false } } } } };
  delete active.quality;
  delete active.quality_approved;
  const candidate = { ...active, id: "synthetic-review-candidate",
    coverage: { ...active.coverage, acquisition_complete: true, acquisition: { progress: { bands_completed: 3, regions_pending: 0 } } } };
  const job = { ...source.job, artifact_id: candidate.id, physical_state: "stopped" };
  await page.route(`**${resource}`, route => route.fulfill({ json: { ...source, active, candidate, job } }));
  async function refresh() { await page.evaluate(() => window.dispatchEvent(new Event("focus"))); }
  await refresh();
  await expect(section.getByTestId("panorama-image-state")).toHaveText("Available");
  await expect(section.getByTestId("panorama-coverage-state")).toHaveText("1 confirmed band · Regions remain pending");
  await expect(section.getByTestId("panorama-camera-state")).toHaveText("Camera stopped; exact return not confirmed.");
  await expect(section.locator('[data-complete="true"]')).toHaveCount(0);
  await expect(section.getByTestId("panorama-quality-warning")).toHaveCount(0);
  await section.getByRole("button", { name: "New panorama", exact: true }).click();
  await expect(section.getByTestId("panorama-image-state")).toHaveText("Assembly needs review");
  await expect(section.getByTestId("panorama-coverage-state")).toHaveText("3 confirmed bands · Reach confirmed");
  await expect(section.getByTestId("panorama-quality-warning")).toBeVisible();
  job.physical_state = "stop_unconfirmed";
  await refresh();
  await expect(section.getByTestId("panorama-camera-state")).toHaveText("Stop not confirmed.");
  await expect(section.getByTestId("panorama-coverage-state")).toHaveText("3 confirmed bands · Reach confirmed");
  await section.getByRole("button", { name: "Current panorama", exact: true }).click();
  await expect(section.getByTestId("panorama-image-state")).toHaveText("Available");
  await expect(section.getByTestId("panorama-coverage-state")).toHaveText("1 confirmed band · Regions remain pending");
});

test("initial-region result shows six confirmed views without claiming total reach", async ({ page, request }) => {
  const section = await ready(page, request);
  const source = await state(request);
  const artifact = {
    ...source.active,
    id: "synthetic-initial-region",
    capture_goal: "initial_region",
    region_status: "ready",
    coverage: { ...source.active.coverage, acquisition_complete: false },
  };
  const job = {
    ...source.job,
    artifact_id: artifact.id,
    status: "partial",
    physical_state: "restored",
    capture_goal: "initial_region",
    captures_accepted: 6,
    coverage_progress: { qualified_views: 6, required_views: 6 },
  };
  await page.route(`**${resource}`, route => route.fulfill({ json: { ...source, active: artifact, candidate: null, job } }));
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(section.getByRole("status")).toHaveText("Initial region ready");
  await expect(section.getByText("6 photographs saved · 6 of 6 views confirmed", { exact: true })).toBeVisible();
  await expect(section.getByTestId("panorama-coverage-state")).toHaveText("Initial region · total reach undetermined");
  await expect(section.getByText("This image covers the initial region. It does not establish the camera’s full reach. Check whether the lower area you need is visible.", { exact: true })).toBeVisible();
  await expect(section.getByRole("button", { name: "Select useful area", exact: true })).toBeEnabled();
});

test("photographed bounds fit presentation while whole image and keyboard crop keep canonical coordinates", async ({ page, request }) => {
  const section = await ready(page, request);
  const before = await state(request);
  const evidence = await fixture(request);
  const bounds = { left: 320, top: 80, right: 960, bottom: 240 };
  const withBounds = artifact => ({ ...artifact, coverage: { ...artifact.coverage, bounds_pixels: bounds } });
  let cropWrites = 0;
  await page.route(`**${resource}`, async route => {
    const response = await route.fetch();
    const source = await response.json();
    await route.fulfill({ response, json: { ...source, active: withBounds(source.active) } });
  });
  await page.route(`**${resource}/crop`, async route => {
    cropWrites += 1;
    const response = await route.fetch();
    const result = await response.json();
    await route.fulfill({ response, json: { ...result, artifact: withBounds(result.artifact) } });
  });
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(section.getByRole("img", { name: "Photographed area of the camera panorama", exact: true })).toBeVisible();
  const picture = section.locator(".sourcePanoramaImage > img").first();
  await expect(picture).toHaveAttribute("style", /width: 200%;.*height: 200%;.*left: -50%;.*top: -50%/);
  expect(cropWrites).toBe(0);
  await section.getByRole("button", { name: "Uncropped image", exact: true }).click();
  await expect(section.getByRole("img", { name: "Saved camera panorama", exact: true })).toBeVisible();
  await expect(picture).toHaveAttribute("style", /width: 100%;.*height: 100%;/);
  await section.getByRole("button", { name: "Photographed area", exact: true }).click();
  await expect(picture).toHaveAttribute("style", /width: 200%;.*height: 200%;/);
  await section.getByText("Details", { exact: true }).click();
  await expect(section.getByRole("link", { name: "Download original image", exact: true })).toHaveAttribute("href", before.active.image_url);
  await section.getByText("Details", { exact: true }).click();
  await section.getByRole("button", { name: "Select useful area", exact: true }).click();
  let editor = page.getByTestId("panorama-crop-editor");
  const stage = editor.getByRole("group", { name: /^Useful area: 100% of image width and 100% of image height/ });
  await expect(stage).toBeVisible();
  // A full-frame move keeps its canonical coordinates and enters adjustment mode.
  await stage.focus();
  await stage.press("ArrowRight");
  await editor.getByRole("button", { name: "Resize right edge", exact: true }).focus();
  await page.keyboard.press("ArrowLeft");
  await editor.getByRole("button", { name: "Save useful area", exact: true }).click();
  await expect(section.getByRole("img", { name: "Selected useful area of the camera panorama", exact: true })).toBeVisible();
  expect((await state(request)).active.crop).toEqual({ u_start: 0, u_width: 0.995, v_start: 0, v_height: 1 });
  await expect(section.getByRole("button", { name: "Edit useful area", exact: true })).toBeFocused();
  await section.getByRole("button", { name: "Edit useful area", exact: true }).click();
  editor = page.getByTestId("panorama-crop-editor");
  await editor.getByRole("button", { name: "Use the whole image", exact: true }).click();
  await editor.getByRole("button", { name: "Save useful area", exact: true }).click();
  await expect(section.getByRole("img", { name: "Saved camera panorama", exact: true })).toBeVisible();
  await expect(section.getByRole("button", { name: "Photographed area", exact: true })).toHaveCount(0);
  expect(cropWrites).toBe(2);
  const after = await state(request);
  expect(after.active.coverage).toEqual(before.active.coverage);
  expect(after.active.image_url).toBe(before.active.image_url);
  expect((await fixture(request)).events).toEqual(evidence.events);
});

test("completed regional acquisition awaiting review does not claim failed relocalization", async ({ page, request }) => {
  const source = await state(request);
  const before = await fixture(request);
  const job = {
    id: "synthetic-regional-completed", camera_id: source.camera_id, source_id: source.source_id,
    operation: "capture", capture_goal: "initial_region", status: "partial", phase: "partial", captures_accepted: 22,
    physical_state: "stopped", can_resume: false, can_return: true,
    resume_unavailable_code: "relocalization_required", created_at: 1, updated_at: 1,
    outcomes: { acquisition: "completed", reconstruction: "review", return: "unverified" },
    coverage_progress: { policy_version: 4, qualified_views: 12, region_complete: false,
      decision: { route_complete: true, sufficient: false } },
  };
  await page.route(`**${resource}`, route => route.fulfill({ json: { ...source, job } }));
  const section = await openPanorama(page);
  await expect(section.getByTestId("panorama-resume-unavailable")).toHaveCount(0);
  await expect(section).not.toContainText("0 of 6");
  await expect(section).toContainText("12");
  expect((await fixture(request)).events).toEqual(before.events);
});

test("control verification distinguishes checks from photographs in both languages", async ({ page, request }) => {
  const source = await state(request);
  const before = await fixture(request);
  const job = {
    id: "synthetic-control-verification", camera_id: source.camera_id, source_id: source.source_id,
    operation: "verify_control", status: "failed", phase: "control_unconfirmed",
    captures_accepted: 0, control_checks_passed: 2, physical_state: "restored",
    can_resume: false, can_return: false, resume_unavailable_code: "resume_unavailable",
    created_at: 1, updated_at: 1, error: { code: "visual_control_unverified" },
  };
  await page.route(`**${resource}`, route => route.fulfill({ json: { ...source, job } }));
  let section = await openPanorama(page);
  await expect(section.getByText("2 checks completed", { exact: true })).toBeVisible();
  await expect(section.getByText("Movement could not be confirmed", { exact: true })).toBeVisible();
  await expect(section).not.toContainText("photographs saved");
  await expect(section.getByTestId("panorama-resume-unavailable")).toHaveCount(0);
  await page.evaluate(() => localStorage.setItem("toposync.locale", "pt-BR"));
  await page.reload();
  await page.getByRole("button", { name: /^Câmeras\b/ }).click();
  section = page.getByTestId("camera-source-panorama");
  await expect(section.getByText("2 verificações concluídas", { exact: true })).toBeVisible();
  await expect(section.getByText("Não foi possível confirmar os movimentos", { exact: true })).toBeVisible();
  await expect(section).not.toContainText("fotografias guardadas");
  await expect(section).not.toContainText("{count}");
  expect((await fixture(request)).events).toEqual(before.events);
});

test("source panorama localizes diagnostic codes, singular counts and numbers without camera commands", async ({ page, request }) => {
  const source = await state(request);
  const before = await fixture(request);
  const job = {
    id: "synthetic-i18n-job", camera_id: source.camera_id, source_id: source.source_id,
    status: "failed", phase: "failed", captures_accepted: 1, physical_state: "stopped",
    can_resume: false, can_return: false, created_at: 1, updated_at: 1,
    coverage_progress: { primary_complete: true, bands_completed: 1, current_band: 0 },
    error: { code: "stability_timeout", message: "Original server diagnostic." },
    issue_codes: ["return_framing_unconfirmed", "future_diagnostic"],
    issues: ["Diagnóstico preservado do servidor."],
    telemetry: { kind: "movement", outcome: "timeout", timing_basis: "local_observation", analysis_width: 1000,
      samples: [{ elapsed_seconds: 1.5, motion_pixels: 12.5, drift_pixels: 0.25, confidence: 0.375,
        media_time: null, speed_px_s: null, state: "timeout", pose: { native_pan: 1.25 } }] },
  };
  await page.route(`**${resource}`, route => route.fulfill({ json: { ...source, job } }));
  let section = await openPanorama(page);
  await expect(section.getByText("1 photograph saved · 1 band completed", { exact: true })).toBeVisible();
  await expect(section.getByText("The usable photograph was kept. Review the details to continue.", { exact: true })).toBeVisible();
  await expect(section.getByText("The stability of some images could not be confirmed in time.", { exact: true })).toBeVisible();
  let diagnostics = section.getByTestId("panorama-diagnostics");
  await diagnostics.locator(":scope > summary").click();
  await expect(diagnostics.getByText("The starting camera view could not be confirmed.", { exact: true })).toBeVisible();
  await expect(diagnostics.getByText("A step could not be confirmed. Review the original diagnostic for details.", { exact: true })).toBeVisible();
  await expect(diagnostics.getByText(/^1 observation from the latest movement, on images analyzed at 1,000 pixels wide\./)).toBeVisible();
  await expect(section.getByText("Original server diagnostic.", { exact: true })).not.toBeVisible();
  await expect(diagnostics.getByText("Diagnóstico preservado do servidor.", { exact: true })).not.toBeVisible();
  await expect(section).not.toContainText("ext.cameras.");
  await page.evaluate(() => localStorage.setItem("toposync.locale", "pt-BR"));
  await page.reload();
  await page.getByRole("button", { name: /^Câmeras\b/ }).click();
  section = page.getByTestId("camera-source-panorama");
  await expect(section.getByText("1 fotografia guardada · 1 faixa concluída", { exact: true })).toBeVisible();
  await expect(section.getByText("A fotografia aproveitada foi guardada. Confira os detalhes para continuar.", { exact: true })).toBeVisible();
  await expect(section.getByText("Não foi possível confirmar a estabilidade de algumas imagens a tempo.", { exact: true })).toBeVisible();
  diagnostics = section.getByTestId("panorama-diagnostics");
  await diagnostics.locator(":scope > summary").click();
  await expect(diagnostics.getByText("Não conseguimos confirmar o enquadramento inicial da câmera.", { exact: true })).toBeVisible();
  await expect(diagnostics.getByText("Não foi possível confirmar uma etapa. Confira o diagnóstico original para mais detalhes.", { exact: true })).toBeVisible();
  await expect(diagnostics.getByText(/^1 observação do último movimento, em imagens analisadas com 1\.000 pixels de largura\./)).toBeVisible();
  await diagnostics.getByText("Ver dados da observação", { exact: true }).click();
  await expect(diagnostics.getByRole("cell", { name: "1,5", exact: true })).toBeVisible();
  await expect(diagnostics.getByRole("cell", { name: "37,5%", exact: true })).toBeVisible();
  await expect(diagnostics.getByRole("cell", { name: "posição horizontal nativa 1,25", exact: true })).toBeVisible();
  await expect(section).not.toContainText("ext.cameras.");
  expect((await fixture(request)).events).toEqual(before.events);
});


test("usable partial panorama groups limitations without hiding an unconfirmed stop", async ({ page, request }) => {
  const section = await ready(page, request);
  const source = await state(request);
  const active = { ...source.active, status: "partial", coverage: { ...source.active.coverage, acquisition_complete: false } };
  const job = { ...source.job, status: "partial", physical_state: "stopped", can_return: true,
    quick_approach: { status: "unavailable", reason: "return_unconfirmed" } };
  await page.route(`**${resource}`, route => route.fulfill({ json: { ...source, active, job } }));
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  const limitations = section.getByTestId("panorama-limitations");
  await expect(limitations.locator("summary")).toHaveText("Panorama available with limitations · View details");
  await expect(limitations).not.toHaveAttribute("open", "");
  await expect(section.getByRole("button", { name: "Return to the starting view", exact: true })).not.toBeVisible();
  const before = (await fixture(request)).events;
  await limitations.locator("summary").click();
  await expect(limitations.getByText(/Some regions could not be confirmed/)).toBeVisible();
  await expect(section.getByTestId("panorama-approach-status")).toContainText("No qualified automatic approach");
  expect((await fixture(request)).events).toEqual(before);
  job.physical_state = "stop_unconfirmed";
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(limitations).toHaveCount(0);
  await expect(section.getByRole("alert")).toContainText("We could not confirm that the camera stopped.");
});

for (const locale of ["en", "pt-BR"]) test(`camera settings hierarchy, technical information and themed states ${locale}`, async ({ page, request }) => {
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const settings = await (await request.get(`${backend}/api/settings`)).json();
  const extension = settings.extensions["com.toposync.cameras"];
  extension.devices[0].onvif = { ...extension.devices[0].onvif, device_id: "synthetic-device", hardware: "Synthetic hardware" };
  expect((await request.patch(`${backend}/api/settings/extensions/com.toposync.cameras`, { data: extension })).ok()).toBeTruthy();
  await ready(page, request);
  const events = (await fixture(request)).events;
  // A context response for presentation only; navigation has separate real-fixture tests.
  await page.route("**/cameras/source-panorama-synthetic-camera/contexts", route => route.fulfill({ json: {
    camera_id: "source-panorama-synthetic-camera", compositions: [{ id: "source-panorama-synthetic-composition", name: "Synthetic composition", areas: [],
      camera_elements: [{ id: "synthetic-element", name: "Synthetic element", calibrated_views: 0, control_points_pairs: 0, has_mapping: false }] }],
  } }));
  for (const theme of ["topo-day", "topo-night"]) {
    await page.evaluate(({ locale, theme }) => {
      localStorage.setItem("toposync.locale", locale);
      localStorage.setItem("toposync.theme", theme);
    }, { locale, theme });
    await page.goto("/settings?panel=com.toposync.cameras&camera_id=source-panorama-synthetic-camera&source_id=synthetic-wide");
    const panel = page.locator(".cameraSettingsPanel");
    const panorama = panel.getByTestId("camera-source-panorama");
    await expect(panorama.getByRole("img")).toBeVisible();
    expect(await panorama.evaluate(element => Boolean(element.closest(".cameraSourceDetails")))).toBe(true);
    await expect(panel.getByText(/^(Analyze an audio sample|Analisar amostra de áudio)$/)).toHaveCount(0);
    await expect(panel.getByRole("button", { name: /^(Review mapping|Revisar mapeamento)$/ })).toHaveCount(0);
    const technical = panel.locator(".cameraTechnicalDetails");
    await technical.locator("summary").click();
    await expect(technical.getByText("synthetic-device", { exact: true })).toBeVisible();
    await expect(technical.getByText("Synthetic hardware", { exact: true })).toBeVisible();
    await expect(technical.locator("input, a")).toHaveCount(0);
    const recipe = panel.locator(".cameraPipelinePresetCard:not(:disabled)").first();
    const disabledRecipe = panel.locator(".cameraPipelinePresetCard:disabled").first();
    const row = panel.locator(".cameraMappingCompositionItem");
    for (const width of [1440, 768, 375, 320]) {
      await page.setViewportSize({ width, height: 1000 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      expect(await panel.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
      await recipe.hover();
      expect(await recipe.evaluate(element => getComputedStyle(element).backgroundColor)).toBe(await row.hover().then(() => row.evaluate(element => getComputedStyle(element).backgroundColor)));
      await recipe.focus();
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Tab");
      await expect(recipe).toBeFocused();
      expect(await recipe.evaluate(element => getComputedStyle(element).outlineStyle)).not.toBe("none");
      const disabledColor = await disabledRecipe.evaluate(element => getComputedStyle(element).backgroundColor);
      await disabledRecipe.hover();
      expect(await disabledRecipe.evaluate(element => getComputedStyle(element).backgroundColor)).toBe(disabledColor);
      for (const button of await row.getByRole("button").all()) {
        const bounds = await button.boundingBox();
        expect(bounds.width).toBeGreaterThanOrEqual(44);
        expect(bounds.height).toBeGreaterThanOrEqual(44);
      }
      await panorama.scrollIntoViewIfNeeded();
      await screenshot(page, `settings-${locale}-${theme}-${width}`);
    }
  }
  expect((await fixture(request)).events).toEqual(events);
  expect(errors).toEqual([]);
});

test("camera technical information stays local and control confirmation persists", async ({ page, request }) => {
  await openPanorama(page);
  const device = page.getByTestId("camera-device-settings");
  await device.locator("summary").click();
  await expect(device.getByText("Not provided", { exact: true })).toHaveCount(2);
  const checkbox = device.getByRole("checkbox", { name: "Tracking and automatic camera movements are disabled", exact: true });
  const description = await checkbox.getAttribute("aria-describedby");
  await expect(page.locator(`[id="${description}"]`)).toContainText("monitoring point");
  await checkbox.uncheck();
  await page.getByRole("button", { name: "Save all changes", exact: true }).click();
  await expect(page.getByText(/^Unsaved:/)).toHaveCount(0);
  await page.reload();
  await expect(checkbox).not.toBeChecked();
  await checkbox.check();
  await page.getByRole("button", { name: "Save all changes", exact: true }).click();
  await expect(page.getByText(/^Unsaved:/)).toHaveCount(0);
  await page.reload();
  await expect(checkbox).toBeChecked();
  const current = await (await request.get(`${backend}/api/settings`)).json();
  const camera = current.extensions["com.toposync.cameras"].devices[0];
  camera.control.type = "none";
  camera.onvif = null;
  expect((await request.patch(`${backend}/api/settings/extensions/com.toposync.cameras`, { data: current.extensions["com.toposync.cameras"] })).ok()).toBeTruthy();
  await page.reload();
  await device.locator("summary").click();
  await expect(device.locator(".cameraTechnicalDetails").getByText("Manual / no ONVIF", { exact: true })).toBeVisible();
  await expect(checkbox).toHaveCount(0);
  expect((await fixture(request)).events).toEqual([]);
});

test("camera settings discard a late panorama response after changing source and camera", async ({ page, request }) => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let reached;
  const started = new Promise(resolve => { reached = resolve; });
  await page.route(`**${resource}`, async route => {
    const response = await route.fetch();
    reached();
    await gate;
    await route.fulfill({ response }).catch(() => {}); // The previous source request may already be aborted.
  });
  await page.goto("/settings?panel=com.toposync.cameras&camera_id=source-panorama-synthetic-camera&source_id=synthetic-wide");
  await started;
  await page.locator(".settingsSourceList").getByRole("button", { name: /^Synthetic tele stream/ }).click();
  await expect(page.getByTestId("camera-source-panorama")).toContainText("Synthetic courtyard · Synthetic tele stream");
  release();
  await expect(page.getByTestId("camera-source-panorama")).not.toContainText("Synthetic wide stream");
  await page.locator(".cameraSettingsPanel .settingsSidebar").getByRole("button", { name: /^Another synthetic camera/ }).click();
  await expect(page.getByTestId("camera-source-panorama")).toContainText("Another synthetic camera · Synthetic wide stream");
  expect((await fixture(request)).events).toEqual([]);
});


test("mapping context separates visual reference, active mapping and unqualified positioning without counting old views", async ({ page, request }) => {
  const source = await state(request);
  const queries = [];
  page.on("request", request => queries.push([request.method(), request.url()]));
  await page.route(`**/api/cameras/cameras/${source.camera_id}/contexts`, route => route.fulfill({ json: {
    camera_id: source.camera_id, compositions: [{ id: "context-map", name: "Courtyard mapping", areas: [],
      camera_elements: [{ id: "element", name: "Camera", calibrated_views: 7, control_points_pairs: 4,
        has_mapping: true, mapping: { source_id: source.source_id, revision: 13, reference: "partial",
          mapping: "active", positioning: "unqualified", reason: null, control_points_pairs: 4 } }] }],
  } }));
  for (const theme of ["topo-day", "topo-night"]) {
    for (const width of [320, 375, 768, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.addInitScript(value => localStorage.setItem("toposync.theme", value), theme);
      await openPanorama(page);
      const mapping = page.locator(".cameraMappingCompositionItem");
      await expect(mapping).toContainText("Active mapping");
      await expect(mapping).toContainText("Revision 13");
      await expect(mapping).toContainText("Positioning not qualified");
      await expect(mapping).toContainText("Partial visual reference available");
      await expect(mapping).not.toContainText("7 views");
      expect(await mapping.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
    }
  }
  expect(queries.filter(([method, url]) => method !== "GET" && /\/(aim|native-reference|jobs|ptz)\b/.test(url))).toEqual([]);
  expect(queries.filter(([, url]) => url.includes("native-reference"))).toEqual([]);
});


test("automatic approach status keeps the image usable and removes manual point preparation", async ({ page, request }) => {
  const section = await ready(page, request);
  await expect(section.getByTestId("panorama-approach-status")).toContainText("No qualified automatic approach");
  await expect(section.getByRole("img", { name: "Saved camera panorama", exact: true })).toBeVisible();
  await expect(section.getByRole("button", { name: /Prepare.*point/i })).toHaveCount(0);
});


for (const width of [1440, 390]) test(`fixed source captures a single image and saves a finite useful area without motion controls at ${width} pixels`, async ({ page, request }) => {
  await page.setViewportSize({ width, height: 1000 });
  await fixture(request, "scenario", { scenario: "static" });
  const section = await openPanorama(page);
  await expect(section.getByRole("button", { name: "Use current frame", exact: true })).toBeEnabled();
  await section.getByRole("button", { name: "Use current frame", exact: true }).click();
  await expect.poll(async () => (await state(request)).job?.status).toBe("ready");
  await expect(section.getByRole("img", { name: "Saved camera panorama", exact: true })).toBeVisible();
  await expect(section.getByRole("button", { name: /Generate.*panorama/ })).toHaveCount(0);
  await expect(section.getByRole("button", { name: /Live panorama|Inspect retired/ })).toHaveCount(0);
  const source = await state(request);
  expect(source.active.projection).toBe("image_plane");
  expect(source.job.physical_state).toBe("not_applicable");
  const evidence = await (await request.get(`${backend}${fixturePath}`)).json();
  expect(evidence.events).toEqual([{ event: "capture_static", camera_id: source.camera_id, source_id: source.source_id }]);
  await section.getByRole("button", { name: "Select useful area", exact: true }).click();
  const editor = page.getByTestId("panorama-crop-editor");
  await expect(editor.locator(".sourcePanoramaStrip img")).toHaveCount(1);
  await expect(editor.getByRole("button", { name: /seam/i })).toHaveCount(0);
  const stage = editor.locator(".sourcePanoramaCropStage");
  const box = await stage.boundingBox();
  await stage.click({ position: { x: box.width * .15, y: box.height * .2 } });
  await expect(editor.getByText("Now select the opposite corner.", { exact: true })).toBeVisible();
  await stage.click({ position: { x: box.width * .85, y: box.height * .8 } });
  await editor.getByRole("button", { name: "Save useful area", exact: true }).click();
  await expect(page.getByTestId("panorama-crop-dialog")).toHaveCount(0);
  const saved = (await state(request)).active;
  expect(saved.crop.u_start + saved.crop.u_width).toBeLessThanOrEqual(1);
  expect(saved.crop.u_width).toBeCloseTo(.7, 1);
  await page.reload();
  await expect(section.getByRole("button", { name: "Edit useful area", exact: true })).toBeVisible();
  await screenshot(page, `static-single-frame-crop-${width}`);
});


test("static spatial shader preserves perspective at interior pixels and finite crop edges", async ({ page }) => {
  await page.goto("/");
  const source = await fs.readFile(path.resolve(__dirname, "../extensions/spatial_video/ui/src/panoramaGroundLayer.ts"), "utf8");
  const fragment = source.match(/fragmentShader: `([\s\S]*?)`/)[1];
  const pixels = await page.evaluate(fragment => {
    const canvas = document.createElement("canvas"); canvas.width = 32; canvas.height = 32;
    const gl = canvas.getContext("webgl", { preserveDrawingBuffer: true });
    if (!gl) throw new Error("WebGL unavailable");
    const program = gl.createProgram();
    for (const [type, source] of [[gl.VERTEX_SHADER, "attribute vec2 position; varying vec2 world; void main(){world=(position+1.0)*5.0;gl_Position=vec4(position,0.0,1.0);}"], [gl.FRAGMENT_SHADER, "precision highp float;" + fragment]]) {
      const shader = gl.createShader(type); gl.shaderSource(shader, source); gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
      gl.attachShader(program, shader);
    }
    gl.linkProgram(program); if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
    gl.useProgram(program);
    const buffer = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(program, "position"); gl.enableVertexAttribArray(position); gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    for (const [unit, name] of [[0, "panorama"], [1, "coverage"]]) {
      const data = new Uint8Array(256 * 256 * 4);
      for (let row = 0; row < 256; row++) for (let column = 0; column < 256; column++) {
        const offset = (row * 256 + column) * 4;
        data.set(name === "coverage" ? [255,255,255,255] : [column,255-row,0,255], offset);
      }
      gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, gl.createTexture());
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 256, 0, gl.RGBA, gl.UNSIGNED_BYTE, data);
      for (const parameter of [gl.TEXTURE_MIN_FILTER, gl.TEXTURE_MAG_FILTER]) gl.texParameteri(gl.TEXTURE_2D, parameter, gl.NEAREST);
      for (const parameter of [gl.TEXTURE_WRAP_S, gl.TEXTURE_WRAP_T]) gl.texParameteri(gl.TEXTURE_2D, parameter, gl.CLAMP_TO_EDGE);
      gl.uniform1i(gl.getUniformLocation(program, name), unit);
    }
    gl.uniform1i(gl.getUniformLocation(program, "imagePlane"), 1);
    gl.uniformMatrix3fv(gl.getUniformLocation(program, "rayMatrix"), false, [.06,.002,.01,.003,.05,.02,.2,.15,1]);
    const render = crop => {
      gl.clear(gl.COLOR_BUFFER_BIT); gl.uniform4fv(gl.getUniformLocation(program, "crop"), crop); gl.drawArrays(gl.TRIANGLES, 0, 6);
      const result = new Uint8Array(32 * 32 * 4); gl.readPixels(0, 0, 32, 32, gl.RGBA, gl.UNSIGNED_BYTE, result); return Array.from(result);
    };
    return { full: render([0,1,0,1]), crop: render([.2,.3,.2,.6]) };
  }, fragment);
  for (let row = 0; row < 32; row++) for (let column = 0; column < 32; column++) {
    const x = (column + .5) / 32 * 10, z = (row + .5) / 32 * 10;
    const u = (.06 * x + .003 * z + .2) / (.01 * x + .02 * z + 1);
    const v = (.002 * x + .05 * z + .15) / (.01 * x + .02 * z + 1);
    const offset = (row * 32 + column) * 4;
    expect(Math.abs(pixels.full[offset] - Math.floor(u * 256))).toBeLessThanOrEqual(1);
    expect(Math.abs(pixels.full[offset + 1] - Math.floor(v * 256))).toBeLessThanOrEqual(1);
    expect(pixels.crop[offset + 3]).toBe(u >= .2 && u <= .5 && v >= .2 && v <= .8 ? 255 : 0);
  }
});
