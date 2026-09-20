const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const ts = require("typescript");
const vm = require("node:vm");

// Source-contract test for the real React hosts. Runtime renderer timers are
// exercised separately in cameras/ui/tests/humanObservation.test.cjs.
for (const [filename, version] of [
  ["frontend/src/ui/main2d/MainViewport2D.tsx", "stateVersion"],
  ["frontend/src/ui/main2d/MainViewportVector2D.tsx", "stateVersion"],
  ["extensions/spatial_video/ui/src/SpatialVideoView.tsx", "version"],
]) test(`${filename} invalidates cached notification pins and passes current map elements`, () => {
  const source = fs.readFileSync(path.resolve(__dirname, "../..", filename), "utf8");
  const file = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const declarations = new Map();
  function visit(node) { if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)) declarations.set(node.name.text, node); ts.forEachChild(node, visit); }
  visit(file);
  const overlay = declarations.get("notificationOverlay").initializer;
  const pin = declarations.get("notificationPin").initializer;
  assert.ok(ts.isCallExpression(overlay) && ts.isCallExpression(pin));
  assert.ok(overlay.arguments[1].elements.some((item) => item.getText(file) === "elements"));
  assert.match(overlay.arguments[0].getText(file), /\{ compositionId, elements, requestRender(?:: invalidate)? \}/);
  assert.ok(pin.arguments[1].elements.some((item) => item.getText(file) === version));
});

test("main 3D host disposes and recreates the notification overlay after a local map edit", () => {
  const source = fs.readFileSync(path.resolve(__dirname, "../src/ui/Viewport3D.tsx"), "utf8");
  const file = ts.createSourceFile("Viewport3D.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let sync;
  const effects = [];
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === "syncNotificationOverlay") sync = node;
    if (ts.isCallExpression(node) && node.expression.getText(file) === "useEffect") effects.push(node);
    ts.forEachChild(node, visit);
  }
  visit(file);
  let created = 0, disposed = 0;
  const contexts = [];
  const ref = (current) => ({ current });
  const context = {
    console, THREE: {}, elements: [],
    notificationOverlayRef: ref(null), sceneRef: ref({ add() {}, remove() {} }),
    rendererRef: ref({}), cameraRef: ref({}), viewRef: ref({}), onOpenImageRef: ref(null),
    activeNotificationRef: ref({ id: "human-1" }),
    activeNotificationRendererRef: ref({ id: "human", create3DOverlay(ctx) {
      created++; contexts.push(ctx); return { object: {}, dispose() { disposed++; }, update() {} };
    } }),
    compositionIdRef: ref("map-1"), requestRenderRef: ref(null),
    notificationOverlayNotificationIdRef: ref(null), notificationOverlayRendererIdRef: ref(null),
    notificationOverlayCompositionIdRef: ref(null), notificationOverlayElementsRef: ref(null),
  };
  vm.createContext(context);
  vm.runInContext(ts.transpileModule(sync.getText(file), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context);
  context.syncNotificationOverlay();
  const original = contexts[0].elements;
  context.elements = [{ id: "edited-wall" }];
  context.syncNotificationOverlay();
  assert.equal(created, 2);
  assert.equal(disposed, 1);
  assert.notEqual(contexts[1].elements, original);
  assert.equal(contexts[1].elements, context.elements);
  assert.ok(effects.some((effect) => effect.arguments[1]?.elements?.some((dependency) => dependency.getText(file) === "elements")
    && effect.arguments[0].getText(file).includes("syncNotificationOverlay()")), "local element edits must trigger synchronization");
});
