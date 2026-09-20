const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const postcss = require("postcss");

const css = postcss.parse(fs.readFileSync(path.resolve(__dirname,
  "../src/ui/screens/pipelines/topology/topology.css"), "utf8"));

// Permanent CSS contracts for the rendered 390px inspector overflow regression.
// Bounds and actual line wrapping still require browser verification.
function baseDeclarations(selector) {
  const values = {};
  css.walkRules((rule) => {
    if (rule.parent.type !== "root" || rule.selector !== selector) return;
    rule.walkDecls((declaration) => { values[declaration.prop] = declaration.value; });
  });
  return values;
}

test("inspector text flex item can shrink while reserving the delete button width", () => {
  const text = baseDeclarations(".pipelineTopologyInspectorHeader > div");
  assert.equal(text["min-width"], "0");
  assert.equal(text.flex, "1 1 0");
  assert.equal(baseDeclarations(".pipelineTopologyInspectorHeader .pillButton").flex, "0 0 auto");
});

test("long inspector titles wrap instead of clipping in both themes and viewport sizes", () => {
  const title = baseDeclarations(".pipelineTopologyInspectorTitle");
  assert.equal(title["white-space"], "normal");
  assert.equal(title["overflow-wrap"], "anywhere");
  assert.notEqual(title.overflow, "hidden");
  assert.notEqual(title["text-overflow"], "ellipsis");
  assert.equal(title["font-size"], "15px");
  assert.equal(title["font-weight"], "800");
});

test("secondary operator identifiers stay contained without hiding header actions", () => {
  const subtitle = baseDeclarations(".pipelineTopologyInspectorSubtitle");
  assert.equal(subtitle["text-overflow"], "ellipsis");
  assert.equal(subtitle.overflow, "hidden");
  assert.equal(subtitle["white-space"], "nowrap");
  const header = baseDeclarations(".pipelineTopologyInspectorHeader");
  assert.equal(header.display, "flex");
  assert.equal(header["align-items"], "flex-start");
  assert.notEqual(header.overflow, "hidden");
});

test("inspector keyboard buttons retain an explicit focus outline independent of hover shadows", () => {
  const focus = baseDeclarations(".pipelineTopologyInspector button:focus-visible");
  assert.equal(focus.outline, "2px solid var(--color-accent-blue)");
  assert.equal(focus["outline-offset"], "2px");
  assert.equal(focus["box-shadow"], undefined, "keep existing button elevation and hover styling");
});
