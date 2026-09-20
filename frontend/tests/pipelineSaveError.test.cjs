const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

// Exercise the actual save handlers and editor JSX in memory. This deliberately
// does not mount routing, polling effects, network clients or the graph canvas.
const filename = path.resolve(__dirname, "../src/ui/screens/PipelinesScreen.tsx");
const source = fs.readFileSync(filename, "utf8");
const ast = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const handlers = {};
let editorExpression;
function visit(node) {
  if (ts.isVariableDeclaration(node) && ["resolveGraphFromActiveMode", "validateResolvedGraph", "handleSave"].includes(node.name.getText(ast))) {
    handlers[node.name.getText(ast)] = node.initializer.getText(ast);
  }
  if (ts.isConditionalExpression(node) && node.condition.getText(ast) === "isAggregateHome" && node.getText(ast).includes('className="pipelinesEditorInner"')) {
    editorExpression = node.getText(ast);
  }
  ts.forEachChild(node, visit);
}
visit(ast);
assert.ok(editorExpression, "editor render branch must be located");
for (const name of ["resolveGraphFromActiveMode", "validateResolvedGraph", "handleSave"]) assert.ok(handlers[name], name);
const compile = (code) => ts.transpileModule(code, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
}).outputText;

const graph = { nodes: [{ id: "unsaved-detector", operator: "vision.detect", config: { model_id: "rfdetr_det_medium" } }], edges: [] };
function harness({ validationFailure = false, saveFailure = false, draftPresent = true } = {}) {
  const noop = () => {};
  const context = {
    React, Date, isAggregateHome: false, loading: false, error: null,
    draft: draftPresent ? { name: "unsaved-pose", enabled: true, processing_server_id: "local", graph: { nodes: [], edges: [] } } : null,
    mode: "json", graphText: JSON.stringify(graph), topologyGraph: graph, topologyDirty: true,
    isDraftReadOnly: false, isPythonLocked: false, pythonText: "", interactiveWarning: null,
    recommendations: [], recommendationsError: null, recommendationsLoading: false,
    selectedServerStatus: null, selectedServerIssues: [], selectedServerStatusLoading: false,
    servers: [{ id: "local" }], pipelineIngestNotices: [], operatorsById: {}, camerasIndex: { cameras: [] },
    operatorPanels: {}, selectedRuntimeGraphInfo: null, topologyRuntimeStatus: {},
    topologyValidationLoading: false, topologyValidationError: null, topologyValidationSuccessAt: null,
    interactiveSteps: [], telemetryResetNonce: 0, telemetryResetting: false, pipelineStorageLimitBytes: 0,
    onOpenProcessingServers: noop, openTelemetryFieldInspector: noop, applyTopologyGraphChange: noop,
    validateActiveGraph: noop, discardTopologyChanges: noop, resetTelemetryAndStats: noop,
    updatePipelineStorageLimitBytes: noop, setDuplicateOpen: noop, handleDelete: noop,
    TopologyView: () => React.createElement("div", { "data-testid": "topology" }),
    PipelineTelemetryOverviewCard: () => null, PipelineStorageCard: () => null,
    t: (key) => key,
    isRecord: (value) => value !== null && typeof value === "object" && !Array.isArray(value),
    safeJsonParse: (text) => ({ ok: true, data: JSON.parse(text) }),
    localizePipelineAlert: (alert) => alert,
    pipelineAlertSeverityLabel: (severity) => severity,
    jsonPretty: (value) => JSON.stringify(value), emptyGraph: () => ({ nodes: [], edges: [] }),
    sortPipelinesForDisplay: (value) => value, setPipelines: noop,
    compilePipeline: async () => ({ alerts: validationFailure ? [{ severity: "error", code: "model_missing", message: "Measured model file is missing" }] : [] }),
    putCalls: [],
  };
  context.putPipeline = async (name, updated) => {
    context.putCalls.push({ name, updated });
    if (saveFailure) throw new Error("Save request rejected");
    return updated;
  };
  for (const field of ["error", "draft", "graphText", "topologyDirty", "topologyValidationLoading", "topologyValidationError", "topologyValidationSuccessAt", "recommendations", "recommendationsError"]) {
    context[`set${field[0].toUpperCase()}${field.slice(1)}`] = (value) => {
      context[field] = typeof value === "function" ? value(context[field]) : value;
    };
  }
  vm.createContext(context);
  vm.runInContext(compile(Object.entries(handlers).map(([name, initializer]) => `${name} = ${initializer};`).join("\n")), context);
  const render = (expression = editorExpression) => {
    vm.runInContext(compile(`rendered = (${expression});`), context);
    return context.rendered;
  };
  return { context, render };
}

function elements(root) {
  if (!root || typeof root !== "object") return [];
  if (Array.isArray(root)) return root.flatMap(elements);
  return [root, ...elements(root.props?.children)];
}

function assertEditorPreserved(tree, context) {
  const nodes = elements(tree);
  assert.ok(nodes.some((node) => node.props?.className === "pipelinesEditorInner"));
  const canvas = nodes.find((node) => node.type === context.TopologyView);
  assert.ok(canvas, "graph editor remains rendered");
  assert.equal(JSON.stringify(canvas.props.graph), JSON.stringify(graph));
  assert.equal(canvas.props.graphText, context.graphText);
  assert.equal(canvas.props.editable, true);
  assert.equal(canvas.props.dirty, true);
  assert.ok(nodes.some((node) => node.type === "button" && React.Children.toArray(node.props.children).includes("core.actions.save") && !node.props.disabled));
  for (const id of ["pipeline-enabled", "pipeline-processing-server"]) {
    const control = nodes.find((node) => node.props?.id === id);
    assert.ok(control, id);
    assert.equal(control.props.disabled, false);
    assert.equal(typeof control.props.onChange, "function");
  }
}

for (const failure of ["validationFailure", "saveFailure"]) {
  test(`${failure}: failed save keeps draft, graph, editable controls and an announced error`, async () => {
    const { context, render } = harness({ [failure]: true });
    const draftBefore = context.draft;
    const textBefore = context.graphText;
    assertEditorPreserved(render(), context);
    await context.handleSave();
    assert.equal(context.draft, draftBefore, "failed save must not replace or clear the draft");
    assert.equal(context.graphText, textBefore, "unsaved graph text survives");
    assert.equal(context.topologyDirty, true);
    assert.equal(context.putCalls.length, failure === "saveFailure" ? 1 : 0);
    const tree = render();
    assertEditorPreserved(tree, context);
    const alert = elements(tree).find((node) => node.props?.role === "alert");
    assert.ok(alert, "error must have an alert role without removing the editor");
    assert.ok(renderToStaticMarkup(alert).includes(context.error));
    const html = renderToStaticMarkup(tree);
    assert.match(html, /role="alert"/);
    assert.match(html, /id="pipeline-processing-server"/);
    assert.match(html, /data-testid="topology"/);
  });
}

test("load error without a draft still blocks the editor", () => {
  const { context, render } = harness({ draftPresent: false });
  context.error = "Load failed";
  const html = renderToStaticMarkup(render());
  assert.match(html, /Load failed/);
  assert.doesNotMatch(html, /pipelinesEditorInner|data-testid="topology"|pipeline-enabled/);
});

test("negative control: the former unconditional error branch loses controls", () => {
  assert.ok(editorExpression.includes("error && !draft"));
  const { context, render } = harness();
  context.error = "Save rejected";
  const formerBranch = editorExpression.replace("error && !draft", "error");
  assert.throws(() => assertEditorPreserved(render(formerBranch), context));
  assertEditorPreserved(render(), context);
});
