const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");
const vm = require("node:vm");

const filename = path.join(__dirname, "../src/elements/CameraPanoramaMappingModal.tsx");
const source = ts.createSourceFile(filename, fs.readFileSync(filename, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let initialScreen = null;
function visit(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === "initialScreen") initialScreen = node;
  ts.forEachChild(node, visit);
}
visit(source);

function fitPoints(count) {
  return Array.from({ length: count }, (_, index) => ({ id: String(index),  }));
}

test("ready panoramas open the same mapping screen regardless of control count", () => {
  assert.ok(initialScreen);
  const code = initialScreen.getText(source).replace("function initialScreen", "globalThis.initialScreen = function");
  const context = { globalThis: {}, MINIMUM_FIT_POINTS: 4 };
  vm.runInNewContext(ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context);
  const job = { active: false, panorama_url: "/panorama.jpg", state: "ready", permissions: { can_activate: false }, points: fitPoints(4), solution: { preview: { eligible: false } } };
  assert.equal(context.globalThis.initialScreen(job), "mapping");
  assert.equal(context.globalThis.initialScreen({ ...job, points: fitPoints(3) }), "mapping");
});
