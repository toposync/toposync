const { test, expect } = require("@playwright/test");

const API = "http://127.0.0.1:8000";

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function currentCamerasSettings(request) {
  const response = await request.get(`${API}/api/settings`);
  expect(response.ok()).toBeTruthy();
  const settings = await response.json();
  return settings?.extensions?.["com.toposync.cameras"] || {};
}

async function patchCamerasSettings(request, settings) {
  const response = await request.patch(`${API}/api/settings/extensions/com.toposync.cameras`, { data: settings });
  expect(response.ok()).toBeTruthy();
  return response;
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("toposync.locale", "en");
    localStorage.setItem("toposync.theme", "topo-day");
  });
});

test("multi-source camera settings feed ingest, stream controls, and the stream wizard", async ({ page, request }) => {
  const suffix = `${Date.now()}`;
  const cameraId = `e2e_front_${suffix}`;
  const streamName = `E2E Front ${suffix}`;
  const streamPath = `e2e-front-${suffix}`;
  const highName = `High ${suffix}`;
  const lowName = `Low ${suffix}`;
  let createdTransmissionId = null;
  let createdPipelineName = null;
  const previousCameras = await currentCamerasSettings(request);

  try {
    await patchCamerasSettings(request, {
      devices: [
        {
          id: cameraId,
          name: `Front ${suffix}`,
          enabled: true,
          control: { type: "none" },
          onvif: null,
          sources: [
            {
              id: "main",
              name: highName,
              enabled: true,
              is_default: true,
              kind: "video",
              role: "main",
              view_id: "front",
              origin: { type: "rtsp", rtsp_url: "rtsp://10.0.0.10/high" },
              video: { width: 1920, height: 1080, fps: 20, codec: "H264" },
              ingest: { mode: "centralized", host_server_id: "local" },
            },
            {
              id: "sub",
              name: lowName,
              enabled: true,
              is_default: false,
              kind: "video",
              role: "sub",
              view_id: "front",
              origin: { type: "rtsp", rtsp_url: "rtsp://10.0.0.10/low" },
              video: { width: 640, height: 360, fps: 10, codec: "H264" },
              ingest: { mode: "direct", host_server_id: "local" },
            },
          ],
        },
      ],
    });

    const createTransmission = await request.post(`${API}/api/streams/transmissions`, {
      data: {
        name: streamName,
        enabled: true,
        host_server_id: "local",
        path: streamPath,
        camera_controls: { enabled: true, camera_id: cameraId, camera_source_id: "sub" },
        outputs: [
          {
            id: "hls",
            protocol: "hls",
            enabled: true,
            quality_profile_id: null,
            resolution: { width: 1280, height: 720 },
            fps_limit: null,
            bitrate_kbps: null,
            latency_profile: "normal",
          },
        ],
      },
    });
    expect(createTransmission.ok()).toBeTruthy();
    const createdTransmission = await createTransmission.json();
    createdTransmissionId = createdTransmission.id;
    expect(createdTransmission?.camera_controls?.camera_source_id).toBe("sub");

    await page.goto("/settings");
    await expect(page.getByText("Settings", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: /^Cameras\b/ }).click();

    await expect(page.getByText("Camera sources", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: new RegExp(`^${escapeRegExp(highName)}\\b`) })).toBeVisible();
    await page.getByRole("button", { name: new RegExp(`^${escapeRegExp(lowName)}\\b`) }).click();
    const ingestSelect = page.locator("label", { hasText: "Camera input" }).locator("..").locator("select");
    await expect(ingestSelect).toHaveValue("direct");
    await expect(page.getByText("Direct connection", { exact: true })).toBeVisible();
    await expect(page.getByText("This camera may receive one connection per consuming flow.", { exact: true })).toBeVisible();

    await page.getByRole("button", { name: /^Streams\b/ }).click();
    await expect(page.getByText("Camera ingest access", { exact: true })).toBeVisible();
    await expect(page.getByText(`ingest-${cameraId}-main`, { exact: true })).toBeVisible();
    await expect(page.getByText(`ingest-${cameraId}-sub`, { exact: true })).toHaveCount(0);

    await page.getByRole("button", { name: new RegExp(`^${escapeRegExp(streamName)}\\b`) }).click();
    await expect(page.getByText("Camera controls", { exact: true })).toBeVisible();
    const streamSourceSelect = page.locator("label", { hasText: "Camera source" }).locator("..").locator("select");
    await expect(streamSourceSelect).toHaveValue("sub");
    await expect(streamSourceSelect.locator("option:checked")).toHaveText(new RegExp(`^${escapeRegExp(lowName)} .*640x360$`));

    await page.getByRole("button", { name: "Create pipeline for this stream", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Create pipeline" });
    await expect(dialog).toBeVisible();
    const wizardSourceSelect = dialog.locator("label", { hasText: "Camera source" }).locator("..").locator("select");
    await expect(wizardSourceSelect).toHaveValue("sub");
    await expect(wizardSourceSelect.locator("option:checked")).toHaveText(new RegExp(`^${escapeRegExp(lowName)} .*640x360$`));
    await expect(dialog.getByText("This flow will open a direct connection to the camera source.", { exact: true })).toBeVisible();

    const wizardRequest = page.waitForRequest(
      (req) => req.url().includes("/api/streams/wizard/create-pipeline") && req.method() === "POST",
    );
    const wizardResponse = page.waitForResponse(
      (resp) => resp.url().includes("/api/streams/wizard/create-pipeline") && resp.request().method() === "POST",
    );
    await dialog.getByRole("button", { name: "Create pipeline", exact: true }).click();
    const requestPayload = JSON.parse((await wizardRequest).postData() || "{}");
    expect(requestPayload.camera_id).toBe(cameraId);
    expect(requestPayload.camera_source_id).toBe("sub");
    const response = await wizardResponse;
    expect(response.ok()).toBeTruthy();
    const wizardResult = await response.json();
    createdPipelineName = wizardResult.pipeline_name;
    expect(wizardResult.camera_id).toBe(cameraId);
    expect(wizardResult.camera_source_id).toBe("sub");
  } finally {
    if (createdPipelineName) {
      await request.delete(`${API}/api/pipelines/${encodeURIComponent(createdPipelineName)}`).catch(() => {});
    }
    if (createdTransmissionId) {
      await request.delete(`${API}/api/streams/transmissions/${encodeURIComponent(createdTransmissionId)}`).catch(() => {});
    }
    await patchCamerasSettings(request, previousCameras).catch(() => {});
  }
});
