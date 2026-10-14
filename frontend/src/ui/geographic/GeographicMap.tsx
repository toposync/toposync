import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as L from "leaflet";
import "leaflet/dist/leaflet.css";
import type { ElementType, Notification, NotificationRenderer } from "@toposync/plugin-api";
import { getGeographicMapConfiguration, type Composition, type GeographicMapConfiguration } from "../../util/api";
import { geographicToLocal, localToGeographic, longitudeNear, readGeoreference, type Georeference } from "../../util/georeference";
import { i18n } from "../../util/i18n";
import { computeMain2DBounds } from "../main2d/shared";
import { geographicElementLabel } from "./mapPresentation";
import { Icon } from "../Icon";
import { Modal } from "../Modal";
import { GeographicCompositionLayer, type GeographicTarget } from "./GeographicCompositionLayer";
import { MapNavigation } from "./MapNavigation";
import { useMapConsent } from "./mapConsent";
import { geographicViewport } from "./projection";

type MapView = { center: [number, number]; zoom: number };
export type GeographicView = MapView & { compositionId: string; target?: MapView & { notificationId: string | null } };

export function GeographicMap({ viewState, editing = false, compositions, activeCompositionId, elementTypesById, notifications, notificationRenderers, activeNotificationId, onAnchorChange, onActivate, onNotification, onImage }: {
  viewState?: React.MutableRefObject<GeographicView | null>; editing?: boolean;
  compositions: Composition[]; activeCompositionId: string; elementTypesById: Record<string, ElementType>;
  notifications: Notification[]; notificationRenderers: NotificationRenderer[]; activeNotificationId?: string | null;
  onAnchorChange?: (point: { latitude: number; longitude: number }) => void;
  onActivate?: (target: GeographicTarget) => void; onNotification?: (id: string) => void;
  onImage?: (args: { url: string; title?: string; subtitle?: string }) => void;
}): React.ReactElement {
  const { t } = i18n.useI18n();
  const saved = useRef(viewState?.current?.compositionId === activeCompositionId ? viewState.current : null);
  const [selection, setSelection] = useState({ compositionId: activeCompositionId, notificationId: saved.current?.target ? saved.current.target.notificationId : activeNotificationId ?? null });
  const previousNotificationId = useRef(activeNotificationId);
  const previousCompositionId = useRef(activeCompositionId);
  useEffect(() => {
    if (previousCompositionId.current !== activeCompositionId) {
      previousCompositionId.current = activeCompositionId;
      previousNotificationId.current = activeNotificationId;
      setSelection({ compositionId: activeCompositionId, notificationId: null });
      return;
    }
    if (previousNotificationId.current === activeNotificationId) return;
    previousNotificationId.current = activeNotificationId;
    setSelection({ compositionId: activeCompositionId, notificationId: activeNotificationId ?? null });
  }, [activeNotificationId, activeCompositionId]);
  const targetNotificationId = previousCompositionId.current === activeCompositionId && selection.compositionId === activeCompositionId ? selection.notificationId : null;
  const target = useRef(saved.current?.target ?? null);
  const lastFitted = useRef(saved.current ? `${activeCompositionId}:${targetNotificationId ?? ""}` : null);
  const applyingView = useRef(false);
  const manualMovement = useRef(false);
  const [away, setAway] = useState(false);
  const options = useRef<HTMLDetailsElement>(null);
  const container = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<L.Map | null>(null);
  const [configuration, setConfiguration] = useState<GeographicMapConfiguration | null>(null);
  const [error, setError] = useState("");
  const [permissionOpen, setPermissionOpen] = useState(false);
  const [targets, setTargets] = useState<GeographicTarget[]>([]);
  const anchorChange = useRef(onAnchorChange);
  anchorChange.current = onAnchorChange;
  const consent = useMapConsent(configuration ? `background:${configuration.background.id}:${configuration.background.url}` : "unconfigured");
  const positioned = useMemo(() => compositions.flatMap(composition => {
    const reference = readGeoreference(composition.meta?.georeference);
    return reference ? [{ composition, reference }] : [];
  }), [compositions]);
  useEffect(() => {
    const controller = new AbortController();
    getGeographicMapConfiguration(controller.signal).then(setConfiguration).catch(() => { if (!controller.signal.aborted) setError("core.map.configuration_failed"); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!container.current) return;
    const reference = positioned.find(item => item.composition.id === activeCompositionId)?.reference ?? positioned[0]?.reference;
    const restored = saved.current;
    const instance = L.map(container.current, { zoomControl: false, maxZoom: 22, minZoom: 1, zoomAnimation: false, markerZoomAnimation: false })
      .setView(restored?.center ?? (reference ? [reference.latitude, reference.longitude] : [0, 0]), restored?.zoom ?? (reference ? 19 : 2));
    if (editing) L.control.zoom({ position: "bottomleft", zoomInTitle: t("core.map.zoom_in"), zoomOutTitle: t("core.map.zoom_out") }).addTo(instance);
    L.control.scale({ imperial: false, position: "bottomright" }).addTo(instance);
    setMap(instance);
    const observer = new ResizeObserver(() => {
      applyingView.current = true;
      instance.invalidateSize({ debounceMoveend: false });
      applyingView.current = false;
    });
    observer.observe(container.current);
    return () => { observer.disconnect(); instance.remove(); };
  }, []);
  const targetBounds = useCallback(() => {
    const bounds = L.latLngBounds([]);
    const active = positioned.find(item => item.composition.id === activeCompositionId);
    const longitude = active?.reference.longitude ?? positioned[0]?.reference.longitude ?? 0;
    const extend = (point: { x: number; z: number }, reference: Georeference) => {
      try {
        const location = localToGeographic(point, reference);
        bounds.extend([location.lat, longitudeNear(location.lng, longitude)]);
      } catch { /* Invalid evidence cannot extend the target bounds. */ }
    };
    const notification = notifications.find(item => item.id === targetNotificationId);
    const renderer = notificationRenderers.find(item => item.type === notification?.type);
    if (notification) for (const { composition, reference } of positioned) {
      let overlay: import("@toposync/plugin-api").Notification2DOverlay | null | undefined;
      try {
        overlay = renderer?.create2DOverlay?.({ compositionId: composition.id, elements: composition.elements }, notification, { openImage: () => undefined });
        const pin = overlay?.pin();
        if (pin) for (const point of [pin, ...(pin.trails ?? (pin.trail ? [pin.trail] : [])).flat()]) extend(point, reference);
      } catch { /* One unavailable composition must not block the others. */ }
      finally { overlay?.dispose?.(); }
    }
    if (bounds.isValid()) return { bounds, maxZoom: 21 };
    if (active) {
      const local = computeMain2DBounds(active.composition.elements, elementTypesById);
      for (const point of [{ x: local.minX, z: local.minZ }, { x: local.maxX, z: local.maxZ }, { x: local.minX, z: local.maxZ }, { x: local.maxX, z: local.minZ }, { x: 0, z: 0 }]) extend(point, active.reference);
    }
    return { bounds, maxZoom: 22 };
  }, [positioned, activeCompositionId, elementTypesById, notifications, notificationRenderers, targetNotificationId]);
  const recenter = useCallback(() => {
    if (!map) return false;
    const { bounds, maxZoom } = targetBounds();
    if (!bounds.isValid()) { target.current = null; setAway(false); return false; }
    applyingView.current = true;
    map.fitBounds(bounds, { padding: [56, 56], maxZoom, animate: false });
    const center = map.getCenter();
    target.current = { center: [center.lat, center.lng], zoom: map.getZoom(), notificationId: targetNotificationId };
    applyingView.current = false;
    manualMovement.current = false;
    setAway(false);
    if (viewState) viewState.current = { compositionId: activeCompositionId, center: target.current.center, zoom: target.current.zoom, target: target.current };
    return true;
  }, [map, targetBounds, targetNotificationId, activeCompositionId, viewState]);
  useEffect(() => {
    const key = `${activeCompositionId}:${targetNotificationId ?? ""}`;
    if (!map || lastFitted.current === key) return;
    if (recenter()) lastFitted.current = key;
  }, [map, activeCompositionId, targetNotificationId, targetBounds, recenter]);
  useEffect(() => {
    if (!map) return;
    const updateAway = () => {
      const reference = target.current;
      setAway(Boolean(reference && (map.getZoom() !== reference.zoom || map.project(map.getCenter()).distanceTo(map.project(reference.center)) > 24)));
    };
    const remember = () => {
      const center = map.getCenter();
      if (viewState) viewState.current = { compositionId: activeCompositionId, center: [center.lat, center.lng], zoom: map.getZoom(), target: target.current ?? undefined };
      if (manualMovement.current && !applyingView.current) { updateAway(); manualMovement.current = false; }
    };
    const movedManually = () => { if (!applyingView.current) manualMovement.current = true; };
    const keyboard = (event: KeyboardEvent) => { if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) movedManually(); };
    updateAway();
    map.on("dragstart boxzoomstart zoomstart", movedManually);
    map.on("moveend", remember);
    map.getContainer().addEventListener("keydown", keyboard);
    return () => {
      map.off("dragstart boxzoomstart zoomstart", movedManually); map.off("moveend", remember);
      map.getContainer().removeEventListener("keydown", keyboard);
    };
  }, [map, activeCompositionId, viewState]);
  useEffect(() => {
    if (!map || !configuration || consent.decision !== true) return;
    const background = configuration.background;
    const layer = L.tileLayer(background.url, { attribution: background.attribution, maxNativeZoom: background.nativeZoom, maxZoom: 22, updateWhenIdle: true, keepBuffer: 0 });
    setError(previous => previous === "core.map.offline" ? "" : previous);
    layer.on("tileerror", () => setError("core.map.offline"));
    layer.on("tileload", () => setError(previous => previous === "core.map.offline" ? "" : previous));
    layer.addTo(map);
    return () => { layer.remove(); layer.off(); setError(previous => previous === "core.map.offline" ? "" : previous); };
  }, [map, configuration, consent.decision]);
  useEffect(() => {
    if (!map || !onAnchorChange) return;
    const reference = positioned[0]?.reference;
    if (!reference) return;
    const marker = L.marker([reference.latitude, reference.longitude], { draggable: true, keyboard: true, title: t("core.map.drag"),
      icon: L.divIcon({ html: '<i class="fa-solid fa-location-crosshairs" aria-hidden="true"></i>', className: "geographicAnchor", iconSize: [44, 44], iconAnchor: [22, 22] }) }).addTo(map);
    marker.on("dragend", () => { const point = marker.getLatLng().wrap(); anchorChange.current?.({ latitude: Math.max(-85, Math.min(85, point.lat)), longitude: point.lng }); });
    return () => { marker.remove(); marker.off(); };
  }, [map, positioned, Boolean(onAnchorChange), t]);
  useEffect(() => {
    if (!map) return;
    const click = (event: L.LeafletMouseEvent) => {
      if (anchorChange.current) { const point = event.latlng.wrap(); anchorChange.current({ latitude: Math.max(-85, Math.min(85, point.lat)), longitude: point.lng }); return; }
      if (!onActivate) return;
      const selected: GeographicTarget[] = [];
      for (const { composition, reference } of positioned) {
        try {
          const world = geographicToLocal({ lat: event.latlng.lat, lng: event.latlng.wrap().lng }, reference);
          const viewport = geographicViewport(map, reference, document.createElement("canvas"));
          for (const element of [...composition.elements].reverse())
            if (elementTypesById[element.type]?.hitTest2D?.({ element, world, viewport })) selected.push({ compositionId: composition.id, elementId: element.id });
        } catch { /* Clicks outside a composition's domain do not target it. */ }
      }
      if (selected.length > 1) setTargets(selected); else if (selected[0]) onActivate(selected[0]);
    };
    map.on("click", click);
    return () => { map.off("click", click); };
  }, [map, positioned, elementTypesById, onActivate]);
  return <div className="geographicMapRoot" data-testid="geographic-map">
    {editing && configuration && map && <div className="geographicToolbar"><MapNavigation map={map} configuration={configuration} onHome={recenter} /></div>}
    <div className="geographicMapStage">
      <div ref={container} className="geographicLeaflet" aria-label={t("core.map.aria")} />
      {!editing && away && <button type="button" className="chipButton geographicRecenter" onClick={recenter}><Icon name="location-crosshairs" /> {t("core.map.recenter")}</button>}
      {configuration && <details ref={options} className="geographicOptions" onKeyDown={event => {
        if (event.key === "Escape") { event.stopPropagation(); if (options.current) { options.current.open = false; options.current.querySelector("summary")?.focus(); } }
      }}>
        <summary aria-label={t("core.map.options")} title={t("core.map.options")}><Icon name="ellipsis" /></summary>
        <button type="button" className="chipButton" onClick={() => {
          if (consent.decision === true) consent.choose(false); else setPermissionOpen(true);
          if (options.current) { options.current.open = false; options.current.querySelector("summary")?.focus(); }
        }}>{t(consent.decision === true ? "core.map.revoke_background" : "core.map.allow_background")}</button>
        {!consent.persistent && <p role="status">{t("core.map.permission_session")}</p>}
      </details>}
      {error && <p className="geographicError" role="status">{t(error)}</p>}
      {map && positioned.map(({ composition, reference }) => <GeographicCompositionLayer key={composition.id} map={map} composition={composition} reference={reference}
        elementTypesById={elementTypesById} notifications={notifications} notificationRenderers={notificationRenderers} activeNotificationId={activeNotificationId}
        selected={composition.id === activeCompositionId} onChooseElements={setTargets} onActivate={onActivate} onNotification={onNotification} onImage={onImage} />)}
      {editing && !positioned.length && <p className="geographicEmpty" role="status">{t("core.map.unpositioned")}</p>}
    </div>
    <Modal open={Boolean(configuration && (consent.decision === null || permissionOpen))} title={t("core.map.background")} onClose={() => { consent.choose(false); setPermissionOpen(false); }}>
      <p>{t("core.map.background_consent", { provider: configuration?.background.name ?? "" })}</p>
      <div className="rowWrap">
        <button type="button" className="primaryButton" onClick={() => { consent.choose(true); setPermissionOpen(false); setError(""); }}>{t("core.map.allow_background")}</button>
        <button type="button" className="chipButton" onClick={() => { consent.choose(false); setPermissionOpen(false); }}>{t("core.map.without_background")}</button>
      </div>
    </Modal>
    <Modal open={targets.length > 0} title={t("core.map.choose_element")} onClose={() => setTargets([])}>
      <div className="geographicChoices">{targets.map(target => {
        const owner = compositions.find(item => item.id === target.compositionId);
        const element = owner?.elements.find(item => item.id === target.elementId);
        return <button className="chipButton" type="button" key={`${target.compositionId}:${target.elementId}`} onClick={() => { setTargets([]); onActivate?.(target); }}>
          <strong>{element ? geographicElementLabel(element, elementTypesById[element.type], t) : t("core.map.element")}</strong>
          <span className="muted">{owner?.name}{element && elementTypesById[element.type] ? ` · ${typeof elementTypesById[element.type].name === "string" ? elementTypesById[element.type].name : t((elementTypesById[element.type].name as { key: string }).key)}` : ""}</span>
        </button>;
      })}</div>
    </Modal>
  </div>;
}
