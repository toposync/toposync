const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");

const root = path.resolve(__dirname, "../..");
const modules = new Map();
function load(relative) {
  const filename = path.resolve(root, relative);
  if (modules.has(filename)) return modules.get(filename);
  const exports = {};
  modules.set(filename, exports);
  const source = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  vm.runInNewContext(source, { exports, require: name => name.startsWith(".")
    ? load(path.resolve(path.dirname(filename), name) + ".ts") : require(name) }, { filename });
  return exports;
}

const { i18n } = load("frontend/src/util/i18n.ts");
const { pipelineOperatorTranslations } = load("frontend/src/util/pipelineOperatorTranslations.ts");
const { prettyConfigKeyLabel, prettyOperatorName, prettyOperatorDescription } = load("frontend/src/ui/screens/pipelines/utils.ts");
const { buildYoloCategoryOptions } = load("frontend/src/ui/screens/pipelines/constants.ts");

test("operator translations have both locales and matching interpolation parameters", () => {
  const bundle = pipelineOperatorTranslations;
  assert.deepEqual(Object.keys(bundle.en).sort(), Object.keys(bundle["pt-BR"]).sort());
  const parameters = value => [...value.matchAll(/{{\s*([^}]+?)\s*}}/g)].map(match => match[1]).sort();
  for (const key of Object.keys(bundle.en)) {
    assert.ok(bundle.en[key].trim(), key);
    assert.ok(bundle["pt-BR"][key].trim(), key);
    assert.deepEqual(parameters(bundle.en[key]), parameters(bundle["pt-BR"][key]), key);
  }
});

test("generic parameters and operator catalog follow language changes at runtime", () => {
  i18n.setLocale("en");
  assert.equal(prettyConfigKeyLabel("target_fps"), "Target frames per second");
  assert.equal(prettyConfigKeyLabel("history"), "History size (frames)");
  assert.equal(prettyOperatorName("core.all_gate"), "Require all conditions");
  assert.match(prettyOperatorDescription("camera.audio_source"), /^Receives audio/);
  i18n.setLocale("pt-BR");
  assert.equal(prettyConfigKeyLabel("target_fps"), "Quadros por segundo desejados");
  assert.equal(prettyConfigKeyLabel("maximum_frame_age_ms"), "Idade máxima do quadro (milissegundos)");
  assert.equal(prettyConfigKeyLabel("mode"), "Modo");
  assert.equal(prettyConfigKeyLabel("missing"), "Valor quando o campo estiver ausente");
  assert.equal(prettyOperatorName("core.all_gate"), "Exigir todas as condições");
  assert.match(prettyOperatorDescription("camera.audio_source"), /^Recebe áudio/);
  assert.equal(prettyConfigKeyLabel("custom_extension_field"), "Custom Extension Field");
  assert.equal(i18n.t("core.ui.pipelines.select.create", { value: "custom" }), "Adicionar custom");
});

test("all standard detection categories translate without changing saved values", () => {
  i18n.setLocale("en");
  const english = buildYoloCategoryOptions(i18n.t);
  i18n.setLocale("pt-BR");
  const portuguese = buildYoloCategoryOptions(i18n.t);
  assert.equal(portuguese.length, 80);
  assert.deepEqual(Array.from(portuguese, item => item.value), Array.from(english, item => item.value));
  assert.equal(portuguese.find(item => item.value === "person").label, "Pessoas");
  assert.equal(portuguese.find(item => item.value === "traffic light").label, "Semáforo");
  assert.equal(portuguese.find(item => item.value === "teddy bear").label, "Urso de pelúcia");
  for (const item of portuguese) {
    assert.notEqual(i18n.t(`core.ui.pipelines.panels.yolo.category.${item.value}`), `core.ui.pipelines.panels.yolo.category.${item.value}`);
  }
});
