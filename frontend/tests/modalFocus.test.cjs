const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

function source(name) {
  return fs.readFileSync(path.join(__dirname, "../src/ui", name), "utf8");
}
function evaluate(code, globals) {
  const context = { exports: {}, ...globals };
  vm.runInNewContext(ts.transpileModule(code, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React,
    esModuleInterop: true,
  } }).outputText, context);
  return context.exports;
}

// Real focus-scope module, deterministic DOM protocol adapter. This does not
// claim browser layout, native Tab navigation or screen-reader qualification.
function harness() {
  const listeners = new Map();
  const document = {
    body: { name: "document body" },
    activeElement: null,
    addEventListener(type, fn) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(fn); },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
    dispatch(type, fields = {}) {
      const event = { defaultPrevented: false, stopped: false, ...fields,
        preventDefault() { this.defaultPrevented = true; }, stopImmediatePropagation() { this.stopped = true; } };
      for (const fn of [...listeners.get(type) ?? []]) { fn(event); if (event.stopped) break; }
      return event;
    },
  };
  class Element {
    constructor(name, children = [], options = {}) {
      Object.assign(this, { name, children, tagName: "BUTTON", tabIndex: 0, isConnected: true, visible: true,
        disabled: false, blocked: false, visibility: "visible", ownerDocument: document }, options);
      children.forEach((child) => { child.parent = this; });
    }
    querySelectorAll() { return this.children.flatMap((child) => [child, ...child.querySelectorAll()]); }
    contains(other) { return other === this || this.children.some((child) => child.contains(other)); }
    matches() { return this.disabled; }
    closest() { return this.blocked ? this : this.parent?.closest() ?? null; }
    getClientRects() { return this.visible ? [{}] : []; }
    focus(options) {
      this.lastFocusOptions = options;
      if (!this.isConnected || !this.visible || this.disabled) return;
      document.activeElement = this;
      document.dispatch("focusin", { target: this });
    }
  }
  const globals = { Element, HTMLElement: Element, document, getComputedStyle: (element) => ({ visibility: element.visibility }) };
  document.defaultView = globals;
  const sharedContext = { module: { exports: {} } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../packages/plugin-api/modalFocus.js"), "utf8"), sharedContext);
  const focusModule = evaluate(source("modalFocus.ts"), { ...globals, require(name) {
    assert.equal(name, "@toposync/plugin-api");
    return sharedContext.module.exports;
  } });
  const { activateModalFocus: activate } = focusModule;
  const make = (name, children = [], options = {}) => new Element(name, children, options);
  const opener = make("opener"); opener.focus();
  return { activate, focusModule, make, opener, document, globals, listeners,
    key: (key, shiftKey = false, fields = {}) => document.dispatch("keydown", { key, shiftKey, ...fields }) };
}

test("opening moves focus inside and Tab/Shift+Tab wrap at the boundaries", () => {
  const h = harness(), first = h.make("close"), last = h.make("last"), panel = h.make("panel", [first, last]);
  const close = h.activate(panel, () => {});
  assert.equal(h.document.activeElement, first);
  assert.equal(h.key("Tab", true).defaultPrevented, true);
  assert.equal(h.document.activeElement, last);
  assert.notEqual(last.lastFocusOptions?.preventScroll, true, "keyboard wrapping must reveal an offscreen target");
  assert.equal(h.key("Tab").defaultPrevented, true);
  assert.equal(h.document.activeElement, first);
  assert.equal(h.key("Tab").defaultPrevented, false, "native navigation remains intact within the scope");
  close(); assert.equal(h.document.activeElement, h.opener);
});

test("disabled, hidden, inert and negative-tabindex controls are excluded; empty scope focuses panel", () => {
  const h = harness();
  const hidden = h.make("hidden", [], { visible: false });
  const disabled = h.make("disabled", [], { disabled: true });
  const inert = h.make("inert", [], { blocked: true });
  const negative = h.make("negative", [], { tabIndex: -1 });
  const invisible = h.make("invisible", [], { visibility: "hidden" });
  const panel = h.make("panel", [hidden, disabled, inert, negative, invisible], { tabIndex: -1 });
  const close = h.activate(panel, () => {});
  assert.equal(h.document.activeElement, panel);
  assert.equal(h.key("Tab").defaultPrevented, true);
  assert.equal(h.key("Tab", true).defaultPrevented, true);
  close();
});

test("existing autoFocus is preserved; external focus is redirected and dynamic controls are rescanned", () => {
  const h = harness(), first = h.make("close"), input = h.make("input"), panel = h.make("panel", [first, input]);
  input.focus();
  const close = h.activate(panel, () => {});
  assert.equal(h.document.activeElement, input);
  h.opener.focus(); assert.equal(h.document.activeElement, first);
  const added = h.make("added"); panel.children.push(added);
  added.focus(); h.key("Tab"); assert.equal(h.document.activeElement, first);
  close();
});

test("positive tab order is respected", () => {
  const h = harness(), zero = h.make("zero"), two = h.make("two", [], { tabIndex: 2 }), one = h.make("one", [], { tabIndex: 1 });
  const close = h.activate(h.make("panel", [zero, two, one]), () => {});
  assert.equal(h.document.activeElement, one);
  h.key("Tab", true); assert.equal(h.document.activeElement, zero);
  close();
});

test("only the top body-portal modal owns Escape/Tab and restores the lower modal's opener", () => {
  const h = harness(), first = h.make("first"), nestedOpener = h.make("nested-opener"), second = h.make("second");
  let outerCalls = 0, innerCalls = 0;
  const outer = h.activate(h.make("outer", [first, nestedOpener]), () => { outerCalls++; });
  nestedOpener.focus();
  const inner = h.activate(h.make("inner", [second]), () => { innerCalls++; });
  h.key("Tab"); assert.equal(h.document.activeElement, second);
  h.key("Escape"); assert.equal(innerCalls, 1); assert.equal(outerCalls, 0);
  inner(); assert.equal(h.document.activeElement, nestedOpener);
  h.key("Escape"); assert.equal(outerCalls, 1);
  outer(); assert.equal(h.document.activeElement, h.opener);
  assert.equal([...h.listeners.values()].reduce((n, set) => n + set.size, 0), 0);
});

test("removing a lower modal neither steals focus nor leaves a stale return target", () => {
  const h = harness(), first = h.make("first"), second = h.make("second");
  const outer = h.activate(h.make("outer", [first]), () => {});
  const inner = h.activate(h.make("inner", [second]), () => {});
  outer(); assert.equal(h.document.activeElement, second);
  first.isConnected = false;
  inner(); assert.equal(h.document.activeElement, h.opener);
});

test("removed opener is not focused, and prevented widget Escape does not close the modal", () => {
  const h = harness(); let calls = 0;
  const close = h.activate(h.make("panel", [h.make("button")]), () => { calls++; });
  h.key("Escape", false, { defaultPrevented: true }); assert.equal(calls, 0);
  h.opener.isConnected = false;
  close(); assert.notEqual(h.document.activeElement, h.opener);
});

test("effect cleanup and setup replay restores the real opener without leaking listeners", () => {
  const h = harness(), first = h.make("first"), panel = h.make("panel", [first]);
  h.activate(panel, () => {})();
  const close = h.activate(panel, () => {});
  assert.equal(h.document.activeElement, first);
  close(); close();
  assert.equal(h.document.activeElement, h.opener);
  assert.equal([...h.listeners.values()].reduce((n, set) => n + set.size, 0), 0);
});

test("camera submodal stays above its host and contains its pointer events", () => {
  const React = {
    createElement: (type, props, ...children) => ({ type, props: { ...props, children } }),
    useRef: (value) => ({ current: value }),
    useLayoutEffect() {},
  };
  const { SubModal } = evaluate(fs.readFileSync(
    path.join(__dirname, "../../extensions/cameras/ui/src/ui/SubModal.tsx"), "utf8"), {
    document: { body: {} },
    require(name) {
      if (name === "react") return React;
      if (name === "react-dom") return { createPortal: (content, target) => ({ content, target }) };
      if (name === "@toposync/plugin-api") return { activateModalFocus: () => {} };
      throw new Error(name);
    },
  });
  let closeCalls = 0;
  const portal = SubModal({ open: true, title: "Calibration", onClose: () => { closeCalls++; }, children: null });
  const backdrop = portal.content;
  assert.equal(backdrop.props.style.zIndex, 101);

  for (const name of ["onPointerDown", "onPointerMove", "onPointerUp", "onPointerCancel", "onClick", "onDoubleClick", "onWheel"]) {
    let stopped = false;
    backdrop.props[name]({ stopPropagation() { stopped = true; } });
    assert.equal(stopped, true, `${name} must not reach the host modal`);
  }
  const panelEvent = { target: {}, currentTarget: {}, stopped: false, stopPropagation() { this.stopped = true; } };
  backdrop.props.onMouseDown(panelEvent);
  assert.equal(panelEvent.stopped, true);
  assert.equal(closeCalls, 0, "an interaction within the submodal must not close its host");

  const background = {};
  backdrop.props.onMouseDown({ target: background, currentTarget: background, stopPropagation() {} });
  assert.equal(closeCalls, 1, "the submodal backdrop itself remains dismissible");
});

for (const componentName of ["Modal", "SubModal"]) test(`actual ${componentName} shares focus with host portals across volatile onClose callbacks`, () => {
  const h = harness(), refs = [], effects = [], pending = [];
  let cursor = 0;
  const React = {
    createElement: (type, props, ...children) => ({ type, props: { ...props, children } }),
    useRef(value) { const index = cursor++; return refs[index] ??= { current: value }; },
    useLayoutEffect(fn, dependencies) {
      const index = cursor++, previous = effects[index];
      if (!dependencies || !previous || dependencies.some((value, i) => value !== previous.dependencies[i])) {
        pending.push(() => { previous?.cleanup?.(); effects[index] = { dependencies, cleanup: fn() }; });
      }
    },
  };
  const componentSource = componentName === "Modal" ? source("Modal.tsx") : fs.readFileSync(
    path.join(__dirname, "../../extensions/cameras/ui/src/ui/SubModal.tsx"), "utf8");
  const component = evaluate(componentSource, { ...h.globals, require(name) {
    if (name === "react") return React;
    if (name === "react-dom") return { createPortal: (content, target) => ({ content, target }) };
    if (name === "./modalFocus") return { activateModalFocus: h.activate };
    if (name === "@toposync/plugin-api") return h.focusModule;
    if (name === "../util/i18n") return { i18n: { useI18n: () => ({ t: (value) => value }) } };
    if (name === "./Icon") return { Icon: () => null };
    throw new Error(name);
  } })[componentName];
  const outerButton = h.make("host-opener"), outerPanel = h.make("host-panel", [outerButton]);
  let outerCalls = 0;
  const outerCleanup = h.activate(outerPanel, () => { outerCalls++; });
  const first = h.make("close"), input = h.make("input"), panel = h.make("panel", [first, input], { tabIndex: -1 });
  const render = (open, onClose) => {
    cursor = 0;
    const result = component({ open, title: "Details", children: null, onClose });
    if (result) {
      assert.equal(result.target, h.document.body);
      const panelNode = result.content.props.children[0];
      assert.equal(panelNode.props.tabIndex, -1);
      assert.equal(panelNode.props.role, "dialog");
      panelNode.props.ref.current = panel;
    }
    while (pending.length) pending.shift()();
    return result;
  };
  let oldCalls = 0, newCalls = 0;
  render(true, () => { oldCalls++; });
  assert.equal(h.document.activeElement, first);
  input.focus();
  assert.equal(h.document.activeElement, input, "parent must not steal focus from child portal");
  render(true, () => { newCalls++; });
  assert.equal(h.document.activeElement, input, "rerender must not restore or reset focus");
  h.key("Escape"); assert.equal(oldCalls, 0); assert.equal(newCalls, 1);
  assert.equal(outerCalls, 0, "Escape only reaches the top dialog");
  h.key("Tab"); assert.equal(h.document.activeElement, first);
  h.key("Tab", true); assert.equal(h.document.activeElement, input);
  render(false, () => {}); assert.equal(h.document.activeElement, outerButton);
  render(true, () => {}); assert.equal(h.document.activeElement, first);
  effects.forEach((effect) => effect?.cleanup?.());
  assert.equal(h.document.activeElement, outerButton);
  outerCleanup();
  assert.equal(h.document.activeElement, h.opener);
});

test("actual image-viewer portal traps above a modal, restores focus and preserves arrow/fullscreen behavior", () => {
  const h = harness(), refs = [], effects = [], pending = [], windowHandlers = new Set();
  let cursor = 0, closeCalls = 0, exitCalls = 0, selected = 0;
  const React = {
    createElement: (type, props, ...children) => ({ type, props: { ...props, children } }),
    useCallback: (fn) => fn,
    useRef(value) { const index = cursor++; return refs[index] ??= { current: value }; },
    useLayoutEffect(fn, dependencies) {
      const index = cursor++, previous = effects[index];
      if (!dependencies || !previous || dependencies.some((value, i) => value !== previous.dependencies[i])) {
        pending.push(() => { previous?.cleanup?.(); effects[index] = { dependencies, cleanup: fn() }; });
      }
    },
  };
  React.useEffect = React.useLayoutEffect;
  const { FullscreenImageViewer } = evaluate(source("FullscreenImageViewer.tsx"), {
    ...h.globals,
    window: { addEventListener: (_, fn) => windowHandlers.add(fn), removeEventListener: (_, fn) => windowHandlers.delete(fn) },
    require(name) {
      if (name === "react") return React;
      if (name === "react-dom") return { createPortal: (content, target) => ({ content, target }) };
      if (name === "./modalFocus") return h.focusModule;
      if (name === "../util/i18n") return { i18n: { useI18n: () => ({ t: (value) => value }) } };
      if (name === "./Icon") return { Icon: () => null };
      throw new Error(name);
    },
  });
  const modalClose = h.make("modal-close"), imageButton = h.make("open-image");
  const outerClose = h.activate(h.make("modal", [modalClose, imageButton]), () => {});
  imageButton.focus();
  const viewerClose = h.make("viewer-close"), next = h.make("next");
  const viewer = h.make("viewer", [viewerClose, next]);
  const render = (open, items = [{ id: "one", url: "one" }, { id: "two", url: "two" }]) => {
    cursor = 0;
    const result = FullscreenImageViewer({ open, items, index: selected,
      onIndexChange: (index) => { selected = index; }, onClose: () => { closeCalls++; } });
    if (result) {
      assert.equal(result.target, h.document.body);
      assert.equal(result.content.props.tabIndex, -1);
      result.content.props.ref.current = viewer;
    }
    while (pending.length) pending.shift()();
  };
  const arrow = (key) => {
    const event = { key, target: h.document.activeElement, preventDefault() {}, stopPropagation() {} };
    for (const fn of windowHandlers) fn(event);
  };
  render(true); assert.equal(h.document.activeElement, viewerClose);
  h.key("Tab", true); assert.equal(h.document.activeElement, next);
  h.key("Tab"); assert.equal(h.document.activeElement, viewerClose);
  arrow("ArrowRight"); assert.equal(selected, 1);
  next.focus(); render(true); assert.equal(h.document.activeElement, next, "volatile callback preserves focus");
  const upper = h.activate(h.make("upper", [h.make("upper-close")]), () => {});
  arrow("ArrowLeft"); assert.equal(selected, 1, "covered viewer must not react to navigation");
  upper(); assert.equal(h.document.activeElement, next);
  h.document.fullscreenElement = viewer;
  h.document.exitFullscreen = () => { exitCalls++; h.document.fullscreenElement = null; };
  h.key("Escape"); assert.equal(closeCalls, 1); assert.equal(exitCalls, 1);
  render(false); assert.equal(h.document.activeElement, imageButton);
  assert.equal(windowHandlers.size, 0);
  render(true); render(true, []); assert.equal(h.document.activeElement, imageButton, "empty viewer releases its scope");
  effects.forEach((effect) => effect?.cleanup?.());
  outerClose(); assert.equal(h.document.activeElement, h.opener);
});
