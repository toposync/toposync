const { test, expect } = require("@playwright/test");

test("assistant keeps private filters and rejects stale or failed answers", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("toposync.locale", "pt-BR"));
  await page.route("**/api/ai/history/profiles", route => route.fulfill({ json: { profiles: [{ id: "fixture_local", name: "Perfil de ensaio" }] } }));
  await page.route("**/api/vision/identities", route => route.fulfill({ json: { identities: [{ id: "fixture_cat", name: "Gato de ensaio", species: "cat" }] } }));
  await page.route("**/api/notifications/data-options?*", route => route.fulfill({ json: { options:
    new URL(route.request().url()).searchParams.get("field") === "camera_id"
      ? [{ value: "fixture_camera", label: "Câmera de ensaio" }]
      : [{ value: "water", label: "Água" }, { value: "elsewhere", label: "Outra área" }] } }));
  let pending;
  const bodies = [];
  await page.route("**/api/ai/history/answer", async route => {
    bodies.push(route.request().postDataJSON());
    if (bodies.length === 1) pending = route;
    else await route.fulfill({ status: 409, json: { detail: "controlled selection change" } });
  });
  await page.goto("/settings");
  await page.getByRole("button", { name: /^Assistente de pipelines/ }).click();
  await page.getByRole("combobox", { name: "Tarefa", exact: true }).selectOption("history");
  const panel = page.getByRole("region", { name: "Consultar acontecimentos", exact: true });
  await panel.getByRole("textbox", { name: "O que deseja saber?", exact: true }).fill("O que o gato fez?");
  await panel.getByText("Restringir busca e escolher modo", { exact: true }).click();
  await panel.getByRole("combobox", { name: "Pessoa ou pet", exact: true }).selectOption("fixture_cat");
  await panel.getByRole("combobox", { name: "Câmera", exact: true }).selectOption("fixture_camera");
  await panel.getByRole("combobox", { name: "Área", exact: true }).selectOption("water");
  await panel.getByRole("combobox", { name: "Recuperar fontes por", exact: true }).selectOption("visual");
  await panel.getByRole("button", { name: "Consultar acontecimentos", exact: true }).click();
  await expect.poll(() => bodies.length).toBe(1);
  expect(bodies[0]).toMatchObject({ identity_id: "fixture_cat", association: "human", mode: "visual", data_filter: { camera_id: "fixture_camera", area_id: "water" } });
  await panel.getByRole("combobox", { name: "Área", exact: true }).selectOption("elsewhere");
  await pending.fulfill({ json: { notifications: [{ id: "stale", title: "STALE ANSWER" }], search: { mode: "literal", status: "ready" } } }).catch(() => {});
  await expect(panel.getByText("STALE ANSWER", { exact: true })).toHaveCount(0);
  await panel.getByRole("button", { name: "Consultar acontecimentos", exact: true }).click();
  await expect(panel.getByRole("alert")).toHaveText("As associações mudaram durante a consulta. Confira a seleção e consulte novamente.");
  await expect(panel.getByRole("combobox", { name: "Pessoa ou pet", exact: true })).toHaveValue("fixture_cat");
  await expect(panel.getByRole("combobox", { name: "Área", exact: true })).toHaveValue("elsewhere");
  await expect(panel.locator("li")).toHaveCount(0);
});
