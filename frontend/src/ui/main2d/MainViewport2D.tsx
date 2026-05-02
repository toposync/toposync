import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { CompositionElement, ElementType } from "@toposync/plugin-api";

import { i18n } from "../../util/i18n";
import { Icon } from "../Icon";
import { Modal } from "../Modal";
import { getOrCreateMain2DRenderManifest } from "./render2dCache";
import type { Main2DRenderManifest } from "./render2dCache";

type Props = {
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  compositionId: string;
  onElementActivated?: (elementId: string, intent?: "click" | "dblclick" | "longpress") => void;
};

type ViewTransform = { scale: number; x: number; y: number };

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

const HOME_ASSISTANT_ELEMENT_TYPE_ID = "com.toposync.home_assistant.item";
const HOME_ASSISTANT_BUTTON_SIZE_PX = 44;
const HOME_ASSISTANT_CLUSTER_THRESHOLD_PX = HOME_ASSISTANT_BUTTON_SIZE_PX * 0.92;
const HOME_ASSISTANT_REST_POLL_INTERVAL_MS = 1000;

type HomeAssistantLiveState = { entity_id?: string; state?: string; attributes?: Record<string, unknown> };

function asRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>;
  return {};
}

function stableStringify(value: unknown): string {
  const seen = new Set<unknown>();
  function inner(v: unknown): any {
    if (v === null) return null;
    const t = typeof v;
    if (t === "string" || t === "number" || t === "boolean") return v;
    if (t !== "object") return null;
    if (seen.has(v)) return null;
    seen.add(v);

    if (Array.isArray(v)) return v.map(inner);

    const rec = v as Record<string, unknown>;
    const keys = Object.keys(rec).sort((a, b) => a.localeCompare(b));
    const out: Record<string, unknown> = {};
    for (const key of keys) out[key] = inner(rec[key]);
    return out;
  }
  return JSON.stringify(inner(value));
}

function readString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function readNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function readSpecialView(value: unknown): "none" | "lamp" | "airflow" {
  const v = readString(value).trim().toLowerCase();
  return v === "lamp" || v === "airflow" ? v : "none";
}

function readPrimaryEntityId(props: Record<string, unknown>): string {
  const configured = readString(props.primary_entity_id).trim();
  if (configured) return configured;
  const items = Array.isArray(props.items) ? props.items : [];
  if (items.length !== 1) return "";
  const item = asRecord(items[0]);
  return readString(item.kind) === "entity" ? readString(item.id).trim() : "";
}

function domainFromEntityId(entityId: string): string {
  const idx = entityId.indexOf(".");
  if (idx <= 0) return "";
  return entityId.slice(0, idx);
}

function boolStateForDomain(domain: string, rawState: string): boolean | null {
  const d = domain.toLowerCase();
  const s = rawState.trim().toLowerCase();
  if (!s || s === "unknown" || s === "unavailable") return null;

  if (d === "lock") return s === "locked";
  if (d === "cover") return s === "closed" || s === "closing";
  if (d === "climate") return s !== "off";

  return s === "on";
}

type HomeAssistantButtonStage = {
  id: string;
  title: string;
  subtitle: string;
  icon: string;
  stageX: number;
  stageY: number;
  boolState: boolean | null;
};

type HomeAssistantButtonEntry =
  | (HomeAssistantButtonStage & {
      kind: "single";
      screenX: number;
      screenY: number;
    })
  | {
      kind: "cluster";
      id: string;
      buttons: Array<HomeAssistantButtonStage & { screenX: number; screenY: number }>;
      screenX: number;
      screenY: number;
      title: string;
    };

type AirflowMode = "off" | "neutral" | "cool" | "heat";

function liveStateSignature(state: HomeAssistantLiveState | null | undefined): string {
  if (!state || typeof state !== "object") return "";
  return JSON.stringify({
    state: typeof state.state === "string" ? state.state : "",
    attributes: state.attributes && typeof state.attributes === "object" ? state.attributes : null,
  });
}

function climateFlowFromLiveState(
  live: HomeAssistantLiveState | null,
  fallbackStateRaw: string,
): { active: boolean; mode: AirflowMode; factor: number } {
  const state = readString(live?.state ?? fallbackStateRaw).trim().toLowerCase();
  const action = readString(live?.attributes?.hvac_action).trim().toLowerCase();

  if (!state || state === "unknown" || state === "unavailable") return { active: false, mode: "off", factor: 0 };
  if (state === "off" || action === "off") return { active: false, mode: "off", factor: 0 };

  if (action === "idle") {
    const inferredMode: AirflowMode =
      state.includes("heat") ? "heat" : state.includes("cool") || state === "dry" ? "cool" : "neutral";
    return { active: true, mode: inferredMode, factor: 0.22 };
  }

  if (action.includes("heat")) return { active: true, mode: "heat", factor: 1.0 };
  if (action.includes("cool") || action.includes("dry")) return { active: true, mode: "cool", factor: 1.0 };
  if (action.includes("fan")) return { active: true, mode: "neutral", factor: 0.75 };

  if (state.includes("heat")) return { active: true, mode: "heat", factor: 0.85 };
  if (state.includes("cool") || state === "dry") return { active: true, mode: "cool", factor: 0.85 };
  if (state === "fan_only") return { active: true, mode: "neutral", factor: 0.65 };

  return { active: true, mode: "neutral", factor: 0.75 };
}

export function MainViewport2D({ compositionId, elements, elementTypesById, onElementActivated }: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const containerRef = useRef<HTMLDivElement | null>(null);

  const [manifest, setManifest] = useState<Main2DRenderManifest | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [transform, setTransform] = useState<ViewTransform>({ scale: 1, x: 0, y: 0 });
  const fitTransformRef = useRef<ViewTransform>({ scale: 1, x: 0, y: 0 });

  const isPanningRef = useRef(false);
  const lastPointerRef = useRef<{ x: number; y: number } | null>(null);
  const clickTimersRef = useRef<Map<string, number>>(new Map());

  const [clusterModalButtons, setClusterModalButtons] = useState<HomeAssistantButtonStage[] | null>(null);

  const renderableElements = useMemo(
    () => elements.filter((element) => Boolean(elementTypesById[element.type]?.create3D)),
    [elements, elementTypesById],
  );

  const renderableElementsLayoutKey = useMemo(() => {
    const signature = renderableElements
      .map((element) => {
        const props = asRecord(element.props);
        const propsForSignature =
          element.type === HOME_ASSISTANT_ELEMENT_TYPE_ID
            ? (() => {
                const { primary_state: _ignoredPrimaryState, ...rest } = props as Record<string, unknown>;
                return rest;
              })()
            : props;
        return {
          id: element.id,
          type: element.type,
          name: element.name,
          position: element.position,
          rotation: element.rotation,
          props: propsForSignature,
        };
      })
      .sort((a, b) => a.id.localeCompare(b.id));
    return stableStringify(signature);
  }, [renderableElements]);
  const homeAssistantElements = useMemo(
    () =>
      elements.filter((element) => element.type === HOME_ASSISTANT_ELEMENT_TYPE_ID).map((element) => {
        const props = asRecord(element.props);
        const serverId = readString(props.server_id).trim();
        const entityId = readPrimaryEntityId(props);
        const icon = readString(props.icon).trim() || "house";
        const specialView = readSpecialView(props.special_view);
        const fallbackState = readString(props.primary_state).trim().toLowerCase();
        const lampIntensity = readNumber(props.lamp_intensity, 1.0);
        const airflowIntensity = readNumber(props.airflow_intensity, 1.0);
        return {
          id: element.id,
          name: element.name,
          compositionElement: element,
          serverId,
          entityId,
          icon,
          specialView,
          fallbackState,
          lampIntensity,
          airflowIntensity,
        };
      }),
    [elements],
  );

  const [homeAssistantLiveStates, setHomeAssistantLiveStates] = useState<Record<string, HomeAssistantLiveState>>({});
  const fallbackStateByEntityKeyRef = useRef<Record<string, string>>({});

  const homeAssistantWatchTargets = useMemo(() => {
    const byServer = new Map<string, Set<string>>();
    for (const homeAssistantElement of homeAssistantElements) {
      if (!homeAssistantElement.serverId || !homeAssistantElement.entityId) continue;
      const set = byServer.get(homeAssistantElement.serverId) ?? new Set<string>();
      set.add(homeAssistantElement.entityId);
      byServer.set(homeAssistantElement.serverId, set);
    }
    return Array.from(byServer.entries())
      .map(([serverId, entityIds]) => ({ serverId, entityIds: Array.from(entityIds).sort() }))
      .sort((a, b) => a.serverId.localeCompare(b.serverId));
  }, [homeAssistantElements]);

  const homeAssistantWatchKey = useMemo(
    () => homeAssistantWatchTargets.map((t) => `${t.serverId}:${t.entityIds.join(",")}`).join("|"),
    [homeAssistantWatchTargets],
  );

  useEffect(() => {
    const prev = fallbackStateByEntityKeyRef.current;
    const next: Record<string, string> = {};
    const changes: Array<{ key: string; entityId: string; fallbackState: string }> = [];

    for (const homeAssistantElement of homeAssistantElements) {
      if (!homeAssistantElement.serverId || !homeAssistantElement.entityId) continue;
      const fallbackState = homeAssistantElement.fallbackState.trim().toLowerCase();
      if (!fallbackState) continue;

      const key = `${homeAssistantElement.serverId}|${homeAssistantElement.entityId}`;
      next[key] = fallbackState;

      const previousFallback = prev[key];
      if (previousFallback && previousFallback !== fallbackState) {
        changes.push({ key, entityId: homeAssistantElement.entityId, fallbackState });
      }
    }

    fallbackStateByEntityKeyRef.current = next;

    if (changes.length === 0) return;

    setHomeAssistantLiveStates((current) => {
      let changed = false;
      const merged = { ...current };

      for (const entry of changes) {
        const existing = current[entry.key];
        const existingState = readString(existing?.state).trim().toLowerCase();
        if (existingState === entry.fallbackState) continue;
        merged[entry.key] = {
          entity_id: entry.entityId,
          state: entry.fallbackState,
          attributes: existing?.attributes,
        };
        changed = true;
      }

      return changed ? merged : current;
    });
  }, [homeAssistantElements]);

  useEffect(() => {
    const sources: EventSource[] = [];
    const pollTimers: number[] = [];
    let cancelled = false;

    setHomeAssistantLiveStates({});

    const upsertSnapshot = (serverId: string, data: unknown) => {
      if (!data || typeof data !== "object") return;
      setHomeAssistantLiveStates((prev) => {
        let changed = false;
        const next = { ...prev };
        for (const [entityId, state] of Object.entries(data as Record<string, any>)) {
          if (!state || typeof state !== "object") continue;
          const key = `${serverId}|${entityId}`;
          const entry = {
            entity_id: readString((state as any).entity_id) || entityId,
            state: readString((state as any).state),
            attributes:
              (state as any).attributes && typeof (state as any).attributes === "object" ? (state as any).attributes : undefined,
          };
          if (liveStateSignature(prev[key]) === liveStateSignature(entry)) continue;
          next[key] = entry;
          changed = true;
        }
        return changed ? next : prev;
      });
    };

    async function fetchInitialStates(serverId: string, entityIds: string[]) {
      if (entityIds.length === 0) return;
      try {
        const response = await fetch(`/api/home_assistant/${encodeURIComponent(serverId)}/states`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ entity_ids: entityIds }),
        });
        if (!response.ok) return;
        const data = await response.json();
        if (cancelled) return;
        upsertSnapshot(serverId, data);
      } catch {
        // ignore
      }
    }

    for (const target of homeAssistantWatchTargets) {
      void fetchInitialStates(target.serverId, target.entityIds);
      const url = `/api/home_assistant/${encodeURIComponent(target.serverId)}/stream?entity_ids=${encodeURIComponent(target.entityIds.join(","))}`;
      const eventSource = new EventSource(url);
      sources.push(eventSource);

      const handleSnapshot = (event: Event) => {
        if (cancelled) return;
        const msg = event as MessageEvent;
        try {
          const data = JSON.parse(msg.data);
          upsertSnapshot(target.serverId, data);
        } catch {
          // ignore
        }
      };

      const handleStateChanged = (event: Event) => {
        if (cancelled) return;
        const msg = event as MessageEvent;
        try {
          const data = JSON.parse(msg.data);
          const entityId = readString((data as any)?.entity_id).trim();
          const state = (data as any)?.state;
          if (!entityId || !state || typeof state !== "object") return;

          setHomeAssistantLiveStates((prev) => {
            const key = `${target.serverId}|${entityId}`;
            const entry = {
              entity_id: readString((state as any).entity_id) || entityId,
              state: readString((state as any).state),
              attributes: (state as any).attributes && typeof (state as any).attributes === "object" ? (state as any).attributes : undefined,
            };
            if (liveStateSignature(prev[key]) === liveStateSignature(entry)) return prev;
            return { ...prev, [key]: entry };
          });
        } catch {
          // ignore
        }
      };

      eventSource.addEventListener("snapshot", handleSnapshot);
      eventSource.addEventListener("state_changed", handleStateChanged);

      pollTimers.push(window.setInterval(() => {
        void fetchInitialStates(target.serverId, target.entityIds);
      }, HOME_ASSISTANT_REST_POLL_INTERVAL_MS));
    }

    return () => {
      cancelled = true;
      for (const eventSource of sources) {
        try {
          eventSource.close();
        } catch {
          // ignore
        }
      }
      for (const timer of pollTimers) window.clearInterval(timer);
    };
  }, [homeAssistantWatchKey]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    if (renderableElements.length === 0) {
      setManifest(null);
      setLoading(false);
      return () => {
        cancelled = true;
      };
    }

    void getOrCreateMain2DRenderManifest({ compositionId, elements: renderableElements, elementTypesById })
      .then((m) => {
        if (cancelled) return;
        setManifest(m);
        setLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [compositionId, elementTypesById, renderableElementsLayoutKey]);

  const recomputeFit = useCallback(() => {
    const container = containerRef.current;
    if (!container || !manifest) return;

    const w = container.clientWidth;
    const h = container.clientHeight;
    const scale = Math.min(w / manifest.widthPx, h / manifest.heightPx) * 0.96;
    const x = (w - manifest.widthPx * scale) / 2;
    const y = (h - manifest.heightPx * scale) / 2;
    const next = { scale, x, y };
    fitTransformRef.current = next;
    setTransform(next);
  }, [manifest]);

  useEffect(() => {
    recomputeFit();
  }, [recomputeFit]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(() => recomputeFit());
    observer.observe(container);
    return () => observer.disconnect();
  }, [recomputeFit]);

  const handleWheel = useCallback(
    (event: React.WheelEvent) => {
      if (!manifest) return;
      event.preventDefault();

      const container = containerRef.current;
      if (!container) return;

      const rect = container.getBoundingClientRect();
      const cursorX = event.clientX - rect.left;
      const cursorY = event.clientY - rect.top;

      const zoomSpeed = event.ctrlKey ? 0.0042 : 0.0024;
      const zoomFactor = Math.exp(-event.deltaY * zoomSpeed);

      setTransform((prev) => {
        const baseMin = fitTransformRef.current.scale;
        const minScale = Math.max(0.05, baseMin * 0.5);
        const maxScale = Math.max(minScale * 1.5, baseMin * 6);

        const nextScale = clamp(prev.scale * zoomFactor, minScale, maxScale);
        const stageX = (cursorX - prev.x) / prev.scale;
        const stageY = (cursorY - prev.y) / prev.scale;
        const nextX = cursorX - stageX * nextScale;
        const nextY = cursorY - stageY * nextScale;
        return { scale: nextScale, x: nextX, y: nextY };
      });
    },
    [manifest],
  );

  const handlePointerDown = useCallback((event: React.PointerEvent) => {
    if (event.button !== 0) return;
    const container = containerRef.current;
    if (!container) return;
    isPanningRef.current = true;
    lastPointerRef.current = { x: event.clientX, y: event.clientY };
    try {
      container.setPointerCapture(event.pointerId);
    } catch {
      // ignore
    }
  }, []);

  const stopPanning = useCallback((event: React.PointerEvent) => {
    const container = containerRef.current;
    isPanningRef.current = false;
    lastPointerRef.current = null;
    if (container) {
      try {
        container.releasePointerCapture(event.pointerId);
      } catch {
        // ignore
      }
    }
  }, []);

  const handlePointerMove = useCallback((event: React.PointerEvent) => {
    if (!isPanningRef.current) return;
    const last = lastPointerRef.current;
    if (!last) return;
    const dx = event.clientX - last.x;
    const dy = event.clientY - last.y;
    lastPointerRef.current = { x: event.clientX, y: event.clientY };
    setTransform((prev) => ({ ...prev, x: prev.x + dx, y: prev.y + dy }));
  }, []);

  useEffect(() => {
    return () => {
      for (const timer of clickTimersRef.current.values()) window.clearTimeout(timer);
      clickTimersRef.current.clear();
    };
  }, []);

  const triggerClick = useCallback(
    (elementId: string) => {
      if (!onElementActivated) return;
      const prevTimer = clickTimersRef.current.get(elementId);
      if (prevTimer) window.clearTimeout(prevTimer);

      const timer = window.setTimeout(() => {
        clickTimersRef.current.delete(elementId);
        onElementActivated(elementId, "click");
      }, 180);
      clickTimersRef.current.set(elementId, timer);
    },
    [onElementActivated],
  );

  const triggerDoubleClick = useCallback(
    (elementId: string) => {
      if (!onElementActivated) return;
      const prevTimer = clickTimersRef.current.get(elementId);
      if (prevTimer) {
        window.clearTimeout(prevTimer);
        clickTimersRef.current.delete(elementId);
      }
      onElementActivated(elementId, "dblclick");
    },
    [onElementActivated],
  );

  const homeAssistantButtonsStage = useMemo<HomeAssistantButtonStage[]>(() => {
    if (!manifest) return [];
    const bounds = manifest.bounds;
    const spanX = Math.max(1e-6, bounds.maxX - bounds.minX);
    const spanZ = Math.max(1e-6, bounds.maxZ - bounds.minZ);

    const buttons = homeAssistantElements.map((homeAssistantElement) => {
      const stageX = ((homeAssistantElement.compositionElement.position.x - bounds.minX) / spanX) * manifest.widthPx;
      const stageY = ((homeAssistantElement.compositionElement.position.z - bounds.minZ) / spanZ) * manifest.heightPx;
      const liveKey =
        homeAssistantElement.serverId && homeAssistantElement.entityId
          ? `${homeAssistantElement.serverId}|${homeAssistantElement.entityId}`
          : "";
      const live = liveKey ? homeAssistantLiveStates[liveKey] ?? null : null;
      const stateRaw = readString(live?.state ?? homeAssistantElement.fallbackState)
        .trim()
        .toLowerCase();
      const domain = homeAssistantElement.entityId ? domainFromEntityId(homeAssistantElement.entityId) : "";
      const boolState = domain ? boolStateForDomain(domain, stateRaw) : null;

      const title = homeAssistantElement.name || homeAssistantElement.entityId || "Home Assistant";
      const subtitle =
        homeAssistantElement.name && homeAssistantElement.entityId && homeAssistantElement.entityId !== homeAssistantElement.name
          ? homeAssistantElement.entityId
          : "";
      return {
        id: homeAssistantElement.id,
        title,
        subtitle,
        icon: homeAssistantElement.icon,
        stageX,
        stageY,
        boolState,
      };
    });

    buttons.sort((a, b) => a.title.localeCompare(b.title) || a.id.localeCompare(b.id));
    return buttons;
  }, [homeAssistantElements, homeAssistantLiveStates, manifest]);

  const homeAssistantButtonEntries = useMemo<HomeAssistantButtonEntry[]>(() => {
    const buttonsWithScreen = homeAssistantButtonsStage.map((button) => ({
      ...button,
      screenX: transform.x + button.stageX * transform.scale,
      screenY: transform.y + button.stageY * transform.scale,
    }));

    if (buttonsWithScreen.length === 0) return [];

    const parent: number[] = Array.from({ length: buttonsWithScreen.length }, (_, i) => i);

    function find(x: number): number {
      let cur = x;
      while (parent[cur] !== cur) {
        parent[cur] = parent[parent[cur]];
        cur = parent[cur];
      }
      return cur;
    }

    function union(a: number, b: number): void {
      const ra = find(a);
      const rb = find(b);
      if (ra === rb) return;
      parent[rb] = ra;
    }

    for (let i = 0; i < buttonsWithScreen.length; i += 1) {
      for (let j = i + 1; j < buttonsWithScreen.length; j += 1) {
        const dx = Math.abs(buttonsWithScreen[i].screenX - buttonsWithScreen[j].screenX);
        const dy = Math.abs(buttonsWithScreen[i].screenY - buttonsWithScreen[j].screenY);
        if (dx < HOME_ASSISTANT_CLUSTER_THRESHOLD_PX && dy < HOME_ASSISTANT_CLUSTER_THRESHOLD_PX) union(i, j);
      }
    }

    const groups = new Map<number, number[]>();
    for (let i = 0; i < buttonsWithScreen.length; i += 1) {
      const root = find(i);
      const list = groups.get(root) ?? [];
      list.push(i);
      groups.set(root, list);
    }

    const out: HomeAssistantButtonEntry[] = [];
    for (const indices of groups.values()) {
      if (indices.length === 1) {
        const button = buttonsWithScreen[indices[0]];
        out.push({ kind: "single", ...button });
        continue;
      }

      const groupButtons = indices.map((idx) => buttonsWithScreen[idx]);
      groupButtons.sort((a, b) => a.title.localeCompare(b.title) || a.id.localeCompare(b.id));

      const centerX = groupButtons.reduce((sum, b) => sum + b.screenX, 0) / groupButtons.length;
      const centerY = groupButtons.reduce((sum, b) => sum + b.screenY, 0) / groupButtons.length;
      const id = groupButtons.map((b) => b.id).join("|");
      out.push({
        kind: "cluster",
        id,
        buttons: groupButtons,
        screenX: centerX,
        screenY: centerY,
        title: t("core.ui.main2d.cluster.tooltip", { count: groupButtons.length }, `${groupButtons.length} items`),
      });
    }

    out.sort((a, b) => {
      const dy = a.screenY - b.screenY;
      if (Math.abs(dy) > 0.1) return dy;
      return a.screenX - b.screenX;
    });
    return out;
  }, [homeAssistantButtonsStage, t, transform.scale, transform.x, transform.y]);

  const overlayViews = useMemo(() => {
    if (!manifest) return [];

    const out: Array<{ key: string; url: string; opacity: number }> = [];
    for (const overlay of manifest.overlays) {
      const homeAssistantElement = homeAssistantElements.find((e) => e.id === overlay.elementId) ?? null;
      if (!homeAssistantElement) continue;

      const liveKey =
        homeAssistantElement.serverId && homeAssistantElement.entityId
          ? `${homeAssistantElement.serverId}|${homeAssistantElement.entityId}`
          : "";
      const live = liveKey ? homeAssistantLiveStates[liveKey] ?? null : null;
      const fallbackState = homeAssistantElement.fallbackState;
      const stateRaw = readString(live?.state ?? fallbackState)
        .trim()
        .toLowerCase();
      const domain = homeAssistantElement.entityId ? domainFromEntityId(homeAssistantElement.entityId) : "";

      if (overlay.kind === "lamp") {
        const boolState = domain ? boolStateForDomain(domain, stateRaw) : null;
        const intensity = clamp(homeAssistantElement.lampIntensity, 0, 3);
        const opacity = boolState === true ? clamp(0.75 * intensity, 0.2, 1.0) : 0;
        out.push({ key: `${overlay.elementId}:lamp`, url: overlay.url, opacity });
        continue;
      }

      if (overlay.kind === "airflow") {
        const flow = climateFlowFromLiveState(live, fallbackState);
        const intensity = clamp(homeAssistantElement.airflowIntensity, 0, 3);
        const baseOpacity = flow.active ? clamp(flow.factor * 0.7 * intensity, 0.1, 1.0) : 0;
        const matches = flow.mode !== "off" && overlay.mode === flow.mode;
        out.push({ key: `${overlay.elementId}:airflow:${overlay.mode}`, url: overlay.url, opacity: matches ? baseOpacity : 0 });
      }
    }

    return out;
  }, [homeAssistantElements, homeAssistantLiveStates, manifest]);

  return (
    <div
      className="viewportRoot main2dRoot"
      ref={containerRef}
      onWheel={handleWheel}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={stopPanning}
      onPointerCancel={stopPanning}
    >
      {loading ? (
        <div className="main2dCenterHint">
          <div className="card">
            <div className="cardBody">{t("core.ui.loading")}</div>
          </div>
        </div>
      ) : null}

      {error ? (
        <div className="main2dCenterHint">
          <div className="card">
            <div className="cardTitle">{t("core.ui.error", {}, "Error")}</div>
            <div className="cardBody">{error}</div>
          </div>
        </div>
      ) : null}

      {manifest ? (
        <>
          <div
            className="main2dStage"
            style={{
              width: manifest.widthPx,
              height: manifest.heightPx,
              transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
            }}
          >
            <img className="main2dImage main2dBase" src={manifest.base.url} alt="" draggable={false} />
            {overlayViews.map((overlay) => (
              <img
                key={overlay.key}
                className="main2dImage main2dOverlay"
                src={overlay.url}
                alt=""
                draggable={false}
                style={{ opacity: overlay.opacity }}
              />
            ))}
          </div>

          <div className="main2dButtonsOverlay">
            {homeAssistantButtonEntries.map((entry) => {
              if (entry.kind === "cluster") {
                return (
                  <button
                    key={`cluster:${entry.id}`}
                    className="main2dMarkerButton main2dMarkerCluster"
                    type="button"
                    title={entry.title}
                    style={{ left: entry.screenX, top: entry.screenY }}
                    onPointerDown={(e) => e.stopPropagation()}
                    onClick={() => {
                      setClusterModalButtons(
                        entry.buttons.map(({ screenX: _screenX, screenY: _screenY, ...rest }) => rest),
                      );
                    }}
                  >
                    <Icon name="layer-group" />
                    <span className="main2dMarkerClusterCount">{entry.buttons.length}</span>
                  </button>
                );
              }

              return (
                <button
                  key={entry.id}
                  className={[
                    "main2dMarkerButton",
                    entry.boolState === true ? "isOn" : "",
                    entry.boolState === false ? "isOff" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  type="button"
                  title={entry.title}
                  style={{ left: entry.screenX, top: entry.screenY }}
                  onPointerDown={(e) => e.stopPropagation()}
                  onClick={() => triggerClick(entry.id)}
                  onDoubleClick={() => triggerDoubleClick(entry.id)}
                >
                  <Icon name={entry.icon} />
                </button>
              );
            })}
          </div>
        </>
      ) : null}

      <Modal
        open={Boolean(clusterModalButtons)}
        title={t(
          "core.ui.main2d.cluster.title",
          { count: clusterModalButtons?.length ?? 0 },
          `Multiple items (${clusterModalButtons?.length ?? 0})`,
        )}
        onClose={() => setClusterModalButtons(null)}
      >
        <div className="main2dClusterList">
          {(clusterModalButtons ?? []).map((item) => (
            <div key={item.id} className="main2dClusterRow">
              <button
                className={[
                  "main2dClusterPrimary",
                  item.boolState === true ? "isOn" : "",
                  item.boolState === false ? "isOff" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                type="button"
                onClick={() => onElementActivated?.(item.id, "click")}
              >
                <span className="main2dClusterIcon">
                  <Icon name={item.icon} />
                </span>
                <span className="main2dClusterMeta">
                  <span className="main2dClusterTitle">{item.title}</span>
                  {item.subtitle ? <span className="main2dClusterSubtitle">{item.subtitle}</span> : null}
                </span>
              </button>

              <button
                className="iconButton"
                type="button"
                aria-label={t("core.ui.action", {}, "Action")}
                title={t("core.ui.action", {}, "Action")}
                onClick={() => {
                  setClusterModalButtons(null);
                  onElementActivated?.(item.id, "dblclick");
                }}
              >
                <Icon name="ellipsis" />
              </button>
            </div>
          ))}
        </div>
      </Modal>
    </div>
  );
}
