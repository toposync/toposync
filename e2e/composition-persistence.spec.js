const { test, expect } = require("@playwright/test");

async function compositionFixture(page) {
  const composition = { id: "persistence-fixture", name: "Persistence fixture", elements: [] };
  const writes = [];
  const unexpectedWrites = [];
  await page.addInitScript(() => {
    localStorage.setItem("toposync.locale", "en");
    localStorage.setItem("toposync.render_mode.v1", "2d");
  });
  await page.route("**/api/**", route => {
    const request = route.request();
    if (!["GET", "HEAD"].includes(request.method())) {
      unexpectedWrites.push(`${request.method()} ${new URL(request.url()).pathname}`);
      return route.abort();
    }
    return route.continue();
  });
  await page.route("**/api/extensions", route => route.fulfill({ json: [] }));
  await page.route("**/api/compositions", route => route.fulfill({ json: {
    active_composition_id: composition.id, compositions: [{ id: composition.id, name: composition.name }],
  } }));
  await page.route("**/api/composition", route => {
    if (route.request().method() === "PUT") {
      const updated = route.request().postDataJSON();
      writes.push(updated);
      return route.fulfill({ json: updated });
    }
    return route.fulfill({ json: composition });
  });
  return { composition, writes, unexpectedWrites };
}

test("opening settings does not write the composition loaded from the server", async ({ page }) => {
  const fixture = await compositionFixture(page);
  await page.goto("/settings");
  await expect(page.getByText("Settings", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /^Compositions\b/ }).click();
  await expect(page.locator(".compositionsSettingsPanel").getByText(fixture.composition.name, { exact: true })).toBeVisible();
  // Observe beyond the 400 ms autosave debounce after the hydrated value is visible.
  await page.waitForTimeout(1200);
  expect(fixture.writes).toEqual([]);
  expect(fixture.unexpectedWrites).toEqual([]);
});

test("measurement edits, undo and redo still save after composition hydration", async ({ page }, testInfo) => {
  const fixture = await compositionFixture(page);
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Composition: Persistence fixture", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByRole("button", { name: "Measure", exact: true }).click();
  const canvas = page.locator("canvas.viewportCanvas");
  const box = await canvas.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.5);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.8, box.y + box.height * 0.65, { steps: 5 });
  await page.mouse.up();
  await expect(canvas).toBeFocused();
  await expect.poll(() => fixture.writes.at(-1)?.elements.length).toBe(1);
  expect(fixture.writes).toHaveLength(1);
  expect(fixture.writes[0].id).toBe(fixture.composition.id);
  expect(fixture.writes[0].elements).toHaveLength(1);
  expect(fixture.writes[0].elements[0].type).toBe("com.toposync.core.measurement.line");
  await page.keyboard.press("Control+z");
  await expect.poll(() => fixture.writes.length).toBe(2);
  expect(fixture.writes[1].elements).toEqual([]);
  await page.keyboard.press("Control+Shift+z");
  await expect.poll(() => fixture.writes.length).toBe(3);
  expect(fixture.writes[2].elements).toEqual(fixture.writes[0].elements);
  expect(fixture.unexpectedWrites).toEqual([]);
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.getByRole("button", { name: /^Tools:/ }).focus();
    await page.keyboard.press("Shift+Tab");
    await expect(page.getByLabel("Composition drawing area", { exact: true })).toBeFocused();
    expect(await canvas.evaluate(element => element.matches(":focus-visible"))).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)).toBe(false);
    await page.screenshot({ path: testInfo.outputPath(`canvas-focus-${width}.png`), animations: "disabled" });
  }
});
