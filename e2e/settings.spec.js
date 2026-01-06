const { test, expect } = require("@playwright/test");

async function openSettings(page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Settings" });
  await expect(dialog).toBeVisible();
  return dialog;
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("toposync.locale", "en");
    localStorage.setItem("toposync.theme", "default");
  });
});

test("theme can be selected from settings", async ({ page }) => {
  const dialog = await openSettings(page);
  await dialog.getByRole("button", { name: "Core" }).click();

  const accentBefore = await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue("--accent").trim(),
  );
  expect(accentBefore.toLowerCase()).toBe("#fbbf24");

  await expect(dialog.getByText("Neon (blue)")).toBeVisible();
  await dialog.getByText("Neon (blue)").click();

  const accentAfter = await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue("--accent").trim(),
  );
  expect(accentAfter.toLowerCase()).toBe("#38bdf8");
});

test("camera detections are persisted in backend settings", async ({ page, request }) => {
  const dialog = await openSettings(page);

  await expect(dialog.getByRole("button", { name: "Cameras" })).toBeVisible();
  await dialog.getByRole("button", { name: "Cameras" }).click();

  await dialog.getByRole("button", { name: "Add camera" }).click();

  await dialog.getByRole("button", { name: "Detections" }).first().click();

  await expect(page.getByRole("dialog", { name: /Detections/ })).toBeVisible();
  const detDialog = page.getByRole("dialog", { name: /Detections/ });

  await detDialog.getByRole("button", { name: "Add" }).click();

  await detDialog.getByRole("combobox").first().selectOption("object");
  await detDialog.getByRole("combobox").nth(1).selectOption("cat");

  await detDialog.getByRole("button", { name: "Save" }).click();

  await dialog.getByRole("button", { name: "Save" }).click();

  const res = await request.get("http://127.0.0.1:8000/api/settings");
  expect(res.ok()).toBeTruthy();
  const data = await res.json();
  const cameras = data?.extensions?.["com.toposync.cameras"]?.cameras ?? [];
  expect(Array.isArray(cameras)).toBeTruthy();
  expect(cameras.length).toBeGreaterThan(0);
  const detections = cameras[0]?.detections ?? [];
  expect(Array.isArray(detections)).toBeTruthy();
  expect(detections.length).toBeGreaterThan(0);
  expect(detections[0]?.trigger?.kind).toBe("object");
  expect(detections[0]?.trigger?.category).toBe("cat");
});
