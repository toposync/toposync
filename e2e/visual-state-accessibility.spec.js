const { test, expect, captureFirst } = require("./fixtures/visual-state");

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

for (const theme of ["topo-day", "topo-night"]) {
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
