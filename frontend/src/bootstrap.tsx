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

function installInteractionGuards(): void {
  const win = window as unknown as { __toposyncInteractionGuards?: boolean };
  if (win.__toposyncInteractionGuards) return;
  win.__toposyncInteractionGuards = true;

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
}

installInteractionGuards();

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("Missing #root");

createRoot(rootEl).render(
  <React.StrictMode>
    <AuthGate />
  </React.StrictMode>,
);
