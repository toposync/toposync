const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const postcss = require("postcss");

test("modal buttons expose keyboard focus without replacing elevation or theme colors", () => {
  const css = postcss.parse(fs.readFileSync(path.join(__dirname, "../src/ui/styles.css"), "utf8"));
  const declarations = {};
  css.walkRules(".modalPanel button:focus-visible", (rule) => {
    rule.walkDecls((declaration) => { declarations[declaration.prop] = declaration.value; });
  });
  assert.equal(declarations.outline, "2px solid var(--color-accent-blue)");
  assert.equal(declarations["outline-offset"], "2px");
  assert.equal(declarations["box-shadow"], undefined);
});

test("image-viewer portal buttons expose keyboard focus outside modalPanel", () => {
  const css = postcss.parse(fs.readFileSync(path.join(__dirname, "../src/ui/styles.css"), "utf8"));
  const declarations = {};
  css.walkRules(".fullscreenImageViewerBackdrop button:focus-visible", (rule) => {
    rule.walkDecls((declaration) => { declarations[declaration.prop] = declaration.value; });
  });
  assert.equal(declarations.outline, "2px solid var(--color-accent-blue)");
  assert.equal(declarations["outline-offset"], "2px");
  assert.equal(declarations["box-shadow"], undefined);
});
