const { test, expect } = require("@playwright/test");

function parseJsonEnv(name) {
  const raw = process.env[name];
  if (!raw) throw new Error(`Missing required environment variable: ${name}`);
  return JSON.parse(raw);
}

const expectedExtensionIds = parseJsonEnv("TOPOSYNC_DISTRIBUTION_EXPECTED_EXTENSIONS");
const expectedRemoteUrls = parseJsonEnv("TOPOSYNC_DISTRIBUTION_REMOTE_URLS");

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("toposync.locale", "en");
    localStorage.setItem("toposync.theme", "topo-day");
  });
});

test("bundled host loads installed extension remotes", async ({ page, request }) => {
  const remoteStatuses = new Map();
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (!expectedRemoteUrls.includes(url.pathname)) return;
    remoteStatuses.set(url.pathname, response.ok());
  });

  const extensionsResponse = await request.get("/api/extensions");
  expect(extensionsResponse.ok()).toBeTruthy();
  const extensions = await extensionsResponse.json();
  const extensionIds = extensions.map((extension) => extension.id).sort();
  expect(extensionIds).toEqual([...expectedExtensionIds].sort());

  const remoteUrls = extensions
    .map((extension) => extension.frontend?.remote_entry_url)
    .filter((value) => typeof value === "string")
    .sort();
  expect(remoteUrls).toEqual([...expectedRemoteUrls].sort());

  await page.goto("/");
  await expect(page.getByRole("button", { name: /^Composition:/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Settings", exact: true })).toBeVisible();

  await expect
    .poll(() => expectedRemoteUrls.filter((path) => remoteStatuses.get(path) === true).length, {
      message: `Loaded remotes: ${JSON.stringify(Array.from(remoteStatuses.entries()))}`,
    })
    .toBe(expectedRemoteUrls.length);

  const failedRemotes = Array.from(remoteStatuses.entries()).filter(([, ok]) => ok === false);
  expect(failedRemotes).toEqual([]);

  await page.goto("/settings");
  await expect(page.getByText("Settings", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Cameras\b/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Home Assistant\b/ })).toBeVisible();

  await page.goto("/");
  await page.getByRole("button", { name: "Edit", exact: true }).click();

  await expect(page.getByRole("button", { name: /^Wall\b/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Import 3D model\b/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Overlay image\b/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Camera\b/ })).toBeVisible();
});
