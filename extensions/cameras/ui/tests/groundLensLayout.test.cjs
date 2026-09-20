const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { parseFragment } = require("parse5");

function load(relativePath) {
  const source = fs.readFileSync(path.join(__dirname, "../src", relativePath), "utf8");
  const context = { exports: {}, require: (name) => name === "../groundLensEditor"
    ? load("groundLensEditor.ts") : require(name) };
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
    jsx: ts.JsxEmit.React, esModuleInterop: true,
  } }).outputText, context);
  return context.exports;
}
const { CameraGroundLensEditor } = load("elements/CameraGroundLensEditor.tsx");
const attr = (node, name) => node.attrs?.find((entry) => entry.name === name)?.value;
function descendants(node) {
  return (node.childNodes ?? []).flatMap((child) => [child, ...descendants(child)]);
}
function editor(choice, key = choice) {
  const measured = choice !== "identity";
  const count = choice === "fisheye4" ? 4 : Number(choice.slice(5));
  return React.createElement(CameraGroundLensEditor, {
    key, view: { projection_model: {
      source_geometry: { width: 1400, height: 1016 },
      lens: measured ? { type: choice === "fisheye4" ? "fisheye_kb4_v1" : "rectilinear_brown_v1",
        fx: 1.2, fy: 1.1, cx: 0.5, cy: 0.5, coefficients: Array(count).fill(0) }
        : { type: "identity_rectilinear_v1" },
    } }, imageSize: { width: 1400, height: 1016 },
    i18n: { useI18n: () => ({ t: (key) => key }) },
    onDirty: () => assert.fail("render cannot modify the draft"),
    onApply: () => assert.fail("render cannot apply calibration"),
  });
}
function render(...elements) {
  // Real React hooks and SSR markup; no browser layout or interaction claims.
  return descendants(parseFragment(renderToStaticMarkup(React.createElement(React.Fragment, null, ...elements))));
}

for (const choice of ["identity", "brown4", "brown5", "brown8", "fisheye4"]) {
  test(`${choice}: every lens control is grouped with its own associated label`, () => {
    const nodes = render(editor(choice));
    const controls = nodes.filter((node) => ["input", "select"].includes(node.tagName));
    assert.equal(controls.length, choice === "identity" ? 3 : 7 + (choice === "fisheye4" ? 4 : Number(choice.slice(5))));
    for (const control of controls) {
      const id = attr(control, "id");
      assert.ok(id);
      const labels = nodes.filter((node) => node.tagName === "label" && attr(node, "for") === id);
      assert.equal(labels.length, 1, `one label for ${id}`);
      assert.equal(labels[0].parentNode, control.parentNode, `label and control share a field: ${id}`);
      assert.ok((attr(control.parentNode, "class") ?? "").split(/\s+/).includes("field"), `field grouping for ${id}`);
      assert.equal(descendants(control.parentNode).filter((node) => ["input", "select"].includes(node.tagName)).length, 1);
      for (const reference of (attr(control, "aria-describedby") ?? "").split(/\s+/).filter(Boolean)) {
        assert.equal(nodes.filter((node) => attr(node, "id") === reference).length, 1);
      }
      if (control.tagName === "input") assert.equal(attr(control, "aria-invalid"), "false");
    }
  });
}

test("multiple rendered lens editors keep distinct IDs and label targets", () => {
  const nodes = render(editor("brown8", "first"), editor("brown8", "second"), editor("identity", "third"));
  const ids = nodes.map((node) => attr(node, "id")).filter(Boolean);
  assert.equal(new Set(ids).size, ids.length);
  for (const label of nodes.filter((node) => node.tagName === "label")) {
    assert.equal(nodes.filter((node) => attr(node, "id") === attr(label, "for")).length, 1);
  }
});
