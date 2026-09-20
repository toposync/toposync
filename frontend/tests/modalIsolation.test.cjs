const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// Real shared coordinator, deterministic DOM protocol adapter. This verifies
// inert ownership and cleanup, not native browser or screen-reader behavior.
function harness() {
  const listeners = new Map(), observers = new Set();
  const document = {
    activeElement: null,
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(callback);
    },
    removeEventListener(type, callback) { listeners.get(type)?.delete(callback); },
    dispatch(type, target) {
      for (const callback of [...listeners.get(type) ?? []]) callback({ target });
    },
  };
  function mutation(record) {
    for (const observer of observers) {
      const subscription = observer.targets.find(([root, options]) =>
        (root === record.target || options.subtree && root.contains(record.target))
        && (record.type === "childList" ? options.childList : options.attributes
          && (!options.attributeFilter || options.attributeFilter.includes(record.attributeName))));
      if (!subscription) continue;
      observer.records.push(record.type === "attributes"
        ? { ...record, oldValue: subscription[1].attributeOldValue ? record.oldValue : null } : record);
    }
  }
  class MutationObserver {
    constructor(callback) { this.callback = callback; this.targets = []; this.records = []; }
    observe(target, options) { this.targets.push([target, options]); observers.add(this); }
    disconnect() { this.targets = []; this.records = []; observers.delete(this); }
    takeRecords() { return this.records.splice(0); }
  }
  class Element {
    constructor(name, tagName = "DIV") {
      Object.assign(this, { name, tagName, nodeType: 1, children: [], parentElement: null,
        ownerDocument: document, attributes: new Map(), disabled: false, tabIndex: tagName === "BUTTON" ? 0 : -1 });
    }
    get parentNode() { return this.parentElement; }
    get childNodes() { return this.children; }
    get isConnected() { return document.documentElement?.contains(this) ?? false; }
    get inert() { return this.hasAttribute("inert"); }
    set inert(value) { if (value) this.setAttribute("inert", ""); else this.removeAttribute("inert"); }
    hasAttribute(name) { return this.attributes.has(name); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    setAttribute(name, value) {
      const oldValue = this.getAttribute(name);
      this.attributes.set(name, String(value));
      mutation({ type: "attributes", target: this, attributeName: name, oldValue });
    }
    removeAttribute(name) {
      if (!this.hasAttribute(name)) return;
      const oldValue = this.getAttribute(name);
      this.attributes.delete(name);
      mutation({ type: "attributes", target: this, attributeName: name, oldValue });
    }
    contains(other) { return this === other || this.children.some((child) => child.contains(other)); }
    appendChild(child) {
      child.remove(); child.parentElement = this; this.children.push(child);
      mutation({ type: "childList", target: this, addedNodes: [child], removedNodes: [] }); return child;
    }
    remove() {
      const parent = this.parentElement;
      if (!parent) return;
      parent.children.splice(parent.children.indexOf(this), 1); this.parentElement = null;
      mutation({ type: "childList", target: parent, addedNodes: [], removedNodes: [this] });
    }
    matches(selector) {
      if (selector === ":disabled") return this.disabled;
      if (selector === "[inert]") return this.inert;
      return false;
    }
    closest(selector) {
      for (let element = this; element; element = element.parentElement) {
        if (selector.includes("[inert]") && element.inert
          || selector.includes("[hidden]") && element.hasAttribute("hidden")
          || selector.includes("aria-hidden") && element.getAttribute("aria-hidden") === "true") return element;
      }
      return null;
    }
    querySelectorAll(selector) {
      const descendants = this.children.flatMap((child) => [child, ...child.querySelectorAll("*")]);
      return selector === "[inert]" ? descendants.filter((child) => child.inert) : descendants;
    }
    getClientRects() { return this.isConnected ? [{}] : []; }
    focus() {
      if (!this.isConnected || this.disabled || this.closest("[inert], [hidden]")) return;
      document.activeElement = this; document.dispatch("focusin", this);
    }
  }
  document.defaultView = { HTMLElement: Element, Element, MutationObserver, getComputedStyle: () => ({ visibility: "visible" }) };
  document.documentElement = new Element("html", "HTML");
  document.body = document.documentElement.appendChild(new Element("body", "BODY"));
  document.querySelectorAll = (selector) => document.documentElement.querySelectorAll(selector);
  const context = { module: { exports: {} }, document, HTMLElement: Element, Element, MutationObserver };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../packages/plugin-api/modalFocus.js"), "utf8"), context);
  const make = (name, parent = document.body, tagName = "DIV") => parent.appendChild(new Element(name, tagName));
  const app = make("app"), opener = make("opener", app, "BUTTON");
  opener.focus();
  function modal(name, parent = document.body) {
    const portal = make(`${name}-portal`, parent), panel = make(`${name}-panel`, portal);
    const button = make(`${name}-close`, panel, "BUTTON");
    return { portal, panel, button };
  }
  return { ...context.module.exports, document, make, modal, app, opener, listeners, observers,
    inert: (element) => Boolean(element.closest("[inert]")),
    flushMutations() {
      // Reconciliation may queue its own writes. Deliver subsequent microtask
      // batches too, and fail explicitly if production keeps rewriting forever.
      for (let batch = 0; batch < 20; batch++) {
        if (![...observers].some((observer) => observer.records.length)) return;
        for (const observer of [...observers]) {
          const records = observer.takeRecords();
          if (records.length) observer.callback(records, observer);
        }
      }
      assert.fail("MutationObserver reconciliation did not settle");
    },
  };
}

test("opening isolates background siblings without making the active modal or its ancestors inert", () => {
  const h = harness(), sidebar = h.make("sidebar"), modal = h.modal("details");
  const close = h.activateModalFocus(modal.panel, () => {});
  try {
    assert.equal(h.inert(h.app), true, "application background must be inert");
    assert.equal(h.inert(sidebar), true, "other body roots must be inert");
    for (let ancestor = modal.panel; ancestor; ancestor = ancestor.parentElement) assert.equal(ancestor.inert, false);
    assert.equal(h.document.activeElement, modal.button);
    h.opener.focus(); assert.equal(h.document.activeElement, modal.button);
  } finally { close(); }
  assert.equal(h.inert(h.app), false); assert.equal(h.inert(sidebar), false);
  assert.equal(h.document.activeElement, h.opener);
});

test("a nested body portal isolates the lower modal and restores it before restoring focus", () => {
  const h = harness(), outer = h.modal("outer"), inner = h.modal("inner");
  const closeOuter = h.activateModalFocus(outer.panel, () => {});
  const closeInner = h.activateModalFocus(inner.panel, () => {});
  try {
    assert.equal(h.inert(outer.panel), true);
    assert.equal(h.inert(inner.panel), false);
    assert.equal(h.inert(h.app), true);
    closeInner();
    assert.equal(h.inert(outer.panel), false);
    assert.equal(h.inert(h.app), true);
    assert.equal(h.document.activeElement, outer.button);
  } finally { closeInner(); closeOuter(); }
  assert.equal(h.document.activeElement, h.opener);
  assert.equal(h.document.querySelectorAll("[inert]").length, 0);
});

test("a physically nested modal isolates sibling controls but never an ancestor containing the active panel", () => {
  const h = harness(), outer = h.modal("outer"), inner = h.modal("inner", outer.panel);
  const closeOuter = h.activateModalFocus(outer.panel, () => {});
  const closeInner = h.activateModalFocus(inner.panel, () => {});
  try {
    assert.equal(h.inert(outer.button), true);
    for (let ancestor = inner.panel; ancestor; ancestor = ancestor.parentElement) assert.equal(ancestor.inert, false);
    assert.equal(h.inert(h.app), true);
    closeInner();
    assert.equal(h.inert(outer.button), false);
    assert.equal(h.document.activeElement, outer.button);
  } finally { closeInner(); closeOuter(); }
});

test("closing preserves the exact preexisting inert attribute instead of enabling owner-disabled content", () => {
  const h = harness(), ownerDisabled = h.make("owner-disabled"), modal = h.modal("details");
  ownerDisabled.setAttribute("inert", "owned-by-background");
  const close = h.activateModalFocus(modal.panel, () => {});
  try { assert.equal(h.inert(h.app), true); } finally { close(); }
  assert.equal(ownerDisabled.getAttribute("inert"), "owned-by-background");
  assert.equal(h.app.hasAttribute("inert"), false);
});

test("out-of-order removal keeps the top modal isolated and ultimately cleans every owned inert attribute", () => {
  const h = harness(), outer = h.modal("outer"), inner = h.modal("inner");
  const closeOuter = h.activateModalFocus(outer.panel, () => {});
  const closeInner = h.activateModalFocus(inner.panel, () => {});
  try {
    closeOuter(); outer.portal.remove(); h.flushMutations();
    assert.equal(h.inert(h.app), true);
    assert.equal(h.inert(inner.panel), false);
    assert.equal(h.document.activeElement, inner.button);
  } finally { closeOuter(); closeInner(); }
  assert.equal(outer.portal.inert, false, "detached roots cannot retain coordinator-owned state");
  assert.equal(h.app.inert, false); assert.equal(inner.portal.inert, false);
  assert.equal(h.document.activeElement, h.opener);
  assert.equal([...h.listeners.values()].reduce((count, set) => count + set.size, 0), 0);
  assert.equal(h.observers.size, 0);
});

test("background body children added while a modal is open become inert and are restored on cleanup", () => {
  const h = harness(), modal = h.modal("details");
  const close = h.activateModalFocus(modal.panel, () => {});
  const dynamic = h.make("new-background-root");
  h.flushMutations();
  try {
    assert.equal(h.inert(dynamic), true, "dynamic body roots must not bypass isolation");
    assert.equal(h.inert(modal.panel), false);
  } finally { close(); }
  assert.equal(dynamic.hasAttribute("inert"), false);
  assert.equal(h.observers.size, 0);
});

for (const previous of [null, "owned-before-open"]) {
  test(`external inert removal is temporarily isolated but respected on close (previous=${previous})`, () => {
    const h = harness(), modal = h.modal("details");
    if (previous !== null) h.app.setAttribute("inert", previous);
    const close = h.activateModalFocus(modal.panel, () => {});
    try {
      h.flushMutations();
      h.app.removeAttribute("inert");
      h.flushMutations();
      assert.equal(h.inert(h.app), true, "external removal cannot expose the background while the modal stays open");
      assert.equal(h.inert(modal.panel), false);
    } finally { close(); }
    assert.equal(h.app.hasAttribute("inert"), false, "do not restore a preexisting inert value removed by its owner");
    assert.equal(h.observers.size, 0);
  });
}

test("an external nonempty inert value assigned during isolation survives cleanup exactly", () => {
  const h = harness(), modal = h.modal("details");
  const close = h.activateModalFocus(modal.panel, () => {});
  try {
    h.flushMutations();
    h.app.setAttribute("inert", "new-background-owner");
    h.flushMutations();
    assert.equal(h.inert(h.app), true);
    assert.equal(h.app.getAttribute("inert"), "new-background-owner");
    assert.equal(h.inert(modal.panel), false);
  } finally { close(); }
  assert.equal(h.app.getAttribute("inert"), "new-background-owner");
  assert.equal(h.observers.size, 0);
});

test("cleanup drains an external removal before the observer callback can run", () => {
  const h = harness(), modal = h.modal("details");
  h.app.setAttribute("inert", "owned-before-open");
  const close = h.activateModalFocus(modal.panel, () => {});
  h.app.removeAttribute("inert");
  close();
  assert.equal(h.app.hasAttribute("inert"), false);
  assert.equal(h.observers.size, 0);
});

test("an external owner explicitly setting an empty inert attribute remains disabled after cleanup", () => {
  const h = harness(), modal = h.modal("details");
  const close = h.activateModalFocus(modal.panel, () => {});
  h.flushMutations();
  h.app.setAttribute("inert", "");
  close();
  assert.equal(h.app.getAttribute("inert"), "");
  assert.equal(h.observers.size, 0);
});
