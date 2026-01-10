import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { CompositionElement, ElementType } from "@toposync/plugin-api";

import { i18n } from "../../util/i18n";
import { Icon } from "../Icon";
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

type HomeAssistantLiveState = { entity_id?: string; state?: string; attributes?: Record<string, any> };

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

type AirflowMode = "off" | "neutral" | "cool" | "heat";

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
        const entityId = readString(props.primary_entity_id).trim();
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
    const sources: EventSource[] = [];
    let cancelled = false;

    setHomeAssistantLiveStates({});

    const upsertSnapshot = (serverId: string, data: unknown) => {
      if (!data || typeof data !== "object") return;
      setHomeAssistantLiveStates((prev) => {
        const next = { ...prev };
        for (const [entityId, state] of Object.entries(data as Record<string, any>)) {
          if (!state || typeof state !== "object") continue;
          const key = `${serverId}|${entityId}`;
          next[key] = {
            entity_id: readString((state as any).entity_id) || entityId,
            state: readString((state as any).state),
            attributes:
              (state as any).attributes && typeof (state as any).attributes === "object" ? (state as any).attributes : undefined,
          };
        }
        return next;
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
            return {
              ...prev,
              [key]: {
                entity_id: readString((state as any).entity_id) || entityId,
                state: readString((state as any).state),
                attributes: (state as any).attributes && typeof (state as any).attributes === "object" ? (state as any).attributes : undefined,
              },
            };
          });
        } catch {
          // ignore
        }
      };

      eventSource.addEventListener("snapshot", handleSnapshot);
      eventSource.addEventListener("state_changed", handleStateChanged);
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

      const zoomFactor = Math.exp(-event.deltaY * 0.0012);

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

  const homeAssistantButtons = useMemo(() => {
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

      return {
        id: homeAssistantElement.id,
        title: homeAssistantElement.name || homeAssistantElement.entityId || "Home Assistant",
        icon: homeAssistantElement.icon,
        stageX,
        stageY,
        boolState,
      };
    });

    // Very small clustering: when multiple buttons overlap, spread them in a ring.
    const grouped = new Map<string, typeof buttons>();
    for (const button of buttons) {
      const key = `${Math.round(button.stageX / 28)}|${Math.round(button.stageY / 28)}`;
      const list = grouped.get(key) ?? [];
      list.push(button);
      grouped.set(key, list);
    }

    const out: Array<
      typeof buttons[number] & {
        offsetX: number;
        offsetY: number;
      }
    > = [];
    for (const list of grouped.values()) {
      for (let i = 0; i < list.length; i += 1) {
        const radius = list.length <= 1 ? 0 : Math.min(18, 8 + (list.length - 2) * 2);
        const angle = list.length <= 1 ? 0 : (i / list.length) * Math.PI * 2;
        out.push({
          ...list[i],
          offsetX: Math.cos(angle) * radius,
          offsetY: Math.sin(angle) * radius,
        });
      }
    }
    out.sort((a, b) => a.title.localeCompare(b.title) || a.id.localeCompare(b.id));
    return out;
  }, [homeAssistantElements, homeAssistantLiveStates, manifest]);

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
          <div className="main2dButtons">
            {homeAssistantButtons.map((button) => (
              <button
                key={button.id}
                className={[
                  "main2dHaButton",
                  button.boolState === true ? "isOn" : "",
                  button.boolState === false ? "isOff" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                type="button"
                title={button.title}
                style={{ left: button.stageX + button.offsetX, top: button.stageY + button.offsetY }}
                onPointerDown={(e) => e.stopPropagation()}
                onClick={() => triggerClick(button.id)}
                onDoubleClick={() => triggerDoubleClick(button.id)}
              >
                <Icon name={button.icon} />
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
