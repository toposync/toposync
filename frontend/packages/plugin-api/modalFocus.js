"use strict";

// This module is shared by the host and federated extensions through the
// plugin-api singleton. Separate stacks would make body portals fight for focus.
const scopes = [];
const isolationByDocument = new Map();

function syncIsolation(document, mutations = []) {
  const state = isolationByDocument.get(document);
  if (!state) return;
  // Drain pending changes even when cleanup happens before the observer's turn.
  // Our only managed write adds an absent attribute (oldValue=null). Changes
  // to an existing attribute belong to another owner and become its new state.
  for (const mutation of [...mutations, ...(state.observer?.takeRecords() ?? [])]) {
    if (mutation.type !== "attributes" || !state.managed.has(mutation.target)) continue;
    const current = mutation.target.getAttribute("inert");
    if (current === null || mutation.oldValue !== null) state.managed.set(mutation.target, current);
  }
  const top = scopes.filter((scope) => scope.panel.ownerDocument === document).at(-1);
  const outside = new Set();
  // Walk the active panel's ancestor path. Isolating an ancestor would also
  // disable the dialog itself, including physically nested extension dialogs.
  for (let branch = top?.panel; branch && branch !== document.body; branch = branch.parentElement) {
    const parent = branch.parentElement;
    if (!parent) break;
    for (const sibling of parent.children) if (sibling !== branch) outside.add(sibling);
  }
  for (const [element, previous] of state.managed) {
    if (outside.has(element)) continue;
    // Do not overwrite a different value assigned by another owner meanwhile.
    if (element.getAttribute("inert") === "") {
      if (previous === null) element.removeAttribute("inert");
      else element.setAttribute("inert", previous);
    }
    state.managed.delete(element);
  }
  for (const element of outside) {
    if (!state.managed.has(element)) state.managed.set(element, element.getAttribute("inert"));
    // Preserve an already-inert owner's attribute exactly.
    if (!element.hasAttribute("inert")) element.setAttribute("inert", "");
  }
  if (!top) {
    state.observer?.disconnect();
    isolationByDocument.delete(document);
  }
}

function isolateBackground(document) {
  if (!isolationByDocument.has(document)) {
    const Observer = document.defaultView.MutationObserver;
    const observer = Observer ? new Observer((mutations) => syncIsolation(document, mutations)) : null;
    isolationByDocument.set(document, { managed: new Map(), observer });
    // Portals and other background roots can be mounted while a modal is open.
    // Reconcile only absent attributes, so our own writes settle after one turn.
    observer?.observe(document.body, { childList: true, subtree: true,
      attributes: true, attributeFilter: ["inert"], attributeOldValue: true });
  }
  syncIsolation(document);
}

function isActiveModalFocus(panel) {
  return panel !== null && scopes[scopes.length - 1]?.panel === panel;
}

function available(element) {
  return element.isConnected && !element.matches(":disabled")
    && !element.closest("[inert], [hidden], [aria-hidden='true']")
    && element.getClientRects().length > 0
    && element.ownerDocument.defaultView.getComputedStyle(element).visibility !== "hidden";
}

function tabbable(panel) {
  return Array.from(panel.querySelectorAll(
    "button, input, select, textarea, a[href], area[href], summary, iframe, [tabindex], [contenteditable='true']",
  )).filter((element) => element.tabIndex >= 0 && available(element))
    .sort((left, right) => (left.tabIndex || Infinity) - (right.tabIndex || Infinity));
}

function focusInside(panel) {
  (tabbable(panel)[0] ?? panel).focus({ preventScroll: true });
}

/** One keyboard/focus owner for all host and extension body portals.
 * Register for the open lifetime, not for each onClose callback identity.
 */
function activateModalFocus(panel, onClose) {
  const document = panel.ownerDocument;
  const scope = {
    panel,
    returnFocus: document.activeElement instanceof document.defaultView.HTMLElement ? document.activeElement : null,
  };
  scopes.push(scope);
  isolateBackground(document);
  const isTop = () => scopes[scopes.length - 1] === scope;

  function onKeyDown(event) {
    if (!isTop() || event.defaultPrevented) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopImmediatePropagation();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const items = tabbable(panel);
    const active = document.activeElement;
    const first = items[0], last = items[items.length - 1];
    if (!first || !panel.contains(active) || active === panel
      || (event.shiftKey ? active === first : active === last)) {
      event.preventDefault();
      // Keyboard navigation must reveal the target in a scrollable dialog.
      (event.shiftKey ? last ?? panel : first ?? panel).focus();
    }
  }

  function onFocusIn(event) {
    if (isTop() && !panel.contains(event.target)) focusInside(panel);
  }

  document.addEventListener("keydown", onKeyDown);
  document.addEventListener("focusin", onFocusIn);
  // React autoFocus may already have selected a meaningful input.
  if (!panel.contains(document.activeElement)) focusInside(panel);
  return () => {
    const wasTop = isTop();
    document.removeEventListener("keydown", onKeyDown);
    document.removeEventListener("focusin", onFocusIn);
    const index = scopes.indexOf(scope);
    if (index < 0) return;
    scopes.splice(index, 1);
    syncIsolation(document);
    for (const other of scopes) {
      if (other.returnFocus && panel.contains(other.returnFocus)) other.returnFocus = scope.returnFocus;
    }
    if (!wasTop) return;
    const remaining = scopes[scopes.length - 1];
    const target = scope.returnFocus;
    if (target && available(target) && (!remaining || remaining.panel.contains(target))) {
      target.focus({ preventScroll: true });
    } else if (remaining) {
      focusInside(remaining.panel);
    }
  };
}

module.exports = { activateModalFocus, isActiveModalFocus };
