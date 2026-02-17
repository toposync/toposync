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

  await page.reload();
  await expect(page.locator(".pipelinesTopbar .pipelinesTitle", { hasText: "Pipelines" })).toBeVisible();

  expect(badRequests).toEqual([]);
});
