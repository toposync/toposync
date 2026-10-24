const { test, nativeZoomTest, expect, captureFirst } = require("./fixtures/visual-state");

test("reference galleries have named groups and browser magnification is allowed", async ({ page, visual }) => {
  await expect(visual.panel.getByRole("group", { name: "Fotos de referência Aberta", exact: true })).toHaveCount(1);
  await expect(visual.panel.getByRole("group", { name: "Fotos de referência Fechada", exact: true })).toHaveCount(1);
  await captureFirst(visual.panel);
  const capture = visual.panel.getByRole("button", { name: "Capturar", exact: true }).nth(1);
  await capture.focus();
  await page.keyboard.press("Enter");
  await expect(visual.panel.locator(".visualStatePhoto")).toHaveCount(2);
  await expect(visual.panel.getByRole("group", { name: "Fotos de referência Aberta", exact: true })).toBeVisible();
  await expect(visual.panel.getByRole("group", { name: "Fotos de referência Fechada", exact: true })).toBeVisible();
  const viewport = await page.locator('meta[name="viewport"]').getAttribute("content");
  expect(viewport).not.toMatch(/user-scalable\s*=\s*(?:no|0)/i);
  const maximum = viewport.match(/maximum-scale\s*=\s*([\d.]+)/i);
  if (maximum) expect(Number(maximum[1])).toBeGreaterThanOrEqual(5);
  const touch = await visual.panel.evaluate(panel => {
    const restrictions = [];
    for (let element = panel; element; element = element.parentElement) {
      const action = getComputedStyle(element).touchAction;
      if (!["auto", "manipulation"].includes(action) && !action.includes("pinch-zoom")) restrictions.push(action);
    }
    return restrictions;
  });
  expect(touch).toEqual([]);
});

test("keyboard removal and undo restore focus to surviving controls", async ({ page, visual }) => {
  const { panel } = visual;
  await captureFirst(panel);
  const remove = panel.getByRole("button", { name: "Remover captura Aberta, 1", exact: true });
  await remove.focus();
  await page.keyboard.press("Enter");
  await expect(panel.locator(".visualStatePhoto")).toHaveCount(0);
  await expect(panel.getByRole("textbox", { name: "Nome", exact: true }).first()).toBeFocused();
  const undo = panel.getByRole("button", { name: "Desfazer remoção", exact: true });
  await undo.focus();
  await page.keyboard.press("Enter");
  await expect(panel.locator(".visualStatePhoto")).toHaveCount(1);
  const add = panel.getByRole("button", { name: "Adicionar classe", exact: true });
  await expect(add).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(panel.locator(".visualStateClass")).toHaveCount(3);
  const removeClass = panel.getByRole("button", { name: "Remover classe Estado 3", exact: true });
  await removeClass.focus();
  await page.keyboard.press("Enter");
  await expect(panel.locator(".visualStateClass")).toHaveCount(2);
  await expect(add).toBeFocused();
});

test("capture completion preserves focus moved to another control", async ({ page, visual }) => {
  const { panel } = visual;
  let release, ready;
  const held = new Promise(resolve => { release = resolve; });
  const captured = new Promise(resolve => { ready = resolve; });
  await page.route("**/api/vision/visual-state/drafts/*/captures?*", async route => {
    const response = await route.fetch();
    ready();
    await held;
    await route.fulfill({ response });
  }, { times: 1 });
  await panel.getByRole("button", { name: "Capturar", exact: true }).first().click();
  const save = page.getByRole("button", { name: "Salvar", exact: true });
  try {
    await captured;
    await save.focus();
  } finally { release(); }
  await expect(panel.locator(".visualStatePhoto")).toHaveCount(1);
  await expect(panel.getByRole("button", { name: "Capturar", exact: true }).first()).toBeEnabled();
  await expect(save).toBeFocused();
});

test("capture errors are announced and recoverable with the keyboard", async ({ page, visual }) => {
  const { panel } = visual;
  await page.route("**/api/vision/visual-state/drafts/*/captures?*", route => route.fulfill({
    status: 403, contentType: "application/json", body: JSON.stringify({ detail: "forbidden" }),
  }), { times: 1 });
  const capture = panel.getByRole("button", { name: "Capturar", exact: true }).first();
  await capture.focus();
  await page.keyboard.press("Enter");
  await expect(panel.getByRole("alert")).toContainText("Somente o proprietário da instalação");
  await expect(capture).toBeFocused();
  await expect(panel.locator(".visualStatePhoto")).toHaveCount(0);
  const retry = panel.getByRole("button", { name: "Recuperar esta captura", exact: true });
  await retry.focus();
  await page.keyboard.press("Enter");
  await expect(panel.locator(".visualStatePhoto")).toHaveCount(1);
  await expect(panel.getByRole("alert")).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Adicionar classe", exact: true })).toBeFocused();
});

test("photo dialog traps keyboard focus and returns it to its trigger", async ({ page, visual }) => {
  await captureFirst(visual.panel);
  const photo = visual.panel.getByRole("button", { name: "Ampliar captura Aberta, 1", exact: true });
  await photo.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Aberta, 1", exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Fechar", exact: true })).toBeFocused();
  for (const key of ["Tab", "Shift+Tab", "Tab"]) {
    await page.keyboard.press(key);
    expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  }
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(photo).toBeFocused();
});

test("one preview announces progress and completion without announcing each frame", async ({ page, visual }) => {
  const { panel } = visual;
  await captureFirst(panel);
  await panel.getByRole("button", { name: "Atualizar perfis e limites", exact: true }).click();
  const preview = panel.getByRole("button", { name: "Testar agora", exact: true });
  await expect(preview).toBeEnabled();
  await panel.evaluate(root => {
    window.visualAnnouncements = [];
    window.visualAnnouncementObserver = new MutationObserver(records => {
      const regions = new Set();
      for (const record of records) {
        const target = record.target.nodeType === Node.ELEMENT_NODE ? record.target : record.target.parentElement;
        const current = target?.closest('[role="status"]');
        if (current && root.contains(current)) regions.add(current);
        for (const node of record.addedNodes) {
          if (node.nodeType !== Node.ELEMENT_NODE) continue;
          if (node.matches('[role="status"]')) regions.add(node);
          node.querySelectorAll('[role="status"]').forEach(region => regions.add(region));
        }
      }
      for (const region of regions) if (region.isConnected && region.textContent.trim()) {
        window.visualAnnouncements.push(region.textContent.trim());
      }
    });
    window.visualAnnouncementObserver.observe(root, { subtree: true, childList: true, characterData: true });
  });
  try {
    await preview.focus();
    await page.keyboard.press("Enter");
    await expect(panel.getByRole("status").filter({ hasText: "Preparando e testando o rascunho" })).toBeVisible();
    await expect(panel.locator(".visualPreviewResult")).toBeVisible();
    await expect(preview).toBeFocused();
    const announcements = await page.evaluate(() => window.visualAnnouncements);
    expect(announcements).toHaveLength(2);
    expect(announcements[0]).toContain("Preparando e testando o rascunho");
    expect(announcements[1]).toContain("Resultado");
  } finally { await page.evaluate(() => window.visualAnnouncementObserver.disconnect()); }
});

for (const theme of ["topo-day", "topo-night"]) {
  nativeZoomTest(`native browser zoom preserves reference controls: ${theme}`, async ({ page, visual, setNativeZoom }) => {
    await page.evaluate(value => localStorage.setItem("toposync.theme", value), theme);
    await page.reload();
    await page.locator('.react-flow__node[data-id="sensor"]').focus();
    await page.keyboard.press("Enter");
    await expect(visual.panel).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("data-toposync-theme", theme);
    const before = await page.evaluate(() => ({ width: innerWidth, ratio: devicePixelRatio }));
    await setNativeZoom(page, 2);
    await expect.poll(() => page.evaluate(() => innerWidth)).toBeCloseTo(before.width / 2, 0);
    const after = await page.evaluate(() => ({ ratio: devicePixelRatio, cssZoom: getComputedStyle(document.documentElement).zoom }));
    expect(after.ratio).toBeCloseTo(before.ratio * 2);
    expect(Number(after.cssZoom)).toBe(1);
    await captureFirst(visual.panel);
    const photo = visual.panel.getByRole("button", { name: "Ampliar captura Aberta, 1", exact: true });
    await photo.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog", { name: "Aberta, 1", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(photo).toBeFocused();
    const description = visual.panel.getByRole("textbox", { name: "O que identifica este estado", exact: true }).first();
    await description.fill("Vão da porta visível com ampliação nativa.");
    await expect(description).toHaveValue("Vão da porta visível com ampliação nativa.");
    expect(await visual.panel.evaluate(panel => panel.scrollWidth - panel.clientWidth)).toBeLessThanOrEqual(1);
    await setNativeZoom(page, 1);
    await expect.poll(() => page.evaluate(() => innerWidth)).toBe(before.width);
  });

  test(`class controls do not overlap at narrow width and 200 percent CSS zoom: ${theme}`, async ({ page, visual }) => {
    await page.evaluate(value => localStorage.setItem("toposync.theme", value), theme);
    await page.setViewportSize({ width: 1100, height: 900 });
    await page.reload();
    await page.locator('.react-flow__node[data-id="sensor"]').focus();
    await page.keyboard.press("Enter");
    await expect(visual.panel).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("data-toposync-theme", theme);
    for (const zoom of ["1", "2"]) {
      await page.evaluate(value => { document.documentElement.style.zoom = value; }, zoom);
      const layout = await visual.panel.evaluate(panel => {
        const row = panel.querySelector(".visualStateClass .identityToolbar");
        const input = row.querySelector("input").getBoundingClientRect();
        const overlaps = [...row.querySelectorAll("button")].some(button => {
          const rect = button.getBoundingClientRect();
          return Math.min(rect.right, input.right) > Math.max(rect.left, input.left)
            && Math.min(rect.bottom, input.bottom) > Math.max(rect.top, input.top);
        });
        return { overlaps, overflow: panel.scrollWidth - panel.clientWidth };
      });
      expect(layout.overlaps).toBe(false);
      expect(layout.overflow).toBeLessThanOrEqual(1);
    }
  });
}
