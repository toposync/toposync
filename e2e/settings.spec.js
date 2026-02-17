const { test, expect } = require("@playwright/test");

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function openSettings(page) {
  await page.goto("/settings");
  await expect(page.getByText("Settings", { exact: true })).toBeVisible();
  return page;
}

async function openViewSettings(page) {
  await page.goto("/settings");
  await expect(page.getByText("Settings", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /^View options\b/ }).click();
  await expect(page.getByText("Wall height")).toBeVisible();
  return page;
}

async function openCompositions(page) {
  await page.goto("/");
  await page.getByRole("button", { name: /^Composition:/ }).click();
  const dialog = page.getByRole("dialog", { name: "Compositions" });
  await expect(dialog).toBeVisible();
  return dialog;
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("toposync.locale", "en");
    localStorage.setItem("toposync.theme", "default");
  });
});

test("wall height preset persists across reload", async ({ page }) => {
  const dialog = await openViewSettings(page);

  await dialog.getByRole("button", { name: /^Low\b/ }).click();

  await page.waitForFunction(() => {
    try {
      const raw = localStorage.getItem("toposync.view.v1");
      if (!raw) return false;
      return JSON.parse(raw)?.wall_height_preset === "low";
    } catch {
      return false;
    }
  });

  await page.reload();

  await page.getByRole("button", { name: /^View options\b/ }).click();
  const low = page.getByRole("button", { name: /^Low\b/ });
  await expect(low).toHaveClass(/isSelected/);
});

test("ghost walls persists across reload", async ({ page }) => {
  const dialog = await openViewSettings(page);

  await dialog.getByRole("button", { name: /^Ghost walls\b/ }).click();

  await page.waitForFunction(() => {
    try {
      const raw = localStorage.getItem("toposync.view.v1");
      if (!raw) return false;
      return JSON.parse(raw)?.ghost_walls === true;
    } catch {
      return false;
    }
  });

  await page.reload();

  await page.getByRole("button", { name: /^View options\b/ }).click();
  const ghost = page.getByRole("button", { name: /^Ghost walls\b/ });
  await expect(ghost).toHaveClass(/isSelected/);
});

test("theme can be selected from settings", async ({ page }) => {
  const dialog = await openSettings(page);
  await dialog.getByRole("button", { name: /^Core\b/ }).click();

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

test("compositions can be created, renamed, and deleted", async ({ page, request }) => {
  const createdName = `E2E ${Date.now()}`;
  const renamedName = `${createdName} renamed`;
  let compositionId = null;

  try {
    const dialog = await openCompositions(page);

    await dialog.getByPlaceholder("Name (e.g. Ground, Upstairs...)").fill(createdName);
    const createReq = page.waitForResponse(
      (resp) => resp.url().includes("/api/compositions") && resp.request().method() === "POST" && resp.status() === 200,
    );
    await dialog.getByRole("button", { name: "Create composition", exact: true }).click();
    const createRes = await createReq;
    const created = await createRes.json().catch(() => ({}));
    if (created?.id) compositionId = created.id;
    await expect(page.getByRole("dialog", { name: "Compositions" })).toBeHidden();

    await expect(page.getByRole("button", { name: new RegExp(`^Composition: ${escapeRegExp(createdName)}$`) })).toBeVisible();

    const listRes = await request.get("http://127.0.0.1:8000/api/compositions");
    expect(listRes.ok()).toBeTruthy();
    const listData = await listRes.json();
    expect(listData?.compositions?.some((c) => c?.name === createdName)).toBeTruthy();

    await page.getByRole("button", { name: /^Composition:/ }).click();
    const dialog2 = page.getByRole("dialog", { name: "Compositions" });
    await expect(dialog2).toBeVisible();

    const selectButton = dialog2.getByRole("button", { name: createdName, exact: true });
    const row = selectButton.locator("..");
    await row.getByRole("button", { name: "Rename composition", exact: true }).click();
    const saveName = dialog2.getByRole("button", { name: "Save name", exact: true });
    await expect(saveName).toBeVisible();
    const editingRow = saveName.locator("..").locator("..");
    const renameInput = editingRow.locator("input.input");
    await expect(renameInput).toBeVisible();
    await renameInput.fill(renamedName);
    const renameReq = page.waitForResponse(
      (resp) => resp.url().includes("/api/compositions/") && resp.request().method() === "PATCH" && resp.status() === 200,
    );
    await saveName.click();
    await renameReq;

    const renamedSelect = dialog2.getByRole("button", { name: renamedName, exact: true });
    await expect(renamedSelect).toBeVisible();

    const deleteRow = renamedSelect.locator("..");
    await deleteRow.getByRole("button", { name: "Delete composition", exact: true }).click();
    const deleteReq = page.waitForResponse(
      (resp) => resp.url().includes("/api/compositions/") && resp.request().method() === "DELETE" && resp.status() === 200,
    );
    await dialog2.getByRole("button", { name: "Confirm delete", exact: true }).click();
    await deleteReq;

    const listRes2 = await request.get("http://127.0.0.1:8000/api/compositions");
    expect(listRes2.ok()).toBeTruthy();
    const listData2 = await listRes2.json();
    expect(listData2?.compositions?.some((c) => c?.name === renamedName)).toBeFalsy();
  } finally {
    if (compositionId) {
      try {
        await request.delete(`http://127.0.0.1:8000/api/compositions/${encodeURIComponent(compositionId)}`);
      } catch {
        // ignore cleanup errors
      }
    }
  }
});

test("wall tool can create a wall and persist to backend", async ({ page, request }) => {
  const name = `E2E wall ${Date.now()}`;
  let compositionId = null;
  try {
    const createRes = await request.post("http://127.0.0.1:8000/api/compositions", { data: { name } });
    expect(createRes.ok()).toBeTruthy();
    const created = await createRes.json();
    expect(created?.id).toBeTruthy();
    compositionId = created.id;

    const activateRes = await request.post(
      `http://127.0.0.1:8000/api/compositions/${encodeURIComponent(compositionId)}/activate`,
    );
    expect(activateRes.ok()).toBeTruthy();

    const beforeRes = await request.get("http://127.0.0.1:8000/api/composition");
    expect(beforeRes.ok()).toBeTruthy();
    const beforeComp = await beforeRes.json();
    const beforeElements = Array.isArray(beforeComp?.elements) ? beforeComp.elements : [];
    const beforeWalls = beforeElements.filter((el) => el?.type === "com.toposync.structural.wall").length;

    await page.goto("/");
    await expect(page.getByRole("button", { name: new RegExp(`^Composition: ${escapeRegExp(name)}$`) })).toBeVisible();

    await page.getByRole("button", { name: "Edit", exact: true }).click();
    await expect(page.getByRole("button", { name: "Back", exact: true })).toBeVisible();

    const wallTool = page.getByRole("button", { name: "Wall", exact: true });
    await expect(wallTool).toBeVisible();
    await wallTool.click();
    await expect(wallTool).toHaveClass(/isSelected/);

    const canvas = page.locator("canvas.viewportCanvas");
    await expect(canvas).toBeVisible();
    const box = await canvas.boundingBox();
    if (!box) throw new Error("Canvas has no bounding box");

    const save = page.waitForResponse(
      (resp) => resp.url().includes("/api/composition") && resp.request().method() === "PUT" && resp.status() === 200,
    );

    await canvas.click({ position: { x: box.width * 0.6, y: box.height * 0.5 } });
    await canvas.click({ position: { x: box.width * 0.8, y: box.height * 0.65 } });
    await save;

    const res = await request.get("http://127.0.0.1:8000/api/composition");
    expect(res.ok()).toBeTruthy();
    const comp = await res.json();
    const elements = Array.isArray(comp?.elements) ? comp.elements : [];
    const afterWalls = elements.filter((el) => el?.type === "com.toposync.structural.wall").length;
    expect(afterWalls).toBe(beforeWalls + 1);
  } finally {
    if (compositionId) {
      try {
        await request.delete(`http://127.0.0.1:8000/api/compositions/${encodeURIComponent(compositionId)}`);
      } catch {
        // ignore cleanup errors
      }
    }
  }
});

 
