const { test, expect } = require("@playwright/test");

const TRANSMISSION = {
  id: "front",
  name: "Front Door",
  path: "front",
  enabled: true,
  camera_controls: { enabled: true, camera_id: "cam_front" },
  outputs: [
    {
      id: "hls_stable_apple_tv",
      protocol: "hls",
      enabled: true,
      quality_profile_id: "stable_apple_tv",
      resolution: { width: 1280, height: 720 },
      fps_limit: 15,
      bitrate_kbps: 1800,
      latency_profile: "normal",
    },
    {
      id: "webrtc_low_latency",
      protocol: "webrtc",
      enabled: true,
      resolution: { width: 1280, height: 720 },
      fps_limit: 15,
      bitrate_kbps: 1800,
      latency_profile: "ultra_low",
    },
  ],
};

const URLS = {
  transmission_id: "front",
  engine_running: true,
  outputs: [
    {
      output_id: "hls_stable_apple_tv",
      protocol: "hls",
      resolved_engine_path: "front-hls",
      url: "/mock-hls/front/index.m3u8",
      requires_auth: false,
      media_auth_type: "signed_url",
      url_expires_at_unix: Math.floor(Date.now() / 1000) + 300,
      renew_after_unix: Math.floor(Date.now() / 1000) + 240,
      quality_profile_id: "stable_apple_tv",
      resolution: { width: 1280, height: 720 },
      fps_limit: 15,
      bitrate_kbps: 1800,
      latency_profile: "normal",
    },
    {
      output_id: "webrtc_low_latency",
      protocol: "webrtc",
      resolved_engine_path: "front-whep",
      url: "/mock-whep/front/whep",
      requires_auth: false,
      media_auth_type: "none",
      quality_profile_id: null,
      resolution: { width: 1280, height: 720 },
      fps_limit: 15,
      bitrate_kbps: 1800,
      latency_profile: "ultra_low",
    },
  ],
  network_contract: {
    environment: "generic",
    public_hls_mode: "proxy",
    status: "ok",
    expected_ports: { direct_api: 18756, hls: 18759, webrtc: 18760, webrtc_udp: 18762 },
    actual_ports: { direct_api: 18756, hls: 18759, webrtc: 18760, webrtc_udp: 18762 },
    webrtc_additional_hosts: ["127.0.0.1"],
    warnings: [],
    blocking_errors: [],
  },
  warnings: [],
  blocking_errors: [],
};

const HOME_ASSISTANT_PROXY_URLS = {
  ...URLS,
  network_contract: {
    ...URLS.network_contract,
    environment: "home_assistant_addon",
    public_hls_mode: "proxy",
    webrtc_additional_hosts: ["192.168.1.20"],
  },
};

function outputHealth(overrides = {}) {
  return {
    output_key: "front:hls_stable_apple_tv",
    output_id: "hls_stable_apple_tv",
    transmission_id: "front",
    protocol: "hls",
    resolved_engine_path: "front-hls",
    viewer_count: 1,
    demand_signal: true,
    publisher_running: true,
    publisher_frames_sent: 120,
    publisher_restart_count: 0,
    publisher_frames_sent_rate: 15,
    status: "live",
    classification: "healthy",
    evidence: [],
    ...overrides,
  };
}

function healthPayload(scenario) {
  const base = {
    transmission_id: "front",
    enabled: true,
    status: "live",
    stale: false,
    fallback_active: false,
    placeholder_active: false,
    selected_frame_age_seconds: 0.2,
    last_incoming_frame_age_seconds: 0.2,
    last_live_frame_at_unix: Math.floor(Date.now() / 1000),
    classification: "healthy",
    evidence: [],
    outputs: [outputHealth()],
  };
  if (scenario === "source_stale") {
    const source_health = {
      source_id: "camera.source:front",
      camera_id: "cam_front",
      source_frame_age_seconds: 31,
      opened: true,
      restarts_total: 1,
      decode_failures: 0,
      frames_captured: 120,
      last_frame_at_unix: Math.floor(Date.now() / 1000) - 31,
      status: "stale",
      recommended_action: "Check camera source.",
    };
    return {
      ...base,
      status: "stale",
      stale: true,
      classification: "source_stale",
      source_health,
      outputs: [outputHealth({ status: "stale", classification: "source_stale", source_health })],
    };
  }
  if (scenario === "stale_hls") {
    return {
      ...base,
      classification: "hls_playlist_stale",
      evidence: ["Recent HLS liveness event reports playlist stopped advancing."],
    };
  }
  if (scenario === "tail_unavailable") {
    return {
      ...base,
      classification: "hls_tail_unavailable",
      evidence: ["Recent HLS liveness event reports tail segment unavailable."],
    };
  }
  if (scenario === "publisher_down") {
    return {
      ...base,
      status: "offline",
      classification: "publisher_down",
      evidence: ["Publisher is not running."],
      outputs: [
        outputHealth({
          status: "offline",
          publisher_running: false,
          publisher_last_error: "ffmpeg exited",
        }),
      ],
    };
  }
  if (scenario === "webrtc_transport_error") {
    return {
      ...base,
      classification: "webrtc_transport_error",
      evidence: ["Recent WebRTC event reports signaling or ICE transport failure."],
    };
  }
  return base;
}

async function mockStreamingDashboard(page, scenario, transportPreference = "hls", options = {}) {
  const urlsPayload = options.urls || URLS;
  const whepCalls = options.whepCalls || [];

  await page.addInitScript((preference) => {
    localStorage.setItem("toposync.locale", "en");
    localStorage.setItem("toposync.theme", "default");
    localStorage.setItem("toposync.render_mode.v1", "streams");
    localStorage.setItem("toposync.streams.grid_mode.v1", "1x1");
    localStorage.setItem("toposync.streams.transport_preference.v1", JSON.stringify({ front: preference }));
  }, transportPreference);

  await page.route(/\/api\/streams\/transmissions$/, async (route) => {
    await route.fulfill({ json: [TRANSMISSION] });
  });
  await page.route(/\/api\/streams\/transmissions\/front\/urls.*/, async (route) => {
    await route.fulfill({ json: urlsPayload });
  });
  await page.route(/\/api\/streams\/transmissions\/front\/demand\/prime.*/, async (route) => {
    await route.fulfill({
      json: { transmission_id: "front", primed: true, primed_outputs: 1 },
    });
  });
  await page.route(/\/api\/streams\/runtime\/health$/, async (route) => {
    await route.fulfill({
      json: {
        updated_at_unix: Date.now() / 1000,
        stale_after_seconds: 3,
        placeholder_after_seconds: 8,
        transmissions: [healthPayload(scenario)],
      },
    });
  });
  await page.route(/\/api\/streams\/runtime\/playback-events$/, async (route) => {
    await route.fulfill({ json: { accepted: 1, retained: 1 } });
  });
  await page.route(/\/api\/streams\/transmissions\/front\/camera\/presets$/, async (route) => {
    await route.fulfill({
      json: {
        transmission_id: "front",
        camera_id: "cam_front",
        presets: [{ token: "home", name: "Home", pan: 0, tilt: 0, zoom: 0 }],
      },
    });
  });
  await page.route(/\/api\/streams\/transmissions\/front\/camera\/status$/, async (route) => {
    await route.fulfill({
      json: {
        transmission_id: "front",
        camera_id: "cam_front",
        status: { pan: 0, tilt: 0, zoom: 0, move_status: "idle" },
      },
    });
  });
  await page.route(/\/api\/streams\/transmissions\/front\/camera\/(move|stop|goto-preset)$/, async (route) => {
    await route.fulfill({ json: { ok: true } });
  });
  await page.route(/\/mock-hls\/front\/index\.m3u8$/, async (route) => {
    await route.fulfill({
      headers: { "content-type": "application/vnd.apple.mpegurl" },
      body: "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:2,\nseg1.ts\n",
    });
  });
  await page.route(/\/mock-hls\/front\/seg1\.ts$/, async (route) => {
    await route.fulfill({ headers: { "content-type": "video/mp2t" }, body: "mock" });
  });
  await page.route(/\/mock-whep\/front\/whep$/, async (route) => {
    whepCalls.push({ method: route.request().method(), url: route.request().url() });
    await route.fulfill({ status: 503, body: "ICE failed" });
  });
}

test.describe("streaming dashboard chaos states", () => {
  for (const [scenario, expectedText] of [
    ["live", "Front Door"],
    ["source_stale", "Camera source stale."],
    ["stale_hls", "hls_playlist_stale"],
    ["tail_unavailable", "hls_tail_unavailable"],
    ["publisher_down", "publisher_down"],
  ]) {
    test(`shows ${scenario} with technical controls in the advanced modal`, async ({ page }) => {
      await mockStreamingDashboard(page, scenario);
      await page.goto("/");

      await expect(page.getByText("Front Door", { exact: true })).toBeVisible();
      await expect(page.getByText(expectedText, { exact: false })).toBeVisible();
      await expect(page.getByLabel("Advanced stream settings")).toBeVisible();
      await expect(page.getByLabel("Stream transport")).toHaveCount(0);

      await page.getByLabel("Advanced stream settings").click();
      await expect(page.getByRole("dialog", { name: "Stream details: Front Door" })).toBeVisible();
      await expect(page.getByLabel("Stream transport")).toBeVisible();
      await expect(page.getByLabel("Stream transport")).toContainText("Auto");
      await expect(page.getByLabel("Stream transport")).toContainText("Low latency");
      await expect(page.getByLabel("Stream transport")).toContainText("HLS");
      await expect(page.getByLabel("Stream quality")).toBeVisible();
      await expect(page.getByText("Selected output", { exact: true })).toBeVisible();
      await expect(page.getByText("1280x720", { exact: false })).toBeVisible();
    });
  }

  test("does not show recovered WebRTC transport noise as the main overlay cause", async ({ page }) => {
    await mockStreamingDashboard(page, "webrtc_transport_error", "auto");
    await page.goto("/");

    await expect(page.getByText("webrtc_transport_error", { exact: false })).toHaveCount(0);
    await page.getByLabel("Advanced stream settings").evaluate((element) => element.click());
    await expect(page.getByText("Classification")).toBeVisible();
    await expect(page.getByText("webrtc_transport_error", { exact: false })).toBeVisible();
  });

  test("desktop Auto keeps WebRTC first and falls back to HLS", async ({ page }) => {
    const whepCalls = [];
    await mockStreamingDashboard(page, "live", "auto", { whepCalls });
    await page.goto("/");

    await expect
      .poll(() => whepCalls.length, { timeout: 4000 })
      .toBeGreaterThan(0);
    await page.getByLabel("Advanced stream settings").dispatchEvent("click");
    await expect(page.getByText("HLS fallback", { exact: false }).first()).toBeVisible();
  });

  test("Low latency forces WebRTC and shows the WHEP error", async ({ page }) => {
    const whepCalls = [];
    await mockStreamingDashboard(page, "live", "webrtc", { whepCalls });
    await page.goto("/");

    await expect.poll(() => whepCalls.length, { timeout: 4000 }).toBeGreaterThan(0);
    await expect(page.getByText("WHEP negotiation failed", { exact: false })).toBeVisible();
  });

  test("Auto uses HLS when the WebRTC contract has warnings", async ({ page }) => {
    const whepCalls = [];
    await mockStreamingDashboard(page, "live", "auto", {
      whepCalls,
      urls: {
        ...URLS,
        network_contract: {
          ...URLS.network_contract,
          warnings: ["WebRTC/WHEP host is not covered by additional hosts."],
        },
      },
    });
    await page.goto("/");

    await page.waitForTimeout(800);
    expect(whepCalls).toHaveLength(0);
    await page.getByLabel("Advanced stream settings").click({ force: true });
    await expect(page.getByText("Auto -> HLS", { exact: false })).toBeVisible();
    await expect(page.getByText("WebRTC/WHEP host is not covered", { exact: false })).toBeVisible();
  });

  test("Auto uses HLS first for Home Assistant proxy access on desktop", async ({ page }) => {
    const whepCalls = [];
    await mockStreamingDashboard(page, "live", "auto", {
      urls: HOME_ASSISTANT_PROXY_URLS,
      whepCalls,
    });
    await page.goto("/");

    await page.waitForTimeout(800);
    expect(whepCalls).toHaveLength(0);
    await page.getByLabel("Advanced stream settings").click({ force: true });
    await expect(page.getByText("Auto -> HLS", { exact: false })).toBeVisible();
  });

  test("opens PTZ controls without permanently changing the transport preference", async ({ page }) => {
    await mockStreamingDashboard(page, "live", "auto");
    await page.goto("/");

    await page.getByLabel("Camera controls").evaluate((element) => element.click());
    await expect(page.getByRole("dialog", { name: "Camera controls" })).toBeVisible();
    await expect(page.getByText("Preset", { exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await page.getByLabel("Advanced stream settings").click();
    await expect(page.getByLabel("Stream transport")).toHaveValue("auto");
  });
});

test.describe("streaming dashboard mobile Home Assistant transport policy", () => {
  test.use({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
    userAgent:
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
  });

  test("uses HLS first in Auto for mobile Home Assistant proxy access", async ({ page }) => {
    const whepCalls = [];
    await mockStreamingDashboard(page, "live", "auto", {
      urls: HOME_ASSISTANT_PROXY_URLS,
      whepCalls,
    });
    await page.goto("/");

    await page.waitForTimeout(800);
    expect(whepCalls).toHaveLength(0);
    await page.getByLabel("Advanced stream settings").evaluate((element) => element.click());
    await expect(page.getByText("Auto -> HLS", { exact: false })).toBeVisible();
  });

  test("requests WebRTC only while PTZ is open, then falls back to HLS", async ({ page }) => {
    const whepCalls = [];
    await mockStreamingDashboard(page, "live", "auto", {
      urls: HOME_ASSISTANT_PROXY_URLS,
      whepCalls,
    });
    await page.goto("/");

    await page.waitForTimeout(500);
    expect(whepCalls).toHaveLength(0);
    await page.getByLabel("Camera controls").evaluate((element) => element.click());
    await expect(page.getByRole("dialog", { name: "Camera controls" })).toBeVisible();
    await expect.poll(() => whepCalls.length, { timeout: 4000 }).toBeGreaterThan(0);
    await page.keyboard.press("Escape");
    await page.getByLabel("Advanced stream settings").evaluate((element) => element.click());
    await expect(page.getByText("HLS fallback", { exact: false }).first()).toBeVisible();
  });
});
