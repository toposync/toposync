const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const { applyNodeChanges } = require("@xyflow/react");

// Exercise actual callbacks wired to ReactFlow, with real node-change handling.
// This does not mount a canvas or claim native keyboard/browser qualification.
const filename = path.resolve(__dirname, "../src/ui/screens/pipelines/topology/TopologyView.tsx");
const source = fs.readFileSync(filename, "utf8");
const ast = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const callbacks = {}, bindings = {};
function visit(node) {
  if (ts.isVariableDeclaration(node) && ["handleNodesChange", "handleEdgesChange"].includes(node.name.getText(ast))) {
    callbacks[node.name.getText(ast)] = node.initializer.getText(ast);
  }
  if (ts.isJsxOpeningElement(node) && node.tagName.getText(ast) === "ReactFlow") {
    for (const attribute of node.attributes.properties) {
      if (!ts.isJsxAttribute(attribute)) continue;
      const name = attribute.name.getText(ast);
      if (["onNodesChange", "onEdgesChange"].includes(name) && ts.isJsxExpression(attribute.initializer)) {
        bindings[name] = attribute.initializer.expression?.getText(ast);
      }
    }
  }
  ts.forEachChild(node, visit);
}
visit(ast);
const compile = (code) => ts.transpileModule(code, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const plain = (value) => JSON.parse(JSON.stringify(value));

function harness(canEdit = true) {
  const context = { canEdit, useCallback: (callback) => callback, applyNodeChanges,
    selection: { kind: "summary" },
    canvasNodes: [{ id: "source", position: { x: 0, y: 0 }, data: {} }, { id: "detect", position: { x: 200, y: 0 }, data: {} }],
  };
  context.setSelection = (update) => { context.selection = typeof update === "function" ? update(context.selection) : update; };
  context.setCanvasNodes = (update) => { context.canvasNodes = typeof update === "function" ? update(context.canvasNodes) : update; };
  vm.createContext(context);
  vm.runInContext(compile(Object.entries(callbacks).map(([name, initializer]) => `${name} = ${initializer};`).join("\n")), context);
  for (const [name, expression] of Object.entries(bindings)) vm.runInContext(compile(`${name} = ${expression};`), context);
  return {
    context,
    emit(kind, changes) { context[kind === "node" ? "onNodesChange" : "onEdgesChange"]?.(changes); },
    selection: () => plain(context.selection),
  };
}
const select = (id, selected = true) => ({ type: "select", id, selected });

test("ReactFlow wires both node and edge selection callbacks", () => {
  const { context } = harness();
  assert.equal(typeof context.onNodesChange, "function");
  assert.equal(typeof context.onEdgesChange, "function", "keyboard edge selection must reach the controlled inspector state");
});

for (const kind of ["node", "edge"]) {
  test(`${kind}: select updates the inspector and only deselecting the current item clears it`, () => {
    const h = harness();
    h.emit(kind, [select("first")]);
    assert.deepEqual(h.selection(), { kind, id: "first" });
    h.emit(kind, [select("unrelated", false)]);
    assert.deepEqual(h.selection(), { kind, id: "first" });
    h.emit(kind, [select("first", false)]);
    assert.deepEqual(h.selection(), { kind: "summary" });
  });

  test(`${kind}: a mixed selection batch keeps the newly selected item in either change order`, () => {
    for (const changes of [[select("first", false), select("second")], [select("second"), select("first", false)]]) {
      const h = harness();
      h.context.selection = { kind, id: "first" };
      h.emit(kind, changes);
      assert.deepEqual(h.selection(), { kind, id: "second" });
    }
  });

  test(`${kind}: readonly still permits keyboard inspection and deselection`, () => {
    const h = harness(false);
    h.emit(kind, [select("readonly-item")]);
    assert.deepEqual(h.selection(), { kind, id: "readonly-item" });
    h.emit(kind, [select("readonly-item", false)]);
    assert.deepEqual(h.selection(), { kind: "summary" });
  });
}

test("interleaved node and edge deselections cannot erase a newer selection of the other kind", () => {
  const h = harness();
  h.context.selection = { kind: "node", id: "shared-id" };
  h.emit("edge", [select("shared-id")]);
  h.emit("node", [select("shared-id", false)]);
  assert.deepEqual(h.selection(), { kind: "edge", id: "shared-id" });
  h.emit("node", [select("detect")]);
  h.emit("edge", [select("shared-id", false)]);
  assert.deepEqual(h.selection(), { kind: "node", id: "detect" });
});

test("position changes preserve the current inspector and continue moving editable nodes", () => {
  const h = harness();
  h.context.selection = { kind: "edge", id: "source-to-detect" };
  h.emit("node", [{ type: "position", id: "source", position: { x: 24, y: 48 }, dragging: true }]);
  assert.deepEqual(h.selection(), { kind: "edge", id: "source-to-detect" });
  assert.deepEqual(plain(h.context.canvasNodes[0].position), { x: 24, y: 48 });
});

test("readonly selection does not enable position changes", () => {
  const h = harness(false);
  h.emit("node", [select("source"), { type: "position", id: "source", position: { x: 24, y: 48 } }]);
  assert.deepEqual(h.selection(), { kind: "node", id: "source" });
  assert.deepEqual(plain(h.context.canvasNodes[0].position), { x: 0, y: 0 });
});
