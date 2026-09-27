const { test, expect } = require("@playwright/test");

test("gallery retains authorized selection on refresh and clears removed or revoked photos", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("toposync.locale", "pt-BR"));
  let identities = [];
  await page.route("**/api/vision/identities", route => route.fulfill({ json: {
    identities, revision: 1, permissions: { curate: true, history: false },
  } }));
  let observations = ["first", "second"].map(id => ({ id, occurrence_id: "visit", species: "cat",
    identity_id: null, eligible: true, confirmed: false, reference: false, cluster_id: null, editable: true }));
  let revoked = false;
  await page.route("**/api/vision/identities/observations?*", route => revoked
    ? route.fulfill({ status: 403, json: { detail: "revoked" } })
    : route.fulfill({ json: { observations: new URL(route.request().url()).searchParams.has("identity_id") && !identities.length ? [] : observations, revision: 1, next_cursor: null } }));
  await page.route("**/api/vision/identities/observations/*/image", route => route.fulfill({ status: 404 }));
  await page.goto("/settings");
  await page.getByRole("button", { name: /^Pessoas e pets/ }).click();
  const gallery = page.locator(".identityGallery");
  await gallery.getByRole("checkbox", { name: "Para revisar", exact: true }).check();
  await gallery.getByRole("checkbox", { name: "Selecionar as 2 fotos exibidas do grupo 1", exact: true }).check();
  await expect(gallery.getByText("2 fotos selecionadas", { exact: true })).toBeVisible();
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
