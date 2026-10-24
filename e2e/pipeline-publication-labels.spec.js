const { test, expect } = require("@playwright/test");
const { installPipelineEditorFixture, openPipelineEditor } = require("./fixtures/pipeline-editor");

test("publication names preserve spaces while typing and after saving", async ({ page }) => {
  const fixture = await installPipelineEditorFixture(page);
  await page.addInitScript(() => localStorage.setItem("toposync.locale", "en"));
  fixture.pipeline.graph.nodes[1] = {
    id: "store", operator: "stream.publish_video", config: { publication_enabled: true },
  };
  await page.route("**/api/pipelines/operators", route => route.fulfill({ json: {
    operators: ["camera.source", "stream.publish_video"].map(id => ({
      id, inputs: id === "camera.source" ? [] : [{ name: "in", type: "frame" }],
      outputs: [{ name: "out", type: "frame" }], capabilities: [], defaults: {}, config_schema: {},
    })),
  } }));
  await openPipelineEditor(page);
  await page.locator('.react-flow__node[data-id="store"]').click();
  const names = [
    ["Transmission name", "Garage reviewed video", "publication_live_view_label"],
    ["Variant name", "Synthetic analysis view", "publication_variant_label"],
  ];
  for (const [label, value] of names) {
    const input = page.getByLabel(label, { exact: true });
    await input.fill("");
    await input.pressSequentially(value);
    await expect(input).toHaveValue(value);
  }
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => fixture.saves.length).toBe(1);
  for (const [, value, key] of names) expect(fixture.saves[0].graph.nodes[1].config[key]).toBe(value);
  await page.reload();
  await page.locator('.react-flow__node[data-id="store"]').click();
  for (const [label, value] of names) await expect(page.getByLabel(label, { exact: true })).toHaveValue(value);
  expect(fixture.unexpected).toEqual([]);
});
