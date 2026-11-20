const { test, expect } = require("@playwright/test");

test("choosing a namesake suggestion opens a draft tied to the reviewed visit", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("toposync.locale", "pt-BR"));
  const notification = { id: "suggestion-notice", type: "pipelines.event", title: "Visita com sugestões", priority: "high",
    payload: { data: { recognition: { occurrence_id: "suggestion-visit" } } } };
  const identities = [1, 2, 3].map(index => ({ id: String(index), name: "Nina", species: "cat", references: index,
    representative_id: null, occurrences: index }));
  let revision = 1;
  let submissions = 0;
  await page.route(/\/api\/notifications(?:\?.*)?$/, route => route.fulfill({ json: { notifications: [notification], next_cursor: null } }));
  await page.route("**/api/notifications/suggestion-notice", route => route.fulfill({ json: notification }));
  await page.route("**/api/vision/identities", route => route.fulfill({ json: { identities, revision, permissions: { curate: true } } }));
  await page.route("**/api/vision/identities/occurrences/suggestion-visit", route => route.fulfill({ json: {
    id: "suggestion-visit", closed: true, species: "cat", revision, identities: identities.slice(0, 2), editable: true,
    decision: { identity_id: null, status: "suggested", candidate_ids: ["2", "1"] },
    observations: [{ id: "photo", editable: true, eligible: true, reference: false, confirmed: false }],
  } }));
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
  revision++;
  await page.evaluate(() => { window.dispatchEvent(new Event("blur")); window.dispatchEvent(new Event("focus")); });
  await expect(form.getByRole("alert")).toContainText("A galeria mudou");
  await expect(save).toBeDisabled();
  await form.evaluate(element => element.requestSubmit());
  expect(submissions).toBe(0);
});
