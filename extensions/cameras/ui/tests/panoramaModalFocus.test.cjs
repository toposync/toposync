const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

const filename = path.join(__dirname, "../src/elements/CameraPanoramaMappingModal.tsx");
const source = ts.createSourceFile(filename, fs.readFileSync(filename, "utf8"),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const focusEffects = [];
function visit(node) {
  if (ts.isCallExpression(node) && node.expression.getText(source) === "useEffect"
    && node.arguments[0]?.getText(source).includes('document.addEventListener("keydown", onKey, true)')) {
    focusEffects.push(node.arguments[0].getText(source));
  }
  ts.forEachChild(node, visit);
}
visit(source);
assert.equal(focusEffects.length, 1, "exercise the actual capture-phase focus effect, not a copied handler");
const compiled = ts.transpileModule(`globalThis.runFocusEffect = ${focusEffects[0]};`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;

// Execute the component's real effect with a minimal DOM protocol adapter.
// This does not claim native browser tab order or a federated bundle smoke.
function harness({ upper = false, olderHost = false, open = true } = {}) {
  const listeners = new Set();
  let closeCalls = 0;
  const document = {
    activeElement: null,
    addEventListener(type, handler, capture) {
      assert.equal(type, "keydown"); assert.equal(capture, true); listeners.add(handler);
    },
    removeEventListener(type, handler, capture) {
      assert.equal(type, "keydown"); assert.equal(capture, true); listeners.delete(handler);
    },
  };
  function element(name) {
    return { name, focus() { document.activeElement = this; },
      getClientRects: () => [{}], setAttribute() {}, closest: () => null };
  }
  const first = element("close"), last = element("last"), upperInput = element("upper input");
  const opener = element("opener"); document.activeElement = opener;
  const panel = {
    classList: { add() {}, remove() {} },
    querySelector: () => first,
    querySelectorAll: () => [first, last],
    contains: (target) => target === first || target === last,
  };
  const context = { document, open, t: (key) => key,
    bodyRef: { current: { closest: () => panel } },
    closeRef: { current: () => { closeCalls++; } },
    ...(olderHost ? {} : { isActiveModalFocus: (target) => {
      assert.equal(target, panel); return !upper;
    } }),
  };
  vm.runInNewContext(compiled, context);
  const cleanup = context.runFocusEffect();
  if (upper) upperInput.focus();
  return { document, first, last, upperInput, listeners, cleanup,
    closeCalls: () => closeCalls,
    key(key, { shiftKey = false, target = document.activeElement } = {}) {
      const event = { key, shiftKey, target, defaultPrevented: false, stopped: false,
        preventDefault() { this.defaultPrevented = true; },
        stopPropagation() { this.stopped = true; } };
      for (const handler of listeners) handler(event);
      return event;
    },
  };
}

for (const key of ["Escape", "Tab"]) test(`upper portal owns ${key}; panorama capture must not interfere`, () => {
  const h = harness({ upper: true });
  const event = h.key(key);
  assert.equal(h.closeCalls(), 0);
  assert.equal(event.defaultPrevented, false);
  assert.equal(event.stopped, false);
  assert.equal(h.document.activeElement, h.upperInput);
  h.cleanup(); assert.equal(h.listeners.size, 0);
});

for (const olderHost of [false, true]) test(`${olderHost ? "older host" : "active panorama"}: Escape and Tab retain existing behavior`, () => {
  const h = harness({ olderHost });
  const backwards = h.key("Tab", { shiftKey: true });
  assert.equal(backwards.defaultPrevented, true);
  assert.equal(h.document.activeElement, h.last);
  assert.equal(h.key("Tab").defaultPrevented, true);
  assert.equal(h.document.activeElement, h.first);
  const escape = h.key("Escape");
  assert.equal(escape.defaultPrevented, true);
  assert.equal(escape.stopped, true);
  assert.equal(h.closeCalls(), 1);
  h.cleanup(); assert.equal(h.listeners.size, 0);
});

test("viewport Escape remains delegated to the existing local editing handler", () => {
  const h = harness();
  const event = h.key("Escape", { target: { closest: () => ({}) } });
  assert.equal(h.closeCalls(), 0);
  assert.equal(event.defaultPrevented, false);
  assert.equal(event.stopped, false);
  h.cleanup();
});

test("closed panorama does not register the capture handler", () => {
  const h = harness({ open: false });
  assert.equal(h.listeners.size, 0);
  assert.equal(h.cleanup, undefined);
});
