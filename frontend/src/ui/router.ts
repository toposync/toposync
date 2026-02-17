import { useSyncExternalStore } from "react";

type Listener = () => void;

const listeners = new Set<Listener>();
let popstateAttached = false;
let currentPathname = typeof window === "undefined" ? "/" : window.location.pathname || "/";
let previousPathname: string | null = null;

function emit(): void {
  const next = snapshotPathname();
  if (next !== currentPathname) {
    previousPathname = currentPathname;
    currentPathname = next;
  }
  for (const listener of listeners) listener();
}

function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  if (!popstateAttached && typeof window !== "undefined") {
    popstateAttached = true;
    window.addEventListener("popstate", emit);
  }
  return () => {
    listeners.delete(listener);
  };
}

function snapshotPathname(): string {
  if (typeof window === "undefined") return "/";
  return window.location.pathname || "/";
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
  if (window.location.pathname === next) return;
  window.history.pushState(null, "", next);
  emit();
}

export function replace(pathname: string): void {
  const next = String(pathname || "/");
  if (typeof window === "undefined") return;
  if (window.location.pathname === next) return;
  window.history.replaceState(null, "", next);
  emit();
}
