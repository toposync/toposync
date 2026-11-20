const { test, expect } = require("@playwright/test");

test("choosing a namesake suggestion opens a draft tied to the reviewed visit", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("toposync.locale", "pt-BR"));
  const notification = { id: "suggestion-notice", type: "pipelines.event", title: "Visita com sugestões", priority: "high",
    payload: { data: { recognition: { occurrence_id: "suggestion-visit" } } } };
  const identities = [1, 2, 3].map(index => ({ id: String(index), name: "Nina", species: "cat", references: index,
    representative_id: null, occurrences: index }));
  let revision = 1;
  let candidateIds = ["2", "1"];
  let submissions = 0;
  let retained = true;
  await page.route(/\/api\/notifications(?:\?.*)?$/, route => route.fulfill({ json: { notifications: [notification], next_cursor: null } }));
  await page.route("**/api/notifications/suggestion-notice", route => route.fulfill({ json: notification }));
  await page.route("**/api/vision/identities", route => route.fulfill({ json: { identities, revision, permissions: { curate: true } } }));
  await page.route("**/api/vision/identities/occurrences/suggestion-visit", route => route.fulfill({ json: {
    id: "suggestion-visit", closed: true, species: "cat", revision, identities: identities.slice(0, 2), editable: true,
    decision: { identity_id: null, status: "suggested", candidate_ids: candidateIds },
    observations: [
      { id: "vector-only", has_image: false, editable: true, eligible: true, reference: false, confirmed: false },
      { id: "photo", has_image: retained, editable: true, eligible: true, reference: false, confirmed: false },
    ],
  } }));
  await page.route("**/api/vision/identities/observations/photo/image", route => route.fulfill({
    contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"/>',
  }));
  await page.route("**/api/vision/identities/curation", route => {
    submissions++;
    return route.fulfill({ status: 409, json: { detail: "unexpected mutation" } });
  });

  await page.goto("/#/main?notification=suggestion-notice");
  const notice = page.getByRole("button", { name: /Visita com sugestões/ }).first();
  const toggle = page.locator(".notificationsToggleButton:visible");
  await expect(notice.or(toggle).first()).toBeVisible();
  if (!await notice.isVisible()) await toggle.click();
  await notice.click();
  const dialog = page.getByRole("dialog");
  if (!await dialog.isVisible()) await page.getByRole("button", { name: /detalhes da notifica/i }).click();
  const cover = dialog.getByRole("button", { name: "Ver foto retida" });
  await expect(cover.locator("img")).toHaveAttribute("src", /observations\/photo\/image/);
  const suggestions = dialog.getByRole("button", { name: /^Escolher Nina/ });
  await expect(suggestions).toHaveCount(2);
  await expect(suggestions.first()).toHaveAccessibleName("Escolher Nina · sugestão 1 de 2 · 2 fotos de referência");
  await suggestions.first().press("Enter");
  const form = dialog.locator(".identityForm");
  const chosen = form.getByRole("radio", { name: "Nina · sugestão 1 de 2 · 2 fotos de referência" });
  await expect(chosen).toBeChecked();
  await expect(chosen).toHaveValue("2");
  await expect(form.getByRole("radio", { name: /Nina · perfil 3 de 3/ })).not.toBeChecked();
  await form.getByRole("button", { name: "Cancelar" }).press("Enter");
  await expect(suggestions.first()).toBeFocused();
  await suggestions.first().press("Enter");
  const save = form.getByRole("button", { name: "Salvar" });
  await expect(save).toBeEnabled();
  candidateIds = ["1"];
  await page.evaluate(() => { window.dispatchEvent(new Event("blur")); window.dispatchEvent(new Event("focus")); });
  await expect(form.getByRole("alert")).toContainText("Esse perfil deixou de ser sugerido");
  await expect(save).toBeDisabled();
  await form.evaluate(element => element.requestSubmit());
  expect(submissions).toBe(0);
  await form.getByRole("button", { name: "Cancelar" }).press("Enter");
  await suggestions.first().press("Enter");
  await expect(save).toBeEnabled();
  revision++;
  await page.evaluate(() => { window.dispatchEvent(new Event("blur")); window.dispatchEvent(new Event("focus")); });
  await expect(form.getByRole("alert")).toContainText("A galeria mudou");
  await expect(save).toBeDisabled();
  await form.evaluate(element => element.requestSubmit());
  expect(submissions).toBe(0);
  await form.getByRole("button", { name: "Cancelar" }).press("Enter");
  await suggestions.first().press("Enter");
  await expect(form.getByRole("heading", { name: "Quem é?" })).toBeFocused();
  await form.locator("summary", { hasText: "Fotos de referência" }).click();
  await form.getByRole("checkbox", { name: "Usar foto 1" }).check();
  await expect(save).toBeEnabled();
  retained = false;
  await page.evaluate(() => { window.dispatchEvent(new Event("blur")); window.dispatchEvent(new Event("focus")); });
  await expect(form.getByRole("alert")).toContainText("A galeria mudou");
  await expect(save).toBeDisabled();
  await form.evaluate(element => element.requestSubmit());
  expect(submissions).toBe(0);
  await form.getByRole("button", { name: "Cancelar" }).press("Enter");
  await expect(dialog.getByText("Ainda não há foto retida disponível para esta visita.")).toBeVisible();
  await expect(cover).toHaveCount(0);
  retained = true;
  await page.evaluate(() => { window.dispatchEvent(new Event("blur")); window.dispatchEvent(new Event("focus")); });
  await cover.press("Enter");
  const inspector = dialog.locator(".identityInspector");
  await expect(inspector.getByRole("heading")).toBeFocused();
  retained = false;
  await page.evaluate(() => { window.dispatchEvent(new Event("blur")); window.dispatchEvent(new Event("focus")); });
  await expect(inspector).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "Escolher outro nome" })).toBeFocused();
});
