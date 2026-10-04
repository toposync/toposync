const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");

const filename = path.resolve(__dirname, "../src/ui/screens/CompositionEditorScreen.tsx");
const nodes = tree => !tree || typeof tree !== "object" ? [] : Array.isArray(tree) ? tree.flatMap(nodes)
  : [tree, ...nodes(tree.props?.children)];

function harness(width, { elements = [], elementTypesById = {} } = {}) {
  let cursor = 0, focused = 0;
  const state = [];
  const hooks = { ...React,
    useState(initial) {
      const index = cursor++;
      if (!(index in state)) state[index] = typeof initial === "function" ? initial() : initial;
      return [state[index], value => { state[index] = typeof value === "function" ? value(state[index]) : value; }];
    },
    useRef(initial) {
      const index = cursor++;
      return state[index] ??= { current: initial };
    },
    useEffect() {}, useMemo: factory => factory(), useCallback: callback => callback,
  };
  function Viewport2D() {}
  const context = { exports: {}, window: { matchMedia: () => ({ matches: width <= 800 }) },
    localStorage: { getItem: () => null }, require(name) {
      if (name === "react") return hooks;
      if (name === "react/jsx-runtime") return require(name);
      if (name.endsWith("/i18n")) return {
        i18n: { useI18n: () => ({ locale: "pt-BR", t: key => key }) },
        resolveLocalizedString: value => typeof value === "string" ? value : value?.key ?? "",
      };
      if (name.endsWith("/Viewport2D")) return { Viewport2D };
      if (name.endsWith("/measurementLineElementType")) return { MEASUREMENT_LINE_ELEMENT_TYPE_ID: "measure" };
      return new Proxy({}, { get: () => () => null });
    },
  };
  vm.createContext(context);
  vm.runInContext(ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, context);
  const props = { elements, elementTypesById, editorTools: [], fileDropHandlers: [], activeCompositionId: "room", compositions: [] };
  const render = () => { cursor = 0; return context.exports.CompositionEditorScreen(props); };
  const find = predicate => nodes(render()).find(predicate);
  const toggle = () => find(node => node.props?.["aria-controls"] === "composition-editor-controls");
  toggle().ref.current = { focus: () => { focused += 1; } };
  return { render, find, toggle, focused: () => focused,
    panel: () => find(node => node.props?.id === "composition-editor-controls"),
    viewport: () => find(node => node.type === Viewport2D),
  };
}

test("mobile controls release the canvas and preserve tool, selection and focus through reopening and Escape", () => {
  const h = harness(390);
  assert.equal(h.toggle().props["aria-expanded"], false);
  assert.equal(h.panel().props.hidden, true);
  assert.match(h.render().props.className, /isControlsCollapsed/);
  h.viewport().props.onSelectElements(["object"]);
  h.toggle().props.onClick();
  assert.equal(h.panel().props.hidden, false);
  h.find(node => node.key === "core.select").props.onClick();
  assert.equal(h.viewport().props.interactionMode, "select");
  assert.equal(h.panel().props.hidden, true);
  assert.equal(h.focused(), 1);
  h.toggle().props.onClick();
  let prevented = false, stopped = false;
  h.panel().props.onKeyDown({ key: "Escape", preventDefault() { prevented = true; }, stopPropagation() { stopped = true; } });
  assert.equal(prevented && stopped, true);
  assert.equal(h.panel().props.hidden, true);
  assert.deepEqual(h.viewport().props.selectedElementIds, ["object"]);
  assert.equal(h.viewport().props.interactionMode, "select");
  assert.equal(h.focused(), 2);
});

test("desktop keeps tools open after selection; native Space activation does not reach canvas shortcuts", () => {
  const h = harness(1280);
  assert.equal(h.panel().props.hidden, false);
  h.find(node => node.key === "core.select").props.onClick();
  assert.equal(h.panel().props.hidden, false);
  const topbar = h.find(node => node.props?.className === "overlayTopRight");
  let stopped = 0;
  for (const handler of [topbar.props.onKeyDown, topbar.props.onKeyUp]) handler({ key: " ", stopPropagation() { stopped += 1; } });
  assert.equal(stopped, 2);
  h.toggle().props.onClick();
  assert.equal(h.panel().props.hidden, true);
  assert.equal(h.viewport().props.interactionMode, "select");
});

for (const layerGroup of [undefined, "background", "walls", "areas", "measurements"]) {
  test(`a single selected ${layerGroup ?? "ungrouped"} layer exposes editing without double click`, () => {
    const elements = ["first", "second"].map(id => ({ id, type: "test.object", name: id, position: { x: 0, y: 0, z: 0 }, rotation: { x: 0, y: 0, z: 0 }, props: {} }));
    const h = harness(390, { elements, elementTypesById: { "test.object": { name: "Object", layerGroup } } });
    const edit = () => h.find(node => node.props?.["aria-label"] === "core.ui.layers.edit");
    assert.equal(edit(), undefined);
    h.viewport().props.onSelectElements(["first"]);
    assert.ok(edit(), "A selected existing element needs a keyboard and touch editing action");
    edit().props.onClick();
    assert.equal(h.find(node => node.props?.title === "core.actions.edit: first").props.open, true);
    h.viewport().props.onSelectElements(["first", "second"]);
    assert.equal(edit(), undefined, "Do not imply a bulk editing action");
    h.viewport().props.onSelectElements([]);
    assert.equal(edit(), undefined);
  });
}
