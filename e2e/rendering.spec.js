const { test, expect } = require("@playwright/test");

async function selectRenderMode(page, title) {
  await page.getByRole("button", { name: /^Rendering:/ }).click();
  const dialog = page.getByRole("dialog", { name: "Rendering" });
  await expect(dialog).toBeVisible();
  await dialog.getByText(title, { exact: true }).click();
  await expect(dialog).toBeHidden();
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("toposync.locale", "en");
    localStorage.setItem("toposync.theme", "default");
  });
});

test("render mode selector switches between 3D, snapshot, vector, and streams", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("button", { name: /^Rendering: 3D$/ })).toBeVisible();

  await selectRenderMode(page, "2D (Snapshot)");
  await expect(page.locator(".main2dRoot")).toBeVisible();
  await expect(page.getByRole("button", { name: /^Rendering: 2D$/ })).toBeVisible();

  await selectRenderMode(page, "2D (Vector)");
  await expect(page.locator(".mainVector2dRoot")).toBeVisible();
  await expect(page.getByRole("button", { name: /^Rendering: 2D \(Vector\)$/ })).toBeVisible();

  await selectRenderMode(page, "Streams");
  await expect(page.locator(".streamsRoot")).toBeVisible();
  await expect(page.getByRole("button", { name: /^Rendering: Streams$/ })).toBeVisible();

  await selectRenderMode(page, "3D");
  await expect(page.getByRole("button", { name: /^Rendering: 3D$/ })).toBeVisible();

  await selectRenderMode(page, "2D (Vector)");
  await page.reload();
  await expect(page.locator(".mainVector2dRoot")).toBeVisible();
  await expect(page.getByRole("button", { name: /^Rendering: 2D \(Vector\)$/ })).toBeVisible();
});
