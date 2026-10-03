const { test, expect } = require("@playwright/test");

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("toposync.locale", "en");
    localStorage.setItem("toposync.theme", "default");
  });
});

test("deep-link refresh works on /settings/pipelines", async ({ page }) => {
  const badRequests = [];
  page.on("request", (req) => {
    const url = req.url();
    if (/\/settings\/main\.js(\?|#|$)/.test(url)) badRequests.push(url);
  });

  await page.goto("/settings/pipelines");
  await expect(page.locator(".pipelinesTopbar .pipelinesTitle", { hasText: "Pipelines" })).toBeVisible();
  await expect(page).toHaveURL(/\/settings\/pipelines$/);

  await page.reload();
  await expect(page.locator(".pipelinesTopbar .pipelinesTitle", { hasText: "Pipelines" })).toBeVisible();
  await expect(page).toHaveURL(/\/settings\/pipelines$/);

  expect(badRequests).toEqual([]);
});

test("collapsed pipeline list stays out of keyboard navigation and Escape restores its toggle", async ({ page }) => {
  await page.route("**/api/pipelines", (route) => route.fulfill({ json: { pipelines: [{
    name: "focus_fixture", enabled: false, editor_mode: "json", processing_server_id: "local",
    graph: { nodes: [], edges: [] },
  }] } }));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/settings/pipelines/focus_fixture");
  const toggle = page.locator('[aria-controls="pipeline-list"]');
  const createName = page.locator("#pipeline-create-name");
  await expect(page.getByRole("button", { name: "Save", exact: true })).toBeVisible();
  await toggle.focus();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Save", exact: true })).toBeFocused();
  await expect(createName).toBeHidden();
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await createName.focus();
  await page.keyboard.press("Escape");
  await expect(toggle).toBeFocused();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Save", exact: true })).toBeFocused();
  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(createName).toBeVisible();
});
