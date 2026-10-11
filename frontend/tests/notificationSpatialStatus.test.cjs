const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const ts = require("typescript");
const THREE = require("three");
const { renderToStaticMarkup } = require("react-dom/server");

const source = fs.readFileSync(path.resolve(__dirname, "../src/ui/notifications/pipelinesNotifications.tsx"), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
} }).outputText;
const rendererModule = { exports: {} };
new Function("require", "module", "exports", compiled)(name => {
  if (name === "@toposync/plugin-api") return { resolveToposyncUrl: url => url };
  if (name === "../../util/i18n") return { i18n: { t: key => key } };
  return require(name);
}, rendererModule, rendererModule.exports);
const { builtinNotificationRenderers, renderNotificationSpatialStatus } = rendererModule.exports;
const renderer = builtinNotificationRenderers.find(item => item.type === "pipelines.tracking");
const actions = { openImage() {} };
const notification = payload => ({ id: "fixture", type: "pipelines.tracking", title: "Fixture", payload });
const point = (x, extra = {}) => ({ x, z: 1, composition_id: "yard", ...extra });

test("missing mapping has an explanation and creates no pin in either renderer", () => {
  const record = notification({ trail: [], data: { mapping: { status: "unmapped", reason: "camera_configuration_changed" } } });
  assert.equal(renderer.create2DOverlay({ compositionId: "yard" }, record, actions), null);
  assert.equal(renderer.create3DOverlay({ compositionId: "yard" }, record, actions), null);
  const html = renderToStaticMarkup(renderNotificationSpatialStatus(record));
  assert.match(html, /spatial.unavailable/);
  assert.match(html, /reason.configuration_changed/);
  assert.match(html, /role="status"/);
  assert.match(renderToStaticMarkup(renderNotificationSpatialStatus(notification({ trail: [] }))), /reason.unknown/);
});

test("recovery preserves the pin but does not draw through a mapping gap in 2D or 3D", () => {
  const previous = global.document;
  global.document = { createElement: () => ({ getContext: () => null }) };
  try {
    const record = notification({ spatial_status: { status: "ready", had_mapping_gaps: true },
      trail: [point(0), point(1), point(10, { gap_before: true }), point(11)] });
    const overlay2D = renderer.create2DOverlay({ compositionId: "yard" }, record, actions);
    assert.equal(overlay2D.pin().x, 11);
    assert.deepEqual(overlay2D.pin().trail.map(item => item.x), [10, 11]);
    const overlay3D = renderer.create3DOverlay({ compositionId: "yard", THREE }, record, actions);
    const geometry = overlay3D.object.children[0].geometry;
    geometry.computeBoundingBox();
    assert.ok(geometry.boundingBox.min.x > 9, "old segment must not be connected to recovery");
    assert.match(renderToStaticMarkup(renderNotificationSpatialStatus(record)), /spatial.partial/);
    const missing = notification({ data: { mapping: { status: "unmapped", reason: "source_point_unavailable" } } });
    overlay3D.update(missing);
    assert.equal(overlay3D.object.visible, false);
    overlay3D.update(record);
    overlay3D.object.children[0].geometry.computeBoundingBox();
    assert.ok(overlay3D.object.children[0].geometry.boundingBox.min.x > 9);
    overlay3D.dispose();
    assert.equal(renderer.create2DOverlay({ compositionId: "other" }, record, actions), null);
  } finally {
    global.document = previous;
  }
});

test("a single point has a pin without an invented trail; generic events do not acquire spatial warnings", () => {
  const record = notification({ trail: [point(2)] });
  assert.deepEqual(renderer.create2DOverlay({ compositionId: "yard" }, record, actions).pin().trail, [{ x: 2, z: 1 }]);
  assert.equal(renderNotificationSpatialStatus(record), null);
  assert.equal(renderNotificationSpatialStatus({ type: "pipelines.event", payload: {} }), null);
});
