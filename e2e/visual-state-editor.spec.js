const { test, expect, captureFirst } = require("./fixtures/visual-state");

test("create a visual sensor from an empty graph using the operator panels", async ({ page, visual }) => {
  await page.route("**/api/cameras/index", route => route.fulfill({ json: {
    cameras: [{ id: "synthetic", name: "Câmera simulada", enabled: true, sources: [
      { id: "video", name: "Vídeo gerado", kind: "video", enabled: true, is_default: true, ingest: { mode: "direct" } },
    ] }],
  } }));
  await page.goto(`${visual.prefix}/settings/pipelines`);
  await page.getByPlaceholder("Nome do fluxo", { exact: true }).fill("Sensor criado pela interface");
  const creation = page.waitForResponse(response => response.request().method() === "POST"
    && new URL(response.url()).pathname === `${visual.prefix}/api/pipelines`);
  await page.getByRole("button", { name: "Criar", exact: true }).click();
  const created = await (await creation).json();
  expect(created.enabled).toBe(false);
  expect(created.graph.nodes).toEqual([]);
  const add = async operator => {
    await page.getByRole("button", { name: "Adicionar nó", exact: true }).click();
    await page.getByPlaceholder("Buscar operador", { exact: true }).fill(operator);
    const choices = page.locator(".pipelineTopologyPaletteList button");
    await expect(choices).toHaveCount(1);
    await choices.click();
  };
  await add("camera.source");
  const nodes = page.locator(".react-flow__node");
  await expect(nodes).toHaveCount(1);
  const sourceId = await nodes.getAttribute("data-id");
  await page.locator(".pipelinesOperatorConfigCard").getByRole("combobox").first().click();
  await page.getByRole("option", { name: "Câmera simulada (synthetic)", exact: true }).click();
  await add("vision.visual_state");
  await expect(nodes).toHaveCount(2);
  const sensor = page.locator(`.react-flow__node:not([data-id="${sourceId}"])`);
  const sensorId = await sensor.getAttribute("data-id");
  await expect(page.locator(".react-flow__edge")).toHaveCount(1);
  await sensor.focus();
  await page.keyboard.press("Enter");
  const panel = page.locator(".visualStatePanel");
  await panel.getByRole("checkbox", { name: "Permitir imagem inteira quando não houver recorte anterior", exact: true }).check();
  await panel.getByRole("combobox", { name: "Estratégia de análise", exact: true }).selectOption("direct");
  await panel.getByRole("button", { name: "Atualizar perfis e limites", exact: true }).click();
  await panel.getByRole("combobox", { name: "Perfil de IA", exact: true }).selectOption("visual_fixture");
  await panel.getByRole("textbox", { name: "Nome", exact: true }).first().fill("Porta aberta");
  await panel.getByRole("textbox", { name: "O que identifica este estado", exact: true }).first().fill("O vão da porta está visível.");
  await panel.getByRole("textbox", { name: "Nome", exact: true }).nth(1).fill("Porta fechada");
  await panel.getByRole("textbox", { name: "O que identifica este estado", exact: true }).nth(1).fill("A folha cobre todo o vão da porta.");
  await captureFirst(panel);
  const preview = panel.getByRole("button", { name: "Testar agora", exact: true });
  await expect(preview).toBeEnabled();
  await preview.click();
  await expect(panel.locator(".visualPreviewResult")).toBeVisible();
  await page.getByRole("button", { name: "Salvar", exact: true }).click();
  await expect.poll(async () => {
    const saved = await (await visual.request.get(`${visual.prefix}/api/pipelines/${created.name}`)).json();
    return saved.graph.nodes.find(node => node.operator === "vision.visual_state")?.config.classes[0].reference_ids?.length ?? 0;
  }).toBe(1);
  const saved = await (await visual.request.get(`${visual.prefix}/api/pipelines/${created.name}`)).json();
  expect(saved.enabled).toBe(false);
  expect(saved.graph.nodes).toHaveLength(2);
  expect(saved.graph.edges).toHaveLength(1);
  const configuration = saved.graph.nodes.find(node => node.operator === "vision.visual_state").config;
  expect(configuration.classes[0].label).toBe("Porta aberta");
  expect(configuration.classes[1].label).toBe("Porta fechada");
  expect(configuration.classes.every(category => category.description.length > 0)).toBe(true);
  expect(configuration.strategy).toBe("direct");
  expect(configuration.source_identity).toBe("camera:synthetic:video");
  expect(configuration.preparation_revision).toBeTruthy();
  expect(configuration.sensor_id).toBeTruthy();
  await page.reload();
  await page.locator(`.react-flow__node[data-id="${sensorId}"]`).focus();
  await page.keyboard.press("Enter");
  await expect(panel.getByRole("textbox", { name: "Nome", exact: true }).first()).toHaveValue("Porta aberta");
  await panel.getByRole("button", { name: "Carregar fotos salvas", exact: true }).click();
  await expect(panel.locator(".visualStatePhoto img")).toBeVisible();
});

test("form and advanced JSON preserve class identities and references in both directions", async ({ page, visual }) => {
  const { panel } = visual;
  await captureFirst(panel);
  const name = panel.getByRole("textbox", { name: "Nome", exact: true }).first();
  const description = panel.getByRole("textbox", { name: "O que identifica este estado", exact: true }).first();
  await name.fill("Porta aberta");
  await description.fill("Vão da porta visível, inclusive com luz noturna.");
  await page.locator(".pipelineTopologyJsonDetails summary").click();
  const editor = page.getByRole("textbox", { name: "JSON avançado", exact: true });
  await expect(editor).toBeVisible();
  const before = JSON.parse(await editor.inputValue());
  expect(before.classes[0].label).toBe("Porta aberta");
  expect(before.classes[0].description).toBe("Vão da porta visível, inclusive com luz noturna.");
  expect(before.classes[0].reference_ids).toHaveLength(1);
  const after = structuredClone(before);
  after.classes[0].label = "Abertura confirmada";
  after.classes[0].description = "Abertura observável na região selecionada.";
  after.evaluation_interval_ms = 300;
  await editor.fill(JSON.stringify(after, null, 2));
  const apply = page.getByRole("button", { name: "Aplicar configuração", exact: true });
  await apply.click();
  await expect(name).toHaveValue(after.classes[0].label);
  await expect(description).toHaveValue(after.classes[0].description);
  await expect(apply).toBeDisabled();
  await expect(panel.locator(".visualStatePhoto")).toHaveCount(1);
  await editor.fill("{");
  await apply.click();
  await expect(page.locator(".pipelineTopologyJsonDetails .pipelineTopologyInspectorNotice")).toBeVisible();
  await expect(name).toHaveValue(after.classes[0].label);
  await editor.fill(JSON.stringify(after, null, 2));
  await expect(apply).toBeDisabled();
  await name.fill("Nome final pelo formulário");
  const final = JSON.parse(await editor.inputValue());
  expect(final.classes[0]).toEqual({ ...after.classes[0], label: "Nome final pelo formulário" });
  expect(final.evaluation_interval_ms).toBe(300);
  await page.getByRole("button", { name: "Salvar", exact: true }).click();
  await expect.poll(async () => {
    const saved = await (await visual.request.get(`${visual.prefix}/api/pipelines/${visual.pipeline.name}`)).json();
    return saved.graph.nodes.find(node => node.id === "sensor").config.classes[0].label;
  }).toBe("Nome final pelo formulário");
  const saved = await (await visual.request.get(`${visual.prefix}/api/pipelines/${visual.pipeline.name}`)).json();
  const configuration = saved.graph.nodes.find(node => node.id === "sensor").config;
  expect(configuration.classes[0].class_id).toBe(before.classes[0].class_id);
  expect(configuration.classes[0].reference_ids).toEqual(before.classes[0].reference_ids);
  expect(configuration.evaluation_interval_ms).toBe(300);
});

test("free classes and advanced settings retain their values when collapsed", async ({ page, visual }) => {
  const { panel } = visual;
  await panel.getByRole("button", { name: "Adicionar classe", exact: true }).click();
  const classes = panel.locator(".visualStateClass");
  await expect(classes).toHaveCount(3);
  await classes.nth(2).getByRole("textbox", { name: "Nome", exact: true }).fill("Entreaberta");
  await classes.nth(2).getByRole("textbox", { name: "O que identifica este estado", exact: true }).fill("Folha entre as duas posições finais.");
  const advanced = page.getByRole("button", { name: "Avançado", exact: true });
  await expect(advanced).toHaveAttribute("aria-expanded", "false");
  await advanced.click();
  await expect(advanced).toHaveAttribute("aria-expanded", "true");
  const key = panel.getByRole("textbox", { name: "Chave estável (letras, números e sublinhado)", exact: true });
  await expect(key).toHaveCount(3);
  await key.nth(2).fill("partially_open");
  await advanced.click();
  await expect(advanced).toHaveAttribute("aria-expanded", "false");
  await expect(key).toHaveCount(0);
  await advanced.click();
  await expect(key).toHaveCount(3);
  await expect(key.nth(2)).toHaveValue("partially_open");
  await panel.locator("summary").filter({ hasText: /^Tempo e estabilidade$/ }).click();
  const interval = panel.getByRole("spinbutton", { name: "Intervalo de análise (milissegundos)", exact: true });
  await expect(interval).toHaveCount(1);
  await interval.fill("350");
  await panel.getByRole("group", { name: "Configuração da decisão", exact: true })
    .getByRole("button", { name: "Aplicar ajustes", exact: true }).click();
  await page.getByRole("button", { name: "Salvar", exact: true }).click();
  await expect.poll(async () => {
    const saved = await (await visual.request.get(`${visual.prefix}/api/pipelines/${visual.pipeline.name}`)).json();
    return saved.graph.nodes.find(node => node.id === "sensor").config.classes.length;
  }).toBe(3);
  await page.reload();
  await page.locator('.react-flow__node[data-id="sensor"]').focus();
  await page.keyboard.press("Enter");
  await expect(classes.nth(2).getByRole("textbox", { name: "Nome", exact: true })).toHaveValue("Entreaberta");
  await advanced.click();
  await expect(key.nth(2)).toHaveValue("partially_open");
  await panel.locator("summary").filter({ hasText: /^Tempo e estabilidade$/ }).click();
  await expect(interval).toHaveValue("350");
  await panel.getByRole("button", { name: "Remover classe Entreaberta", exact: true }).click();
  await expect(classes).toHaveCount(2);
  await expect(panel.getByRole("button", { name: "Remover classe Aberta", exact: true })).toBeDisabled();
});

test("capture, undo, test, save and reopen through ingress", async ({ page, visual }) => {
  const { panel, pipeline } = visual;
  await captureFirst(panel);
  await panel.getByRole("button", { name: "Remover captura Aberta, 1", exact: true }).press("Enter");
  await expect(panel.locator(".visualStatePhoto")).toHaveCount(0);
  await expect(panel.getByRole("textbox", { name: "Nome", exact: true }).first()).toBeFocused();
  await panel.getByRole("button", { name: "Desfazer remoção", exact: true }).click();
  await expect(panel.locator(".visualStatePhoto")).toHaveCount(1);
  await panel.getByRole("button", { name: "Atualizar perfis e limites", exact: true }).click();
  const preview = panel.getByRole("button", { name: "Testar agora", exact: true });
  await expect(preview).toBeEnabled();
  await preview.press("Enter");
  await expect(panel.locator(".visualPreviewResult")).toBeVisible();
  await expect(preview).toBeFocused();
  await page.getByRole("button", { name: "Salvar", exact: true }).click();
  await expect.poll(async () => {
    const saved = await (await visual.request.get(`${visual.prefix}/api/pipelines/${pipeline.name}`)).json();
    return saved.graph.nodes.find(node => node.id === "sensor").config.classes[0].reference_ids?.length ?? 0;
  }).toBe(1);
  await page.reload();
  await page.locator('.react-flow__node[data-id="sensor"]').focus();
  await page.keyboard.press("Enter");
  await panel.getByRole("button", { name: "Carregar fotos salvas", exact: true }).click();
  await expect(panel.locator(".visualStatePhoto img")).toBeVisible();
  expect(await panel.locator(".visualStatePhoto img").evaluate(image => image.complete && image.naturalWidth > 0)).toBe(true);
});

test("conflicting save preserves the local edit and the winning configuration", async ({ page, visual }) => {
  const name = visual.panel.getByRole("textbox", { name: "Nome", exact: true }).first();
  await name.fill("Edição local preservada");
  const current = await (await visual.request.get(`${visual.prefix}/api/pipelines/${visual.pipeline.name}`)).json();
  current.display_name = "Alteração concorrente";
  expect((await visual.request.put(`${visual.prefix}/api/pipelines/${visual.pipeline.name}`, { data: current })).status()).toBe(200);
  await page.getByRole("button", { name: "Salvar", exact: true }).click();
  await expect(name).toHaveValue("Edição local preservada");
  await expect(page.getByText(/foi alterado|outra edição|conflito/i).first()).toBeVisible();
  const winner = await (await visual.request.get(`${visual.prefix}/api/pipelines/${visual.pipeline.name}`)).json();
  expect(winner.display_name).toBe("Alteração concorrente");
  expect(winner.graph.nodes.find(node => node.id === "sensor").config.classes[0].label).toBe("Aberta");
});

test("cancel discards a late completed preview and returns keyboard focus", async ({ page, visual }) => {
  const { panel } = visual;
  await captureFirst(panel);
  await panel.getByRole("button", { name: "Atualizar perfis e limites", exact: true }).click();
  let release, completed;
  const held = new Promise(resolve => { release = resolve; });
  const ready = new Promise(resolve => { completed = resolve; });
  await page.route("**/api/vision/visual-state/preview", async route => {
    const response = await route.fetch();
    completed();
    await held;
    await route.fulfill({ response }).catch(() => {});
  });
  const preview = panel.getByRole("button", { name: "Testar agora", exact: true });
  await expect(preview).toBeEnabled();
  await preview.click();
  try {
    await ready;
    await panel.getByRole("button", { name: "Cancelar", exact: true }).click();
    await expect(preview).toBeFocused();
  } finally { release(); }
  await expect(panel.locator(".visualPreviewResult")).toHaveCount(0);
  await expect(preview).toBeEnabled();
});
