const { test, expect } = require("@playwright/test");

const LIVE_VIEWS = [
  {
    id: "camera-a",
    owner_kind: "camera_source",
    camera_id: "front",
    name: "Front Door",
    enabled: true,
    host_server_id: "local",
    defaults: {
      thumbnail_variant_id: "sub",
      pip_variant_id: "sub",
      large_variant_id: "main",
      fullscreen_variant_id: "main",
    },
    variants: [
      {
        id: "main",
        label: "Main",
        role: "main",
        camera_source_id: "main",
        transmission_id: "tx-camera-a",
        enabled: true,
      },
    ],
  },
  {
    id: "camera-b",
    owner_kind: "camera_source",
    camera_id: "back",
    name: "Back Yard",
    enabled: true,
    host_server_id: "local",
    defaults: {
      thumbnail_variant_id: "sub",
      pip_variant_id: "sub",
      large_variant_id: "main",
      fullscreen_variant_id: "main",
    },
    variants: [
      {
        id: "main",
        label: "Main",
        role: "main",
        camera_source_id: "main",
        transmission_id: "tx-camera-b",
        enabled: true,
      },
    ],
  },
];

async function selectRenderMode(page, title) {
  await page.getByRole("button", { name: /^Rendering:/ }).click();
  const dialog = page.getByRole("dialog", { name: "Rendering" });
  await expect(dialog).toBeVisible();
  await dialog.getByText(title, { exact: true }).click();
  await expect(dialog).toBeHidden();
}

async function mockLiveViews(page) {
  await page.route(/\/api\/streams\/live-views$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(LIVE_VIEWS),
    });
  });
  await page.route(/\/api\/streams\/live-views\/[^/]+\/playback(?:\?.*)?$/, async (route) => {
    const url = new URL(route.request().url());
    const liveViewId = decodeURIComponent(url.pathname.split("/").at(-2) || "");
    const liveView = LIVE_VIEWS.find((item) => item.id === liveViewId) || LIVE_VIEWS[0];
    const transmissionId = liveView.variants[0].transmission_id;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        live_view: liveView,
        context: url.searchParams.get("context") || "large",
        variant: liveView.variants[0],
        camera_id: liveView.camera_id,
        camera_name: liveView.name,
        camera_source_id: "main",
        camera_source_name: "Main",
        transmission: {
          id: transmissionId,
          name: liveView.name,
          enabled: true,
          outputs: [],
          camera_controls: { enabled: false },
        },
        urls: {
          transmission_id: transmissionId,
          engine_running: false,
          outputs: [],
          warnings: [],
          hls_warnings: [],
          webrtc_warnings: [],
          blocking_errors: [],
        },
        blocking_errors: [],
        warnings: [],
      }),
    });
  });
  await page.route(/\/api\/streams\/runtime\/health$/, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ transmissions: [] }),
    });
  });
}

test.beforeEach(async ({ page }) => {
  await mockLiveViews(page);
  await page.addInitScript(() => {
    localStorage.setItem("toposync.locale", "en");
    localStorage.setItem("toposync.theme", "default");
  });
});

test("render mode selector switches between spatial views, camera grid, and live camera", async ({ page }) => {
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await expect(page.getByRole("button", { name: /^Rendering: 3D$/ })).toBeVisible();

  await selectRenderMode(page, "2D (Image)");
  await expect(page.locator(".main2dRoot")).toBeVisible();
  await expect(page.getByRole("button", { name: /^Rendering: 2D \(Image\)$/ })).toBeVisible();

  await selectRenderMode(page, "2D (Plan)");
  await expect(page.locator(".mainVector2dRoot")).toBeVisible();
  await expect(page.getByRole("button", { name: /^Rendering: 2D \(Plan\)$/ })).toBeVisible();

  await selectRenderMode(page, "Camera Grid");
  await expect(page.locator(".streamsRoot")).toBeVisible();
  await expect(page.getByRole("button", { name: /^Rendering: Camera Grid$/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "1x1" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "2x2" })).toBeVisible();
  await expect(page.getByRole("button", { name: "3x3" })).toBeVisible();

  await selectRenderMode(page, "Live Camera");
  await expect(page).toHaveURL(/\/cameras\/live\/camera-a$/);
  await expect(page.getByRole("button", { name: /^Rendering: Live Camera$/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Camera: Front Door$/ })).toBeVisible();

  await selectRenderMode(page, "3D");
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("button", { name: /^Rendering: 3D$/ })).toBeVisible();
});

test("live camera render mode reopens the remembered selected camera", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("toposync.streams.last_live_view_id.v1", "camera-b");
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  await selectRenderMode(page, "Live Camera");
  await expect(page).toHaveURL(/\/cameras\/live\/camera-b$/);
  await expect(page.getByRole("button", { name: /^Camera: Back Yard$/ })).toBeVisible();

  await selectRenderMode(page, "3D");
  await expect(page).toHaveURL(/\/$/);
  await selectRenderMode(page, "Live Camera");
  await expect(page).toHaveURL(/\/cameras\/live\/camera-b$/);
  await expect(page.getByRole("button", { name: /^Camera: Back Yard$/ })).toBeVisible();
});

test("direct live camera links do not overwrite the remembered camera", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("toposync.streams.last_live_view_id.v1", "camera-b");
  });
  await page.goto("/cameras/live/camera-a", { waitUntil: "domcontentloaded" });

  await expect(page.getByRole("button", { name: /^Rendering: Live Camera$/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Camera: Front Door$/ })).toBeVisible();
  await expect(page).toHaveURL(/\/cameras\/live\/camera-a$/);
  const rememberedLiveViewId = await page.evaluate(() => localStorage.getItem("toposync.streams.last_live_view_id.v1"));
  expect(rememberedLiveViewId).toBe("camera-b");
});
