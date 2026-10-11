const { test, expect } = require("@playwright/test");
const cameraId = "source-panorama-synthetic-camera";

async function openCameras(page, locale = "en") {
  await page.addInitScript(locale => {
    localStorage.setItem("toposync.locale", locale);
    localStorage.setItem("toposync.theme", locale === "en" ? "topo-day" : "topo-night");
  }, locale);
  await page.route("**/api/cameras/processing/servers/local/status", route => route.fulfill({ json: {
    vision: { models: [{ model_id: "rfdetr_det_medium", display_name: "RF-DETR Medium", availability: "available", artifact_exists: true }] },
  } }));
  await page.goto("/settings");
  if (page.viewportSize().width < 800) await page.locator(".settingsMobileNavigation select").selectOption("com.toposync.cameras");
  else await page.getByRole("button", { name: locale === "en" ? /^Cameras\b/ : /^Câmeras\b/ }).click();
}

for (const [locale, width] of [["en", 1440], ["pt-BR", 375], ["pt-BR", 320]]) {
  test(`ONVIF catalog is read-only, partial and accessible ${locale} ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const requests = [];
    page.on("request", request => requests.push([request.method(), request.url()]));
    await openCameras(page, locale);
    await page.locator(".cameraTechnicalDetails summary").click();
    await page.getByRole("button", { name: locale === "en" ? "ONVIF resources" : "Recursos ONVIF", exact: true }).click();
    const dialog = page.getByRole("dialog").filter({ has: page.getByText(locale === "en" ? "ONVIF resources · Synthetic courtyard" : "Recursos ONVIF · Synthetic courtyard", { exact: true }) });
    await expect(dialog.getByText(/Synthetic · Fixture · 1/)).toBeVisible();
    await expect(dialog.getByText(locale === "en" ? /Some resources could not/ : /Alguns recursos não puderam/)).toBeVisible();
    expect(requests.filter(([method, url]) => method !== "GET" && /onvif|snapshot|pipelines\/presets|panorama/.test(url))).toEqual([]);
    const observe = dialog.getByRole("button", { name: locale === "en" ? "Observe events for 60 seconds" : "Observar eventos por 60 segundos" });
    await observe.click();
    await expect(dialog.getByText(/Motion: false/)).toBeVisible();
    await dialog.getByRole("button", { name: locale === "en" ? "Stop observation" : "Encerrar observação" }).click();
    expect(await dialog.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    expect(requests.filter(([, url]) => /snapshot|audio\/analyze/.test(url))).toEqual([]);
  });
}

test("people recipe saves a disabled pipeline with the qualified ONVIF gate", async ({ page, request }) => {
  await openCameras(page);
  await page.getByRole("button", { name: /People detection/ }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Use camera detection to start analysis", { exact: true }).check();
  const create = dialog.getByRole("button", { name: "Create pipeline", exact: true });
  await expect(create).toBeDisabled();
  const signals = dialog.getByLabel("Observed event from this video source");
  await signals.click();
  await dialog.getByRole("option", { name: /Motion.*RuleEngine/ }).click();
  await dialog.getByText("Advanced observation window", { exact: true }).click();
  await dialog.getByLabel("Keep analysis after inactive (seconds)", { exact: true }).fill("12.5");
  await dialog.getByLabel(/I verified that this signal covers people/).check();
  await expect(create).toBeEnabled();
  const savedRequest = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith(`/api/cameras/cameras/${cameraId}/pipelines/presets`));
  await create.click();
  const response = await savedRequest;
  expect(response.ok()).toBeTruthy();
  const saved = await request.get(`/api/pipelines/${(await response.json()).pipeline_name}`);
  const pipeline = await saved.json();
  expect(pipeline.enabled).toBe(false);
  const gate = pipeline.graph.nodes.find(node => node.operator === "camera.onvif_state_gate");
  expect(gate.config.source).toEqual({ VideoSourceToken: "synthetic-one" });
  expect(gate.config.fail_open).toBe(true);
  expect(gate.config.hold_seconds).toBe(12.5);
  expect(pipeline.graph.nodes.find(node => node.operator === "camera.source").config.gate_min_observation_seconds).toBe(5);
  expect(pipeline.graph.edges.some(edge => edge.from.node === gate.id && edge.to.port === "gate")).toBe(true);
});


test("recipe observes idle events and keeps its checkbox through interruptions", async ({ page }) => {
  let connected = false;
  let observations = 0;
  await page.route("**/onvif/capabilities*", async route => {
    const response = await route.fetch();
    const catalog = await response.json();
    await route.fulfill({ json: { ...catalog, observed_states: [], observation: "not_observed" } });
  });
  await page.route("**/onvif/observation?*", async route => {
    observations += 1;
    const response = await route.fetch();
    const observation = await response.json();
    await route.fulfill({ json: { ...observation, available: connected, states: connected ? observation.states : [] } });
  });
  await openCameras(page);
  await page.getByRole("button", { name: /People detection/ }).click();
  const dialog = page.getByRole("dialog");
  const checkbox = dialog.getByLabel("Use camera detection to start analysis", { exact: true });
  await expect(checkbox).toBeVisible();
  await dialog.getByRole("button", { name: "Refresh resources", exact: true }).click();
  await expect(checkbox).toBeEnabled();
  expect(observations).toBe(0);
  await checkbox.check();
  await expect(dialog.getByText(/Observation interrupted/)).toBeVisible();
  await expect(checkbox).toBeChecked();
  connected = true;
  const signals = dialog.getByLabel("Observed event from this video source");
  await expect(signals).toBeEnabled();
  await signals.click();
  await dialog.getByRole("option", { name: /Motion.*RuleEngine/ }).click();
  const qualify = dialog.getByLabel(/I verified that this signal covers people/);
  await qualify.check();
  const create = dialog.getByRole("button", { name: "Create pipeline", exact: true });
  await expect(create).toBeEnabled();
  connected = false;
  await expect(create).toBeDisabled();
  await expect(checkbox).toBeChecked();
  await expect(dialog.getByText("Motion", { exact: true })).toBeVisible();
  connected = true;
  await expect(qualify).toBeVisible();
  await expect(qualify).toBeChecked();
  await expect(create).toBeEnabled();
  await checkbox.uncheck();
  await expect(create).toBeEnabled();
  const stoppedAt = observations;
  await page.waitForTimeout(1800);
  expect(observations).toBeLessThanOrEqual(stoppedAt + 1);
});


test("recipe lists compatible signals with details and excludes vendor or other-source events", async ({ page }) => {
  function signals(state) {
    return [state,
      { ...state, topic: "VideoSource/MotionAlarm", qualified_topic: "{http://www.onvif.org/ver10/topics}VideoSource/MotionAlarm", item_name: "State", source: { Source: "synthetic-one" } },
      { ...state, label: "Person", topic: "RuleEngine/PeopleDetector/Person", qualified_topic: "{http://www.onvif.org/ver10/topics}RuleEngine/PeopleDetector/Person" },
      { ...state, label: "Vendor motion", qualified_topic: "{urn:vendor}RuleEngine/CellMotionDetector/Motion" },
      { ...state, label: "Other lens", source: { VideoSourceToken: "another-source" } },
    ];
  }
  await page.route("**/onvif/capabilities*", async route => {
    const response = await route.fetch();
    const catalog = await response.json();
    const states = signals(catalog.observed_states[0]);
    await route.fulfill({ json: { ...catalog, events: states.map(state => ({ ...state, is_property: true, is_boolean: true })), observed_states: states } });
  });
  await page.route("**/onvif/observation?*", async route => {
    const response = await route.fetch();
    const observation = await response.json();
    await route.fulfill({ json: { ...observation, states: signals(observation.states[0]) } });
  });
  await openCameras(page);
  await page.getByRole("button", { name: /People detection/ }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Use camera detection to start analysis", { exact: true }).check();
  await dialog.getByLabel("Observed event from this video source").click();
  await expect(dialog.getByRole("listbox").getByRole("option")).toHaveCount(3);
  await expect(dialog.getByRole("option", { name: /Person.*PeopleDetector.*synthetic-one/ })).toBeVisible();
  await dialog.getByRole("option", { name: /Person.*PeopleDetector/ }).click();
  await expect(dialog.getByLabel(/I verified that this signal covers people/)).not.toBeChecked();
  await dialog.getByRole("button", { name: "Refresh resources", exact: true }).click();
  await expect(dialog.getByLabel("Use camera detection to start analysis", { exact: true })).toBeChecked();
});
