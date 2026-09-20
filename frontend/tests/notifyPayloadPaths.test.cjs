const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const CreatableSelect = require("react-select/creatable").default;

// Extract the actual component and helper, not a parallel implementation of
// their validation or updates. No routing, effects, backend or browser starts.
function parse(relative) {
  const filename = path.resolve(__dirname, relative);
  return ts.createSourceFile(filename, fs.readFileSync(filename, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
}
function functionSource(ast, name) {
  const declaration = ast.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === name);
  assert.ok(declaration, `${name} must exist in the source`);
  return declaration.getText(ast);
}
const componentSource = functionSource(parse("../src/ui/screens/pipelines/editor/panels/CorePanels.tsx"), "NotifyConfigCard");
const helperSource = functionSource(parse("../src/ui/screens/pipelines/utils.ts"), "textConfigValue");
const localeAst = parse("../src/util/i18n.ts");
const interpolationContext = {};
vm.createContext(interpolationContext);
vm.runInContext(ts.transpileModule(functionSource(localeAst, "interpolate"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022 },
}).outputText, interpolationContext);
const localeDeclaration = localeAst.statements
  .filter(ts.isVariableStatement)
  .flatMap((node) => [...node.declarationList.declarations])
  .find((node) => node.name.getText(localeAst) === "translationsByLocale");
assert.ok(localeDeclaration && ts.isObjectLiteralExpression(localeDeclaration.initializer));
const dictionaries = {};
for (const locale of localeDeclaration.initializer.properties) {
  if (!ts.isPropertyAssignment(locale) || !ts.isObjectLiteralExpression(locale.initializer)) continue;
  const name = locale.name.text;
  dictionaries[name] = {};
  for (const entry of locale.initializer.properties) {
    if (ts.isPropertyAssignment(entry) && ts.isStringLiteral(entry.initializer)) {
      dictionaries[name][entry.name.text] = entry.initializer.text;
    }
  }
}
const prefix = "core.ui.pipelines.panels.notify.";
const json = (value) => JSON.parse(JSON.stringify(value));
function elements(root) {
  if (!root || typeof root !== "object") return [];
  if (Array.isArray(root)) return root.flatMap(elements);
  return [root, ...elements(root.props?.children)];
}
function harness(initial = {}, locale = "en", showAdvanced = true) {
  let config = initial;
  let updates = 0;
  const dictionary = dictionaries[locale];
  const t = (key, params = {}) => {
    return interpolationContext.interpolate(dictionary[key] ?? key, params);
  };
  const context = {
    exports: {}, React, CreatableSelect, pipelinesReactSelectStyles: {},
    PipelinesNumberInput: () => null,
    i18n: { useI18n: () => ({ t, locale }) },
  };
  vm.createContext(context);
  vm.runInContext(ts.transpileModule(`${helperSource}\n${componentSource}`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React },
  }).outputText, context);
  const render = () => context.exports.NotifyConfigCard({
    config, showAdvanced,
    onUpdateConfig: (updater) => { config = updater(config); updates += 1; },
  });
  const control = () => elements(render()).find((element) => element.type === CreatableSelect);
  return { render, control, config: () => config, updates: () => updates, t };
}

test("default is an empty optional selection without mutating legacy configuration", () => {
  const initial = Object.freeze({ title: "Unchanged", priority: "silent" });
  const app = harness(initial);
  assert.deepEqual(json(app.control().props.value), []);
  assert.deepEqual(json(app.control().props.options), []);
  assert.equal(app.control().props.isMulti, true);
  assert.equal(app.config(), initial);
  assert.equal(app.updates(), 0);
  assert.equal(harness(initial, "en", false).control(), undefined);
});

test("add, remove and clear preserve unrelated fields and serialize only a path array", () => {
  const initial = { title: "Human observation", realtime: false, notification_type: "fixture.human", custom: { preserved: true } };
  const app = harness(initial);
  app.control().props.onChange([{ value: "vision.poses", label: "display label" }, { value: "spatial.person_ground", label: "Ground" }]);
  assert.deepEqual(json(app.config()), { ...initial, include_payload_paths: ["vision.poses", "spatial.person_ground"] });
  assert.equal(app.config().custom, initial.custom);
  assert.equal(initial.include_payload_paths, undefined);
  const reloaded = harness(json(app.config()));
  assert.deepEqual(json(reloaded.control().props.value), [
    { value: "vision.poses", label: "vision.poses" },
    { value: "spatial.person_ground", label: "spatial.person_ground" },
  ]);
  reloaded.control().props.onChange([{ value: "spatial.person_ground", label: "Ground" }]);
  assert.deepEqual(json(reloaded.config()), { ...initial, include_payload_paths: ["spatial.person_ground"] });
  reloaded.control().props.onChange([]);
  assert.deepEqual(json(reloaded.config()), { ...initial, include_payload_paths: [] });
});

test("creation enforces simple dotted identifiers, uniqueness and 256 characters", () => {
  const control = harness({ include_payload_paths: ["vision.poses"] }).control();
  for (const valid of ["vision.gestures", "spatial.person_ground", "_field.child2", "x".repeat(256)]) {
    assert.equal(control.props.isValidNewOption(valid), true, valid);
  }
  for (const invalid of ["", " a", "a ", ".a", "a.", "a..b", "a.*", "a[0]", "a.0", "a-b", "a/b", "á", "vision.poses", "x".repeat(257)]) {
    assert.equal(control.props.isValidNewOption(invalid), false, invalid);
  }
});

test("the sixteenth path is allowed, seventeenth rejected, and removal frees a slot", () => {
  const paths = Array.from({ length: 15 }, (_, index) => `field${index}`);
  const app = harness({ include_payload_paths: paths, title: "Preserved" });
  assert.equal(app.control().props.isValidNewOption("last"), true);
  app.control().props.onChange([...paths, "last"].map((value) => ({ value, label: value })));
  assert.equal(app.config().include_payload_paths.length, 16);
  assert.equal(app.control().props.isValidNewOption("seventeenth"), false);
  app.control().props.onChange(paths.map((value) => ({ value, label: value })));
  assert.equal(app.control().props.isValidNewOption("replacement"), true);
  assert.equal(app.config().title, "Preserved");
});

for (const locale of ["en", "pt-BR"]) {
  test(`${locale}: actual translations label the control, creation and help`, () => {
    const app = harness({}, locale);
    const control = app.control();
    for (const suffix of ["payload_paths", "payload_paths_placeholder", "payload_paths_add", "payload_paths_hint"]) {
      assert.equal(typeof dictionaries[locale][prefix + suffix], "string", suffix);
      assert.ok(dictionaries[locale][prefix + suffix].length > 0, suffix);
      assert.notEqual(dictionaries.en[prefix + suffix], dictionaries["pt-BR"][prefix + suffix], suffix);
    }
    assert.equal(control.props["aria-label"], dictionaries[locale][prefix + "payload_paths"]);
    assert.equal(control.props.placeholder, dictionaries[locale][prefix + "payload_paths_placeholder"]);
    assert.equal(control.props.formatCreateLabel("vision.poses"), locale === "en" ? "Include vision.poses" : "Incluir vision.poses");
    const html = renderToStaticMarkup(app.render());
    assert.ok(html.includes(`aria-label="${dictionaries[locale][prefix + "payload_paths"]}"`));
    assert.ok(html.includes(dictionaries[locale][prefix + "payload_paths_hint"]));
    assert.match(html, /role="combobox"/);
    assert.doesNotMatch(html, /core\.ui\.pipelines\.panels\.notify\.payload_paths/);
  });
}

test("ephemeral image is opt-in, realtime-only, and preserves the sixteen payload paths", () => {
  const label = dictionaries.en[prefix + "ephemeral_image"];
  const find = (app) => elements(app.render()).find((element) => element.type === "input" && element.props["aria-label"] === label);
  const initial = { title: "Preserved", include_payload_paths: Array.from({ length: 16 }, (_, index) => `field${index}`) };
  const app = harness(initial);
  assert.equal(find(app).props.checked, false);
  assert.equal(find(app).props.disabled, false);
  find(app).props.onChange({ target: { checked: true } });
  assert.deepEqual(json(app.config()), { ...initial, include_ephemeral_image: true });
  find(app).props.onChange({ target: { checked: false } });
  assert.equal(app.config().include_ephemeral_image, false);
  const disabled = harness({ ...initial, realtime: false, include_ephemeral_image: true });
  assert.equal(find(disabled).props.disabled, true);
  assert.equal(find(disabled).props.checked, false);
  find(disabled).props.onChange({ target: { checked: true } });
  assert.equal(disabled.updates(), 0);
});

test("ephemeral image has translated label and expiration/history guidance", () => {
  for (const locale of ["en", "pt-BR"]) {
    const app = harness({}, locale, false), html = renderToStaticMarkup(app.render());
    assert.ok(html.includes(dictionaries[locale][prefix + "ephemeral_image"]));
    assert.ok(html.includes(dictionaries[locale][prefix + "ephemeral_image_hint"]));
    assert.match(html, /750 ms/);
    assert.doesNotMatch(html, /core\.ui\.pipelines\.panels\.notify\.ephemeral/);
  }
});

test("disabling realtime also disables ephemeral image configuration", () => {
  const app = harness({ realtime: true, include_ephemeral_image: true });
  const realtime = elements(app.render()).find((element) => element.type === "input"
    && element.props.type === "checkbox" && element.props.checked === true && !element.props["aria-label"]);
  assert.ok(realtime);
  realtime.props.onChange({ target: { checked: false } });
  assert.equal(app.config().realtime, false);
  assert.equal(app.config().include_ephemeral_image, false);
});
