const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const root = path.resolve(__dirname, "../src");
const compile = (source) => ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React, esModuleInterop: true },
}).outputText;
const lensContext = { exports: {} };
vm.runInNewContext(compile(fs.readFileSync(path.join(root, "groundLensEditor.ts"), "utf8")), lensContext);
const source = fs.readFileSync(path.join(root, "elements/CameraGroundMappingModal.tsx"), "utf8");
const ast = ts.createSourceFile("modal.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const declarations = {};
const functions = {};
const imageHandlers = {};
function visit(node) {
  if (ts.isVariableDeclaration(node)) declarations[node.name.getText(ast)] = node.initializer?.getText(ast);
  if (ts.isFunctionDeclaration(node)) functions[node.name?.text] = node.getText(ast);
  if (ts.isJsxSelfClosingElement(node) && node.tagName.getText(ast) === "img") {
    for (const prop of node.attributes.properties) {
      if (ts.isJsxAttribute(prop) && ["onLoad", "onError"].includes(prop.name.getText(ast))) imageHandlers[prop.name.getText(ast)] = prop.initializer.expression.getText(ast);
    }
  }
  ts.forEachChild(node, visit);
}
visit(ast);
function harness({ state = "ready", size = { width: 1400, height: 1016 }, calibration = size, binding = "binding" } = {}) {
  const image = { complete: true, naturalWidth: 1400, naturalHeight: 1016, getBoundingClientRect: () => ({ left: 0, top: 0, width: 1400, height: 1016 }) };
  const c = {
    ...lensContext.exports, image, snapshotState: state, snapshotUrl: "blob:current", snapshotContext: "binding",
    loadedImage: size ? { ...size, url: "blob:current", context: binding } : null,
    selectedView: { id: "view", projection_model: { source_geometry: calibration ?? { width: 1920, height: 1080 }, correspondences: [] } },
    selectedSolution: { accepted: true }, lensEditingId: null, ptzFeedbackIsActive: false, ptzCurrentMatchesView: null,
    poseIsBound: true, snapshotImageRef: { current: image }, snapshotRequestContextRef: { current: "binding" },
    isMarkingImage: true, pendingImage: null, canAddPoint: true, nextPointDescription: "point", t: (key) => key,
    useCallback: (fn) => fn, REQUIRED_FIT_POINTS: 6, REQUIRED_CHECK_POINTS: 2,
    setMessage: () => {}, setIsMarkingImage: () => {}, setImageLayoutRevision: () => {},
    pointWrites: 0, dragStarts: 0, updateSelected: () => { c.pointWrites++; },
    beginPointDrag: () => { c.dragStarts++; },
    dragRef: { current: { id: "p", side: "image" } },
    selectedIdRef: { current: "view" }, pendingImageRef: { current: { x: .5, y: .5 } },
    viewsRef: { current: [] },
  };
  c.viewsRef.current = [c.selectedView];
  for (const field of ["loadedImage", "snapshotState", "snapshotUrl", "pendingImage"]) {
    c[`set${field[0].toUpperCase()}${field.slice(1)}`] = (value) => { c[field] = value; };
  }
  vm.createContext(c);
  vm.runInContext(compile(["imageContentBox", "imagePoint", "onImagePointerDown", "onImagePointerMove"].map((name) => functions[name]).join("\n")), c);
  for (const name of ["startMarking", "addWorldPoint", "movePoint"]) vm.runInContext(compile(`${name} = ${declarations[name]}`), c);
  for (const [name, expression] of Object.entries(imageHandlers)) vm.runInContext(compile(`${name} = ${expression}`), c);
  const refresh = () => {
    for (const name of ["imageSize", "resolutionStatus", "imageEditingReady", "imageDiagnostic", "pointEditingBlocked", "canActivate"]) {
      vm.runInContext(compile(`${name} = ${declarations[name]}`), c);
    }
    c.pointEditingBlockedRef = { current: c.pointEditingBlocked };
  };
  refresh();
  return { c, refresh, event: { button: 0, clientX: 700, clientY: 508, currentTarget: image } };
}

for (const options of [
  { state: "idle", size: null }, { state: "loading" }, { state: "error" },
  { size: null }, { binding: "other-source-or-view" },
  { calibration: { width: 1920, height: 1080 } },
]) {
  test(`mark, drag, world completion and activation blocked: ${JSON.stringify(options)}`, () => {
    const { c, event } = harness(options);
    assert.equal(c.pointEditingBlocked, true);
    assert.equal(c.canActivate, false);
    c.onImagePointerDown(event);
    c.onImagePointerMove(event);
    c.addWorldPoint({ x: 2, z: 3 });
    c.movePoint("p", "world", { x: 2, z: 3 });
    assert.equal(c.pointWrites, 0);
    assert.equal(c.dragStarts, 0);
    assert.equal(c.pendingImage, null);
  });
}

test("decoded matching image permits marking; calibration remains unchanged", () => {
  const { c, event } = harness();
  const before = JSON.stringify(c.selectedView);
  assert.equal(c.canActivate, true);
  c.dragRef.current = null;
  c.onImagePointerDown(event);
  assert.equal(c.pendingImage.x, .5);
  assert.equal(c.pendingImage.y, .5);
  assert.equal(JSON.stringify(c.selectedView), before);
});

test("imagePoint refuses missing, broken, incomplete and zero-layout images", () => {
  const { c, event } = harness();
  for (const image of [null, { ...c.image, complete: false }, { ...c.image, naturalWidth: 0 }, { ...c.image, naturalHeight: 0 }]) {
    assert.equal(c.imagePoint(event, image), null);
  }
  assert.equal(c.imagePoint({ ...event, currentTarget: { getBoundingClientRect: () => ({ width: 0, height: 0 }) } }, c.image), null);
});

test("actual image load diagnoses 1400x1016 versus fallback1920x1080 without mutation", () => {
  const { c, refresh } = harness({ state: "loading", size: null, calibration: { width: 1920, height: 1080 } });
  const before = JSON.stringify(c.selectedView);
  c.onLoad({ currentTarget: c.image });
  refresh();
  assert.equal(c.snapshotState, "ready");
  assert.equal(c.loadedImage.width, 1400);
  assert.equal(c.loadedImage.height, 1016);
  assert.equal(c.resolutionStatus, "mismatch");
  assert.equal(c.canActivate, false);
  assert.equal(JSON.stringify(c.selectedView), before);
});

test("failed decode and stale load cannot authorize editing", () => {
  const { c, refresh } = harness({ state: "loading", size: null });
  c.onLoad({ currentTarget: { ...c.image } });
  assert.equal(c.loadedImage, null);
  c.snapshotRequestContextRef.current = "old-source";
  c.onLoad({ currentTarget: c.image });
  assert.equal(c.loadedImage, null);
  c.onError({ currentTarget: c.image });
  refresh();
  assert.equal(c.snapshotState, "error");
  assert.equal(c.snapshotUrl, null);
  assert.equal(c.canActivate, false);
});

const translations = { exports: {}, require: () => ({ sourcePanoramaTranslations: { en: {}, "pt-BR": {} } }) };
vm.runInNewContext(compile(fs.readFileSync(path.join(root, "translations.ts"), "utf8")), translations);
const editor = { exports: {}, require: (name) => name === "react" ? React : lensContext.exports };
vm.runInNewContext(compile(fs.readFileSync(path.join(root, "elements/CameraGroundLensEditor.tsx"), "utf8")), editor);
for (const language of ["en", "pt-BR"]) {
  test(`${language}: real lens editor announces observed dimensions and resolution mismatch`, () => {
    const dictionary = translations.exports.camerasTranslations[language];
    const view = { projection_model: { lens: { type: "identity_rectilinear_v1" }, source_geometry: { width: 1920, height: 1080 } } };
    const html = renderToStaticMarkup(React.createElement(editor.exports.CameraGroundLensEditor, {
      view, imageSize: { width: 1400, height: 1016 }, i18n: { useI18n: () => ({ t: (key) => dictionary[key] ?? key }) }, onDirty: () => {}, onApply: () => {},
    }));
    assert.match(html, /1400 × 1016/);
    assert.match(html, /role="alert"/);
    assert.ok(html.includes(dictionary["ext.cameras.ground_lens.resolution_mismatch"]));
    assert.doesNotMatch(html, /ext\.cameras\.ground_lens\./);
    assert.match(html, /value="1920"/);
    assert.match(html, /value="1080"/);
  });
}

test("lens application rejects a mismatched loaded image even with a valid dirty draft", () => {
  const editorSource = fs.readFileSync(path.join(root, "elements/CameraGroundLensEditor.tsx"), "utf8");
  const editorAst = ts.createSourceFile("lens.tsx", editorSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let apply;
  function find(node) {
    if (ts.isJsxOpeningElement(node) && node.tagName.getText(editorAst) === "button") apply = node;
    ts.forEachChild(node, find);
  }
  find(editorAst);
  assert.ok(apply);
  const attributes = Object.fromEntries(apply.attributes.properties.filter(ts.isJsxAttribute).map((prop) => [prop.name.getText(editorAst), prop.initializer?.expression?.getText(editorAst)]));
  for (const status of ["mismatch", "match"]) {
    const context = { dirty: true, validation: { value: {} }, resolutionStatus: status, draft: {}, writes: 0, setDirty: () => {} };
    context.onApply = () => context.writes++;
    vm.createContext(context);
    vm.runInContext(compile(`disabled = ${attributes.disabled}; click = ${attributes.onClick}; click();`), context);
    assert.equal(context.disabled, status === "mismatch");
    assert.equal(context.writes, status === "mismatch" ? 0 : 1);
  }
});
