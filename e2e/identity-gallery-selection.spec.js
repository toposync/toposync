const { test, expect } = require("@playwright/test");

test("gallery retains authorized selection on refresh and clears removed or revoked photos", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("toposync.locale", "pt-BR"));
  let identities = [{ id: "cat", name: "Nina", species: "cat", references: 0, occurrences: 1, representative_id: null }];
  await page.route("**/api/vision/identities", route => route.fulfill({ json: {
    identities, revision: 1, permissions: { curate: true, history: false },
  } }));
  let observations = ["first", "second"].map(id => ({ id, occurrence_id: "visit", species: "cat",
    identity_id: null, eligible: true, confirmed: false, reference: false, cluster_id: null, editable: true }));
  let revoked = false;
  await page.route("**/api/vision/identities/observations?*", route => revoked
    ? route.fulfill({ status: 403, json: { detail: "revoked" } })
    : route.fulfill({ json: { observations: new URL(route.request().url()).searchParams.has("identity_id") && !identities.length ? [] : observations, revision: 1, next_cursor: null } }));
  let partialAvailable = false;
  await page.route("**/api/vision/identities/observations/*/image", route => partialAvailable || route.request().url().includes("/first/image")
    ? route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><circle cx="16" cy="16" r="12"/></svg>' })
    : route.fulfill({ status: 404 }));
  await page.goto("/settings");
  await page.getByRole("button", { name: /^Pessoas e pets/ }).click();
  const gallery = page.locator(".identityGallery");
  await gallery.getByRole("checkbox", { name: "Para revisar", exact: true }).check();
  await gallery.getByRole("checkbox", { name: "Selecionar as 2 fotos exibidas do grupo 1", exact: true }).check();
  await expect(gallery.getByText("2 fotos selecionadas", { exact: true })).toBeVisible();
  await gallery.getByRole("button", { name: "Identificar ou mover seleção" }).click();
  const editor = gallery.locator("form");
  const save = editor.getByRole("button", { name: "Salvar", exact: true });
  await editor.getByRole("radio", { name: "Nina" }).check();
  await expect(save).toBeEnabled();
  await editor.getByRole("searchbox").fill("outra gata");
  await expect(editor.getByRole("radio", { name: "Nina" })).toHaveCount(0);
  await expect(save).toBeDisabled();
  let submissions = 0;
  await page.route("**/api/vision/identities/curation", route => {
    submissions++;
    return route.fulfill({ status: 409, json: { detail: "unexpected mutation" } });
  });
  await editor.evaluate(form => form.requestSubmit());
  await editor.getByRole("searchbox").fill("Nina");
  await expect(save).toBeEnabled();
  expect(submissions).toBe(0);
  await editor.locator("summary", { hasText: "Fotos de referência" }).press("Enter");
  const first = editor.getByRole("checkbox", { name: "Usar foto 1" });
  await expect(first).toBeEnabled();
  await expect(first).not.toBeChecked();
  await expect(editor.getByRole("checkbox", { name: "Usar foto 2" })).toBeDisabled();
  const retryPhoto = editor.getByRole("button", { name: "Recarregar foto 2" });
  await retryPhoto.press("Enter");
  await expect(retryPhoto).toBeEnabled();
  await expect(retryPhoto).toBeFocused();
  partialAvailable = true;
  await retryPhoto.press("Enter");
  await expect(editor.getByRole("checkbox", { name: "Usar foto 2" })).toBeFocused();
  await editor.locator('img[src*="/first/image"]').click();
  await expect(first).toBeChecked();
  const submitted = page.waitForRequest(request => request.url().endsWith("/curation") && request.method() === "POST");
  await save.press("Enter");
  const { values } = (await submitted).postDataJSON();
  expect(values.observation_ids).toEqual(["first", "second"]);
  expect(values.reference_observation_ids).toEqual(["first"]);
  await expect(editor.getByRole("alert")).toBeVisible();
  await editor.getByRole("button", { name: "Cancelar" }).click();
  await gallery.getByRole("checkbox", { name: "Selecionar as 2 fotos exibidas do grupo 1", exact: true }).check();
  const refresh = gallery.getByRole("button", { name: "Atualizar", exact: true });
  await refresh.click();
  await expect(refresh).toBeEnabled();
  await expect(gallery.getByText("2 fotos selecionadas", { exact: true })).toBeVisible();
  await expect(refresh).toBeFocused();
  observations = observations.slice(1);
  await refresh.click();
  await expect(gallery.getByText("1 foto selecionada", { exact: true })).toBeVisible();
  revoked = true;
  await refresh.click();
  await expect(gallery.getByRole("alert")).toBeVisible();
  await expect(gallery.getByRole("checkbox", { name: /Selecionar/ })).toHaveCount(0);
  revoked = false;
  await gallery.getByRole("alert").getByRole("button").click();
  await expect(gallery.getByText("0 fotos selecionadas", { exact: true })).toBeVisible();
  identities = [{ id: "profile", name: "Perfil de ensaio", species: "cat", references: 0, occurrences: 1, representative_id: null }];
  await refresh.click();
  await gallery.getByRole("checkbox", { name: "Para revisar", exact: true }).uncheck();
  await gallery.getByRole("button", { name: /Perfil de ensaio/ }).click();
  await expect(gallery.getByRole("heading", { name: "Perfil de ensaio", exact: true })).toBeVisible();
  identities = [];
  await refresh.click();
  await gallery.getByRole("checkbox", { name: "Para revisar", exact: true }).check();
  await expect(gallery.getByRole("checkbox", { name: "Selecionar foto 1", exact: true })).toBeVisible();
});
