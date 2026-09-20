const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const filename = path.resolve(__dirname, "../src/ui/screens/pipelines/topology/TopologyConnectionEditor.tsx");
const prefix = "core.ui.pipelines.topology.connection.";
const localeFile = path.resolve(__dirname, "../src/util/i18n.ts");
const ast = ts.createSourceFile(localeFile, fs.readFileSync(localeFile, "utf8"), ts.ScriptTarget.Latest, true);
const locales = ast.statements.filter(ts.isVariableStatement).flatMap(s => [...s.declarationList.declarations])
  .find(d => d.name.getText(ast) === "translationsByLocale").initializer;
const dictionaries = Object.fromEntries(locales.properties.filter(ts.isPropertyAssignment).map(locale => [locale.name.text,
  Object.fromEntries(locale.initializer.properties.filter(p => ts.isPropertyAssignment(p) && ts.isStringLiteral(p.initializer))
    .map(p => [p.name.text, p.initializer.text]))]));
const node = (id, outputs = ["out"], inputs = ["in"]) => ({ id, data: { label: `Label ${id}`, outputPorts: outputs, inputPorts: inputs } });
const elements = tree => !tree || typeof tree !== "object" ? [] : Array.isArray(tree) ? tree.flatMap(elements)
  : [tree, ...elements(tree.props?.children)];
const plain = value => JSON.parse(JSON.stringify(value));

// Stateful render harness for the actual component; native focus is tested separately in Chrome.
function harness({ editable = true, locale = "en", result = null } = {}) {
  assert.ok(fs.existsSync(filename), "keyboard connection editor must exist");
  const source = node("source", ["video", "events"]);
  const target = node("target", [], ["image", "gate"]);
  const props = { node: source, model: { nodes: [source, target], edges: [] }, editable };
  const calls = [];
  props.onConnect = connection => { calls.push(plain(connection)); return result; };
  let cursor = 0;
  const state = [];
  const hooks = { ...React,
    useState(initial) {
      const index = cursor++;
      if (!(index in state)) state[index] = typeof initial === "function" ? initial() : initial;
      return [state[index], value => { state[index] = typeof value === "function" ? value(state[index]) : value; }];
    },
    useId: () => "test-connection",
  };
  const dictionary = dictionaries[locale];
  const context = { exports: {}, require(name) {
    if (name === "react") return hooks;
    if (name === "react/jsx-runtime") return require(name);
    if (name.endsWith("/i18n")) return { i18n: { useI18n: () => ({ t: (key, params, fallback) => dictionary[key] ?? fallback ?? key }) } };
    throw new Error(`Unexpected dependency ${name}`);
  }};
  vm.createContext(context);
  vm.runInContext(ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, context);
  // React's jsx runtime is separate from the hook adapter.
  return {
    props, calls,
    render() { cursor = 0; return context.exports.TopologyConnectionEditor(props); },
    controls() { return elements(this.render()).filter(e => e.type === "select"); },
    button() { return elements(this.render()).find(e => e.type === "button"); },
    change(index, value) { this.controls()[index].props.onChange({ target: { value } }); },
    submit() { this.button().props.onClick(); },
  };
}

test("native labeled controls expose exact output and destination ports; submit starts disabled", () => {
  const h = harness();
  const controls = h.controls();
  assert.equal(controls.length, 2);
  const html = renderToStaticMarkup(h.render());
  for (const control of controls) {
    assert.ok(control.props.id);
    assert.ok(html.includes(`for="${control.props.id}"`));
  }
  assert.ok(elements(controls[0]).some(e => e.type === "option" && e.props.value === "events"));
  assert.ok(elements(controls[1]).some(e => e.type === "option" && e.props.value === JSON.stringify(["target", "gate"])));
  assert.equal(h.button().props.disabled, true);
});

test("chosen ports are submitted once and success stays in a live status region", () => {
  const h = harness();
  h.change(0, "events"); h.change(1, JSON.stringify(["target", "gate"]));
  assert.equal(h.button().props.disabled, false);
  h.submit();
  assert.deepEqual(h.calls, [{ source: "source", sourceHandle: "events", target: "target", targetHandle: "gate" }]);
  assert.ok(elements(h.render()).some(e => e.props?.role === "status" && e.props.children));
});

test("validation errors are announced without clearing selected ports; editing clears feedback", () => {
  const h = harness({ result: "Input already occupied" });
  h.change(1, JSON.stringify(["target", "image"])); h.submit();
  assert.ok(renderToStaticMarkup(h.render()).includes('role="alert"'));
  assert.ok(renderToStaticMarkup(h.render()).includes("Input already occupied"));
  assert.equal(h.controls()[1].props.value, JSON.stringify(["target", "image"]));
  h.change(1, JSON.stringify(["target", "gate"]));
  assert.ok(!renderToStaticMarkup(h.render()).includes("Input already occupied"));
});

test("removed destination or output cannot submit stale ports", () => {
  for (const remove of [h => { h.props.model.nodes = [h.props.node]; }, h => { h.props.node.data.outputPorts = []; }]) {
    const h = harness(); h.change(1, JSON.stringify(["target", "image"]));
    remove(h);
    const button = h.button();
    if (button) { assert.equal(button.props.disabled, true); h.submit(); }
    assert.deepEqual(h.calls, []);
  }
});

test("readonly and missing callback expose no actionable connection controls", () => {
  for (const h of [harness({ editable: false }), harness()]) {
    if (h.props.editable) h.props.onConnect = undefined;
    assert.equal(h.controls().length, 0);
    assert.equal(h.button(), undefined);
  }
});

for (const locale of ["en", "pt-BR"]) {
  test(`${locale}: connection instructions and native controls use actual translations`, () => {
    const h = harness({ locale });
    for (const suffix of ["title", "output", "destination", "choose", "connect", "success", "hint"]) {
      assert.equal(typeof dictionaries[locale][prefix + suffix], "string", suffix);
      assert.notEqual(dictionaries.en[prefix + suffix], dictionaries["pt-BR"][prefix + suffix], suffix);
    }
    const html = renderToStaticMarkup(h.render());
    for (const suffix of ["title", "output", "destination", "connect", "hint"]) {
      const escaped = renderToStaticMarkup(React.createElement(React.Fragment, null, dictionaries[locale][prefix + suffix]));
      assert.ok(html.includes(escaped), suffix);
    }
  });
}
