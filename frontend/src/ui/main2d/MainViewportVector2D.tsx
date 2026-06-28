import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type {
  CompositionElement,
  ElementType,
  Main2DEffectTarget,
  Main2DMarker,
  Notification,
  NotificationRenderer,
} from "@toposync/plugin-api";

import { isAbortError } from "../../util/api";
import { i18n } from "../../util/i18n";
import { Notification2DPinView } from "../notifications/Notification2DPinView";
import { Icon } from "../Icon";
import { Modal } from "../Modal";
import {
  clamp,
  clusterMain2DMarkers,
  computeFitTransform,
  computeMain2DVectorViewBox,
  computeMain2DBounds,
  orderElementsForMain2D,
  padBounds,
  projectWorldToStage,
  stableStringify,
  type Main2DMarkerStage,
  type ViewTransform,
} from "./shared";
import { getOrCreateMain2DEffectManifest, type Main2DEffectRenderManifest } from "./vectorEffectCache";

type Props = {
  elements: CompositionElement[];
  elementTypesById: Record<string, ElementType>;
  compositionId: string;
  onElementActivated?: (elementId: string, intent?: "click" | "dblclick" | "longpress") => void;
  activeNotification?: Notification | null;
  activeNotificationRenderer?: NotificationRenderer | null;
};

const MARKER_BUTTON_SIZE_PX = 44;
const MARKER_CLUSTER_THRESHOLD_PX = MARKER_BUTTON_SIZE_PX * 0.92;
const STAGE_PX_PER_METER = 96;

type WindowWithIdleCallback = Window & {
  requestIdleCallback?: (callback: () => void, options?: { timeout?: number }) => number;
  cancelIdleCallback?: (id: number) => void;
};

function markerKey(marker: Main2DMarker, fallbackElementId: string): string {
  return marker.id || marker.elementId || fallbackElementId;
}

function effectTargetCacheKey(targets: Main2DEffectTarget[]): string {
  return stableStringify(
    targets
      .map((target) => ({
        id: target.id,
        element: target.element,
        baseElement: target.baseElement ?? null,
        signature: target.signature ?? null,
        warmupSeconds: target.warmupSeconds ?? null,
        hideNonLightRenderables: Boolean(target.hideNonLightRenderables),
        blendMode: target.blendMode ?? "source-over",
      }))
      .sort((a, b) => a.id.localeCompare(b.id)),
  );
}

export function MainViewportVector2D({
  compositionId,
  elements,
  elementTypesById,
  onElementActivated,
  activeNotification,
  activeNotificationRenderer,
}: Props): React.ReactElement {
  const { t } = i18n.useI18n();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const fitTransformRef = useRef<ViewTransform>({ scale: 1, x: 0, y: 0 });
  const isPanningRef = useRef(false);
  const lastPointerRef = useRef<{ x: number; y: number } | null>(null);
  const clickTimersRef = useRef<Map<string, number>>(new Map());
  const invalidateRafRef = useRef<number | null>(null);
  const transformRef = useRef<ViewTransform>({ scale: 1, x: 0, y: 0 });
  const pendingTransformRef = useRef<ViewTransform | null>(null);
  const transformRafRef = useRef<number | null>(null);

  const [transform, setTransform] = useState<ViewTransform>({ scale: 1, x: 0, y: 0 });
  const [stateVersion, setStateVersion] = useState(0);
  const [effectManifest, setEffectManifest] = useState<Main2DEffectRenderManifest | null>(null);
  const [effectLoading, setEffectLoading] = useState(false);
  const [effectError, setEffectError] = useState<string | null>(null);
  const [clusterModalMarkers, setClusterModalMarkers] = useState<Main2DMarkerStage[] | null>(null);
  const [viewportSize, setViewportSize] = useState({ width: 1, height: 1 });

  const flushTransform = useCallback(() => {
    transformRafRef.current = null;
    const next = pendingTransformRef.current;
    pendingTransformRef.current = null;
    if (!next) return;
    transformRef.current = next;
    setTransform(next);
  }, []);

  const scheduleTransform = useCallback(
    (updater: (prev: ViewTransform) => ViewTransform) => {
      const base = pendingTransformRef.current ?? transformRef.current;
      pendingTransformRef.current = updater(base);
      if (transformRafRef.current == null) transformRafRef.current = window.requestAnimationFrame(flushTransform);
    },
    [flushTransform],
  );

  const setTransformImmediately = useCallback((next: ViewTransform) => {
    if (transformRafRef.current != null) window.cancelAnimationFrame(transformRafRef.current);
    transformRafRef.current = null;
    pendingTransformRef.current = null;
    transformRef.current = next;
    setTransform(next);
  }, []);

  const invalidate = useCallback(() => {
    if (invalidateRafRef.current != null) return;
    invalidateRafRef.current = window.requestAnimationFrame(() => {
      invalidateRafRef.current = null;
      setStateVersion((prev) => prev + 1);
    });
  }, []);

  useEffect(() => {
    return () => {
      if (invalidateRafRef.current != null) window.cancelAnimationFrame(invalidateRafRef.current);
      if (transformRafRef.current != null) window.cancelAnimationFrame(transformRafRef.current);
      invalidateRafRef.current = null;
      transformRafRef.current = null;
    };
  }, []);

  useEffect(() => {
    const unsubscribers: Array<() => void> = [];
    for (const element of elements) {
      const def = elementTypesById[element.type];
      if (!def?.subscribeMain2DState) continue;
      try {
        const unsubscribe = def.subscribeMain2DState({ element, invalidate });
        if (typeof unsubscribe === "function") unsubscribers.push(unsubscribe);
      } catch (err) {
        console.warn(`[vector2d:subscribeMain2DState:${element.type}]`, err);
      }
    }
    const onGlobalInvalidate = () => invalidate();
    window.addEventListener("toposync:invalidate", onGlobalInvalidate);
    return () => {
      window.removeEventListener("toposync:invalidate", onGlobalInvalidate);
      for (const unsubscribe of unsubscribers) {
        try {
          unsubscribe();
        } catch {
          // ignore
        }
      }
    };
  }, [elementTypesById, elements, invalidate]);

  const rawMarkers = useMemo(() => {
    const out: Array<{ elementId: string; marker: Main2DMarker }> = [];
    for (const element of elements) {
      const def = elementTypesById[element.type];
      if (!def?.getMain2DMarker) continue;
      try {
        const marker = def.getMain2DMarker({ element });
        if (marker) out.push({ elementId: element.id, marker });
      } catch (err) {
        console.warn(`[vector2d:getMain2DMarker:${element.type}]`, err);
      }
    }
    return out;
  }, [elementTypesById, elements, stateVersion]);

  const bounds = useMemo(() => {
    const markerPoints = rawMarkers.map(({ marker }) => ({ x: marker.x, z: marker.z }));
    return padBounds(computeMain2DBounds(elements, elementTypesById, markerPoints), 0.08, 0.5);
  }, [elementTypesById, elements, rawMarkers]);

  const spanX = Math.max(1e-6, bounds.maxX - bounds.minX);
  const spanZ = Math.max(1e-6, bounds.maxZ - bounds.minZ);
  const stageWidth = Math.max(320, Math.round(spanX * STAGE_PX_PER_METER));
  const stageHeight = Math.max(320, Math.round(spanZ * STAGE_PX_PER_METER));

  const orderedElements = useMemo(() => orderElementsForMain2D(elements, elementTypesById), [elementTypesById, elements]);

  const recomputeFit = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    const viewportWidth = Math.max(1, container.clientWidth);
    const viewportHeight = Math.max(1, container.clientHeight);
    setViewportSize((prev) =>
      prev.width === viewportWidth && prev.height === viewportHeight ? prev : { width: viewportWidth, height: viewportHeight },
    );
    const next = computeFitTransform(viewportWidth, viewportHeight, stageWidth, stageHeight);
    fitTransformRef.current = next;
    setTransformImmediately(next);
  }, [setTransformImmediately, stageHeight, stageWidth]);

  useLayoutEffect(() => {
    recomputeFit();
  }, [recomputeFit]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(() => recomputeFit());
    observer.observe(container);
    return () => observer.disconnect();
  }, [recomputeFit]);

  const markersStage = useMemo<Main2DMarkerStage[]>(() => {
    const out = rawMarkers.map(({ elementId, marker }, index) => {
      const stage = projectWorldToStage({ x: marker.x, z: marker.z }, bounds, stageWidth, stageHeight);
      const id = `${markerKey(marker, elementId)}:${index}`;
      return {
        ...marker,
        id,
        elementId: marker.elementId || elementId,
        stageX: stage.x,
        stageY: stage.y,
      };
    });
    out.sort((a, b) => a.title.localeCompare(b.title) || a.id.localeCompare(b.id));
    return out;
  }, [bounds, rawMarkers, stageHeight, stageWidth]);

  const markerEntries = useMemo(
    () =>
      clusterMain2DMarkers({
        markers: markersStage,
        transform,
        thresholdPx: MARKER_CLUSTER_THRESHOLD_PX,
        clusterTitle: (count) => t("core.ui.main2d.cluster.tooltip", { count }, `${count} items`),
      }),
    [markersStage, t, transform],
  );

  const effectTargets = useMemo<Main2DEffectTarget[]>(() => {
    const out: Main2DEffectTarget[] = [];
    for (const element of elements) {
      const def = elementTypesById[element.type];
      if (!def?.getMain2DEffectTargets) continue;
      try {
        for (const target of def.getMain2DEffectTargets({ element, elements })) {
          if (!target?.id || !target.element) continue;
          out.push(target);
        }
      } catch (err) {
        console.warn(`[vector2d:getMain2DEffectTargets:${element.type}]`, err);
      }
    }
    return out;
  }, [elementTypesById, elements, stateVersion]);

  const effectCacheKey = useMemo(() => effectTargetCacheKey(effectTargets), [effectTargets]);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    setEffectError(null);
    if (effectTargets.length === 0) {
      setEffectManifest(null);
      setEffectLoading(false);
      return () => {
        cancelled = true;
        controller.abort();
      };
    }

    setEffectLoading(true);
    const win = window as WindowWithIdleCallback;
    let timeoutId: number | null = null;
    let idleId: number | null = null;
    const run = () => {
      void getOrCreateMain2DEffectManifest({
        compositionId,
        elements,
        elementTypesById,
        bounds,
        targets: effectTargets,
        signal: controller.signal,
      })
        .then((manifest) => {
          if (cancelled) return;
          setEffectManifest(manifest);
          setEffectLoading(false);
        })
        .catch((err) => {
          if (cancelled || controller.signal.aborted || isAbortError(err)) return;
          console.warn("[vector2d:effects]", err);
          setEffectError(err instanceof Error ? err.message : String(err));
          setEffectLoading(false);
        });
    };
    if (typeof win.requestIdleCallback === "function") idleId = win.requestIdleCallback(run, { timeout: 1000 });
    else timeoutId = window.setTimeout(run, 250);

    return () => {
      cancelled = true;
      controller.abort();
      if (idleId != null && typeof win.cancelIdleCallback === "function") win.cancelIdleCallback(idleId);
      if (timeoutId != null) window.clearTimeout(timeoutId);
    };
  }, [bounds, compositionId, effectCacheKey, elementTypesById, elements]);

  const effectOpacityById = useMemo(() => {
    const out = new Map<string, number>();
    for (const target of effectTargets) out.set(target.id, clamp(target.opacity ?? 1, 0, 1));
    return out;
  }, [effectTargets]);

  const handleWheel = useCallback(
    (event: React.WheelEvent) => {
      event.preventDefault();
      const container = containerRef.current;
      if (!container) return;

      const rect = container.getBoundingClientRect();
      const cursorX = event.clientX - rect.left;
      const cursorY = event.clientY - rect.top;
      const zoomSpeed = event.ctrlKey ? 0.0042 : 0.0024;
      const zoomFactor = Math.exp(-event.deltaY * zoomSpeed);

      scheduleTransform((prev) => {
        const baseMin = fitTransformRef.current.scale;
        const minScale = Math.max(0.05, baseMin * 0.5);
        const maxScale = Math.max(minScale * 1.5, baseMin * 10);
        const nextScale = clamp(prev.scale * zoomFactor, minScale, maxScale);
        const stageX = (cursorX - prev.x) / prev.scale;
        const stageY = (cursorY - prev.y) / prev.scale;
        return { scale: nextScale, x: cursorX - stageX * nextScale, y: cursorY - stageY * nextScale };
      });
    },
    [scheduleTransform],
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
    if (!container) return;
    try {
      container.releasePointerCapture(event.pointerId);
    } catch {
      // ignore
    }
  }, []);

  const handlePointerMove = useCallback((event: React.PointerEvent) => {
    if (!isPanningRef.current) return;
    const last = lastPointerRef.current;
    if (!last) return;
    const dx = event.clientX - last.x;
    const dy = event.clientY - last.y;
    lastPointerRef.current = { x: event.clientX, y: event.clientY };
    scheduleTransform((prev) => ({ ...prev, x: prev.x + dx, y: prev.y + dy }));
  }, [scheduleTransform]);

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

  const renderFallbackVector = (element: CompositionElement, key: string): React.ReactNode => (
    <g key={key} className="mainVector2dFallback">
      <circle cx={element.position.x} cy={element.position.z} r={0.16} />
      <title>{element.name || element.type}</title>
    </g>
  );

  const vectorElements = useMemo(
    () =>
      orderedElements.map((element, index) => {
        const key = `${element.id}:${index}`;
        const def = elementTypesById[element.type];
        if (!def?.renderMain2DVector) return renderFallbackVector(element, key);
        try {
          return <React.Fragment key={key}>{def.renderMain2DVector({ element, elements, ctx: { bounds, scale: 1 } })}</React.Fragment>;
        } catch (err) {
          console.warn(`[vector2d:renderMain2DVector:${element.type}]`, err);
          return renderFallbackVector(element, key);
        }
      }),
    [bounds, elementTypesById, orderedElements, stateVersion],
  );

  const notificationOverlay = useMemo(() => {
    if (!activeNotification || !activeNotificationRenderer?.create2DOverlay) return null;
    try {
      return activeNotificationRenderer.create2DOverlay(
        { compositionId },
        activeNotification,
        { openImage: () => undefined },
      );
    } catch (err) {
      console.warn(`[vector2d:create2DOverlay:${activeNotificationRenderer.id}]`, err);
      return null;
    }
  }, [activeNotification, activeNotificationRenderer, compositionId]);

  useEffect(() => {
    return () => {
      notificationOverlay?.dispose?.();
    };
  }, [notificationOverlay]);

  const notificationPin = useMemo(() => {
    if (!notificationOverlay) return null;
    const pinData = notificationOverlay.pin();
    if (!pinData) return null;

    const stage = projectWorldToStage({ x: pinData.x, z: pinData.z }, bounds, stageWidth, stageHeight);
    const screenX = transform.x + stage.x * transform.scale;
    const screenY = transform.y + stage.y * transform.scale;

    let trail: Array<{ x: number; y: number }> | undefined;
    if (pinData.trail && pinData.trail.length >= 2) {
      trail = pinData.trail.map((p) => {
        const s = projectWorldToStage({ x: p.x, z: p.z }, bounds, stageWidth, stageHeight);
        return { x: transform.x + s.x * transform.scale, y: transform.y + s.y * transform.scale };
      });
    }

    return { screenX, screenY, trail, priority: pinData.priority, closed: pinData.closed };
  }, [bounds, notificationOverlay, stageHeight, stageWidth, transform]);

  const vectorViewBox = useMemo(() => {
    const viewBox = computeMain2DVectorViewBox({
      bounds,
      stageWidth,
      stageHeight,
      viewportWidth: viewportSize.width,
      viewportHeight: viewportSize.height,
      transform,
    });
    const width = Math.max(1e-6, viewBox.maxX - viewBox.minX);
    const height = Math.max(1e-6, viewBox.maxZ - viewBox.minZ);
    return `${viewBox.minX} ${viewBox.minZ} ${width} ${height}`;
  }, [bounds, stageHeight, stageWidth, transform, viewportSize.height, viewportSize.width]);

  return (
    <div
      className="viewportRoot mainVector2dRoot"
      ref={containerRef}
      onWheel={handleWheel}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={stopPanning}
      onPointerCancel={stopPanning}
    >
      <div className="mainVector2dStage">
        <svg
          className="mainVector2dSvg"
          viewBox={vectorViewBox}
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <defs>
            <filter id="mainVector2dSoftShadow" x="-20%" y="-20%" width="140%" height="140%">
              <feDropShadow dx="0" dy="0.035" stdDeviation="0.04" floodColor="rgba(0,0,0,0.24)" />
            </filter>
            <pattern id="mainVector2dGrassPattern" patternUnits="userSpaceOnUse" width="0.34" height="0.34">
              <path d="M 0.05 0.30 L 0.11 0.08 M 0.18 0.32 L 0.22 0.12 M 0.29 0.28 L 0.31 0.05" stroke="rgba(5,46,22,0.36)" strokeWidth="0.012" strokeLinecap="round" />
              <path d="M 0.08 0.32 L 0.14 0.18 M 0.23 0.31 L 0.28 0.18" stroke="rgba(134,239,172,0.28)" strokeWidth="0.008" strokeLinecap="round" />
            </pattern>
            <pattern id="mainVector2dConcretePattern" patternUnits="userSpaceOnUse" width="0.42" height="0.42">
              <path d="M 0.04 0.10 H 0.12 M 0.28 0.06 H 0.36 M 0.18 0.30 H 0.31" stroke="rgba(15,23,42,0.20)" strokeWidth="0.009" strokeLinecap="round" />
              <circle cx="0.12" cy="0.28" r="0.012" fill="rgba(255,255,255,0.14)" />
              <circle cx="0.34" cy="0.20" r="0.009" fill="rgba(15,23,42,0.16)" />
            </pattern>
            <linearGradient id="mainVector2dWaterGradient" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" stopColor="rgba(186,230,253,0.50)" />
              <stop offset="45%" stopColor="rgba(14,165,233,0.28)" />
              <stop offset="100%" stopColor="rgba(3,105,161,0.46)" />
            </linearGradient>
            <pattern id="mainVector2dWaterPattern" patternUnits="userSpaceOnUse" width="0.52" height="0.30">
              <path d="M 0.02 0.17 C 0.12 0.08, 0.22 0.08, 0.32 0.17 S 0.48 0.26, 0.56 0.17" fill="none" stroke="rgba(224,242,254,0.34)" strokeWidth="0.012" strokeLinecap="round" />
              <path d="M -0.04 0.28 C 0.08 0.20, 0.18 0.20, 0.30 0.28 S 0.48 0.36, 0.60 0.28" fill="none" stroke="rgba(7,89,133,0.22)" strokeWidth="0.01" strokeLinecap="round" />
            </pattern>
          </defs>
          <g className="mainVector2dElements">{vectorElements}</g>
          {effectManifest ? (
            <g className="mainVector2dEffects">
              {effectManifest.effects.map((effect) => {
                const opacity = effectOpacityById.get(effect.id) ?? 0;
                return (
                  <image
                    key={effect.id}
                    href={effect.url}
                    x={effect.x}
                    y={effect.z}
                    width={effect.width}
                    height={effect.height}
                    preserveAspectRatio="none"
                    opacity={opacity}
                    style={effect.blendMode === "screen" ? { mixBlendMode: "screen" } : undefined}
                  />
                );
              })}
            </g>
          ) : null}
        </svg>
      </div>

      <div className="main2dButtonsOverlay">
        {notificationPin ? (
          <Notification2DPinView
            screenX={notificationPin.screenX}
            screenY={notificationPin.screenY}
            priority={notificationPin.priority}
            closed={notificationPin.closed}
            trail={notificationPin.trail}
          />
        ) : null}
        {markerEntries.map((entry) => {
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
                  setClusterModalMarkers(entry.markers.map(({ screenX: _screenX, screenY: _screenY, ...rest }) => rest));
                }}
              >
                <Icon name="layer-group" />
                <span className="main2dMarkerClusterCount">{entry.markers.length}</span>
              </button>
            );
          }
          return (
            <button
              key={entry.id}
              className={[
                "main2dMarkerButton",
                entry.className ?? "",
                entry.state === "on" ? "isOn" : "",
                entry.state === "off" ? "isOff" : "",
                entry.state === "unknown" ? "isUnknown" : "",
              ]
                .filter(Boolean)
                .join(" ")}
              type="button"
              title={entry.title}
              style={{ left: entry.screenX, top: entry.screenY }}
              onPointerDown={(e) => e.stopPropagation()}
              onClick={() => triggerClick(entry.elementId)}
              onDoubleClick={() => triggerDoubleClick(entry.elementId)}
            >
              <Icon name={entry.icon || "circle-dot"} />
            </button>
          );
        })}
      </div>

      {(effectLoading || effectError) && effectTargets.length > 0 ? (
        <div className="mainVector2dStatus">
          {effectError ? t("core.ui.main2d.vector.effects_error", {}, "Some effects could not be rendered.") : t("core.ui.main2d.vector.effects_loading", {}, "Preparing effects...")}
        </div>
      ) : null}

      <Modal
        open={Boolean(clusterModalMarkers)}
        title={t(
          "core.ui.main2d.cluster.title",
          { count: clusterModalMarkers?.length ?? 0 },
          `Multiple items (${clusterModalMarkers?.length ?? 0})`,
        )}
        onClose={() => setClusterModalMarkers(null)}
      >
        <div className="main2dClusterList">
          {(clusterModalMarkers ?? []).map((item) => (
            <div key={item.id} className="main2dClusterRow">
              <button
                className={[
                  "main2dClusterPrimary",
                  item.state === "on" ? "isOn" : "",
                  item.state === "off" ? "isOff" : "",
                  item.state === "unknown" ? "isUnknown" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                type="button"
                onClick={() => onElementActivated?.(item.elementId, "click")}
              >
                <span className="main2dClusterIcon">
                  <Icon name={item.icon || "circle-dot"} />
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
                  setClusterModalMarkers(null);
                  onElementActivated?.(item.elementId, "dblclick");
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
