const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const postcss = require("postcss");

const css = postcss.parse(fs.readFileSync(path.resolve(__dirname,
  "../src/ui/screens/pipelines/topology/topology.css"), "utf8"));
const dayRules = [];
css.walkRules((rule) => {
  if (rule.selector.includes('data-toposync-base-theme="topo-day"')) dayRules.push(rule);
});
function dayDeclaration(selector, property) {
  const values = [];
  for (const rule of dayRules) {
    if (rule.selector.includes(selector)) rule.walkDecls(property, (declaration) => values.push(declaration.value));
  }
  return values;
}

test("day editor surfaces use the existing solid theme token", () => {
  for (const selector of [".pipelineTopologyInspector", ".pipelineTopologyNode",
    ".pipelineTopologyActionError", ".pipelineTopologyPalette", ".pipelineTopologyStatusChip"])
    assert.ok(dayDeclaration(selector, "background").includes("var(--color-surface-solid)"), selector);
});

test("light overrides are scoped to both supported day-theme attributes", () => {
  assert.ok(dayRules.length > 0);
  for (const rule of dayRules) {
    assert.ok(rule.selector.startsWith(':root:is([data-toposync-base-theme="topo-day"], [data-toposync-theme="topo-day"])'));
    assert.ok(!rule.selector.includes("topo-night"));
  }
});

test("day status and edge labels do not retain night-only contrast pairs", () => {
  assert.deepEqual(dayDeclaration(".pipelineTopologyStatusChip", "color"), ["var(--text)"]);
  assert.deepEqual(dayDeclaration(".react-flow__edge-textbg", "fill"), ["var(--color-surface-solid)"]);
});

test("canvas preserves grid geometry with day canvas tokens", () => {
  const value = dayRules.find((rule) => rule.selector.endsWith(" .pipelineTopologyCanvas"));
  assert.ok(value);
  const declarations = Object.fromEntries(value.nodes.map((node) => [node.prop, node.value]));
  assert.match(declarations.background, /var\(--color-canvas2d-grid-minor\)/);
  assert.match(declarations.background, /var\(--color-canvas2d-background\)/);
  assert.equal(declarations["background-size"], "48px 48px");
});

test("day palette keeps an explicit keyboard and pointer highlight", () => {
  const rule = dayRules.find((entry) => entry.selector.endsWith(" .pipelineTopologyPaletteList button:is(:hover, :focus-visible)"));
  assert.ok(rule);
  const background = rule.nodes.find((node) => node.prop === "background");
  assert.equal(background.value, "color-mix(in srgb, var(--accent) 12%, var(--color-surface-solid))");
});
