const { test, expect, captureFirst } = require("./fixtures/visual-state");

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
