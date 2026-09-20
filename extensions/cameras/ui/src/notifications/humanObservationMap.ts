import { resolveToposyncUrl } from "@toposync/plugin-api";
import type { CompositionElement } from "@toposync/plugin-api";
import type { HumanObservation } from "./humanObservation";

// This compares JSON snapshots, not map revisions. The revision is exclusively
// the canonical backend digest emitted with ground estimates or pointing.
function snapshot(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(snapshot).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, item]) => `${JSON.stringify(key)}:${snapshot(item)}`).join(",")}}`;
  if (typeof value === "number" && !Number.isFinite(value)) throw new Error("Nonfinite map");
  const encoded = JSON.stringify(value);
  if (encoded === undefined) throw new Error("Invalid map snapshot");
  return encoded;
}

// A bounded map lease is independent of, and never extends, capture freshness.
// Recheck halfway through it so a healthy response does not blink the overlay.
const MAP_LEASE_MS = 100;
const MAP_REFRESH_MS = 50;

/** Reuse only matching geometry scopes; checks are not atomic with server edits. */
export function createHumanObservationMapGate(
  compositionId: string, elements: CompositionElement[] | undefined,
  read: () => HumanObservation, changed: () => void,
) {
  let disposed = false, verified: string | null = null;
  let generation = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let expiryTimer: ReturnType<typeof setTimeout> | undefined;
  let requestTimer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | undefined;
  let activeKey: string | null = null, verifiedAt = 0, verifiedUntil = 0;
  let expectedElements: string | null = null;
  try { if (elements) expectedElements = snapshot(elements); } catch { /* fail closed */ }
  const key = (model: HumanObservation) => model.state === "current" && model.composition === compositionId
    && model.metricScopeKey && model.mapRevision && expectedElements !== null
    ? JSON.stringify([model.scopeKey, model.metricScopeKey, model.mapRevision]) : null;
  const clearVerification = () => {
    if (expiryTimer !== undefined) clearTimeout(expiryTimer);
    expiryTimer = undefined;
    const wasVerified = verified !== null;
    verified = null;
    if (wasVerified) changed();
  };
  const schedule = (delay: number) => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
    if (!disposed && key(read()) !== null) timer = setTimeout(verify, delay);
  };
  const verify = async () => {
    if (disposed) return;
    timer = undefined;
    const attempt = ++generation;
    const before = read();
    const requestedKey = key(before), started = performance.now();
    if (requestedKey === null) { clearVerification(); return; }
    activeKey = requestedKey;
    const requestController = new AbortController();
    controller = requestController;
    let timedOut = false;
    requestTimer = setTimeout(() => {
      if (disposed || attempt !== generation) return;
      timedOut = true;
      requestController.abort();
      clearVerification();
      schedule(MAP_REFRESH_MS);
    }, MAP_LEASE_MS);
    try {
      const response = await fetch(resolveToposyncUrl(`/api/cameras/compositions/${encodeURIComponent(compositionId)}/observation-revision`),
        { cache: "no-store", credentials: "same-origin", signal: requestController.signal });
      if (!response.ok) throw new Error("Map verification failed");
      const result: unknown = await response.json();
      if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("Invalid map response");
      const current = result as Record<string, unknown>, after = read();
      if (disposed || timedOut || attempt !== generation) return;
      if (key(after) !== requestedKey || performance.now() < started || performance.now() >= started + MAP_LEASE_MS) {
        clearVerification(); return;
      }
      if (current.composition_id === compositionId && current.map_revision === after.mapRevision
        && Array.isArray(current.elements) && snapshot(current.elements) === expectedElements) {
        const changedVerdict = verified !== requestedKey || performance.now() >= verifiedUntil;
        verified = requestedKey; verifiedAt = started; verifiedUntil = started + MAP_LEASE_MS;
        if (expiryTimer !== undefined) clearTimeout(expiryTimer);
        expiryTimer = setTimeout(clearVerification, Math.max(0, verifiedUntil - performance.now()));
        if (changedVerdict) changed();
      } else clearVerification();
    } catch {
      if (!disposed && !timedOut && attempt === generation) clearVerification();
    }
    finally {
      if (!disposed && !timedOut && attempt === generation) {
        if (requestTimer !== undefined) clearTimeout(requestTimer);
        requestTimer = undefined;
        schedule(Math.max(0, started + MAP_REFRESH_MS - performance.now()));
      }
    }
  };
  timer = setTimeout(verify, 0);
  return {
    allows: (model: HumanObservation) => !disposed && verified !== null && verified === key(model)
      && performance.now() >= verifiedAt && performance.now() < verifiedUntil,
    invalidate: () => {
      if (disposed || (activeKey !== null && key(read()) === activeKey)) return;
      generation++; activeKey = null; controller?.abort();
      if (requestTimer !== undefined) clearTimeout(requestTimer);
      clearVerification(); schedule(0);
    },
    dispose: () => {
      disposed = true; generation++; verified = null; controller?.abort();
      for (const pending of [timer, expiryTimer, requestTimer]) if (pending !== undefined) clearTimeout(pending);
    },
  };
}
