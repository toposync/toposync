import React from "react";
import { createRoot } from "react-dom/client";

import "@fortawesome/fontawesome-free/css/fontawesome.css";
import "@fortawesome/fontawesome-free/css/solid.css";

import { AuthGate } from "./ui/auth/AuthGate";
import "./ui/styles/tokens.base.css";
import "./ui/styles/tokens.theme.topo-day.css";
import "./ui/styles/tokens.theme.topo-night.css";
import "./ui/styles/tokens.user-preferences.css";
import "./ui/styles.css";

type EmbeddedFrameHop = {
  frame: HTMLIFrameElement;
  ownerWindow: Window;
};

function findChildFrame(ownerWindow: Window, childWindow: Window): HTMLIFrameElement | null {
  const frames = ownerWindow.document.getElementsByTagName("iframe");
  for (const frame of Array.from(frames)) {
    try {
      if (frame.contentWindow === childWindow) return frame;
    } catch {
      // Cross-origin frames are not inspectable.
    }
  }
  return null;
}

function getSameOriginFrameChain(): EmbeddedFrameHop[] {
  const chain: EmbeddedFrameHop[] = [];
  let childWindow: Window = window;

  while (childWindow.parent && childWindow.parent !== childWindow) {
    const ownerWindow = childWindow.parent;
    let frame: HTMLIFrameElement | null = null;
    try {
      frame = findChildFrame(ownerWindow, childWindow);
    } catch {
      break;
    }
    if (!frame) break;
    chain.push({ frame, ownerWindow });
    childWindow = ownerWindow;
  }

  return chain;
}

function pointFromAncestorToCurrentFrame(
  event: WheelEvent,
  frameChain: EmbeddedFrameHop[],
  ownerIndex: number,
): { x: number; y: number } | null {
  let x = event.clientX;
  let y = event.clientY;

  for (let index = ownerIndex; index >= 0; index -= 1) {
    const rect = frameChain[index].frame.getBoundingClientRect();
    if (x < rect.left || y < rect.top || x > rect.right || y > rect.bottom) return null;
    x -= rect.left;
    y -= rect.top;
  }

  return { x, y };
}

function dispatchWheelInCurrentFrame(event: WheelEvent, clientX: number, clientY: number): boolean {
  const target = document.elementFromPoint(clientX, clientY);
  if (!target) return false;

  const wheel = new WheelEvent("wheel", {
    bubbles: true,
    cancelable: true,
    composed: true,
    deltaX: event.deltaX,
    deltaY: event.deltaY,
    deltaZ: event.deltaZ,
    deltaMode: event.deltaMode,
    clientX,
    clientY,
    screenX: event.screenX,
    screenY: event.screenY,
    ctrlKey: event.ctrlKey,
    shiftKey: event.shiftKey,
    altKey: event.altKey,
    metaKey: event.metaKey,
    button: event.button,
    buttons: event.buttons,
  });
  return target.dispatchEvent(wheel);
}

function installEmbeddedPinchWheelBridge(): void {
  const frameChain = getSameOriginFrameChain();
  if (frameChain.length === 0) return;

  frameChain.forEach(({ ownerWindow }, ownerIndex) => {
    ownerWindow.addEventListener(
      "wheel",
      (event) => {
        if (!event.ctrlKey) return;
        const point = pointFromAncestorToCurrentFrame(event, frameChain, ownerIndex);
        if (!point) return;

        event.preventDefault();
        event.stopImmediatePropagation();
        dispatchWheelInCurrentFrame(event, point.x, point.y);
      },
      { passive: false, capture: true },
    );
  });
}

function installInteractionGuards(): void {
  const win = window as unknown as { __toposyncInteractionGuards?: boolean };
  if (win.__toposyncInteractionGuards) return;
  win.__toposyncInteractionGuards = true;

  let lastTouchEndAt = 0;
  const nativeDefaultActionSelector = [
    "a[href]",
    "button",
    "input",
    "label",
    "option",
    "select",
    "summary",
    "textarea",
    "[contenteditable='true']",
    "[role='button']",
    "[role='combobox']",
    "[role='menuitem']",
    "[role='option']",
  ].join(",");

  const targetsNativeDefaultAction = (event: Event): boolean => {
    const path = typeof event.composedPath === "function" ? event.composedPath() : [];
    if (path.some((item) => item instanceof Element && item.matches(nativeDefaultActionSelector))) return true;

    const target = event.target;
    if (target instanceof Element) return Boolean(target.closest(nativeDefaultActionSelector));
    if (target instanceof Node && target.parentElement) return Boolean(target.parentElement.closest(nativeDefaultActionSelector));
    return false;
  };

  // Prevent browser zoom (trackpad pinch on macOS => wheel with ctrlKey in Chromium).
  window.addEventListener(
    "wheel",
    (e) => {
      if (e.ctrlKey) e.preventDefault();
    },
    { passive: false, capture: true },
  );

  // Prevent Safari gesture zoom (trackpad pinch => gesture* events).
  const prevent = (e: Event) => e.preventDefault();
  document.addEventListener("gesturestart" as any, prevent, { passive: false, capture: true });
  document.addEventListener("gesturechange" as any, prevent, { passive: false, capture: true });
  document.addEventListener("gestureend" as any, prevent, { passive: false, capture: true });

  // Prevent double-tap page zoom on mobile browsers while preserving ordinary taps.
  document.addEventListener(
    "touchend",
    (e) => {
      if (targetsNativeDefaultAction(e)) {
        lastTouchEndAt = 0;
        return;
      }
      const now = Date.now();
      if (now - lastTouchEndAt <= 300) e.preventDefault();
      lastTouchEndAt = now;
    },
    { passive: false, capture: true },
  );
}

installInteractionGuards();
installEmbeddedPinchWheelBridge();

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("Missing #root");

createRoot(rootEl).render(
  <React.StrictMode>
    <AuthGate />
  </React.StrictMode>,
);
