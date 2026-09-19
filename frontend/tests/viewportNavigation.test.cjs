const assert = require("node:assert/strict");
const { test, after } = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const typescript = require("typescript");
const directory = fs.mkdtempSync(path.join(os.tmpdir(), "toposync-viewport-"));
const compiled = path.join(directory, "viewportNavigation.cjs");
fs.writeFileSync(compiled, typescript.transpileModule(fs.readFileSync(path.resolve(__dirname, "../src/ui/viewportNavigation.ts"), "utf8"), {
  compilerOptions: { module: typescript.ModuleKind.CommonJS, target: typescript.ScriptTarget.ES2022 },
}).outputText);
const { navigationToContent, navigationToScreen, panNavigation, zoomNavigation, wheelNavigationScale, fitNavigation } = require(compiled);
after(() => fs.rmSync(directory, { recursive: true, force: true }));
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`);
const samePoint = (actual, expected) => { close(actual.x, expected.x); close(actual.y, expected.y); };

for (const rotation of [0, 90, 180, 270]) test(`pan and cursor-anchored zoom preserve geometry at ${rotation} degrees`, () => {
  const view = { center: { x: 3.75, y: -12.5 }, scale: 52 };
  const before = structuredClone(view);
  const size = { width: 913, height: 527 };
  const pointer = { x: 153, y: 320 };
  const content = navigationToContent(pointer, view, size, rotation);
  samePoint(navigationToScreen(content, view, size, rotation), pointer);
  const zoomed = zoomNavigation(view, 130, pointer, size, rotation);
  samePoint(navigationToScreen(content, zoomed, size, rotation), pointer);
  const moved = panNavigation(zoomed, { x: -70, y: 18 }, rotation);
  samePoint(navigationToScreen(content, moved, size, rotation), { x: pointer.x - 70, y: pointer.y + 18 });
  assert.deepEqual(view, before);
});

test("wheel units and zoom limits agree for mouse and trackpad", () => {
  close(wheelNavigationScale(1, 420, 0, 800, 0.1, 16), 0.5);
  close(wheelNavigationScale(1, 3, 1, 800, 0.1, 16), wheelNavigationScale(1, 48, 0, 800, 0.1, 16));
  close(wheelNavigationScale(1, 1, 2, 420, 0.1, 16), 0.5);
  close(wheelNavigationScale(1, -1e9, 0, 800, 0.5, 16), 16);
  close(wheelNavigationScale(1, 1e9, 0, 800, 0.5, 16), 0.5);
});

test("fit respects content bounds and resize preserves the viewed point at the center", () => {
  const bounds = { x: 100, y: 400, width: 800, height: 200 };
  const size = { width: 400, height: 300 };
  const fitted = fitNavigation(bounds, size);
  close(fitted.scale, 0.5);
  samePoint(fitted.center, { x: 500, y: 500 });
  samePoint(navigationToScreen(fitted.center, fitted, { width: 600, height: 100 }), { x: 300, y: 50 });
});
