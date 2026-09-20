const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");
const vm = require("node:vm");

const panelDirectory = path.resolve(__dirname, "../src/ui/screens/pipelines/editor/panels");
const context = { exports: {} };
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(panelDirectory, "visionCatalogSelection.ts"), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, context);
const { resolveVisionCatalogSelection: resolve } = context.exports;
const available = { modelId: "rfdetr_det_medium", availability: "available", artifactExists: true };
const missing = { modelId: "second", availability: "manifest_only", artifactExists: false };
const input = (override = {}) => ({ serverId: "local", responseServerId: "local", responseOk: true,
  loading: false, error: null, items: [available, missing], modelId: available.modelId, ...override });

for (const [name, override, state] of [
  ["first render before effect", { responseServerId: undefined, responseOk: false, items: null }, "checking"],
  ["initial fetch pending", { loading: true, responseServerId: undefined, items: null }, "checking"],
  ["refresh pending despite a previous ready response", { loading: true }, "checking"],
  ["fetch failed despite a previous ready response", { error: "offline" }, "failed"],
  ["unsuccessful response without error prose", { responseOk: false }, "unavailable"],
  ["task catalog absent", { items: null }, "unavailable"],
  ["valid but empty catalog", { items: [] }, "model_missing"],
  ["selected model missing in nonempty catalog", { modelId: "not-listed" }, "model_missing"],
  ["model not selected", { modelId: "" }, "select_model"],
  ["server switched before effect", { serverId: "remote" }, "checking"],
  ["late previous-server response", { serverId: "remote", responseServerId: "local", loading: false }, "checking"],
  ["static fallback never establishes readiness", { items: [{ ...available, availabilityReason: "fallback" }] }, "model_missing"],
]) {
  test(`${name}: no selected model is reported ready`, () => {
    const result = resolve(input(override));
    assert.equal(result.state, state);
    assert.equal(result.ready, false);
    assert.equal(result.item, null);
    assert.ok(!result.readyItems.some((item) => item.availabilityReason === "fallback"));
    if (["checking", "failed", "unavailable"].includes(state)) assert.equal(result.readyItems.length, 0);
  });
}

test("current server and model can be ready only with availability and artifact evidence", () => {
  const result = resolve(input());
  assert.equal(result.state, "resolved");
  assert.equal(result.ready, true);
  assert.equal(result.item, available);
  assert.equal(result.readyItems.length, 1);
  for (const item of [
    { ...available, availability: "manifest_only" },
    { ...available, availability: "incompatible" },
    { ...available, artifactExists: false },
  ]) assert.equal(resolve(input({ items: [item] })).ready, false);
});

test("switching models or servers cannot inherit readiness", () => {
  assert.equal(resolve(input()).ready, true);
  const nextModel = resolve(input({ modelId: "second" }));
  assert.equal(nextModel.item, missing);
  assert.equal(nextModel.ready, false);
  const remote = resolve(input({ serverId: "remote", responseServerId: "remote",
    items: [{ ...available, availability: "manifest_only", artifactExists: false }] }));
  assert.equal(remote.state, "resolved");
  assert.equal(remote.ready, false);
});

test("the panel's actual provisioning item, status and summary consume verified selection", () => {
  const filename = path.join(panelDirectory, "VisionPanels.tsx");
  const source = fs.readFileSync(filename, "utf8");
  const ast = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const declarations = {};
  const names = ["manualInstallItem", "selectedModelIncompatible", "provisionStatusTone", "provisionSummary"];
  function visit(node) {
    if (ts.isVariableDeclaration(node) && names.includes(node.name.getText(ast))) {
      declarations[node.name.getText(ast)] = `const ${node.getText(ast)};`;
    }
    ts.forEachChild(node, visit);
  }
  visit(ast);
  for (const name of names) assert.ok(declarations[name], name);
  const snippet = `${names.map((name) => declarations[name]).join("\n")}\nresult={item:manualInstallItem,tone:provisionStatusTone,summary:provisionSummary};`;
  for (const override of [{ responseServerId: undefined, items: null }, { loading: true },
    { error: "offline" }, { items: [] }, { serverId: "remote" }, { modelId: "second" }, {}]) {
    const selection = resolve(input(override));
    const sandbox = { catalogSelection: selection,
      // A ready-looking picker fallback must never leak into provisioning.
      selectedCatalogItem: { ...available, availabilityReason: "fallback" },
      manualInstallBusy: false, manualInstallFailed: false, manualLocalBuildActionable: false,
      t: (key) => key };
    vm.runInNewContext(snippet, sandbox);
    assert.equal(sandbox.result.item, selection.item);
    assert.equal(sandbox.result.tone === "ready", selection.ready);
    assert.equal(sandbox.result.summary.endsWith("summary_ready"), selection.ready);
  }
});

test("all unknown-catalog states have matching English and Portuguese messages", () => {
  const source = fs.readFileSync(path.resolve(__dirname, "../src/util/i18n.ts"), "utf8");
  for (const state of ["checking", "failed", "unavailable", "model_missing", "select_model"]) {
    const key = `core.ui.pipelines.panels.yolo.provisioning.catalog_${state}`;
    const matches = [...source.matchAll(new RegExp(`"${key.replaceAll(".", "\\.")}"\\s*:\\s*"([^"]+)"`, "g"))];
    assert.equal(matches.length, 2, key);
    const parameters = (text) => [...text.matchAll(/{{(\w+)}}/g)].map((match) => match[1]).sort();
    assert.deepEqual(parameters(matches[0][1]), parameters(matches[1][1]), key);
  }
});
