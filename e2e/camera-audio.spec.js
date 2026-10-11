const { test, expect } = require("@playwright/test");

function tone(amplitude) {
  const rate = 16000;
  const buffer = Buffer.alloc(44 + rate * 2);
  buffer.write("RIFF", 0); buffer.writeUInt32LE(buffer.length - 8, 4);
  buffer.write("WAVEfmt ", 8); buffer.writeUInt32LE(16, 16);
  buffer.writeUInt16LE(1, 20); buffer.writeUInt16LE(1, 22);
  buffer.writeUInt32LE(rate, 24); buffer.writeUInt32LE(rate * 2, 28);
  buffer.writeUInt16LE(2, 32); buffer.writeUInt16LE(16, 34);
  buffer.write("data", 36); buffer.writeUInt32LE(rate * 2, 40);
  for (let index = 0; index < rate; index += 1) {
    buffer.writeInt16LE(Math.round(amplitude * Math.sin(2 * Math.PI * 1000 * index / rate)), 44 + index * 2);
  }
  return buffer;
}

for (const [locale, width] of [["en", 1440], ["pt-BR", 390]]) {
  test(`audio file review ${locale} at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.addInitScript((value) => localStorage.setItem("toposync.locale", value), locale);
    await page.goto("/settings");
    if (width < 800) {
      await page.locator(".settingsMobileNavigation select").selectOption("com.toposync.cameras.audio-tools");
    } else {
      await page.getByRole("button", { name: /^(Audio tools|Ferramentas de áudio)\b/ }).click();
    }
    const title = locale === "en" ? "Analyze an audio sample" : "Analisar amostra de áudio";
    await page.getByText(title, { exact: true }).click();
    const panel = page.locator("details").filter({ has: page.locator("summary", { hasText: title }) }).first();
    const fileLabel = locale === "en" ? "Audio sample" : "Amostra de áudio";
    await panel.getByLabel(fileLabel, { exact: true }).setInputFiles({ name: "tone.wav", mimeType: "audio/wav", buffer: tone(8000) });
    await panel.getByText(locale === "en" ? "Compare with a reference sample" : "Comparar com uma amostra de referência", { exact: true }).click();
    await panel.getByLabel(locale === "en" ? "Normal sound reference (optional)" : "Referência de som normal (opcional)", { exact: true })
      .setInputFiles({ name: "reference.wav", mimeType: "audio/wav", buffer: tone(1000) });
    await panel.getByRole("button", { name: locale === "en" ? "Analyze sample" : "Analisar amostra", exact: true }).click();
    await expect(panel.getByRole("status")).toHaveText(locale === "en" ? "Signal detected" : "Sinal detectado");
    await expect(panel.getByText("1000 Hz", { exact: true })).toBeVisible();
    await expect(panel.getByText(locale === "en" ? "Review: above reference" : "Revisar: acima da referência", { exact: true })).toBeVisible();
    await expect(panel.locator("audio")).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    if (width < 500) {
      await panel.getByText("1000 Hz", { exact: true }).scrollIntoViewIfNeeded();
      await page.screenshot({ path: testInfo.outputPath(`audio-${locale}-${width}.png`) });
    } else {
      await panel.screenshot({ path: testInfo.outputPath(`audio-${locale}-${width}.png`) });
    }
  });
}

test("audio tools handle errors and abort analysis when leaving without changing camera settings", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("toposync.locale", "en"));
  await page.goto("/settings?panel=com.toposync.cameras.audio-tools");
  const title = page.getByText("Analyze an audio sample", { exact: true });
  await title.click();
  const panel = page.locator("details").filter({ has: page.locator("summary", { hasText: "Analyze an audio sample" }) }).first();
  await panel.getByLabel("Audio sample", { exact: true }).setInputFiles({ name: "tone.wav", mimeType: "audio/wav", buffer: tone(8000) });
  await page.route("**/api/cameras/audio/analyze?*", route => route.fulfill({ status: 503, json: { detail: "Synthetic analysis failure" } }));
  await panel.getByRole("button", { name: "Analyze sample", exact: true }).click();
  await expect(panel.getByRole("alert")).toContainText("Could not analyze");
  await expect(page.getByText(/^Unsaved:/)).toHaveCount(0);
  await page.unroute("**/api/cameras/audio/analyze?*");
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let reached;
  const started = new Promise(resolve => { reached = resolve; });
  await page.route("**/api/cameras/audio/analyze?*", async route => {
    reached();
    await gate;
    await route.fulfill({ status: 503, json: { detail: "Delayed synthetic failure" } }).catch(() => {});
  });
  await panel.getByRole("button", { name: "Analyze sample", exact: true }).click();
  await started;
  const cancelled = page.waitForEvent("requestfailed", { predicate: request => request.url().includes("/audio/analyze?") });
  await page.getByRole("button", { name: /^Cameras\b/ }).click();
  await cancelled;
  release();
  await expect(page.getByTestId("camera-device-settings")).toBeVisible();
  await expect(page.getByText(/^Unsaved:/)).toHaveCount(0);
  await expect(page.getByText("Delayed synthetic failure", { exact: true })).toHaveCount(0);
});
