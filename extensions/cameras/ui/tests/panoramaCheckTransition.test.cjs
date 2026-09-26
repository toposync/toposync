const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");
const vm = require("node:vm");

const filename = path.join(__dirname, "../src/elements/CameraPanoramaMappingModal.tsx");
const source = ts.createSourceFile(filename, fs.readFileSync(filename, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let initialStep = null;
let resumeEffect = null;
let saveCurrentPoint = null;
function visit(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === "initialStep") initialStep = node;
  if (ts.isFunctionDeclaration(node) && node.name?.text === "saveCurrentPoint") saveCurrentPoint = node;
  if (ts.isCallExpression(node) && node.expression.getText(source) === "useEffect" && node.arguments[0]?.getText(source).includes('const role = fitCount < MINIMUM_FIT_POINTS ? "fit" : "check"')) resumeEffect = node;
  ts.forEachChild(node, visit);
}
visit(source);

function fitPoints(count) {
  return Array.from({ length: count }, (_, index) => ({ id: String(index), role: "fit" }));
}

test("six saved pairs resume at an independent check even without a preview", () => {
  assert.ok(initialStep);
  const code = initialStep.getText(source).replace("function initialStep", "globalThis.initialStep = function");
  const context = { globalThis: {}, MINIMUM_FIT_POINTS: 6 };
  vm.runInNewContext(ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context);
  const job = { active: false, panorama_url: "/panorama.jpg", state: "ready", permissions: { can_activate: false }, points: fitPoints(6), solution: { preview: { eligible: false } } };
  assert.equal(context.globalThis.initialStep(job), 2);
  assert.equal(context.globalThis.initialStep({ ...job, points: fitPoints(5) }), 1);
});

test("an unavailable preview cannot keep the sixth point selected", () => {
  assert.ok(resumeEffect && saveCurrentPoint);
  assert.doesNotMatch(resumeEffect.getText(source), /preview\?\.eligible/);
  assert.doesNotMatch(saveCurrentPoint.getText(source), /preview\?\.eligible/);
  const addCheckButton = [...source.text.matchAll(/onClick=\{\(\) => startPoint\("check"\)\}/g)].at(-1);
  assert.ok(addCheckButton, "the independent-check action remains present");
  const nearby = source.text.slice(Math.max(0, addCheckButton.index - 180), addCheckButton.index);
  assert.doesNotMatch(nearby, /preview\?\.eligible/);
});
