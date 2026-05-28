import { useSyncExternalStore } from "react";

import { getToposyncBasePath, resolveToposyncUrl } from "@toposync/plugin-api";

type Listener = () => void;

const listeners = new Set<Listener>();
let popstateAttached = false;
let currentPathname = snapshotPathname();
let previousPathname: string | null = null;

function emit(options?: { updatePrevious?: boolean }): void {
  const next = snapshotPathname();
  if (next !== currentPathname) {
    if (options?.updatePrevious !== false) previousPathname = currentPathname;
    currentPathname = next;
  }
  for (const listener of listeners) listener();
}

function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  if (!popstateAttached && typeof window !== "undefined") {
    popstateAttached = true;
    window.addEventListener("popstate", () => emit({ updatePrevious: true }));
  }
  return () => {
    listeners.delete(listener);
  };
}

function snapshotPathname(): string {
  if (typeof window === "undefined") return "/";
  const pathname = window.location.pathname || "/";
  const basePath = getToposyncBasePath();
  if (basePath === "/") return pathname;
  if (pathname === basePath) return "/";
  if (pathname.startsWith(`${basePath}/`)) {
    return pathname.slice(basePath.length) || "/";
  }
  return pathname;
}

export function usePathname(): string {
  return useSyncExternalStore(subscribe, snapshotPathname, () => "/");
}

export function getPreviousPathname(): string | null {
  return previousPathname;
}

export function navigate(pathname: string): void {
  const next = String(pathname || "/");
  if (typeof window === "undefined") return;
  if (snapshotPathname() === next) return;
  window.history.pushState(null, "", resolveToposyncUrl(next));
  emit({ updatePrevious: true });
}

export function replace(pathname: string): void {
  const next = String(pathname || "/");
  if (typeof window === "undefined") return;
  if (snapshotPathname() === next) return;
  window.history.replaceState(null, "", resolveToposyncUrl(next));
  emit({ updatePrevious: false });
}
