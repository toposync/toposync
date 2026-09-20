const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

const directory = path.resolve(__dirname, "../src/ui/screens/pipelines");
const compile = (source) => ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
function parsed(relative) {
  const filename = path.join(directory, relative), source = fs.readFileSync(filename, "utf8");
  return ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, filename.endsWith("tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
}
// Load the real graph validator and its sole runtime dependency, rather than
// reimplementing DAG rules or importing unrelated UI/network initialization.
const utils = parsed("utils.ts"), isRecordSource = utils.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === "isRecord");
assert.ok(isRecordSource);
const utilsContext = { exports: {} };
vm.runInNewContext(compile(isRecordSource.getText(utils)), utilsContext);
const graphContext = { exports: {}, structuredClone, require: (name) => {
  assert.equal(name, "../utils"); return utilsContext.exports;
} };
vm.runInNewContext(compile(fs.readFileSync(path.join(directory, "topology/topologyGraph.ts"), "utf8")), graphContext);

const ast = parsed("topology/TopologyView.tsx"), callbacks = {};
let messageSource, inspectorBinding;
function visit(node) {
  if (ts.isVariableDeclaration(node) && ["commitGraphResult", "undoLastEdit", "handleInspectorConnect"].includes(node.name.getText(ast))) {
    callbacks[node.name.getText(ast)] = node.initializer.getText(ast);
  }
  if (ts.isFunctionDeclaration(node) && node.name?.text === "editResultMessage") messageSource = node.getText(ast);
  if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && node.tagName.getText(ast) === "TopologyInspector") {
    const attribute = node.attributes.properties.find((item) => ts.isJsxAttribute(item) && item.name.getText(ast) === "onConnect");
    if (attribute && ts.isJsxExpression(attribute.initializer)) inspectorBinding = attribute.initializer.expression?.getText(ast);
  }
  ts.forEachChild(node, visit);
}
visit(ast);
assert.ok(callbacks.commitGraphResult && callbacks.undoLastEdit && messageSource);
const plain = (value) => JSON.parse(JSON.stringify(value));
const edge = (source, target, sourcePort = "out", targetPort = "in") => ({
  uid: `${source}-${target}`, from: { node: source, port: sourcePort }, to: { node: target, port: targetPort },
});
function graph() {
  return { schema_version: 2, uid: "rollback-fixture", custom: { preserved: true },
    nodes: [
      { id: "source", operator: "camera.source", config: { camera_id: "fixture" } },
      { id: "detect", operator: "vision.detect", config: { model_id: "existing", threshold: 0.6 } },
      { id: "track", operator: "vision.track", config: { maximum_gap: 2 } },
      { id: "notify", operator: "core.notify", config: { title_template: "Existing title" } },
    ], edges: [{ ...edge("source", "detect"), queue: { max_items: 7 } }],
    layout: { nodes: { detect: { x: 120, y: 80 } } },
  };
}
const connection = () => ({ source: "detect", sourceHandle: "detections", target: "track", targetHandle: "frames" });
function harness({ canEdit = true, initial = graph() } = {}) {
  const context = { canEdit, useCallback: (callback) => callback,
    ...graphContext.exports,
    operatorsById: {
      "vision.detect": { outputs: [{ name: "detections", required: true }],
        default_output_policy: { queue: { max_items: 8, drop_policy: "latest_only" }, traffic: { modality: "image" } } },
      "vision.track": { inputs: [{ name: "frames", required: true }],
        default_input_policy: { queue: { max_items: 4 }, lifecycle: { propagate_close: true } } },
    },
    latestGraphRef: { current: initial }, undoGraphStackRef: { current: [] },
    selection: { kind: "node", id: "detect" }, pendingFocusNodeId: null, actionError: null, undoGraphStack: [], changes: [],
    t: (key, parameters, fallback) => fallback ?? key,
  };
  for (const key of ["selection", "pendingFocusNodeId", "actionError", "undoGraphStack"]) {
    context[`set${key[0].toUpperCase()}${key.slice(1)}`] = (update) => {
      context[key] = typeof update === "function" ? update(context[key]) : update;
    };
  }
  context.onChangeGraph = (next) => context.changes.push(next);
  vm.createContext(context);
  vm.runInContext(compile(messageSource + "\n" + Object.entries(callbacks).map(([name, source]) => `${name} = ${source};`).join("\n")), context);
  if (inspectorBinding) vm.runInContext(compile(`inspectorConnect = ${inspectorBinding};`), context);
  return { context, connect: (value = connection()) => context.handleInspectorConnect?.(value) };
}

test("TopologyInspector receives the actual connection handler", () => {
  const { context } = harness();
  assert.equal(typeof context.inspectorConnect, "function");
  assert.equal(context.inspectorConnect, context.handleInspectorConnect);
});

test("readonly refuses connection without changing graph, undo or selection", () => {
  const h = harness({ canEdit: false }), original = h.context.latestGraphRef.current;
  assert.equal(typeof h.connect(), "string", "readonly must report rejection, not a successful null");
  assert.equal(h.context.latestGraphRef.current, original);
  assert.equal(h.context.changes.length, 0);
  assert.equal(h.context.undoGraphStackRef.current.length, 0);
  assert.deepEqual(plain(h.context.selection), { kind: "node", id: "detect" });
});

for (const scenario of ["occupied input", "cycle", "self", "removed node"]) {
  test(`${scenario}: actual validator rejects without mutating graph, selection or undo history`, () => {
    const initial = graph(), requested = connection();
    if (scenario === "occupied input") initial.edges.push(edge("source", "track", "out", "frames"));
    if (scenario === "cycle") initial.edges = [edge("track", "detect")];
    if (scenario === "self") requested.target = "detect";
    if (scenario === "removed node") initial.nodes = initial.nodes.filter((node) => node.id !== "track");
    const before = structuredClone(initial), h = harness({ initial });
    const error = h.connect(requested);
    assert.equal(typeof error, "string");
    assert.ok(error.length > 0);
    assert.equal(h.context.latestGraphRef.current, initial);
    assert.deepEqual(initial, before);
    assert.equal(h.context.changes.length, 0);
    assert.equal(h.context.undoGraphStackRef.current.length, 0);
    assert.deepEqual(plain(h.context.selection), { kind: "node", id: "detect" });
  });
}

test("successful connection preserves source selection, configurations and operator defaults, and supports real undo", () => {
  const initial = graph(), before = structuredClone(initial), h = harness({ initial });
  assert.equal(h.connect(), null);
  const committed = plain(h.context.latestGraphRef.current);
  assert.equal(h.context.changes.length, 1);
  assert.deepEqual(committed.nodes, before.nodes);
  assert.deepEqual(committed.layout, before.layout);
  assert.deepEqual(committed.custom, before.custom);
  assert.deepEqual(committed.edges[0], before.edges[0]);
  assert.equal(committed.edges.length, 2);
  assert.deepEqual(committed.edges[1].from, { node: "detect", port: "detections" });
  assert.deepEqual(committed.edges[1].to, { node: "track", port: "frames" });
  assert.deepEqual(committed.edges[1].queue, { max_items: 4, drop_policy: "latest_only" });
  assert.deepEqual(committed.edges[1].traffic, { modality: "image" });
  assert.deepEqual(committed.edges[1].lifecycle, { propagate_close: true });
  assert.deepEqual(plain(h.context.selection), { kind: "node", id: "detect" });
  assert.equal(h.context.pendingFocusNodeId, null);
  assert.deepEqual(initial, before);
  assert.equal(h.context.undoGraphStackRef.current.length, 1);
  assert.equal(h.context.undoGraphStackRef.current[0], initial);
  h.context.undoLastEdit();
  assert.equal(h.context.latestGraphRef.current, initial);
  assert.equal(h.context.changes.length, 2);
  assert.equal(h.context.undoGraphStackRef.current.length, 0);
});
